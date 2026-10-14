#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""promo-forge · 阶段 8「混音」：人声 + 配乐 + ducking + 音效 → 母带 -14 LUFS。

用途
----
读 ``src/timing.json``，用 numpy 在内存里拼出 48kHz 立体声混音，再用 ffmpeg 两遍
``loudnorm`` 母带到 **-14 LUFS / 真峰值 -1 dBFS**，写到 ``public/<music.file>``
（模板里就是 ``public/audio/mix.wav``，也就是 Remotion 配音轨读的那个文件）。

构成
----
1. **人声**：``audio-src/vo/<id>.mp3`` 按 ``voice.lines[].at`` 摆放，按 ``voice.rmsDb``
   （默认 -13）做 RMS 归一化；
2. **音乐**：``audio-src/music/`` 里由 ``music.source`` 指定的文件，按 ``music.offset``
   换算起点后铺满全片；``music.segments`` 存在时按 ``{at, from, to}`` 剪接（交叉淡化 80ms）；
3. **ducking**：每句人声前后 0.25 秒余弦羽化，人声区间把音乐压到 ``voice.duckDb``
   （默认 -9 dB，相对值），全程增益曲线，没有硬切；
4. **音效**（numpy 合成，不需要素材）：``music.drop`` 处一个低频下潜，之后每个场景起点
   一个轻微转场音，统一 -18 dB；``--no-sfx`` 可关。

人声余量检查（AGENTS.md 阶段 8）
--------------------------------
逐句算「该句区间内人声 RMS − 该处音乐 RMS」：**低于 6 dB 直接 ``ERROR`` 并退出码 1**
（AGENTS.md 阶段 8 的硬性要求，没有 WARN 缓冲带；量的是人声/音乐两条**分开**的总线）。
该句窗口里音乐本来就是静音（例如 ``offset>0`` 的片头）时会跳过比较并注明原因。

典型调用
--------
    python scripts/mix.py                  # 完整混音 → public/audio/mix.wav
    python scripts/mix.py --env            # 额外打印每 0.5 秒响度包络，肉眼查空档/爆音
    python scripts/mix.py --no-vo          # 不混人声不 duck，只出音乐版 → out/mix-music.wav
    python scripts/mix.py --no-sfx         # 关掉音效
    python scripts/mix.py --out public/audio/mix.wav

依赖
----
必需：ffmpeg / ffprobe、numpy（``pip install -r requirements.txt``）。

退出码
------
0 成功；1 有 ERROR：缺 ffmpeg、缺音乐文件、boom、人声余量 < 6 dB（AGENTS.md 阶段 8；
母带 PLR 回落成动态模式时该阈值自动抬到 8 dB，见阶段 8 的「按打印余量再留 2 dB」）、母带失败。
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import struct
import subprocess
import sys
import time
from pathlib import Path

# ---------------------------------------------------------------------------
# 导入公共库 scripts/_common.py（由另一位维护者提供，本脚本不修改它）
# ---------------------------------------------------------------------------
sys.path.insert(0, str(Path(__file__).resolve().parent))
try:  # pragma: no cover - 只在 _common.py 缺失时走到
    from _common import (  # type: ignore
        PROJECT_ROOT,
        error,
        has_cmd,
        line_window,
        load_timing,
        log,
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

SR = 48000                      # 采样率；Remotion/ffmpeg 都按 48k 出，避免重采样
CHANNELS = 2

# AGENTS.md 阶段 8：母带 -14 LUFS / 真峰值 -1 dBFS
TARGET_LUFS = -14.0
TARGET_TP = -1.0
LOUDNESS_RANGE = 11.0

# AGENTS.md 阶段 8：每句人声比该处音乐高 ≥6 dB
# 这里曾经留了一条 3 dB 的"硬底线"（3~6 dB 只打 WARN），等于 4 dB 的混音也能全绿通过，
# 与 AGENTS.md 的「≥6 dB」不一致。现已统一：低于 6 dB 直接 ERROR（不再有 WARN 缓冲带）。
VO_MARGIN_OK_DB = 6.0
VO_MARGIN_FAIL_DB = VO_MARGIN_OK_DB     # 刻意取同一个值：少一档就是不合格
# AGENTS.md 阶段 8 的两条路之②：「接受这 1~2 dB 误差，**按打印余量再留 2 dB**
# （打印 ≥8 dB 才当安全交付）」。母带真的回落成动态模式时，逐句余量按这条判：
# 打印 8 dB 才等于成片里的 6 dB。
VO_MARGIN_PLR_FALLBACK_DB = VO_MARGIN_OK_DB + 2.0

# 母带模式只有两种，明确写成状态，别让它只活在一条 WARN 文本里：
#   "linear"   —— PLR ≤ 13.3 dB，loudnorm 做的是整体增益，打印余量 ≈ 成片余量，按 6 dB 判；
#   "fallback" —— PLR 超限，loudnorm 静默回落成动态模式，逐段压掉 1~2 dB，按 8 dB 判。
# （原来这个状态既没有常量也不进总结：回落了照样按 6 dB 判，
#   于是打印 +7.6 dB 的片子能退 0 交付，而 AGENTS.md 阶段 8 要求这时打印 ≥8 dB。）
MASTER_MODE_LINEAR = "linear"
MASTER_MODE_PLR_FALLBACK = "fallback"
MASTER_MODE_LABEL = {
    MASTER_MODE_LINEAR: "linear（loudnorm 走线性模式，母带≈整体增益）",
    MASTER_MODE_PLR_FALLBACK: "fallback（PLR 超限，loudnorm 回落成动态模式）",
}


def _margin_threshold_db(master_mode: str) -> float:
    """母带模式 → 人声余量阈值。回落时按 AGENTS.md 阶段 8 的「再留 2 dB」用 8 dB。"""
    return (VO_MARGIN_PLR_FALLBACK_DB if master_mode == MASTER_MODE_PLR_FALLBACK
            else VO_MARGIN_FAIL_DB)

# AGENTS.md 阶段 8：片头 0.3 秒淡入、片尾 0.6 秒淡出
FADE_IN_S = 0.3
FADE_OUT_S = 0.6

# timing.json 的默认值（契约里写死的兜底）
DEFAULT_RMS_DB = -13.0          # voice.rmsDb
DEFAULT_DUCK_DB = -9.0          # voice.duckDb（相对值，不是绝对电平）
DEFAULT_MUSIC_GAIN = 0.6        # music.gain（与 AGENTS.md §5 的模板默认值一致）

# ducking 羽化时长：人声前后各 0.25 秒（AGENTS.md 阶段 8 的「羽化」，禁止硬切）
DUCK_FEATHER_S = 0.25
# music.segments 剪接处的交叉淡化时长
SEGMENT_XFADE_S = 0.08

# 音效一律保守：-18 dB 左右（AGENTS.md 总原则「一切审美判断落成数字」）
SFX_LEVEL_DB = -18.0

# 人声增益的安全夹紧范围，避免把噪声底噪也拉起来
VO_GAIN_MIN_DB = -24.0
VO_GAIN_MAX_DB = 18.0

# --env 表格步长
ENV_STEP_S = 0.5
# --env 里判定「空档」/「爆音」的门槛（与 check_video.py 的阈值保持一致）
ENV_SILENCE_DB = -45.0
ENV_CLIP_DB = -0.5

# 写出前留的余量，防止临时 wav 自身削波（loudnorm 之前不能有削波）
PREMASTER_CEILING = 0.99

# 临时文件与中间产物
TMP_WAV_NAME = "out/.mix-premaster.wav"


# ===========================================================================
# 小工具（_common 里没有的，按约定在本文件内用局部私有函数实现）
# ===========================================================================

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
        return path.as_posix()


def _resolve(path_text: str) -> Path:
    path = Path(path_text)
    return path if path.is_absolute() else (PROJECT_ROOT / path)


def _num(cfg: dict, key: str, default, where: str) -> float:
    """取值 + 校验的数字读取工具：坏值一律一行 `ERROR:` + 非零退出，绝不抛裸 traceback。

    代替散落各处的 `float(x.get(...) or 0.0)`：timing.json 里把数字写成 `"10s"`、
    `"0:00"`、`"0.6s"` 这类字符串时，原来会以未捕获的 `ValueError` 崩掉
    （stderr 里只有 traceback、没有 `ERROR:` 行，用户看不出是哪个字段写错了）。
    缺键 / 显式 null / 空串 → 用 default；default 给 None 表示这个字段必填。
    """
    if not isinstance(cfg, dict):
        error(f"{where} 应该是一个对象，现在是 {type(cfg).__name__}：{cfg!r}")
    value = cfg.get(key, default)
    if value is None or (isinstance(value, str) and not value.strip()):
        if default is None:
            error(f"{where} 缺少必填的数字字段 {key}（单位是秒，例如 {key}: 3.6）")
        return float(default)
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        error(f"{where} 的 {key} 不是数字：{value!r}（要数字，不要引号、不要单位后缀，"
              f"例如 {key}: 3.6）")
    try:
        return float(value)
    except (TypeError, ValueError):
        error(f"{where} 的 {key} 不是数字：{value!r}（要数字，不要引号、不要单位后缀，"
              f"例如 {key}: 3.6）")
        raise


def _cfg_num(cfg: dict, key: str, default: float = 0.0, where: str = "") -> float:
    """读 timing.json 里的数字配置：**只在缺键、显式 null 或空串时**才用默认值。

    原来写的是 `float(cfg.get(key, DEFAULT) or DEFAULT)`，`or` 会把合法的 0 也
    当成「没填」：music.gain=0（不要配乐）、voice.duckDb=0（不压音乐）、
    voice.rmsDb=0 都会被静默换成默认值，日志里看不出用户填过 0。
    现在统一走 `_num`：非数字（如 "loud"）会变成一行 ERROR + 非零退出，而不是裸 traceback。
    """
    return _num(cfg, key, default, where or "timing.json")


def _db_to_gain(db: float) -> float:
    return float(10.0 ** (db / 20.0))


def _gain_to_db(gain: float) -> float:
    return 20.0 * math.log10(gain) if gain > 1e-9 else -180.0


def _require_numpy():
    try:
        import numpy as np  # noqa: F401
    except ImportError:
        error(
            "未安装 numpy。执行 `pip install -r requirements.txt`（或 `pip install numpy`）后重试"
        )
    import numpy as np

    return np


def _require_ffmpeg() -> None:
    missing = [name for name in ("ffmpeg", "ffprobe") if not has_cmd(name)]
    if missing:
        error(
            "缺少 %s。混音必须用 ffmpeg 解码与母带："
            "Windows `winget install Gyan.FFmpeg` / macOS `brew install ffmpeg` / "
            "Ubuntu `sudo apt install ffmpeg`" % ("、".join(missing),)
        )


def _run_ffmpeg(args, desc: str, want_stdout: bool = False):
    """跑一次 ffmpeg。want_stdout=True 时返回 bytes，否则只关心成功与否。"""
    cmd = ["ffmpeg", "-hide_banner", "-nostats"] + [str(item) for item in args]
    try:
        proc = subprocess.run(
            cmd,
            stdout=subprocess.PIPE if want_stdout else subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
    except OSError as exc:
        error(f"{desc} 启动 ffmpeg 失败：{exc}")
    if proc.returncode != 0:
        stderr = (proc.stderr or b"").decode("utf-8", "replace").strip()
        error(f"{desc} 失败（ffmpeg 退出码 {proc.returncode}）：{stderr[-800:]}")
    return proc.stdout


def _decode_audio(path: Path, channels: int):
    """用 ffmpeg 解码成 numpy float32，形状 (n, channels)，取值范围约 [-1, 1]。"""
    np = _require_numpy()
    if not Path(path).is_file():
        error(f"找不到音频文件：{path}")
    raw = _run_ffmpeg(
        ["-v", "error", "-i", str(path), "-vn", "-map", "0:a:0",
         "-ac", str(channels), "-ar", str(SR), "-f", "f32le", "-"],
        f"解码 {_rel(Path(path))}",
        want_stdout=True,
    )
    data = np.frombuffer(raw or b"", dtype="<f4")
    if data.size == 0:
        return np.zeros((0, channels), dtype=np.float32)
    usable = (data.size // channels) * channels
    data = data[:usable].reshape(-1, channels)
    return np.ascontiguousarray(data, dtype=np.float32)


def _rms_db(samples) -> float:
    np = _require_numpy()
    if samples is None or samples.size == 0:
        return -180.0
    rms = float(np.sqrt(np.mean(np.square(samples.astype(np.float64)))))
    return _gain_to_db(rms)


def _peak_db(samples) -> float:
    np = _require_numpy()
    if samples is None or samples.size == 0:
        return -180.0
    return _gain_to_db(float(np.max(np.abs(samples))))


def _write_wav_f32(path: Path, samples) -> None:
    """写 32 位浮点 WAV（IEEE float），给 loudnorm 当输入，避免 16 位量化损失。"""
    np = _require_numpy()
    path.parent.mkdir(parents=True, exist_ok=True)
    data = np.ascontiguousarray(samples, dtype="<f4").tobytes()
    channels = int(samples.shape[1]) if samples.ndim > 1 else 1
    block_align = channels * 4
    header = b"RIFF" + struct.pack("<I", 36 + len(data)) + b"WAVE"
    header += b"fmt " + struct.pack(
        "<IHHIIHH", 16, 3, channels, SR, SR * block_align, block_align, 32
    )
    header += b"data" + struct.pack("<I", len(data))
    path.write_bytes(header + data)


def _parse_loudnorm_json(stderr_text: str):
    """从 loudnorm 的 print_format=json 输出里抠出测量值。

    返回的键名统一成第二遍要用的 measured_* 参数名：
        input_i / measured_i / measured_tp / measured_lra / measured_thresh / target_offset

    为什么必须在这里改名：ffmpeg 第一遍 JSON 里的键是 input_i / input_tp / input_lra /
    input_thresh，而第二遍 loudnorm 要的是 measured_I / measured_TP / measured_LRA /
    measured_thresh —— 名字不一样。要是直接把原始 JSON 传出去，_master 会在取
    measured_tp 时 KeyError，被 except 吞掉后**每次都静默退化成单遍母带**，-14 LUFS
    就打不准了（单遍 loudnorm 是动态归一化，响度会随段落起伏）。
    """
    match = re.search(r'\{\s*"input_i".*?\}', stderr_text, re.S)
    if not match:
        return None
    try:
        raw = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
    if not isinstance(raw, dict):
        return None

    def pick(*keys):
        for key in keys:
            value = raw.get(key)
            if value is not None and value != "":
                return value
        return None

    normalized = {
        "input_i": pick("input_i"),
        "measured_i": pick("input_i", "measured_i", "measured_I"),
        "measured_tp": pick("input_tp", "measured_tp", "measured_TP"),
        "measured_lra": pick("input_lra", "measured_lra", "measured_LRA"),
        "measured_thresh": pick("input_thresh", "measured_thresh"),
        "target_offset": pick("target_offset"),
    }
    if normalized["input_i"] is None:
        return None
    return normalized


def _parse_ebur128(stderr_text: str):
    """解析 ebur128 的 Summary：返回 (integrated_LUFS, true_peak_dBFS)。"""
    integrated = None
    peak = None
    for match in re.finditer(r"\bI:\s*(-?[\d.]+|-inf)\s*LUFS", stderr_text):
        integrated = match.group(1)
    for match in re.finditer(r"\bPeak:\s*(-?[\d.]+|-inf)\s*dBFS", stderr_text):
        peak = match.group(1)

    def _to_float(value):
        if value is None or value == "-inf":
            return None
        try:
            return float(value)
        except ValueError:
            return None

    return _to_float(integrated), _to_float(peak)


def _measure_loudness(path: Path):
    """用 ebur128 复测成品：返回 (integrated_LUFS, true_peak_dBFS)。"""
    cmd = ["ffmpeg", "-hide_banner", "-nostats", "-i", str(path),
           "-af", "ebur128=peak=true", "-f", "null", "-"]
    proc = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    if proc.returncode != 0:
        warn("ebur128 复测失败：" + (proc.stderr or b"").decode("utf-8", "replace")[-400:])
        return None, None
    return _parse_ebur128((proc.stderr or b"").decode("utf-8", "replace"))


# ===========================================================================
# 音乐
# ===========================================================================

AUDIO_SUFFIXES = {".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg", ".opus", ".wma", ".aiff", ".aif"}


def _find_music_source(timing: dict) -> Path:
    """定位音乐文件；找不到就给出「把音乐放哪、把文件名写哪」的清晰提示。"""
    music_cfg = timing.get("music", {}) or {}
    declared = str(music_cfg.get("source", "") or "").strip()
    music_dir = PROJECT_ROOT / "audio-src" / "music"
    candidates = []
    if music_dir.is_dir():
        candidates = sorted(
            p for p in music_dir.iterdir() if p.is_file() and p.suffix.lower() in AUDIO_SUFFIXES
        )

    if declared:
        path = _resolve(declared)
        if path.is_file():
            return path
        listing = "、".join(p.name for p in candidates) or "（目录里没有任何音频文件）"
        error(
            f"timing.json 的 music.source=\"{declared}\" 指向的文件不存在：{_rel(path)}。"
            f"audio-src/music/ 里现有：{listing}。"
            "请把选好的免费可商用配乐放进 audio-src/music/，"
            "并把文件名写进 src/timing.json 的 music.source（相对项目根，例如 "
            "\"audio-src/music/track.mp3\"）"
        )

    if candidates:
        error(
            "timing.json 的 music.source 是空的，但 audio-src/music/ 里已经有配乐："
            + "、".join(p.name for p in candidates)
            + "。请把其中一个文件名写进 src/timing.json 的 music.source"
            "（例如 \"audio-src/music/%s\"）" % candidates[0].name
        )
    error(
        "找不到配乐，且 audio-src/music/ 是空的（或不存在）。"
        "自由配乐必须由用户从免费可商用曲库下载：把 1~3 首候选放进 audio-src/music/，"
        "把选中的文件名写进 src/timing.json 的 music.source，再用 "
        "`python scripts/music.py analyze <file>` 分析节拍并回填 beat/bar/drop/offset。"
        "**不会**给你一条只有人声的混音——那不算完成混音"
    )


def _place_with_xfade(np, target, chunk, at_sample: int, xfade_samples: int) -> bool:
    """把 chunk 放到 target 的 at_sample 处，两端各做一段交叉淡化。

    返回是否真的放下了内容（调用方用它判断「一段都没摆进去」；原来返回 None，
    音乐段的循环因此没法知道自己是不是白跑了一圈）。
    """
    total = target.shape[0]
    if at_sample >= total or chunk.shape[0] == 0:
        return False
    if at_sample < 0:
        chunk = chunk[-at_sample:]
        at_sample = 0
        if chunk.shape[0] == 0:
            return False
    end = min(at_sample + chunk.shape[0], total)
    seg = chunk[: end - at_sample]
    length = seg.shape[0]
    xf = int(min(xfade_samples, max(0, (length - 1) // 2)))
    if xf <= 0:
        target[at_sample:end] = seg
        return True
    ramp_in = np.linspace(0.0, 1.0, xf, dtype=np.float32)[:, None]
    ramp_out = np.linspace(1.0, 0.0, xf, dtype=np.float32)[:, None]
    # 起点：已有内容淡出、新内容淡入
    target[at_sample : at_sample + xf] = (
        target[at_sample : at_sample + xf] * (1.0 - ramp_in) + seg[:xf] * ramp_in
    )
    body_end = at_sample + length - xf
    if body_end > at_sample + xf:
        target[at_sample + xf : body_end] = seg[xf:-xf]
    # 终点：新内容淡出、后续已有内容保留
    # （两个加数不能都乘 ramp_out：那样「已有内容」会跟着新内容一起被淡掉，
    #   交叠处末尾直接归零 —— 实测段尾 80ms 出现静音缺口、且起点还叠成 2×。）
    target[body_end:end] = target[body_end:end] * (1.0 - ramp_out) + seg[-xf:] * ramp_out
    return True


def _build_music(np, timing: dict, total_samples: int, music_path: Path):
    """把配乐铺满全片，返回 (n, 2) float32。"""
    music_cfg = timing.get("music", {}) or {}
    raw = _decode_audio(music_path, CHANNELS)
    if raw.shape[0] == 0:
        error(f"配乐 {_rel(music_path)} 解码后是空的，文件可能损坏；换一个文件或重新下载")
    out = np.zeros((total_samples, CHANNELS), dtype=np.float32)
    xfade = max(0, int(round(SEGMENT_XFADE_S * SR)))

    segments = music_cfg.get("segments") or []
    if segments:
        log(f"配乐：{_rel(music_path)} · 按 music.segments 剪接（{len(segments)} 段，交叉淡化 {SEGMENT_XFADE_S*1000:.0f}ms）")
        placed_count = 0
        skipped = 0
        for index, seg in enumerate(segments):
            if not isinstance(seg, dict):
                warn(f"music.segments[{index}] 不是对象，已跳过：{seg!r}")
                skipped += 1
                continue
            at = _num(seg, "at", 0.0, f"music.segments[{index}]")
            start = _num(seg, "from", 0.0, f"music.segments[{index}]")
            stop = seg.get("to", None)
            src_start = max(0, int(round(start * SR)))
            src_end = raw.shape[0] if stop in (None, "") else min(
                raw.shape[0],
                int(round(_num(seg, "to", 0.0, f"music.segments[{index}]") * SR)),
            )
            if src_end <= src_start:
                warn(f"music.segments[{index}] 的 from/to 区间为空（from={start}, to={stop}），已跳过")
                skipped += 1
                continue
            chunk = raw[src_start:src_end]
            at_sample = int(round(at * SR))
            if at_sample >= total_samples:
                warn(f"music.segments[{index}] 的 at={at}s 已经超出片长，实际听不到")
                skipped += 1
                continue
            if not _place_with_xfade(np, out, chunk, at_sample, xfade):
                # at_sample < 片长、但内容整段落在片尾之后（chunk 为空的边界）
                warn(f"music.segments[{index}] 没有内容落在片长以内（at={at}s），已跳过")
                skipped += 1
                continue
            placed_count += 1
            log(f"  段 {index + 1}: 视频 {at:.2f}s ← 音乐 {start:.2f}~{src_end / SR:.2f}s")
        # 非 segments 分支有 `take <= 0 → error(...)`，这条分支原来什么都没有：
        # 例如每段的 at 都 ≥ 片长、或 from/to 全填反，脚本会打几条 WARN、然后以 0 退出，
        # 交出去的成片没有任何配乐。
        if placed_count == 0:
            error(
                f"music.segments 里 {len(segments)} 段一段都没摆进片子（跳过 {skipped} 段）："
                f"配乐会是整条静音，{total_samples / SR:.2f}s 的成片只有人声和音效。"
                "请检查每段的 at（视频时间，必须 < 片长）与 from/to（音乐时间，from < to），"
                "或用 `python scripts/music.py grid` 重新量一遍再写回 timing.json"
            )
        if placed_count < len(segments):
            warn(
                f"music.segments 有 {len(segments)} 段，只摆进去 {placed_count} 段"
                f"（跳过 {skipped} 段）：成片里的配乐比你写的少，确认这是想要的剪接"
            )
    else:
        # 契约：视频时间 = 音乐时间 + offset  →  音乐时间 = 视频时间 - offset
        #
        # 摆放规则（两种符号都必须支持，见 AGENTS.md 阶段 5 与 references/配乐与卡点.md）：
        #   offset > 0 → 音乐从**视频第 offset 秒**起播，视频 [0, offset) 是静音段
        #                （此时 offset = drop - downbeat，音乐里的 downbeat 正好落在画面 drop）
        #   offset < 0 → 跳过音乐开头 |offset| 秒（读 raw[|offset|·SR:]）
        #   offset = 0 → 音乐从 0 秒铺满
        # 注意：以前 offset > 0 时只打 WARN 然后把音乐夹到 0 秒起播——那会让
        # 「音乐时间 = 视频时间 - offset」整体错位 offset 秒，downbeat 落不到 drop 上。
        music_dur = raw.shape[0] / SR
        offset = _num(music_cfg, "offset", 0.0, "timing.json 的 music")

        if offset < 0.0:
            # 跳过前奏：音乐时间 = 视频时间 + |offset|，即从音乐 |offset| 秒开始读
            start_music = min(-offset, music_dur)
            src_start = int(round(start_music * SR))
            if src_start >= raw.shape[0]:
                error(
                    f"music.offset={offset} 让配乐起点（音乐 {start_music:.2f}s）超出了音乐本身长度"
                    f"（{music_dur:.2f}s）。请检查 timing.json 的 offset，"
                    "或用 `python scripts/music.py cut` 剪一段更长的曲子"
                )
            take = min(total_samples, raw.shape[0] - src_start)
            if take > 0:
                out[:take] = raw[src_start : src_start + take]
            video_start = 0.0
        elif offset > 0.0:
            # 音乐比画面晚进：对齐公式要求音乐整体后移 offset 秒，前面留静音
            start_video = int(round(offset * SR))
            if start_video >= total_samples:
                error(
                    f"music.offset={offset} 已经不小于片长 {total_samples / SR:.2f}s："
                    "整条配乐都落在片尾之后，成品会没有人声以外的任何音乐。"
                    "请检查 timing.json 的 offset / duration，或去掉片头静音段"
                )
            take = min(total_samples - start_video, raw.shape[0])
            if take > 0:
                out[start_video : start_video + take] = raw[:take]
            start_music = 0.0
            video_start = start_video / SR
            warn(
                f"music.offset={offset:+.2f}s 的分工：音乐从**视频 {video_start:.2f}s** 起播，"
                f"片头 {video_start:.2f} 秒没有配乐。参考 references/配乐与卡点.md 补低能量铺底："
                "用同曲弱段或环境铺底写进 music.segments（例如 "
                '{"at": 0.0, "from": 0.0, "to": 2.0}），或把开场设计成先人声后进乐'
            )
            if take < total_samples - start_video:
                warn(
                    f"配乐只有 {music_dur:.2f}s，铺到视频 {(start_video + take) / SR:.2f}s 就没了，"
                    f"尾部 {(total_samples - start_video - take) / SR:.2f}s 已用静音补齐。"
                    "请换更长的曲子，或用 `python scripts/music.py cut` 剪一段循环填满"
                )
        else:
            src_start = 0
            take = min(total_samples, raw.shape[0])
            out[:take] = raw[:take]
            start_music = 0.0
            video_start = 0.0

        log(
            f"配乐：{_rel(music_path)} · 音乐 {music_dur:.2f}s，offset={offset:+.3f}，"
            f"从音乐 {start_music:.2f}s 起铺到视频 {video_start:.2f}s，共 {take / SR:.2f}s"
        )
        if take <= 0:
            error(
                f"配乐一点都没摆进片子里：music.offset={offset:+.3f}s，音乐 {music_dur:.2f}s，"
                f"片长 {total_samples / SR:.2f}s"
            )
        if offset <= 0.0 and take < total_samples:
            warn(
                f"配乐只有 {music_dur:.2f}s，比片长 {total_samples / SR:.2f}s 短 "
                f"{(total_samples - take) / SR:.2f}s，尾部已用静音补齐。"
                "请换更长的曲子，或用 `python scripts/music.py cut` 剪一段循环填满"
            )

    gain = _cfg_num(music_cfg, "gain", DEFAULT_MUSIC_GAIN, "timing.json 的 music")
    if abs(gain - 1.0) > 1e-6:
        out *= gain
        log(f"配乐增益：music.gain={gain}")
    return out


# ===========================================================================
# 人声
# ===========================================================================

def _build_voice(np, timing: dict, total_samples: int):
    """摆放并归一化人声。

    返回 (vo_bus, intervals, stats)：
      vo_bus    (n, 2) float32
      intervals [(start_sample, end_sample, line_id, vo_rms_db), ...]
      stats     逐句统计，供人声余量检查与总结打印
    """
    voice_cfg = timing.get("voice", {}) or {}
    target_rms_db = _cfg_num(voice_cfg, "rmsDb", DEFAULT_RMS_DB, "timing.json 的 voice")
    lines = [line for line in (voice_cfg.get("lines") or []) if isinstance(line, dict)]
    vo_dir = PROJECT_ROOT / "audio-src" / "vo"

    bus = np.zeros((total_samples, CHANNELS), dtype=np.float32)
    intervals = []
    stats = []
    missing = []

    for line in lines:
        line_id = str(line.get("id", "")).strip()
        path = vo_dir / f"{line_id}.mp3"
        if not path.is_file():
            missing.append(line_id)
            continue
        mono = _decode_audio(path, 1)
        if mono.shape[0] == 0:
            warn(f"第 {line_id} 句 {_rel(path)} 解码后是空的，已跳过")
            missing.append(line_id)
            continue

        raw_rms = _rms_db(mono)
        gain_db = target_rms_db - raw_rms
        clamped = max(VO_GAIN_MIN_DB, min(VO_GAIN_MAX_DB, gain_db))
        if abs(clamped - gain_db) > 0.5:
            warn(
                f"第 {line_id} 句原始电平 {raw_rms:.1f} dBFS，"
                f"要归一化到 {target_rms_db:.1f} dBFS 需要 {gain_db:+.1f} dB，"
                f"已夹到 {clamped:+.1f} dB（避免把人声底噪一起拉起来）"
            )
        gain = _db_to_gain(clamped)
        chunk = mono * gain

        peak = float(np.max(np.abs(chunk))) if chunk.size else 0.0
        if peak > 0.99:
            chunk *= 0.99 / peak
            warn(f"第 {line_id} 句归一化后削波，已整体降 {_gain_to_db(0.99 / peak):.1f} dB")

        at = _num(line, "at", 0.0, f"timing.json 的 voice.lines[id={line_id}]")
        start = int(round(at * SR))
        if start >= total_samples:
            warn(f"第 {line_id} 句 at={at}s 已经超出片长，实际听不到")
            continue
        if start < 0:
            warn(f"第 {line_id} 句 at={at}s 是负数，按 0 处理")
            start = 0
        end = min(total_samples, start + chunk.shape[0])
        usable = end - start
        if usable <= 0:
            continue
        stereo = np.repeat(chunk[:usable], CHANNELS, axis=1)
        bus[start:end] += stereo

        placed_rms = _rms_db(stereo)
        intervals.append((start, end, line_id, placed_rms))
        stats.append(
            {
                "id": line_id,
                "at": round(at, 3),
                "duration": round(usable / SR, 3),
                "vo_rms_db": round(placed_rms, 2),
                "gain_db": round(clamped, 2),
                "raw_rms_db": round(raw_rms, 2),
            }
        )

    if missing:
        warn(
            "audio-src/vo/ 里缺少这些句子：%s。先跑 `python scripts/tts.py all` 生成；"
            "本次混音里这几句是缺的" % ("、".join(missing),)
        )
    if not lines:
        warn("timing.json 的 voice.lines 是空的：本次输出没有任何人声")
    return bus, intervals, stats


def _duck_curve(np, total_samples: int, intervals, duck_db: float):
    """梯形增益曲线（两端余弦羽化），人声区间把音乐压 duck_db。

    duck_db > 0 表示**反过来**：人声区间把音乐抬 duck_db（有些片子故意这么做）。
    原来写死 `np.minimum(gain, curve)`，curve > 1 的部分被原样抹平成 1.0（静默
    no-op），而 main 仍然照抄配置打印「ducking +6.0 dB」——用户以为生效了。
    """
    gain = np.ones(total_samples, dtype=np.float32)
    target = _db_to_gain(duck_db)
    boost = duck_db > 0.0
    feather = max(1, int(round(DUCK_FEATHER_S * SR)))
    for start, end, _line_id, _rms in intervals:
        start = max(0, start)
        end = min(total_samples, end)
        length = end - start
        if length <= 0:
            continue
        weight = np.ones(length, dtype=np.float32)
        ramp = min(feather, length)
        if ramp > 1:
            up = 0.5 - 0.5 * np.cos(np.pi * np.arange(ramp, dtype=np.float32) / (ramp - 1))
            weight[:ramp] = np.minimum(weight[:ramp], up)
            weight[length - ramp :] = np.minimum(weight[length - ramp :], up[::-1])
        curve = 1.0 - (1.0 - target) * weight
        if boost:
            # curve 在 1 以上（duck_db > 0 = 抬升）时只有 maximum 才是抬升；
            # 写成 minimum 会被 1.0 抹平，成为静默 no-op（本次修的就是这个）。
            gain[start:end] = np.maximum(gain[start:end], curve)
        else:
            gain[start:end] = np.minimum(gain[start:end], curve)
    return gain


# ===========================================================================
# 音效（numpy 合成，不需要素材）
# ===========================================================================

def _stereo(np, mono):
    return np.repeat(mono.astype(np.float32)[:, None], CHANNELS, axis=1)


def _sfx_boom(np, duration: float = 1.1):
    """低频下潜（sub boom）：95Hz 指数滑到 ~34Hz，指数衰减。"""
    n = int(duration * SR)
    t = np.arange(n, dtype=np.float32) / SR
    freq = 95.0 * np.exp(-t * 2.2) + 34.0
    phase = 2.0 * np.pi * np.cumsum(freq) / SR
    env = np.exp(-t * 4.2)
    wave = np.sin(phase) * env
    wave *= np.minimum(1.0, t / 0.008)  # 8ms 起音，避免咔哒声
    return _stereo(np, wave)


def _sfx_tick(np, duration: float = 0.18):
    """转场音：两个高频正弦 + 极短噪声，快速衰减，听感上只是「叮」一下。"""
    n = int(duration * SR)
    t = np.arange(n, dtype=np.float32) / SR
    env = np.exp(-t * 26.0)
    wave = 0.7 * np.sin(2 * np.pi * 1480.0 * t) + 0.3 * np.sin(2 * np.pi * 2960.0 * t)
    rng = np.random.default_rng(20240607)  # 固定种子，保证每次渲染完全一致
    noise = rng.standard_normal(n).astype(np.float32) * np.exp(-t * 90.0) * 0.25
    wave = (wave * env + noise) * np.minimum(1.0, t / 0.004)
    return _stereo(np, wave.astype(np.float32))


def _build_sfx(np, timing: dict, total_samples: int):
    """drop 低频下潜 + 每个场景起点（第一个除外）的转场音。"""
    out = np.zeros((total_samples, CHANNELS), dtype=np.float32)
    amp = _db_to_gain(SFX_LEVEL_DB)
    music_cfg = timing.get("music", {}) or {}

    drop = music_cfg.get("drop", None)
    placed = []
    if drop not in (None, ""):
        try:
            drop_s = float(drop)
        except (TypeError, ValueError):
            # 原来这里静默把非数字当成「没有 drop」：music.drop 是 AGENTS.md 第 5 节的
            # 必填字段（第一次爆发的视频时间），写错类型不该一声不吭地少掉一个音效。
            # 注意不能用哨兵值（如 -1.0）继续往下走：那会让下面「不是正数」的分支对着
            # 哨兵再报一次，用户看到的是 music.drop=-1 这种他根本没写过的值。
            warn(
                f"timing.json 的 music.drop={drop!r} 不是数字，已跳过低频下潜音效。"
                "music.drop 是 AGENTS.md 第 5 节的必填字段（第一次爆发的**视频时间**，单位秒），"
                "请用 music.py grid 算出来写进去（例如 3.6）"
            )
            drop_s = None
        if drop_s is not None:
            if drop_s > 0:
                boom = _sfx_boom(np) * amp
                _place_with_xfade(np, out, boom, int(round(drop_s * SR)), 0)
                placed.append(f"drop {drop_s:.2f}s 低频下潜")
            else:
                # 0 / 负数同样静默过：drop 是必填项，填 0 等于把「第一次爆发」定在片头，
                # 要么是漏填、要么是算错了符号（drop 是视频时间，负数只可能是写反了 offset）。
                warn(
                    f"timing.json 的 music.drop={drop_s:g} 不是正数，已跳过低频下潜音效。"
                    "music.drop 是 AGENTS.md 第 5 节的必填字段（第一次爆发的**视频时间**，应落在片头之后、"
                    "全片之内）；如果确实不要这个音效，请显式删掉 music.drop 并把这条 WARN 写进交付说明"
                )

    scenes = [s for s in (timing.get("scenes") or []) if isinstance(s, dict)]
    turn_count = 0
    for index, scene in enumerate(scenes):
        if index == 0:
            continue  # 片头自己不叠转场音，会和 0.3s 淡入打架
        try:
            start = float(scene.get("start", 0.0) or 0.0)
        except (TypeError, ValueError):
            continue
        if start <= 0:
            continue
        tick = _sfx_tick(np) * amp
        if _place_with_xfade(np, out, tick, int(round(start * SR)), 0):
            turn_count += 1
    # 按**实际摆进去的**数量记账：原来写死 len(scenes) - 1。start 非数字 / ≤0 /
    # 落在片尾之后的场景都会被上面的 continue 跳掉，日志却照旧说「N 个场景转场音」
    #（实测 5 个场景只有 1 个真出声，日志写 4 个）。
    if turn_count:
        placed.append(f"{turn_count} 个场景转场音")
    if placed:
        log(f"音效（numpy 合成，{SFX_LEVEL_DB:.0f} dB）：" + "、".join(placed))
    return out


# ===========================================================================
# 母带
# ===========================================================================

def _apply_fades(np, mix):
    """AGENTS.md 阶段 8：片头 0.3 秒淡入、片尾 0.6 秒淡出。"""
    total = mix.shape[0]
    fade_in = min(total, int(round(FADE_IN_S * SR)))
    fade_out = min(total, int(round(FADE_OUT_S * SR)))
    if fade_in > 1:
        mix[:fade_in] *= np.linspace(0.0, 1.0, fade_in, dtype=np.float32)[:, None]
    if fade_out > 1:
        mix[total - fade_out :] *= np.linspace(1.0, 0.0, fade_out, dtype=np.float32)[:, None]
    return mix


def _master(np, premaster_path: Path, out_path: Path) -> str:
    """两遍 loudnorm：第一遍测量，第二遍按测量值线性母带。

    返回**母带状态常量**：MASTER_MODE_LINEAR（正常）或 MASTER_MODE_PLR_FALLBACK
    （素材 PLR 超 13 dB，loudnorm 静默回落成动态模式，成片里的逐句人声余量会比打印值
    低 1~2 dB）。main() 拿这个状态把「人声余量闸门」的阈值从 6 dB 抬到 8 dB
    （AGENTS.md 阶段 8 的第二条路：按打印余量再留 2 dB），并把它打印在总结里。
    """
    master_mode = MASTER_MODE_LINEAR
    base = f"loudnorm=I={TARGET_LUFS}:TP={TARGET_TP}:LRA={LOUDNESS_RANGE}"
    measure_cmd = [
        "ffmpeg", "-hide_banner", "-nostats", "-i", str(premaster_path),
        "-af", f"{base}:print_format=json", "-f", "null", "-",
    ]
    proc = subprocess.run(measure_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    measured = _parse_loudnorm_json((proc.stderr or b"").decode("utf-8", "replace"))

    # 线性母带的物理上限：PLR（真峰值 − 响度）不能超过「目标峰值 − 目标响度」。
    # -14 LUFS / TP -1 dBFS 只允许 13 dB。素材 PLR 超了，ffmpeg 的 loudnorm 会**静默**
    # 回落成动态模式，母带就不再是"整体增益"：逐句人声余量会被逐段压掉 1~2 dB。
    # 而 _check_vo_margin 量的是**母带前**总线，所以这里必须明确提醒，
    # 否则"mix.py 报 +6.3 dB、成品里其实只有 +4.4 dB"这种偏差永远查不出来。
    if measured:
        try:
            plr = float(measured["measured_tp"]) - float(measured["measured_i"])
        except (KeyError, TypeError, ValueError):
            plr = None
        linear_budget = TARGET_TP - TARGET_LUFS
        if plr is not None and plr > linear_budget + 0.3:
            master_mode = MASTER_MODE_PLR_FALLBACK
            warn(
                "素材峰值余量不足：母带前 TP %.2f dBFS − 响度 %.2f LUFS = PLR %.1f dB，"
                "超过 %.1f LUFS / TP %.1f dBFS 允许的 %.1f dB → loudnorm 会回落成动态模式，"
                "成品里的逐句人声余量会比上面打印的（按母带前总线量的）低 1~2 dB。"
                "改法：给母带前总线过一遍限制器把真峰值压到 -6 dBFS 以下，"
                "或接受这 1~2 dB 误差、把打印余量留到 ≥8 dB 再交付"
                "（本次已自动改用 %.0f dB 这一档判人声余量，见下面的总结与闸门）。"
                "（光降 music.gain 不一定有效：峰值大多是人声总线顶上去的——实测 gain 1.0→0.6，"
                "母带前响度只从 -18.0 变成 -19.25 LUFS，PLR 仍是 19.2 dB。）"
                % (
                    float(measured["measured_tp"]),
                    float(measured["measured_i"]),
                    plr,
                    TARGET_LUFS,
                    TARGET_TP,
                    linear_budget,
                    VO_MARGIN_PLR_FALLBACK_DB,
                )
            )

    codec_args = []
    if out_path.suffix.lower() in (".wav", ".wave"):
        codec_args = ["-c:a", "pcm_s24le"]

    if measured and measured.get("measured_i") not in (None, "-inf"):
        try:
            af = (
                f"{base}"
                f":measured_I={measured['measured_i']}"
                f":measured_TP={measured['measured_tp']}"
                f":measured_LRA={measured['measured_lra']}"
                f":measured_thresh={measured['measured_thresh']}"
                f":offset={measured.get('target_offset') or 0.0}"
                ":linear=true:print_format=summary"
            )
            log(
                "母带（两遍 loudnorm）：输入 %.1f LUFS / TP %.1f dBFS / LRA %.1f LU → "
                "目标 %.1f LUFS / TP %.1f dBFS"
                % (
                    float(measured["measured_i"]),
                    float(measured["measured_tp"]),
                    float(measured["measured_lra"]),
                    TARGET_LUFS,
                    TARGET_TP,
                )
            )
        except (KeyError, TypeError, ValueError):
            measured = None
            af = f"{base}:print_format=summary"
    else:
        measured = None
        af = f"{base}:print_format=summary"
        warn("第一遍 loudnorm 没测出有效响度（输入可能近乎静音或太短），退化为单遍母带")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    _run_ffmpeg(
        ["-y", "-i", str(premaster_path), "-af", af, "-ar", str(SR), "-ac", str(CHANNELS)]
        + codec_args
        + [str(out_path)],
        f"母带写出 {_rel(out_path)}",
    )
    if not out_path.is_file() or out_path.stat().st_size == 0:
        error(f"母带写出失败：{_rel(out_path)} 不存在或是空文件")
    return master_mode


# ===========================================================================
# 报告
# ===========================================================================

def _check_vo_windows(timing: dict, stats) -> None:
    """检查同屏人声窗口：声明的窗口按 `_common.line_window` 是否成立 + 实际摆放是否相交。

    背景：本脚本以前**只**用 `voice.lines[].at` 摆放人声，从来不问「这一句的窗口有多长」。
    于是两种坏情况全都没有任何提示：
      ① 声明的窗口本身不成立（`at` 落在场景末尾、或和下一句挨得太近，扣掉
         `LINE_TAIL_GAP` 后窗口 ≤ 0）——`_common.line_window` 会抛 ValueError，
         而 mix.py 虽然从 `_common` import 了 `line_window`（第 67 行）却一次都没调用；
      ② 摆放后两句真的**同屏相交**（`at` 填成同一个数、或前一句的音频比窗口长），
         两句人声叠在一起，听感是「抢话」，脚本一声不吭。

    只在 --no-vo 之外调用；全部是 WARN（不拦交付：tts.py audit 才是硬闸门）。
    """
    bad_windows = 0
    lines = [line for line in ((timing.get("voice") or {}).get("lines") or [])
             if isinstance(line, dict)]
    for line in lines:
        try:
            line_window(timing, line)
        except ValueError as exc:
            bad_windows += 1
            warn(f"第 {str(line.get('id', '')).strip() or '?'} 句的旁白窗口不成立：{exc}")

    # 实际摆放相交：用【真正的落点】(at, at + 音频长度) 判，而不是声明窗口 ——
    # 声明窗口由 line_window 兜着，真正会叠的是「音频比窗口长」和「at 填重了」。
    spans = []
    for item in (stats or []):
        try:
            start = float(item["at"])
            end = start + float(item["duration"])
        except (KeyError, TypeError, ValueError):
            continue
        spans.append((start, end, str(item.get("id", "")).strip()))
    spans.sort()
    overlaps = 0
    last_end, last_id, last_start = None, None, None
    for start, end, line_id in spans:
        if last_end is not None and start < last_end - 1e-6:
            overlaps += 1
            warn(
                f"人声同屏相交：第 {line_id} 句在 {start:.2f}s 起播，但第 {last_id} 句"
                f"（{last_start:.2f}s 起）要到 {last_end:.2f}s 才结束，两句重叠 "
                f"{last_end - start:.2f}s。同屏两句人声会互相盖住，听感是「抢话」——"
                "请把 timing.json 的 voice.lines[].at 拉开，或把前一句的稿子改短"
                "（AGENTS.md 阶段 7：字数超窗口要改稿，不要硬压）"
            )
        if last_end is None or end > last_end:
            last_end, last_id, last_start = end, line_id, start
    if overlaps:
        warn(f"本次混音有 {overlaps} 处人声同屏相交（见上），成片里会出现两句人声叠着说。")
    elif bad_windows:
        warn(f"有 {bad_windows} 句的窗口不成立（见上）：这几句的摆放位置可能不是你要的。")


def _check_vo_margin(np, vo_bus, ducked_music, stats, intervals):
    """逐句算「人声 RMS − 该处音乐 RMS」，返回 (逐句结果, 最差的一条)。

    两条总线必须**分开**量：以前这里传的是 `vo_bus + ducked_music` 的合总线，
    分母自带人声，算出来是 `vo_rms - rms(vo + music)` ≤ 0，数学上永远达不到 6 dB，
    于是任何一次混音都会退出码 1。而 AGENTS.md 阶段 8 的要求是
    「每句人声比该处音乐高 ≥6 dB」，量的是**人声与音乐两条总线的差**。

    只量*两边都有声音*的那一段：offset>0 时片头本来就没有配乐，无音乐可比。
    """
    results = []
    worst = None
    for (start, end, line_id, vo_rms_db) in intervals:
        window = ducked_music[start:end]
        if window.shape[0] == 0:
            continue
        # 只取「音乐真的在响」的样点：比该窗峰值低 30 dB 以下的样点不算数，
        # 否则片头那段纯静音会把音乐 RMS 拉到 -69 dB，算出 +160 dB 这种假余量。
        floor = 10 ** ((_peak_db(window) - 30.0) / 20.0)
        audible = window[np.max(np.abs(window), axis=1) > floor] if window.ndim > 1 \
            else window[np.abs(window) > floor]
        if audible.shape[0] < window.shape[0] * 0.02:
            results.append(
                {"id": line_id, "at": round(start / SR, 3), "vo_rms_db": round(vo_rms_db, 2),
                 "music_rms_db": None, "margin_db": None, "note": "该句窗口里音乐是静音，无需比较"}
            )
            continue
        music_rms_db = _rms_db(audible)
        margin = vo_rms_db - music_rms_db
        results.append(
            {"id": line_id, "at": round(start / SR, 3), "vo_rms_db": round(vo_rms_db, 2),
             "music_rms_db": round(music_rms_db, 2), "margin_db": round(margin, 2)}
        )
        if worst is None or margin < worst["margin_db"]:
            worst = results[-1]
    return results, worst


def _print_env_table(np, samples, intervals):
    """--env：每 0.5 秒一行 RMS（dBFS）+ 语音区间标记，肉眼查空档与爆音。"""
    total = samples.shape[0]
    step = max(1, int(round(ENV_STEP_S * SR)))
    marks = []
    for (start, end, line_id, _rms) in intervals:
        marks.append((start, end, line_id))

    log("")
    log(f"响度包络（每 {ENV_STEP_S:.1f} 秒一窗，RMS dBFS；[VO] = 该窗有人声）")
    header = "  时间      RMS      峰值     包络（-60…0 dBFS）                标记"
    log(header)
    log("-" * 96)
    for index in range(0, total, step):
        window = samples[index : index + step]
        if window.shape[0] == 0:
            continue
        rms = _rms_db(window)
        peak = _peak_db(window)
        bar_len = int(round(max(0.0, min(60.0, rms + 60.0)) / 60.0 * 32))
        bar = "█" * bar_len + "·" * (32 - bar_len)
        mid = index + window.shape[0] / 2.0
        tags = []
        if any(start <= mid < end for (start, end, _lid) in marks):
            tags.append("[VO]")
        if rms < ENV_SILENCE_DB:
            tags.append("空档")
        if peak > ENV_CLIP_DB:
            tags.append("爆音")
        log(f"  {index / SR:6.2f}s {rms:8.1f} {peak:8.1f}  {bar}  {' '.join(tags)}")
    log("-" * 96)


# ===========================================================================
# main
# ===========================================================================

def main(argv=None) -> int:
    _force_utf8()
    parser = argparse.ArgumentParser(
        prog="mix.py",
        description="promo-forge 混音与母带（AGENTS.md 阶段 8）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "默认输出：public/<timing.json 的 music.file>（模板里是 public/audio/music.wav）\n"
            "退出码：0 通过；1 有 ERROR（缺 ffmpeg / 缺配乐 / 人声余量 <6 dB——"
            "母带 PLR 回落成动态模式时该阈值自动抬到 8 dB / 母带失败）。"
        ),
    )
    parser.add_argument("--no-vo", action="store_true",
                        help="不混人声、不做 ducking，只出音乐版（默认 out/mix-music.wav，用于试听）")
    parser.add_argument("--env", action="store_true",
                        help="打印每 0.5 秒的全片 RMS 表与语音区间标记，检查空档/爆音")
    parser.add_argument("--no-sfx", action="store_true", help="关掉 numpy 合成的音效（drop/转场音）")
    parser.add_argument("--out", default=None,
                        help="输出路径（相对项目根）；默认 public/<music.file>，--no-vo 时默认 out/mix-music.wav")
    args = parser.parse_args(argv)

    _require_ffmpeg()
    np = _require_numpy()
    timing = load_timing()

    duration = _num(timing, "duration", None, "timing.json")
    if duration <= 0:
        error("timing.json 的 duration 必须 > 0（单位：秒）；它等于最后一个场景的 end")
    total_samples = int(round(duration * SR))
    log(
        f"混音：{timing.get('slug', '')} · {duration:.2f}s @ {SR}Hz "
        f"{CHANNELS}ch · {timing.get('width')}×{timing.get('height')} @ {timing.get('fps')}fps"
    )

    music_cfg = timing.get("music", {}) or {}
    music_path = _find_music_source(timing)
    music = _build_music(np, timing, total_samples, music_path)

    voice_cfg = timing.get("voice", {}) or {}
    duck_db = _cfg_num(voice_cfg, "duckDb", DEFAULT_DUCK_DB, "timing.json 的 voice")

    if args.no_vo:
        vo_bus = np.zeros((total_samples, CHANNELS), dtype=np.float32)
        intervals = []
        stats = []
        duck_gain = np.ones(total_samples, dtype=np.float32)
        log("--no-vo：不混人声、不做 ducking（试听版）")
    else:
        vo_bus, intervals, stats = _build_voice(np, timing, total_samples)
        duck_gain = _duck_curve(np, total_samples, intervals, duck_db)
        if duck_db > 0.0:
            duck_note = (
                f"人声区间把音乐**抬升** {duck_db:+.1f} dB（duckDb 填正数 = 不闪避、反向抬升；"
                f"羽化 {DUCK_FEATHER_S*1000:.0f}ms）"
            )
        elif duck_db == 0.0:
            duck_note = "duckDb=0：人声区间不做闪避，音乐保持原电平"
        else:
            duck_note = f"ducking {duck_db:+.1f} dB（羽化 {DUCK_FEATHER_S*1000:.0f}ms）"
        log(
            f"人声：{len(intervals)} 句，RMS 归一化到 {_cfg_num(voice_cfg, 'rmsDb', DEFAULT_RMS_DB, 'timing.json 的 voice'):.1f} dBFS，"
            + duck_note
        )
        # 顺手查一遍「声明的旁白窗口」和「实际摆放是否同屏相交」——两条以前都是盲区
        # （line_window 从 _common 导进来了却一次没用过）。
        _check_vo_windows(timing, stats)

    ducked_music = music * duck_gain[:, None]
    sfx = (
        np.zeros((total_samples, CHANNELS), dtype=np.float32)
        if (args.no_sfx or args.no_vo)
        else _build_sfx(np, timing, total_samples)
    )
    if args.no_vo and not args.no_sfx:
        log("--no-vo：同时跳过音效（试听版只保留配乐）")

    premaster = vo_bus + ducked_music + sfx
    _apply_fades(np, premaster)

    peak = float(np.max(np.abs(premaster))) if premaster.size else 0.0
    if peak > PREMASTER_CEILING:
        scale = PREMASTER_CEILING / peak
        premaster *= scale
        warn(
            f"混音总线峰值 {_gain_to_db(peak):+.2f} dBFS 会削波，已整体降 "
            f"{_gain_to_db(scale):.2f} dB 后再交给 loudnorm"
        )

    if args.out:
        out_path = _resolve(args.out)
    elif args.no_vo:
        out_path = PROJECT_ROOT / "out" / "mix-music.wav"
    else:
        declared = str(music_cfg.get("file", "audio/mix.wav") or "audio/mix.wav")
        out_path = PROJECT_ROOT / "public" / declared
        if not out_path.suffix:
            out_path = out_path.with_suffix(".wav")

    premaster_path = PROJECT_ROOT / TMP_WAV_NAME
    _write_wav_f32(premaster_path, premaster)
    log(
        "混音总线：人声 %.1f dBFS / 音乐(duck 前) %.1f dBFS / duck 后 %.1f dBFS / 音效 %.1f dBFS"
        % (_rms_db(vo_bus), _rms_db(music), _rms_db(ducked_music), _rms_db(sfx))
    )

    # 人声余量检查要在「母带前」的总线上做（母带是整体增益，不影响相对关系）
    margin_results, worst = ([], None)
    if intervals:
        margin_results, worst = _check_vo_margin(np, vo_bus, ducked_music, stats, intervals)

    # 母带会告诉我们 loudnorm 有没有回落成动态模式（PLR 超 13 dB）：回落时成片里的
    # 逐句余量比这里打印的低 1~2 dB，闸门必须跟着抬到 8 dB（AGENTS.md 阶段 8 的路②）。
    master_mode = _master(np, premaster_path, out_path)
    plr_fallback = master_mode == MASTER_MODE_PLR_FALLBACK
    margin_threshold_db = _margin_threshold_db(master_mode)
    integrated, true_peak = _measure_loudness(out_path)

    # 真峰值兜底：loudnorm 的 TP 限制器是「自己的真峰值估计」，与 ebur128 的 4× 过采样测量
    # 会差 ~0.1 dB（实测固定输出 -0.90 dBFS，而 AGENTS.md 阶段 8 要求 ≤ -1.0）。这里以
    # ebur128 的读数为准收一次总增益，保证落盘的成品自己就达标。
    if true_peak is not None and true_peak > TARGET_TP:
        trim_db = true_peak - TARGET_TP
        warn(
            "真峰值 %.2f dBFS 超过目标 %.1f dBFS（loudnorm 的 TP 估计与 ebur128 差 %.2f dB），"
            "已整体收 %.2f dB 再复测" % (true_peak, TARGET_TP, trim_db, trim_db + 0.05)
        )
        trimmed_path = out_path.with_name(out_path.stem + ".tp-trim" + out_path.suffix)
        codec_args_trim = (["-c:a", "pcm_s24le"]
                           if out_path.suffix.lower() in (".wav", ".wave") else [])
        _run_ffmpeg(
            ["-y", "-i", str(out_path), "-af", "volume=%.4fdB" % (-(trim_db + 0.05)),
             "-ar", str(SR), "-ac", str(CHANNELS)] + codec_args_trim + [str(trimmed_path)],
            "真峰值兜底",
        )
        os.replace(str(trimmed_path), str(out_path))
        integrated, true_peak = _measure_loudness(out_path)
        log("兜底后：%.1f LUFS / 真峰值 %.2f dBFS（要求 ≤ %.1f）"
            % (integrated if integrated is not None else float("nan"),
               true_peak if true_peak is not None else float("nan"), TARGET_TP))

    # 调试用：想让母带前的总线留在 out/ 里做逐段核对，就把 PF_KEEP_PREMASTER 设成 1。
    if os.environ.get("PF_KEEP_PREMASTER", "") not in ("1", "true", "yes"):
        try:
            premaster_path.unlink(missing_ok=True)
        except OSError:
            pass
    else:
        log("已保留母带前总线：%s（PF_KEEP_PREMASTER=1）" % TMP_WAV_NAME)

    if args.env:
        mixed = _decode_audio(out_path, CHANNELS)
        _print_env_table(np, mixed, intervals)

    # ---- 总结 ----
    log("")
    log("=" * 72)
    log(f"输出：{_rel(out_path)}（{out_path.stat().st_size / 1024 / 1024:.1f} MB）")
    log(f"总时长：{duration:.2f} 秒（{total_samples} 采样）")
    if integrated is None:
        warn("母带后响度复测失败（ebur128 没有输出可解析的结果）")
    else:
        flag = "OK" if TARGET_LUFS - 1.0 <= integrated <= TARGET_LUFS + 1.0 else "偏差偏大"
        log(f"母带后响度：{integrated:.1f} LUFS（目标 {TARGET_LUFS:.0f}，{flag}）")
    if true_peak is not None:
        log(f"真峰值：{true_peak:.2f} dBFS（目标 ≤ {TARGET_TP:.1f}）")
        if true_peak > TARGET_TP:
            warn(f"真峰值 {true_peak:.2f} dBFS 仍高于目标 {TARGET_TP:.1f} dBFS，平台转码可能削波")
    if intervals:
        judged = [item for item in margin_results if item["margin_db"] is not None]
        skipped = [item for item in margin_results if item["margin_db"] is None]
        if judged:
            log(
                f"人声余量：最小 {min(item['margin_db'] for item in judged):+.1f} dB"
                f"（第 {worst['id']} 句 @ {worst['at']:.2f}s，AGENTS.md 阶段 8 要求 ≥ {VO_MARGIN_OK_DB:.0f} dB，低于它即 ERROR）"
            )
            log("  逐句： " + "  ".join(
                f"{item['id']}:{item['margin_db']:+.1f}" for item in judged))
        if skipped:
            log("  未比较： " + "  ".join(
                f"{item['id']}（{item['note']}）" for item in skipped))
    else:
        log("人声余量：无（本次没有混入人声）")
    # 母带的两种模式：回落成动态模式时，成片余量比上面打印的低 1~2 dB，
    # 所以闸门按 8 dB 判（AGENTS.md 阶段 8 的路②：按打印余量再留 2 dB）。
    log(f"母带模式：{MASTER_MODE_LABEL[master_mode]}")
    log(
        "人声余量判据：PLR 回落状态 = "
        + ("已回落（loudnorm 走动态模式）→ 本次按 %.0f dB 判" % margin_threshold_db
           if plr_fallback else
           "未回落（loudnorm 走线性模式）→ 本次按 %.0f dB 判" % margin_threshold_db)
    )
    if plr_fallback:
        log(
            "  ↑ 素材 PLR 超过 %.0f LUFS / TP %.1f dBFS 允许的 %.1f dB，母带不是整体增益："
            "成片里每句的实际余量比打印值低 1~2 dB，所以判据从 %.0f dB 抬到 %.0f dB。"
            "想回到 %.0f dB 判据，就给母带前总线过一遍限制器把真峰值压到 -6 dBFS 以下。"
            % (TARGET_LUFS, TARGET_TP, TARGET_TP - TARGET_LUFS,
               VO_MARGIN_FAIL_DB, VO_MARGIN_PLR_FALLBACK_DB, VO_MARGIN_FAIL_DB)
        )
    log("=" * 72)

    # ---- 闸门 ----
    # AGENTS.md 阶段 8：每句人声比该处音乐高 ≥6 dB。低于它就是 ERROR（不再有 3~6 dB 的 WARN 区）。
    # PLR 回落时阈值抬到 8 dB：打印 8 dB 才等于成片里的 6 dB（阶段 8 的路②）。
    hard_fail = False
    for item in margin_results:
        if item["margin_db"] is None:
            continue
        if item["margin_db"] < margin_threshold_db:
            sys.stderr.write(
                "ERROR: 第 %s 句人声只高出音乐 %.1f dB（AGENTS.md 阶段 8 要求 ≥ %.0f dB%s）\n"
                % (item["id"], item["margin_db"], margin_threshold_db,
                   "；本次母带 PLR 回落成动态模式，按打印余量再留 2 dB" if plr_fallback else "")
            )
            hard_fail = True
    if hard_fail:
        error(
            "有句子的人声被配乐盖住（余量 < %.0f dB，AGENTS.md 阶段 8 的硬性要求%s），本次混音不通过。"
            "改法：调小 timing.json 的 music.gain、把 voice.duckDb 调得更低（例如 -12）、"
            "或让 tts.py 把音量再抬一点"
            % (margin_threshold_db,
               "；本次是 PLR 回落后的加严阈值" if plr_fallback else "")
        )
    if integrated is not None and not (TARGET_LUFS - 2.0 <= integrated <= TARGET_LUFS + 2.0):
        warn(
            f"母带响度 {integrated:.1f} LUFS 偏离目标 {TARGET_LUFS:.0f} LUFS 超过 2 dB；"
            "素材本身动态太大时 loudnorm 线性模式会有偏差，可考虑压缩后再母带"
        )
    log("混音完成。下一步：python scripts/check_assets.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
