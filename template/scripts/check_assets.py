#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""promo-forge · 阶段 9「校验闸门」之渲染前检查（check_assets.py）。

用途
----
渲染**之前**把能查的都查掉，避免白渲一遍才发现缺图/缺字/没混音。检查项：

1. **素材引用**：扫 ``src/**/*.ts,tsx`` 与 ``web/**/*.ts,tsx``（先挖掉注释，避免把示例代码
   当成真实引用），抓出 ``staticFile("...")``、``assetUrl("...")`` 与 ``src="..."`` 三类字面量，
   检查 ``<项目根>/public/<路径>`` 是否存在；顺带查 ``timing.json`` 的 ``brand.logo``
   与 ``fonts.json`` 的 ``roles.*.file``；
2. **timing.json 结构**：场景首尾相接、``duration`` = 最后场景 end、``music.beat/bar > 0``、
   每句旁白的 ``at`` 落在所属场景窗口内；
3. **旁白字数**：按 6.5 字/秒预估，超窗口的给出「删掉 N 个字」的建议（WARN，AGENTS.md 阶段 7）；
4. **图片尺寸**：``public/img/`` 下宽度 < 1000px 会发虚、> 3000px 吃内存（WARN）；
5. **字体缺字**：用 ``fonts.json`` 的 ``check.systemCandidates`` 找第一个存在的字体，
   检查 timing.json 全部文案的每个字符是否都被 cmap 覆盖，缺字 → ERROR（会渲染成方块）；
6. **音频**：``public/<music.file>`` 与 ``audio-src/vo/<id>.mp3`` 是否齐全；
7. **真实素材配额**：``public/img/`` 图片数 < 6 → WARN（AGENTS.md 阶段 4 要求 ≥6 张）；
   总数达标后再按场景分段核「每个功能段落 ≥1 张真实截图」「有一段 ≥6 张真实作品的快切蒙太奇」
   （用了 ``PhoneFrame/BrowserFrame/Shot`` 的段落一张真实图都没引用 → WARN；变量拼路径的段落不下结论）。

典型调用
--------
    python scripts/check_assets.py            # 渲染前闸门
    python scripts/check_assets.py --strict   # 把所有 WARN 也当 ERROR（交付前严格模式）

依赖
----
必需：无（纯标准库也能跑，缺 Pillow/fontTools 时对应检查降级为 WARN）。
可选：Pillow（图片尺寸）、fontTools（字体缺字）。

退出码
------
0 通过（0 ERROR）；1 有 ERROR。输出格式固定为
``OK/WARN/ERROR: <检查项> — <说明>``，最后一行 ``检查完成：X ERROR / Y WARN``。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# 导入公共库 scripts/_common.py（由另一位维护者提供，本脚本不修改它）
# ---------------------------------------------------------------------------
sys.path.insert(0, str(Path(__file__).resolve().parent))
try:  # pragma: no cover - 只在 _common.py 缺失时走到
    from _common import (  # type: ignore
        PROJECT_ROOT,
        error,
        human,
        line_window,
        load_timing,
        log,
        scene_of,
        warn,
    )
except ImportError as exc:  # pragma: no cover
    sys.stderr.write(
        "ERROR: 无法导入 scripts/_common.py（%s）。请确认 scripts 目录完整，"
        "并在项目根目录下运行本脚本。\n" % (exc,)
    )
    raise SystemExit(1)


# ===========================================================================
# 阈值常量（每条都对应 AGENTS.md 的一处硬性要求）
# ===========================================================================

# AGENTS.md 阶段 7：语速 5.5~6.5 字/秒，这里按上限 6.5 保守预估。
CHARS_PER_SEC = 6.5

# AGENTS.md 阶段 9 / 第 7 节：图片宽度 ≥ 显示宽度 ×2（1080 宽画布 → ≥2160 才不虚）。
# 这里把「明显发虚」定义在 1000px 以下，把「太吃内存」定义在 3000px 以上。
IMG_MIN_WIDTH = 1000
IMG_MAX_WIDTH = 3000

# AGENTS.md 阶段 4：片长 ≤45 秒时全片真实素材 ≥6 张（>45 秒 ≥8 张）。
IMG_MIN_COUNT_SHORT = 6
IMG_MIN_COUNT_LONG = 8
SHORT_FILM_S = 45.0

# 场景衔接、duration 的容差（timing.json 里都是浮点秒）。
TIME_EPS = 1e-6

# 引用 public/ 素材的三种写法，全部要抓：
#   1. staticFile("img/x.png")  —— Remotion 官方写法
#   2. assetUrl("img/x.png")    —— 本模板 src/assets.ts 的等价助手（网页版也用它）
#   3. src="img/x.png"          —— 组件里直接写相对 public/ 的字符串
# 只认字面量；变量/模板字符串一律跳过并提示（静态扫不出来，别假装扫过了）。
ASSET_CALL_RE = re.compile(r"""\b(staticFile|assetUrl)\s*\(\s*(['"])([^'"]*)\2\s*\)""")
ASSET_PROP_RE = re.compile(r"""\b(?:src|poster|image)\s*=\s*(["'])([^"'{}]+)\1""")
# 场景组件里"裸字符串"形式的素材引用（数组 / 变量 / props 都算）：
#   const WORKS = ["img/work-01.png", …]  或  <Shot src="img/work-01.png" />
# 用来统计每个场景到底引用了几张真实图（「分段素材」检查）。
SCENE_LITERAL_RE = re.compile(r"""(["'])([^"'\\\n]+)\1""")
# 静态扫不出来的写法：素材 prop 里放的是表达式（`src={…}`），或 staticFile()/assetUrl()
# 的实参不是字面量。出现这些就不对这一段下结论（不猜）。
# 注意别把「样式里的模板字符串」当成动态素材引用：场景代码里到处都是 `blur(${p}px)` 这类写法，
# 那跟素材路径无关，把它当动态会让整项检查永远不敢下结论。
DYNAMIC_ASSET_RE = re.compile(
    r"""\b(?:src|poster|image)\s*=\s*\{|(?:staticFile|assetUrl)\s*\(\s*[^"')\s]"""
)
# 组件化的"素材容器"：用了它们就说明这一段要展示真实界面/作品（AGENTS.md 阶段 4/6）。
MATERIAL_COMPONENTS = ("PhoneFrame", "BrowserFrame", "Shot")
# AGENTS.md 阶段 4：必须有一段由 ≥6 张真实作品组成的快切蒙太奇。
MONTAGE_MIN = 6
REMOTE_PREFIXES = ("http://", "https://", "data:", "blob:", "//")

SCAN_DIRS = ("src", "web")
SCAN_SUFFIXES = (".ts", ".tsx")
IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".webp", ".avif", ".bmp", ".gif", ".tif", ".tiff")
# 凡是这些后缀的字面量，都当成"public/ 下的素材"来查存在性。
ASSET_SUFFIXES = IMAGE_SUFFIXES + (
    ".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac",
    ".mp4", ".webm", ".mov", ".m4v",
    ".woff", ".woff2", ".ttf", ".otf",
)
# 这些目录开头的字面量一定在 public/ 下（模板约定：img/ audio/ fonts/）。
ASSET_DIR_PREFIXES = ("img/", "audio/", "fonts/", "video/", "assets/")
# 这些开头的是源码/依赖路径，不是 public/ 素材。
SOURCE_DIR_PREFIXES = ("src/", "web/", "scripts/", "node_modules/", "out/", "@")
SKIP_DIR_NAMES = {"node_modules", ".git", "dist", "build"}

# fonts.json 的 roles.<role>.file 是相对 public/ 的字体文件（src/lib/fonts.ts 用 staticFile 加载）。
FONT_ROLES = ("display", "body", "mono")


# ===========================================================================
# 结果收集
# ===========================================================================

class Report:
    """收集 OK/WARN/ERROR 三类结果，最后统一统计与打印。"""

    def __init__(self, strict: bool):
        self.strict = strict
        self.errors = 0
        self.warnings = 0
        self.rows = []

    def ok(self, item: str, message: str) -> None:
        self._emit("OK", item, message)

    def warn(self, item: str, message: str) -> None:
        if self.strict:
            self.errors += 1
            self._emit("ERROR", item, message + "（--strict：WARN 升级为 ERROR）")
        else:
            self.warnings += 1
            self._emit("WARN", item, message)

    def fail(self, item: str, message: str) -> None:
        self.errors += 1
        self._emit("ERROR", item, message)

    def _emit(self, level: str, item: str, message: str) -> None:
        line = f"{level}: {item} — {message}"
        self.rows.append(line)
        stream = sys.stderr if level == "ERROR" else sys.stdout
        stream.write(line + "\n")
        stream.flush()


def _force_utf8() -> None:
    """Windows 控制台默认 GBK，中文会炸；这里兜底成 UTF-8。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except Exception:
            pass


def _rel(path: Path) -> str:
    try:
        return path.resolve().relative_to(PROJECT_ROOT.resolve()).as_posix()
    except Exception:
        return Path(path).as_posix()


# `_read_json_file` 的错误分类哨兵：
#   文件**不存在** = "没配"（可选文件，调用方一般 WARN）；
#   其余错误（不是 UTF-8 / 读不出来 / JSON 语法坏）= "配坏了"，调用方必须报 ERROR ——
#   闸门不能因为一个坏文件就静默跳过一整项检查（那就是假绿）。
JSON_MISSING = "文件不存在"


def _read_json_file(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8")), None
    except FileNotFoundError:
        return None, JSON_MISSING
    except UnicodeDecodeError as exc:
        # 以前这里会裸崩：Windows 上把 JSON 存成 GBK（记事本默认编码）就会走到这。
        return None, f"不是 UTF-8 文本（第 {exc.start} 字节起：{exc.reason}）"
    except json.JSONDecodeError as exc:
        return None, f"JSON 解析失败：{exc}"
    except OSError as exc:
        return None, f"读不了文件：{exc}"


def _obj(value):
    """只归一化、不报错的版本：坏输入（非对象）→ 空 dict。

    给没有 report 参数的工具函数用（_collect_texts / _local_window 这类）；
    真正的 `ERROR:` 行由对应的检查项报（见 _sub_dict），避免同一处坏输入刷出四条。
    """
    return value if isinstance(value, dict) else {}


def _sub_dict(container: dict, key: str, report: Report, item: str, expected: str) -> dict:
    """取 timing.json 的子对象（brand/captions/music/voice…）之前先做类型断言。

    契约里它们都是对象；坏输入（字符串、列表、数字）以前会在最深的 `.get()` 上
    AttributeError 裸崩，用户看到的是 traceback 而不是 `ERROR: ...` 行。
    这里统一拦住：不是对象就报一行 ERROR 并返回空 dict，让后面的检查继续跑
    （一个字段坏掉不该让整项闸门消失，也不该顺手掩盖别的问题）。
    """
    value = container.get(key)
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    report.fail(
        item,
        f"{key} 应该是对象（{expected}），现在是 {type(value).__name__}：{value!r}",
    )
    return {}


def _positive_number(value):
    """把数字字段转成 float；非数字 / 非正数 / NaN / Inf 一律 None（调用方报 ERROR）。"""
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not 0 < number < float("inf"):
        return None
    return number


# ===========================================================================
# 1. 素材引用（staticFile / assetUrl / src="..."）
# ===========================================================================

def _strip_ts_comments(text: str) -> str:
    """把 TS/TSX 里的注释挖掉（保留换行，行号不变）。

    必须做这一步：模板的场景文件里大量示例代码写在注释里
    （例如 ``*   <Shot src="img/work-01.png" />``），不挖掉会把示例当真实引用误报缺图。
    """
    out = []
    index = 0
    length = len(text)
    state = None  # None | "'" | '"' | '`' | "//" | "/*"
    while index < length:
        char = text[index]
        nxt = text[index + 1] if index + 1 < length else ""
        if state is None:
            if char == "/" and nxt == "/":
                state = "//"
                index += 2
                continue
            if char == "/" and nxt == "*":
                state = "/*"
                index += 2
                continue
            if char in "\"'`":
                state = char
            out.append(char)
            index += 1
            continue
        if state == "//":
            if char == "\n":
                state = None
                out.append(char)
            index += 1
            continue
        if state == "/*":
            if char == "*" and nxt == "/":
                state = None
                index += 2
                continue
            if char == "\n":
                out.append(char)
            index += 1
            continue
        # 在字符串里：原样保留，这样正则还能看到 staticFile("img/x.png")
        if char == "\\" and state != "`":
            out.append(char)
            out.append(nxt)
            index += 2
            continue
        if char == state:
            state = None
        out.append(char)
        index += 1
    return "".join(out)


def _looks_like_public_asset(value: str) -> bool:
    """判断一个字面量是不是"相对 public/ 的素材路径"。"""
    text = value.strip().replace("\\", "/")
    if not text or "{" in text or "$" in text:
        return False
    if text.startswith(REMOTE_PREFIXES):
        return False
    lowered = text.lstrip("/")
    while lowered.startswith("./"):
        lowered = lowered[2:]
    if not lowered:
        return False
    if lowered.startswith(SOURCE_DIR_PREFIXES):
        return False
    if lowered.lower().endswith(ASSET_SUFFIXES):
        return True
    return lowered.startswith(ASSET_DIR_PREFIXES)


def _collect_asset_refs():
    """扫 src/ 与 web/ 下的 TS/TSX，返回 (引用列表, 扫描文件数, 静态扫不出来的行)。"""
    references = []
    opaque = []
    scanned = 0
    for sub in SCAN_DIRS:
        base = PROJECT_ROOT / sub
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*")):
            if not path.is_file() or path.suffix.lower() not in SCAN_SUFFIXES:
                continue
            if any(part in SKIP_DIR_NAMES for part in path.parts):
                continue
            scanned += 1
            try:
                raw = path.read_text(encoding="utf-8", errors="replace")
            except OSError as exc:
                opaque.append((path, 0, f"读不了这个文件：{exc}"))
                continue
            text = _strip_ts_comments(raw)
            for lineno, line in enumerate(text.splitlines(), start=1):
                if (
                    "staticFile" not in line
                    and "assetUrl" not in line
                    and "src=" not in line
                    and not ASSET_PROP_RE.search(line)
                ):
                    continue
                # 变量/拼接写法：静态扫不出来，明确提示而不是假装通过
                if "staticFile(" in line and not ASSET_CALL_RE.search(line):
                    opaque.append((path, lineno, line.strip()[:80]))
                if "assetUrl(" in line and not ASSET_CALL_RE.search(line):
                    opaque.append((path, lineno, line.strip()[:80]))
                for _fn, _quote, value in ASSET_CALL_RE.findall(line):
                    if _looks_like_public_asset(value):
                        references.append((path, lineno, value.strip()))
                for _quote, value in ASSET_PROP_RE.findall(line):
                    if _looks_like_public_asset(value):
                        references.append((path, lineno, value.strip()))
    return references, scanned, opaque


def check_asset_refs(report: Report) -> None:
    item = "素材引用"
    references, scanned, opaque = _collect_asset_refs()

    # 变量/模板字符串拼出来的路径静态扫不到。它们是框架级管道
    # （Promo.tsx 挂 timing.music.file、lib/fonts.ts 挂 fonts.json 的 file、
    #  web/App.tsx 预取 ASSETS），已经分别由"混音文件"与"品牌与字体文件"两项检查覆盖，
    # 所以这里只做提示，不报 WARN —— 否则每次跑都会刷出噪音，把 --strict 变成不可用。
    for path, lineno, snippet in opaque:
        where = f"{_rel(path)}:{lineno}" if lineno else _rel(path)
        log(f"  动态引用（静态扫不到，已由其它检查覆盖）：{where}  {snippet}")

    note = f"另有 {len(opaque)} 处变量拼接的引用由其它检查覆盖" if opaque else ""

    if not references:
        report.warn(
            item,
            f"扫了 {scanned} 个 TS/TSX，没发现 staticFile(\"...\") / assetUrl(\"...\") / src=\"...\" 形式的"
            f"字面量素材引用{('，' + note) if note else ''}。"
            "如果片子本来就要用真实截图（AGENTS.md 阶段 4 要求 ≥6 张），"
            "请确认场景里确实引用了 public/img/ 下的文件",
        )
        return

    missing = []
    for path, lineno, value in references:
        candidate = PROJECT_ROOT / "public" / value.lstrip("/").replace("\\", "/")
        if not candidate.is_file():
            missing.append((path, lineno, value, candidate))

    unique = sorted({value for _, _, value in references})
    if missing:
        for path, lineno, value, candidate in missing:
            report.fail(
                item,
                f"{_rel(path)}:{lineno} 引用 \"{value}\"，但 {_rel(candidate)} 不存在"
                "（渲染时 Remotion 会报 failed to load，画面直接缺一块）",
            )
        report.fail(
            item,
            f"{len(references)} 处引用里有 {len(missing)} 处指向不存在的文件",
        )
        return

    report.ok(
        item,
        f"扫描 {scanned} 个 TS/TSX，{len(references)} 处素材引用覆盖 {len(unique)} 个文件，"
        f"全部存在于 public/{('；' + note) if note else ''}",
    )


def check_brand_and_font_files(report: Report, timing: dict) -> None:
    """timing.json 的 brand.logo 与 fonts.json 的 roles.*.file 也是 public/ 下的素材。"""
    item = "品牌与字体文件"
    problems = []

    brand = _sub_dict(timing, "brand", report, item, "{name, url, logo, colors}")
    logo = str(brand.get("logo") or "").strip()
    if logo:
        candidate = PROJECT_ROOT / "public" / logo.lstrip("/")
        if not candidate.is_file():
            problems.append(
                f"timing.json 的 brand.logo=\"{logo}\" 指向 {_rel(candidate)}，但文件不存在"
                "（场景里 staticFile(空文件) 会 404；不确定就先在 timing.json 里留空字符串）"
            )

    fonts_path = PROJECT_ROOT / "fonts.json"
    fonts_cfg, err = _read_json_file(fonts_path)
    roles_checked = 0
    if fonts_cfg is None:
        if err == JSON_MISSING:
            report.warn(item, f"读不到 fonts.json（{err}），跳过字体文件检查")
        else:
            report.fail(
                item,
                f"fonts.json 存在但读不出来（{err}）：这一项不能跳过 —— 角色的字体文件不查，"
                "等于没查「字体 404 / 回退系统字体」，修好这个文件再重跑",
            )
        return
    for role in FONT_ROLES:
        role_cfg = (fonts_cfg.get("roles") or {}).get(role) or {}
        file_value = str(role_cfg.get("file") or "").strip()
        if not file_value:
            continue
        roles_checked += 1
        candidate = PROJECT_ROOT / "public" / file_value.lstrip("/")
        if not candidate.is_file():
            problems.append(
                f"fonts.json 的 roles.{role}.file=\"{file_value}\" 指向 {_rel(candidate)}，但文件不存在"
                "（@remotion/fonts 会加载失败并回退系统字体，同一行里可能混出两种字形；"
                "新克隆没有子集字体是正常的，跑 `python scripts/fonts.py subset` 生成）"
            )

    if problems:
        for text in problems:
            report.warn(item, text)
        return
    if logo or roles_checked:
        report.ok(
            item,
            f"brand.logo 与 {roles_checked} 个本地字体文件都指向真实文件"
            if roles_checked else "brand.logo 指向真实文件（fonts.json 未指定本地字体，用系统字体栈）",
        )
    else:
        report.ok(
            item,
            "timing.json 未设 brand.logo、fonts.json 未指定本地字体——两者都是可选项，"
            "模板靠系统字体栈出字",
        )


# ===========================================================================
# 2. timing.json 结构
# ===========================================================================

def check_timing_structure(report: Report, timing: dict) -> None:
    item = "timing.json 结构"
    scenes = [s for s in (timing.get("scenes") or []) if isinstance(s, dict)]
    if not scenes:
        report.fail(item, "scenes 是空的：timing.json 至少要有 1 个场景（name/start/end）")
        return

    problems = []
    # 首个场景必须从 0 开始。scenes 首尾相接只保证它们彼此不断开：首场景 start=2 时，
    # 前面那 2 秒既没有画面也没有字幕（Promo 的 <Sequence> 从 0 开始，场景组件在自己窗口外
    # return null，屏幕上就是黑的），但下面的 OK 文案会写「0 ~ N 秒」，看起来一切正常
    # —— 声明的 [0, duration] 覆盖不到开头那段，成片还比 duration 短。
    try:
        first_start = float(scenes[0].get("start"))
    except (TypeError, ValueError):
        first_start = None
        problems.append("第一个场景的 start 不是数字（必须从 0 开始）")
    if first_start is not None and abs(first_start) > 1e-4:
        problems.append(
            f"第一个场景「{scenes[0].get('name', '#0')}」start={first_start:g}，不是 0："
            "片头这几秒是黑屏也没有字幕，成片时长还会比 duration 短。"
            "把首场景 start 改成 0，并顺移后面的场景"
        )

    previous = None
    for index, scene in enumerate(scenes):
        name = scene.get("name", f"#{index}")
        try:
            start = float(scene.get("start"))
            end = float(scene.get("end"))
        except (TypeError, ValueError):
            problems.append(f"场景「{name}」的 start/end 不是数字")
            continue
        if end <= start:
            problems.append(f"场景「{name}」end({end:g}) ≤ start({start:g})")
        if previous is not None and abs(start - previous[1]) > 1e-4:
            gap = start - previous[1]
            problems.append(
                f"场景「{previous[0]}」end={previous[1]:g} 与场景「{name}」start={start:g} "
                f"之间有 {gap:+.3f} 秒的空隙/重叠（必须首尾相接）"
            )
        previous = (name, end)

    if problems:
        for text in problems:
            report.fail(item, text)
    else:
        # 起点用**实际**算出来的首场景 start（上面的断言保证它 ≈0），不许再写死「0 ~ …」：
        # 真出现 0 以外的值时，那句话会替坏时间轴打掩护。
        report.ok(
            item,
            f"{len(scenes)} 个场景首尾相接（{first_start:g} ~ {float(scenes[-1].get('end')):g} 秒）",
        )

    # duration == 最后一个场景的 end
    try:
        last_end = float(scenes[-1].get("end"))
    except (TypeError, ValueError):
        last_end = None
    duration = timing.get("duration")
    try:
        duration = float(duration)
    except (TypeError, ValueError):
        duration = None
    if duration is None:
        report.fail(item, "duration 缺失或不是数字（单位：秒）")
    elif last_end is not None and abs(duration - last_end) > TIME_EPS:
        report.fail(
            item,
            f"duration={duration:g} 不等于最后一个场景的 end={last_end:g}"
            "（契约要求 duration = 最后场景 end，否则成片会多出/少掉尾巴）",
        )
    elif last_end is not None:
        report.ok(item, f"duration={duration:g} 秒 = 最后一个场景的 end")

    # 字幕安全区：单行 ≤13 字、最多 2 行（AGENTS.md 阶段 7）
    captions = _sub_dict(timing, "captions", report, item, "{enabled, maxChars, maxLines, bottom}")
    max_chars = _positive_number(captions.get("maxChars", 13))
    max_lines = _positive_number(captions.get("maxLines", 2))
    if max_chars is None:
        report.fail(
            item,
            f"captions.maxChars 必须是正数，现在是 {captions.get('maxChars')!r}："
            "它是「一行放多少字」的判据，不是数字就没法算容量（默认 13）",
        )
        max_chars = 13.0
    if max_lines is None:
        report.fail(
            item,
            f"captions.maxLines 必须是正数，现在是 {captions.get('maxLines')!r}："
            "它是「最多几行」的判据，不是数字就没法算容量（默认 2）",
        )
        max_lines = 2.0
    too_long = []
    for line in (_obj(timing.get("voice")).get("lines", []) or []):
        if not isinstance(line, dict):
            continue
        text = str(line.get("text", "") or "")
        if len(text) > max_chars * max_lines:
            too_long.append((str(line.get("id", "")), len(text)))
    if too_long:
        detail = "、".join(f"第 {lid} 句 {n} 字" for lid, n in too_long)
        report.warn(
            item,
            f"这些句子超过字幕容量（{max_lines:g} 行 × {max_chars:g} 字）：{detail}。"
            "竖屏字幕会挤爆或被迫缩字号，建议拆句或删字",
        )
    else:
        report.ok(item, f"字幕容量 {max_lines:g} 行 × {max_chars:g} 字，所有句子都放得下")


def check_music_fields(report: Report, timing: dict) -> None:
    item = "配乐字段"
    # music 的坏输入（字符串/列表/数字）只在这里报一次 ERROR；
    # 其它消费点（check_audio 等）用 _obj 静默归一，避免同一个字段刷出多条。
    music = _sub_dict(timing, "music", report, item, "{file, source, offset, beat, bar, drop, gain}")
    problems = []
    for key in ("beat", "bar"):
        try:
            value = float(music.get(key))
        except (TypeError, ValueError):
            problems.append(f"music.{key} 缺失或不是数字")
            continue
        if value <= 0:
            problems.append(f"music.{key}={value:g} 必须 > 0（单位：秒）")
    try:
        beat = float(music.get("beat"))
        bar = float(music.get("bar"))
        if beat > 0 and bar > 0 and bar < beat - 1e-9:
            problems.append(f"music.bar={bar:g} 比 music.beat={beat:g} 还短，一个小节不可能少于 1 拍")
    except (TypeError, ValueError):
        pass

    drop = music.get("drop")
    if drop not in (None, ""):
        try:
            drop_value = float(drop)
            if drop_value < 0:
                problems.append(f"music.drop={drop_value:g} 不能是负数（它是第一次爆发的视频时间）")
            elif drop_value > 4.0:
                problems.append(
                    f"music.drop={drop_value:g}s 晚于第 4 秒——AGENTS.md 阶段 5 要求最晚第 4 秒完成第一次爆发"
                )
        except (TypeError, ValueError):
            problems.append(f"music.drop={drop!r} 不是数字")

    if problems:
        for text in problems:
            report.fail(item, text)
    else:
        report.ok(
            item,
            "music.beat=%.4fs / bar=%.4fs / drop=%s 都合法"
            % (float(music.get("beat")), float(music.get("bar")), music.get("drop")),
        )


_WINDOW_FALLBACKS: list = []


def _local_window(timing: dict, line: dict):
    """本地那份窗口实现（`_common.line_window` 的等价物，独立算一遍用）。

    只在 ``line_window`` 抛错时当兜底用；单独抽成函数是为了让
    :func:`check_window_consistency` 能拿它跟公共口径逐句比对。
    """
    start = float(line.get("at", 0.0) or 0.0)
    scenes = [s for s in (timing.get("scenes") or []) if isinstance(s, dict)]
    scene = None
    for candidate in scenes:
        if str(candidate.get("name")) == str(line.get("scene", "")):
            scene = candidate
            break
    scene_end = float(scene.get("end", start + 8.0)) if scene else start + 8.0
    next_at = None
    for other in _obj(timing.get("voice")).get("lines", []) or []:
        if not isinstance(other, dict):
            continue
        try:
            at = float(other.get("at", 0.0) or 0.0)
        except (TypeError, ValueError):
            continue
        if at > start + 1e-9 and (next_at is None or at < next_at):
            next_at = at
    end = min(scene_end, next_at) if next_at is not None else scene_end
    return start, end - 0.15


def _safe_window(timing: dict, line: dict):
    """优先用 _common.line_window；它抛错时退回本地等价实现，保证检查不中断。

    **退回不是静默的**：本地这份实现（下一句按「at 严格更大里最小的那个」取）与
    ``_common.line_window``（下一步按「按 at 排序后紧跟其后、且 at 严格更大」取）在
    ``at`` 回跳的时间轴上是两个算法，可能给出不同的窗口。一旦退回就把这条记进
    ``_WINDOW_FALLBACKS``，收尾由 :func:`check_window_consistency` 打 WARN 说明
    「本次用的不是公共口径」，避免检查结果看起来正常、其实换了一套口径。
    """
    try:
        start, end = line_window(timing, line)
        return float(start), float(end)
    except Exception as exc:
        _WINDOW_FALLBACKS.append((str(line.get("id", "")).strip() or "?", exc))
    return _local_window(timing, line)


def check_window_consistency(report: Report, timing: dict) -> None:
    """报出「本轮用过本地窗口实现」这件事，并逐句核对两份实现是否一致。

    ``_common.line_window`` 对同一份 timing 抛错（例如某句 at 已经贴到场景末尾、
    扣掉 0.15s 尾巴后窗口为空），说明时间轴本身有问题，那是 ``tts.py audit`` 的 ERROR；
    这里只负责：① 告诉使用者本次检查没有用公共口径；② 把能算出窗口的句子逐句比对，
    一致就明说一致，不一致就列出差在哪 —— 这正是审计里担心的「两份实现各自漂移」。

    **不能只看 ``_WINDOW_FALLBACKS``**：那个列表只有 ``_safe_window`` 被调用过才会填，
    而这个检查项跑在 ``check_voice_length`` 之前的话它永远是空的（曾经就是这么错的）。
    所以这里自己逐句调一次 ``line_window``，有多少句抛错、哪些句不一致，都在本轮现算。
    """
    item = "旁白窗口口径"
    lines = [ln for ln in (_obj(timing.get("voice")).get("lines") or []) if isinstance(ln, dict)]
    if not lines:
        return

    failed: list = []
    mismatched: list = []
    checked = 0
    for line in lines:
        line_id = str(line.get("id", "")).strip() or "?"
        try:
            want = line_window(timing, line)
        except Exception as exc:  # 这一句公共口径算不出来 → 检查里走的是本地兜底
            failed.append((line_id, exc))
            continue
        try:
            got = _local_window(timing, line)
        except Exception:
            continue
        checked += 1
        if abs(want[0] - got[0]) > 1e-6 or abs(want[1] - got[1]) > 1e-6:
            mismatched.append(
                "%s: 公共口径 %.3f~%.3f vs 本地 %.3f~%.3f"
                % (line_id, want[0], want[1], got[0], got[1])
            )

    if failed:
        ids = "、".join(lid for lid, _ in failed)
        report.warn(
            item,
            f"{len(failed)} 句（{ids}）的窗口取不到 _common.line_window 的值，"
            "本次检查里这些句子用的是脚本内的等价实现；"
            "那些句子本身需要按 tts.py audit 的报错改 timing.json",
        )
    if mismatched:
        report.warn(item, "两份窗口实现结果不一致：" + "；".join(mismatched))
    elif checked:
        report.ok(item, f"能算出窗口的 {checked} 句里，本地实现与 _common.line_window 完全一致")


def check_voice_lines(report: Report, timing: dict) -> None:
    item = "旁白时间窗"
    # voice 的坏输入（字符串/列表/数字）只在这一个检查项里报 ERROR；
    # 其余消费点用 _obj 静默归一，避免同一个字段刷出好几条 ERROR。
    voice = _sub_dict(timing, "voice", report, item, "{engine, voice, rate, lines}")
    lines = [line for line in (voice.get("lines") or []) if isinstance(line, dict)]
    if not lines:
        report.warn(item, "voice.lines 是空的：片子里没有任何旁白（确认这是有意为之）")
        return

    scenes = {str(s.get("name")): s for s in (timing.get("scenes") or []) if isinstance(s, dict)}
    hard = []
    for line in lines:
        line_id = str(line.get("id", "")).strip() or "?"
        scene_name = str(line.get("scene", "") or "")
        scene = scenes.get(scene_name)
        if scene is None:
            hard.append(
                f"第 {line_id} 句的 scene=\"{scene_name}\" 在 timing.json 的 scenes 里找不到"
                f"（现有：{'、'.join(scenes) or '空'}）"
            )
            continue
        try:
            at = float(line.get("at", 0.0) or 0.0)
            start = float(scene.get("start"))
            end = float(scene.get("end"))
        except (TypeError, ValueError):
            hard.append(f"第 {line_id} 句的 at 或所属场景的 start/end 不是数字")
            continue
        if at < start - 1e-6 or at >= end - 1e-6:
            hard.append(
                f"第 {line_id} 句 at={at:g}s 不在场景「{scene_name}」的窗口 [{start:g}, {end:g}) 内"
            )
        if at + 0.4 > end:
            hard.append(
                f"第 {line_id} 句 at={at:g}s 离场景「{scene_name}」结束只有 {end - at:.2f}s，"
                "旁白会跨到下一个场景里"
            )

    if hard:
        for text in hard:
            report.fail(item, text)
    else:
        report.ok(item, f"{len(lines)} 句旁白的 at 都落在所属场景窗口内")


def check_voice_length(report: Report, timing: dict) -> None:
    item = "旁白字数"
    lines = [line for line in (_obj(timing.get("voice")).get("lines") or []) if isinstance(line, dict)]
    if not lines:
        return
    over = []
    bad_window = []
    for line in lines:
        line_id = str(line.get("id", "")).strip() or "?"
        text = str(line.get("text", "") or "")
        try:
            start, end = _safe_window(timing, line)
        except Exception:
            continue
        window = end - start
        if window <= 0:
            # 窗口非法 = at 排到了场景结束之后、或和下一句挤在一起。
            # tts.py audit 对这种情况是 ERROR；这里至少要说一句，别静默跳过（静默跳过
            # 会让「窗口算不出来」看起来像「这句没问题」）。
            bad_window.append((line_id, start, end))
            continue
        budget = CHARS_PER_SEC * window
        if len(text) > budget:
            cut = int(round(len(text) - budget + 0.49))
            over.append((line_id, len(text), window, max(1, cut)))
    for line_id, start, end in bad_window:
        report.warn(
            item,
            f"第 {line_id} 句的旁白窗口不合法（{start:.2f}s~{end:.2f}s，长度 {end - start:.2f}s）："
            "at 排到了场景结束之后，或者和下一句挤在一起。字数检查没法按这个窗口算，"
            "请按 `python scripts/tts.py audit` 的报错改 timing.json",
        )
    if over:
        for line_id, count, window, cut in over:
            report.warn(
                item,
                f"第 {line_id} 句 {count} 字 / 窗口 {window:.2f}s 只能放 {CHARS_PER_SEC * window:.1f} 字"
                f"（按 {CHARS_PER_SEC} 字/秒）——建议删掉 {cut} 个字。"
                "AGENTS.md 阶段 7：字数超窗口必须改稿，不许靠加速硬压",
            )
    else:
        checked = len(lines) - len(bad_window)
        if checked > 0:
            report.ok(item, f"{checked} 句都在 {CHARS_PER_SEC} 字/秒的预算内")


# ===========================================================================
# 3. 图片
# ===========================================================================

def _image_files(directory: Path):
    if not directory.is_dir():
        return []
    return sorted(
        p for p in directory.rglob("*")
        if p.is_file() and p.suffix.lower() in IMAGE_SUFFIXES
    )


def check_images(report: Report, timing: dict) -> None:
    item = "图片尺寸"
    directory = PROJECT_ROOT / "public" / "img"
    files = _image_files(directory)
    if not files:
        report.warn(
            item,
            "public/img/ 里没有图片。AGENTS.md 阶段 4 要求真实界面截图用 "
            "`python scripts/capture.py <url> <name> --factor 2` 出 2 倍图再放进这里",
        )
        return

    try:
        from PIL import Image  # type: ignore
    except ImportError:
        report.warn(
            item,
            f"未安装 Pillow，跳过 {len(files)} 张图片的尺寸检查（`pip install Pillow` 后重跑）",
        )
        return

    small = []
    large = []
    unreadable = []
    for path in files:
        try:
            with Image.open(path) as image:
                width, height = image.size
        except Exception as exc:  # noqa: BLE001 - Pillow 抛的异常类型很多
            unreadable.append((path, str(exc)))
            continue
        if width < IMG_MIN_WIDTH:
            small.append((path, width, height))
        elif width > IMG_MAX_WIDTH:
            large.append((path, width, height))

    for path, exc in unreadable:
        report.fail(item, f"{_rel(path)} 无法用 Pillow 打开（{exc}）；渲染时很可能读不出来")

    for path, width, height in small:
        report.warn(
            item,
            f"{_rel(path)} 只有 {width}×{height}px，太小（1080 宽画布会发虚）。"
            "请回阶段 4 重截 2 倍图（capture.py --factor 2）",
        )
    for path, width, height in large:
        report.warn(
            item,
            f"{_rel(path)} 有 {width}×{height}px，太大（4K 渲染吃内存，建议 "
            f"`python scripts/media.py prepare <src> <dst> --max 2800` 压一下）",
        )
    if not small and not large and not unreadable:
        report.ok(
            item,
            f"{len(files)} 张图片宽度都在 {IMG_MIN_WIDTH}~{IMG_MAX_WIDTH}px 下限护栏内（1080 宽画布的 2 倍图目标 ≥2160px，「≥ 显示宽度 ×2」需人工核对）",
        )


def check_asset_quota(report: Report, timing: dict) -> None:
    item = "素材配额"
    files = _image_files(PROJECT_ROOT / "public" / "img")
    try:
        duration = float(timing.get("duration", 0) or 0)
    except (TypeError, ValueError):
        duration = 0.0
    required = IMG_MIN_COUNT_SHORT if duration <= SHORT_FILM_S else IMG_MIN_COUNT_LONG
    if len(files) < required:
        report.warn(
            item,
            f"public/img/ 只有 {len(files)} 张图片，片长 {duration:g}s 要求 ≥{required} 张"
            "（AGENTS.md 阶段 4：全片真实素材配额，且必须有一段 ≥6 张真实作品的快切蒙太奇）。"
            "数量不够请先补素材，不要用 AI 图凑数",
        )
    else:
        report.ok(item, f"public/img/ 有 {len(files)} 张图片（片长 {duration:g}s 要求 ≥{required}）")


def _scene_files(timing: dict):
    """把 timing.json 的 scenes[].name 映到 src/scenes/<Name>.tsx（大小写不敏感）。

    AGENTS.md 阶段 6 的约定是「一个场景 = 一个组件」，模板里是 hook → Hook.tsx。
    名字在 src/scenes/ 下找不到对应文件就跳过 —— 不猜（宁可不查，也不误报）。
    """
    directory = PROJECT_ROOT / "src" / "scenes"
    if not directory.is_dir():
        return []
    by_stem = {path.stem.casefold(): path for path in sorted(directory.glob("*.tsx"))}
    mapping = []
    for scene in (timing.get("scenes") or []):
        if not isinstance(scene, dict):
            continue
        name = str(scene.get("name", "") or "").strip()
        path = by_stem.get(name.casefold())
        if path is not None:
            mapping.append((name, path))
    return mapping


def _inspect_scene_file(path: Path):
    """→ (场景引用的 public/ 图片集合, 有没有静态扫不出来的动态引用, 有没有用素材容器组件)。"""
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return set(), True, False  # 读不出来：按"有动态引用"处理，不对这一段下结论
    text = _strip_ts_comments(raw)
    dynamic = bool(DYNAMIC_ASSET_RE.search(text))
    material = any(re.search(r"\b%s\b" % component, text) for component in MATERIAL_COMPONENTS)
    refs = set()
    for _quote, value in SCENE_LITERAL_RE.findall(text):
        candidate = value.strip().replace("\\", "/")
        if candidate.lower().endswith(IMAGE_SUFFIXES) and _looks_like_public_asset(candidate):
            refs.add(candidate.lstrip("/"))
    return refs, dynamic, material


def check_scene_assets(report: Report, timing: dict) -> None:
    """按场景分段核验真实素材（AGENTS.md 阶段 4 的硬性配额）。

    总数配额（check_asset_quota）只说明"素材够不够"，说明不了"这一段有没有素材"：
    6 张图全塞进 proof、用了 PhoneFrame 的 demo 一张都没引用时，以前照样 0 ERROR / 0 WARN。
    这里按「一个场景 = 一个组件」把引用分组，逐段判「每个功能段落 ≥1 张真实截图」，
    并核「有一段 ≥6 张真实作品（快切蒙太奇）」。

    只对静态能证明的部分下结论：场景里出现变量拼路径（``src={…}`` 或模板字符串）时，
    那一段的计数标为"扫不出来"、不下断言；全片总数不达标时也不重复报（那是
    :func:`check_asset_quota` 的 WARN）。
    """
    item = "分段素材"
    scenes = _scene_files(timing)
    if not scenes:
        return

    files = _image_files(PROJECT_ROOT / "public" / "img")
    try:
        duration = float(timing.get("duration", 0) or 0)
    except (TypeError, ValueError):
        duration = 0.0
    required = IMG_MIN_COUNT_SHORT if duration <= SHORT_FILM_S else IMG_MIN_COUNT_LONG
    if len(files) < required:
        # 总数就不够：check_asset_quota 已经报了 WARN，这里不再把同一件事说第二遍。
        return

    counts = []
    empty_segments = []
    dynamic_segments = []
    for name, path in scenes:
        refs, dynamic, material = _inspect_scene_file(path)
        counts.append((name, len(refs), material, dynamic))
        if dynamic:
            dynamic_segments.append(name)
        elif material and not refs:
            empty_segments.append(name)

    detail = "、".join(f"{name} {count} 张" for name, count, _m, _d in counts)
    montage = max((count for _n, count, _m, _d in counts), default=0)

    if empty_segments:
        report.warn(
            item,
            f"这些功能段落一张真实截图都没引用：{'、'.join(empty_segments)}（分段计数：{detail}）。"
            "AGENTS.md 阶段 4 的硬性配额是「每个功能段落至少 1 张真实截图」——"
            "全片总数够了不代表每一段都有素材，请给这些场景补 capture.py --factor 2 的真实截图",
        )
    if montage < MONTAGE_MIN:
        hint = ""
        if dynamic_segments:
            hint = (
                f"（{'、'.join(dynamic_segments)} 用变量拼路径，静态扫不出来 —— "
                "如果那一段就是把 ≥6 张作品拼起来的蒙太奇，请人工确认）"
            )
        report.warn(
            item,
            f"没有任何一个场景静态可见 ≥{MONTAGE_MIN} 张真实作品图（分段计数：{detail}）。"
            "AGENTS.md 阶段 4 要求必须有一段由 ≥6 张真实作品组成的快切蒙太奇" + hint,
        )
    if not empty_segments and montage >= MONTAGE_MIN:
        note = ""
        if dynamic_segments:
            note = (
                f"；{'、'.join(dynamic_segments)} 用变量拼路径"
                "，那一段的素材数静态扫不出来（未下结论）"
            )
        report.ok(item, f"按场景分段统计：{detail}；每个功能段落都有真实截图{note}")


# ===========================================================================
# 4. 字体缺字
# ===========================================================================

def _collect_texts(timing: dict):
    """收集所有会渲染到画面上的文字（缺字会变方块，必须全查）。"""
    texts = []
    title = timing.get("title")
    if title:
        texts.append(("title", str(title)))
    brand = _obj(timing.get("brand"))
    if brand.get("name"):
        texts.append(("brand.name", str(brand["name"])))
    if brand.get("url"):
        texts.append(("brand.url", str(brand["url"])))
    for line in (_obj(timing.get("voice")).get("lines") or []):
        if isinstance(line, dict) and line.get("text"):
            texts.append((f"voice.lines[{line.get('id')}].text", str(line["text"])))
    for scene in ((timing.get("scenes") or [])):
        if isinstance(scene, dict) and scene.get("note"):
            texts.append((f"scenes[{scene.get('name')}].note", str(scene["note"])))
    return texts


def check_fonts(report: Report, timing: dict) -> None:
    item = "字体缺字"
    fonts_path = PROJECT_ROOT / "fonts.json"
    fonts_cfg, err = _read_json_file(fonts_path)
    if fonts_cfg is None:
        if err == JSON_MISSING:
            report.warn(item, f"读不到 fonts.json（{err}），跳过缺字检查")
        else:
            report.fail(
                item,
                f"fonts.json 存在但读不出来（{err}）：缺字检查不能跳过 —— "
                "一个坏文件不该让整项闸门悄悄消失，修好它再重跑",
            )
        return

    check_cfg = fonts_cfg.get("check", {}) or {}
    candidates = list(check_cfg.get("systemCandidates", []) or [])
    chosen = None
    for candidate in candidates:
        path = Path(str(candidate)).expanduser()
        if not path.is_absolute():
            # fonts.json 里的候选是相对项目根的（如 public/fonts/promo-subset-display.ttf），
            # 必须按 PROJECT_ROOT 解析：按进程 CWD 解析时，从项目根之外调用脚本会悄悄退到
            # 系统字体（msyh.ttc），内嵌子集缺字就被静默放过（会渲染成方块）。
            path = PROJECT_ROOT / path
        try:
            if path.is_file():
                chosen = path
                break
        except OSError:
            continue

    if chosen is None:
        report.warn(
            item,
            "systemCandidates 里一个字体文件都没找到，无法检查缺字。"
            "建议安装 Noto Sans CJK SC（Linux: `sudo apt install fonts-noto-cjk`；"
            "Windows 用自带的微软雅黑 C:/Windows/Fonts/msyh.ttc；"
            "macOS 用 PingFang）。缺字时中文会渲染成方块（□）",
        )
        return

    try:
        from fontTools.ttLib import TTFont  # type: ignore
    except ImportError:
        report.warn(
            item,
            f"未安装 fontTools，跳过对 {chosen.name} 的缺字检查"
            "（`pip install fonttools` 后重跑）",
        )
        return

    try:
        if chosen.suffix.lower() in (".ttc", ".otc"):
            font = TTFont(str(chosen), fontNumber=0, lazy=True)
        else:
            font = TTFont(str(chosen), lazy=True)
        try:
            cmap = font.getBestCmap() or {}
        finally:
            try:
                font.close()
            except Exception:
                pass
    except Exception as exc:  # noqa: BLE001
        report.warn(
            item,
            f"字体 {chosen.name} 读不出 cmap（{exc}），跳过缺字检查。"
            "如果渲染出来是方块，请换一个完整的 CJK 字体",
        )
        return

    texts = _collect_texts(timing)
    extra = str(check_cfg.get("extraChars", "") or "")
    if extra:
        texts.append(("fonts.json check.extraChars", extra))

    missing_map = {}
    checked = 0
    seen = set()
    for source, text in texts:
        for char in text:
            if char.isspace() or char in seen:
                continue
            seen.add(char)
            checked += 1
            if ord(char) not in cmap:
                missing_map.setdefault(char, []).append(source)

    if missing_map:
        chars = "".join(sorted(missing_map))
        detail = "、".join(
            f"「{char}」(来自 {missing_map[char][0]})" for char in sorted(missing_map)[:12]
        )
        more = "" if len(missing_map) <= 12 else f" 等共 {len(missing_map)} 个"
        report.fail(
            item,
            f"{chosen.name} 缺少这些字符：{detail}{more} —— 缺字：「{chars}」。"
            "它们在片子里会渲染成方块（□）。请改用覆盖它们的字体"
            "（Noto Sans CJK SC / 思源黑体 / 微软雅黑），或把文案换成不缺字的写法",
        )
        return

    report.ok(item, f"{chosen.name} 覆盖了片子里全部 {checked} 个不重复字符（含标点数字）")


# ===========================================================================
# 5. 音频
# ===========================================================================

def check_audio(report: Report, timing: dict) -> None:
    music = _obj(timing.get("music"))
    declared = str(music.get("file", "") or "").strip()
    if not declared:
        report.fail(
            "混音文件",
            "timing.json 的 music.file 是空的：它应当是相对 public/ 的混音输出路径"
            "（模板里是 \"audio/music.wav\"），Remotion 靠它找音轨",
        )
    else:
        mix_path = PROJECT_ROOT / "public" / declared
        if not mix_path.is_file() or mix_path.stat().st_size == 0:
            report.fail(
                "混音文件",
                f"{_rel(mix_path)} 不存在。先跑 `python scripts/mix.py` 生成混音"
                "（混音会写入 timing.json 的 music.file 指向的位置）",
            )
        else:
            report.ok("混音文件", f"{_rel(mix_path)}（{human(mix_path.stat().st_size)}）")

    lines = [line for line in (_obj(timing.get("voice")).get("lines") or []) if isinstance(line, dict)]
    vo_dir = PROJECT_ROOT / "audio-src" / "vo"
    missing = []
    for line in lines:
        line_id = str(line.get("id", "")).strip()
        path = vo_dir / f"{line_id}.mp3"
        if not path.is_file() or path.stat().st_size == 0:
            missing.append(line_id)
    if missing:
        report.fail(
            "配音文件",
            f"audio-src/vo/ 里缺少 {len(missing)} 句：{'、'.join(missing)}。"
            "先跑 `python scripts/tts.py all` 生成配音，再跑 `python scripts/mix.py` 重新混音",
        )
    elif lines:
        report.ok("配音文件", f"{len(lines)}/{len(lines)} 句配音齐全（audio-src/vo/）")
    else:
        report.warn("配音文件", "timing.json 没有任何旁白句子，跳过配音检查")


# ===========================================================================
# main
# ===========================================================================

def main(argv=None) -> int:
    _force_utf8()
    parser = argparse.ArgumentParser(
        prog="check_assets.py",
        description="promo-forge 渲染前闸门（AGENTS.md 阶段 9）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "输出格式：OK/WARN/ERROR: <检查项> — <说明>；最后一行统计。\n"
            "退出码：0 通过（0 ERROR）；1 有 ERROR。"
        ),
    )
    parser.add_argument("--strict", action="store_true",
                        help="把所有 WARN 也当 ERROR（交付前的严格模式）")
    args = parser.parse_args(argv)

    report = Report(strict=args.strict)
    log(f"渲染前检查：{PROJECT_ROOT}")
    log("")
    # 模块级兜底表要按「本轮」清零：同一进程里再调一次 main() 时，
    # 上一轮的行号不能跟着冒出来（否则会出现指向旧输入的幽灵告警）。
    _WINDOW_FALLBACKS.clear()

    timing = load_timing()

    check_asset_refs(report)
    check_brand_and_font_files(report, timing)
    check_timing_structure(report, timing)
    check_music_fields(report, timing)
    check_voice_lines(report, timing)
    check_voice_length(report, timing)
    # 放在 check_voice_length 之后：那个检查项会（在兜底时）填 _WINDOW_FALLBACKS，
    # 这里的 WARN 才能反映真实用例。不过本函数也不依赖那张表 —— 它自己逐句现算。
    check_window_consistency(report, timing)
    check_images(report, timing)
    check_asset_quota(report, timing)
    check_scene_assets(report, timing)
    check_fonts(report, timing)
    check_audio(report, timing)

    log("")
    label = f"检查完成：{report.errors} ERROR / {report.warnings} WARN"
    if args.strict:
        label += "（--strict 模式）"
    log(label)
    if report.errors:
        # 汇总行已经在 stdout 打过了，这里只负责退出码（别再写一遍 stderr，否则终端里同一行出现两次）。
        return 1
    log("渲染前闸门通过。下一步：npm run stills -- --scenes，再 media.py sheet out/stills --cols 4 拼联系表逐张审图")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
