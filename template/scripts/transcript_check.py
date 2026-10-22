#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""promo-forge · 阶段 9「配音与字幕反查」（transcript_check.py）。

为什么要有这个脚本
------------------
AGENTS.md 阶段 7 要求「字幕必须活到人声读完」，但**字幕文本和配音文本对不对得上**
一直是人肉核对：TTS 念的是 ``voice.lines[].text``，字幕也是它，可一旦有人改错了某一句、
换了音色重配音却忘了同步字幕、或者（更常见的）成片剪掉了某句旁白，画面与声音就会错位，
而所有闸门都是绿的。

竞品 MoneyPrinterTurbo 的解法是 **STT 反查**：用语音识别把成片重新转写成文字，再和字幕源
逐句比对。本脚本是这个思路的**零依赖、可离线**替代：

* 不需要付费 ASR（不引入 whisper / 云 API），符合本仓库「零付费默认」的路线；
* 用户提供一份 transcript（``txt`` 逐行，或平台导出的 ``srt``），由脚本做交叉校验；
* 以后想接真 STT，只要把识别结果写成同样的 txt / srt 即可，本脚本不用改。

校验项
------
1. **行覆盖率**：``timing.json`` 的每个 ``voice.lines[]``（或已生成字幕文件的每条 cue）
   都要在 transcript 里找到对应行；缺一行 = ERROR（漏念 / 漏字幕 / 成片剪掉了）。
2. **顺序**：语音行与转写行的顺序必须一致；对不上（错位）报 ERROR（声画会错位）。
3. **字符相似度**：用标准库 ``difflib.SequenceMatcher`` 算每行相似度，低于 ``--threshold``
   = ERROR；**字符覆盖率**（匹配上的字符数 / 参考行总字符数）低于 ``--min-coverage`` = ERROR
   （防「整行只差两个字」被行相似度放过去）。
4. **纯风格差异**：标点、全角/半角、空白、大小写这些归一化后相同的差异只算 WARN，不拦发布。
5. **品牌名 / 网址逐字一致**：参考行里出现 ``timing.json`` 的 ``brand.name`` / ``brand.url``
   （或 ``--brand`` 追加的词）时，对应转写行必须包含同一个词 —— 错一个字就是发布事故。
6. **多余转写行**：transcript 里有、参考源里没有的行 = WARN（STT 幻觉 / 多念了一句）。

用法
----
    python scripts/transcript_check.py transcript.txt                # 对 timing.json 的 voice.lines
    python scripts/transcript_check.py transcript.srt               # 自动识别 SRT / 纯文本
    python scripts/transcript_check.py transcript.txt --ref srt     # 对 out/subtitles/zh.srt 交叉校验
    python scripts/transcript_check.py stt.txt --min-coverage 0.9 --threshold 0.75
    python scripts/transcript_check.py transcript.txt --json
    python scripts/transcript_check.py --selftest                   # 纯函数自测（不读项目素材）

退出码
------
0 = 0 ERROR（WARN 可接受）；1 = 有 ERROR 或输入缺失 / 格式不对。

依赖
----
**纯标准库、零新增依赖、不联网**：``difflib`` + ``json`` + ``re``。不调用 ffmpeg / STT 服务。
"""
from __future__ import annotations

import argparse
import difflib
import json
import math
import re
import sys
import unicodedata
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

SCRIPTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS_DIR))
try:  # pragma: no cover - 只在 _common.py 缺失时走到
    from _common import PROJECT_ROOT  # type: ignore
except ImportError:  # pragma: no cover
    PROJECT_ROOT = SCRIPTS_DIR.parent
    sys.stderr.write(
        "WARN: 读不到 scripts/_common.py，改用最小内置实现继续。\n"
        "      请确认 scripts 目录完整。\n"
    )

DEFAULT_TIMING = "src/timing.json"
DEFAULT_SRT = "out/subtitles/zh.srt"
#: 行相似度阈值：低于它算「对不上」。中文短视频一句 10~20 字，错 1~2 个字 ≈ 0.90~0.95。
DEFAULT_THRESHOLD = 0.85
#: 字符覆盖率下限：匹配上的参考字符数 / 参考字符总数。默认 1.0 = 必须逐字对上
#: （用户按脚本提供的 transcript / 已校对 STT 应该 100%）。未校对 STT 可放宽到 0.9。
DEFAULT_MIN_COVERAGE = 1.0
#: 参考行里找品牌词时，最多把这么多个候选词拿去匹配（brand.name / brand.url / --brand）。
_SRT_TIME_RE = re.compile(
    r"(\d{1,2}):(\d{2}):(\d{2})[,.](\d{1,3})\s*-->\s*(\d{1,2}):(\d{2}):(\d{2})[,.](\d{1,3})")


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------
def _force_utf8() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):
                pass


def _finite(value: Any) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    return math.isfinite(float(value))


def _problem(level: str, message: str) -> Dict[str, str]:
    return {"level": level, "message": message}


def normalize_text(text: str) -> str:
    """归一化：NFKC（全角→半角）、大小写折叠，只保留字母 / 数字 / 汉字。

    标点、空白、连字符全部丢掉 —— 「每天记账要花 10 分钟！」与「每天记账要花10分钟」
    归一化后完全相同，于是纯风格差异不会被误判成内容错误。
    """
    out: List[str] = []
    for char in unicodedata.normalize("NFKC", str(text)).casefold():
        if char.isalnum() or unicodedata.category(char).startswith(("L", "N")):
            out.append(char)
    return "".join(out)


def style_compare_text(text: str) -> str:
    """只去空白 / 统一大小写的对比串：用来识别「只差标点/全半角」的纯风格差异。"""
    return "".join(unicodedata.normalize("NFKC", str(text)).casefold().split())


def similarity(reference: str, transcript: str) -> float:
    """两行归一化文本的相似度 0~1（difflib.SequenceMatcher，按字符）。"""
    if not reference and not transcript:
        return 1.0
    if not reference or not transcript:
        return 0.0
    return difflib.SequenceMatcher(None, reference, transcript, autojunk=False).ratio()


def matched_chars(reference: str, transcript: str) -> int:
    """两行归一化文本里匹配上的参考字符数（用于字符覆盖率）。"""
    if not reference or not transcript:
        return 0
    matcher = difflib.SequenceMatcher(None, reference, transcript, autojunk=False)
    return sum(block.size for block in matcher.get_matching_blocks())


def _parse_srt_clock(hours: str, minutes: str, seconds: str, millis: str) -> float:
    return (int(hours) * 3600 + int(minutes) * 60 + int(seconds)
            + int(millis.ljust(3, "0")) / 1000.0)


# ---------------------------------------------------------------------------
# 读输入：transcript（txt / srt）与参考源（timing.json / srt）
# ---------------------------------------------------------------------------
def read_text_file(path: Path) -> str:
    try:
        with open(str(path), "r", encoding="utf-8-sig") as handle:
            return handle.read()
    except OSError as exc:
        raise ValueError("读不了 %s：%s" % (path, exc))
    except UnicodeDecodeError as exc:
        raise ValueError("%s 不是 UTF-8 编码（%s）：另存为 UTF-8 再试" % (path, exc))


def parse_srt(text: str) -> List[Dict[str, Any]]:
    """解析 SRT；格式坏了抛 ValueError（带行号），不静默跳过。"""
    lines = text.splitlines()
    cues: List[Dict[str, Any]] = []
    index = 0
    while index < len(lines):
        if not lines[index].strip():
            index += 1
            continue
        block_start = index
        block: List[str] = []
        while index < len(lines) and lines[index].strip():
            block.append(lines[index])
            index += 1
        time_at = None
        for offset, line in enumerate(block):
            if "-->" in line:
                time_at = offset
                break
        if time_at is None:
            raise ValueError("SRT 第 %d 行开始的字幕块缺少 `-->` 时间行（不是合法 SRT？）"
                             % (block_start + 1,))
        match = _SRT_TIME_RE.search(block[time_at])
        if not match:
            raise ValueError("SRT 第 %d 行的时间戳格式不对：%r（应为 00:00:01,000 --> 00:00:03,000）"
                             % (block_start + time_at + 1, block[time_at]))
        start = _parse_srt_clock(*match.group(1, 2, 3, 4))
        end = _parse_srt_clock(*match.group(5, 6, 7, 8))
        if end < start:
            raise ValueError("SRT 第 %d 行的结束时间早于开始时间：%r"
                             % (block_start + time_at + 1, block[time_at]))
        text_lines = [line.strip() for line in block[time_at + 1:] if line.strip()]
        if not text_lines:
            raise ValueError("SRT 第 %d 行开始的字幕块没有文本" % (block_start + 1,))
        cues.append({"index": len(cues) + 1, "start": start, "end": end,
                     "text": " ".join(text_lines)})
    if not cues:
        raise ValueError("SRT 里没有解析出任何字幕块")
    return cues


def parse_txt(text: str) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        rows.append({"index": len(rows) + 1, "start": None, "end": None, "text": stripped})
    return rows


def detect_format(path: Path, text: str) -> str:
    """自动识别 txt / srt：有 --> 时间行就按 SRT 解析。"""
    if re.search(r"-->\s*\d{1,2}:\d{2}:\d{2}", text):
        return "srt"
    return "txt"


def read_transcript(path: Path, fmt: str = "auto") -> Tuple[List[Dict[str, Any]], str]:
    text = read_text_file(path)
    if not text.strip():
        raise ValueError("transcript 是空文件：%s" % (path,))
    actual = fmt
    if fmt == "auto":
        actual = detect_format(path, text)
    if actual == "srt":
        rows = parse_srt(text)
    elif actual == "txt":
        rows = parse_txt(text)
    else:
        raise ValueError("--format 只能是 auto / txt / srt，现在是 %r" % (fmt,))
    if not rows:
        raise ValueError("transcript 里没有可用的行：%s" % (path,))
    return rows, actual


def read_reference_timing(timing: Dict[str, Any], path: Path) -> List[Dict[str, Any]]:
    """从 timing.json 的 voice.lines 取参考行（按 at 排序；缺 at 保持原顺序）。"""
    voice = timing.get("voice")
    if not isinstance(voice, dict):
        raise ValueError("%s 缺少 voice 对象：参考源只能是 timing.json 的 voice.lines 或 SRT" % (path,))
    lines = voice.get("lines")
    if not isinstance(lines, list) or not lines:
        raise ValueError("%s 的 voice.lines 必须是非空数组" % (path,))
    rows: List[Dict[str, Any]] = []
    for index, line in enumerate(lines):
        if not isinstance(line, dict):
            raise ValueError("voice.lines[%d] 必须是对象，现在是 %s"
                             % (index, type(line).__name__))
        text = line.get("text")
        if not isinstance(text, str) or not text.strip():
            raise ValueError("voice.lines[%d].text 必须是非空字符串（现在是 %r）"
                             % (index, text))
        at = line.get("at")
        rows.append({
            "index": index + 1,
            "id": line.get("id"),
            "at": float(at) if _finite(at) else None,
            "text": text.strip(),
        })
    rows.sort(key=lambda row: (row["at"] is None, row["at"] if row["at"] is not None else 0.0,
                               row["index"]))
    return rows


def read_reference_srt(path: Path) -> List[Dict[str, Any]]:
    cues = parse_srt(read_text_file(path))
    return [{"index": cue["index"], "id": None, "at": cue["start"], "text": cue["text"]}
            for cue in cues]


def _read_timing(path: Path) -> Dict[str, Any]:
    try:
        with open(str(path), "r", encoding="utf-8-sig") as handle:
            data = json.load(handle)
    except OSError as exc:
        raise ValueError("读不了 %s：%s" % (path, exc))
    except ValueError as exc:
        raise ValueError("%s 不是合法 JSON：%s" % (path, exc))
    if not isinstance(data, dict):
        raise ValueError("%s 顶层必须是 JSON 对象" % (path,))
    return data


# ---------------------------------------------------------------------------
# 对齐
# ---------------------------------------------------------------------------
def align_lines(refs: Sequence[Dict[str, Any]], transcripts: Sequence[Dict[str, Any]],
                threshold: float) -> Tuple[List[Dict[str, Any]], List[int], List[List[float]]]:
    """保序对齐（允许两边各自跳过若干行），返回 (逐行结果, 未匹配的转写行下标, 相似度矩阵)。

    用动态规划最大化「匹配上的相似度之和」；跳行不罚分，因此不会为了凑对齐把
    完全不相干的两行硬配在一起（它们的相似度接近 0，加了也不涨分）。
    """
    n, m = len(refs), len(transcripts)
    sims: List[List[float]] = [
        [similarity(normalize_text(ref["text"]), normalize_text(trans["text"])) for trans in transcripts]
        for ref in refs
    ]
    dp = [[0.0] * (m + 1) for _ in range(n + 1)]
    move = [[0] * (m + 1) for _ in range(n + 1)]  # 0=skip_ref 1=skip_trans 2=match
    for i in range(1, n + 1):
        dp[i][0] = dp[i - 1][0]
        move[i][0] = 0
    for j in range(1, m + 1):
        dp[0][j] = dp[0][j - 1]
        move[0][j] = 1
    for i in range(1, n + 1):
        for j in range(1, m + 1):
            best = dp[i - 1][j]
            best_move = 0
            if dp[i][j - 1] > best + 1e-12:
                best = dp[i][j - 1]
                best_move = 1
            score = sims[i - 1][j - 1]
            if score > 0.0 and dp[i - 1][j - 1] + score >= best - 1e-12:
                best = dp[i - 1][j - 1] + score
                best_move = 2
            dp[i][j] = best
            move[i][j] = best_move

    pairs: List[Tuple[int, int]] = []
    i, j = n, m
    while i > 0 or j > 0:
        kind = move[i][j]
        if kind == 2:
            pairs.append((i - 1, j - 1))
            i -= 1
            j -= 1
        elif kind == 1:
            j -= 1
        else:
            i -= 1
    pairs.reverse()
    matched_j = {pair[1] for pair in pairs}
    extra = [index for index in range(m) if index not in matched_j]

    rows: List[Dict[str, Any]] = []
    for i in range(n):
        ref = refs[i]
        row: Dict[str, Any] = {
            "ref_index": i,
            "ref_id": ref.get("id"),
            "ref_at": ref.get("at"),
            "ref_text": ref["text"],
            "transcript_index": None,
            "transcript_text": None,
            "similarity": None,
            "matched_chars": 0,
            "status": "missing",
        }
        for pair_i, pair_j in pairs:
            if pair_i != i:
                continue
            trans = transcripts[pair_j]
            ref_norm = normalize_text(ref["text"])
            trans_norm = normalize_text(trans["text"])
            sim = similarity(ref_norm, trans_norm)
            row.update({
                "transcript_index": pair_j,
                "transcript_text": trans["text"],
                "similarity": round(sim, 4),
                "matched_chars": matched_chars(ref_norm, trans_norm),
                "status": "match" if sim >= threshold else "low",
            })
            break
        rows.append(row)
    return rows, extra, sims


# ---------------------------------------------------------------------------
# 主检查
# ---------------------------------------------------------------------------
def _finalize(result: Dict[str, Any]) -> Dict[str, Any]:
    """补齐 stats 计数（早退分支也要有 errors/warnings，CLI 才不会打「0 ERROR」）。"""
    problems = result.get("problems") or []
    stats = result.setdefault("stats", {})
    for key in ("ref_lines", "transcript_lines", "matched_lines", "missing_lines",
                "low_lines", "extra_lines", "ref_chars", "matched_chars"):
        stats.setdefault(key, 0)
    stats["errors"] = sum(1 for item in problems if item.get("level") == "ERROR")
    stats["warnings"] = sum(1 for item in problems if item.get("level") == "WARN")
    result["ok"] = stats["errors"] == 0
    return result


def check_transcript(
    transcript_path: Path,
    ref_kind: str = "timing",
    project_root: Optional[Path] = None,
    timing: Optional[Dict[str, Any]] = None,
    timing_path: Optional[Path] = None,
    srt_path: Optional[Path] = None,
    threshold: float = DEFAULT_THRESHOLD,
    min_coverage: float = DEFAULT_MIN_COVERAGE,
    brand_strict: bool = True,
    extra_brands: Sequence[str] = (),
    transcript_format: str = "auto",
) -> Dict[str, Any]:
    """交叉校验 transcript 与字幕源，返回 {ok, coverage, lines, problems, ...}。"""
    project_root = Path(project_root) if project_root is not None else Path(PROJECT_ROOT)
    transcript_path = Path(transcript_path)
    problems: List[Dict[str, str]] = []
    result: Dict[str, Any] = {
        "ok": False,
        "transcript": str(transcript_path),
        "ref": {"kind": ref_kind, "path": None, "lines": 0},
        "threshold": threshold,
        "min_coverage": min_coverage,
        "coverage": None,
        "line_coverage": None,
        "mean_similarity": None,
        "lines": [],
        "extra_lines": [],
        "problems": problems,
        "stats": {},
    }

    if not (0.0 < threshold <= 1.0):
        problems.append(_problem("ERROR", "--threshold 必须在 (0, 1] 之间，现在是 %r" % (threshold,)))
        return _finalize(result)
    if not (0.0 <= min_coverage <= 1.0):
        problems.append(_problem("ERROR", "--min-coverage 必须在 0~1 之间，现在是 %r"
                                 % (min_coverage,)))
        return _finalize(result)
    if not transcript_path.is_file():
        problems.append(_problem("ERROR", "找不到 transcript 文件：%s" % (transcript_path,)))
        return _finalize(result)

    try:
        transcripts, actual_fmt = read_transcript(transcript_path, transcript_format)
    except ValueError as exc:
        problems.append(_problem("ERROR", str(exc)))
        return _finalize(result)
    result["stats"]["transcript_format"] = actual_fmt

    if ref_kind == "srt" and timing is None:
        # --ref srt 只换「参考行」的来源，品牌词仍应来自 timing.brand.name/url；
        # 以前这里直接跳过，导致 --ref srt 时品牌错字能静默通过（文档承诺逐字校验）。
        brand_timing_path = (Path(timing_path) if timing_path is not None
                             else project_root / DEFAULT_TIMING)
        if brand_timing_path.is_file():
            try:
                timing = _read_timing(brand_timing_path)
                timing_path = brand_timing_path
            except ValueError as exc:
                problems.append(_problem(
                    "WARN", "读了 timing.json 想核验品牌词，但解析失败，跳过品牌词校验：%s" % (exc,)))

    try:
        if ref_kind == "timing":
            if timing is None:
                timing_path = timing_path or (project_root / DEFAULT_TIMING)
                timing = _read_timing(Path(timing_path))
            else:
                timing_path = Path(timing_path) if timing_path else None
            refs = read_reference_timing(timing, Path(timing_path) if timing_path else Path("<timing.json>"))
            result["ref"]["path"] = str(timing_path) if timing_path else None
        elif ref_kind == "srt":
            srt_path = Path(srt_path) if srt_path else (project_root / DEFAULT_SRT)
            refs = read_reference_srt(srt_path)
            result["ref"]["path"] = str(srt_path)
        else:
            raise ValueError("--ref 只能是 timing / srt，现在是 %r" % (ref_kind,))
    except ValueError as exc:
        problems.append(_problem("ERROR", str(exc)))
        return _finalize(result)
    result["ref"]["lines"] = len(refs)

    ref_norms = [normalize_text(ref["text"]) for ref in refs]
    trans_norms = [normalize_text(trans["text"]) for trans in transcripts]

    rows, extra, sims = align_lines(refs, transcripts, threshold)
    result["lines"] = rows
    total_ref_chars = sum(len(text) for text in ref_norms)
    total_matched = sum(row["matched_chars"] for row in rows)
    coverage = (total_matched / float(total_ref_chars)) if total_ref_chars else 1.0
    line_coverage = (sum(1 for row in rows if row["status"] == "match") / float(len(rows))
                     if rows else 1.0)
    result["coverage"] = round(coverage, 6)
    result["line_coverage"] = round(line_coverage, 6)
    matched_sims = [row["similarity"] for row in rows
                    if row["similarity"] is not None and row["status"] == "match"]
    result["mean_similarity"] = (round(sum(matched_sims) / len(matched_sims), 4)
                                 if matched_sims else None)

    # 1) 漏行 / 低相似度 / 纯风格差异
    for row in rows:
        label = "第 %d 句（%s）" % (row["ref_index"] + 1, row["ref_text"])
        if row["status"] == "missing":
            best_j = max(range(len(transcripts)), key=lambda j: sims[row["ref_index"]][j]) \
                if transcripts else None
            best_sim = sims[row["ref_index"]][best_j] if best_j is not None else 0.0
            used_by = _who_uses(rows, best_j) if best_j is not None else None
            if (best_j is not None and best_sim >= threshold and used_by is not None
                    and used_by != row["ref_index"]):
                problems.append(_problem(
                    "ERROR", "%s 顺序错位：它最像转写第 %d 行（相似度 %.2f），但那一行被参考"
                    "第 %d 句占用了 —— 声画会错位，检查 transcript 顺序或配音/字幕是否剪过"
                    % (label, best_j + 1, best_sim, used_by + 1)))
            else:
                problems.append(_problem(
                    "ERROR", "%s 在 transcript 里没有对应行（漏念 / 漏字幕 / 成片剪掉了）"
                    % (label,)))
        elif row["status"] == "low":
            problems.append(_problem(
                "ERROR", "%s 相似度 %.2f < 阈值 %.2f：转写是「%s」，内容对不上"
                % (label, float(row["similarity"]), threshold, row["transcript_text"])))
        else:
            ref_raw = style_compare_text(refs[row["ref_index"]]["text"])
            trans_raw = style_compare_text(transcripts[row["transcript_index"]]["text"])
            if ref_raw != trans_raw:
                problems.append(_problem(
                    "WARN", "%s 只差标点 / 全半角 / 空白（相似度 %.2f）：属纯风格差异，不拦发布"
                    % (label, float(row["similarity"]))))

    # 2) 字符覆盖率（防「整行差两个字」被行相似度放过）
    if coverage < min_coverage - 1e-9:
        problems.append(_problem(
            "ERROR", "字符覆盖率 %.1f%% < 要求 %.1f%%（参考共 %d 字，只匹配上 %d 字）："
            "有字没被念 / 转写漏字，字幕与配音对不上"
            % (coverage * 100.0, min_coverage * 100.0, total_ref_chars, total_matched)))

    # 3) 多余转写行 = WARN（STT 幻觉 / 多念）
    result["extra_lines"] = [transcripts[index]["text"] for index in extra]
    for index in extra:
        problems.append(_problem(
            "WARN", "transcript 第 %d 行「%s」在参考字幕源里没有对应行（多余/幻觉，"
            "人工确认是否需要删）" % (index + 1, transcripts[index]["text"])))

    # 4) 顺序错位（只看强匹配）：best j 是否单调递增
    if len(rows) >= 2 and transcripts:
        best_pairs = []
        for row in rows:
            i = row["ref_index"]
            best_j = max(range(len(transcripts)), key=lambda j: sims[i][j])
            if sims[i][best_j] >= threshold:
                best_pairs.append((i, best_j))
        for (i1, j1), (i2, j2) in zip(best_pairs, best_pairs[1:]):
            if j2 < j1:
                problems.append(_problem(
                    "ERROR", "顺序错位：参考第 %d 句最像转写第 %d 行，但参考第 %d 句最像更靠前的"
                    "第 %d 行 —— 配音与字幕顺序不一致" % (i1 + 1, j1 + 1, i2 + 1, j2 + 1)))
                break

    # 5) 品牌名 / 网址逐字一致
    brand_tokens = _brand_tokens(timing, extra_brands)
    result["stats"]["brand_tokens"] = brand_tokens
    if brand_strict and brand_tokens:
        for row in rows:
            if row["status"] == "missing":
                continue
            ref_norm = ref_norms[row["ref_index"]]
            hit = [token for token in brand_tokens if token and token in ref_norm]
            if not hit:
                continue
            trans_norm = normalize_text(transcripts[row["transcript_index"]]["text"])
            missing = [token for token in hit if token not in trans_norm]
            if missing:
                problems.append(_problem(
                    "ERROR", "品牌名 / 网址对不上（错一个字就是发布事故）：参考第 %d 句应包含"
                    "「%s」，转写是「%s」"
                    % (row["ref_index"] + 1, "、".join(missing), row["transcript_text"])))

    errors = sum(1 for item in problems if item["level"] == "ERROR")
    result["ok"] = errors == 0
    result["stats"].update({
        "ref_lines": len(refs),
        "transcript_lines": len(transcripts),
        "matched_lines": sum(1 for row in rows if row["status"] == "match"),
        "missing_lines": sum(1 for row in rows if row["status"] == "missing"),
        "low_lines": sum(1 for row in rows if row["status"] == "low"),
        "extra_lines": len(extra),
        "ref_chars": total_ref_chars,
        "matched_chars": total_matched,
        "errors": errors,
        "warnings": sum(1 for item in problems if item["level"] == "WARN"),
    })
    return _finalize(result)


def _who_uses(rows: Sequence[Dict[str, Any]], trans_index: Optional[int]) -> Optional[int]:
    if trans_index is None:
        return None
    for row in rows:
        if row["transcript_index"] == trans_index:
            return row["ref_index"]
    return None


def _brand_tokens(timing: Optional[Dict[str, Any]], extra: Sequence[str]) -> List[str]:
    tokens: List[str] = []
    if isinstance(timing, dict):
        brand = timing.get("brand")
        if isinstance(brand, dict):
            for key in ("name", "url"):
                value = brand.get(key)
                if isinstance(value, str) and value.strip():
                    tokens.append(normalize_text(value))
    for value in extra or ():
        if isinstance(value, str) and value.strip():
            tokens.append(normalize_text(value))
    seen = set()
    unique: List[str] = []
    for token in tokens:
        if token and token not in seen:
            seen.add(token)
            unique.append(token)
    return unique


# ---------------------------------------------------------------------------
# 输出
# ---------------------------------------------------------------------------
def _trim(text: str, limit: int = 36) -> str:
    text = str(text)
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def format_text(result: Dict[str, Any]) -> str:
    lines: List[str] = []
    stats = result.get("stats") or {}
    ref = result.get("ref") or {}
    lines.append("转写反查：%s（%s 行）↔ 参考 %s（%s 行）"
                 % (result.get("transcript"), stats.get("transcript_lines", 0),
                    ref.get("kind"), ref.get("lines", 0)))
    lines.append("口径：行相似度阈值 %.2f · 字符覆盖率下限 %.1f%% · 品牌名逐字校验 %s"
                 % (result.get("threshold", DEFAULT_THRESHOLD),
                    result.get("min_coverage", DEFAULT_MIN_COVERAGE) * 100.0,
                    "开" if stats.get("brand_tokens") else "关（无品牌词）"))
    lines.append("")
    headers = ["参考行", "相似度", "转写行", "状态"]
    widths = [40, 8, 40, 6]
    lines.append("  ".join(_pad(headers[i], widths[i]) for i in range(len(headers))))
    lines.append("  ".join("-" * widths[i] for i in range(len(headers))))
    for row in result.get("lines") or []:
        status = {"match": "OK", "low": "低", "missing": "缺行"}[row["status"]]
        sim_text = ("%.2f" % row["similarity"]) if row["similarity"] is not None else "-"
        trans_text = row.get("transcript_text") or "-"
        lines.append("  ".join([
            _pad(_trim("[%d] %s" % (row["ref_index"] + 1, row["ref_text"])), widths[0]),
            _pad(sim_text, widths[1]),
            _pad(_trim(trans_text), widths[2]),
            _pad(status, widths[3]),
        ]))
    for index, text in enumerate(result.get("extra_lines") or [], start=1):
        lines.append("  ".join([
            _pad("（多余）", widths[0]),
            _pad("-", widths[1]),
            _pad(_trim(text), widths[2]),
            _pad("多行", widths[3]),
        ]))
    problems = result.get("problems") or []
    if problems:
        lines.append("")
        lines.append("问题：")
        for item in problems:
            prefix = "ERROR" if item["level"] == "ERROR" else "WARN"
            lines.append("  %s: %s" % (prefix, item["message"]))
    lines.append("")
    lines.append("覆盖：字符 %.1f%% · 行 %.1f%% · 平均相似度 %s"
                 % ((result.get("coverage") or 0.0) * 100.0,
                    (result.get("line_coverage") or 0.0) * 100.0,
                    "%.2f" % result["mean_similarity"]
                    if result.get("mean_similarity") is not None else "-"))
    lines.append("汇总：转写反查 %d ERROR / %d WARN → %s"
                 % (stats.get("errors", 0), stats.get("warnings", 0),
                    "通过" if result.get("ok") else "未通过"))
    return "\n".join(lines)


def _pad(text: str, width: int) -> str:
    shown = 0
    for char in str(text):
        shown += 2 if unicodedata.east_asian_width(char) in ("W", "F") else 1
    return str(text) + " " * max(0, width - shown)


# ---------------------------------------------------------------------------
# 命令行
# ---------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="transcript_check.py",
        description="转写反查：transcript（txt/srt）与字幕源（timing.voice.lines / SRT）逐句交叉校验",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例：\n"
            "  python scripts/transcript_check.py transcript.txt\n"
            "  python scripts/transcript_check.py stt.srt --ref srt --min-coverage 0.9\n"
            "  python scripts/transcript_check.py stt.txt --threshold 0.75 --json\n"
        ),
    )
    parser.add_argument("transcript", nargs="?", default=None,
                        help="转写文件（txt 逐行 或 srt），必填（--selftest 除外）")
    parser.add_argument("--project", default=None, help="项目根目录，默认当前项目（PROMO_ROOT / scripts 上级）")
    parser.add_argument("--ref", choices=("timing", "srt"), default="timing",
                        help="参考源：timing（voice.lines，默认）或 srt（out/subtitles/zh.srt）")
    parser.add_argument("--timing", default=None, help="timing.json 路径，默认 <项目根>/src/timing.json")
    parser.add_argument("--srt", dest="srt_path", default=None,
                        help="参考 SRT 路径（--ref srt 时用），默认 %s" % DEFAULT_SRT)
    parser.add_argument("--format", dest="transcript_format", default="auto",
                        choices=("auto", "txt", "srt"), help="transcript 格式，默认 auto")
    parser.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD,
                        help="行相似度阈值 (0,1]，默认 %g" % DEFAULT_THRESHOLD)
    parser.add_argument("--min-coverage", type=float, default=DEFAULT_MIN_COVERAGE,
                        help="字符覆盖率下限 0~1，默认 %g（逐字）" % DEFAULT_MIN_COVERAGE)
    parser.add_argument("--brand", action="append", default=None, metavar="WORD",
                        help="追加一个必须逐字出现的品牌词（可重复；默认自动读 timing.brand.name/url）")
    parser.add_argument("--no-brand-strict", dest="brand_strict", action="store_false",
                        help="关闭品牌名/网址逐字校验")
    parser.add_argument("--json", action="store_true", help="输出单行 JSON（给程序 / MCP 用）")
    parser.add_argument("--selftest", action="store_true", help="只跑纯函数自测（不读项目素材）")
    return parser


def _resolve(root: Path, raw: Optional[str], default: str) -> Path:
    if not raw:
        return (root / default).resolve()
    candidate = Path(raw)
    if candidate.is_absolute():
        return candidate
    if candidate.is_file() or candidate.is_dir():
        return candidate.resolve()
    return (root / raw).resolve()


def _run(args: argparse.Namespace) -> Dict[str, Any]:
    if not args.transcript:
        raise ValueError("缺少 transcript 参数：把转写文件作为第一个参数传进来"
                         "（txt 逐行或 srt；没有就先用平台/剪辑软件导出）")
    root = Path(args.project) if args.project else Path(PROJECT_ROOT)
    if not root.is_dir():
        raise ValueError("项目根目录不存在：%s" % (root,))
    transcript_path = _resolve(root, args.transcript, args.transcript)
    timing: Optional[Dict[str, Any]] = None
    timing_path: Optional[Path] = None
    srt_path: Optional[Path] = None
    if args.ref == "timing":
        timing_path = _resolve(root, args.timing, DEFAULT_TIMING)
        timing = _read_timing(timing_path)
    else:
        srt_path = _resolve(root, args.srt_path, DEFAULT_SRT)
    return check_transcript(
        transcript_path,
        ref_kind=args.ref,
        project_root=root,
        timing=timing,
        timing_path=timing_path,
        srt_path=srt_path,
        threshold=args.threshold,
        min_coverage=args.min_coverage,
        brand_strict=args.brand_strict,
        extra_brands=tuple(args.brand or ()),
        transcript_format=args.transcript_format,
    )


def main(argv: Optional[Sequence[str]] = None) -> int:
    _force_utf8()
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    if "--selftest" in raw_argv:
        return _selftest()
    parser = build_parser()
    args = parser.parse_args(raw_argv)
    try:
        result = _run(args)
    except ValueError as exc:
        sys.stderr.write("ERROR: %s\n" % (exc,))
        return 1
    if args.json:
        print(json.dumps(result, ensure_ascii=False), flush=True)
    else:
        print(format_text(result), flush=True)
        if not result["ok"]:
            stats = result.get("stats") or {}
            sys.stderr.write("ERROR: 转写反查未通过：%d ERROR / %d WARN\n"
                             % (stats.get("errors", 0), stats.get("warnings", 0)))
    return 0 if result["ok"] else 1


# ---------------------------------------------------------------------------
# 自测（--selftest）：不读项目素材，纯 Python 造夹具
# ---------------------------------------------------------------------------
def _assert(condition: Any, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def _levels(result: Dict[str, Any], level: str) -> List[str]:
    return [item["message"] for item in result["problems"] if item["level"] == level]


def _write_text(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(str(path), "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
    return path


def _selftest() -> int:
    import tempfile

    checks = 0

    def ok(condition: Any, message: str) -> None:
        nonlocal checks
        _assert(condition, message)
        checks += 1

    try:
        # 1) 归一化 / 相似度 / 匹配字符数
        ok(normalize_text("每天记账要花 10 分钟！") == normalize_text("每天记账要花10分钟"),
           "标点/空白/全半角差异归一化后应相同")
        ok(normalize_text("Acme 记账") == normalize_text("acme记账"),
           "大小写差异归一化后应相同")
        ok(similarity("", "") == 1.0, "两个空串相似度应为 1.0")
        ok(similarity("abc", "") == 0.0, "空串相似度应为 0.0")
        ok(abs(similarity("每天记账要花十分钟", "每天记账要花十分钟") - 1.0) < 1e-9,
           "完全相同两行相似度应为 1.0")
        ok(matched_chars("abcdef", "abcxef") == 5, "abcdef vs abcxef 应匹配 5 个字符")
        checks += 1

        with tempfile.TemporaryDirectory(prefix="transcript-check-selftest-") as tmp:
            tmp_path = Path(tmp)
            root = tmp_path / "proj"
            (root / "src").mkdir(parents=True)
            timing = {
                "slug": "selftest",
                "brand": {"name": "青柚记账", "url": "qingyou.app"},
                "voice": {
                    "lines": [
                        {"id": "01", "scene": "hook", "at": 0.5, "text": "每天记账要花十分钟"},
                        {"id": "02", "scene": "demo", "at": 4.0, "text": "打开青柚记账就有答案"},
                        {"id": "03", "scene": "demo", "at": 8.0, "text": "三秒记完一笔账"},
                        {"id": "04", "scene": "cta", "at": 12.0, "text": "搜索青柚记账立刻体验"},
                    ]
                },
            }
            _write_text(root / "src" / "timing.json",
                        json.dumps(timing, ensure_ascii=False, indent=2))
            good_lines = [line["text"] for line in timing["voice"]["lines"]]

            def run(text: str, name: str = "transcript.txt", **kwargs: Any) -> Dict[str, Any]:
                path = _write_text(tmp_path / name, text)
                return check_transcript(path, project_root=root, timing=timing, **kwargs)

            # 2) 完全一致的 transcript = 通过
            good = run("\n".join(good_lines) + "\n")
            ok(good["ok"], "逐行一致的 transcript 应 0 ERROR，实际：%r"
               % (_levels(good, "ERROR"),))
            ok(good["line_coverage"] == 1.0 and good["coverage"] == 1.0,
               "完全一致时行覆盖与字符覆盖都应为 1.0")
            ok(good["mean_similarity"] == 1.0, "完全一致时平均相似度应为 1.0")
            ok(len(good["lines"]) == 4 and all(row["status"] == "match"
                                              for row in good["lines"]),
               "完全一致时 4 行都应是 match")
            ok(json.loads(json.dumps(good, ensure_ascii=False))["ok"] is True,
               "--json 载荷应可序列化")
            text = format_text(good)
            ok("汇总：转写反查 0 ERROR / 0 WARN → 通过" in text,
               "人读输出应打印通过汇总，实际：%r" % (text[-80:],))

            # 3) 纯风格差异（标点/全角数字/空格）= WARN，不拦
            styled = "\n".join([
                "每天记账，要花 十分钟！",
                good_lines[1],
                "三秒记完一笔账。",
                good_lines[3],
            ]) + "\n"
            style = run(styled)
            ok(style["ok"], "纯标点/全半角/空白差异不应让校验失败")
            ok(any("纯风格差异" in msg for msg in _levels(style, "WARN")),
               "纯风格差异应给 WARN，实际 WARN：%r" % (_levels(style, "WARN"),))

            # 4) 漏一行 = ERROR + 覆盖率下降
            missing_text = "\n".join([good_lines[0], good_lines[1], good_lines[3]]) + "\n"
            missing = run(missing_text)
            ok(not missing["ok"], "漏一行应失败")
            ok(any("没有对应行" in msg for msg in _levels(missing, "ERROR")),
               "漏行应报「没有对应行」")
            ok(missing["line_coverage"] < 1.0 and missing["coverage"] < 1.0,
               "漏行后覆盖率应小于 1.0")

            # 5) 顺序错位 = ERROR
            swapped = "\n".join([good_lines[0], good_lines[2], good_lines[1],
                                 good_lines[3]]) + "\n"
            order = run(swapped)
            ok(not order["ok"], "顺序错位应失败")
            ok(any("顺序错位" in msg for msg in _levels(order, "ERROR")),
               "顺序错位应报「顺序错位」，实际：%r" % (_levels(order, "ERROR"),))

            # 6) 内容对不上 = ERROR（相似度低于阈值）
            wrong = "\n".join([good_lines[0], "打开青柚记账就有疑惑", good_lines[2],
                               good_lines[3]]) + "\n"
            wrong_result = run(wrong)
            ok(not wrong_result["ok"], "内容对不上的行应失败")
            ok(any("相似度" in msg for msg in _levels(wrong_result, "ERROR")),
               "内容对不上应报「相似度」")

            # 7) 品牌名错一个字 = ERROR（逐字校验，行相似度仍很高）
            brand_wrong = "\n".join([good_lines[0], "打开青柚记帐就有答案", good_lines[2],
                                     good_lines[3]]) + "\n"
            brand = run(brand_wrong)
            ok(not brand["ok"], "品牌名错一个字应失败")
            ok(any("品牌" in msg for msg in _levels(brand, "ERROR")),
               "品牌错字应报「品牌」，实际：%r" % (_levels(brand, "ERROR"),))
            ok(any("青柚记账" in msg for msg in _levels(brand, "ERROR")),
               "品牌错误应说明期望的品牌词")

            # 8) --no-brand-strict 时不因品牌错字失败（差一个字 → 覆盖率放宽到 0.85）
            relaxed_brand = run(brand_wrong, brand_strict=False, min_coverage=0.85)
            ok(relaxed_brand["ok"], "关掉品牌逐字校验后，只差一个同音字不应失败")

            # 9) 阈值/覆盖率可配：一行差一个字
            near = "\n".join([good_lines[0], good_lines[1], "三秒记完一笔帐", good_lines[3]]) + "\n"
            near_default = run(near)
            ok(not near_default["ok"], "默认逐字覆盖率下，差一个字应失败")
            ok(any("字符覆盖率" in msg for msg in _levels(near_default, "ERROR")),
               "差一个字应报「字符覆盖率」")
            near_relaxed = run(near, min_coverage=0.8)
            ok(near_relaxed["ok"], "放宽 --min-coverage 0.8 后，差一个字应通过")
            near_threshold = run(near, min_coverage=0.8, threshold=0.99)
            ok(not near_threshold["ok"], "把 --threshold 提到 0.99 后，差一个字应失败")

            # 10) 多余转写行 = WARN，不改变 ok
            extra = run("\n".join(good_lines + ["这句是 STT 幻觉"]) + "\n")
            ok(extra["ok"], "多一行只应 WARN，不应失败")
            ok(any("多余" in msg or "没有对应行" in msg for msg in _levels(extra, "WARN")),
               "多余行应给 WARN")

            # 11) SRT transcript 也能读；SRT 参考源
            srt_lines: List[str] = []
            for index, line_text in enumerate(good_lines, start=1):
                start = (index - 1) * 3
                srt_lines += [str(index), "00:00:%02d,000 --> 00:00:%02d,000" % (start, start + 2),
                              line_text, ""]
            srt_transcript = run("\n".join(srt_lines), name="stt.srt")
            ok(srt_transcript["ok"], "SRT 格式 transcript 应能自动识别并通过")
            ok(srt_transcript["stats"]["transcript_format"] == "srt", "应识别为 srt 格式")

            srt_ref_path = _write_text(tmp_path / "zh.srt", "\n".join(srt_lines))
            by_srt = check_transcript(
                _write_text(tmp_path / "plain.txt", "\n".join(good_lines)),
                ref_kind="srt", project_root=root, srt_path=srt_ref_path)
            ok(by_srt["ok"], "--ref srt 与 txt transcript 应通过")
            ok(by_srt["ref"]["kind"] == "srt" and by_srt["ref"]["lines"] == 4,
               "--ref srt 应有 4 条参考 cue")

            # 11b) --ref srt 时品牌词仍来自 timing.brand：以前不读 timing，品牌错字会静默通过
            srt_brand_wrong = check_transcript(
                _write_text(tmp_path / "brand_srt.txt", "\n".join([
                    good_lines[0], "打开青柚记帐就有答案", good_lines[2], good_lines[3]])),
                ref_kind="srt", project_root=root, srt_path=srt_ref_path)
            ok(not srt_brand_wrong["ok"] and any(
                "品牌" in msg for msg in _levels(srt_brand_wrong, "ERROR")),
               "--ref srt 时品牌错字应报「品牌」（不能静默跳过），实际：%r"
               % (_levels(srt_brand_wrong, "ERROR"),))

            # 12) 输入错误：坏 SRT / 空文件 / 找不到文件 / 坏 timing
            bad_srt = _write_text(tmp_path / "bad.srt", "1\n00:00:xx,000 --> 00:00:02,000\n嗨\n")
            try:
                read_transcript(bad_srt, "srt")
            except ValueError as exc:
                ok("时间戳" in str(exc), "坏 SRT 应报时间戳错误，实际：%s" % (exc,))
            else:
                raise AssertionError("坏 SRT 应抛 ValueError")

            empty = check_transcript(_write_text(tmp_path / "empty.txt", "  \n\n"),
                                    project_root=root, timing=timing)
            ok(not empty["ok"] and any("空文件" in msg for msg in _levels(empty, "ERROR")),
               "空 transcript 应报空文件")

            absent = check_transcript(tmp_path / "nope.txt", project_root=root, timing=timing)
            ok(not absent["ok"] and any("找不到" in msg for msg in _levels(absent, "ERROR")),
               "找不到 transcript 应报找不到")
            ok(absent["stats"].get("errors", 0) >= 1,
               "早退分支的 stats.errors 必须与 problems 同源（否则 CLI 会打「0 ERROR」），"
               "实际：%r" % (absent["stats"],))
            bad_threshold = check_transcript(
                tmp_path / "nope.txt", project_root=root, timing=timing, threshold=0.0)
            ok(bad_threshold["stats"].get("errors", 0) >= 1,
               "坏 --threshold 的早退分支也要有 stats.errors，实际：%r"
               % (bad_threshold["stats"],))

            bad_timing = check_transcript(
                _write_text(tmp_path / "t.txt", "x"), project_root=root,
                timing={"voice": {"lines": [{"id": "01", "text": ""}]}})
            ok(not bad_timing["ok"] and any("text" in msg for msg in _levels(bad_timing, "ERROR")),
               "坏 voice.lines（空 text）应报干净 ERROR")

            # 13) 字符覆盖率数值精确（abcdef vs abcxef = 5/6）
            exact_ref = {"voice": {"lines": [{"id": "01", "text": "abcdef"}]}}
            exact = check_transcript(_write_text(tmp_path / "exact.txt", "abcxef\n"),
                                    project_root=root, timing=exact_ref, threshold=0.5,
                                    min_coverage=0.0)
            ok(exact["coverage"] == round(5 / 6.0, 6),
               "字符覆盖率应为 5/6，实际 %r" % (exact["coverage"],))

    except AssertionError as exc:
        print("FAILED: transcript_check.py --selftest：%s" % (exc,))
        return 1
    except Exception as exc:  # noqa: BLE001 - 自测里任何异常都算失败，但不该裸崩
        import traceback
        traceback.print_exc()
        print("FAILED: transcript_check.py --selftest 异常：%s" % (exc,))
        return 1
    print("OK: selftest — 转写反查 %d 项断言通过（归一化/相似度/SRT 解析/漏行/错序/品牌逐字/"
          "覆盖率与阈值可配/多余行 WARN/坏输入干净报错）" % (checks,))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
