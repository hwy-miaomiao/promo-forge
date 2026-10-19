#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""promo-forge · 阶段 7「配音与字幕」的字幕生成与烧录。

用途
----
把 ``src/timing.json`` 的旁白稿 + **真实配音时长** 变成可直接交付的字幕，并提供硬字幕成片：

1. ``srt`` —— 生成 SRT。时间轴优先用 ffprobe 量 ``audio-src/vo/<id>.mp3`` 的**实测时长**：
   起点取 ``voice.lines[].at``，终点 = 起点 + 实测时长，同时不短于 AGENTS.md 阶段 7 的
   ``字数 / 6 + 0.35`` 秒（两者取较大），且**不得越过该句所属场景的窗口末端**
   （``scene_window()``：字幕活到下一场景开始就是「串场」，是审图时最容易被抓的错）。
2. ``ass`` —— 生成 ASS（Advanced SubStation）。``PlayResX/PlayResY`` 取 timing.json 的
   width/height，字体取 ``fonts.json`` 的 ``roles.body``，字号按画布高度换算、并受
   「左右安全区可用宽度 / captions.maxChars」压上限，
   ``MarginV`` 由 ``captions.bottom`` 换算并压进竖屏安全区（上 10% / 下 18% / 左 6% / 右 14%）。
3. ``burn`` —— 用 ffmpeg 把 ASS 烧成硬字幕（``-vf ass=...``），或 ``--soft`` 封装成软字幕
   （``-c:s mov_text``）。``--preview N`` 只处理前 N 秒，方便先看 8 秒再决定要不要整片跑。
   ASS 不存在、或比 ``src/timing.json`` 旧（改过稿 / 重跑过 tts）时**自动重建**再烧；
   用的是磁盘上已有的新 ASS 时会打一条 WARN，提醒它不校验内容（那份 ASS 不在项目里时，
   WARN 里会额外点名）。

AI 看不到画面，所以这里把「字幕读得完、不串场、不出安全区」全部变成可计算的秒数与像素值。
**烧完仍然要人工审图**：脚本会把抽帧存到 ``out/verify-subs/``，再跑 ``check_video.py`` 复查。

典型调用
--------
    python scripts/subtitles.py srt                       # -> out/subtitles/zh.srt
    python scripts/subtitles.py srt --out out/subtitles/zh.srt
    python scripts/subtitles.py ass --fontsize 76          # 覆盖字号
    python scripts/subtitles.py burn --preview 8           # 8 秒快速验证
    python scripts/subtitles.py burn out/final-1080p.mp4 -o out/final-1080p-hard.mp4
    python scripts/subtitles.py burn --soft                # 软字幕封装（播放器可关）

依赖
----
必需：ffmpeg / ffprobe（硬依赖，``doctor.py`` 会体检）、``scripts/_common.py``、``src/timing.json``。
不需要任何 pip 包：本脚本只用标准库 + 已装好的 ffmpeg。
配音缺失（``report.json`` 或某条 mp3 不见了）**不会崩**：回退到按字数估算并打 WARN。

退出码
------
0 成功（含「按字数估算」这类 WARN）；
1 有 ERROR：缺 ffmpeg、timing.json 非法、没写旁白稿、字幕写不出去、烧录失败、``--preview`` 用错等。
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import math
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unicodedata
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union

# ---------------------------------------------------------------------------
# 导入公共库 scripts/_common.py（由另一位维护者提供，本脚本不修改它）
# ---------------------------------------------------------------------------
sys.path.insert(0, str(Path(__file__).resolve().parent))
try:  # pragma: no cover - 只在 _common.py 缺失时走到
    from _common import (  # type: ignore
        FONTS_PATH,
        PROJECT_ROOT,
        TIMING_PATH,
        error,
        fmt_sec,
        fonts_config,
        has_cmd,
        human,
        load_json,
        load_timing,
        line_window,
        log,
        scene_window,
        warn,
    )
except ImportError as exc:  # pragma: no cover
    sys.stderr.write(
        "ERROR: 无法导入 scripts/_common.py（%s）。请确认 scripts 目录完整，"
        "并在项目根目录下运行本脚本。\n" % (exc,)
    )
    raise SystemExit(1)


# ===========================================================================
# 阈值常量（每条都对应 AGENTS.md 的一处硬性要求，改这里前先改文档）
# ===========================================================================

# AGENTS.md 阶段 7：「停留时长 ≥ 字数 / 6 + 0.35 秒」。分母 6 = 语速 5.5~6.5 字/秒的乐观端。
CHARS_PER_SEC = 6.0
#: 上式的固定尾巴（秒）。0.35 是给标点停顿留的余量。
MIN_HOLD_PAD_S = 0.35
#: 收尾提前量（秒）。与 AGENTS.md 阶段 7 模板组件的「场景结束前 0.15 秒收掉」一致，
#: 避免上一句字幕和下一场景的字幕同时在屏上。
TAIL_OFF_S = 0.15

# 安全区（AGENTS.md 第 4 节，相对画面高宽）。
#: 底部安全区下沿——字幕的 MarginV 必须落在这条线以上。
SAFE_BOTTOM = 0.18
#: 顶部安全区下沿——MarginV 太大（字幕顶到画面上三分之三）同样算破版。
SAFE_TOP = 0.10
#: 左右安全区。ASS 的 MarginL/MarginR 要留够，否则长行会顶到平台互动按钮。
SAFE_LEFT = 0.06
SAFE_RIGHT = 0.14

# ASS 字号 = min(画布高度 × 这个系数, 左右安全区可用宽度 / captions.maxChars)。
# 只按高度算会在 13 个全角字时出界：1080×1920 上字号 73，13 × 73 = 949px，
# 而 MarginL 6% + MarginR 14% 之后只剩 864px（实测记录见 references/配音与字幕.md §6）。
# 所以 _build_ass 还会按宽度公式再压一次，两个上限取小——画面字幕与硬字幕同一口径。
FONT_SCALE = 0.038
FONT_SIZE_MIN = 12

# ASS 描边/阴影（竖屏手机上看，描边是「任何底色都读得清」的保险）。
DEFAULT_OUTLINE = 3.0
DEFAULT_SHADOW = 1.2

#: 允许「为避免行首标点」而超出 maxChars 的宽容度。实测到过 13 字的句子切出
#: 「已经有六万人在用它管理开支 / 。」—— 一个孤零零的句号占一行，比多出 1 个字难看得多。
#: 超过这个宽容度就宁可让标点留在行首（并打 WARN），也不再撑宽行。
FOLD_OVERFLOW_GRACE = 2.0

# 中文避头尾（禁则处理）：
#: 收尾标点不能出现在行首，否则会切出「……管理开支 / 。」这种断法；
CLOSING_PUNCT = "。，、！？；：…—,.!?;:）〕」』》〉】”’%"
#: 起始标点不能出现在行尾（左括号/左引号后面必须还有字）。
OPENING_PUNCT = "（〔「『《〈【“‘("
# 归一化空白时统一替换成的字符（字幕里换行由 \N 控制，正文不能自带换行）。
SPACE_CHARS = " \t\u3000\u00a0"

VO_DIRNAME = "audio-src/vo"
REPORT_NAME = "report.json"
SUBTITLE_DIRNAME = "out/subtitles"
DEFAULT_SRT = SUBTITLE_DIRNAME + "/zh.srt"
DEFAULT_ASS = SUBTITLE_DIRNAME + "/zh.ass"
DEFAULT_VIDEO = "out/final-1080p.mp4"
DEFAULT_OUT_VIDEO = "out/final-1080p-hard.mp4"
VERIFY_DIRNAME = "out/verify-subs"

#: 烧录时抽帧存 PNG 的时刻（相对预览区间的比例）——避开 0.0s 的淡入黑帧。
FRAME_AT_RATIO = 0.5
#: 默认 ASS 字体（Windows 简体中文环境的通用选择；fonts.json 的 roles.body 优先）。
DEFAULT_FONT_FAMILY = "Microsoft YaHei"

FFMPEG_TIMEOUT_S = 1800.0
FFPROBE_TIMEOUT_S = 120.0


# ===========================================================================
# 小工具
# ===========================================================================

def _force_utf8() -> None:
    """Windows 控制台默认 GBK，中文/emoji 会炸；这里兜底成 UTF-8。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except Exception:
            pass


def _rel(path: Any) -> str:
    """相对项目根的展示路径（不在项目内则原样返回）。"""
    p = Path(path)
    try:
        return p.resolve().relative_to(PROJECT_ROOT.resolve()).as_posix()
    except Exception:
        return p.as_posix()


def _in_project(path: Any) -> bool:
    """路径是否落在项目根里（``_rel()`` 的反面判据，用来把「项目外的文件」点名说清）。"""
    try:
        Path(path).resolve().relative_to(PROJECT_ROOT.resolve())
        return True
    except Exception:
        return False


def _resolve_user_path(value: Any, allow_cwd_fallback: bool = True) -> Path:
    """把用户给的路径变成绝对路径。

    **相对路径优先按项目根解析**：本项目所有命令的路径口径都相对项目根
    （``out/subtitles/zh.ass``、``out/final-1080p.mp4`` 指的就是工程里那一份）。
    以前反过来（当前工作目录优先），于是站在别处跑、或那个目录下碰巧有一份同名
    ``out/subtitles/zh.ass``，就会把**项目外**的文件当成项目自己的字幕用
    （``burn`` 连内容都不看就烧进成片），而项目里那份被完全无视、一条提示都没有。

    只有项目根下确实没有这个路径时，才退回当前工作目录，并且必须打一条 WARN 说清
    「这不是项目里的文件」——往项目外写的东西不会同步进工程，不能静默发生。
    """
    p = Path(str(value)).expanduser()
    if p.is_absolute():
        return p
    project_path = (PROJECT_ROOT / p).resolve()
    if project_path.exists():
        return project_path
    if allow_cwd_fallback and p.exists():
        warn(
            f"{p.as_posix()} 不在项目里（{PROJECT_ROOT} 下没有这个路径），"
            f"本次用的是当前工作目录下的 {p.resolve()}；"
            "往项目外写文件不会进工程，请确认这就是你要的那份"
        )
        return p.resolve()
    return project_path


def _disp_width(text: str) -> int:
    """终端显示宽度（中日韩全角算 2 列），只用于打印对齐。"""
    width = 0
    for ch in text:
        width += 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
    return width


def _pad(text: str, width: int) -> str:
    return text + " " * max(0, width - _disp_width(text))


def _num(value: Any, fallback: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return fallback


def _int(value: Any, fallback: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback


# --- 时间戳 ---------------------------------------------------------------

def _split_ms(seconds: float) -> Tuple[int, int, int, int]:
    """把秒拆成 (时, 分, 秒, 毫秒)；四舍五入到毫秒，负数夹到 0。"""
    total_ms = int(round(max(0.0, float(seconds)) * 1000.0))
    hours, rest = divmod(total_ms, 3600_000)
    minutes, rest = divmod(rest, 60_000)
    secs, millis = divmod(rest, 1000)
    return hours, minutes, secs, millis


def fmt_srt_time(seconds: float) -> str:
    """SRT 时间戳：``HH:MM:SS,mmm``（逗号是 SRT 规范，写成点号部分播放器不认）。"""
    h, m, s, ms = _split_ms(seconds)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def fmt_ass_time(seconds: float) -> str:
    """ASS 时间戳：``H:MM:SS.cc``（ASS 只到厘秒，四舍五入到最近厘秒）。

    以前用截断（``int(seconds * 100)``），``fmt_ass_time(1.15)`` 会写成 0:00:01.14，
    比 SRT 的毫秒四舍五入早最多 10ms；现在两种格式统一取整，逐帧比对不会再差一个厘秒。
    """
    total_cs = int(round(max(0.0, float(seconds)) * 100.0))
    hours, rest = divmod(total_cs, 360_000)
    minutes, rest = divmod(rest, 6_000)
    secs, centis = divmod(rest, 100)
    return f"{hours:d}:{minutes:02d}:{secs:02d}.{centis:02d}"


# --- 文本与折行 -----------------------------------------------------------

#: C0/C1 控制字符（保留 \t\n\r：它们交给下面的空白正则会变成空格）。
#: **必须剥掉**：SRT 正文只要含一个 NUL(0x00)/BEL(0x07)，ffmpeg 的 srt demuxer 就会把
#: 整份字幕文件读成 0 条（实测 `-f srt` 往返输出 "Output file is empty"），而脚本
#: 自己 rc=0 看不出问题（R6-subs-01）。ASS 的 Dialogue 行同样会被污染。
CONTROL_CHARS_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")


def _clean_text(text: Any) -> str:
    """去掉换行与多余空白（换行由折行逻辑统一生成），并剥掉 C0/C1 控制字符。"""
    raw = CONTROL_CHARS_RE.sub("", str(text or ""))
    for ch in SPACE_CHARS:
        raw = raw.replace(ch, " ")
    return re.sub(r"\s+", " ", raw).strip()


#: ASS 里「反斜杠 + 字符」的转义表：``\N``/``\n`` 换行、``\h`` 不换行空格、``\{``/``\}`` 字面花括号。
#: 正文里的反斜杠如果紧挨 N/n/h，会被 libass 当成转义指令（``C:\new`` 直接被拆成两行）。
ASS_ZWSP = "\u200b"


def _escape_ass_text(text: str) -> str:
    r"""把字幕正文转成 ASS 的「显示出来与原文逐字一致」形式（R5-subs-02）。

    ffmpeg 7.1 + libass 实测（1080×1920，Microsoft YaHei 66，像素 bbox 对比 + 目检）：

    * 花括号是 override 块定界符：正文 ``{over} ABCD`` 渲染成 ``  ABCD``（整块被吞）。
      libass 认 ``\{`` / ``\}`` 是字面花括号，所以花括号统一转义。
    * 正文里只有 ``\N`` ``\n`` ``\h`` ``\{`` ``\}`` 是转义，其余反斜杠按字面渲染；
      所以问题只出在「反斜杠紧跟 N/n/h/{/}」——``C:\new`` 会变成换行。
      在反斜杠后面插一个 U+200B 零宽空格打断它：ZWSP 不进画面（像素实测与不插的
      ``A\ND`` 完全一致），渲染出来的字与原文相同。
    * 结构性换行由调用方用 ``\N`` join，不走本函数（本函数只处理单行正文）。
    """
    out: List[str] = []
    for ch in text:
        if ch == "\\":
            out.append(ch + ASS_ZWSP)
        elif ch == "{":
            out.append("\\{")
        elif ch == "}":
            out.append("\\}")
        else:
            out.append(ch)
    return "".join(out)



def _vis_len(text: str) -> int:
    """字幕的「字数」：东亚洲宽字符算 1 个字，半角算 0.5，凑整进位。

    ``captions.maxChars`` 按 AGENTS.md 阶段 7 的「单行 ≤13 字」理解成**中文正文字数**，
    所以这里不能直接用 ``len()``（那会把 1 个汉字和 1 个标点、1 个字母算成一样宽）。
    """
    total = 0.0
    for ch in text:
        total += 1.0 if unicodedata.east_asian_width(ch) in ("W", "F") else 0.5
    return int(round(total))


def _char_w(ch: str) -> float:
    """单字宽度：东亚洲宽字符算 1 个字，半角算 0.5。"""
    return 1.0 if unicodedata.east_asian_width(ch) in ("W", "F") else 0.5


def _valid_break(tokens: Sequence[str], cut: int) -> bool:
    """cut 这个切点合法吗：两边都不是标点。

    非法切点只有两种，都是实测踩到的坑：

    * 下一行以收尾标点开头 —— ``已经有六万人在用它管理开支。`` 按 13 字硬切会变成
      「……管理开支 / 。」，一个孤零零的句号独占一行；
    * 本行以起始标点结尾 —— ``点击「开始记账」按钮（在右下角）。`` 会切出「点击 / 「」，
      左引号被甩在行尾。

    ``tokens`` 既可以是逐字的字符列表，也可以是 ``_split_atoms()`` 的折行原子列表；
    多字的拉丁词 / URL 原子不会命中标点判断，所以切点不会落进词内部（V35）。
    """
    if cut <= 0 or cut >= len(tokens):
        return False
    if tokens[cut] in CLOSING_PUNCT or tokens[cut - 1] in OPENING_PUNCT:
        return False
    return True


#: 拉丁词 / URL / 数字：字母数字开头结尾，内部允许 URI 里常见的连接符。
#: 折行时整段不可拆——否则 ``xiaozhangben.app`` 会被切成两行各半个域名（V35）。
#: ``scheme://`` 前缀也算进原子：否则 ``https://example.com/x`` 会从 ``https://``
#: 后面被静默拆行（R5-subs-03；V35 的 URL 原子修复没覆盖 scheme）。
LATIN_TOKEN_RE = re.compile(
    r"(?:[A-Za-z][A-Za-z0-9+.\-]*://)?"
    r"[A-Za-z0-9]+(?:[._\-/:@#?=&%][A-Za-z0-9]+)*"
)

#: 平衡折行时在平衡点附近往回找标点边界的搜索半径（字）。
BALANCE_LOOKBACK = 2


def _split_atoms(text: str) -> List[str]:
    """把文本拆成折行原子：CJK / 标点 / 空格逐字，拉丁词与 URL 整段一个原子。"""
    atoms: List[str] = []
    i = 0
    while i < len(text):
        match = LATIN_TOKEN_RE.match(text, i)
        if match and match.end() > i:
            atoms.append(match.group(0))
            i = match.end()
        else:
            atoms.append(text[i])
            i += 1
    return atoms


def _atom_w(atom: str) -> float:
    """原子的字数宽度（东亚洲宽 1、半角 0.5，与 ``_char_w`` 同一口径）。"""
    return sum(_char_w(ch) for ch in atom)


def _atoms_chars(atoms: Sequence[str], upto: int) -> int:
    """前 ``upto`` 个原子一共占多少个字符。"""
    return sum(len(atom) for atom in atoms[:upto])


def _char_cut_atom(atoms: Sequence[str], char_cut: int) -> int:
    """字符下标 -> 原子下标；char_cut 不在原子边界上时返回 -1。"""
    acc = 0
    for index, atom in enumerate(atoms):
        if acc == char_cut:
            return index
        acc += len(atom)
    return len(atoms) if acc == char_cut else -1


def _wrap(text: str, width: float, warn_hard: bool = True) -> Tuple[int, float]:
    """从 text 头部取一行，返回 ``(切点字符数, 超宽字数)``。

    折行单位是**原子**：CJK 逐字、拉丁词 / URL 整段不可拆（V35）。先贪心装到
    ``width`` 拿到一个**上界**，再在它之前找**最后一个合法切点**；找不到就容许放行
    1~2 个收尾标点（``FOLD_OVERFLOW_GRACE``），宁可这行多装一个字，也不要下一行以
    标点开头。唯一允许硬切的情况是「单个拉丁词本身比整行还宽」，这时打 WARN 建议
    改短；**绝不丢字**：切点永远 ≥1 且 < ``len(text)``。
    """
    atoms = _split_atoms(text)
    consumed = 0.0
    fit = 0
    while fit < len(atoms):
        atom_w = _atom_w(atoms[fit])
        if consumed + atom_w > width:
            break
        consumed += atom_w
        fit += 1

    if fit <= 0:
        # 第一个原子就比整行还宽：只能在这一个词内部硬切，并提醒改稿（V35）。
        hard = 0
        acc = 0.0
        while hard < len(atoms[0]) and acc + _char_w(atoms[0][hard]) <= width:
            acc += _char_w(atoms[0][hard])
            hard += 1
        if warn_hard:
            warn(
                f"「{atoms[0]}」这段比一行（{width:g} 字）还宽，只能从中间硬切；"
                "建议把 URL / 英文改短，或调大 timing.json 的 captions.maxChars"
            )
        return max(1, min(hard, len(text) - 1)), 0.0

    # 全部原子都装得下时（平衡分支的 mid 可能给得比整行还宽），退一个保证有剩余。
    fit = min(fit, len(atoms) - 1)

    # 主选：上界以内的最后一个合法切点。
    cut = fit
    while cut > 0 and not _valid_break(atoms, cut):
        cut -= 1
    if cut > 0:
        return _atoms_chars(atoms, cut), 0.0

    # 退让：上界之后最近的合法切点，只要多装的都是收尾标点、且不超宽容度。
    over = fit
    extra = 0.0
    while over < len(atoms) - 1 and extra < FOLD_OVERFLOW_GRACE:
        if atoms[over] not in CLOSING_PUNCT:
            break
        extra += _char_w(atoms[over])
        over += 1
        if _valid_break(atoms, over):
            return _atoms_chars(atoms, over), extra
    return _atoms_chars(atoms, fit), 0.0


#: 第二行以「单个字 + 收尾标点」开头 = 中文词被拆开的指纹（V39），例如「小 / 票，」。
LONELY_HEAD_RE = re.compile(r"^[^\s，。、；：！？,.;:!?…—]\s*[，。、；：！？,.;:!?…—]")


def _lonely_head(tail: str) -> bool:
    """第二行是不是以「单个字 + 收尾标点」开头？"""
    return bool(LONELY_HEAD_RE.match(tail))


def _balance_cut(rest: str, center: int, width: float) -> int:
    """在中心切点附近的原子边界里挑「两行最均衡」的合法切点（V39）。

    均衡优先（两行字数差最小），标点边界只作为同分时的加分项——这样「小票」这类
    中文词不会为了凑标点被拆开，下一个「单字 + 标点」规则再兜底。
    返回 0 表示 ``center`` 附近没有合法切点。
    """
    atoms = _split_atoms(rest)
    boundaries = [0]
    acc = 0
    for atom in atoms:
        acc += len(atom)
        boundaries.append(acc)
    index = _char_cut_atom(atoms, center)
    if index < 0:
        return 0
    best_score = (10 ** 9, 10 ** 9)  # (两行字数差, 非标点边界的罚分)
    best_cut = 0
    for offset in range(-BALANCE_LOOKBACK, BALANCE_LOOKBACK + 1):
        atom_cut = index + offset
        if atom_cut <= 0 or atom_cut >= len(atoms) or not _valid_break(atoms, atom_cut):
            continue
        cand = boundaries[atom_cut]
        head = _vis_len(rest[:cand])
        tail = _vis_len(rest[cand:])
        if head > width or tail > width:
            continue
        if rest[cand - 1] in CLOSING_PUNCT or rest[cand - 1] == " ":
            score = (abs(head - tail), 0)
        else:
            score = (abs(head - tail), 1)
        if score < best_score:
            best_score = score
            best_cut = cand
    return best_cut


def _fold_text(text: str, max_chars: int, max_lines: int) -> List[str]:
    """按字数折行：优先在标点后断，其次整词下移（V35/V39）。

    * 折行单位是原子：CJK 逐字、拉丁词 / URL 整段不可拆，装不下就整词下移；
      只有单个词本身比整行宽才硬切并 WARN；
    * 平衡分支在平衡点 ±2 字内优先找标点边界，且第二行不能以「单字 + 标点」开头；
    * 本函数**不丢字**，返回行数可能超过 ``max_lines``——超行由
      ``_check_fold_lines()`` 直接 error（AGENTS.md 阶段 7「最多 2 行」是硬要求，V42）。
    """
    cleaned = _clean_text(text)
    if not cleaned:
        return []
    width = max(1.0, float(max_chars))
    lines: List[str] = []
    rest = cleaned
    while rest:
        if _vis_len(rest) <= width:
            lines.append(rest.strip(" "))
            break
        cut, overflow = _wrap(rest, width)
        cut = max(1, min(cut, len(rest) - 1))
        tail = rest[cut:]
        # 平衡：首行偏长、剩下的字一行还装得下时，把切点挪到中间——
        # 两行差不多长比「满行 + 短尾巴」好看得多（这也是「尊重标点」的另一半）。
        # 判据只用「尾巴一行装得下」：原来的 len(lines) + 1 == max_lines 只有在
        # 「这一刀切完正好是最后一行」时才成立，maxLines=2 时那一刀落在第二行上，
        # 于是平衡分支永远进不来（13 字 + 3 字尾巴照样输出）。
        # 平衡点按**可见字数**算一半，不按字符下标：含拉丁词时字符数会骗人
        #（``xiaozhangben.app`` 16 个字符只占 8 个字宽），V39。
        if tail and _vis_len(tail) <= width and cut > len(rest) / 2:
            atoms = _split_atoms(rest)
            half = sum(_atom_w(atom) for atom in atoms) / 2.0
            acc = 0.0
            center = 0
            for atom in atoms:
                if acc >= half:
                    break
                acc += _atom_w(atom)
                center += len(atom)
            mid_cut = _balance_cut(rest, center, width)
            # 平衡后的两行仍然都要 ≤ max_chars（避头尾会把切点撑宽 1~2 个字），
            # 撑超了就保持原来的切点：宁可「满行 + 短尾巴」也不能破单行字数上限。
            if (
                0 < mid_cut < len(rest)
                and _vis_len(rest[:mid_cut]) <= width
                and _vis_len(rest[mid_cut:]) <= width
            ):
                cut, overflow, tail = mid_cut, 0.0, rest[mid_cut:]
        # 第二行以「单字 + 收尾标点」开头时切点左移 1 字（V39）。
        if _lonely_head(tail) and cut > 1 and _vis_len(rest[cut - 1:]) <= width:
            atoms = _split_atoms(rest)
            atom_cut = _char_cut_atom(atoms, cut - 1)
            if atom_cut > 0 and _valid_break(atoms, atom_cut):
                cut -= 1
                tail = rest[cut:]
        # 断行处的空格是排版垃圾：SRT/ASS 里行首行尾的空格肉眼看不见，删掉更干净。
        lines.append(rest[:cut].strip(" "))
        rest = tail
        if overflow:
            # 只为避行首标点才撑宽，且最多 2 个字；说清楚免得看起来像折行失败。
            warn(
                f"「{cleaned}」首行多含了 {overflow:g} 个标点（>{max_chars} 字）以避免把标点切到行首："
                "这是有意的避头尾处理，不是折行失败"
            )
    return lines


def _check_fold_lines(line_id: str, text: str, lines: List[str], max_chars: int, max_lines: int) -> None:
    """字幕超过 ``captions.maxLines`` 就硬失败（V42）。

    AGENTS.md 阶段 7 把「最多 2 行」定成硬要求，但原来只 WARN 并原样写进 SRT/ASS；
    现在 srt / ass / burn 三条路径都走 ``build_cues()``，在这里统一拦下，让作者改稿。
    """
    if max_lines > 0 and len(lines) > max_lines:
        error(
            f"第 {line_id} 句「{_clean_text(text)}」折成 {len(lines)} 行，"
            f"超过 captions.maxLines={max_lines}（每行 ≤{max_chars} 字）。"
            "字幕最多 2 行是 AGENTS.md 阶段 7 的硬要求：请回 timing.json 改稿删字/拆句，"
            "或调大 captions.maxChars / maxLines"
        )


# --- 字体 -----------------------------------------------------------------

#: 通用族名，不算「具体字体」，解析时跳过（交给 fontconfig 决定）。
GENERIC_FAMILIES = {"sans-serif", "serif", "monospace", "system-ui", "cursive", "fantasy"}

#: 「ASS 里该写的字体名」 -> 「本机用来判断它存不存在的字体文件」。
#: 这是**本机事实**的映射表，不是猜测：``_family_available()`` 会就地检查文件是否存在。
_FAMILY_FONTS = {
    "microsoft yahei": (
        "C:/Windows/Fonts/msyh.ttc", "C:/Windows/Fonts/msyhbd.ttc", "C:/Windows/Fonts/msyhl.ttc",
    ),
    "微软雅黑": ("C:/Windows/Fonts/msyh.ttc",),
    "simhei": ("C:/Windows/Fonts/simhei.ttf",),
    "黑体": ("C:/Windows/Fonts/simhei.ttf",),
    "simsun": ("C:/Windows/Fonts/simsun.ttc",),
    "dengxian": ("C:/Windows/Fonts/Deng.ttf",),
    "pingfang sc": ("/System/Library/Fonts/PingFang.ttc", "/System/Library/Fonts/PingFang.ttf"),
    "heiti sc": ("/System/Library/Fonts/STHeiti Medium.ttc",),
    "noto sans cjk sc": (
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "C:/Windows/Fonts/NotoSansSC-VF.ttf",
    ),
    "noto sans sc": ("C:/Windows/Fonts/NotoSansSC-VF.ttf",),
    "source han sans sc": (
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/adobe-source-han-sans/SourceHanSansSC-Regular.otf",
    ),
    "wenquanyi micro hei": ("/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",),
}

# 注（2026-10-12）：这里原本有一个 `_PROBE_CHAR = "龙"` 常量，用来「用一个只有中文字体
# 才有的汉字」探某个字体能不能打中文。它已被 `_family_missing(font_file, text)` 取代：
# 后者逐个查**本片字幕真正要用的**每个字在字体 cmap 里有没有码位（用常见字「一」探会漏，
# 更别说单一探针字根本代表不了整段文案）。不再保留那个常量，免得后来者以为它还在用。

#: 系统字体索引缓存（族名 -> 字体文件），首次用到时才构建。
_FONT_INDEX: Optional[Dict[str, Path]] = None

#: 字体 cmap 缓存（字体路径 -> 码位集合，读不了记 None）。缺字判据用它，别用 getmask()。
_CMAP_CACHE: Dict[str, Optional[set]] = {}

#: 建索引时调 fc-list 的超时（本机 4426 条，通常 1 秒内返回）。
FC_LIST_TIMEOUT_S = 30.0


def _norm_family(name: str) -> str:
    """把族名归一化成可比较的 key：小写、去掉空格与连字符。"""
    return re.sub(r"[\s\-_]+", "", str(name or "").lower())


def _system_font_index() -> Dict[str, Path]:
    """族名（归一化）-> 字体文件，来自 fontconfig 自己，所以必须按原样比对。

    不能拿文件名去猜族名：``msyh.ttc`` 是 ``Microsoft YaHei``，``simhei.ttf`` 是
    ``SimHei``，没有任何可靠规律。唯一权威的来源是 ``fc-list``（本机 4426 条），
    加上 ``fonts.json`` 的 ``systemCandidates``（``fc-list`` 不在时它还是有内容的那一份）。
    """
    global _FONT_INDEX
    if _FONT_INDEX is None:
        index: Dict[str, Path] = {}
        if has_cmd("fc-list"):
            try:
                proc = subprocess.run(
                    ["fc-list", "-f", "%{family}|%{file}\n"],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    timeout=FC_LIST_TIMEOUT_S,
                )
                for line in proc.stdout.decode("utf-8", errors="replace").splitlines():
                    if "|" not in line:
                        continue
                    family_text, file_text = line.rsplit("|", 1)
                    path = _font_path_file(Path(file_text.strip()))
                    if path is None:
                        continue
                    for family in family_text.split(","):
                        key = _norm_family(family)
                        if key:
                            index.setdefault(key, path)
            except (OSError, subprocess.TimeoutExpired):
                pass
        # 映射表兜底：fc-list 不在（或它没收录某个字体）时，_FAMILY_FONTS 里的路径仍然算数。
        for family, paths in _FAMILY_FONTS.items():
            for item in paths:
                path = Path(item)
                try:
                    if path.is_file():
                        index.setdefault(_norm_family(family), path)
                        break
                except OSError:
                    continue
        _FONT_INDEX = index
    return _FONT_INDEX


def _family_available(family: str) -> Optional[Path]:
    """这个族名在本机有对应的字体文件吗？返回那个文件，没有返回 None。"""
    return _system_font_index().get(_norm_family(family))


def _font_path_file(font_path: Path) -> Optional[Path]:
    """把 fontconfig 报出来的路径（ttc 里可能是 ``file.ttc:face``）还原成真实文件。"""
    text = str(font_path).strip()
    if not text:
        return None
    for candidate in (text, text.split(":")[0], text.split("(")[0].strip()):
        try:
            path = Path(candidate)
            if path.is_file():
                return path
        except (OSError, ValueError):
            continue
    return None


def _font_cmap(font_file: Path) -> Optional[set]:
    """用 fontTools 读字体 cmap 里的码位集合；读不了（没装 fontTools / 字体损坏）返回 None。"""
    key = str(font_file)
    if key in _CMAP_CACHE:
        return _CMAP_CACHE[key]
    codes: Optional[set] = None
    try:
        from fontTools.ttLib import TTFont  # 模板依赖里已有 fontTools

        with TTFont(key, lazy=True) as tt:
            codes = set(tt.getBestCmap().keys())
    except Exception:  # noqa: BLE001 - 字体坏掉不该中断烧录，降级到下面的 getmask
        codes = None
    _CMAP_CACHE[key] = codes
    return codes


def _family_missing(font_file: Path, text: str) -> Optional[str]:
    """字体缺的第一个字；全都有返回 None。

    **必须查 cmap，不能用 `font.getmask(ch).getbbox() is None`**（本函数的老实现）：
    Pillow 会自己做字体回退，字体 cmap 里根本没有那个字时 getmask 照样从系统字体里
    借一个字形，bbox 永远非空 —— 判据因此恒为「有字」。实测本仓库的
    `public/fonts/promo-subset-*.ttf`（cmap 342 个码位）对「马上安卓候鑫」这 6 个
    不在子集里的字，老判据真阳性 0 / 假阴性 6，于是「缺字就换字体」的兜底成了死代码，
    字幕整行变方框却退 0（`deliver.py` 的 `_font_supports` 早就改用 cmap 了，这里对齐它）。
    没有 fontTools 时才退回 getmask —— 不可靠，只求聊胜于无。
    """
    if not text:
        return None
    codes = _font_cmap(font_file)
    for ch in dict.fromkeys(text):
        if ch.isspace():
            continue
        if codes is not None:
            if ord(ch) not in codes:
                return ch
            continue
        try:  # 降级路径
            from PIL import ImageFont  # type: ignore

            face = ImageFont.truetype(str(font_file), 32)
            if face.getmask(ch).getbbox() is None:
                return ch
        except Exception:  # noqa: BLE001 - 量不了就当它没问题
            continue
    return None


def _first_generic(cfg: Dict[str, Any]) -> str:
    """stack 尾部的通用族名（``sans-serif`` 之类），兜底用。"""
    roles = cfg.get("roles") if isinstance(cfg.get("roles"), dict) else {}
    body = roles.get("body") if isinstance(roles.get("body"), dict) else {}
    stack = body.get("stack")
    if isinstance(stack, list):
        for item in reversed(stack):
            name = str(item or "").strip()
            if name.lower() in GENERIC_FAMILIES:
                return name
    return "sans-serif"


def _body_font_stack(cfg: Dict[str, Any]) -> List[str]:
    roles = cfg.get("roles") if isinstance(cfg.get("roles"), dict) else {}
    body = roles.get("body") if isinstance(roles.get("body"), dict) else {}
    stack = body.get("stack")
    if isinstance(stack, list):
        return [str(x).strip() for x in stack if str(x or "").strip()]
    return [DEFAULT_FONT_FAMILY, "sans-serif"]


def _resolve_font_family(cfg: Dict[str, Any], text: str = "") -> str:
    """从 ``roles.body.stack`` 里挑一个**本机真的能用**的字体名。

    这里是本脚本最容易静默出错的地方，所以宁可多做一步：ASS 的 Fontname 是交给
    libass/fontconfig 解析的**名字**，写了一个本机没有的名字（例如在 Windows 上写
    ``fonts.json`` stack 首位的 ``PingFang SC``）libass **不会报错**，它只是静默换一个
    字体——实测本机 ``fc-match "PingFang SC"`` 得到 ``Cascadia Code``，一个不含汉字的
    拉丁字体，字幕会整行变成豆腐块方框，而烧录命令退出码是 0。

    选择顺序：

    1. 用映射表 / ``systemCandidates`` 找到本机有字体文件的那个族名；
    2. 都没有就问 fontconfig（``fc-match``，macOS/Linux 上能尊重 fontconfig 别名配置）
       把名字解析成真实字体；**必须确认返回的族名就是请求的那个**（或以完整词为前缀的
       别名，如 ``Noto Sans CJK SC`` -> ``Noto Sans SC``），再用 Pillow 确认它真的含汉字。
       fontconfig 从不失败，不做这一步就会把 ``PingFang SC`` 的「替代字体」
       ``Cascadia Code`` 当成答案写进 ASS；
    3. 还没有就退回 stack 里第一个具体名字，并 WARN 说明没法在本机核实。
    """
    stack = _body_font_stack(cfg)
    concrete = [n for n in stack if n.lower() not in GENERIC_FAMILIES]

    # 本机有这个族名的字体文件，**还要**确认它真的含本片字幕用到的字。
    # 只查「文件存在」是不够的：stack 里第二位可能是个能解析、但不含汉字的字体
    # （实测：把 Cascadia Code 放在 PingFang SC 后面时，早期版本会直接返回它，
    #  字幕整行变豆腐块，而烧录退出码是 0）。缺字就继续往后找，不在这里静默定案。
    first_available: Optional[Tuple[str, Path, str]] = None
    for name in concrete:
        found_file = _family_available(name)
        if found_file is None:
            continue
        missing = _family_missing(found_file, text) if text else None
        if missing is None:
            return name
        if first_available is None:
            first_available = (name, found_file, missing)
        warn(
            f"字体栈里的「{name}」（{found_file.name}）本机有，但它缺「{missing}」这类字，"
            "用它字幕会变成方框；继续往后找"
        )

    for name in concrete:
        found = _fc_match(name)
        if not found:
            continue
        family_text, font_file = found
        families = [f.strip() for f in family_text.split(",") if f.strip()]
        if not _family_matches_request(name, families):
            # fontconfig 回落到一个不相干的字体（实测：PingFang SC -> Cascadia Code），
            # 这不是「替代字体」，是「没找到」。用它会把汉字渲成方框。
            continue
        missing = _family_missing(font_file, text) if text else None
        if missing is not None:
            warn(
                f"fontconfig 把「{name}」解析到「{families[0]}」（{font_file.name}），"
                f"但那个字体缺「{missing}」这类字，字幕会变成方框；继续往后找"
            )
            continue
        main = families[0]
        warn(
            f"「{name}」在本机没有对应字体文件，改用 fontconfig 解析出的「{main}」"
            f"（{font_file.name}）；它已确认含有本片字幕用到的汉字"
        )
        return main

    if first_available is not None:
        # 走到这里说明整条栈里没有一个「文件存在且字全」的字体。手上这个至少是
        # 真实的字体文件（比 fontconfig 随便给的替代字体强），用它并明确报警。
        name, found_file, missing = first_available
        warn(
            f"字体栈里没有一条能覆盖本片字幕全部用字：退回首选可用字体「{name}」"
            f"（{found_file.name}），它缺「{missing}」这类字，字幕会出现方框。"
            "换一个含这些字的字体（改 fonts.json 的 roles.body.stack）或改文案；"
            "烧完**必须**打开 out/verify-subs/ 下的审图帧确认汉字正常显示"
        )
        return name

    chosen = concrete[0] if concrete else DEFAULT_FONT_FAMILY
    warn(
        f"fonts.json 的字体栈（{'、'.join(stack) or '空'}）在本机一个都核实不到，"
        f"仍按首选「{chosen}」写入 ASS：libass 可能静默换成别的字体甚至渲成方框。"
        "烧完**必须**打开 out/verify-subs/ 下的审图帧确认汉字正常显示"
    )
    return chosen


def _family_matches_request(name: str, families: Sequence[str]) -> bool:
    """fontconfig 返回的族名里，有没有「就是请求的那个」或它的完整词前缀别名。

    实测 ``fc-match "Noto Sans CJK SC"`` 在装有 Noto Sans SC 的机器上会返回
    ``Noto Sans SC``——这是同一个字体的不同命名，算命中；而返回 ``Cascadia Code``
    就是「没找到，随便给了一个」，不算。这里用**完整词**前缀判断（``Noto`` 单独一个词
    不算），避免 ``sans`` 之类短词误命中。
    """
    req = _norm_family(name)
    for family in families:
        got = _norm_family(family)
        if not got:
            continue
        if got == req or got.startswith(req) or req.startswith(got):
            return True
    return False


def _fc_match(name: str) -> Optional[Tuple[str, Path]]:
    """问 fontconfig 这个族名实际会匹配到哪个字体，返回 ``(族名原文, 字体文件)``。

    关键：fontconfig **从不失败**。本机没有 ``PingFang SC`` 时它会返回
    ``Cascadia Code``（一个拉丁字体），而不是报错——调用方必须自己用族名比对
    （见 :func:`_resolve_font_family`）判断「这到底是不是我要的字体」。
    Windows 上通常没有 ``fc-match`` 这个命令（ffmpeg 内嵌 fontconfig，不提供 CLI），
    这时返回 None，交给映射表负责。
    """
    try:
        proc = subprocess.run(
            ["fc-match", "-f", "%{family}|%{file}", name],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10,
        )
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    out = proc.stdout.decode("utf-8", errors="replace").strip()
    if "|" not in out:
        return None
    family_text, file_text = out.split("|", 1)
    font_file = _font_path_file(Path(file_text.strip()))
    if not family_text.strip() or font_file is None:
        return None
    return family_text.strip(), font_file


#: 字体「行高 / em」比例缓存（族名 -> (ascender-descender)/unitsPerEm；量不出记 None）。
#:
#: ASS 的 Fontsize 语义**不是 em 高**，而是「ascender−descender 的像素高」：libass 用
#: 字体自己的 hhea 行高做归一，同一个 Fontsize 在不同字体上实际字面大小不同。实测
#: （ffmpeg 7.1 + libass，1080×1920，Fontsize 66，13 个全角字「一二三四五六七八九十一二三」）：
#:
#:   - ``msyh.ttc``（asc 2167 / desc -536 / upem 2048 → 比值 1.320）：实宽 648px，实际 em≈50.0px
#:   - ``simhei.ttf`` / ``simsun.ttc``（220 / -36 / 256 → 1.000）：实宽 850 / 849px，实际 em≈66px
#:   - ``Deng.ttf``（1659 / -475 / 2048 → 1.042）：实宽 816px，实际 em≈63.3px
#:   - 2018 与 7.1 两个 ffmpeg 构建（fontconfig / DirectWrite provider）都是 0.75 左右，结论一致
#:
#: 所以按 AGENTS 公式算出的 66px 字号写进 ASS 后，微软雅黑上实际只有约 50px 大——硬字幕
#: 比画面里同一公式的 ``<Caption>`` 小约 24%，且不报任何错。修法：写 ASS 的 Fontsize 乘上
#: 这个比值，让 libass 渲出来的 em 高 = AGENTS 公式要的像素数（R6-subs-06）。
_FONT_HEIGHT_RATIO_CACHE: Dict[str, Optional[float]] = {}
FONT_RATIO_MIN = 0.8
FONT_RATIO_MAX = 2.0


def _font_height_ratio(family: str) -> Optional[float]:
    """族名对应字体的 ``(hhea.ascent - hhea.descent) / unitsPerEm``；量不出返回 None。

    只用来给 ASS Fontsize 做补偿（见上面缓存注释的实测数字）。量不出（族名是
    ``sans-serif`` 这类通用名 / 没装 fontTools / 字体坏）就返回 None，调用方按 1.0
    处理——宁可字号回到旧行为，也不能因为量一个比例把烧录搞挂。
    """
    key = str(family or "").strip()
    if not key:
        return None
    if key in _FONT_HEIGHT_RATIO_CACHE:
        return _FONT_HEIGHT_RATIO_CACHE[key]

    font_file = _family_available(key)
    if font_file is None:
        matched = _fc_match(key)
        if matched is not None and _family_matches_request(key, [matched[0]]):
            font_file = matched[1]

    ratio: Optional[float] = None
    if font_file is not None:
        try:
            from fontTools.ttLib import TTFont  # 模板依赖里已有 fontTools

            with TTFont(str(font_file), fontNumber=0, lazy=True) as tt:
                upem = float(tt["head"].unitsPerEm)
                hhea = tt["hhea"]
                raw = (float(hhea.ascent) - float(hhea.descent)) / upem
            if math.isfinite(raw) and FONT_RATIO_MIN <= raw <= FONT_RATIO_MAX:
                ratio = raw
        except Exception:  # noqa: BLE001 - 量不出比例不该中断烧录，退回旧行为
            ratio = None
    _FONT_HEIGHT_RATIO_CACHE[key] = ratio
    return ratio


# --- 颜色 -----------------------------------------------------------------

def _hex_to_ass_color(value: Any, alpha: int = 0) -> str:
    """``#RRGGBB`` -> ASS 的 ``&HAABBGGRR``（**ASS 是 BGR 顺序**，写反了就是红蓝互换）。"""
    text = str(value or "").strip().lstrip("#")
    if len(text) == 3:
        text = "".join(ch * 2 for ch in text)
    if len(text) != 6 or not re.fullmatch(r"[0-9A-Fa-f]{6}", text):
        return f"&H{alpha:02X}FFFFFF"
    r, g, b = text[0:2], text[2:4], text[4:6]
    return f"&H{alpha:02X}{b}{g}{r}".upper()


# --- ffmpeg ---------------------------------------------------------------

def _ensure_ffmpeg() -> None:
    missing = [name for name in ("ffmpeg", "ffprobe") if not has_cmd(name)]
    if missing:
        error(
            "找不到 %s。ffmpeg/ffprobe 是本脚本的硬依赖（AGENTS.md 第 6 节），"
            "装好后重跑：Windows `winget install Gyan.FFmpeg`（或把解压出的 bin 目录加进 PATH）、"
            "macOS `brew install ffmpeg`、Ubuntu `sudo apt install ffmpeg`；"
            "装完跑 `python scripts/doctor.py` 确认。" % ("、".join(missing),)
        )


def _run_ffmpeg(args: Sequence[Any], what: str, timeout: float = FFMPEG_TIMEOUT_S):
    """跑一条 ffmpeg；失败时回显 stderr 尾部（过滤器报错全在 stderr 里）。"""
    cmd = ["ffmpeg", "-hide_banner", "-nostdin", "-y", *[str(a) for a in args]]
    try:
        proc = subprocess.run(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout
        )
    except FileNotFoundError:
        error(f"执行 {what} 时找不到 ffmpeg。请确认它已安装并在 PATH 里（跑 doctor.py 体检）。")
    except subprocess.TimeoutExpired:
        error(
            f"{what} 超过 {timeout:.0f} 秒还没结束，已放弃。"
            "整片烧字幕本来就慢，先用 `--preview 8` 验证，或换更快的机器/降低 --preset。"
        )
    except OSError as exc:
        error(f"执行 {what} 失败：{exc}")
    if proc.returncode != 0:
        detail = proc.stderr.decode("utf-8", errors="replace").strip().splitlines()
        tail = "\n".join(detail[-12:]) if detail else "（ffmpeg 没有输出错误信息）"
        error(f"{what} 失败（退出码 {proc.returncode}）：\n{tail}")
    return proc


def _ffprobe(path: Path) -> Dict[str, Any]:
    try:
        proc = subprocess.run(
            [
                "ffprobe", "-v", "error", "-print_format", "json",
                "-show_format", "-show_streams", str(path),
            ],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=FFPROBE_TIMEOUT_S,
        )
    except FileNotFoundError:
        error("找不到 ffprobe。它随 ffmpeg 一起安装，请确认 PATH 里有它（跑 doctor.py 体检）。")
    except subprocess.TimeoutExpired:
        error(f"ffprobe 读 {path.name} 超过 {FFPROBE_TIMEOUT_S:.0f} 秒还没返回。")
    except OSError as exc:
        error(f"执行 ffprobe 失败：{exc}")
    text = proc.stdout.decode("utf-8", errors="replace").strip()
    if not text:
        error(f"ffprobe 读不出 {path.name}（退出码 {proc.returncode}）：文件可能不是有效媒体或已损坏")
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        error(f"ffprobe 返回的 JSON 解析失败（{exc}）")
    return data if isinstance(data, dict) else {}


def _audio_duration(path: Path) -> Optional[float]:
    """ffprobe 量音频时长；测不到返回 None（调用方回退到字数估算）。

    入口的 ``_ensure_ffmpeg()`` 已经挡住「没装 ffprobe」，这里再自查一次：调用链不止
    一条（``srt``/``ass``/``burn`` 都走到这里），而「缺工具」是 ERROR 级故障，不是降级
    路径 —— 以前它被下面的 ``except SystemExit`` 吞掉，于是没装 ffprobe 的机器上会打
    ERROR 行、却以退出码 0 写出按字数估算的字幕（闸门形同虚设，见 FIX-B P2-20）。
    吞 ``SystemExit`` 因此只剩「文件本身量不出来」（坏文件 / 不是媒体），调用方会为它打
    WARN 并回退，那是文件头写明的降级路径。
    """
    if not has_cmd("ffprobe"):
        # FIX-B P2-20 兜底：缺工具 → 真 ERROR + 非零退出，绝不静默估算
        error("找不到 ffprobe。它随 ffmpeg 一起安装，请确认 PATH 里有它（跑 doctor.py 体检）。")
    try:
        info = _ffprobe(path)
    except SystemExit:
        return None
    fmt = info.get("format") or {}
    for candidate in (fmt.get("duration"),):
        value = _num(candidate, -1.0)
        if value > 0:
            return value
    for stream in info.get("streams") or []:
        if stream.get("codec_type") == "audio":
            value = _num(stream.get("duration"), -1.0)
            if value > 0:
                return value
    return None


# ---------------------------------------------------------------------------
# Windows 下 ass= 滤镜的路径转义（**实测过的坑，见文件头注释**）
# ---------------------------------------------------------------------------

def _escape_filter_path(path: Path) -> str:
    r"""把文件路径转成能塞进 ffmpeg 滤镜图的字符串（**实测过的规则，别改**）。

    Windows 上 ``ass=`` 的路径是这块最著名的坑，实测（本机 ffmpeg 7.1，见
    out/verify-subs/_esc3.py / _esc4.py）得到三条硬规则：

    1. **反斜杠必须换成正斜杠**。ffmpeg 的滤镜参数把 ``\`` 当转义引导符吃掉：
       ``F:\DeepSeek Harness\...`` 会被解析成 ``F:DeepSeek Harness...``（空格都没了），
       于是报 ``Unable to parse option value "DeepSeek Harness..." as image size`` ——
       它把后半截当成 ``original_size`` 的值了。
    2. **盘符的 ``:`` 必须转义成 ``\:``**。``:`` 是滤镜参数的 key:value 分隔符，
       不转义的话 ``F:/x/y.ass`` 会被拆成 ``filename=F`` + ``original_size=/x/y.ass``。
    3. **整个文件名必须用单引号包起来**。注意：只做第 2 条、不加引号**照样失败**
       （实测 `F\:/x/y.ass` 仍然被拆成 original_size），引号是把 ``:`` 挡在
       key:value 解析之外的真正原因，``\:`` 只负责让 ``:`` 活到引号里。

    能安全处理的字符（全部实测通过）：空格、中文、``,`` ``;`` ``[`` ``]`` ``(`` ``)``
    ``%`` ``#``。**唯一处理不了的是单引号 ``'``**：ffmpeg 的解析器先做「引号翻转」再做
    转义，``'\''`` / ``\'`` / ``\\'`` / ``'"'"'`` 四种写法实测全部被解析成 ``quotes.ass``
    （撇号连同反斜杠一起消失），这是解析器的固有行为，无解。真遇到这种路径，
    调用方应该改用 ``_write_filter_script()`` 那条路（把滤镜图写进文件），别在这里硬凑。
    """
    text = str(path).replace("\\", "/").replace(":", "\\:")
    return "'" + text + "'"


def _filter_script_text(ass_path: Path) -> str:
    """生成滤镜图脚本文本：``ass='F\\:/x/y.ass',format=yuv420p``。"""
    return "ass=" + _escape_filter_path(ass_path) + ",format=yuv420p"


def _write_filter_script(dst: Path, ass_path: Path) -> Path:
    r"""把滤镜图写进文件，用 ``-/filter_complex`` 交给 ffmpeg。

    **为什么不用 `-vf "ass=..."` 直接在命令行里塞路径**：路径要先过滤镜图解析器，
    反斜杠会被当转义符吃掉、盘符的 ``:`` 会被当 key:value 分隔符，而引号又只能解决
    后者。实测（out/verify-subs/_esc3.py、_esc4.py）：命令行里唯一能用的写法是
    ``ass='F\:/x/y.ass'``（单引号 + 正斜杠 + 转义冒号），一共三层规则，很容易写错；
    而写进文件后路径是**原样的普通文本**，只有正斜杠这一条要求，简单得多。

    注意 ffmpeg 7.1 已把 ``-filter_complex_script`` 标记为 deprecated，会打印
    ``-filter_complex_script is deprecated, use -/filter_complex <file> instead`` 并
    直接失败；``-/filter_complex`` 是它的替代写法（斜杠开头的选项 = 从文件读该选项的值）。
    """
    path = dst.parent / f".subtitles-{dst.stem}.filter"
    # 内容照样要按滤镜图语法转义（从文件读并不跳过滤镜解析器，实测如此）：
    # 复用 _escape_filter_path 那三条规则，文件里就不必再写第二套转义逻辑。
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_filter_script_text(ass_path) + "\n", encoding="utf-8")
    except OSError as exc:
        # 输出目录不可写时别抛裸 traceback：与 _write_srt/_build_ass 一样给 ERROR 行（R5-subs-04）。
        error(f"滤镜脚本写不进 {path}：{exc}（检查输出目录 {dst.parent} 是否存在/可写）")
    return path


# ===========================================================================
# 读配音产物（audio-src/vo/report.json + ffprobe 实测时长）
# ===========================================================================

def _load_vo_report() -> Tuple[Dict[str, Dict[str, Any]], Path]:
    """把 report.json 的 lines 变成 ``{id: entry}``；缺失/损坏返回空表（调用方 WARN）。"""
    path = PROJECT_ROOT / VO_DIRNAME / REPORT_NAME
    if not path.is_file():
        return {}, path
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return {}, path
    if not isinstance(data, dict) or not isinstance(data.get("lines"), list):
        return {}, path
    out: Dict[str, Dict[str, Any]] = {}
    for item in data["lines"]:
        if isinstance(item, dict):
            out[str(item.get("id", "")).strip()] = item
    return out, path


class Cue:
    """一条字幕：真的只是「一行字 + 起止秒」，时间轴已按 AGENTS.md 的口径算好。"""

    __slots__ = ("line_id", "scene", "text", "lines", "start", "end", "vo_s", "min_s", "source")

    def __init__(self, line_id, scene, text, lines, start, end, vo_s, min_s, source):
        self.line_id = line_id
        self.scene = scene
        self.text = text
        self.lines = lines
        self.start = start
        self.end = end
        self.vo_s = vo_s        #: 真实配音时长（秒）；估算时为 None
        self.min_s = min_s      #: 字数 / 6 + 0.35
        self.source = source    #: "ffprobe" | "report" | "estimate"

    @property
    def dur(self) -> float:
        return self.end - self.start


def _estimate_duration(text: str) -> float:
    """没量到 mp3 时的兜底时长：按 AGENTS.md 的语速口径（5.5~6.5 字/秒）取中间值。"""
    return max(MIN_HOLD_PAD_S, _vis_len(text) / CHARS_PER_SEC + MIN_HOLD_PAD_S)


def build_cues(timing: Dict[str, Any]) -> List[Cue]:
    """把 timing.json 的旁白稿 + 真实配音时长算成字幕条目。"""
    captions = timing.get("captions") if isinstance(timing.get("captions"), dict) else {}
    max_chars = _int(captions.get("maxChars"), 13)
    max_lines = _int(captions.get("maxLines"), 2)
    if max_lines <= 0:
        # 0 / 负数会让 _check_fold_lines 把「超行」硬闸门静默关掉（V42 的意义就没了），
        # 是无效配置而不是「不限制」：直接停，并说清楚怎么改。
        error(
            f"timing.json 的 captions.maxLines={max_lines} 不合法：必须是正整数。"
            "字幕最多 2 行是 AGENTS.md 阶段 7 的硬要求（0/负数会让超行闸门静默失效），"
            "请把它改成 2（或你确实要的行数）再跑"
        )

    voice = timing.get("voice") if isinstance(timing.get("voice"), dict) else {}
    raw_lines = voice.get("lines")
    if not isinstance(raw_lines, list) or not raw_lines:
        error(
            "timing.json 的 voice.lines 是空的，没有可做字幕的内容。"
            "请先写旁白稿：每句带 id/scene/at/text（AGENTS.md 阶段 7）"
        )

    entries, report_path = _load_vo_report()
    vo_dir = PROJECT_ROOT / VO_DIRNAME
    if not entries:
        warn(
            f"没有读到 {_rel(report_path)}（或格式不对），无法核对真实配音时长："
            "字幕时长回退到按字数估算。先跑 `python scripts/tts.py all` 生成配音，再重跑本脚本。"
        )

    cues: List[Cue] = []
    for order, item in enumerate(raw_lines, start=1):
        if not isinstance(item, dict):
            continue
        line_id = str(item.get("id", "")).strip() or f"#{order}"
        raw_text = item.get("text")
        if isinstance(raw_text, str) and CONTROL_CHARS_RE.search(raw_text):
            warn(
                f"第 {line_id} 句的 text 含控制字符（NUL/BEL 等不可见字符），已剥掉："
                "它们会让 SRT/ASS 里的字幕整份读不出来（R6-subs-01）"
            )
        text = _clean_text(raw_text)
        if not text:
            if raw_text is None or raw_text == "":
                error(
                    f"第 {line_id} 句没有 text 字段，无法生成字幕"
                    "（AGENTS.md 阶段 7：每句要有 id/scene/at/text）"
                )
            error(
                f"第 {line_id} 句的 text={raw_text!r} 只有空白字符（清洗后为空），无法生成字幕："
                "请写实际文案，或把这句从 voice.lines 里删掉"
            )
        scene = str(item.get("scene", "") or "")
        at = _num(item.get("at"), 0.0)
        if not math.isfinite(at) or at < 0:
            # NaN/Infinity 会让所有区间比较恒为 False，最后被 _split_ms 的 max(0, …) 夹成
            # 00:00:00,000 --> 00:00:00,000 的零时长字幕，rc 还是 0；负数同理（R6-subs-02）。
            error(
                f"第 {line_id} 句的 at={at:g} 不合法：at 必须是不小于 0 的有限秒数"
                "（AGENTS.md §5；timing.json 里不要写 NaN / Infinity / 负数）"
            )

        # 起点取 timing.json 的 at（字幕可以比人声略早出现，但不早于这句的 at）。
        start = at

        # 字数目测下限：AGENTS.md 阶段 7「停留时长 ≥ 字数 / 6 + 0.35 秒」。
        min_s = _vis_len(text) / CHARS_PER_SEC + MIN_HOLD_PAD_S

        # 真实配音时长：以 ffprobe 实测为准（report.json 里的 duration 是 tts.py 当时的测量，
        # 换过音色/重新生成过就会过期，所以只当兜底）。
        mp3 = vo_dir / f"{line_id}.mp3"
        vo_s: Optional[float] = None
        source = "estimate"
        if mp3.is_file() and mp3.stat().st_size > 0:
            measured = _audio_duration(mp3)
            if measured:
                vo_s = measured
                source = "ffprobe"
            else:
                warn(f"第 {line_id} 句的 {_rel(mp3)} 量不出时长（文件坏了？），回退到按字数估算")
        elif line_id in entries:
            recorded = _num(entries[line_id].get("duration"), 0.0)
            if recorded > 0:
                vo_s = recorded
                source = "report"
        if vo_s is None:
            warn(
                f"第 {line_id} 句找不到 {_rel(mp3)}，按字数估算时长"
                f"（{_vis_len(text)} 字 -> {_estimate_duration(text):.2f}s）。"
                "配音还没生成时这是正常的，但交付前必须跑 tts.py all 后重做字幕"
            )

        # 终点 = 起点 + max(实测时长, 字数目测下限)，但不得越过该句所属场景的窗口末端。
        hold = max(vo_s if vo_s else _estimate_duration(text), min_s)
        end = start + hold

        if scene:
            try:
                scene_start, scene_end = scene_window(timing, scene)
            except ValueError as exc:
                warn(f"第 {line_id} 句的 scene={scene!r} 在 timing.json 里找不到（{exc}），按全片时长收边")
                scene_start, scene_end = 0.0, _num(timing.get("duration"), end)
            limit = scene_end - TAIL_OFF_S
            if end > limit:
                # 只提示、不报错：量出来的配音确实可能比场景还长，那是 tts.py audit 该拦的事
                # （TOO_LONG 要改稿），本脚本的职责是别让字幕串场。
                warn(
                    f"第 {line_id} 句按实测时长要走到 {fmt_sec(end)}，越过场景「{scene}」末端"
                    f"（{fmt_sec(scene_end)}）减 {TAIL_OFF_S}s 的收边线：字幕已压到 {fmt_sec(limit)}，"
                    "避免和下一场景的字幕叠在一起。请跑 `python scripts/tts.py audit` 确认这句是不是 TOO_LONG"
                )
                end = limit
            if end <= start:
                # 收边分支不能再往外顺延：越过 scene_end 就会和下一场景的字幕同屏，
                # 越过 timing.duration 则会写出一条片尾之后的字幕（R5-subs-01）。
                # 能给的只有「场景末端 / 片长」这一截，取两者较小值。
                room_end = min(scene_end, _num(timing.get("duration"), scene_end))
                if room_end <= start:
                    error(
                        f"第 {line_id} 句的 at={fmt_sec(start)} 已经在场景「{scene}」末端"
                        f"（{fmt_sec(scene_end)}）或之后，场景里没有位置写字幕。"
                        "请把 timing.json 里这句的 at 前移，或延长这个场景"
                    )
                end = min(start + hold, room_end)
                warn(
                    f"第 {line_id} 句在场景「{scene}」末端只剩 {fmt_sec(room_end - start)}"
                    f"（{fmt_sec(start)}~{fmt_sec(room_end)}，字数目测下限 {fmt_sec(min_s)}）："
                    f"字幕终点压到 {fmt_sec(end)}，不再越过场景末端（越过就会串场/越片长）。"
                    "请把 timing.json 的 at 前移或删字缩短这句，否则这句会一闪而过"
                )

        lines = _fold_text(text, max_chars, max_lines)
        _check_fold_lines(line_id, text, lines, max_chars, max_lines)
        cues.append(Cue(line_id, scene, text, lines, start, end, vo_s, min_s, source))

    if not cues:
        error("timing.json 的 voice.lines 里没有一个能用的句子，无法生成字幕")

    # 叠字检查：后一句比前一句早开就算叠字，玩家会把两句同时压在屏上（人工审图最容易漏的项）。
    ordered = sorted(cues, key=lambda c: c.start)
    for prev, cur in zip(ordered, ordered[1:]):
        if cur.start < prev.end - 1e-6:
            warn(
                f"第 {prev.line_id} 句（{fmt_sec(prev.start)}~{fmt_sec(prev.end)}）与第 {cur.line_id} 句"
                f"（{fmt_sec(cur.start)}~{fmt_sec(cur.end)}）在屏上重叠 {fmt_sec(prev.end - cur.start)}："
                "同屏两句字幕会互相盖住，请调整 timing.json 的 at 或缩短前一句"
            )

    check_voice_windows(timing, raw_lines)
    return cues


def check_voice_windows(timing: Dict[str, Any], raw_lines: List[Any]) -> int:
    """旁白**窗口不够放**检查，返回有问题的句数。

    先说清楚为什么不是「窗口相交检查」（这一点被实测推翻了）：
    ``_common.line_window()`` 的窗口是「本句 at → min(场景结束, 下一句 at) - 0.15s」，
    所以任何时刻的窗口集合都是**按 at 切出来的一串互不相交的区间**（实测：at=0/10/37/17
    四句得到 [0,9.85] [10,16.85] [17,36.85] [37,59.85]，两两间隙恰好都是 0.15 秒）。
    因此「窗口重叠」永远不可能发生，写成交叉检查就是死代码。

    真正的风险是这个：**窗口装不下这句的实测人声**。窗口末端被下一句的 at 钉死，
    而实测时长由稿子长度决定；`subtitles.py` 自己会把字幕压到窗口内（不串场），
    但压不回来的部分是**人声本身溢出了**，听感上就是「这句还没说完下一句就开口」。
    本检查把它显式报出来，并给出每种写法还差多少秒。

    ``tts.py audit`` 也判同一件事（TOO_LONG，先尝试 ≤12% 加速）；本函数是字幕侧的
    独立提示，用 ``audio-src/vo/report.json`` 的实测时长，两者口径相同、互相印证。
    窗口取不到（``line_window`` 抛错）时不在这里报错 —— 那是 ``tts.py audit`` 的 ERROR。
    """
    entries, _ = _load_vo_report()
    vo_dir = PROJECT_ROOT / VO_DIRNAME
    problems = 0

    for order, item in enumerate(raw_lines, start=1):
        if not isinstance(item, dict):
            continue
        line_id = str(item.get("id", "")).strip() or f"#{order}"
        try:
            win_start, win_end = line_window(timing, item)
        except (ValueError, KeyError, TypeError):
            continue

        # 实测人声时长：与 build_cues 同口径（ffprobe 优先，report.json 兜底）。
        mp3 = vo_dir / f"{line_id}.mp3"
        measured: Optional[float] = None
        source = ""
        if mp3.is_file() and mp3.stat().st_size > 0:
            measured = _audio_duration(mp3)
            source = _rel(mp3)
        elif line_id in entries:
            recorded = _num(entries[line_id].get("duration"), 0.0)
            if recorded > 0:
                measured = recorded
                source = "report.json"
        if not measured:
            continue  # 还没有配音；build_cues 那边已经 Warn 过了

        room = win_end - win_start
        over = measured - room
        if over <= 1e-6:
            continue
        problems += 1
        need_speed = measured / room if room > 0 else float("inf")
        warn(
            f"第 {line_id} 句的窗口只放得下 {fmt_sec(room)}"
            f"（{fmt_sec(win_start)}~{fmt_sec(win_end)}），但 {source} 实测 {fmt_sec(measured)}，"
            f"超出 {fmt_sec(over)}：这句人声会盖到下一句的起播点。"
            f"要到齐得加速到 {need_speed:.2f}×（tts.py 只允许 ≤12%，即 1.12×），"
            "所以按 AGENTS.md 阶段 7 改短稿子，或把后一句的 at 往后挪"
        )

    if problems:
        warn(
            f"共 {problems} 句窗口不够放。tts.py audit 对同一批句子会报 TOO_LONG；"
            "字幕侧只能把字幕压进窗口（不串场），压不住人声，请按上面的秒数改稿后重跑 "
            "`python scripts/tts.py all` 再回来生成字幕"
        )
    return problems


# ===========================================================================
# 写出 SRT / ASS
# ===========================================================================

def _write_srt(cues: List[Cue], path: Path) -> None:
    blocks = []
    # SRT 消费端默认按文件顺序读；timing.json 里 voice.lines 万一是乱序（不按 at 升序），
    # 直接照抄会让时间轴非单调（R6-subs-03）。这里按开始时间稳定排序；已排序的项目输出不变。
    ordered = sorted(enumerate(cues), key=lambda pair: (pair[1].start, pair[1].end, pair[0]))
    for index, (_, cue) in enumerate(ordered, start=1):
        body = "\r\n".join(cue.lines)
        blocks.append(
            f"{index}\r\n{fmt_srt_time(cue.start)} --> {fmt_srt_time(cue.end)}\r\n{body}\r\n"
        )
    # SRT 用 CRLF（Windows 播放器与 Premiere/达芬奇都最省事），末尾留一个空行。
    text = "\r\n".join(blocks)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # Path.write_text(newline=...) 是 Python 3.10+ 的参数；本仓库下限 3.9（见
        # AGENTS.md §6 与 fonts.py 的同款注释），所以用 open() 来指定换行符。
        with path.open("w", encoding="utf-8", newline="") as fh:
            fh.write(text)
    except OSError as exc:
        error(f"字幕写不进 {path}：{exc}")


ASS_HEADER = """[Script Info]
; promo-forge 生成的字幕（scripts/subtitles.py ass）
; 时间轴 = timing.json 的 voice.lines[].at + audio-src/vo/<id>.mp3 的实测时长，
; 停留下限 = 字数 / {cps:g} + {pad:g} 秒，终点不越过所属场景的窗口末端。
Title: {title}
ScriptType: v4.00+
WrapStyle: 2
ScaledBorderAndShadow: yes
YCbCr Matrix: TV.709
PlayResX: {play_w}
PlayResY: {play_h}

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: {style}

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""


def _warn_about_project_font(cfg: Dict[str, Any], family: str) -> None:
    """用了自带字体就提示一句：字幕走的是系统字体，字形可能和画面不完全一致。

    为什么是 WARN 而不是「干脆用自带字体」：``roles.<role>.file`` 指向的往往是
    ``scripts/fonts.py subset`` 生成的**用字子集**（只含当时片中用到的字）。字幕是
    按旁白稿生成的，改一句文案就可能引入子集外的字，那时渲染出来的还是豆腐块，
    而且同样**不会报错**。宁可字形略有差异，也不能悄悄变成方框。
    """
    roles = cfg.get("roles") if isinstance(cfg.get("roles"), dict) else {}
    body = roles.get("body") if isinstance(roles.get("body"), dict) else {}
    rel_file = str(body.get("file") or "").strip()
    if not rel_file:
        return
    # file 是相对 public/ 的路径（见 fonts.json 的 _note）。
    font_file = PROJECT_ROOT / "public" / rel_file
    if font_file.is_file():
        warn(
            f"fonts.json 的 roles.body.file 指向自带字体 {rel_file}，但本脚本的字幕走系统字体 "
            f"「{family}」（libass 只认系统族名）。字形可能与画面字幕略有差异；"
            "要让字幕也用这个字体，需要给 ass 滤镜加 fontsdir，当前未实现"
        )


def _build_ass(cues: List[Cue], timing: Dict[str, Any], path: Path, fontsize: Optional[float]) -> str:
    captions = timing.get("captions") if isinstance(timing.get("captions"), dict) else {}
    brand = timing.get("brand") if isinstance(timing.get("brand"), dict) else {}
    colors = brand.get("colors") if isinstance(brand.get("colors"), dict) else {}

    play_w = max(1, _int(timing.get("width"), 1080))
    play_h = max(1, _int(timing.get("height"), 1920))
    margin_l = int(round(play_w * SAFE_LEFT))
    margin_r = int(round(play_w * SAFE_RIGHT))
    max_chars = max(1, _int(captions.get("maxChars"), 13))
    avail_w = max(1, play_w - margin_l - margin_r)

    # 字号按画布高度换算，保证 9:16 与 16:9 用同一套 timing.json 都不破版；
    # 再用「左右安全区可用宽度 / maxChars」压一次上限（V36）：只按高度算时
    # 13 个全角字 × 73px = 949px，会超出 6% + 14% 留白后的 864px，侵入两侧安全区。
    if fontsize:
        size = float(fontsize)
    else:
        size_by_height = round(play_h * FONT_SCALE)
        size = float(max(FONT_SIZE_MIN, min(size_by_height, int(avail_w // max_chars))))
        if size < size_by_height:
            log(
                f"字号压到 {size:g}：{max_chars} 字要落在左右安全区"
                f"（{margin_l}+{margin_r} 留白后剩 {avail_w}px）内，按画布高度会到 {size_by_height}"
            )

    # MarginV 由 captions.bottom（相对高度）换算：ASS 里 Alignment=2 的 MarginV 是
    # 「文字底边离画面底边的距离」，所以直接乘画布高；这样烧出来的字幕与画面里
    # <Caption> 组件用 bottom: captions.bottom 渲染的位置完全一致。
    # （2026-10-11 实测踩坑：这里曾写成 (1 - bottom) × 画布高，MarginV 变成 1613，
    #   字幕被渲到画面顶部，而 ffmpeg 退出码仍是 0 —— 只能靠抽帧目视发现。）
    # 缺省值就是安全区下界（SAFE_BOTTOM）：以前这里写死 0.16，比安全区还低，
    # 于是「一个字都没填」的 timing.json 必然触发下面的抬升 + WARN，画面字幕
    # （<Caption> 用 0.16 渲在 307px）与烧入硬字幕（被抬到 346px）差出 39px。
    bottom = _num(captions.get("bottom"), SAFE_BOTTOM)
    if not 0.0 < bottom < 1.0:
        warn(f"timing.json 的 captions.bottom={bottom!r} 不是 0~1 之间的小数，按 {SAFE_BOTTOM} 处理")
        bottom = SAFE_BOTTOM
    margin_v = int(round(bottom * play_h))
    v_min = int(round(play_h * SAFE_BOTTOM))          # 离底边至少 18%（不被平台 UI 盖住）
    v_max = int(round(play_h * (1.0 - SAFE_TOP)))     # 离底边最多 90%（不顶到画面上部）
    if margin_v < v_min:
        warn(
            f"captions.bottom={bottom:g} 换算出的 MarginV={margin_v} 落在底部安全区（下 {SAFE_BOTTOM:.0%}）里，"
            f"已抬到 {v_min}（约屏高 {SAFE_BOTTOM:.0%}）"
        )
        margin_v = v_min
    if margin_v > v_max:
        warn(
            f"captions.bottom={bottom:g} 换算出的 MarginV={margin_v} 顶到上部安全区，已降到 {v_max}"
            f"（上 {SAFE_TOP:.0%} 以下）"
        )
        margin_v = v_max

    # 字体要走「本机真能用」的解析，不能直接取 stack 首位——在 Windows 上取到
    # macOS 专属的 PingFang SC 时 libass 会静默换字体、把汉字渲成方框（本节注释有实测记录）。
    all_text = "".join(cue.text for cue in cues)
    cfg = fonts_config()
    family = _resolve_font_family(cfg, all_text)
    # fonts.json 的 roles.body.file 指向项目自带字体时，**我们没有用它**：libass 只认
    # 系统里装了的族名，要让它用项目字体得给 ass 滤镜加 fontsdir（本脚本当前没做）。
    # 项目字体通常是「只含片中用字」的子集（scripts/fonts.py subset 的产物），而字幕文案
    # 改一句就可能引入子集外的字，所以这里照旧走系统字体更稳；但要提醒用户：
    # 若模板画面用的是自带字体，字幕字形可能和画面不完全一致。
    _warn_about_project_font(cfg, family)
    # ASS 的 Fontsize 不是 em 高：libass 按字体 (ascender−descender) 归一（实测数字见
    # _font_height_ratio 的注释）。把比例乘回去，烧进画面的字才真的等于上面按 AGENTS
    # 公式算出的 em 像素高；不乘的话微软雅黑实际只有 ~76%，硬字幕比画面 <Caption> 小一截。
    ratio = _font_height_ratio(family)
    ass_size = round(size * (ratio or 1.0), 1)
    if ratio is not None and abs(ratio - 1.0) > 0.01:
        log(
            f"ASS 字号补偿：{family} 的 (ascender−descender)/em={ratio:.3f}，"
            f"em {size:g}px → 写进 ASS 的 Fontsize={ass_size:g}"
            f"（不补偿实测只有 {size / ratio:.1f}px，R6-subs-06）"
        )
    primary = _hex_to_ass_color(colors.get("fg", "#FFFFFF"))
    outline_colour = _hex_to_ass_color(colors.get("bg", "#000000"))

    # BorderStyle=1（描边 + 阴影）比 BackColour 色块更像短视频字幕；Alignment=2 是底部居中。
    style = ", ".join(
        [
            "Default",
            family,
            f"{ass_size:g}",
            primary,
            _hex_to_ass_color(colors.get("accent", "#FFFFFF")),
            outline_colour,
            _hex_to_ass_color(colors.get("bg", "#000000"), alpha=0x80),
            "0", "0", "0", "0",
            "100", "100", "0", "0",
            "1",
            f"{DEFAULT_OUTLINE:g}",
            f"{DEFAULT_SHADOW:g}",
            "2",
            f"{margin_l}",
            f"{margin_r}",
            f"{margin_v}",
            "1",
        ]
    )

    header = ASS_HEADER.format(
        cps=CHARS_PER_SEC,
        pad=MIN_HOLD_PAD_S,
        title=str(timing.get("title", "") or timing.get("slug", "") or "promo-forge"),
        play_w=play_w,
        play_h=play_h,
        style=style,
    )

    events = []
    for cue in cues:
        # ASS 正文必须转义：花括号是 override 块、\N/\n/\h 是换行/空格指令，
        # 不转义的话烧出来会静默改字（R5-subs-02，_escape_ass_text 的 docstring 有像素实测）。
        body = r"\N".join(_escape_ass_text(_clean_text(line)) for line in cue.lines)
        start = fmt_ass_time(cue.start)
        end = fmt_ass_time(cue.end)
        events.append(f"Dialogue: 0,{start},{end},Default,,0,0,0,,{body}")
    text = header + "\n".join(events) + "\n"

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # 用 UTF-8 **不带 BOM** 写：libass 对 BOM 开头的 [Script Info] 会解析失败。
        # newline="\n" 经 open() 传（Path.write_text(newline=) 要 Python 3.10+，下限 3.9）。
        with path.open("w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
    except OSError as exc:
        error(f"字幕写不进 {path}：{exc}")

    log(
        f"ASS 样式：字体 {family}，字号 {size:g}（em，写进 ASS 的 Fontsize={ass_size:g}；"
        f"画布 {play_w}×{play_h} 的 {size / play_h:.1%}；上限 = min(高度×{FONT_SCALE:g}, "
        f"{avail_w}px / {max_chars} 字)），"
        f"MarginV {margin_v}（离底边，对应 captions.bottom={margin_v / play_h:.3f}），"
        f"左右留白 {margin_l}/{margin_r}"
    )
    return text


def _describe(cue: Cue) -> str:
    """一行摘要：句号 / 场景 / 时间轴 / 实测时长 vs 下限 / 折行数。"""
    vo = f"{cue.vo_s:.2f}s" if cue.vo_s is not None else "—（估算）"
    return (
        _pad(f"第 {cue.line_id} 句", 10)
        + _pad(f"[{cue.scene or '-'}]", 12)
        + _pad(f"{fmt_srt_time(cue.start)} → {fmt_srt_time(cue.end)}", 34)
        + _pad(f"停留 {cue.dur:.2f}s", 12)
        + _pad(f"配音 {vo}", 14)
        + _pad(f"下限 {cue.min_s:.2f}s", 12)
        + f"{len(cue.lines)} 行"
    )


def _print_cues(cues: List[Cue]) -> None:
    log("")
    for cue in cues:
        log(_describe(cue))
        for line in cue.lines:
            log(f"          │ {line}")


def _file_stats(path: Path) -> str:
    data = path.read_bytes()
    lines = data.decode("utf-8", errors="replace").splitlines()
    return f"{human(len(data))}（{len(data)} 字节） / {len(lines)} 行"


def _source_stamp() -> float:
    """``src/timing.json`` 的修改时间；读不到时返回「现在 + 1 秒」。

    读不到就不该相信磁盘上任何已有产物 —— 返回一个比「刚刚写出来的文件」还晚的时间戳，
    让 mtime 比较一律判过期、宁可重建，也不烧一份来路不明的旧 ASS。
    """
    try:
        return TIMING_PATH.stat().st_mtime
    except OSError:
        return time.time() + 1.0


def _is_stale(path: Path, stamp: float, slack: float = 0.5) -> bool:
    """``path`` 是不是比 ``stamp`` 旧（``slack`` 秒容差，避开同秒写入的抖动）。

    ``slack`` 的必要性：``subtitles.py ass`` 紧接着 ``burn`` 跑时，两份产物的 mtime
    可能落在同一秒里；没有容差就会把刚生成的 ASS 再生成一遍（无害但会刷屏）。
    """
    try:
        return path.stat().st_mtime < stamp - slack
    except OSError:
        return True


def _stale_audio_note(stamp: float) -> str:
    """有配音音频比 ``src/timing.json`` 更新时，返回一句该提醒的话（否则空串）。

    为什么要有这条：ASS 的每句时长既可能来自 timing.json 的窗口，也可能来自 tts 跑完
    写下的实测时长。只重跑 ``tts.py all``（音频变长变短）而不动 timing.json 时，
    mtime 判据一点都看不出来，烧出去的仍是老时间轴。这里不自动重建（重建与否该由人定），
    只把「你现在烧的可能是旧时间轴」这层信息说出来。
    """
    try:
        voice_dir = PROJECT_ROOT / VO_DIRNAME
        files = sorted(voice_dir.glob("*")) if voice_dir.is_dir() else []
    except OSError:
        return ""
    newest = 0.0
    newest_name = ""
    for path in files:
        if path.suffix.lower() not in (".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg"):
            continue
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        if mtime > newest:
            newest, newest_name = mtime, path.name
    if newest <= stamp - 0.5:
        return ""
    return (
        f"注意：{newest_name} 比 {TIMING_PATH.name} 新（重跑过 tts？），"
        "而本命令只按 timing.json 的修改时间决定要不要重建 ASS；"
        "音频长短变了要显式跑 `python scripts/subtitles.py ass` 重生成字幕。"
    )


# ===========================================================================
# 子命令
# ===========================================================================

def _out_was_given() -> bool:
    """命令行里是否显式给了 ``-o`` / ``--out``。

    用来区分「用户指定了输出名」和「用了默认值」：只有后者才允许在 ``--preview``
    时自动把名字改成 ``*-preview.mp4``，不能偷偷改用户点名要的路径。
    """
    return any(a == "--out" or a == "-o" or a.startswith("--out=") for a in sys.argv[1:])


def cmd_srt(args) -> int:
    # FIX-B P2-20 入口闸门：缺 ffmpeg/ffprobe 必须在这里就非零退出。
    # _audio_duration() 会把 _ffprobe 的 SystemExit 吞成「按字数估算」，所以以前
    # 没装 ffprobe 的机器上会打一堆 ERROR 行、却以退出码 0 写出估算出来的字幕。
    _ensure_ffmpeg()
    timing = load_timing()
    out = _resolve_user_path(args.out)
    log(f"生成 SRT：{_rel(out)}")
    cues = build_cues(timing)
    _write_srt(cues, out)
    _print_cues(cues)

    # SRT 没有转义语法：文案里的字面 \N/\n/\h 在部分播放器/工具里会被当成换行指令
    # （ffmpeg 的 srt demuxer 实测就会把 \N 变成换行），看到的内容与 ASS/硬字幕不一致。
    # 脚本没法把字面反斜杠无损写进 SRT，只能明确提醒（R6-subs-04）。
    backslash_ids = [c.line_id for c in cues if re.search(r"\\[Nnh]", c.text)]
    if backslash_ids:
        warn(
            "SRT 不支持反斜杠转义：这些句子的 text 里有 \\N / \\n / \\h，部分播放器会把 \\N 当换行，"
            f"看到的字会和 ASS/硬字幕不一致（{'、'.join(backslash_ids)}）。"
            "要逐字一致请改文案（不要写反斜杠转义序列），或只用 ASS/硬字幕这条路"
        )

    estimated = [c for c in cues if c.vo_s is None]
    log("")
    log(f"SRT 完成：{len(cues)} 条 -> {_rel(out)}（{_file_stats(out)}）")
    if estimated:
        warn(
            f"有 {len(estimated)} 条字幕的时长是按字数估算的（缺 audio-src/vo 下的配音）。"
            "交付前请跑 `python scripts/tts.py all` 生成配音，再重跑本命令让时间轴对上真实人声"
        )
    log("下一步：python scripts/subtitles.py ass   # 生成可烧录的 ASS")
    return 0


def cmd_ass(args) -> int:
    # FIX-B P2-20 入口闸门：缺 ffmpeg/ffprobe 必须在这里就非零退出。
    # _audio_duration() 会把 _ffprobe 的 SystemExit 吞成「按字数估算」，所以以前
    # 没装 ffprobe 的机器上会打一堆 ERROR 行、却以退出码 0 写出估算出来的字幕。
    _ensure_ffmpeg()
    timing = load_timing()
    out = _resolve_user_path(args.out)
    if args.fontsize is not None and args.fontsize <= 0:
        error(f"--fontsize 必须是正数，收到 {args.fontsize}")
    log(f"生成 ASS：{_rel(out)}")
    cues = build_cues(timing)
    _build_ass(cues, timing, out, args.fontsize)
    _print_cues(cues)

    estimated = [c for c in cues if c.vo_s is None]
    log("")
    log(f"ASS 完成：{len(cues)} 条 -> {_rel(out)}（{_file_stats(out)}）")
    if estimated:
        warn(f"有 {len(estimated)} 条字幕的时长是按字数估算的（缺 audio-src/vo 下的配音）")
    log("下一步：python scripts/subtitles.py burn --preview 8   # 先烧 8 秒看效果")
    return 0


def _frame_time(cues: Sequence["Cue"], cover: float) -> float:
    """挑一个「字幕真的在屏上」的时刻抽帧。

    一开始这里取的是区间中点，结果 ``--preview 8`` 的中点是 4.0s，而第 01 句
    3.617s 就收掉了、第 02 句 5.0s 才出来 —— 抽出来的审图帧里根本没有字幕，
    等于白抽。所以改成「落在处理区间内、且开始得最早的那条字幕的中点」；
    一条都没有时才退回区间中点。下限抬到 0.3s 是为了避开片头淡入。
    """
    for cue in cues:
        if cue.start < cover and cue.end > 0:
            mid = cue.start + (min(cue.end, cover) - cue.start) * FRAME_AT_RATIO
            return max(mid, 0.3)
    return max(cover * FRAME_AT_RATIO, 0.3)


def cmd_burn(args) -> int:
    _ensure_ffmpeg()
    timing = load_timing()

    src = _resolve_user_path(args.video)
    if not src.is_file():
        error(
            f"找不到要烧字幕的成片 {src}。先跑 `npm run render:1080`"
            f"（输出 {DEFAULT_VIDEO}），或把正确的 mp4 路径作为第一个参数传进来"
        )
    if src.stat().st_size == 0:
        error(f"{src} 是空文件（0 字节），渲染没有真正完成，先重新渲染")

    dst = _resolve_user_path(args.out)
    # 预览默认别覆盖「正式成片」的名字：`--preview 8` 不小心写成默认输出，
    # out/final-1080p-hard.mp4 里就只有 8 秒，很容易被当成成品发出去。
    # 软字幕也一样：`--soft --preview 3` 不加判据就会把 3 秒的软字幕文件写成
    # out/final-1080p-hard.mp4（默认输出名），把真正的硬字幕成片覆盖掉。
    if args.preview and not _out_was_given():
        dst = dst.with_name(dst.stem + "-preview" + dst.suffix)
    if dst == src:
        error(f"输出路径和输入是同一个文件（{dst}），会覆盖原片。请用 -o 指定别的路径")

    # 输出目录不存在时 ffmpeg 只会报一句难懂的 "No such file or directory"（软字幕路径
    # 连滤镜脚本都不会写，于是更早翻车）：先建目录，建不出来给 ERROR 行（R5-subs-04）。
    try:
        dst.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        error(f"输出目录建不出来 {dst.parent}：{exc}（把 -o 指到可写目录，或先手工建好它）")

    if args.preview is not None and args.preview <= 0:
        error(f"--preview 必须是正数秒数，收到 {args.preview}")

    # ASS 不存在、或者比 timing.json 旧，就先自动生成（burn 的语义是「一条命令拿到带字幕的成片」）。
    #
    # 以前这里只判 ``not ass_path.is_file()``：改完稿、重跑 tts 之后磁盘上那份
    # out/subtitles/zh.ass 还是旧的，于是**照旧被烧进成片**（下面用的是 ass_path），
    # 退出码 0、一条提示都没有 —— 画面上的字幕和 timing.json 对不上，还找不到原因。
    # 现在按 mtime 判过期（同一份 src/timing.json 是唯一的字幕来源），过期就重建。
    ass_path = _resolve_user_path(args.ass)
    # 时间轴一律现算一遍：抽帧要挑「字幕在屏上」的时刻，不能依赖磁盘上那份 ASS 还新不新。
    cues = build_cues(timing)
    stamp = _source_stamp()
    if not ass_path.is_file():
        log(f"没有 {_rel(ass_path)}，先自动生成一份 ASS")
        _build_ass(cues, timing, ass_path, None)
        log(f"ASS 已生成：{_rel(ass_path)}（{_file_stats(ass_path)}）")
    elif _is_stale(ass_path, stamp):
        log(
            f"{_rel(ass_path)} 比 {_rel(TIMING_PATH)} 旧（改过稿），先重建再烧。"
            "注意判据只看 timing.json 的修改时间：句子的 at / 文字变了会重建，"
            "但只重跑过 tts、音频长短变了而 timing.json 没动，这里不会重建"
            "（那种情况见下面那条提醒）"
        )
        _build_ass(cues, timing, ass_path, None)
        log(f"ASS 已重建：{_rel(ass_path)}（{_file_stats(ass_path)}）")
    else:
        # 项目外的同名 ASS 是老坑：_resolve_user_path 以前按当前工作目录优先解析，
        # 站在别处跑就会把项目外那份「来源不明」的 ASS 烧进成片（FIX-B P2-3）。
        # 解析顺序已经改成项目根优先，这里再把「不在项目里」明说出来。
        outside = "" if _in_project(ass_path) else "注意：这份 ASS 不在项目里（是项目外的文件），来源不明；"
        warn(
            f"用的是磁盘上已有的 {_rel(ass_path)}：它比 {_rel(TIMING_PATH)} 新，"
            "但本命令不校验它的内容。" + outside
            + "改过字体、字幕参数（captions 字号 / bottom）"
            "或重跑过 tts（音频时长变了）之后，请显式跑 `python scripts/subtitles.py ass` 重生成，别直接 burn"
        )
    audio_note = _stale_audio_note(stamp)
    if audio_note:
        warn(audio_note)

    # 画面里已经有 <Caption> 渲染的字幕时，再烧一遍硬字幕会变成两行叠字 —— 实测过：
    # 两行糊在一起，哪一行都看不清（两者位置都由 captions.bottom 决定，必然重合）。
    captions_cfg = timing.get("captions") if isinstance(timing.get("captions"), dict) else {}
    if captions_cfg.get("enabled", True) and not args.soft:
        warn(
            "timing.json 的 captions.enabled=true：输入成片里已经带画面字幕，烧硬字幕后会两行重叠。"
            "正确做法是先渲染一版不带字幕的画面（npm run render:1080:nosubs，"
            "它带 --props=props-nosubs.json 把 withCaptions 关掉），再对那个文件跑 burn；"
            "或者把 timing.json 的 captions.enabled 临时改成 false 后重新渲染"
        )

    probe = _ffprobe(src)
    total = _num((probe.get("format") or {}).get("duration"), 0.0)
    if total <= 0:
        for stream in probe.get("streams") or []:
            total = _num(stream.get("duration"), 0.0)
            if total > 0:
                break
    if total <= 0:
        warn(f"ffprobe 没报出 {src.name} 的时长，--preview 的区间只能按 0~N 秒算")

    cover = min(total, float(args.preview)) if args.preview else total
    mode = "软字幕封装" if args.soft else "硬字幕烧录"
    log(
        f"{mode}：{_rel(src)} -> {_rel(dst)}"
        + (f"（--preview 只处理前 {cover:.2f}s / 全片 {total:.2f}s）" if args.preview else f"（全片 {total:.2f}s）")
    )
    log(f"字幕：{_rel(ass_path)}")

    # 参数顺序是有讲究的：ffmpeg 把选项绑到**它后面最近的那个文件**上，所以
    #   - 滤镜选项必须排在**所有** `-i` 之后，否则会被当成 ass 输入文件的选项，
    #     报 "Option vf ... cannot be applied to input url ...zh.ass"；
    #   - `-t` 对所有输出生效，放在输出文件之前即可。
    args_list: List[Any] = ["-i", str(src)]
    filter_script: Optional[Path] = None
    if args.soft:
        # 软字幕要把 ass 当**输入流**读（第 2 个输入），再用 mov_text 编码成字幕轨。
        # 注意 `-map` 是**输出**选项：它必须排在两个 `-i` **之后**，紧跟 ass 输入写
        # 会报 "Option map ... cannot be applied to input url ...zh.ass"。
        args_list += ["-i", str(ass_path)]
        if args.preview:
            # -t 放在输出选项区 = 对输出生效；预览时视频必须重编码（copy 不支持截断）。
            # 音频不用动：`-t` 只截时长，copy 出来的 AAC 包本来就是分段的，播放器按时间轴读，
            # 没必要为了 8 秒预览把整条音轨重编码一遍（慢且会引入又一次有损编码）。
            args_list += ["-t", f"{cover:.3f}"]
        args_list += ["-map", "0:v:0", "-map", "0:a?", "-map", "1:0",
                      "-c:v", "copy", "-c:a", "copy", "-c:s", "mov_text"]
        if args.preview:
            args_list += ["-c:v", "libx264", "-preset", "medium", "-crf", "20"]
    else:
        # 硬字幕：ass 是**滤镜**，不是输入文件。
        # 滤镜图走 `-filter_complex_script`（写进文件）而不是 `-vf "ass=..."`，
        # 这样路径里的反斜杠/引号都不经过滤镜解析器，见 _write_filter_script 的说明。
        args_list += ["-t", f"{cover:.3f}"] if args.preview else []
        filter_script = _write_filter_script(dst, ass_path)
        log(f"滤镜图：{_rel(filter_script)}（{filter_script.read_text(encoding='utf-8').strip()}）")
        args_list += [
            "-/filter_complex", str(filter_script),
            "-c:v", "libx264", "-preset", "medium", "-crf", "20", "-c:a", "copy",
        ]
    # 视频被重编码过就必须 +faststart（moov 前置），否则网页播放器要下完整段才能起播。
    if args.preview or not args.soft:
        args_list += ["-movflags", "+faststart"]
    args_list += [str(dst)]

    try:
        _run_ffmpeg(args_list, f"{mode} {dst.name}")
    finally:
        # 滤镜脚本是临时产物，成片烧完就没用了；失败也要清掉，免得 out/ 里留垃圾。
        if filter_script is not None and filter_script.is_file():
            try:
                filter_script.unlink()
            except OSError:
                pass

    if not dst.is_file() or dst.stat().st_size == 0:
        error(f"{mode}没有产生有效文件：{dst}")

    out_probe = _ffprobe(dst)
    out_dur = _num((out_probe.get("format") or {}).get("duration"), 0.0)
    audio_streams = [s for s in out_probe.get("streams") or [] if s.get("codec_type") == "audio"]
    subtitle_streams = [s for s in out_probe.get("streams") or [] if s.get("codec_type") == "subtitle"]

    log("")
    log(f"输出：{dst}")
    log(f"  体积 {human(dst.stat().st_size)}（{dst.stat().st_size} 字节） / 时长 {out_dur:.2f}s")
    if audio_streams:
        a = audio_streams[0]
        log(f"  音轨：有（{a.get('codec_name', '?')}，{a.get('sample_rate', '?')} Hz，{a.get('channels', '?')} 声道）")
    else:
        error(
            f"输出的 {dst.name} 没有音轨！硬字幕烧录用的是 `-c:a copy`，原片必须有音轨。"
            "请先用 `python scripts/mix.py` 生成混音并重新渲染（AGENTS.md 阶段 10：别把无声片当成品交出去）"
        )
    if args.soft:
        log(f"  字幕轨：{'有（mov_text）' if subtitle_streams else '没有 —— 封装失败'}")
    else:
        log("  字幕轨：无（已烧进画面，这是硬字幕的预期结果；播放器里关不掉）")

    # 抽一帧存 png：AI 看不到画面，但人（和审图闸门）需要一张能直接打开看的证据。
    verify_dir = PROJECT_ROOT / VERIFY_DIRNAME
    frame_time = _frame_time(cues, cover if cover > 0 else (args.preview or 8.0))
    frame_path = verify_dir / f"{Path(args.out).stem}-{frame_time:05.2f}s.png"
    try:
        verify_dir.mkdir(parents=True, exist_ok=True)
        _run_ffmpeg(
            ["-ss", f"{frame_time:.3f}", "-i", str(dst), "-frames:v", "1", "-y", str(frame_path)],
            f"抽取审图帧 {frame_time:.2f}s",
        )
    except SystemExit:
        frame_path = None  # type: ignore[assignment]
        warn("抽帧失败（不影响成片），可以手工跑 ffmpeg 抽帧自查")
    if frame_path is not None and frame_path.is_file():
        log(f"  审图帧：{_rel(frame_path)}（{frame_time:.2f}s，{human(frame_path.stat().st_size)}）")
        if frame_path.read_bytes()[:4] == b"\x89PNG":
            log("  （已确认是合法 PNG；请打开这张图核对字幕是否落在安全区内）")

    log("")
    log(f"复查：python scripts/check_video.py {_rel(dst)}")
    if args.preview:
        log(
            f"注意：这是 {cover:.2f}s 的预览片段，长度和 timing.duration 对不上，"
            "check_video.py 的时长闸门**会**报 ERROR —— 那不算问题，预览只用来眼检字幕位置。"
        )
    else:
        log("下一步：把上面那张审图帧逐张看过（字幕在安全区内、没有叠字、没有截字），再交付。")
    return 0


# ===========================================================================
# main
# ===========================================================================

# ===========================================================================
# 自测（--selftest / scripts/selftest.py 可挑）
# ===========================================================================
#
# 纯函数级自测：不碰 ffmpeg、不读 timing.json、不联网，任何机器与 CI 都能跑。
# 覆盖 Round 6 修过的回归点：ASS 文本转义、字号行高补偿、控制字符清洗、负 at/NaN、
# 纯空白 text、SRT 乱序排序、收边/跨场景、maxLines 硬闸门（正反两向都测）。

_SELFTEST_BASE_TIMING = {
    "slug": "selftest",
    "title": "自测",
    "fps": 30,
    "width": 1080,
    "height": 1920,
    "aspect": "9:16",
    "duration": 10.0,
    "scenes": [{"name": "main", "start": 0.0, "end": 10.0, "note": ""}],
    "voice": {"lines": []},
    "captions": {"enabled": True, "maxChars": 13, "maxLines": 2, "bottom": 0.18},
    "brand": {"name": "T", "url": "t.example", "logo": "",
              "colors": {"bg": "#000000", "fg": "#FFFFFF", "accent": "#33E1A1"}},
}


def _selftest() -> int:
    """跑纯函数自测；全部通过返回 0，任一失败打印 ERROR 行并返回 1。"""
    failures: List[str] = []
    passed = 0

    def check(name: str, got: Any, want: Any) -> None:
        nonlocal passed
        if got != want:
            failures.append(f"{name}: 期望 {want!r}，实际 {got!r}")
        else:
            passed += 1

    def expect_error(name: str, fn, needle: str) -> None:
        nonlocal passed
        buf_err = io.StringIO()
        try:
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(buf_err):
                fn()
        except SystemExit as exc:
            code = int(exc.code or 0)
            text = buf_err.getvalue()
            if code == 0:
                failures.append(f"{name}: 期望非零退出，实际 rc=0")
            elif needle not in text:
                failures.append(f"{name}: ERROR 文案里找不到 {needle!r}，实际 {text!r}")
            else:
                passed += 1
            return
        except Exception as exc:  # noqa: BLE001 - 自测里任何异常都算失败
            failures.append(f"{name}: 期望打印 ERROR 行，实际抛 {type(exc).__name__}: {exc}")
            return
        failures.append(f"{name}: 期望打印 ERROR 行，实际正常返回")

    def make_timing(lines, scenes=None, duration=10.0, captions=None):
        timing = json.loads(json.dumps(_SELFTEST_BASE_TIMING))
        timing["voice"]["lines"] = lines
        if scenes is not None:
            timing["scenes"] = scenes
        timing["duration"] = duration
        if captions is not None:
            timing["captions"] = captions
        return timing

    def one_line(at=1.0, text="测试一句", line_id="L1", scene="main"):
        return {"id": line_id, "scene": scene, "at": at, "text": text}

    def capture_cues(timing):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            cues = build_cues(timing)
        return cues, out.getvalue()

    originals: Dict[str, Any] = {}

    def patch(name: str, value: Any) -> None:
        if name not in originals:
            originals[name] = globals()[name]
        globals()[name] = value

    try:
        with tempfile.TemporaryDirectory(prefix="subtitles-selftest-") as tmp:
            root = Path(tmp)
            (root / "audio-src" / "vo").mkdir(parents=True, exist_ok=True)
            # 这个进程里把项目根指到临时目录：不读真实 audio-src/vo，也不写项目文件。
            patch("PROJECT_ROOT", root)
            patch("fonts_config", lambda: {"roles": {}, "brand": {}})
            patch("_resolve_font_family", lambda cfg, text="": "TestFont")

            # --- 1) ASS 文本转义（R6-subs-02：{ } \N \h 会被 libass 当语义字符） ---
            check("_escape_ass_text 花括号", _escape_ass_text("{over} ABCD"), r"\{over\} ABCD")
            check("_escape_ass_text 反斜杠插零宽空格",
                  _escape_ass_text("C:\\new"), "C:\\" + ASS_ZWSP + "new")
            check("_escape_ass_text 不再留下字面 \\N",
                  "\\N" in _escape_ass_text("AB\\NCD"), False)

            # --- 2) ASS 样式与字号行高补偿（R6-subs-06） ---
            patch("_font_height_ratio", lambda family: 1.3198)
            timing = make_timing([one_line(at=1.0, text="{over} ABCD")])
            cues, _ = capture_cues(timing)
            ass_path = root / "out" / "a.ass"

            def build_ass(ass_file=None):
                with contextlib.redirect_stdout(io.StringIO()), \
                        contextlib.redirect_stderr(io.StringIO()):
                    return _build_ass(cues, timing, ass_file or ass_path, None)

            ass_text = build_ass()
            style = next(line for line in ass_text.splitlines() if line.startswith("Style: "))
            fields = style[len("Style: "):].split(",")
            check("ASS Style 字段数", len(fields), 23)
            check("ASS 事件行含转义花括号", r"\{over\}" in ass_text, True)
            check("ASS 事件行无裸 {over}", "{over}" in ass_text.split("Dialogue: ")[-1], False)
            check("ASS 字号补偿 66em×1.3198", fields[2].strip(), "87.1")
            patch("_font_height_ratio", lambda family: None)
            style2 = next(line for line in build_ass().splitlines()
                          if line.startswith("Style: "))
            check("ASS 量不出比例不补偿", style2[len("Style: "):].split(",")[2].strip(), "66")

            # --- 3) 控制字符清洗（R6-subs-01：NUL/BEL 会让 SRT/ASS 整份读不出来） ---
            check("_clean_text 剥 NUL/BEL/ESC", _clean_text("a\x00b\x07c\x1bd"), "abcd")
            cues, warnings = capture_cues(make_timing([one_line(text="a\x00b\x07c")]))
            check("cue 文本已剥控制字符", cues[0].text, "abc")
            check("控制字符会 WARN", "控制字符" in warnings, True)

            # --- 4) at 合法性（R6-subs-02：负 / NaN 不许静默变零时长字幕） ---
            expect_error("负 at 报错", lambda: build_cues(make_timing([one_line(at=-5.0)])), "不合法")
            expect_error("NaN at 报错",
                         lambda: build_cues(make_timing([one_line(at=float("nan"))])), "不合法")

            # --- 5) 纯空白 / 缺 text（R6-subs-05：文案要能指出真实原因） ---
            expect_error("纯空白 text 报错",
                         lambda: build_cues(make_timing(
                             [{"id": "W1", "scene": "main", "at": 1.0, "text": "\u3000\u3000"}])),
                         "只有空白字符")
            expect_error("缺 text 字段报错",
                         lambda: build_cues(make_timing([{"id": "W2", "scene": "main", "at": 1.0}])),
                         "没有 text 字段")

            # --- 6) SRT 乱序排序（R6-subs-03：时间轴必须单调） ---
            cues, _ = capture_cues(make_timing([
                one_line(at=8.0, text="八", line_id="A"),
                one_line(at=2.0, text="二", line_id="B"),
                one_line(at=5.0, text="五", line_id="C"),
            ]))
            srt_path = root / "out" / "b.srt"
            _write_srt(cues, srt_path)
            srt_text = srt_path.read_text(encoding="utf-8")
            times = re.findall(r"(\d\d:\d\d:\d\d,\d{3}) --> ", srt_text)
            check("SRT 时间轴单调不降", times, sorted(times))

            # --- 7) 收边 / 跨场景（R5 修过、R6 不许回归） ---
            scenes = [{"name": "s1", "start": 0.0, "end": 5.0, "note": ""},
                      {"name": "s2", "start": 5.0, "end": 10.0, "note": ""}]
            cues, _ = capture_cues(make_timing(
                [one_line(at=4.9, text="一二三四五六七八九十一二三", line_id="E1", scene="s1"),
                 one_line(at=5.05, text="下一句", line_id="E2", scene="s2")],
                scenes=scenes))
            check("收边终点不越过场景末端", cues[0].end <= 5.0, True)
            check("收边终点不压到下一句", cues[0].end <= cues[1].start, True)
            expect_error("场景内没位置时 ERROR",
                         lambda: build_cues(make_timing([one_line(at=12.0)])), "没有位置写字幕")

            # --- 8) 折行硬闸门（maxLines 是硬要求，正反两向都测） ---
            expect_error("maxLines=1 超行 ERROR",
                         lambda: build_cues(make_timing(
                             [one_line(text="一二三四五六七八九十一二三四五六七")],
                             captions={"enabled": True, "maxChars": 13, "maxLines": 1,
                                       "bottom": 0.18})),
                         "超过 captions.maxLines=1")
            expect_error("maxLines=0 直接 ERROR",
                         lambda: build_cues(make_timing(
                             [one_line()],
                             captions={"enabled": True, "maxChars": 13, "maxLines": 0,
                                       "bottom": 0.18})),
                         "不合法：必须是正整数")
    finally:
        for name, value in originals.items():
            globals()[name] = value

    if failures:
        for message in failures:
            sys.stderr.write("ERROR: selftest — %s\n" % message)
        sys.stderr.write("ERROR: selftest — %d 项失败\n" % len(failures))
        return 1
    print("OK: selftest — %d 项断言通过（ASS 转义 / 字号补偿 / 控制字符 / at 合法性 / "
          "空白 text / SRT 排序 / 收边跨场景 / maxLines 硬闸门）" % passed)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="subtitles.py",
        description="promo-forge 字幕生成与烧录（AGENTS.md 阶段 7）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "例：\n"
            "  python scripts/subtitles.py srt\n"
            "  python scripts/subtitles.py srt --out out/subtitles/zh.srt\n"
            "  python scripts/subtitles.py ass --fontsize 76\n"
            "  python scripts/subtitles.py burn --preview 8\n"
            "  python scripts/subtitles.py burn out/final-1080p.mp4 -o out/final-1080p-hard.mp4\n"
            "  python scripts/subtitles.py burn --soft        # 软字幕封装，播放器可关\n"
            "\n"
            "时间轴口径：起点取 timing.json 的 voice.lines[].at，终点 = 起点 + 实测配音时长\n"
            "（不足则用「字数/6+0.35」秒兜底），且不越过该句所属场景的窗口末端。\n"
            "退出码：0 成功（含按字数估算的 WARN）；1 有 ERROR（缺 ffmpeg / 无旁白稿 / 写不出文件 / 烧录失败）。\n"
            "纯函数自测（不需要 ffmpeg/网络）：python scripts/subtitles.py --selftest"
        ),
    )
    parser.add_argument("--selftest", action="store_true",
                        help="只跑纯函数自测（不读 timing.json、不需要 ffmpeg/网络）")
    sub = parser.add_subparsers(dest="cmd", metavar="<子命令>")

    p_srt = sub.add_parser("srt", help="生成 SRT 字幕（时间轴用真实配音时长）")
    p_srt.add_argument("--out", default=DEFAULT_SRT, help=f"输出路径（默认 {DEFAULT_SRT}）")
    p_srt.set_defaults(func=cmd_srt)

    p_ass = sub.add_parser("ass", help="生成 ASS 字幕（含样式与安全区换算，可直接烧录）")
    p_ass.add_argument("--out", default=DEFAULT_ASS, help=f"输出路径（默认 {DEFAULT_ASS}）")
    p_ass.add_argument("--fontsize", type=float, default=None,
                       help=f"覆盖字号；默认按画布高度换算（高 × {FONT_SCALE}）")
    p_ass.set_defaults(func=cmd_ass)

    p_burn = sub.add_parser("burn", help="把 ASS 烧进成片（默认硬字幕，--soft 走软字幕封装）")
    p_burn.add_argument("video", nargs="?", default=DEFAULT_VIDEO,
                        help=f"输入成片（默认 {DEFAULT_VIDEO}）")
    p_burn.add_argument("-o", "--out", default=DEFAULT_OUT_VIDEO,
                        help=f"输出路径（默认 {DEFAULT_OUT_VIDEO}）")
    p_burn.add_argument("--soft", action="store_true",
                        help="软字幕封装（-c:s mov_text，播放器可开关）；默认硬字幕烧进画面")
    p_burn.add_argument("--preview", type=float, default=None, metavar="秒",
                        help="只处理前 N 秒，用于快速验证效果（例如 --preview 8）")
    p_burn.add_argument("--ass", default=DEFAULT_ASS,
                        help=f"要烧的字幕文件；不存在会先自动生成（默认 {DEFAULT_ASS}）")
    p_burn.set_defaults(func=cmd_burn)

    return parser


def main(argv=None) -> int:
    _force_utf8()
    parser = build_parser()
    args = parser.parse_args(argv)
    if getattr(args, "selftest", False):
        return _selftest()
    if not getattr(args, "cmd", None):
        # 不带子命令 = 只打印帮助（AGENTS.md 第 7 节：不要静默通过，但这不是错误）。
        parser.print_help()
        log("")
        log("提示：最常用的两条是 `subtitles.py ass` 与 `subtitles.py burn --preview 8`。")
        return 0
    try:
        return int(args.func(args) or 0)
    except KeyboardInterrupt:
        sys.stderr.write("ERROR: 用户中断\n")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
