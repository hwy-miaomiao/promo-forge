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
5. **字体缺字**：用 ``fonts.json`` 的 ``check.systemCandidates`` 找第一个存在的字体
   （内嵌子集存在就只查子集，都不存在才退回系统字体栈），检查**片子里会上屏的文本**每个字符
   是否都被 cmap 覆盖，缺字 → ERROR（会渲染成方块）。上屏文本 = ``timing.json`` 的文案 +
   ``src/``、``web/`` 等源文件里剥掉注释后的文本（复用 ``fonts.py`` 的字符集口径，含 JSX
   文本节点里写死的中文），不然只查 timing.json 会漏掉一多半屏上字；
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
import unicodedata
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

# 帧率护栏（R6-TPL-03）：timing.json 的 fps 是「秒 × fps = 帧号」换算与渲染命令的依据，
# 必须有限正数。模板默认 60；上限按「高帧率但合理」取 240，再高基本都是多写了一个 0。
MAX_FPS = 240.0

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
        # utf-8-sig：允许 BOM，否则 BOM 会让 json.loads 在第一列就抛「Expecting value」，
        # 两个闸门都硬 ERROR，而文件内容实际是合法的（V26/V9 同根因）。
        return json.loads(path.read_text(encoding="utf-8-sig")), None
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


def _finite_number(value):
    """把数字字段转成 float；非数字 / 布尔 / NaN / ±Inf 一律 None（调用方报 ERROR）。

    R6-CA-01：Python 的 json.loads 默认接受非标准 NaN / Infinity。裸 `float()` 解析后
    再比大小的话，NaN 与任何数比较都是 False，会绕过所有范围判断 ——
    `duration: NaN` / `voice.lines[].at: NaN` / `music.beat: NaN` 都能 0 ERROR 通过。
    """
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number or number in (float("inf"), float("-inf")):
        return None
    return number


def _strict_number(value):
    """把「必须是 JSON 数字」的字段转成 float；字符串 / 布尔 / 非数字一律 None。

    R8-CA-01：为什么不能像 `_finite_number` 那样收数字字符串 —— timing.json 是唯一事实
    来源，而 TS 侧对这几个字段有**字符串拼接**而不是算术：
      * `src/components/Scene.tsx` 的 `useAbsTime()`：`framesToSec(frame, fps) + (offset ?? 0)`，
        offset 是 `scenes[].start`；
      * `src/components/Caption.tsx` 的 `captionUntil()`：`line.at + Math.max(...)`；
      * `src/lib/beats.ts` 的 `musicToVideo()`：`musicTime + timing.music.offset`。
    字符串一旦混进去就是 `"4.5" + 0.15 === "4.50.15"` → 后续 Number/比较得 NaN，
    画面卡点与画面内字幕整段静默消失，而闸门 0 ERROR。类型口径与 fps /
    captions.bottom（R6-TPL-03）保持一致：数字写错就是错，不靠渲染侧强转掩盖。
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return _finite_number(value)


def _seconds_value(value):
    """把「秒」字段转成 float；返回 (number, None) 或 (None, 中文原因)。

    与 `_positive_number` 的区别：允许 0（`music.segments[].at` 可以写 0.0，表示从片头起），
    但仍然拒绝布尔值、NaN / Inf、负数 —— 这些写进 timing.json 都是配置错误，不是「没填」。
    调用方负责把原因拼进 ERROR 行。
    """
    if value is None or (isinstance(value, str) and not value.strip()):
        return None, "缺失（单位：秒，例如 12.0）"
    if isinstance(value, bool):
        return None, f"是布尔值 {value!r}，不是数字"
    if isinstance(value, str):
        # R8-CA-01：字符串秒数一律拒绝。mix.py 的 segments 计算（at + from/to 与
        # 时间轴相加）会静默变成字符串拼接或 NaN，剪接片段整段错位且不报错。
        return None, f"是字符串 {value!r}（秒数不要加引号）"
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None, f"不是数字：{value!r}（要数字，不要单位后缀，例如 12.0）"
    if number != number or number in (float("inf"), float("-inf")):
        return None, f"必须是有限的数字：{number!r}"
    if number < 0:
        return None, f"不能是负数：{number:g}"
    return number, None


#: 当文件名会出问题的字符：voice.lines[].id 会被拼成 audio-src/vo/<id>.mp3 这类路径，
#: tts.py / mix.py / subtitles.py 都把它当文件名与缓存键用。
_PATH_UNSAFE_CHARS = set('/\\:*?"<>|')
_RESERVED_WIN_NAMES = ({"CON", "PRN", "AUX", "NUL"}
                       | {"COM%d" % index for index in range(1, 10)}
                       | {"LPT%d" % index for index in range(1, 10)})


def _unsafe_filename_reason(name: str) -> str:
    """返回 id 不能当文件名的原因；安全时返回空字符串。"""
    if not name:
        return "是空字符串"
    if any(ch in _PATH_UNSAFE_CHARS or ord(ch) < 32 for ch in name):
        return "含路径分隔符 / 冒号 / 引号 / 通配符 / 控制字符"
    if name in (".", "..") or name.startswith(".") or name.endswith(".") or name.endswith(" "):
        return "是 . / .. 或首尾是点 / 空格（写盘时会被系统静默改名或指向别处）"
    if name.split(".")[0].upper() in _RESERVED_WIN_NAMES:
        return "是 Windows 保留设备名（CON/PRN/AUX/NUL/COM1.. /LPT1..）"
    return ""


def _public_relative_problem(value: str, label: str) -> str:
    """返回「必须是 public/ 下的相对路径」被违反的原因；合法时返回空字符串。

    music.file 会被 mix.py 直接拼成 ``PROJECT_ROOT/public/<music.file>`` 写出，
    brand.logo / fonts.json 的 roles.*.file 会被拼成 ``public/<路径>`` 读取：
    含 ``..`` 或盘符就能读写到项目目录之外，闸门只查「文件存在」时会被静默放行。
    """
    if value.startswith(("/", "\\")) or re.match(r"^[A-Za-z]:", value):
        return f"{label}=\"{value}\" 不能是绝对路径 / 盘符路径：必须是相对 public/ 的路径"
    if ".." in value.replace("\\", "/").split("/"):
        return (f"{label}=\"{value}\" 含 .. 段，会解析到 public/ 之外"
                "（mix.py 会按这个路径写盘，Remotion 侧也拿不到文件）")
    return ""


# ===========================================================================
# 1. 素材引用（staticFile / assetUrl / src="..."）
# ===========================================================================

#: 正则字面量只可能出现在这些 token 之后（JS/TS 语法）：`= ( , : [ ! & | ? { } ;`。
#: 用来区分「正则字面量开头的 /」和「除法 / JSX 闭标签 </div> 的 /」。
_REGEX_START_PUNCT = frozenset("=(,:[!&|?{};")


def _prev_significant(out) -> str:
    """已输出内容里最后一个非空白字符（判断 `/` 是不是正则起点用）。"""
    for char in reversed(out):
        if not char.isspace():
            return char
    return ""


def _strip_ts_comments(text: str) -> str:
    """把 TS/TSX 里的注释挖掉（保留换行，行号不变）。

    必须做这一步：模板的场景文件里大量示例代码写在注释里
    （例如 ``*   <Shot src="img/work-01.png" />``），不挖掉会把示例当真实引用误报缺图。

    V34：还要认**正则字面量**。以前状态机只认字符串引号，遇到
    ``const re = /https?:\\/\\//;`` 这类正则时把里面的 ``//`` 当成行尾注释，
    同一行后面的引用（``const p = staticFile('img/x.png')``）就被静默吃掉、
    漏报「引用存在但文件不存在」。判据：`/` 前一个非空白 token 属于
    ``= ( , : [ ! & | ? { } ;`` 时视为正则起点，一直扫到未转义的 `/`；
    字符类 ``[...]`` 里的 `/` 不算结束。除法与 JSX ``</div>`` 不会命中（前一个 token 不是这些符号）。
    """
    out = []
    index = 0
    length = len(text)
    state = None  # None | "'" | '"' | '`' | "//" | "/*" | "regex"
    in_class = False
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
            if char == "/" and _prev_significant(out) in _REGEX_START_PUNCT:
                state = "regex"
                in_class = False
                out.append(char)
                index += 1
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
        if state == "regex":
            # 正则字面量原样保留（内容不是注释）：转义吞两个字符，字符类里 / 不算结束。
            out.append(char)
            if char == "\\" and nxt:
                out.append(nxt)
                index += 2
                continue
            if char == "[":
                in_class = True
            elif char == "]":
                in_class = False
            elif char == "/" and not in_class:
                state = None
            elif char == "\n":  # 正则不跨行；写坏了也别吞掉后面的代码
                state = None
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


def _flush_brand_problems(report: Report, item: str, problems) -> None:
    """把已经算出来的 brand.logo 问题先打出来（V25：提前 return 别把它们吞掉）。"""
    for text in problems:
        report.warn(item, text)


def check_brand_and_font_files(report: Report, timing: dict) -> None:
    """timing.json 的 brand.logo 与 fonts.json 的 roles.*.file 也是 public/ 下的素材。"""
    item = "品牌与字体文件"
    problems = []

    brand = _sub_dict(timing, "brand", report, item, "{name, url, logo, colors}")
    logo = str(brand.get("logo") or "").strip()
    if logo:
        logo_unsafe = _public_relative_problem(logo, "brand.logo")
        if logo_unsafe:
            problems.append(logo_unsafe)
        else:
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
        # V25：brand.logo 的问题已经算出来了，不能因为 fonts.json 读不到就一起吞掉。
        _flush_brand_problems(report, item, problems)
        if err == JSON_MISSING:
            report.warn(item, f"读不到 fonts.json（{err}），跳过字体文件检查")
        else:
            report.fail(
                item,
                f"fonts.json 存在但读不出来（{err}）：这一项不能跳过 —— 角色的字体文件不查，"
                "等于没查「字体 404 / 回退系统字体」，修好这个文件再重跑",
            )
        return
    # V28：结构异常（顶层不是对象 / roles 不是对象 / 某个角色不是对象）必须是干净的
    # ERROR: 行，而不是在下面 `.get()` 上抛 AttributeError traceback。
    if not isinstance(fonts_cfg, dict):
        _flush_brand_problems(report, item, problems)
        report.fail(
            item,
            f"fonts.json 的顶层必须是 JSON 对象，现在是 {type(fonts_cfg).__name__}："
            f"{fonts_cfg!r}。修成 {{\"roles\": {{…}}, \"check\": {{…}}}} 再重跑",
        )
        return
    roles = fonts_cfg.get("roles")
    if roles is None:
        roles = {}
    elif not isinstance(roles, dict):
        _flush_brand_problems(report, item, problems)
        report.fail(
            item,
            f"fonts.json 的 roles 必须是对象（display/body/mono），现在是 "
            f"{type(roles).__name__}：{roles!r}。roles 缺失是合法的（用系统字体栈），但写成 [] 不行",
        )
        return
    for role in FONT_ROLES:
        role_cfg = roles.get(role) or {}
        if not isinstance(role_cfg, dict):
            _flush_brand_problems(report, item, problems)
            report.fail(
                item,
                f"fonts.json 的 roles.{role} 必须是对象（{{stack, weight, file}}），现在是 "
                f"{type(role_cfg).__name__}：{role_cfg!r}",
            )
            return
        file_value = str(role_cfg.get("file") or "").strip()
        if not file_value:
            continue
        roles_checked += 1
        file_unsafe = _public_relative_problem(file_value, f"fonts.json 的 roles.{role}.file")
        if file_unsafe:
            problems.append(file_unsafe)
            continue
        candidate = PROJECT_ROOT / "public" / file_value.lstrip("/")
        if not candidate.is_file():
            problems.append(
                f"fonts.json 的 roles.{role}.file=\"{file_value}\" 指向 {_rel(candidate)}，但文件不存在"
                "（@remotion/fonts 会加载失败并回退系统字体，同一行里可能混出两种字形；"
                "新克隆没有子集字体是正常的，跑 `python scripts/fonts.py subset` 生成）"
            )

    if problems:
        _flush_brand_problems(report, item, problems)
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


#: TS 侧按这组键**无条件**读 brand（template/src/lib/timing.ts 的 ``brand: Brand`` 必填类型；
#: Promo.tsx 读 brand.colors.bg、Cta.tsx 读 brand.logo.trim()/brand.name/brand.url、
#: theme.ts 读 brand.colors）。所以它们是契约必填项，不是可选装饰。
BRAND_STRING_KEYS = ("name", "url", "logo")
BRAND_COLOR_KEYS = ("bg", "fg", "accent")


def check_brand_fields(report: Report, timing: dict) -> None:
    """brand 结构校验（R9-CA-02）：缺 brand / colors / logo 时闸门绿灯、渲染期 TypeError。"""
    item = "品牌信息"
    raw = timing.get("brand")
    if raw is None:
        report.fail(
            item,
            "timing.json 缺 brand：TS 侧 Timing 类型把 brand 标为必填（src/lib/timing.ts 的 "
            "brand: Brand），Promo.tsx 读 TIMING.brand.colors.bg、Cta.tsx 读 brand.logo.trim()/"
            "brand.name/brand.url、lib/theme.ts 读 brand.colors —— 缺了渲染期直接 "
            "TypeError（Cannot read properties of undefined），而渲染前闸门 0 ERROR。"
            "补上 {name, url, logo, colors:{bg,fg,accent}}",
        )
        return
    if not isinstance(raw, dict):
        # 类型错误已由 check_brand_and_font_files 的 _sub_dict 报过，这里不重复刷 ERROR。
        return
    problems = []
    for key in BRAND_STRING_KEYS:
        value = raw.get(key)
        if not isinstance(value, str):
            problems.append(
                f"brand.{key} 必须是字符串（TS 侧必填；logo 没有就写空字符串），"
                f"现在是 {type(value).__name__}：{value!r}"
            )
        elif key in ("name", "url") and not value.strip():
            problems.append(
                f"brand.{key} 不能是空字符串：CTA 场景会把它直接渲染到屏上"
                "（空值等于 CTA 少一半信息，发布出去就是事故）"
            )
    colors = raw.get("colors")
    if not isinstance(colors, dict):
        problems.append(
            "brand.colors 必须是对象（bg / fg / accent 三个颜色值），现在是 %s：%r"
            " —— src/lib/theme.ts 直接把它当 BrandColors 用"
            % (type(colors).__name__, colors)
        )
    else:
        bad_colors = []
        for key in BRAND_COLOR_KEYS:
            value = colors.get(key)
            if not isinstance(value, str) or not value.strip():
                bad_colors.append("%s=%r" % (key, value))
        if bad_colors:
            problems.append(
                "brand.colors 缺少合法颜色值：%s（必须是 CSS 颜色字符串，例如 #0B0B12）"
                % ("、".join(bad_colors),)
            )
    if problems:
        for text in problems:
            report.fail(item, text)
    else:
        report.ok(item, "brand 的 name / url / logo / colors(bg,fg,accent) 结构齐全")


# ===========================================================================
# 2. timing.json 结构
# ===========================================================================

def _caption_capacity_width(text: str) -> int:
    """字幕容量用的「字宽」：全角 1 / 半角 0.5，最后四舍五入。

    与 ``subtitles.py`` 的 ``_vis_len``（``_fold_text``/``_build_ass`` 用的同一套宽度
    模型）保持同一口径：横屏/竖屏排版判「一行放得下几个字」看的是视觉宽度，不是
    Unicode 码点个数。旧实现用 ``len(text)`` 数字符：27 个半角字符（URL、英文长句、
    数字串）实际只有 13.5 个全角字宽，2 行 × 13 的容量明明放得下，却会被误报
    「超过字幕容量」；反过来全角/emoji 与半角的宽度差也被抹平了。
    """
    total = 0.0
    for char in text:
        total += 1.0 if unicodedata.east_asian_width(char) in ("W", "F") else 0.5
    return int(round(total))


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
    first_start = _strict_number(scenes[0].get("start"))
    if first_start is None:
        problems.append("第一个场景的 start 不是数字（NaN/Infinity/字符串都不行；必须是不带引号的 0）")
    if first_start is not None and abs(first_start) > 1e-4:
        problems.append(
            f"第一个场景「{scenes[0].get('name', '#0')}」start={first_start:g}，不是 0："
            "片头这几秒是黑屏也没有字幕，成片时长还会比 duration 短。"
            "把首场景 start 改成 0，并顺移后面的场景"
        )

    previous = None
    for index, scene in enumerate(scenes):
        name = scene.get("name", f"#{index}")
        start = _strict_number(scene.get("start"))
        end = _strict_number(scene.get("end"))
        if start is None or end is None:
            problems.append(
                f"场景「{name}」的 start/end 不是数字（NaN/Infinity 也不行；"
                "字符串要写成不带引号的数字，否则 useAbsTime/sequence 换算会得 NaN）"
            )
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
    last_end = _strict_number(scenes[-1].get("end"))
    duration = _strict_number(timing.get("duration"))
    if duration is None:
        report.fail(
            item,
            "duration 缺失或不是数字（单位：秒；字符串/NaN/Infinity 都不行，"
            "Root.tsx 的 durationInFrames 直接乘它）",
        )
    elif last_end is not None and abs(duration - last_end) > TIME_EPS:
        report.fail(
            item,
            f"duration={duration:g} 不等于最后一个场景的 end={last_end:g}"
            "（契约要求 duration = 最后场景 end，否则成片会多出/少掉尾巴）",
        )
    elif last_end is not None:
        report.ok(item, f"duration={duration:g} 秒 = 最后一个场景的 end")

    # 画幅契约（AGENTS.md 第 5 节）：aspect 只有 "9:16" / "16:9" 两个取值，且必须和
    # width/height 一致。V24：以前两个闸门都不读 aspect —— 一份 aspect="16:9" 但
    # width/height=1080×1920 的工程会一路绿灯，成片投出去按 16:9 处理才发现。
    aspect = timing.get("aspect")
    allowed_aspects = {"9:16": (9.0, 16.0), "16:9": (16.0, 9.0)}
    if not isinstance(aspect, str) or aspect not in allowed_aspects:
        report.fail(
            item,
            f"aspect={aspect!r} 不合法：契约只允许 \"9:16\" 或 \"16:9\"（AGENTS.md 第 5 节）。"
            "竖屏成片用 9:16，横版用 16:9；自定义比例不在契约里",
        )
    else:
        width = _strict_number(timing.get("width"))
        height = _strict_number(timing.get("height"))
        want_w, want_h = allowed_aspects[aspect]
        if width is None or height is None or width <= 0 or height <= 0:
            report.fail(
                item,
                f"aspect={aspect} 但 width/height 缺失或不是数字（现在是 "
                f"{timing.get('width')!r}×{timing.get('height')!r}）：字符串/布尔都不行，"
                "契约要求两者一致",
            )
        elif abs(height - width * want_h / want_w) > 1.0:
            report.fail(
                item,
                f"aspect={aspect} 与 width×height={width:g}×{height:g} 矛盾："
                f"{aspect} 要求宽高比 {want_w:g}:{want_h:g}（1080 宽时高应为 "
                f"{width * want_h / want_w:g}，允许 ±1px）。改尺寸忘改 aspect，或反过来",
            )
        else:
            report.ok(item, f"aspect={aspect} 与 width×height={width:g}×{height:g} 一致")

    # 帧率契约（R6-TPL-03）：fps 必须是有限正数、且在合理范围内。
    # 以前只被 check_video 拿来比成片帧率、被 stills/渲染命令消费 —— fps=0 或 "60"
    # 在 check_assets 这里 0 ERROR 照过，到 stills/渲染阶段才硬失败或静默错帧。
    # 字符串也不行：渲染命令会把它拼进 --fps 参数。
    fps_value = timing.get("fps")
    if isinstance(fps_value, bool) or not isinstance(fps_value, (int, float)):
        report.fail(
            item,
            f"fps={fps_value!r} 不是数字：必须是有限正数（模板默认 60，上限 {MAX_FPS:g}）。"
            "帧号换算（秒 × fps）和渲染命令都读它，写字符串/布尔会一路带到渲染阶段才炸",
        )
    else:
        fps_number = _finite_number(fps_value)
        if fps_number is None or fps_number <= 0 or fps_number > MAX_FPS:
            report.fail(
                item,
                f"fps={fps_value!r} 不是合理帧率：要求 0 < fps ≤ {MAX_FPS:g} 的有限数"
                "（模板默认 60）。0/负数会让帧号换算恒为 0，NaN/Infinity 直接算不出帧",
            )
        else:
            report.ok(item, f"fps={fps_number:g}（0 < fps ≤ {MAX_FPS:g}）")

    # 字幕安全区：单行 ≤13 字、最多 2 行（AGENTS.md 阶段 7）
    captions = _sub_dict(timing, "captions", report, item, "{enabled, maxChars, maxLines, bottom}")
    # 与 fps / bottom 同口径：必须是 JSON 数字（字符串 "13" 会先被 _strict_number 拦下）。
    # maxChars 在 TS 侧走 normalizeMaxChars()（非数字回退模板默认 13），Python 侧
    # subtitles.py 的 _int() 也回退 13 —— 若这里放行字符串 "20"，容量按 20 算、
    # 实际渲染按 13 折行，检查结论和画面就不是同一件事。
    max_chars = _positive_number(_strict_number(captions.get("maxChars", 13)))
    max_lines = _positive_number(_strict_number(captions.get("maxLines", 2)))
    if max_chars is None:
        report.fail(
            item,
            f"captions.maxChars 必须是数字，现在是 {captions.get('maxChars')!r}："
            "它是「一行放多少字」的判据，字符串/非正数就没法算容量（默认 13）",
        )
        max_chars = 13.0
    if max_lines is None:
        report.fail(
            item,
            f"captions.maxLines 必须是数字，现在是 {captions.get('maxLines')!r}："
            "它是「最多几行」的判据，字符串/非正数就没法算容量（默认 2）",
        )
        max_lines = 2.0
    # 字幕位置（R6-TPL-03）：captions.bottom / captions.top 是画布高度的比例，必须 0~1 的有限数。
    # 以前 bottom=1.5 也 0 ERROR 照过 —— 画面内 <Caption> 与硬字幕都按它定位，越界会把字幕
    # 推出画面或压到平台互动按钮区，而且不报错，要到成片/投平台才发现。模板默认 bottom=0.18。
    # 类型与 fps 同口径：必须是 int/float（布尔不算），字符串不放过 —— 渲染侧 Number() 虽然能
    # 把 "0.3" 强转成 0.3，但那只是掩盖类型契约问题；timing.json 是唯一事实来源，类型错了就是错。
    positions_ok = []
    for position_key in ("bottom", "top"):
        if position_key not in captions:
            continue
        raw_position = captions.get(position_key)
        if isinstance(raw_position, bool) or not isinstance(raw_position, (int, float)):
            report.fail(
                item,
                f"captions.{position_key}={raw_position!r} 必须是 0~1 的有限数（不要写字符串）："
                "与 fps 同口径，类型必须是数字。渲染侧 Number() 会强转，但 timing.json 是唯一"
                "事实来源，类型写错这里不拦就只是被渲染掩盖",
            )
            continue
        position_value = _finite_number(raw_position)
        if position_value is None or not (0.0 <= position_value <= 1.0):
            report.fail(
                item,
                f"captions.{position_key}={raw_position!r} 必须是 0~1 的有限数"
                "（画布高度的比例；模板默认 bottom=0.18，0 贴底、1 贴顶）。"
                "越界会让画面内字幕和硬字幕一起定位到画面外/平台按钮区，且渲染不报错",
            )
        else:
            positions_ok.append(f"{position_key}={position_value:g}")
    if positions_ok:
        report.ok(item, "字幕位置 " + "、".join(positions_ok) + " 都在 0~1 内")
    too_long = []
    capacity = max_chars * max_lines
    for line in (_obj(timing.get("voice")).get("lines", []) or []):
        if not isinstance(line, dict):
            continue
        text = str(line.get("text", "") or "")
        # R7-PY-06：按视觉宽度（全角 1 / 半角 0.5）判容量，和 subtitles.py 的 _vis_len
        # 同口径；用 len() 会把 27 个半角字符（13.5 字宽）误报成超过 2 行 × 13 的容量。
        vis_width = _caption_capacity_width(text)
        if vis_width > capacity:
            too_long.append((str(line.get("id", "")), vis_width))
    if too_long:
        detail = "、".join(f"第 {lid} 句 {n} 字宽" for lid, n in too_long)
        report.warn(
            item,
            f"这些句子超过字幕容量（{max_lines:g} 行 × {max_chars:g} 字，按全角 1 / 半角 0.5 "
            f"的字宽算）：{detail}。"
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
    # R9-CA-04：music.file 必须是 public/ 下的相对路径。mix.py 直接把它拼成
    # PROJECT_ROOT/public/<music.file> 写出（还会自动建父目录）：写 ../.. 就能写穿项目，
    # 检查项以前只查「解析后的文件存在」，路径本身完全没查。
    music_file = music.get("file")
    if music_file not in (None, ""):
        if not isinstance(music_file, str):
            problems.append(
                f"music.file 必须是字符串（相对 public/ 的路径，例如 audio/music.wav），"
                f"现在是 {type(music_file).__name__}：{music_file!r}"
            )
        else:
            file_unsafe = _public_relative_problem(music_file, "music.file")
            if file_unsafe:
                problems.append(file_unsafe)

    beat = bar = None
    for key in ("beat", "bar"):
        # R8-CA-01：严格数字。beats.ts 的 beatAt/barAt 用 beat/bar 做 Math.round 吸附，
        # 字符串会让比较与乘法得 NaN，卡点整段失效而渲染不报错。
        value = _strict_number(music.get(key))
        if value is None:
            problems.append(
                f"music.{key} 缺失或不是数字（NaN/Infinity/字符串都不行；单位：秒）"
            )
            continue
        if key == "beat":
            beat = value
        else:
            bar = value
        if value <= 0:
            problems.append(f"music.{key}={value:g} 必须 > 0（单位：秒）")
    if beat is not None and bar is not None and beat > 0 and bar > 0 and bar < beat - 1e-9:
        problems.append(f"music.bar={bar:g} 比 music.beat={beat:g} 还短，一个小节不可能少于 1 拍")

    # R8-CA-05：music.drop 是必填字段（`src/lib/timing.ts` 的 `Music` 类型标为必填），
    # 渲染侧 `src/scenes/Hook.tsx` 直接 `TIMING.music.drop - beat.start` 算钩子爆发时刻：
    # 缺失时 `undefined - number = NaN`，钩子/爆发整段静默失效而渲染不报错。
    # 旧实现是「存在时才查」，漏写 drop 能 0 ERROR 过闸门 —— 与 R8-CA-01 同类，这里收口。
    drop = music.get("drop")
    if drop is None or drop == "":
        problems.append(
            "music.drop 缺失（必填：第一次爆发的视频时间，单位秒）："
            "src/scenes/Hook.tsx 直接拿它做算术，缺失/空串会让钩子爆发变成 NaN 且渲染不报错"
        )
    else:
        drop_value = _strict_number(drop)
        if drop_value is None:
            problems.append(f"music.drop={drop!r} 不是有限数字（NaN/Infinity 也不行）")
        elif drop_value < 0:
            problems.append(f"music.drop={drop_value:g} 不能是负数（它是第一次爆发的视频时间）")
        elif drop_value > 4.0:
            problems.append(
                f"music.drop={drop_value:g}s 晚于第 4 秒——AGENTS.md 阶段 5 要求最晚第 4 秒完成第一次爆发"
            )

    # R8-CA-05：music.gain 保持「存在时校验」的宽松口径 —— mix.py 对缺失 gain 用
    # DEFAULT_MUSIC_GAIN=0.6，TS 侧不消费这个字段，省略是有意支持的；别改成必填。
    gain = music.get("gain")
    if gain not in (None, ""):
        if _strict_number(gain) is None:
            problems.append(f"music.gain={gain!r} 不是有限数字（NaN/Infinity 也不行）")

    # R7-PY-07：music.offset 是「视频时间 = 音乐时间 + offset」的网格平移量（秒），
    # 之前一个字段都没查。NaN/Infinity 会被 mix.py / music.py 的算术一路带进时间轴，
    # 而这里是渲染前的闸门，必须比消费端更早拦住。负数合法（配乐可以先于画面起），
    # 所以只校验有限性、不限制符号。
    # R8-CA-05：offset 同时也是**必填**字段（`src/lib/timing.ts` 的 `Music` 必填），
    # `src/lib/beats.ts` 的 `musicToVideo()` 直接 `musicTime + timing.music.offset`：
    # 缺失时 `number + undefined = NaN`，全片卡点/网格整段失效而渲染不报错，故缺失/空串 ERROR。
    offset = music.get("offset")
    if offset is None or offset == "":
        problems.append(
            "music.offset 缺失（必填：视频时间 = 音乐时间 + offset，单位秒）："
            "src/lib/beats.ts 的 musicToVideo() 直接拿它相加，缺失/空串会让卡点网格变成 NaN 且渲染不报错"
        )
    else:
        if _strict_number(offset) is None:
            problems.append(
                f"music.offset={offset!r} 不是有限数字（NaN/Infinity 也不行；单位：秒，"
                "含义是视频时间 = 音乐时间 + offset）"
            )

    # music.segments（剪接片段）的结构校验（R5-CA-03）：
    # 契约见 AGENTS.md 阶段 5：每段是 {"at": 视频时间, "from": 音乐起点, "to": 音乐终点}
    # （单位秒，from < to；at 必须 < 片长才听得见）。旧实现一个字段都不查，`segments: ["x"]`
    # 能 0 ERROR 通过，到 mix.py 才被跳过/报错 —— 渲染前的闸门应该先拦住坏配置。
    segments = music.get("segments")
    if segments not in (None, "", []):
        if not isinstance(segments, (list, tuple)):
            problems.append(
                f"music.segments 应该是数组（每段一个对象 {{at, from, to}}），"
                f"现在是 {type(segments).__name__}：{segments!r}"
            )
        else:
            for index, segment in enumerate(segments):
                label = f"music.segments[{index}]"
                if not isinstance(segment, dict):
                    problems.append(
                        f"{label} 应该是对象（at/from/to），现在是 "
                        f"{type(segment).__name__}：{segment!r}"
                    )
                    continue
                at, at_error = _seconds_value(segment.get("at"))
                if at_error:
                    problems.append(f"{label}.at {at_error}")
                start, start_error = _seconds_value(segment.get("from"))
                if start_error:
                    problems.append(f"{label}.from {start_error}")
                stop, stop_error = None, None
                raw_stop = segment.get("to")
                if raw_stop not in (None, ""):
                    stop, stop_error = _seconds_value(raw_stop)
                    if stop_error:
                        problems.append(f"{label}.to {stop_error}")
                if (not start_error and not stop_error
                        and start is not None and stop is not None
                        and stop <= start):
                    problems.append(
                        f"{label} 的 from={start:g}s / to={stop:g}s 区间为空（要求 from < to）"
                    )

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
    declared = voice.get("lines")
    if declared is None:
        declared = []
    if not isinstance(declared, (list, tuple)):
        report.fail(
            item,
            f"voice.lines 应该是数组（每句一个对象 {{id, scene, at, text}}），"
            f"现在是 {type(declared).__name__}：{declared!r}",
        )
        return
    if not declared:
        report.warn(item, "voice.lines 是空的：片子里没有任何旁白（确认这是有意为之）")
        return

    # 结构必须先校验（R5-CA-02）：以前非 dict 的行被静默过滤、缺 at 的按 0.0 兜底，
    # 坏 timing.json 能 0 ERROR 通过 check_assets，到渲染/字幕阶段才悄悄少一句。
    lines = []
    for index, line in enumerate(declared):
        if not isinstance(line, dict):
            report.fail(
                item,
                f"voice.lines[{index}] 应该是对象（id/scene/at/text），"
                f"现在是 {type(line).__name__}：{line!r}",
            )
            continue
        missing_keys = [key for key in ("id", "scene", "at", "text") if key not in line]
        if missing_keys:
            report.fail(
                item,
                f"voice.lines[{index}] 缺少必需字段 {'、'.join(missing_keys)}：{line!r}",
            )
            continue
        line_id = str(line.get("id", "")).strip()
        if not line_id:
            report.fail(item, f"voice.lines[{index}] 的 id 是空的：audio-src/vo/<id>.mp3 无从对应")
            continue
        # R9-CA-03：id 是 audio-src/vo/<id>.mp3 的文件名、tts.py 的缓存键与 report.json 的索引。
        # 以前只查「非空」：id="../evil" 只要那个位置真有一份 mp3，闸门就放行 ——
        # 配音文件读写会跑到项目目录外。
        id_unsafe = _unsafe_filename_reason(line_id)
        if id_unsafe:
            report.fail(
                item,
                f"voice.lines[{index}] 的 id=\"{line_id}\" 不能当文件名：{id_unsafe}。"
                "id 会拼成 audio-src/vo/<id>.mp3 并被 tts.py / mix.py 当路径用，"
                "含分隔符 / .. / 盘符会读写到项目目录之外",
            )
            continue
        if not str(line.get("scene", "") or "").strip():
            report.fail(item, f"第 {line_id} 句的 scene 是空的")
            continue
        # R9-CA-01：text 是这一句的「唯一事实来源」——tts.py 拿它合成、subtitles.py 拿它
        # 上屏、mix.py 拿它算人声窗口。以前只查 key 存在：text="" / text=123 / 全空白
        # 都能 0 ERROR 通过，成片里那句变成静音 + 空字幕，而所有闸门绿灯。
        text_raw = line.get("text")
        if not isinstance(text_raw, str):
            report.fail(
                item,
                f"第 {line_id} 句的 text 必须是字符串（旁白台词；数字/对象都不行），"
                f"现在是 {type(text_raw).__name__}：{text_raw!r}",
            )
            continue
        if not text_raw.strip():
            report.fail(
                item,
                f"第 {line_id} 句的 text 是空的 / 只有空白：这句配音是静音、字幕是空行，"
                "但混音 / 字幕 / 渲染全链路都不会报错",
            )
            continue
        at_raw = line.get("at")
        # R8-CA-01：不再接受数字字符串。"at" 是 Caption.tsx 里 `line.at + Math.max(...)`
        # 的直接输入，字符串 "5.0" 会拼成 "5.02.5" → captionUntil 变 NaN，画面内字幕
        # 整段不出现且渲染不报错。类型口径与 fps / captions.bottom 一致。
        if isinstance(at_raw, bool) or not isinstance(at_raw, (int, float)):
            report.fail(
                item,
                f"第 {line_id} 句的 at={at_raw!r} 不是数字（单位：秒，相对片头；"
                "字符串要写成不带引号的数字，否则字幕会静默消失）",
            )
            continue
        at_value = _finite_number(at_raw)
        if at_value is None:
            report.fail(
                item,
                f"第 {line_id} 句的 at={at_raw!r} 不是有限数字（单位：秒，相对片头；"
                "JSON 里不允许写 NaN/Infinity）",
            )
            continue
        lines.append(line)

    # R8-CA-02：id 唯一性。id 是 audio-src/vo/<id>.mp3 的文件名、tts.py 的缓存键与
    # report.json 的索引；重复时后一句的配音会覆盖前一句，而字幕/混音仍按各句 text 摆位，
    # 成片里画面字幕与听到的话对不上，所有闸门却 0 ERROR（R8 已复现）。
    seen_ids = {}
    duplicate_ids = []
    for index, line in enumerate(declared):
        if not isinstance(line, dict):
            continue
        line_id = str(line.get("id", "")).strip()
        if not line_id:
            continue
        first = seen_ids.get(line_id)
        if first is None:
            seen_ids[line_id] = index
        else:
            duplicate_ids.append((line_id, first, index))
    for line_id, first, again in duplicate_ids:
        report.fail(
            item,
            f"voice.lines 的 id「{line_id}」重复（voice.lines[{first}] 与 voice.lines[{again}]）："
            "每句 id 必须唯一 —— 它是 audio-src/vo/<id>.mp3 的文件名与 report.json 的索引，"
            "重复会让后一句的配音覆盖前一句（字幕和听到的话对不上）且不报错",
        )

    if not lines:
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
            at = float(line.get("at"))
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
        # R8-CA-03：字数按「视觉字宽」算（全角 1、半角 0.5，同 _caption_capacity_width()、
        # subtitles.py::_vis_len 与画面内 Caption 的折行口径）。旧代码用 len() 数码点：
        # 32 字符的英文句会被判成 32「字」，在本窗口里 WARN「超过 6.5 字/秒预算，建议删 8 个字」，
        # 而同一次检查的「字幕容量」却判「放得下」—— 同份报告自相矛盾，且英文 TTS
        # (~15 字符/秒) 实际塞得进。中文全角字符的字宽仍是 1，判定不变。
        width = _caption_capacity_width(text)
        if width > budget:
            cut = int(round(width - budget + 0.49))
            over.append((line_id, width, window, max(1, cut)))
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
                f"第 {line_id} 句 {count} 字宽（全角 1 / 半角 0.5）/ 窗口 {window:.2f}s "
                f"只能放 {CHARS_PER_SEC * window:.1f} 字（按 {CHARS_PER_SEC} 字/秒）"
                f"——建议删掉 {cut} 个字。"
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
                # R6-CA-02：只读头部尺寸会放过「头部有效、数据截断」的图（传输中断 /
                # 磁盘写满 / 拷贝没完成），到渲染时那张图才会报 image file is truncated。
                # 这里强制完整解码一遍，坏图在闸门阶段就拦下。
                image.load()
        except Exception as exc:  # noqa: BLE001 - Pillow 抛的异常类型很多
            unreadable.append((path, str(exc)))
            continue
        if width < IMG_MIN_WIDTH:
            small.append((path, width, height))
        elif width > IMG_MAX_WIDTH:
            large.append((path, width, height))

    for path, exc in unreadable:
        report.fail(
            item,
            f"{_rel(path)} 无法完整读取（{exc}）；头部损坏或数据截断都会这样，渲染时读不出来。"
            "截断的截图请重新截/重新拷贝",
        )

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
    """收集 timing.json 里会渲染到画面上的文字（缺字会变方块，必须全查）。

    注意：这里只是**契约字段**里的文案。模板场景里还有大量屏上文字写死在 JSX 文本节点
    （Demo.tsx 的「本月账目」、Cta.tsx 的「免费下载」等），只查本函数会漏掉它们 ——
    源码文本由 `_collect_source_chars()` 补齐（V22）。
    """
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


def _collect_source_chars(report: Report, item: str):
    """收集 src/、web/ 等「会上屏的文本文件」里的非 ASCII 字符（剥注释后）。

    V22：以前缺字闸门只扫 timing.json 的文案，OK 行却写「覆盖了片子里全部字符」——
    模板场景里大量中文写在 JSX 文本节点里，闸门根本看不见（实测 timing.json 口径 95 字，
    真正会上屏的 TSX 文本 207 字，147 个字在闸门视野之外）。

    这里直接复用 fonts.py 的字符集口径（`_src_texts()`：TEXT_SUFFIXES 白名单 + 注释剥离 +
    跳过 out/public/audio-src/scripts），保证 `fonts.py subset` 与 `check_assets.py` 两边
    对「片子里用到哪些字」的结论一致；缩小/扩大扫描面只改 fonts.py 一处。

    返回 (chunks, 扫描文件数)；导入或扫描失败时返回 ([], 0) 并给一条 WARN
    （闸门不能因为工具模块坏了就静默变成「全绿」）。
    """
    try:
        import fonts  # type: ignore  # 延迟导入：fonts.py 也复用本脚本的 _strip_ts_comments
    except Exception as exc:  # noqa: BLE001 - 任何导入失败都只降级，不让闸门崩
        report.warn(
            item,
            f"导入 fonts.py 失败（{exc}），只检查 timing.json 的文案："
            "src/web 里写死在 JSX 的屏上文字可能漏检",
        )
        return [], 0
    try:
        # fonts.py 的 _src_texts 需要 check_assets 模块本身作为「注释剥离器」，
        # 这样两边剥注释用的是同一份实现（V34 的正则字面量分支也一起生效）。
        chunks, scanned = fonts._src_texts(sys.modules[__name__])
    except Exception as exc:  # noqa: BLE001
        report.warn(item, f"扫 src/web 上屏文本失败（{exc}），只检查 timing.json 的文案")
        return [], 0
    # 防御性处理（R5-CA-01 的兜底层）：BOM（U+FEFF）和零宽字符不是「上屏要看到字形」的
    # 字符，不该拿去查字体 cmap。根因（按 UTF-8 读带 BOM 源码文件）由 fonts.py 修成
    # utf-8-sig；这里再兜一层，保证某个文件带 BOM 时缺字闸门也不会误报「字体缺少 」。
    chunks = [chunk.lstrip("\ufeff") for chunk in chunks]
    return chunks, scanned


def _is_project_subset(chosen: Path) -> bool:
    """chosen 是不是项目自己产出的内嵌子集（public/fonts/ 下）。"""
    try:
        chosen.resolve().relative_to((PROJECT_ROOT / "public" / "fonts").resolve())
        return True
    except (ValueError, OSError):
        return False


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

    check_cfg = fonts_cfg.get("check") if isinstance(fonts_cfg, dict) else None
    if not isinstance(fonts_cfg, dict):
        # V28：结构异常必须是干净的 ERROR: 行，而不是在下面 .get() 上抛 AttributeError traceback。
        report.fail(
            item,
            f"fonts.json 的顶层必须是 JSON 对象，现在是 {type(fonts_cfg).__name__}：{fonts_cfg!r}。"
            "修成 {\"roles\": {…}, \"check\": {…}} 再重跑",
        )
        return
    if check_cfg is None:
        check_cfg = {}
    elif not isinstance(check_cfg, dict):
        report.fail(
            item,
            f"fonts.json 的 check 必须是对象（{{systemCandidates, extraChars}}），现在是 "
            f"{type(check_cfg).__name__}：{check_cfg!r}",
        )
        return
    raw_candidates = check_cfg.get("systemCandidates")
    if raw_candidates is None:
        candidates = []
    elif not isinstance(raw_candidates, list) or not all(
        isinstance(candidate, str) for candidate in raw_candidates
    ):
        report.fail(
            item,
            "fonts.json 的 check.systemCandidates 必须是字符串数组，现在是 "
            f"{raw_candidates!r}：这里放字体文件路径（相对项目根或绝对路径），"
            "类型写坏会导致缺字检查静默跳过",
        )
        return
    else:
        candidates = list(raw_candidates)

    # 口径（V22）：内嵌子集存在就只查子集；所有子集都不存在才退回系统字体栈。
    # 子集存在却缺字时**不能**悄悄换成系统字体再查一遍 —— 那会掩盖「文案改了没重跑
    # fonts.py subset」，正是 AGENTS.md 阶段 3 反复警告的方块字场景。
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
    timing_chars = {ch for _source, text in texts for ch in text if not ch.isspace()}
    extra = str(check_cfg.get("extraChars", "") or "")
    if extra:
        texts.append(("fonts.json check.extraChars", extra))
    extra_chars = {ch for ch in extra if not ch.isspace()}
    source_chunks, source_files = _collect_source_chars(report, item)
    source_chars = set()
    if source_chunks:
        joined_sources = "".join(source_chunks)
        source_chars = {ch for ch in joined_sources if not ch.isspace()}
        texts.append((f"src/web 上屏文本（{source_files} 个文件，注释已剥）", joined_sources))

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
        if _is_project_subset(chosen):
            advice = (
                "这是项目内嵌子集：改了文案/加了场景之后必须重跑 "
                "`python scripts/fonts.py subset` 重新生成（缺字会在片子里渲染成方块 □）"
            )
        else:
            advice = (
                "这是系统字体：改用覆盖它们的字体（Noto Sans CJK SC / 思源黑体 / 微软雅黑），"
                "或把文案换成不缺字的写法"
            )
        report.fail(
            item,
            f"{_rel(chosen)} 缺少这些字符：{detail}{more} —— 缺字：「{chars}」。"
            f"它们会渲染成方块（□）。{advice}",
        )
        return

    extras_note = f" + check.extraChars {len(extra_chars)} 个" if extra_chars else ""
    source_note = (
        f" + src/web 上屏文本 {len(source_chars)} 个（{source_files} 个文件，注释已剥）"
        if source_chars
        else ""
    )
    kind = "项目内嵌子集" if _is_project_subset(chosen) else "系统字体"
    report.ok(
        item,
        f"{_rel(chosen)}（{kind}）覆盖了片子里 {checked} 个不重复字符"
        f"（timing.json {len(timing_chars)} 个{extras_note}{source_note}，含标点数字）",
    )


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
# 自测（--selftest / scripts/selftest.py）
# ===========================================================================
#
# 纯函数级自测：不读素材、不跑 ffmpeg、不依赖网络；CI 里当确定性检查跑。

def _selftest() -> int:
    failures = []

    def check(name: str, got, want) -> None:
        if got != want:
            failures.append(f"{name}: 期望 {want!r}，实际 {got!r}")

    # --- 注释剥离（V34：正则字面量、除法、JSX 闭标签都不能吞掉后续代码） ---
    cases = (
        (
            'const a = staticFile("img/a.png"); // 注释 staticFile("img/b.png")',
            ("img/a.png", None),
            "行尾注释里的引用要挖掉",
        ),
        (
            '/* old */\nconst p = staticFile("img/new.png");',
            ("img/new.png", None),
            "块注释里的引用要挖掉",
        ),
        (
            'const re = /https?:\\/\\//;\nconst p = staticFile("img/x.png");',
            ('img/x.png', 'https?:\\/\\/'),
            "正则字面量里的 // 不是注释",
        ),
        (
            'const q = a / b;\nconst p = staticFile("img/y.png");',
            ("img/y.png", None),
            "除法 / 不是正则起点",
        ),
        (
            'return <div>{staticFile("img/z.png")}</div>;',
            ("img/z.png", None),
            "JSX 闭标签的 / 不能当注释起点",
        ),
    )
    for source, (must_have, also_keep), label in cases:
        stripped = _strip_ts_comments(source)
        check(f"注释剥离保留引用（{label}）", must_have in stripped, True)
        if also_keep:
            check(f"注释剥离保留正则原文（{label}）", also_keep in stripped, True)
    check(
        "注释剥离挖掉行尾注释",
        "img/b.png" in _strip_ts_comments('staticFile("img/a.png"); // staticFile("img/b.png")'),
        False,
    )

    # --- 字面量是不是 public/ 素材路径 ---
    for value, want in (
        ("img/a.png", True),
        ("audio/music.wav", True),
        ("fonts/x.ttf", True),
        ("https://example.com/a.png", False),
        ("src/App.tsx", False),
        ("img/{name}.png", False),
        ("", False),
    ):
        check(f"_looks_like_public_asset({value!r})", _looks_like_public_asset(value), want)

    # --- 本地窗口兜底实现（和 _common.line_window 同语义） ---
    timing = {
        "scenes": [{"name": "s1", "start": 0, "end": 10}],
        "voice": {"lines": [
            {"id": "1", "at": 2.0, "scene": "s1"},
            {"id": "2", "at": 5.0, "scene": "s1"},
        ]},
    }
    start, end = _local_window(timing, {"at": 2.0, "scene": "s1"})
    check("_local_window 起点", round(start, 3), 2.0)
    check("_local_window 终点（下一句 at 5.0 减 0.15）", round(end, 3), 4.85)
    last_start, last_end = _local_window(timing, {"at": 5.0, "scene": "s1"})
    check("_local_window 最后一句终点（场景 end 10 减 0.15）", round(last_end, 3), 9.85)

    # --- json 读取：带 UTF-8 BOM 的 timing/fonts 也要能读（R5-CA-01） ---
    import contextlib
    import copy
    import io
    import tempfile

    with tempfile.TemporaryDirectory() as tmpdir:
        bom_path = Path(tmpdir) / "timing-bom.json"
        bom_path.write_bytes(b'\xef\xbb\xbf{"title": "\xe6\xb5\x8b\xe8\xaf\x95"}')
        data, err = _read_json_file(bom_path)
        check("_read_json_file 带 BOM 能读", (data or {}).get("title"), "测试")
        check("_read_json_file 带 BOM 无报错", err, None)

    def _collect_report_errors(func) -> int:
        report = Report(strict=False)
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(buffer):
            func(report)
        return report.errors

    # --- music.segments 结构校验（R5-CA-03）：坏段必须 ERROR ---
    # R8-CA-05：offset 是必填字段，基准夹具也要带上，否则下面「合法 → 0 ERROR」类用例会
    # 因为「缺 offset」而误报，掩盖真正要测的那一项。
    base_music = {"beat": 0.5769, "bar": 2.3076, "drop": 1.5, "offset": 0.0}

    def _segments_errors(segments) -> int:
        timing_fixture = {"music": dict(base_music, segments=segments)}
        return _collect_report_errors(
            lambda report: check_music_fields(report, timing_fixture))

    check("segments 合法 → 0 ERROR",
          _segments_errors([{"at": 22.0, "from": 12.0, "to": 30.0}]), 0)
    check("segments 非数组 → ERROR", _segments_errors("oops") > 0, True)
    check("segments 段非对象 → ERROR", _segments_errors(["x"]) > 0, True)
    check("segments 缺 at → ERROR", _segments_errors([{"from": 1.0, "to": 2.0}]) > 0, True)
    check("segments 空区间 → ERROR",
          _segments_errors([{"at": 1.0, "from": 5.0, "to": 3.0}]) > 0, True)

    # --- voice.lines 结构校验（R5-CA-02）：缺 at / 非对象行必须 ERROR ---
    base_scenes = [{"name": "s1", "start": 0.0, "end": 10.0}]

    def _voice_errors(lines) -> int:
        timing_fixture = {"scenes": base_scenes, "voice": {"lines": lines}}
        return _collect_report_errors(
            lambda report: check_voice_lines(report, timing_fixture))

    check("voice.lines 合法 → 0 ERROR",
          _voice_errors([{"id": "01", "scene": "s1", "at": 2.0, "text": "文案"}]), 0)
    check("voice.lines 缺 at → ERROR",
          _voice_errors([{"id": "01", "scene": "s1", "text": "文案"}]) > 0, True)
    check("voice.lines 非对象行 → ERROR", _voice_errors([1, 2]) > 0, True)
    check("voice.lines 非数组 → ERROR", _voice_errors("oops") > 0, True)
    # R8-CA-02：id 同时是配音文件名 / tts.py 缓存键 / report.json 索引，重复必须 ERROR
    check("voice.lines 重复 id → ERROR", _voice_errors(
        [{"id": "01", "scene": "s1", "at": 2.0, "text": "文案"},
         {"id": "01", "scene": "s1", "at": 4.0, "text": "另一句"}]) > 0, True)

    # --- R9-CA-03/01：id 要能当文件名；text 必须是非空字符串（以前只查 key 存在）---
    check("voice.lines text 空字符串 → ERROR", _voice_errors(
        [{"id": "01", "scene": "s1", "at": 2.0, "text": ""}]) > 0, True)
    check("voice.lines text 数字 → ERROR", _voice_errors(
        [{"id": "01", "scene": "s1", "at": 2.0, "text": 123}]) > 0, True)
    check("voice.lines text 全空白 → ERROR", _voice_errors(
        [{"id": "01", "scene": "s1", "at": 2.0, "text": "  \n "}]) > 0, True)
    for bad_id in ("../../evil", "a/b", "a\\b", "C:x", "..", ".hidden", "CON", "trail."):
        check(f"voice.lines id={bad_id!r} 不能当文件名 → ERROR", _voice_errors(
            [{"id": bad_id, "scene": "s1", "at": 2.0, "text": "文案"}]) > 0, True)
    check("voice.lines id=01-5 合法 → 0 ERROR", _voice_errors(
        [{"id": "01-5", "scene": "s1", "at": 2.0, "text": "文案"}]), 0)

    # --- 数值有限性（R6-CA-01）：NaN / Infinity 是坏配置，不许静默放行 ---
    for value, want in (("1.5", 1.5), (2, 2.0), (None, None), (True, None),
                        ("nan", None), (float("nan"), None),
                        ("inf", None), (float("inf"), None), ("abc", None)):
        check(f"_finite_number({value!r})", _finite_number(value), want)

    base_timing = {
        "slug": "selftest", "fps": 60, "width": 1080, "height": 1920, "aspect": "9:16",
        "duration": 10.0,
        "scenes": [{"name": "s1", "start": 0.0, "end": 10.0}],
        "captions": {"maxChars": 13, "maxLines": 2, "bottom": 0.18},
        "music": dict(base_music),
        "voice": {"lines": []},
    }

    # --- R9-CA-02：brand 是 TS 侧必填契约（缺了渲染期 TypeError，闸门以前 0 ERROR）---
    def _brand_errors(mutate) -> int:
        timing_fixture = copy.deepcopy(base_timing)
        timing_fixture["brand"] = {
            "name": "小账本", "url": "x.app", "logo": "",
            "colors": {"bg": "#000000", "fg": "#ffffff", "accent": "#33e1a1"},
        }
        mutate(timing_fixture)
        return _collect_report_errors(
            lambda report: check_brand_fields(report, timing_fixture))

    check("brand 完整（logo 空串合法）→ 0 ERROR", _brand_errors(lambda t: None), 0)
    check("timing 缺 brand → ERROR", _brand_errors(lambda t: t.pop("brand")) > 0, True)
    check("brand 缺 colors → ERROR",
          _brand_errors(lambda t: t["brand"].pop("colors")) > 0, True)
    check("brand.logo 不是字符串 → ERROR",
          _brand_errors(lambda t: t["brand"].__setitem__("logo", None)) > 0, True)
    check("brand.name 全空白 → ERROR",
          _brand_errors(lambda t: t["brand"].__setitem__("name", " ")) > 0, True)
    check("brand.colors.accent 缺失 → ERROR",
          _brand_errors(lambda t: t["brand"]["colors"].pop("accent")) > 0, True)

    # --- R9-CA-04：music.file 必须是 public/ 下的相对路径（mix.py 按它写出并自动建目录）---
    def _music_file_errors(value) -> int:
        timing_fixture = {"music": dict(base_music, file=value)}
        return _collect_report_errors(
            lambda report: check_music_fields(report, timing_fixture))

    check("music.file 正常相对路径 → 0 ERROR", _music_file_errors("audio/music.wav"), 0)
    for bad_file in ("../../evil.wav", "/abs/evil.wav", "C:/evil.wav",
                     "audio/../evil.wav", 123):
        check(f"music.file={bad_file!r} 越界/坏类型 → ERROR",
              _music_file_errors(bad_file) > 0, True)

    def _timing_structure_errors(mutate) -> int:
        # deepcopy：下面的夹具会改嵌套的 scenes/captions，浅拷贝会让改动串到下一个用例。
        timing_fixture = copy.deepcopy(base_timing)
        mutate(timing_fixture)
        return _collect_report_errors(
            lambda report: check_timing_structure(report, timing_fixture))

    check("timing 正常 → 0 ERROR", _timing_structure_errors(lambda t: None), 0)
    check("timing duration=NaN → ERROR",
          _timing_structure_errors(lambda t: t.__setitem__("duration", float("nan"))) > 0, True)
    check("timing 场景 end=NaN → ERROR",
          _timing_structure_errors(
              lambda t: t["scenes"][0].__setitem__("end", float("nan"))) > 0, True)
    check("timing 场景 start=Infinity → ERROR",
          _timing_structure_errors(
              lambda t: t["scenes"][0].__setitem__("start", float("inf"))) > 0, True)
    # --- fps / 字幕位置（R6-TPL-03）：坏帧率与越界字幕位置以前 0 ERROR 照过闸门 ---
    check("timing fps=0 → ERROR",
          _timing_structure_errors(lambda t: t.__setitem__("fps", 0)) > 0, True)
    check("timing fps 字符串 → ERROR",
          _timing_structure_errors(lambda t: t.__setitem__("fps", "60")) > 0, True)
    check("timing captions.bottom=1.5 → ERROR",
          _timing_structure_errors(
              lambda t: t["captions"].__setitem__("bottom", 1.5)) > 0, True)
    check("timing captions.bottom=0.05 → 0 ERROR",
          _timing_structure_errors(
              lambda t: t["captions"].__setitem__("bottom", 0.05)), 0)
    # 类型与 fps 同口径（R6 复验 minor）：字符串/布尔/NaN 都必须 ERROR，不能靠 _finite_number 放行数字串。
    check("timing captions.bottom 字符串 → ERROR",
          _timing_structure_errors(
              lambda t: t["captions"].__setitem__("bottom", "0.3")) > 0, True)
    check("timing captions.bottom 布尔 → ERROR",
          _timing_structure_errors(
              lambda t: t["captions"].__setitem__("bottom", True)) > 0, True)
    check("timing captions.bottom=NaN → ERROR",
          _timing_structure_errors(
              lambda t: t["captions"].__setitem__("bottom", float("nan"))) > 0, True)
    check("timing captions.top 字符串 → ERROR",
          _timing_structure_errors(
              lambda t: t["captions"].__setitem__("top", "0.1")) > 0, True)

    # --- R7-PY-06：字幕容量按视觉字宽（全角 1 / 半角 0.5）算，不再数字符个数 ---
    # 27 个半角字符（URL/英文/数字串）只有 13.5 个全角字宽，2 行 × 13 放得下；
    # 旧实现 len() 数出 27+ 字符会误报「超过字幕容量」。
    def _caption_capacity_output(lines) -> str:
        timing_fixture = copy.deepcopy(base_timing)
        timing_fixture["captions"] = {"maxChars": 13, "maxLines": 2, "bottom": 0.18}
        timing_fixture["voice"] = {"lines": lines}
        report = Report(strict=False)
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(buffer):
            check_timing_structure(report, timing_fixture)
        return buffer.getvalue()

    latin_long = "https://example.com/really/long/path"  # 36 个半角字符 ≈ 18 字宽
    check("纯拉丁/URL 长句不误报超容量",
          "超过字幕容量" in _caption_capacity_output(
              [{"id": "01", "scene": "s1", "at": 2.0, "text": latin_long}]), False)
    cjk_long = "一二三四五六七八九十一二三四五六七八九十一二三四五六七"  # 27 全角字宽 > 26
    check("真超宽中文仍报超容量",
          "超过字幕容量" in _caption_capacity_output(
              [{"id": "01", "scene": "s1", "at": 2.0, "text": cjk_long}]), True)
    check("_caption_capacity_width('ab')", _caption_capacity_width("ab"), 1)
    check("_caption_capacity_width('中文')", _caption_capacity_width("中文"), 2)

    # --- R8-CA-03：旁白字数也按视觉字宽算（全角 1 / 半角 0.5），不再数字符个数 ---
    # 旧实现用 len()：32 个半角字符在 3.75s 窗口里会被判成「超过 6.5 字/秒预算」，
    # 而同一次检查的字幕容量却判「放得下」；实际 16 字宽 / 英文 TTS ~2.1s 都放得下。
    def _voice_length_output(text_value: str) -> str:
        timing_fixture = {
            "duration": 3.9,
            "scenes": [{"name": "s1", "start": 0.0, "end": 3.9}],
            "voice": {"lines": [{"id": "01", "scene": "s1", "at": 0.0,
                                 "text": text_value}]},
        }
        report = Report(strict=False)
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(buffer):
            check_voice_length(report, timing_fixture)
        return buffer.getvalue()

    check("旁白字数不误报拉丁短句",
          "WARN: 旁白字数" in _voice_length_output(
              "Snap a photo and we do the rest."), False)
    check("旁白字数真超长中文仍 WARN",
          "WARN: 旁白字数" in _voice_length_output(
              "一二三四五六七八九十一二三四五六七八九十一二三四五六"), True)

    check("music.beat=NaN → ERROR", _collect_report_errors(
        lambda report: check_music_fields(
            report, {"music": dict(base_music, beat=float("nan"))})) > 0, True)
    check("music.gain=NaN → ERROR", _collect_report_errors(
        lambda report: check_music_fields(
            report, {"music": dict(base_music, gain=float("nan"))})) > 0, True)
    # --- R7-PY-07：music.offset 必须有限（旧实现完全没校验） ---
    check("music.offset=NaN → ERROR", _collect_report_errors(
        lambda report: check_music_fields(
            report, {"music": dict(base_music, offset=float("nan"))})) > 0, True)
    check("music.offset=Infinity → ERROR", _collect_report_errors(
        lambda report: check_music_fields(
            report, {"music": dict(base_music, offset=float("inf"))})) > 0, True)
    check("music.offset=1.5 → 0 ERROR", _collect_report_errors(
        lambda report: check_music_fields(
            report, {"music": dict(base_music, offset=1.5)})), 0)
    check("music.offset=-0.5（允许负偏移）→ 0 ERROR", _collect_report_errors(
        lambda report: check_music_fields(
            report, {"music": dict(base_music, offset=-0.5)})), 0)
    check("music.offset='oops' → ERROR", _collect_report_errors(
        lambda report: check_music_fields(
            report, {"music": dict(base_music, offset="oops")})) > 0, True)

    # --- R8-CA-05：music.offset / drop 是必填字段，缺失/None/空串都必须 ERROR ---
    # 渲染侧 beats.ts:19 `musicTime + timing.music.offset`、Hook.tsx
    # `TIMING.music.drop - beat.start` 直接运算，undefined 参与算术得 NaN，画面卡点/钩子
    # 静默失效而 check_assets 却 0 ERROR（与 R8-CA-01 同类）。旧实现只在字段存在时校验。
    def _music_errors(music) -> int:
        return _collect_report_errors(
            lambda report: check_music_fields(report, {"music": music}))

    def _music_output(music) -> str:
        report = Report(strict=False)
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(buffer):
            check_music_fields(report, {"music": music})
        return buffer.getvalue()

    music_without_offset = {k: v for k, v in base_music.items() if k != "offset"}
    music_without_drop = {k: v for k, v in base_music.items() if k != "drop"}
    check("music 合法（beat/bar/drop/offset 都有）→ 0 ERROR", _music_errors(base_music), 0)
    check("music.offset 缺失 → ERROR", _music_errors(music_without_offset) > 0, True)
    check("music.drop 缺失 → ERROR", _music_errors(music_without_drop) > 0, True)
    check("music.offset=None → ERROR",
          _music_errors(dict(base_music, offset=None)) > 0, True)
    check("music.drop=None → ERROR",
          _music_errors(dict(base_music, drop=None)) > 0, True)
    check("music.offset 空串 → ERROR",
          _music_errors(dict(base_music, offset="")) > 0, True)
    check("music.drop 空串 → ERROR",
          _music_errors(dict(base_music, drop="")) > 0, True)
    # 报错信息必须点名字段（验收要求）：不能只说「配乐字段有问题」让人猜。
    check("music.offset 缺失的报错点名 music.offset",
          "music.offset 缺失" in _music_output(music_without_offset), True)
    check("music.drop 缺失的报错点名 music.drop",
          "music.drop 缺失" in _music_output(music_without_drop), True)

    check("voice at=NaN → ERROR", _voice_errors(
        [{"id": "01", "scene": "s1", "at": float("nan"), "text": "文案"}]) > 0, True)

    # --- R8-CA-01：时间/尺寸字段必须是 JSON 数字，数字字符串一律 ERROR ---
    # TS 侧对 scenes[].start（Scene.tsx 的 useAbsTime）、voice.lines[].at
    # （Caption.tsx 的 captionUntil）、music.offset（beats.ts 的 musicToVideo）用 `+` 拼接，
    # 字符串会拼成 "4.50.15" → 后续 Number 得 NaN，画面卡点与画面内字幕整段静默消失；
    # check_assets 以前走 _finite_number 的 float() 把 "4.5" 收下，闸门 0 ERROR 假绿。
    for value, want in ((5, 5.0), (5.5, 5.5), (0, 0.0), (1e3, 1000.0), (-0.5, -0.5),
                        ("5.5", None), ("5", None), ("0", None), ("0.0", None),
                        ("1e3", None), ("-0.5", None), (" 0.5 ", None), ("", None),
                        (True, None), (False, None),
                        (float("nan"), None), (float("inf"), None), (None, None), ({}, None)):
        check(f"_strict_number({value!r})", _strict_number(value), want)
    # 负例：真正以字符串为契约的字段（voice id、music.file 等）不能被严格数字校验误伤。
    check("字符串字段不被严格数字校验误伤（voice id='01'）",
          _voice_errors([{"id": "01", "scene": "s1", "at": 2.0, "text": "文案"}]), 0)
    check("字符串字段不被严格数字校验误伤（music.file / gain / offset）",
          _collect_report_errors(
              lambda report: check_music_fields(
                  report, {"music": dict(base_music, file="audio/music.wav",
                                         gain=1.0, offset=0.5)})), 0)
    check("timing duration 字符串 → ERROR",
          _timing_structure_errors(lambda t: t.__setitem__("duration", "10.0")) > 0, True)
    check("timing 场景 start 字符串 → ERROR",
          _timing_structure_errors(
              lambda t: t["scenes"][0].__setitem__("start", "0.0")) > 0, True)
    check("timing 场景 end 字符串 → ERROR",
          _timing_structure_errors(
              lambda t: t["scenes"][0].__setitem__("end", "10.0")) > 0, True)
    check("timing width 字符串 → ERROR",
          _timing_structure_errors(lambda t: t.__setitem__("width", "1080")) > 0, True)
    check("timing captions.maxChars 字符串 → ERROR",
          _timing_structure_errors(
              lambda t: t["captions"].__setitem__("maxChars", "20")) > 0, True)
    check("voice at 字符串 → ERROR", _voice_errors(
        [{"id": "01", "scene": "s1", "at": "2.0", "text": "文案"}]) > 0, True)
    for key in ("beat", "bar", "drop", "gain", "offset"):
        check(f"music.{key} 字符串 → ERROR", _collect_report_errors(
            lambda report, k=key: check_music_fields(
                report, {"music": dict(base_music, **{k: "1.0"})})) > 0, True)
    check("music.segments at 字符串 → ERROR", _segments_errors(
        [{"at": "1.0", "from": 0.0, "to": 2.0}]) > 0, True)

    # --- 截断图片（R6-CA-02）：头部有效、数据截断，必须 ERROR ---
    try:
        from PIL import Image as _PILImage  # type: ignore  # noqa: F401
        has_pillow = True
    except ImportError:
        has_pillow = False
    if has_pillow:
        global PROJECT_ROOT
        original_root = PROJECT_ROOT
        with tempfile.TemporaryDirectory() as tmpdir:
            try:
                PROJECT_ROOT = Path(tmpdir)
                img_dir = PROJECT_ROOT / "public" / "img"
                img_dir.mkdir(parents=True)
                # 1×1 PNG 的头部：IHDR 完整、IDAT 截断 —— Pillow 能读出 size，
                # 但 load() 必抛 "image file is truncated"。
                (img_dir / "broken.png").write_bytes(
                    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
                    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\rIDATx\x9cc")
                check("截断 PNG → ERROR",
                      _collect_report_errors(lambda report: check_images(report, {})) > 0, True)
            finally:
                PROJECT_ROOT = original_root
    else:
        check("截断 PNG 检查跳过（没有 Pillow）", True, True)

    # --- 决策与成本审计（融合项 B：缺账本 WARN；账本存在则结构/需求确认/付费记账/预算 ERROR）---
    import contextlib as _contextlib
    import importlib as _importlib
    import io as _io

    audit = _importlib.import_module("audit")

    def _collect_report(func):
        report = Report(strict=False)
        buffer = _io.StringIO()
        with _contextlib.redirect_stdout(buffer), _contextlib.redirect_stderr(buffer):
            func(report)
        return report

    with tempfile.TemporaryDirectory() as tmpdir_decisions:
        decision_root = Path(tmpdir_decisions)
        # 1) 缺 decisions.json → 只 WARN（存量项目不被新闸门拦红）
        missing_report = _collect_report(
            lambda report: check_decisions(report, root=decision_root))
        check("决策审计：缺 decisions.json → 0 ERROR", missing_report.errors, 0)
        check("决策审计：缺 decisions.json → WARN 提示 init", missing_report.warnings, 1)
        # 2) init 后只有 D001 技术自决基线 → ERROR（阶段 2 需求确认还没落账）
        audit.init_decisions(root=decision_root, budget=0)
        check(
            "决策审计：init 后缺 user_requirement → ERROR",
            _collect_report_errors(
                lambda report: check_decisions(report, root=decision_root)) > 0,
            True,
        )
        # 3) 记上用户需求 → 0 ERROR
        audit.append_decision(
            root=decision_root, kind="user_requirement",
            decision="用户确认：45 秒竖屏，零付费路线")
        check(
            "决策审计：记上需求确认 → 0 ERROR",
            _collect_report_errors(
                lambda report: check_decisions(report, root=decision_root)),
            0,
        )
        # 4) 花了钱却没写付费服务 → ERROR（付费必须逐项记账）
        audit.append_decision(
            root=decision_root, kind="technical_self",
            decision="买了一个素材包", cost=1, paid_service=None)
        unbooked_report = _collect_report(
            lambda report: check_decisions(report, root=decision_root))
        check(
            "决策审计：花钱未写付费服务 → ERROR",
            any("paid_service" in line for line in unbooked_report.rows
                if line.startswith("ERROR:")),
            True,
        )
        # 5) 付费服务超预算 → ERROR（零付费默认预算 0 元）
        audit.append_decision(
            root=decision_root, kind="technical_self",
            decision="试用付费音色", cost=10, paid_service="ElevenLabs")
        check(
            "决策审计：超预算 → ERROR",
            _collect_report_errors(
                lambda report: check_decisions(report, root=decision_root)) > 0,
            True,
        )
        # 6) decisions 结构坏（空数组）→ ERROR
        with open(str(decision_root / "decisions.json"), "w",
                  encoding="utf-8", newline="\n") as handle:
            handle.write('{"schema_version": 1, "decisions": []}')
        check(
            "决策审计：decisions 为空 → ERROR",
            _collect_report_errors(
                lambda report: check_decisions(report, root=decision_root)) > 0,
            True,
        )

    if failures:
        for message in failures:
            sys.stderr.write(f"ERROR: selftest — {message}\n")
        sys.stderr.write(f"ERROR: selftest — {len(failures)} 项失败\n")
        return 1
    print("OK: selftest — 注释剥离 / 素材字面量 / 本地窗口兜底 / json BOM / "
          "music.segments / music.offset / music.drop 必填 / voice.lines / id 唯一性 / "
          "voice.text 非空 / voice.id 文件名安全 / music.file 路径 / brand 结构 / "
          "数值有限性 / 数字类型口径 / "
          "截断图片 / fps / 字幕位置 / 字幕容量字宽 / 旁白字数 / 决策账本 全部通过")
    return 0


# ===========================================================================
# 决策与成本审计（融合项 B）
# ===========================================================================

def check_decisions(report: "Report", root=None) -> None:
    """产物须有 decisions.json，结构完整、金额不超预算；零付费默认通过。

    对应 AGENTS.md 阶段 9 闸门与「只需人拍板三类事」：谁拍板 / 何时 / 花了多少 /
    用没用付费服务必须逐项记账。**存量项目缺账本只 WARN 提醒**（不突然把老项目
    拦红）；账本存在时：结构坏、缺 user_requirement（阶段 2 需求确认）、花了钱却
    没写付费服务、累计金额超预算都是 ERROR（默认预算 0 元，env ``PROMO_BUDGET_CNY``
    可调）。
    """
    try:
        import audit  # type: ignore  # 同目录脚本；sys.path 已插入 scripts/
    except ImportError:
        report.fail("决策审计", "找不到 scripts/audit.py（决策与成本审计闸门缺失）")
        return
    base = Path(root) if root is not None else PROJECT_ROOT
    path = audit.decisions_path(base)
    if not path.is_file():
        # P5：存量项目（模板 copy 出来的老目录）还没有账本时降级为 WARN，
        # 不因为新增闸门把正常渲染流程拦红；交付前应补建。
        report.warn(
            "决策审计",
            "还没建 %s（存量项目可继续；交付前建议跑 python scripts/audit.py init "
            "补建决策/成本账本）" % (path,))
        return
    data, load_err = audit.load_decisions_file(path)
    if load_err is not None:
        report.fail("决策审计", f"{load_err}；先跑 python scripts/audit.py init")
        return
    assert data is not None
    results = audit.validate_decisions(data)
    for row in results:
        text = f"{row['item']}：{row['message']}"
        if row["level"] == "ERROR":
            report.fail("决策审计", text)
        else:
            report.warn("决策审计", text)
    if not any(row["level"] == "ERROR" for row in results):
        summary = audit.audit_summary(data)
        report.ok(
            "决策审计",
            "%d 条决策 / 累计 ¥%.2f / 预算 ¥%.2f（%s）"
            % (summary["count"], summary["total_cost_cny"],
               summary["budget_cny"], summary["budget_source"]),
        )


# ===========================================================================
# main
# ===========================================================================

def main(argv=None) -> int:
    _force_utf8()
    raw_argv = sys.argv[1:] if argv is None else list(argv)
    if "--selftest" in raw_argv:
        return _selftest()
    parser = argparse.ArgumentParser(
        prog="check_assets.py",
        description="promo-forge 渲染前闸门（AGENTS.md 阶段 9）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "输出格式：OK/WARN/ERROR: <检查项> — <说明>；最后一行统计。\n"
            "退出码：0 通过（0 ERROR）；1 有 ERROR。\n"
            "纯函数自测（不读素材/不需要 ffmpeg）：python scripts/check_assets.py --selftest"
        ),
    )
    parser.add_argument("--selftest", action="store_true",
                        help="只跑纯函数自测（注释剥离/字面量判定/窗口兜底；不检查项目）")
    parser.add_argument("--strict", action="store_true",
                        help="把所有 WARN 也当 ERROR（交付前的严格模式）")
    args = parser.parse_args(raw_argv)

    report = Report(strict=args.strict)
    log(f"渲染前检查：{PROJECT_ROOT}")
    log("")
    # 模块级兜底表要按「本轮」清零：同一进程里再调一次 main() 时，
    # 上一轮的行号不能跟着冒出来（否则会出现指向旧输入的幽灵告警）。
    _WINDOW_FALLBACKS.clear()

    timing = load_timing()

    check_asset_refs(report)
    check_brand_and_font_files(report, timing)
    # R9-CA-02：brand 是 TS 侧必填契约（缺了渲染期 TypeError），单独一项校验。
    check_brand_fields(report, timing)
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
    # 融合项 B（成本与决策审计）：产物必须带 decisions.json，结构完整且不超预算。
    # 放在最后：它独立于素材/时间轴，读的是项目根的账本。
    check_decisions(report)

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
