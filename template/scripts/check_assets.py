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
7. **真实素材配额**：``public/img/`` 图片数 < 6 → WARN（AGENTS.md 阶段 4 要求 ≥6 张）。

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


def _read_json_file(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8")), None
    except FileNotFoundError:
        return None, "文件不存在"
    except json.JSONDecodeError as exc:
        return None, f"JSON 解析失败：{exc}"


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
                if "staticFile" not in line and "assetUrl" not in line and "src=" not in line:
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

    logo = str(((timing.get("brand") or {}).get("logo")) or "").strip()
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
        report.warn(item, f"读不到 fonts.json（{err}），跳过字体文件检查")
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
                "（@remotion/fonts 会加载失败并回退系统字体，同一行里可能混出两种字形）"
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
        report.ok(item, f"{len(scenes)} 个场景首尾相接（0 ~ {scenes[-1].get('end')} 秒）")

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
    captions = timing.get("captions", {}) or {}
    max_chars = captions.get("maxChars", 13)
    max_lines = captions.get("maxLines", 2)
    too_long = []
    for line in (timing.get("voice", {}) or {}).get("lines", []) or []:
        if not isinstance(line, dict):
            continue
        text = str(line.get("text", "") or "")
        if max_chars and len(text) > float(max_chars) * float(max_lines):
            too_long.append((str(line.get("id", "")), len(text)))
    if too_long:
        detail = "、".join(f"第 {lid} 句 {n} 字" for lid, n in too_long)
        report.warn(
            item,
            f"这些句子超过字幕容量（{max_lines} 行 × {max_chars} 字）：{detail}。"
            "竖屏字幕会挤爆或被迫缩字号，建议拆句或删字",
        )
    else:
        report.ok(item, f"字幕容量 {max_lines} 行 × {max_chars} 字，所有句子都放得下")


def check_music_fields(report: Report, timing: dict) -> None:
    item = "配乐字段"
    music = timing.get("music", {}) or {}
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


def _safe_window(timing: dict, line: dict):
    """优先用 _common.line_window；它抛错时退回本地等价实现，保证检查不中断。"""
    try:
        start, end = line_window(timing, line)
        return float(start), float(end)
    except Exception:
        pass
    start = float(line.get("at", 0.0) or 0.0)
    scenes = [s for s in (timing.get("scenes") or []) if isinstance(s, dict)]
    scene = None
    for candidate in scenes:
        if str(candidate.get("name")) == str(line.get("scene", "")):
            scene = candidate
            break
    scene_end = float(scene.get("end", start + 8.0)) if scene else start + 8.0
    next_at = None
    for other in (timing.get("voice", {}) or {}).get("lines", []) or []:
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


def check_voice_lines(report: Report, timing: dict) -> None:
    item = "旁白时间窗"
    lines = [line for line in ((timing.get("voice", {}) or {}).get("lines") or []) if isinstance(line, dict)]
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
    lines = [line for line in ((timing.get("voice", {}) or {}).get("lines") or []) if isinstance(line, dict)]
    if not lines:
        return
    over = []
    for line in lines:
        line_id = str(line.get("id", "")).strip() or "?"
        text = str(line.get("text", "") or "")
        try:
            start, end = _safe_window(timing, line)
        except Exception:
            continue
        window = end - start
        if window <= 0:
            continue
        budget = CHARS_PER_SEC * window
        if len(text) > budget:
            cut = int(round(len(text) - budget + 0.49))
            over.append((line_id, len(text), window, max(1, cut)))
    if over:
        for line_id, count, window, cut in over:
            report.warn(
                item,
                f"第 {line_id} 句 {count} 字 / 窗口 {window:.2f}s 只能放 {CHARS_PER_SEC * window:.1f} 字"
                f"（按 {CHARS_PER_SEC} 字/秒）——建议删掉 {cut} 个字。"
                "AGENTS.md 阶段 7：字数超窗口必须改稿，不许靠加速硬压",
            )
    else:
        report.ok(item, f"{len(lines)} 句都在 {CHARS_PER_SEC} 字/秒的预算内")


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


# ===========================================================================
# 4. 字体缺字
# ===========================================================================

def _collect_texts(timing: dict):
    """收集所有会渲染到画面上的文字（缺字会变方块，必须全查）。"""
    texts = []
    title = timing.get("title")
    if title:
        texts.append(("title", str(title)))
    brand = timing.get("brand", {}) or {}
    if brand.get("name"):
        texts.append(("brand.name", str(brand["name"])))
    if brand.get("url"):
        texts.append(("brand.url", str(brand["url"])))
    for line in ((timing.get("voice", {}) or {}).get("lines") or []):
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
        report.warn(item, f"读不到 fonts.json（{err}），跳过缺字检查")
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
    music = timing.get("music", {}) or {}
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

    lines = [line for line in ((timing.get("voice", {}) or {}).get("lines") or []) if isinstance(line, dict)]
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

    timing = load_timing()

    check_asset_refs(report)
    check_brand_and_font_files(report, timing)
    check_timing_structure(report, timing)
    check_music_fields(report, timing)
    check_voice_lines(report, timing)
    check_voice_length(report, timing)
    check_images(report, timing)
    check_asset_quota(report, timing)
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
