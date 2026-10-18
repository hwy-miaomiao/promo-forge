#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""环境体检：只检查、不安装。有任何 MISSING 就退出码 1。

用途
    AGENTS.md 阶段 0 / 阶段 3 的闸门。检查 Node、npm、pnpm 版本、npm 依赖、ffmpeg/ffprobe、Python、Python 包、
    Playwright 浏览器、中文字体、src/timing.json 契约、素材目录数量、可选 ELEVENLABS_API_KEY。
    每行给出「项目 / 状态 / 说明 / 修复命令」，缺什么照着修复命令装什么。

典型调用
    python scripts/doctor.py              # 在项目根（含 src/timing.json 的目录）里跑
    python scripts/doctor.py --help

依赖
    仅标准库（顺便探测本机有没有 ffmpeg / node / Python 包）。不会安装任何东西。

退出码
    0 = 没有 MISSING（WARN 可接受，符合 AGENTS.md 第 7 节的 doctor.py 闸门）
    1 = 有 MISSING（缺硬依赖或 timing.json 不合格），最后一行为「环境体检失败：M 项 MISSING」
"""

from __future__ import annotations

import argparse
import importlib
import importlib.metadata as importlib_metadata
import json
import os
import re
import sys
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _common as C  # noqa: E402

# ---------------------------------------------------------------------------
# 常量（阈值集中在此，注释标注来源）
# ---------------------------------------------------------------------------

#: Node 最低版本。来源：AGENTS.md 第 6 节「Node ≥18、Python ≥3.9、ffmpeg/ffprobe 是硬依赖」
MIN_NODE_MAJOR = 18
#: pnpm 最低版本。来源：`pnpm-workspace.yaml` 里的 `allowBuilds`（esbuild 挂构建脚本的放行名单）
#: 是 pnpm 10.4 才有的配置项；更老的 pnpm 认不出这一项，esbuild 的 postinstall 会被跳过，
#: 症状是 `remotion render` 时报 `esbuild: Failed to install correctly, please delete node_modules`
#: （装依赖的当天看不出来，渲染那天才炸）。低于这个版本一律 MISSING，不做「反正能装」的妥协。
MIN_PNPM = (10, 4)
#: `node --version` 的输出必须**整行**是一个版本号（`v24.16.0` / `24.16`）。
#: 用整行锚定而不是 `re.search`：`Error: unsupported engine node 20.11.0` 这种报错里
#: 也含「20.11.0」，用 search 会把失败输出当成版本号（BUGS-REPORT P2-14）。
NODE_VERSION_RE = re.compile(r"^v?(\d+)(?:\.(\d+))?(?:\.(\d+))?$")
#: Node 装法（命令缺失时用）。
NODE_INSTALL_FIX = "winget install OpenJS.NodeJS.LTS / brew install node / sudo apt install nodejs npm"
#: pnpm 装法（命令缺失时用）。
PNPM_INSTALL_FIX = "corepack enable pnpm / npm install -g pnpm@11 / winget install pnpm.pnpm"
#: pnpm 版本要求来源（写进 MISSING / WARN 的说明，方便照着查）。
PNPM_FIX_HINT = "pnpm-workspace.yaml 的 allowBuilds 需要 pnpm ≥10.4；package.json 的 packageManager 声明了期望版本"
#: 项目 npm 依赖的关键包（node_modules/<包>/package.json 存在即算装了）。
#: 来源：template/package.json 的 dependencies —— 渲染、打包、接口全靠它们。
NODE_DEP_PACKAGES = ("remotion", "@remotion/cli", "@remotion/bundler", "react")
#: npm 依赖目录（相对项目根）。
NODE_MODULES_DIR = "node_modules"
#: 装依赖的命令（仓库默认 pnpm，npm 兜底）。
NODE_INSTALL_CMD = "pnpm install        # 没装 pnpm 就用 npm install"
#: 项目真正用到的 ffmpeg 滤镜。来源：各脚本实际拼的 -vf / -af / -filter_complex。
#: 缺了不是「少个可选功能」，而是对应阶段直接失败或数值口径变化。
FFMPEG_REQUIRED_FILTERS: List[Tuple[str, str]] = [
    ("ass", "subtitles.py 烧硬字幕（缺了烧字幕必失败）"),
    ("afade", "music.py cut 试听段的淡入淡出（缺了 cut 必失败）"),
    ("ebur128", "check_video.py / mix.py 测响度（缺了退回 loudnorm，数值口径不同）"),
]
#: Python 最低版本。来源：AGENTS.md 第 6 节
MIN_PYTHON = (3, 9)
#: ffmpeg 三种平台的安装命令。来源：AGENTS.md 阶段 0「缺什么就按脚本给出的命令装什么」
FFMPEG_FIX_WIN = "winget install Gyan.FFmpeg"
FFMPEG_FIX_MAC = "brew install ffmpeg"
FFMPEG_FIX_LINUX = "sudo apt install ffmpeg"
#: Python 包装法。来源：AGENTS.md 阶段 0 + template/requirements.txt
PY_FIX = "pip install -r requirements.txt"
#: Playwright 浏览器装法。来源：AGENTS.md 阶段 4「真实界面截图用本仓库脚本」
PW_BROWSER_FIX = "python -m playwright install chromium"
#: 需要检查的 Python 包：(import 名, 发行包名, 用途, 谁用)
PY_PACKAGES: List[Tuple[str, str, str, str]] = [
    ("numpy", "numpy", "数值计算", "music.py 音频分析"),
    ("scipy", "scipy", "滤波 / 重采样", "music.py bars"),
    ("PIL", "Pillow", "图片读写与拼图", "media.py"),
    ("fontTools", "fonttools", "字体缺字检查", "check_assets.py"),
    ("requests", "requests", "HTTP 下载", "media.py grab / music.py get"),
    ("playwright", "playwright", "真实网页截图", "capture.py"),
    ("edge_tts", "edge-tts", "免费配音", "tts.py（默认引擎）"),
]
#: Playwright 浏览器缓存目录。三个平台各查一遍：
#: Windows 用 %LOCALAPPDATA%\ms-playwright；macOS 是 ~/Library/Caches/ms-playwright
#: （Playwright 用的是系统 cache 目录，不是 ~/.cache）；Linux 才是 ~/.cache/ms-playwright。
#: 三个都列进去，跨平台/跨容器（WSL、挂载的 home）少报假 MISSING。
PW_BROWSER_DIRS = [
    ("Windows", Path.home() / "AppData" / "Local" / "ms-playwright"),
    ("macOS", Path.home() / "Library" / "Caches" / "ms-playwright"),
    ("Linux", Path.home() / ".cache" / "ms-playwright"),
]
#: 素材目录（数量为 0 -> WARN）。来源：AGENTS.md 第 2 节目录约定 + 阶段 4/5
ASSET_DIRS = [
    ("audio-src/vo/", "配音文件", {".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg"}),
    ("audio-src/music/", "配乐候选", {".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg"}),
    ("public/img/", "图片素材", {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif", ".tif", ".tiff"}),
]
#: 表格列宽（显示宽度，中文算 2）
COL_WIDTHS = [22, 8, 58, 46]
STATUS_ORDER = {"MISSING": 0, "WARN": 1, "OK": 2}


# ---------------------------------------------------------------------------
# 表格渲染（中文按 2 列宽计算，否则表格会歪）
# ---------------------------------------------------------------------------

def _dwidth(text: str) -> int:
    w = 0
    for ch in text:
        w += 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
    return w


def _dpad(text: str, width: int) -> str:
    return text + " " * max(0, width - _dwidth(text))


def _wrap_paragraph(text: str, width: int) -> List[str]:
    """按显示宽度折行；优先在空格 / 斜杠 / 中文标点处断，断不开就硬断。"""
    text = " ".join(str(text).split())
    if not text:
        return [""]
    # 注意：不在 "." 处断行，否则 3.12.8 / media.py 会被劈开
    breaks = set(" \t/-|，。；、：,;)")
    lines: List[str] = []
    cur = ""
    cur_w = 0
    last_break = -1
    for ch in text:
        ch_w = 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
        if cur_w + ch_w > width and cur:
            if last_break > 0 and _dwidth(cur[:last_break]) <= width:
                head, tail = cur[:last_break].rstrip(), cur[last_break:].lstrip()
                lines.append(head)
                cur, cur_w = tail, _dwidth(tail)
            else:
                lines.append(cur)
                cur, cur_w = "", 0
            last_break = -1
        cur += ch
        cur_w += ch_w
        if ch in breaks:
            last_break = len(cur)
    if cur.strip():
        lines.append(cur)
    return lines or [""]


def _wrap_display(text: str, width: int) -> List[str]:
    """逐行折行；显式换行符保留（用来把长说明拆成多段）。"""
    out: List[str] = []
    for para in str(text).splitlines():
        out.extend(_wrap_paragraph(para, width))
    return out or [""]


@dataclass
class Row:
    item: str
    status: str  # OK | WARN | MISSING
    detail: str
    fix: str = ""


def _print_table(rows: List[Row]) -> None:
    header = ["项目", "状态", "说明", "修复命令"]
    widths = list(COL_WIDTHS)
    print("  ".join(_dpad(h, w) for h, w in zip(header, widths)).rstrip())
    print("  ".join("-" * w for w in widths))
    for row in rows:
        cols = [
            _wrap_display(row.item, widths[0]),
            [row.status],
            _wrap_display(row.detail, widths[2]),
            _wrap_display(row.fix or "-", widths[3]),
        ]
        height = max(len(c) for c in cols)
        for i in range(height):
            cells = []
            for c, w in zip(cols, widths):
                cells.append(_dpad(c[i] if i < len(c) else "", w))
            print("  ".join(cells).rstrip())


# ---------------------------------------------------------------------------
# 单项检查
# ---------------------------------------------------------------------------

def check_node(rows: List[Row]) -> None:
    """node 版本：分三种情形——命令缺失 / 输出解析不出主版本号 / 版本真的偏低。"""
    ver = C.cmd_version("node", "--version")
    if ver is None:
        rows.append(Row("Node.js", "MISSING", f"没找到 node（需要 ≥ v{MIN_NODE_MAJOR}）",
                        NODE_INSTALL_FIX))
        return
    parsed = _parse_version(ver)
    if parsed is None:
        # 输出不是版本号：可能是 PATH 里排在前面的 shim / 包装脚本抢了 node，也可能只是打印了别的东西。
        # 这种情况「装/升级 Node」未必有用，所以不给升级建议，先让人看清 node 到底是谁。
        rows.append(Row("Node.js", "MISSING",
                        f"`node --version` 的输出不是版本号：{ver}（解析不出主版本号，按不满足 ≥ v{MIN_NODE_MAJOR} 处理）",
                        "先看 `node --version` 到底打印了什么，再看 node 是谁："
                        "where node（Windows）/ which -a node（macOS、Linux）；"
                        "多半是 PATH 里排在前面的 nvm / conda / 包装脚本抢了 node，"
                        "让 node 指向官方发行版即可"))
        return
    major = parsed[0]
    if major >= MIN_NODE_MAJOR:
        rows.append(Row("Node.js", "OK", f"{ver}（要求 ≥ v{MIN_NODE_MAJOR}）", ""))
    else:
        rows.append(Row("Node.js", "MISSING", f"{ver} 太老（要求 ≥ v{MIN_NODE_MAJOR}）",
                        "winget install OpenJS.NodeJS.LTS / brew upgrade node"))


def check_npm(rows: List[Row]) -> None:
    ver = C.cmd_version("npm", "--version")
    if ver is None:
        rows.append(Row("npm", "MISSING", "没找到 npm（Remotion 渲染要用）", "随 Node.js 一起装：winget install OpenJS.NodeJS.LTS"))
        return
    parsed = _parse_version(ver)
    if parsed is None:
        rows.append(Row(
            "npm", "WARN",
            f"`npm --version` 的输出不是版本号：{ver}（解析不出主版本，无法确认 npm 是否正常）",
            "先看 `npm --version` 到底打印了什么；再看 npm 是谁：where npm（Windows）/ which -a npm",
        ))
    else:
        rows.append(Row("npm", "OK", f"v{ver}", ""))


#: `pnpm --version` 这类输出可能带工具名前缀（`pnpm 10.4.0` / `npm v10.9`）。
#: 两种形态都只认**整串**匹配；解析细节见 `_parse_version` 的 docstring。
_VERSION_LINE_RE = re.compile(r"^v?(\d+)(?:\.(\d+))?(?:\.(\d+))?$")
_VERSION_PREFIX_RE = re.compile(
    r"^([A-Za-z][A-Za-z0-9_.+-]*)\s+v?(\d+)(?:\.(\d+))?(?:\.(\d+))?\s*$")


def _parse_version(text: str) -> Optional[Tuple[int, ...]]:
    """从 `x.y.z` / `vX.Y.Z` / `工具名 x.y.z` 这类**整行输出**里取版本号；取不到返回 None。

    旧实现用 `re.search`：`pnpm --version` 实际打的是
    ``Error: unsupported engine node 20.11.0``（pnpm 自己版本不兼容时的报错）时，
    search 会把报错里的 20.11.0 当成 pnpm 版本，判成「满足 ≥10.4」直接放行；
    corepack 的 ``Corepack is about to download …/pnpm-11.7.0.tgz`` 同理
    （BUGS-REPORT P2-14）。现在只认整行版本号（或单个「工具名 + 版本」前缀），
    解析不出返回 None，调用方按「无法确认」报 WARN/MISSING，而不是误判通过。
    """
    if not text:
        return None
    stripped = text.strip()
    match = _VERSION_LINE_RE.match(stripped)
    if match is not None:
        groups = match.groups()
    else:
        match = _VERSION_PREFIX_RE.match(stripped)
        if match is None:
            return None
        groups = match.groups()[1:]  # 去掉「工具名」那一段
    return tuple(int(g) for g in groups if g is not None)


def _project_package_manager() -> str:
    """读 package.json 的 `packageManager`（形如 `pnpm@11.7.0`）；读不到返回空串。

    读不到就返回空串而不是抛错：doctor 是环境体检，package.json 缺失/不合法由别的检查项报，
    这里只管「有没有声明、声明的够不够新」。
    """
    try:
        raw = (C.PROJECT_ROOT / "package.json").read_text(encoding="utf-8-sig", errors="replace")
        data = json.loads(raw)
    except (OSError, ValueError):
        return ""
    if not isinstance(data, dict):
        return ""
    return str(data.get("packageManager", "") or "").strip()


def check_pnpm(rows: List[Row]) -> None:
    """pnpm 版本：`pnpm-workspace.yaml` 的 `allowBuilds` 要 pnpm ≥10.4，低于它 esbuild 装不全。

    三种情形与 check_node 同构：命令缺失 / 版本低于下限 / 满足（顺带报出 package.json 声明的期望版本）。
    没装 pnpm 时按 WARN 报（`npm install` 是文档允许的兜底路径），但说明 pnpm 是模板默认。
    """
    declared = _project_package_manager()
    ver = C.cmd_version("pnpm", "--version")
    if ver is None:
        rows.append(Row(
            "pnpm", "WARN",
            f"没找到 pnpm（模板默认用它；{PNPM_FIX_HINT}）",
            f"{PNPM_INSTALL_FIX}；装不上也可以 npm install 兜底"
            + (f"，但 npm 不认 allowBuilds（package.json 声明的是 {declared}）" if declared else ""),
        ))
        return

    parsed = _parse_version(ver)
    if parsed is None:
        rows.append(Row(
            "pnpm", "WARN",
            f"`pnpm --version` 的输出不是版本号：{ver}"
            f"（解析不出主版本，无法确认是否满足 ≥{'.'.join(str(n) for n in MIN_PNPM)}）",
            f"先看 `pnpm --version` 到底打印了什么；再看 pnpm 是谁：where pnpm（Windows）/ which -a pnpm",
        ))
        return

    need = MIN_PNPM
    got = parsed[: len(need)] + (0,) * max(0, len(need) - len(parsed))
    tail = f"；package.json 声明 {declared}" if declared else ""
    if got >= need:
        rows.append(Row("pnpm", "OK",
                        f"{ver}（要求 ≥{'.'.join(str(n) for n in need)}）{tail}", ""))
    else:
        rows.append(Row(
            "pnpm", "MISSING",
            f"{ver} 太老（要求 ≥{'.'.join(str(n) for n in need)}）："
            "更老的 pnpm 认不出 pnpm-workspace.yaml 的 allowBuilds，esbuild 的构建脚本会被跳过，"
            f"渲染时才炸（`esbuild: Failed to install correctly`）{tail}",
            PNPM_INSTALL_FIX,
        ))


def _pending_builds(node_modules: Path) -> List[str]:
    """读 node_modules/.modules.yaml（pnpm 写的，实际是 JSON），返回还没跑过构建脚本的包名。

    读不到 / 解析不了就返回空表 —— 判断不了就别报（npm、yarn 项目本来就没有这个文件）。
    """
    try:
        # utf-8-sig：pnpm 写的文件可能带 BOM，json.loads 认 BOM 会直接报错。
        text = (node_modules / ".modules.yaml").read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        return []
    try:
        data = json.loads(text)
    except ValueError:
        data = None
    if isinstance(data, dict):
        pending = data.get("pendingBuilds")
        return [str(item) for item in pending if str(item).strip()] if isinstance(pending, list) else []
    # 老版本 pnpm 写的是 YAML：pendingBuilds:\n  - esbuild
    m = re.search(r"pendingBuilds\s*:\s*\n((?:\s*-\s*\S+\s*\n)+)", text)
    if not m:
        return []
    return [line.strip().lstrip("-").strip() for line in m.group(1).splitlines() if line.strip()]


def check_node_deps(rows: List[Row]) -> None:
    """npm 依赖是否就位：渲染命令全都跑在 node_modules 上，缺了后面每一步都是 ERR_MODULE_NOT_FOUND。"""
    node_modules = C.PROJECT_ROOT / NODE_MODULES_DIR
    if not node_modules.is_dir():
        rows.append(Row("npm 依赖", "MISSING",
                        f"没找到 {NODE_MODULES_DIR}/：依赖还没装，render / stills / web 全都会直接报 ERR_MODULE_NOT_FOUND",
                        NODE_INSTALL_CMD))
        return
    lack = [pkg for pkg in NODE_DEP_PACKAGES if not (node_modules / pkg / "package.json").is_file()]
    if lack:
        rows.append(Row("npm 依赖", "MISSING",
                        f"{NODE_MODULES_DIR}/ 在，但缺关键包：{'、'.join(lack)}（多半是上一次安装中途断了）",
                        NODE_INSTALL_CMD))
        return
    pending = _pending_builds(node_modules)
    if pending:
        rows.append(Row("npm 依赖", "WARN",
                        f"{NODE_MODULES_DIR}/ 关键包齐全，但 {'、'.join(pending[:3])} 的构建脚本还没跑"
                        f"（esbuild 这类包不跑 postinstall 会缺二进制，渲染才会炸）",
                        "pnpm approve-builds        # 或者 pnpm rebuild"))
        return
    rows.append(Row("npm 依赖", "OK",
                    f"{NODE_MODULES_DIR}/ 关键包齐全（{'、'.join(NODE_DEP_PACKAGES)}）", ""))


def _ffmpeg_filters() -> Optional[Set[str]]:
    """`ffmpeg -filters` 里的滤镜名集合；跑不起来（缺命令、超时、非 0）返回 None。"""
    try:
        cp = C.run(["ffmpeg", "-hide_banner", "-filters"], check=False, timeout=30)
    except Exception:
        return None
    if cp.returncode != 0:
        return None
    names: Set[str] = set()
    for line in (cp.stdout or "").splitlines():
        # 例：` ... acopy             A->A       (null)`；图例行（`T.. = Timeline support`）不会命中。
        m = re.match(r"^\s*[A-Z.]{3}\s+(\S+)\s+[AVN|]+->[AVN|]+", line)
        if m:
            names.add(m.group(1))
    return names or None


def check_ffmpeg(rows: List[Row]) -> None:
    """ffmpeg 与 ffprobe 一起查：缺任意一个都算 MISSING；都在时再查本项目必需的滤镜在不在。"""
    fix = f"{FFMPEG_FIX_WIN}  |  {FFMPEG_FIX_MAC}  |  {FFMPEG_FIX_LINUX}"
    have = {name: C.has_cmd(name) for name in ("ffmpeg", "ffprobe")}
    missing = [n for n, ok in have.items() if not ok]
    if not missing:
        ver = C.cmd_version("ffmpeg", "-version") or "ffmpeg"
        short = ver.split()[2] if len(ver.split()) > 2 else ver
        filters = _ffmpeg_filters()
        if filters is None:
            rows.append(Row("ffmpeg/ffprobe", "OK",
                            f"{short}（音频转换、时长探测、剪段都靠它；`ffmpeg -filters` 没跑起来，本次没查滤镜能力）", ""))
            return
        lack = [(name, why) for name, why in FFMPEG_REQUIRED_FILTERS if name not in filters]
        if not lack:
            rows.append(Row("ffmpeg/ffprobe", "OK", f"{short}（音频转换、时长探测、剪段都靠它）", ""))
            return
        detail = (f"{short} 缺 {len(lack)} 个滤镜：" + "、".join(name for name, _ in lack)
                  + "（" + "；".join(f"{name} → {why}" for name, why in lack) + "）")
        rows.append(Row("ffmpeg/ffprobe", "WARN", detail,
                        "换完整版 ffmpeg（这个构建多半是某个工具自带的裁剪版）：" + fix))
        return
    detail = "缺 " + "、".join(missing) + "（AGENTS.md 第 6 节硬依赖，没有它不要往下走）"
    rows.append(Row("ffmpeg/ffprobe", "MISSING", detail, fix))


def check_python(rows: List[Row]) -> None:
    v = sys.version_info
    ver = f"{v.major}.{v.minor}.{v.micro}"
    need = ".".join(str(x) for x in MIN_PYTHON)
    if (v.major, v.minor) >= MIN_PYTHON:
        rows.append(Row("Python", "OK", f"{ver}（要求 ≥ {need}）\n{sys.executable}", ""))
    else:
        rows.append(Row("Python", "MISSING", f"{ver} 太老（要求 ≥ {need}）",
                        "winget install Python.Python.3.12 / brew install python@3.12"))


def check_py_packages(rows: List[Row]) -> None:
    for mod, dist, purpose, used_by in PY_PACKAGES:
        version = ""
        try:
            importlib.import_module(mod)
        except Exception as exc:  # ImportError 或装坏了
            rows.append(Row(f"包 {dist}", "MISSING", f"{purpose}（{used_by}）导入失败：{type(exc).__name__}: {exc}", PY_FIX))
            continue
        try:
            version = importlib_metadata.version(dist)
        except Exception:
            version = "版本未知"
        rows.append(Row(f"包 {dist}", "OK", f"{version} · {purpose}（{used_by}）", ""))


def check_playwright_browser(rows: List[Row]) -> None:
    """playwright 包 + 浏览器缓存目录一起判断。"""
    try:
        import playwright  # noqa: F401
    except Exception:
        rows.append(Row("Playwright 浏览器", "MISSING", "playwright 包装不上，无法截图",
                        f"{PY_FIX} && {PW_BROWSER_FIX}"))
        return

    cli = None
    try:
        cp = C.run([sys.executable, "-m", "playwright", "--version"], check=False, timeout=30)
        if cp.returncode == 0:
            cli = (cp.stdout or "").strip().splitlines()[0] if (cp.stdout or "").strip() else None
    except Exception:
        cli = None

    found_dir: Optional[Path] = None
    browsers: List[str] = []
    for _label, d in PW_BROWSER_DIRS:
        try:
            if d.is_dir():
                entries = sorted(p.name for p in d.iterdir() if p.is_dir())
                if entries:
                    found_dir = d
                    browsers = entries
                    break
        except OSError:
            continue

    chromium = [b for b in browsers if b.lower().startswith("chromium")]
    if found_dir is not None and chromium:
        rows.append(Row("Playwright 浏览器", "OK",
                        f"{cli or 'playwright'} · {C.rel(found_dir)} 里有 {len(chromium)} 个 chromium 版本", ""))
        return
    if found_dir is not None:
        rows.append(Row("Playwright 浏览器", "MISSING",
                        f"{C.rel(found_dir)} 存在但没有 chromium（只有 {', '.join(browsers) or '空'}）",
                        PW_BROWSER_FIX))
        return
    rows.append(Row("Playwright 浏览器", "MISSING",
                    "没找到浏览器缓存目录（" + "、".join(str(d) for _l, d in PW_BROWSER_DIRS) + "）",
                    PW_BROWSER_FIX))


def check_cjk_font(rows: List[Row]) -> None:
    cands = C.system_font_candidates()
    found = C.first_existing_font(cands)
    if found is not None:
        rows.append(Row("中文字体", "OK", f"命中 {found}（来自 fonts.json 的 check.systemCandidates）", ""))
        return
    rows.append(Row("中文字体", "WARN",
                    f"fonts.json 的 {len(cands)} 个候选一个都不存在；联系表标签会退回 Pillow 默认字体（中文变方块）",
                    "装 Noto Sans CJK（apt install fonts-noto-cjk / brew install font-noto-sans-cjk-sc）或确认已装微软雅黑 msyh.ttc"))


def check_timing(rows: List[Row]) -> None:
    """timing.json 契约检查（AGENTS.md 第 5 节）。不合格一律 MISSING（doctor 闸门要求 0 MISSING）。"""
    if not C.TIMING_PATH.exists():
        rows.append(Row("src/timing.json", "MISSING", f"文件不存在：{C.rel(C.TIMING_PATH)}",
                        "从 template 复制一份工程（别手写）：cp -r template <输出目录>"))
        return
    try:
        timing = C.load_json(C.TIMING_PATH)
    except ValueError as exc:
        rows.append(Row("src/timing.json", "MISSING", str(exc), "把文件修成合法 JSON（见 AGENTS.md 第 5 节的字段契约）"))
        return
    if not isinstance(timing, dict):
        rows.append(Row("src/timing.json", "MISSING", "顶层不是 JSON 对象", "见 AGENTS.md 第 5 节"))
        return

    problems: List[str] = []
    scenes = timing.get("scenes")
    if not isinstance(scenes, list) or not scenes:
        problems.append("没有 scenes")
        scenes = []

    parsed: List[Tuple[str, float, float]] = []
    for i, s in enumerate(scenes):
        if not isinstance(s, dict):
            problems.append(f"scenes[{i}] 不是对象")
            continue
        try:
            parsed.append((str(s.get("name", f"#{i}")), float(s.get("start")), float(s.get("end"))))
        except (TypeError, ValueError):
            problems.append(f"scenes[{i}] 的 start/end 不是数字")

    for (n1, _s1, e1), (n2, s2, _e2) in zip(parsed, parsed[1:]):
        if s2 < e1 - C.SCENE_TOL:
            problems.append(f"场景 {n1} 与 {n2} 重叠 {C.fmt_sec(e1 - s2)}")
        elif s2 > e1 + C.SCENE_TOL:
            problems.append(f"场景 {n1} 与 {n2} 之间有 {C.fmt_sec(s2 - e1)} 空隙")
    for n, s, e in parsed:
        if e <= s:
            problems.append(f"场景 {n} 的 end({C.fmt_sec(e)}) 不大于 start({C.fmt_sec(s)})")

    duration = timing.get("duration")
    try:
        duration_f = float(duration)
    except (TypeError, ValueError):
        duration_f = None
        problems.append("duration 不是数字")
    if duration_f is not None and parsed:
        last_end = parsed[-1][2]
        if abs(duration_f - last_end) > C.DURATION_TOL:
            problems.append(f"duration={C.fmt_sec(duration_f)} 不等于最后场景 end={C.fmt_sec(last_end)}（容差 ±{C.DURATION_TOL}s）")

    music = timing.get("music") if isinstance(timing.get("music"), dict) else {}
    beat = music.get("beat")
    bar = music.get("bar")
    for key, val in (("music.beat", beat), ("music.bar", bar)):
        try:
            num = float(val)
        except (TypeError, ValueError):
            num = None
        if num is None or num <= 0:
            problems.append(f"{key}={val!r} 必须 > 0（卡点全靠它，AGENTS.md 第 5 节）")

    if problems:
        rows.append(Row("src/timing.json", "MISSING", "；".join(problems),
                        "按 AGENTS.md 第 5 节字段契约修；场景必须升序、首尾相接，duration = 最后一个场景 end"))
    else:
        total = f"{C.fmt_sec(duration_f)}" if duration_f is not None else "时长未知"
        rows.append(Row("src/timing.json", "OK",
                        f"{total} · {len(parsed)} 个场景首尾相接 · beat={beat}s bar={bar}s", ""))


def _count_files(d: Path, exts: set) -> int:
    try:
        return sum(1 for p in d.iterdir() if p.is_file() and p.suffix.lower() in exts)
    except OSError:
        return 0


def check_assets(rows: List[Row]) -> None:
    for rel_dir, label, exts in ASSET_DIRS:
        d = C.PROJECT_ROOT / rel_dir
        if not d.is_dir():
            rows.append(Row(f"素材 {rel_dir}", "WARN", f"目录不存在（{label}）", f"mkdir -p {rel_dir}"))
            continue
        n = _count_files(d, exts)
        if n > 0:
            rows.append(Row(f"素材 {rel_dir}", "OK", f"{n} 个{label}", ""))
        else:
            hint = {
                "audio-src/vo/": "配音还没生成：写好 timing.json 的 voice.lines 后跑 python scripts/tts.py all",
                "audio-src/music/": "还没选配乐：从免费可商用曲库下载 1~3 首放进 audio-src/music/（不许爬音乐网站）",
                "public/img/": "还没有图片素材：用 capture.py 截真实界面，或把用户给的一手素材放进来",
            }[rel_dir]
            rows.append(Row(f"素材 {rel_dir}", "WARN", f"0 个{label}，还没准备素材", hint))


def _read_env_file() -> Dict[str, str]:
    """.env 解析（极简 KEY=VALUE；不引第三方库）。"""
    out: Dict[str, str] = {}
    p = C.PROJECT_ROOT / ".env"
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return out
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def check_elevenlabs(rows: List[Row]) -> None:
    value = os.environ.get("ELEVENLABS_API_KEY", "").strip()
    src = "环境变量"
    if not value:
        value = _read_env_file().get("ELEVENLABS_API_KEY", "").strip()
        src = ".env"
    if value:
        rows.append(Row("ELEVENLABS_API_KEY", "OK", f"已配置（来自 {src}，{len(value)} 字符）", ""))
    else:
        rows.append(Row("ELEVENLABS_API_KEY", "WARN",
                        "没配置。不填也能做片子，配音走免费 edge-tts（AGENTS.md 阶段 7）",
                        "需要更自然的音色时：把项目根的 .env.example 复制成 .env，"
                        "填上 ELEVENLABS_API_KEY=xxx，"
                        "然后 python scripts/tts.py all --engine eleven"))


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# 自测（--selftest / scripts/selftest.py）：
# 纯函数级、不需要 node/ffmpeg/网络，覆盖历史上真的误判过的版本解析。
# ---------------------------------------------------------------------------

def _selftest() -> int:
    failures = []

    def check(name: str, got, want) -> None:
        if got != want:
            failures.append(f"{name}: 期望 {want!r}，实际 {got!r}")

    cases = (
        ("24.16.0", (24, 16, 0)),
        ("v24.16.0", (24, 16, 0)),
        ("10.4", (10, 4)),
        ("pnpm 10.4.0", (10, 4, 0)),
        ("npm v10.9", (10, 9)),
        # 失败输出里的版本号不能被当成工具版本（BUGS-REPORT P2-14）
        ("Error: unsupported engine node 20.11.0", None),
        ("Corepack is about to download https://registry.npmjs.org/pnpm/-/pnpm-11.7.0.tgz", None),
        ("", None),
        ("   ", None),
    )
    for text, want in cases:
        check(f"_parse_version({text!r})", _parse_version(text), want)

    if failures:
        for message in failures:
            sys.stderr.write(f"ERROR: selftest — {message}\n")
        sys.stderr.write(f"ERROR: selftest — {len(failures)} 项失败\n")
        return 1
    print("OK: selftest — 版本解析（整行锚定，失败输出不误判）全部通过")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="doctor.py",
        description="promo-forge 环境体检：只检查、不安装。有任何 MISSING 就退出码 1（AGENTS.md 阶段 0 闸门）。",
        epilog="装完缺的东西再跑一次，直到没有 MISSING 再开工。WARN 可以带着走，但要心里有数。\n"
               "纯函数自测（不检查环境）：python scripts/doctor.py --selftest",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--version", action="version", version="promo-forge doctor 1.0")
    p.add_argument("--selftest", action="store_true",
                   help="只跑纯函数自测（版本解析等；不检查环境、不需要 node/ffmpeg）")
    return p


def main(argv: Optional[List[str]] = None) -> int:
    raw_argv = sys.argv[1:] if argv is None else list(argv)
    if "--selftest" in raw_argv:
        return _selftest()
    build_parser().parse_args(argv)

    print(f"promo-forge 环境体检")
    print(f"项目根：{C.PROJECT_ROOT}")
    print("")

    rows: List[Row] = []
    for fn in (
        check_node,
        check_npm,
        check_pnpm,
        check_node_deps,
        check_ffmpeg,
        check_python,
        check_py_packages,
        check_playwright_browser,
        check_cjk_font,
        check_timing,
        check_assets,
        check_elevenlabs,
    ):
        try:
            fn(rows)
        except Exception as exc:  # 单项检查崩了不该拖垮整张表
            rows.append(Row(fn.__name__, "MISSING", f"检查自身出错：{type(exc).__name__}: {exc}", "把这条报给维护者"))

    _print_table(rows)

    missing = [r for r in rows if r.status == "MISSING"]
    warns = [r for r in rows if r.status == "WARN"]
    print("")
    for r in missing:
        C.error(f"{r.item} - {r.detail}" + (f"\n修复：{r.fix}" if r.fix else ""), code=0)
    if missing:
        print(f"环境体检失败：{len(missing)} 项 MISSING")
        if warns:
            print(f"（另有 {len(warns)} 项 WARN 可以带着走）")
        return 1
    print(f"环境体检通过（{len(warns)} 项 WARN）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
