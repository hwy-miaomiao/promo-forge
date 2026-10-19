#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""promo-forge · 字体子集化 + 内嵌（fonts.py）。

用途
----
模板默认靠**运行这台机器的系统字体**出字（`fonts.json` 的 `roles.*.file` 是空串）。
把项目复制到另一台没装中文字体的机器上渲染，中文就会变成方块（□）。
本脚本把全片用到的字符从系统字体里切出来，生成几十 KB 的子集字体放进 `public/fonts/`，
并把路径写回 `fonts.json` —— 于是换一台机器渲染也不再缺字。

两个子命令
----------
    python scripts/fonts.py subset [--role body] [--out public/fonts] [--name promo-subset]
      收集全片字符集 → 从系统字体子集化 → 写 ``public/fonts/<name>-<role>.<ext>`` → 更新 ``fonts.json``。
      缺字 → ERROR 非零退出：既不会写出半个字体，也不会改 ``fonts.json``。

    python scripts/fonts.py check
      只检查、不改文件：打印当前走的是「内嵌子集字体」还是「系统字体栈」、覆盖了多少字符、缺哪些字，
      并逐个字符确认字体里有真实轮廓（有 cmap 映射却是空字形的字，渲染出来同样是方块，且不会触发回退）。
      缺字 / 空字形 → ERROR 非零退出。

字符集口径（与 ``check_assets.py`` 一致，另做两处补强）
----------------------------------------------------
1. ``check_assets.py`` 的 ``_collect_texts()``（``title`` / ``brand.name`` / ``brand.url`` /
   ``voice.lines[].text`` / ``scenes[].note``）—— 直接 import 复用，保证两边结论不打架；
2. ``src/timing.json`` 里**所有**字符串（含 ``slug`` / 场景 ``name`` 等第 1 条没覆盖的字段）；
3. 项目内**全部上屏文本文件**（``src/**`` 与 ``web/**`` 的 ts/tsx/js/jsx/mjs/cjs、css/html/svg、
   以及 json）挖掉注释后的**全部非 ASCII 字符**。这里比「只收字符串字面量」更严：
   模板场景里大量中文写在 JSX 文本节点里（例如 ``src/scenes/Demo.tsx`` 的「本月账目」），
   只扫字面量会漏掉它们，渲染时就是方块；注释（``//`` / ``/* */`` / ``<!-- -->`` / JSON 的
   ``_comment`` 键）必须先剥掉，否则注释里的装饰字会进子集、让 subset 因缺字形误报 ERROR；
4. ``fonts.json`` 的 ``check.extraChars``；
5. 可打印 ASCII（英文、数字、半角标点、网址）；
6. 联系表标签兜底字符：``media.py`` / ``check_video.py`` 画在联系表上的中文标题，
   它们用的是 ``check.systemCandidates`` 里第一个存在的字体（见 ``TOOLING_LABEL_TEXTS``）。

典型调用
--------
    python scripts/fonts.py subset                     # 三个角色都嵌（默认 --role all）
    python scripts/fonts.py subset --role body         # 只嵌正文
    python scripts/fonts.py subset --font C:/Windows/Fonts/simhei.ttf
    python scripts/fonts.py check                      # 只检查（交付前 / CI）

依赖
----
必需：fontTools（模板环境里已装）。缺了直接 ERROR（纯标准库做不了子集化）。
可选：brotli —— 有它才能导出 ``.woff2``（体积再小一半），没有就导出 ``.ttf``。
      ``.woff2`` 与 ``.ttf`` 都能被 ``@remotion/fonts`` 的 ``loadFont`` 加载。

退出码
------
0 通过；1 有 ERROR（缺字 / 源字体找不到 / 字体读不出来 / 参数不对）。
"""

from __future__ import annotations

import argparse
import json
import re
import string
import sys
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

# ---------------------------------------------------------------------------
# 导入公共库 scripts/_common.py（与其它脚本共用一套日志/路径/退出码语义）
# ---------------------------------------------------------------------------
sys.path.insert(0, str(Path(__file__).resolve().parent))
try:  # pragma: no cover - 只在 _common.py 缺失时走到
    import _common as C  # type: ignore
except ImportError as exc:  # pragma: no cover
    sys.stderr.write(
        "ERROR: 无法导入 scripts/_common.py（%s）。请确认 scripts 目录完整，"
        "并在项目根目录下运行本脚本。\n" % (exc,)
    )
    raise SystemExit(1)


# ===========================================================================
# 常量
# ===========================================================================

ROLES: Tuple[str, ...] = ("display", "body", "mono")
ROLE_LABEL = {"display": "标题大字", "body": "正文", "mono": "等宽"}
DEFAULT_ROLE_WEIGHT = {"display": 800, "body": 400, "mono": 400}
DEFAULT_OUT = Path("public") / "fonts"
DEFAULT_NAME = "promo-subset"
FLAVORS = ("auto", "woff2", "ttf", "otf")

# 扫源码时跳过的目录（与 check_assets.py 保持一致）
SKIP_DIR_NAMES = {"node_modules", ".git", "dist", "build"}
#: 需要挖掉注释再收字的后缀（TS/JS 有 `//` 与 `/* */`，CSS/HTML/SVG 有块注释，JSON 有 `_comment` 键）。
#: TS 与普通 JS 的注释语法完全相同（含 JSX 里的 `{/* ... */}`），所以共用同一个剥离器：
#: 之前只有 `.ts`/`.tsx` 参与剥注释，`.js`/`.jsx`/`.mjs`/`.cjs`/`.svg` 虽然被扫到却原样收字，
#: 注释里的装饰字（emoji、生僻字）就会让子集化因「源字体覆盖不了这个字符」直接 ERROR。
TS_SUFFIXES = (".ts", ".tsx")
#: 有 `//` 与 `/* */` 注释、需要先剥注释再收字的源码后缀（TS/TSX 与 JS 同语法）。
SCRIPT_SUFFIXES = TS_SUFFIXES + (".js", ".jsx", ".mjs", ".cjs")
#: 会出现在屏幕上的文本文件后缀：TS/TSX 之外的 JSON（timing.json / props-*.json）、
#: CSS（web/style.css 的界面文案）、HTML（web/index.html 的标题）、SVG（矢量图里的 text）。
#: 原来只扫 `src/**/*.{ts,tsx}`，`web/**`（同一套子集 webfont 的网页版）与
#: `src/*.css` 里的中文全漏 —— 子集里没有这些字，网页版就会出方块。
#: 注：`scripts/` 整个目录不扫（见 `_src_texts`）：`stills.mjs` 这类工具脚本的中文只打在
#: 终端上，不进画面，收进来只会白白撑大子集。
TEXT_SUFFIXES = SCRIPT_SUFFIXES + (".json", ".css", ".html", ".svg")

# 子集化时直接丢掉的表：数字签名、位图/彩色字形（web 渲染用不到）与老式 hint 表。
# MERG / meta 留在里面只会让 fontTools 打一行 "NOT subset; don't know how to subset; dropped"。
DROP_TABLES = (
    "DSIG", "EBDT", "EBLC", "EBSC", "SVG ", "MERG", "meta",
    "hdmx", "VDMX", "LTSH", "PCLT",
)

GENERATED_BY = "python scripts/fonts.py subset"

# 可打印 ASCII（不含空白）：数字、英文、半角标点。空白单独处理，不参与计数。
ASCII_TEXT = "".join(sorted(set(string.printable) - set(string.whitespace)))

# 联系表标签兜底字符：media.py / check_video.py 用 `check.systemCandidates` 里第一个存在的字体
# 画这些中文标题（Pillow 不做字形回退，缺字就是方块）。子集字体一旦占据 candidates[0]，
# 必须把这些字也带上，否则人审用的联系表标题会变成方块。
#   media.py:665        f"{title_prefix} —— {n} 张，缩到 {width}px 宽"
#   media.py:719        f"联系表：{rel(path)}"
#   media.py:831        f"cover 裁切预览：{ratio}（anchor={anchor}）—— 共 {n} 张"
#   check_video.py:752  f"{slug} · 审图联系表 · {n} 帧 · 每场景 40% / 80%"
#   check_video.py:761  f"{name}  {second:.2f}s（{pct}%）"
#
# ⚠️ 这张表**兜不住全部**：`联系表：` 后面接的是用户给的目录名，每张缩略图下面还有**文件名**
# （`media.py` 的 `_render_sheet` / `cmd_crop`），`cover 裁切预览：` 里的 ratio/anchor 也是
# 命令行的原样字符串 —— 目录名和文件名是任意中文，这个固定清单永远列不全（实测给
# `--scenes` 之外的自定义目录出联系表时，标题里 10 个字（产告广截材标电素脑首）不在子集 cmap 里）。
# 所以 `media.py` 出联系表时会自己按 cmap 挑一个覆盖得住标签的字体（见其 `_font_covering()`），
# 不指望这里补字；这段保留是为了 check_video.py 与兜底路径仍能拿到固定标题的字。
TOOLING_LABEL_TEXTS: Tuple[str, ...] = (
    "联系表：—— 张，缩到 px 宽",
    "cover 裁切预览：（anchor=）—— 共 张",
    "· 审图联系表 · 帧 · 每场景",
    "s（）%",
)

ROUTE_SUBSET = "内嵌子集字体"
ROUTE_SYSTEM = "系统字体栈"

# 不占任何视觉宽度的不可见字符：有些编辑器（或一次 PowerShell 重定向）会往文件头写 UTF-8 BOM，
# 文案里也可能混进零宽空格。它们一旦留在字符集里，任何字体都缺这个字形 —— 闸门会报
# 「缺 1 个字符…会渲染成方块」，而用户盯着的却是一个看不见、删不掉的字符。所以收集时直接丢掉。
INVISIBLE_CHARS = frozenset("\ufeff\u200b\u200c\u200d\u2060")


# ===========================================================================
# fonts.json 读写
# ===========================================================================

def _read_fonts_cfg() -> Dict[str, Any]:
    """读 fonts.json（不存在或不是合法 JSON 都是 ERROR：它是字体角色表的唯一来源）。"""
    try:
        # utf-8-sig：记事本/某些编辑器存 UTF-8 时会带 BOM，BOM 留在字符串里会让
        # json.loads 报「Expecting value: line 1 column 1」，整条链直接 abort。
        raw = C.FONTS_PATH.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        C.error(f"读不到 {C.rel(C.FONTS_PATH)}：文件不存在（模板里必须有这个文件）")
    except UnicodeDecodeError as exc:
        # UnicodeDecodeError 是 ValueError 子类，不是 OSError；不单独接住就会裸 traceback
        # （R6-fonts-01：GBK 存过的 fonts.json 实测直接抛栈，连 ERROR: 行都没有）。
        C.error(
            f"{C.rel(C.FONTS_PATH)} 不是 UTF-8 编码：{exc}"
            "（用编辑器另存为 UTF-8，别存成 GBK/ANSI）"
        )
    except OSError as exc:
        C.error(f"读不了 {C.rel(C.FONTS_PATH)}：{exc}")
    try:
        cfg = json.loads(raw)
    except json.JSONDecodeError as exc:
        C.error(f"{C.rel(C.FONTS_PATH)} 不是合法 JSON：{exc}")
    if not isinstance(cfg, dict):
        C.error(f"{C.rel(C.FONTS_PATH)} 的顶层必须是对象，实际是 {type(cfg).__name__}")
    return cfg


def _write_fonts_cfg(cfg: Dict[str, Any]) -> None:
    """把字体角色表写回去（保持 2 空格缩进、中文不转义、LF 换行，方便人读 diff）。

    显式用 ``newline="\\n"``：Python 文本模式默认会把 ``\\n`` 翻成平台换行（Windows 上
    就是 CRLF），而这个仓库里的 json/ts 都是 LF，翻成 CRLF 会让整份 fonts.json 在
    diff 里看起来全变了。这里走 ``open()`` 而不是 ``Path.write_text(newline=...)``，
    因为后者要 Python 3.10+，而 AGENTS.md 第 6 节的下限是 3.9。
    """
    text = json.dumps(cfg, ensure_ascii=False, indent=2) + "\n"
    with C.FONTS_PATH.open("w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)


def _roles_of(cfg: Dict[str, Any]) -> Dict[str, Any]:
    roles = cfg.get("roles")
    return roles if isinstance(roles, dict) else {}


def _role_entry(cfg: Dict[str, Any], role: str) -> Dict[str, Any]:
    raw = _roles_of(cfg).get(role)
    return raw if isinstance(raw, dict) else {}


def _role_weight(cfg: Dict[str, Any], role: str) -> int:
    try:
        return int(_role_entry(cfg, role).get("weight"))
    except (TypeError, ValueError):
        return DEFAULT_ROLE_WEIGHT.get(role, 400)


def _role_stack(cfg: Dict[str, Any], role: str) -> List[str]:
    stack = _role_entry(cfg, role).get("stack")
    if isinstance(stack, list):
        return [str(item) for item in stack]
    return []


def _public_root() -> Path:
    return C.PROJECT_ROOT / "public"


def _public_rel(path: Path) -> str:
    """转成相对 public/ 的 posix 路径（`staticFile()` 的写法）。"""
    try:
        return Path(path).resolve().relative_to(_public_root().resolve()).as_posix()
    except ValueError:
        C.error(
            f"{C.rel(path)} 不在 public/ 下。@remotion/fonts 只能加载 public/ 里的文件"
            "（`staticFile()` 的路径必须相对 public/），请把 --out 指到 public/ 里面"
        )


def _project_rel(path: Path) -> str:
    """转成相对项目根的 posix 路径（systemCandidates 里的写法）。"""
    try:
        return Path(path).resolve().relative_to(C.PROJECT_ROOT.resolve()).as_posix()
    except ValueError:
        return Path(path).as_posix()


def _brotli_available() -> Tuple[bool, str]:
    try:
        import brotli  # noqa: F401
    except ImportError as exc:
        return False, str(exc)
    return True, ""


# ===========================================================================
# 字符集收集
# ===========================================================================

def _import_check_assets():
    """复用 check_assets.py 的文案收集与注释剥离，避免两边口径不一致。"""
    try:
        import check_assets  # type: ignore
    except Exception as exc:  # noqa: BLE001 - 队友正在改这个文件时也要能跑
        C.warn(
            f"导入 check_assets.py 失败（{exc}），改用本脚本内置的等价文案收集。"
            "口径可能有细微差别，修好 check_assets.py 后重跑一次"
        )
        return None
    return check_assets


def _fallback_collect_texts(timing: Dict[str, Any]) -> List[Tuple[str, str]]:
    """check_assets._collect_texts() 的等价实现（只在 import 失败时兜底）。"""
    texts: List[Tuple[str, str]] = []
    if timing.get("title"):
        texts.append(("title", str(timing["title"])))
    brand = timing.get("brand", {}) or {}
    if brand.get("name"):
        texts.append(("brand.name", str(brand["name"])))
    if brand.get("url"):
        texts.append(("brand.url", str(brand["url"])))
    for line in ((timing.get("voice", {}) or {}).get("lines") or []):
        if isinstance(line, dict) and line.get("text"):
            texts.append((f"voice.lines[{line.get('id')}].text", str(line["text"])))
    for scene in timing.get("scenes") or []:
        if isinstance(scene, dict) and scene.get("note"):
            texts.append((f"scenes[{scene.get('name')}].note", str(scene["note"])))
    return texts


def _strip_ts_comments(text: str, checker: Any) -> str:
    if checker is not None:
        return checker._strip_ts_comments(text)
    # 兜底：整段原样使用（宁可多收几个注释里的字，也不要漏字变方块）
    return text


#: css 的 `/* ... */` 与 html 的 `<!-- ... -->`。这里**不是**为了重写 CSS/HTML 解析，
#: 只是把注释区域换成空格（保留行数与缩进），避免注释里的装饰字进子集。
_COMMENT_RE = re.compile(r"/\*(?!\*).*?\*/|<!--.*?-->", re.S)


def _strip_style_comments(text: str) -> str:
    """CSS / HTML 的注释也挖掉。

    扩展扫描面到 `src/*.css`、`web/**` 之后，注释里的字会被当成「片子里用到的字」收进子集：
    `src/index.css` 的一条中文注释里有个装饰性 emoji ❗（U+2757），微软雅黑没有这个字形，
    于是 `fonts.py subset` 直接 ERROR「源字体覆盖不了这 1 个字符：❗」——**一个永远不上屏的
    注释字符把整个人工产物挡住了**。`check_assets.py` 的 `check_fonts()` 只看 timing.json 的
    文案（`_collect_texts`），所以剥掉注释不会影响那道闸门的口径。
    """
    prev = None
    while prev != text:
        prev = text
        text = _COMMENT_RE.sub(" ", text)
    return text


def _json_comment_stripped(text: str) -> str:
    """JSON 里的注释键（`_comment` / `_note` …）挖掉，只留**会上屏的字符串值**。

    `props-nosubs.json` 这类文件用 `_comment` 存一段给人看的中文说明（Remotion 会忽略它），
    它一个字节都不进画面，但会把「渲染」「用法」这些字塞进子集。JSON 没有注释语法，
    所以这里按「丢掉 `_` 开头的键，其余字符串值收下」处理；解析失败就原样返回
    （宁可多收几个字，也不要漏掉真正上屏的文案）。
    """
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        return text
    out: List[str] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            for key, item in node.items():
                if isinstance(key, str) and key.startswith("_"):
                    continue
                walk(item)
        elif isinstance(node, (list, tuple)):
            for item in node:
                walk(item)
        elif isinstance(node, str):
            out.append(node)

    walk(data)
    return "".join(out)


def _source_text(raw: str, suffix: str, checker: Any) -> str:
    """按后缀选注释剥离器：JS/TS 系走 check_assets 的公共实现，css/html/svg 走本地实现。"""
    if suffix in SCRIPT_SUFFIXES:
        return _strip_ts_comments(raw, checker)
    if suffix in (".css", ".html", ".svg"):
        return _strip_style_comments(raw)
    if suffix == ".json":
        return _json_comment_stripped(raw)
    return raw


def _json_strings(value: Any) -> List[str]:
    """递归取 JSON 里所有字符串（键也取；数字转字面量，覆盖小数点与负号）。"""
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        out: List[str] = []
        for key, item in value.items():
            out.append(str(key))
            out.extend(_json_strings(item))
        return out
    if isinstance(value, (list, tuple)):
        out = []
        for item in value:
            out.extend(_json_strings(item))
        return out
    if isinstance(value, bool) or value is None:
        return []
    if isinstance(value, (int, float)):
        return [str(value)]
    return []


def _src_texts(checker: Any) -> Tuple[List[str], int]:
    """项目内会上屏的文本文件里挖掉注释后剩下的非 ASCII 字符，以及扫到的文件数。

    扫描根是 `C.PROJECT_ROOT`（原来是 `<root>/src`）：`web/` 的网页版引用**同一套子集
    webfont**，`web/**` 与 `src/*.css` 里的中文以前一个都不在子集里。后缀白名单见
    `TEXT_SUFFIXES`；注释要挖掉（ts/tsx 走 check_assets 的公共实现，css/html 走本地实现，
    json 里的 `_comment` 键同理），否则注释里的装饰字会进子集、并可能因为源字体没有那个
    字形而让整个 subset 失败。
    不扫的目录分两类：生成物 / 依赖（`out/`、`node_modules/`、`public/` 里上一次的子集字体、
    `audio-src/vo/` 的 tts 缓存 —— 删掉缓存不该让子集变小），以及 `scripts/` ——
    里面 `stills.mjs` 之类是**给人看终端输出**的工具脚本，它的中文（帮助文本、报错话术、
    代码注释）一个字都不会进画面，收进来只会白白撑大子集（实测多收 300+ 个用不到的字）。
    所以这里说的是「会上屏的文本」，不是「项目里所有文本」。
    """
    base = C.PROJECT_ROOT
    skip_dirs = SKIP_DIR_NAMES | {"out", "public", "audio-src", "scripts"}
    chunks: List[str] = []
    scanned = 0
    if not base.is_dir():
        return chunks, scanned
    for path in sorted(base.rglob("*")):
        suffix = path.suffix.lower()
        if not path.is_file() or suffix not in TEXT_SUFFIXES:
            continue
        if any(part in skip_dirs for part in path.parts):
            continue
        scanned += 1
        try:
            # utf-8-sig：带 BOM 的 fonts.json / props json 走 _json_comment_stripped 时，
            # BOM 留在字符串里会让 json.loads 失败、回退成整段原文（_note 里的汉字进子集、
            # check 报假缺字，R5-fonts-01）。剥掉 BOM 就与不带 BOM 的文件行为一致。
            raw = path.read_text(encoding="utf-8-sig", errors="replace")
        except OSError as exc:
            C.warn(f"读不了 {C.rel(path)}：{exc}")
            continue
        stripped = _source_text(raw, suffix, checker)
        chunks.append("".join(ch for ch in stripped if ord(ch) > 127))
    return chunks, scanned


@dataclass
class Charset:
    text: str                                     # 喂给子集化工具的字符（末尾补了一个空格）
    chars: str                                    # 参与计数的字符（不含空白，首次出现顺序）
    groups: List[Tuple[str, int]] = field(default_factory=list)   # (来源, 首次贡献字符数)
    origin: Dict[str, str] = field(default_factory=dict)          # 字符 → 最早出现的来源


def build_charset(timing: Dict[str, Any], cfg: Dict[str, Any]) -> Charset:
    checker = _import_check_assets()
    collected = (
        checker._collect_texts(timing) if checker is not None else _fallback_collect_texts(timing)
    )
    src_chunks, scanned = _src_texts(checker)
    extra = str(((cfg.get("check") or {}).get("extraChars")) or "")

    groups: List[Tuple[str, str]] = [
        ("check_assets.py 口径（title / brand / voice / scenes.note）",
         "".join(text for _src, text in collected)),
        ("timing.json 其他字段（全部字符串）", "".join(_json_strings(timing))),
        (f"项目内上屏文本（{scanned} 个文件，注释已挖掉：ts/tsx/js/jsx/mjs/cjs、css/html/svg、json 的 _comment）",
         "".join(src_chunks)),
    ]
    if extra:
        groups.append(("fonts.json check.extraChars", extra))
    groups.append(("联系表标签兜底（media.py / check_video.py）", "".join(TOOLING_LABEL_TEXTS)))
    groups.append(("可打印 ASCII", ASCII_TEXT))

    counted: List[str] = []
    breakdown: List[Tuple[str, int]] = []
    origin: Dict[str, str] = {}
    for label, text in groups:
        fresh = 0
        for char in text:
            if char.isspace() or char in origin:
                continue
            if char in INVISIBLE_CHARS:      # BOM / 零宽字符：没有字形可查，不该进闸门
                continue
            origin[char] = label
            counted.append(char)
            fresh += 1
        breakdown.append((label, fresh))

    chars = "".join(counted)
    # 空格单独补上：不参与计数，但字体里得有（字距/换行会用到空格字形）。
    return Charset(text=chars + " ", chars=chars, groups=breakdown, origin=origin)


def _describe_missing(charset: Charset, missing: Sequence[str], limit: int = 6) -> str:
    """把缺字列成「字符(来源)」，方便直接定位是哪段文案缺字。"""
    parts = []
    for char in list(missing)[:limit]:
        parts.append(f"「{char}」(来自 {charset.origin.get(char, '未知来源')})")
    more = "" if len(missing) <= limit else f" 等共 {len(missing)} 个"
    return "、".join(parts) + more


# ===========================================================================
# 字体面（TTC 集合里的一个子字体）
# ===========================================================================

@dataclass
class Face:
    path: Path
    index: int
    weight: int
    family: str
    subfamily: str
    full: str
    num_fonts: int

    @property
    def describe(self) -> str:
        name = self.full or self.family or self.path.name
        if self.num_fonts > 1:
            return f"{self.path.name} [{self.index}] {name}（usWeightClass={self.weight}）"
        return f"{self.path.name} {name}（usWeightClass={self.weight}）"


def _load_ttfont(path: Path, font_number: int):
    try:
        from fontTools.ttLib import TTFont  # type: ignore
    except ImportError:
        C.error(
            "未安装 fontTools，无法做字体子集化。装它：`pip install fonttools`"
            "（模板的 Python 环境里已装；本脚本不新增任何依赖）"
        )
    return TTFont(str(path), fontNumber=font_number, lazy=True)


def enumerate_faces(path: Path) -> List[Face]:
    """列出一个字体文件里的所有面（TTC/OTC 有多个面，ttf/otf 只有 1 个）。"""
    if not path.is_file():
        C.error(f"字体文件不存在：{C.rel(path)}")
    try:
        probe = _load_ttfont(path, 0)
    except Exception as exc:  # noqa: BLE001
        C.error(f"读不出字体 {C.rel(path)}：{exc}（文件坏了？或者根本不是字体？）")
    try:
        num = int(getattr(getattr(probe, "reader", None), "numFonts", 1) or 1)
    except Exception:  # noqa: BLE001
        num = 1
    finally:
        try:
            probe.close()
        except Exception:  # noqa: BLE001
            pass

    faces: List[Face] = []
    for index in range(max(1, num)):
        try:
            font = _load_ttfont(path, index)
            try:
                try:
                    weight = int(font["OS/2"].usWeightClass)
                except Exception:  # noqa: BLE001
                    weight = 400
                name = font["name"]
                family = name.getDebugName(1) or ""
                subfamily = name.getDebugName(2) or ""
                full = name.getDebugName(4) or ""
            finally:
                try:
                    font.close()
                except Exception:  # noqa: BLE001
                    pass
        except Exception as exc:  # noqa: BLE001
            C.warn(f"{C.rel(path)} 的第 {index} 个面读不出来：{exc}（跳过）")
            continue
        faces.append(Face(path=path, index=index, weight=weight, family=family,
                          subfamily=subfamily, full=full, num_fonts=max(1, num)))
    if not faces:
        C.error(f"{C.rel(path)} 里一个可用的字体面都没有")
    return faces


def _family_match(face: Face, stack: Sequence[str]) -> int:
    """面名与角色 CSS 字体栈的匹配度：0 同名，1 包含关系，2 无关。"""
    wanted = [item.strip().casefold() for item in stack if item and item.strip()]
    family = face.family.strip().casefold()
    full = face.full.strip().casefold()
    if family and family in wanted:
        return 0
    if full and full in wanted:
        return 0
    for item in wanted:
        if item and (item in family or family in item or item in full):
            return 1
    return 2


def _recorded_font_files(cfg: Dict[str, Any]) -> set:
    """子集化产物会写到这些文件（用于避免「拿自己生成的子集当源字体」）。"""
    paths = set()
    subset_meta = cfg.get("subset")
    recorded = subset_meta.get("roles") if isinstance(subset_meta, dict) else None
    if isinstance(recorded, dict):
        for info in recorded.values():
            if isinstance(info, dict) and str(info.get("file", "") or "").strip():
                paths.add((_public_root() / str(info["file"]).strip()).resolve())
    for role in ROLES:
        rel_file = str(_role_entry(cfg, role).get("file", "") or "").strip()
        if rel_file:
            paths.add((_public_root() / rel_file).resolve())
    return paths


def _resolve_candidate(text: str) -> Optional[Path]:
    """systemCandidates 里的路径：绝对路径直接用，相对路径按项目根解析。

    check_assets.py 按 CWD 解析（AGENTS.md 规定脚本都在项目根下跑），
    这里按项目根解析，于是从任何目录调用本脚本都能得到同样的结论。
    """
    try:
        raw = str(text).strip()
    except Exception:  # noqa: BLE001
        return None
    if not raw:
        return None
    try:
        path = Path(raw)
        return (path if path.is_absolute() else C.PROJECT_ROOT / path)
    except OSError:
        return None


def _is_own_subset(path: Path, out_dir: Path, own: set) -> bool:
    try:
        resolved = path.resolve()
    except OSError:
        return False
    if resolved in own:
        return True
    try:
        resolved.relative_to(out_dir)
        return True
    except ValueError:
        return False


def candidate_sources(cfg: Dict[str, Any], out_dir: Path, explicit: Optional[Path]):
    """返回 (候选字体面列表, 打印用的清单行, 是否由 --font 强制指定)。"""
    own = _recorded_font_files(cfg)
    lines: List[str] = []

    if explicit is not None:
        faces = enumerate_faces(explicit)
        lines.append(f"  --font 指定的源字体：{C.rel(explicit)}（{len(faces)} 个面）")
        return faces, lines, True

    faces: List[Face] = []
    for order, candidate in enumerate(((cfg.get("check") or {}).get("systemCandidates") or []), start=1):
        text = str(candidate)
        path = _resolve_candidate(text)
        if path is None or not path.is_file():
            lines.append(f"  [ ] {text}（不存在）")
            continue
        size = C.human(path.stat().st_size)
        if _is_own_subset(path, out_dir, own):
            lines.append(
                f"  [跳过] {C.rel(path)}（{size}）—— 上次生成的子集，只含当时的字符，"
                "拿它当源字体会漏掉新加的字"
            )
            continue
        try:
            found = enumerate_faces(path)
        except Exception as exc:  # noqa: BLE001
            lines.append(f"  [!] {C.rel(path)}（{size}）读不出来：{exc}")
            continue
        lines.append(f"  [{order}] {C.rel(path)}（{size}，{len(found)} 个面）")
        faces.extend(found)

    if not faces:
        C.error(
            "一个可用的源字体都没找到。请装一个中文系统字体"
            "（Windows 自带 C:/Windows/Fonts/msyh.ttc；Linux: `sudo apt install fonts-noto-cjk`；"
            "macOS 自带 PingFang），或用 `--font <路径>` 直接指定字体文件；"
            f"候选清单在 {C.rel(C.FONTS_PATH)} 的 check.systemCandidates"
        )
    return faces, lines, False


def select_faces(faces: List[Face], cfg: Dict[str, Any], roles: Sequence[str],
                 weight_override: Optional[int], font_number: Optional[int]):
    """给每个角色挑一个面：先比字重差，再比候选顺序与字体栈匹配度。"""
    picks: Dict[str, Tuple[Face, str]] = {}
    for role in roles:
        target = weight_override if weight_override is not None else _role_weight(cfg, role)
        stack = _role_stack(cfg, role)
        best = None
        for order, face in enumerate(faces):
            if font_number is not None and face.index != font_number:
                continue
            score = (abs(face.weight - target), order, _family_match(face, stack), face.index)
            if best is None or score < best[0]:
                best = (score, face)
        if best is None:
            C.error(
                f"没有面能匹配角色 {role}：--font-number {font_number} 在这个字体的可选项中不存在。"
                "去掉 --font-number 让脚本按字重自动挑，或换一个面数更多的字体"
            )
        face = best[1]
        note = f"目标字重 {target}，字重差 {abs(face.weight - target)}"
        if weight_override is not None:
            note += "（--weight 覆盖）"
        if font_number is not None:
            note += "（--font-number 强制）"
        picks[role] = (face, note)
    return picks


# ===========================================================================
# 子集化
# ===========================================================================

def _has_cff_outline(font) -> bool:
    """是不是 CFF/CFF2（PostScript）轮廓。

    判据必须同时认 "CFF " 与 "CFF2"：原来只判 "CFF "，于是 CFF2 源（可变字体、
    Noto Sans SC VF 之类）被当成 TrueType 判成 .ttf，写出一个里面装着 OTTO 轮廓的
    `.ttf` —— 正是 `:866-872` 那条 ERROR 想拦的「后缀与内容不符，部分渲染器会拒收」。
    """
    return "CFF " in font or "CFF2" in font


def _extension_for(font, flavor: Optional[str]) -> str:
    if flavor == "woff2":
        return ".woff2"
    if flavor == "otf":
        return ".otf"
    if flavor == "ttf":
        return ".ttf"
    # 后缀按真实轮廓类型定：glyf -> .ttf，CFF/CFF2 -> .otf（漏判 CFF2 就会写出
    # 后缀 .ttf、内容 OTTO 的文件）
    return ".otf" if _has_cff_outline(font) else ".ttf"


def _codepoints(chars: Sequence[str], limit: int = 10) -> str:
    """把字符列成 `U+200B ZERO WIDTH SPACE` 这种能直接搜的形式（诊断不可见字符用）。"""
    items = list(chars)
    parts = [f"U+{ord(char):04X} {unicodedata.name(char, '?')}" for char in items[:limit]]
    if len(items) > limit:
        parts.append(f"…… 另外 {len(items) - limit} 个")
    return "、".join(parts)


def _empty_glyphs(font, chars: str) -> List[str]:
    """回读产物：找出「cmap 里有这个字、字形里却没有轮廓」的字符。

    这比"缺映射"更隐蔽：check_assets.py 那种只看 cmap 的检查发现不了它，
    渲染时浏览器也不会回退到别的字体（映射是有的），结果就是一片空白或一个方块。

    例外：空白字符、零宽字符（U+200B 零宽空格、U+FEFF 之流）与组合用记号**本来**就没有轮廓，
    Python 的 ``str.isspace()`` 不把零宽空格算作空白，所以这里按 Unicode 类别再排一次，
    否则会把「正常的零宽字符」误报成缺字形。
    """
    from fontTools.pens.boundsPen import BoundsPen  # type: ignore

    blank_categories = {"Cc", "Cf", "Mn", "Me", "Zl", "Zp", "Zs"}
    cmap = font.getBestCmap() or {}
    glyph_set = font.getGlyphSet()
    empty: List[str] = []
    for char in chars:
        if char.isspace() or unicodedata.category(char) in blank_categories:
            continue  # 本来就不该有轮廓的字符
        name = cmap.get(ord(char))
        if name is None:
            continue  # 缺映射交给调用方按 missing 处理
        try:
            pen = BoundsPen(glyph_set)
            glyph_set[name].draw(pen)
        except Exception:  # noqa: BLE001
            empty.append(char)
            continue
        if pen.bounds is None:  # 没有轮廓 = 空字形
            empty.append(char)
    return empty


def subset_face(face: Face, chars: str, out_path: Path, flavor: Optional[str]) -> Dict[str, Any]:
    """把一个面切成只含 chars 的字体存到 out_path，再回读校验覆盖率与字形非空。"""
    from fontTools import subset  # type: ignore

    options = subset.Options()
    options.text = chars
    options.notdef_outline = True        # 万一还有字没覆盖，画 .notdef 轮廓而不是留白
    options.layout_features = ["*"]      # 保留 kerning / vert 等排版特性
    options.name_IDs = ["*"]             # 名字留着，方便反查这是哪个字体的子集
    options.name_legacy = True
    options.name_languages = ["*"]
    options.recalc_bounds = True
    options.recalc_timestamp = False     # 别把"今天"写进字体，否则每次生成的二进制都不同
    options.drop_tables = sorted(set(options.drop_tables) | set(DROP_TABLES))
    if flavor:
        options.flavor = flavor

    font = _load_ttfont(face.path, face.index)
    try:
        subsetter = subset.Subsetter(options=options)
        subsetter.populate(text=chars)
        subsetter.subset(font)
        C.ensure_dir(out_path.parent)
        font.save(str(out_path))
    except Exception as exc:  # noqa: BLE001
        C.error(f"子集化失败（{face.describe}）：{exc}")
    finally:
        try:
            font.close()
        except Exception:  # noqa: BLE001
            pass

    try:
        check = _load_ttfont(out_path, 0)
        try:
            cmap = check.getBestCmap() or {}
            glyphs = len(check.getGlyphOrder())
            empty = _empty_glyphs(check, chars)
        finally:
            try:
                check.close()
            except Exception:  # noqa: BLE001
                pass
    except Exception as exc:  # noqa: BLE001
        C.error(f"子集字体写出来了却读不回（{C.rel(out_path)}）：{exc}")

    missing = sorted({char for char in chars if ord(char) not in cmap})
    size = out_path.stat().st_size if out_path.is_file() else 0
    if missing or size <= 0:
        try:
            out_path.unlink()
        except OSError:
            pass
        C.error(
            f"子集校验没通过（{face.describe}）：产物覆盖不了这些字符「{''.join(missing)}」"
            f"（已删掉 {C.rel(out_path)}）。这是 fontTools 的意外行为，"
            "请把这条命令与字体路径报给维护者"
        )
    if empty:
        try:
            out_path.unlink()
        except OSError:
            pass
        C.error(
            f"子集里这些字符是空字形（在任何机器上都会渲成空白或方块，字体回退也救不了）"
            f"：「{''.join(empty)}」（{_codepoints(empty)}）—— 已删掉 {C.rel(out_path)}。"
            "缺字形比缺 cmap 映射更隐蔽，check_assets.py 那种检查看不出来，"
            "请把这条命令与字体路径报给维护者"
        )
    return {"bytes": size, "glyphs": glyphs, "cmapSize": len(cmap), "missing": missing, "emptyGlyphs": empty}


def _cmap_of(path: Path, font_number: int = 0) -> Dict[int, str]:
    #: font_number 必须跟着 `--font-number` / 选中的 Face.index 走：TTC 里各面的 cmap 并不相同，
    #: 固定读第 0 面会用错面的覆盖情况判「缺字」。
    font = _load_ttfont(path, font_number)
    try:
        return font.getBestCmap() or {}
    finally:
        try:
            font.close()
        except Exception:  # noqa: BLE001
            pass


def _empty_glyphs_of(path: Path, chars: str, font_number: int = 0) -> List[str]:
    """打开一个字体文件的第 font_number 面，返回「有 cmap 映射、却没有轮廓」的字符。"""
    font = _load_ttfont(path, font_number)
    try:
        return _empty_glyphs(font, chars)
    finally:
        try:
            font.close()
        except Exception:  # noqa: BLE001
            pass


# ===========================================================================
# 子命令：subset
# ===========================================================================

def _parse_roles(value: str) -> List[str]:
    raw = [item.strip() for item in str(value).split(",") if item.strip()]
    if not raw or raw == ["all"]:
        return list(ROLES)
    unknown = [item for item in raw if item not in ROLES]
    if unknown:
        C.error(
            f"未知角色 {unknown}。可选：all / " + " / ".join(ROLES)
            + "（也可以逗号组合，例如 display,body）"
        )
    return [role for role in ROLES if role in raw]


def cmd_subset(args: argparse.Namespace) -> int:
    roles = _parse_roles(args.role)
    cfg = _read_fonts_cfg()
    timing = C.load_timing()

    out_dir = Path(args.out)
    if not out_dir.is_absolute():
        out_dir = C.PROJECT_ROOT / out_dir
    out_dir = out_dir.resolve()
    out_rel = _public_rel(out_dir)  # 不合法就立刻 ERROR，别等切完字体才发现

    name = str(args.name).strip()
    if not name or "/" in name or "\\" in name:
        C.error(f"--name 只能是文件名前缀（不能带路径分隔符），实际是 {args.name!r}")

    brotli_ok, brotli_err = _brotli_available()
    if args.flavor == "woff2" and not brotli_ok:
        C.error(
            f"--flavor woff2 需要 brotli，但当前 Python 环境里没有：{brotli_err}。"
            "装上才能用（`pip install brotli`）；不想新增依赖就改用默认的 --flavor auto，"
            "会导出 .ttf —— @remotion/fonts 的 loadFont 两种都能加载"
        )
    # 交给 fontTools 的 flavor 只认 "woff"/"woff2"/None：'otf' 不是合法 flavor
    # （save() 里会 AssertionError: Unknown flavor 'otf'），未压缩的 sfnt 必须用 None。
    # 所以 --flavor otf 等价于「原样导出轮廓」= auto 的输出，后缀交给 _extension_for() 按轮廓类型定。
    flavor = "woff2" if (args.flavor == "woff2" or (args.flavor == "auto" and brotli_ok)) else None
    #: 用户显式点名某种轮廓（ttf / otf），None 表示随源字体。
    #: 判据是「有没有 glyf 表」：.ttf = glyf 轮廓（True），.otf = CFF 轮廓（没有 glyf，False）。
    want_outline = {"ttf": True, "otf": False}.get(args.flavor)

    C.log(f"字体子集化：{C.rel(C.TIMING_PATH)} + src/** → {C.rel(out_dir)}")
    if flavor == "woff2":
        C.log("字体格式：woff2（brotli 可用，体积最小；loadFont 同样能加载）")
    elif args.flavor == "auto":
        C.log(
            "字体格式：ttf/otf（按字形类型自动选后缀）"
            f" —— brotli 不可用（{brotli_err}），所以导出不了 .woff2。"
            "装了 brotli 会再小一半，但不装也不影响渲染：@remotion/fonts 两种都能加载"
        )
    else:
        C.log(
            f"字体格式：按 --flavor {args.flavor} 导出未压缩的 sfnt"
            f"（后缀就是 {args.flavor}；源字体的轮廓类型对不上会直接 ERROR）"
        )
        if args.flavor == "otf":
            C.log("（fontTools 没有 otf 这个 flavor，--flavor otf 就是「原样导出 CFF 轮廓」）")
    if args.font:
        C.log(f"源字体：--font {C.rel(Path(args.font))}")
    if args.font_number is not None:
        C.log(f"字体面：--font-number {args.font_number}")
    if args.weight is not None:
        C.log(f"字重：--weight {args.weight}（覆盖所有角色）")
    C.log("")

    # ---- 1. 字符集 ----
    charset = build_charset(timing, cfg)
    C.log(f"字符集：{len(charset.chars)} 个不重复字符（不含空白）")
    for label, count in charset.groups:
        C.log(f"  +{count:<4} {label}")
    C.log("")

    # ---- 2. 源字体面 ----
    explicit = Path(args.font) if args.font else None
    if explicit is not None and not explicit.is_absolute():
        explicit = C.PROJECT_ROOT / explicit
    faces, lines, forced = candidate_sources(cfg, out_dir, explicit)
    C.log("源字体：" if forced else
          "源字体候选（fonts.json check.systemCandidates，已跳过本脚本自己生成的子集）：")
    for line in lines:
        C.log(line)
    C.log("")

    picks = select_faces(faces, cfg, roles, args.weight, args.font_number)
    C.log("角色 → 字体面（--weight / --font-number 的选择结果）：")
    for role in roles:
        face, note = picks[role]
        C.log(f"  {role:<8} {ROLE_LABEL.get(role, ''):<6} {face.describe}，{note}")
    C.log("")

    # ---- 3. 覆盖检查（缺字就什么都别写）----
    problems: List[Tuple[str, Face, List[str]]] = []
    for role in roles:
        face, _note = picks[role]
        cmap = _cmap_of(face.path, face.index)
        missing = [char for char in charset.chars if ord(char) not in cmap]
        if missing:
            problems.append((role, face, missing))

    if problems:
        seen_chars: List[str] = []
        for _role, _face, missing in problems:
            for char in missing:
                if char not in seen_chars:
                    seen_chars.append(char)
        C.log("缺字：")
        for role, face, missing in problems:
            C.log(f"  {role}: {face.describe} 缺 {len(missing)} 个：「{''.join(missing)}」")
        detail = _describe_missing(charset, seen_chars)
        C.error(
            f"源字体覆盖不了片子里这 {len(seen_chars)} 个字符：{detail}。"
            "它们在片子里会渲染成方块（□）。先按每个字后面的「来自 …」核对来源："
            "如果来自注释或示例代码，说明它根本不上屏——把那段装饰字删掉，"
            "或改成脚本能剥干净的写法（本脚本会剥 ts/tsx/js/jsx/mjs/cjs 的 `//`、`/* */`，"
            "css/html/svg 的块注释，以及 json 的 `_comment` 键；字符串字面量里的假文案剥不掉，"
            "要手动挪走）；如果确实是上屏文案，再换一个覆盖它们的字体"
            "（`--font C:/Windows/Fonts/msyh.ttc` / Noto Sans CJK SC / 思源黑体），"
            "或把文案换成不缺字的写法后重跑"
        )

    # ---- 4. 子集化 ----
    # 多个角色常常落在同一个字体面（例如 body 与 mono 都用 msyh.ttc 的 Regular）：
    # 那是一模一样的字节，复用同一份文件，别在 public/fonts/ 里放两个副本。
    C.log("产物（源字体 → 子集）：")
    results: Dict[str, Dict[str, Any]] = {}
    reused: Dict[Tuple[str, int, str], str] = {}   # (字体文件, 面号, 后缀) → 已经生成它的角色
    for role in roles:
        face, _note = picks[role]
        probe = _load_ttfont(face.path, face.index)
        try:
            if want_outline is not None:
                has_glyf = "glyf" in probe
                #: CFF2 源（可变字体的 PostScript 轮廓）没有 glyf 表，所以本来就走
                #: want_outline is False 这一支、「对得上」不报错；这里只是把描述文案
                #: 说准（别把 CFF2 说成 CFF）。判据统一走 _has_cff_outline()。
                cff = _has_cff_outline(probe)
                if has_glyf != want_outline:
                    outline = ("glyf（TrueType 轮廓，只能出 .ttf）" if has_glyf
                               else "CFF2（PostScript 轮廓，只能出 .otf）" if cff
                               else "既没有 glyf 也没有 CFF/CFF2 轮廓")
                    # 注：other 的取值原样留着（原代码就取反了：要 .ttf 时却建议
                    # 「换一个 glyf/.ttf」= 又给一个同样的字体）。这次只改轮廓判据，不动它。
                    other = "glyf/.ttf" if want_outline else "CFF/.otf"
                    C.error(
                        f"--flavor {args.flavor} 和源字体对不上：{C.rel(face.path)} 是 {outline}。"
                        "fontTools 不会在 glyf 与 CFF 之间转换轮廓，硬写出来的文件后缀与内容不符"
                        "（.otf 里塞 glyf / .ttf 里塞 CFF），部分渲染器会拒收。"
                        "两个办法：去掉 --flavor（用 auto，会按轮廓类型自动选后缀），"
                        f"或换一个 {other} 的源字体给 --font。"
                    )
            ext = _extension_for(probe, flavor)
        finally:
            try:
                probe.close()
            except Exception:  # noqa: BLE001
                pass
        target = args.weight if args.weight is not None else _role_weight(cfg, role)
        src_size = face.path.stat().st_size
        key = (face.path.resolve().as_posix(), face.index, ext)
        first = reused.get(key)
        if first is not None:
            info = dict(results[first])
            info.update({"targetWeight": target, "reusedFrom": first})
            out_path = out_dir / Path(str(info["file"])).name
            C.log(
                f"  {C.rel(out_path)}  {C.human(info['bytes'])}"
                f"  ←  与 {first} 复用同一份子集（同一个字体面 + 同一套字符，不再写第二个副本）"
            )
        else:
            out_path = out_dir / f"{name}-{role}{ext}"
            info = subset_face(face, charset.text, out_path, flavor)
            info.update({
                "file": _public_rel(out_path),
                "fontNumber": face.index,
                "face": face.full or face.family,
                "faceWeight": face.weight,
                "targetWeight": target,
                "source": face.path.as_posix(),
                "sourceBytes": src_size,
                "flavor": flavor or "sfnt",
                "reusedFrom": None,
            })
            reused[key] = role
            pct = (info["bytes"] / src_size * 100) if src_size else 0.0
            C.log(
                f"  {C.rel(out_path)}  {C.human(info['bytes'])}"
                f"  ←  {face.describe}（原件 {C.human(src_size)} 的 {pct:.2f}%，"
                f"字形 {info['glyphs']} 个，cmap {info['cmapSize']} 个码位，"
                f"字形非空校验 {len(charset.chars) - len(info['emptyGlyphs'])}"
                f"/{len(charset.chars)} 个字符都有轮廓）"
            )
        results[role] = info
    C.log("")

    # ---- 5. 写回 fonts.json ----
    roles_cfg = cfg.setdefault("roles", {})
    if not isinstance(roles_cfg, dict):  # 手改成别的类型了，直接修好
        roles_cfg = {}
        cfg["roles"] = roles_cfg
    for role in roles:
        entry = roles_cfg.get(role)
        if not isinstance(entry, dict):
            entry = {"stack": _role_stack(cfg, role) or ["sans-serif"],
                     "weight": results[role]["targetWeight"], "file": ""}
            roles_cfg[role] = entry
        entry["file"] = results[role]["file"]

    subset_meta = cfg.get("subset")
    if not isinstance(subset_meta, dict):
        subset_meta = {}
    meta_roles = subset_meta.get("roles")
    if not isinstance(meta_roles, dict):
        meta_roles = {}
    for role in roles:
        info = results[role]
        entry = {
            "file": info["file"],
            "source": info["source"],
            "fontNumber": info["fontNumber"],
            "face": info["face"],
            "faceWeight": info["faceWeight"],
            "targetWeight": info["targetWeight"],
            "bytes": info["bytes"],
        }
        if info.get("reusedFrom"):
            entry["reusedFrom"] = info["reusedFrom"]
        meta_roles[role] = entry
    subset_meta.update({
        "generatedBy": GENERATED_BY,
        "out": out_rel,
        "name": name,
        "flavor": flavor or "sfnt",
        "ext": Path(results[roles[0]]["file"]).suffix,
        "chars": len(charset.chars),
        "roles": meta_roles,
    })
    cfg["subset"] = subset_meta

    # systemCandidates[0] 指向子集字体：让 check_assets.py 的缺字检查查「渲染真正会用的那一份」，
    # 而不是这台机器的系统字体。
    check_cfg = cfg.get("check")
    if not isinstance(check_cfg, dict):
        check_cfg = {}
        cfg["check"] = check_cfg
    primary = results[roles[0]]["file"]
    own = _recorded_font_files(cfg)
    generated = {str(results[role]["file"]) for role in roles}
    kept: List[str] = []
    for item in (check_cfg.get("systemCandidates") or []):
        text = str(item)
        normalized = text.replace("\\", "/")
        try:
            resolved: Optional[Path] = Path(text).resolve()
        except OSError:
            resolved = None
        if resolved is not None and resolved in own:
            continue
        if any(normalized == entry or normalized.endswith("/" + entry) for entry in generated):
            continue
        kept.append(text)
    check_cfg["systemCandidates"] = [_project_rel(_public_root() / primary)] + kept

    _write_fonts_cfg(cfg)
    C.log(f"已更新 {C.rel(C.FONTS_PATH)}：")
    for role in roles:
        C.log(f"  roles.{role}.file = {results[role]['file']}")
    C.log(f"  check.systemCandidates[0] = {check_cfg['systemCandidates'][0]}"
          "（check_assets.py 的缺字检查会用它；路径相对项目根）")
    C.log("")

    # 上一次跑留下的、这次没人引用的产物（比如上次嵌了 mono、这次只嵌 display,body）：
    # 只提示不删 —— 脚本不该悄悄删掉用户目录里的文件。
    referenced = {(_public_root() / str(results[role]["file"])).resolve() for role in roles}
    stale: List[str] = []
    for path in sorted(out_dir.glob(f"{name}-*")):
        if not path.is_file() or path.suffix.lower() not in (".ttf", ".otf", ".woff2"):
            continue
        if path.resolve() not in referenced:
            stale.append(f"{C.rel(path)}（{C.human(path.stat().st_size)}）")
    if stale:
        C.log("注意：目录里还有没人引用的旧产物（本次没生成它们，可以自己删掉）：")
        for item in stale:
            C.log(f"  {item}")
        C.log("")
    C.log("说明：联系表的中文标签也用 systemCandidates 里第一个存在的字体画"
          "（media.py / check_video.py），子集里已经带上这些标签字符（见 TOOLING_LABEL_TEXTS）")
    C.log("")
    C.log("下一步：")
    C.log("  python scripts/fonts.py check        # 只看覆盖情况（不改文件）")
    C.log("  npm run stills -- --scenes           # 抽帧确认没有方块")
    C.log("  python scripts/check_assets.py       # 渲染前闸门，字体缺字必须 0 ERROR")
    return 0


# ===========================================================================
# 子命令：check
# ===========================================================================

def cmd_check(args: argparse.Namespace) -> int:
    cfg = _read_fonts_cfg()
    timing = C.load_timing()
    charset = build_charset(timing, cfg)
    total = len(charset.chars)

    C.log(f"字体路线检查（只读，不改任何文件）：{C.rel(C.PROJECT_ROOT)}")
    C.log("")
    C.log(f"字符集：{total} 个不重复字符（不含空白）")
    for label, count in charset.groups:
        C.log(f"  +{count:<4} {label}")
    C.log("")

    embedded: List[Tuple[str, Path]] = []
    recorded: List[Tuple[str, str]] = []
    for role in ROLES:
        rel_file = str(_role_entry(cfg, role).get("file", "") or "").strip()
        if not rel_file:
            continue
        recorded.append((role, rel_file))
        path = _public_root() / rel_file
        if path.is_file() and path.stat().st_size > 0:
            embedded.append((role, path))

    subset_meta = cfg.get("subset") if isinstance(cfg.get("subset"), dict) else {}
    meta_roles = subset_meta.get("roles") if isinstance(subset_meta.get("roles"), dict) else {}

    if embedded:
        C.log(f"当前路线：{ROUTE_SUBSET}（{len(embedded)} 个角色）")
        for role, path in embedded:
            extra = ""
            info = meta_roles.get(role) if isinstance(meta_roles, dict) else None
            if isinstance(info, dict):
                if info.get("face"):
                    extra += f"，面 {info['face']}（usWeightClass={info.get('faceWeight')}）"
                if info.get("targetWeight") is not None:
                    extra += f"，目标字重 {info['targetWeight']}"
            C.log(f"  {role:<8} {C.rel(path)}（{C.human(path.stat().st_size)}{extra}）")
    else:
        C.log(f"当前路线：{ROUTE_SYSTEM}（fonts.json 的 roles.*.file 没有可用的内嵌子集字体）")
        if recorded:
            for role, rel_file in recorded:
                C.log(f"  {role:<8} fonts.json 里写的是 {rel_file}，但 public/{rel_file} 不存在")
            C.warn(
                "fonts.json 里记着子集字体、文件却不在。public/fonts/* 被 .gitignore 忽略，"
                "克隆下来的项目要重跑一次 `python scripts/fonts.py subset` 才会补上；"
                "在那之前 src/lib/fonts.ts 会自动回退系统字体栈（渲染不崩，但换机器就缺字）"
            )
        else:
            C.log("  roles.display / roles.body / roles.mono 的 file 都是空串")
        C.log("  提示：`python scripts/fonts.py subset` 生成子集字体并写回 fonts.json，"
              "换一台机器渲染也不会缺字")
    C.log("")

    gate_path: Optional[Path] = None
    for candidate in ((cfg.get("check") or {}).get("systemCandidates") or []):
        path = _resolve_candidate(str(candidate))
        if path is not None and path.is_file():
            gate_path = path
            break

    targets: List[Tuple[str, Path]] = []
    seen_targets: set = set()

    def _add_target(label: str, path: Path) -> None:
        key = str(path)
        if key in seen_targets:
            return
        seen_targets.add(key)
        targets.append((label, path))

    # 每个角色单独查：内嵌子集是按角色切出来的（display 用 msyhbd、body/mono 用 msyh），
    # 只查「第一个存在的候选」会让其余角色的子集完全不过闸门 —— 那几个角色缺字时
    # 一个字都不报。路径去重，避免 body/mono 共用一份子集时报两遍。
    unprobed: List[str] = []
    for role, rel_file in recorded:
        path = _public_root() / rel_file
        if path.is_file() and path.stat().st_size > 0:
            _add_target(f"内嵌子集 · {role}", path)
        else:
            unprobed.append(f"{role}（{rel_file}）")
    if gate_path is not None and str(gate_path) not in seen_targets:
        _add_target("系统字体（check.systemCandidates）", gate_path)
    if unprobed and gate_path is not None:
        C.warn(
            f"这些角色的字体文件不在 {_project_rel(_public_root())}/ 下，查不到它们的覆盖率"
            f"（已改用 check.systemCandidates 里第一个存在的 {C.rel(gate_path)} 代替）："
            + "、".join(unprobed)
            + "。跑一次 `python scripts/fonts.py subset` 会把它们补齐"
        )
    if not targets:
        C.error(
            "既没有可用的内嵌子集字体，也没有可用的系统字体"
            "（check.systemCandidates 里一个都不存在）。装一个中文字体，"
            "或跑 `python scripts/fonts.py subset --font <你的字体文件>`"
        )

    all_missing: List[Tuple[str, Path, List[str]]] = []
    all_empty: List[Tuple[str, Path, List[str]]] = []
    for label, path in targets:
        cmap = _cmap_of(path)
        missing = [char for char in charset.chars if ord(char) not in cmap]
        covered = total - len(missing)
        pct = covered * 100.0 / max(1, total)
        C.log(f"{label}：{C.rel(path)}")
        C.log(f"  覆盖 {covered}/{total} 个字符（{pct:.1f}%）"
              f"，字体 cmap 共 {len(cmap)} 个码位")
        empty = _empty_glyphs_of(path, charset.chars)
        if empty:
            C.log(f"  空字形 {len(empty)} 个：「{''.join(empty)}」")
            all_empty.append((label, path, empty))
        else:
            C.log(f"  字形非空：这 {covered} 个字符在字体里都有真实轮廓（不会渲成方块）")
        if missing:
            C.log(f"  缺 {len(missing)} 个：「{''.join(missing)}」")
            all_missing.append((label, path, missing))
    C.log("")

    if gate_path is not None:
        C.log(f"check_assets.py 会选中：{C.rel(gate_path)}（check.systemCandidates 里第一个存在的）")
        if embedded and all(path.resolve() != gate_path.resolve() for _role, path in embedded):
            C.warn(
                "内嵌子集字体不在 check.systemCandidates 里：闸门查的是系统字体，"
                "和渲染实际用的不是同一个文件。重跑一次 `python scripts/fonts.py subset` 会把它排到第一位"
            )
    C.log("")

    if all_missing:
        label, path, missing = all_missing[0]
        detail = _describe_missing(charset, missing)
        C.error(
            f"{label} 缺 {len(missing)} 个字符：{detail} —— 它们在片子里会渲染成方块（□）。"
            "换字体（`python scripts/fonts.py subset --font <路径>`），或把文案换成不缺字的写法"
        )

    if all_empty:
        label, path, empty = all_empty[0]
        detail = _describe_missing(charset, empty)
        C.error(
            f"{label} 里这些字符是空字形：{detail} —— 字体里有映射、却没有轮廓，"
            "渲染出来是空白或方块，而且不会触发字体回退。"
            f"（码位：{_codepoints(empty)}）"
            "重跑 `python scripts/fonts.py subset`（生成时会做同样的校验），或换一个字体文件"
        )

    if embedded:
        C.log(f"OK: {ROUTE_SUBSET}覆盖片子里全部 {total} 个不重复字符（含标点数字）")
        C.log(f"    换机器渲染也不会缺字：{len(embedded)} 个角色都指向 public/ 下的子集字体")
    else:
        C.log(f"OK: {ROUTE_SYSTEM}（{C.rel(targets[0][1])}）覆盖片子里全部 {total} 个不重复字符（含标点数字）")
        C.log("")
        C.log("⚠ 但这只在装了中文字体的机器上成立：系统字体栈依赖运行机器的字体。")
        C.log("  要「换机器也不缺字」，跑 `python scripts/fonts.py subset` 把字体嵌进 public/fonts/")
    return 0


# ===========================================================================
# main
# ===========================================================================

def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="fonts.py",
        description="promo-forge 字体子集化 + 内嵌（把全片字符切进几十 KB 的字体，换机器渲染也不缺字）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "例子：\n"
            "  python scripts/fonts.py subset                     # 三个角色都嵌\n"
            "  python scripts/fonts.py subset --role body         # 只嵌正文\n"
            "  python scripts/fonts.py subset --font C:/Windows/Fonts/msyhbd.ttc --font-number 0\n"
            "  python scripts/fonts.py check\n"
            "\n退出码：0 通过；1 有 ERROR（缺字 / 找不到字体 / 参数不对）。"
        ),
    )
    sub = parser.add_subparsers(dest="cmd", required=True, metavar="{subset,check}")

    p_subset = sub.add_parser(
        "subset",
        help="从系统字体切出全片字符集的子集字体，写 public/fonts/ 并更新 fonts.json",
        description="收集全片字符集 → 子集化 → 写 public/fonts/ → 更新 fonts.json。"
                    "缺字则 ERROR 非零退出，且不写任何字体文件、不动 fonts.json。",
    )
    p_subset.add_argument("--role", default="all",
                          help="要嵌哪些角色：all（默认）或 display / body / mono，可逗号组合（display,body）")
    p_subset.add_argument("--out", default=str(DEFAULT_OUT).replace("\\", "/"),
                          help=f"产物目录（相对项目根或绝对路径），默认 {DEFAULT_OUT.as_posix()}；必须在 public/ 下")
    p_subset.add_argument("--name", default=DEFAULT_NAME,
                          help=f"文件名前缀，产物形如 <name>-<role>.ttf，默认 {DEFAULT_NAME}")
    p_subset.add_argument("--font", default=None,
                          help="直接指定源字体文件（默认取 fonts.json check.systemCandidates 里第一个存在的）")
    p_subset.add_argument("--font-number", type=int, default=None,
                          help="TTC 集合里的第几个面（默认按 roles.<role>.weight 就近匹配，并把选择打印出来）")
    p_subset.add_argument("--weight", type=int, default=None,
                          help="覆盖所有角色的目标字重（默认用 fonts.json roles.<role>.weight）")
    p_subset.add_argument("--flavor", choices=FLAVORS, default="auto",
                          help="字体格式：auto（默认，有 brotli 就 woff2，否则按轮廓类型出 .ttf/.otf）/ "
                               "woff2 / ttf（要求源字体是 glyf 轮廓）/ otf（要求源字体是 CFF 轮廓；"
                               "不支持从 glyf 转换，源字体对不上会 ERROR）")
    p_subset.set_defaults(func=cmd_subset)

    p_check = sub.add_parser(
        "check",
        help="只检查不改文件：打印当前用系统字体还是内嵌子集字体、覆盖多少字符、缺哪些字",
        description="打印字体路线与覆盖率；缺字 → ERROR 非零退出。只读，不写任何文件。",
    )
    p_check.set_defaults(func=cmd_check)

    args = parser.parse_args(list(argv) if argv is not None else None)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
