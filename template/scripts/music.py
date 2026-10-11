#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""配乐分析：算 BPM/节拍网格/逐小节能量/剪段，以及直链下载你已经获得授权的音频。

用途
    AGENTS.md 阶段 5（配乐与卡点）。AI 听不到声音，所以把「这首曲子该怎么卡点」变成可测量的数字：
      - analyze  时长/采样率/声道/BPM/每秒能量条形图/最猛的 5 个时刻（候选 drop）
      - grid     拍长、小节长、节拍一致性、第一个强拍（downbeat）、以及写回 timing.json 的换算
      - bars     逐小节的全频/低频/高频能量表，标出能量突增的小节，告诉你第几小节适合切镜头
      - cut      从长曲子里剪一段并加淡入淡出（试听用的 15 秒片段）
      - get      直链下载（请只下载你已获得授权的音频）

典型调用
    python scripts/music.py analyze audio-src/music/track.mp3
    python scripts/music.py grid audio-src/music/track.mp3 --from 0 --to 30
    python scripts/music.py bars audio-src/music/track.mp3 --beat 0.4615 --downbeat 0.23 --offset -3.4
    python scripts/music.py cut audio-src/music/track.mp3 out/audition-a.mp3 --start 22 --dur 15 --fade 0.4
    python scripts/music.py get "https://example.com/free-track.mp3" -o audio-src/music --name candidate-a

依赖
    ffmpeg（解码成 16k 单声道临时 wav）、numpy、scipy。没有 ffmpeg 会打印 ERROR 告诉你怎么装。
    不做任何音乐网站爬虫：请从免费可商用曲库（YouTube Audio Library / Pixabay / Free Music Archive 等）
    自行下载到 audio-src/music/，或用 get 直链下载你已经获得授权的音频。每首歌的出处与授权要写进 调研笔记.md。

退出码
    0 = 成功（可能有 WARN）
    1 = 缺 ffmpeg / 缺 numpy、scipy / 文件不存在 / 参数不合法 / 下载失败
    2 = 命令行参数写错（argparse 的标准行为）
"""

from __future__ import annotations

import argparse
import atexit
import math
import os
import re
import shutil
import sys
import tempfile
import wave
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple
from urllib.parse import unquote, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _common as C  # noqa: E402

# ---------------------------------------------------------------------------
# 常量（阈值集中在此，注释标注来源）
# ---------------------------------------------------------------------------

#: 分析用采样率。16kHz 足够覆盖 kick（40~120Hz）到 hi-hat（8kHz），又不至于吃内存
ANALYZE_SR = 16000
#: 能量包络：帧长 512 样点（32ms）、步长 128 样点（8ms，即 125 帧/秒）
ANALYZE_FRAME = 512
ANALYZE_HOP = 128
#: 自相关搜索的 BPM 范围（覆盖绝大多数商业曲库）
BPM_MIN, BPM_MAX = 60.0, 200.0
#: 速度先验：人耳觉得「正常」的速度在 120 BPM 附近，用对数正态加权压制倍频/半频误判。
#: 这是节拍估计的标准做法（librosa 的 tempo prior 同思路）；对数标准差单位是“倍频程”。
TEMPO_PRIOR_BPM = 120.0
TEMPO_PRIOR_OCTAVES = 0.9
#: 自相关低于它的峰当噪声丢掉
TEMPO_MIN_SCORE = 0.05
#: 判定「同一个速度族」的相对误差（BPM）
TEMPO_FAMILY_TOL = 3.0
#: 读拍点强度时在 ±这么多帧内取最大值（起音峰只有 1 帧宽，非整数拍长会让网格落偏 1~2 帧）
POOL_HALF = 2
#: 起音包络的系统性时间偏置（秒）。帧窗口是 [i*hop, i*hop+frame)，一个脉冲最早会被
#: 「窗口末尾刚盖住它」的那一帧吃掉能量，所以差分峰比真实发声早 (frame-hop) 个样点。
#: 不减掉它，报出来的 downbeat 会系统性偏早 24ms（合成轨实测 0.028s）。
ONSET_BIAS_SEC = (ANALYZE_FRAME - ANALYZE_HOP) / float(ANALYZE_SR)
#: 起音间隔折算成「拍」时允许的分数比值：十六分 / 三连 / 八分 / 附点八分 / 附点四分 /
#: 整拍 / 跨 1.5、2、3、4 拍。用它们把间隔归到最接近的比值，比「除以最小间隔」稳得多。
SIMPLE_RATIOS = (0.25, 1.0 / 3.0, 0.5, 2.0 / 3.0, 0.75, 1.0, 1.5, 2.0, 3.0, 4.0)
#: downbeat 往前退整小节时的容差（秒）：退到区间起点附近的抖动就认成 0
DOWNBEAT_WALK_TOL = 0.02
#: 小节 = 几拍。来源：AGENTS.md 第 5 节「bar：每小节秒数（通常 4 beat）」
BAR_BEATS = 4.0
#: 节拍一致性闸门。来源：AGENTS.md 阶段 5「节拍一致性 ≥0.9」
CONSISTENCY_MIN = 0.9
#: 低于它就建议换曲子。来源：本脚本契约（一致性 <0.8 → WARN，建议换鼓点更清晰的曲子）
CONSISTENCY_WARN = 0.8
#: 第一次爆发的最晚视频时间。来源：AGENTS.md 阶段 5「最晚第 4 秒完成第一次爆发」
FIRST_DROP_MAX_SEC = 4.0
#: 判定「能量突增」的 dB 增量（比前两小节均值高这么多就标 DROP?）。来源：本脚本契约
DROP_RISE_DB = 3.0
#: bars 的切镜头建议只推「比全场最高小节低不超过这么多 dB」的小节。来源：本脚本契约
CUT_BAR_TOLERANCE_DB = 3.0
#: 切镜头建议最多列几个小节
CUT_BAR_MAX = 4
#: 逐小节能量的频段划分（Hz）。来源：本脚本契约（低频 = 鼓/贝斯，高频 = 镲片/空气感）
BAND_LOW = (40.0, 200.0)
BAND_HIGH = (4000.0, 8000.0)
#: dB 下限（避免 log(0)）
DB_FLOOR = -80.0
#: analyze 的条形图宽度（# 的最大个数）
BAR_CHART_WIDTH = 40
#: 下载相关
DL_CHUNK = 65536
DL_TIMEOUT = 30
DL_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
         "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
AUDIO_EXTS = {".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg", ".opus", ".wma", ".aiff", ".aif"}
EXT_BY_CT = {
    "audio/mpeg": ".mp3",
    "audio/mp3": ".mp3",
    "audio/wav": ".wav",
    "audio/x-wav": ".wav",
    "audio/wave": ".wav",
    "audio/mp4": ".m4a",
    "audio/x-m4a": ".m4a",
    "audio/aac": ".aac",
    "audio/flac": ".flac",
    "audio/x-flac": ".flac",
    "audio/ogg": ".ogg",
    "audio/opus": ".opus",
}

_TMP_DIR: Optional[Path] = None


def _tmp_dir() -> Path:
    global _TMP_DIR
    if _TMP_DIR is None:
        _TMP_DIR = Path(tempfile.mkdtemp(prefix="promo-music-"))
        atexit.register(shutil.rmtree, str(_TMP_DIR), True)
    return _TMP_DIR


# ---------------------------------------------------------------------------
# 延迟导入（没装依赖也要能看 --help）
# ---------------------------------------------------------------------------

def _np() -> Any:
    try:
        import numpy
    except ImportError:
        C.error("缺少 numpy，请运行 pip install -r requirements.txt")
    return numpy


def _signal() -> Any:
    try:
        from scipy import signal
    except ImportError:
        C.error("缺少 scipy，请运行 pip install -r requirements.txt")
    return signal


def _requests() -> Any:
    try:
        import requests
    except ImportError:
        C.error("缺少 requests，请运行 pip install -r requirements.txt")
    return requests


# ---------------------------------------------------------------------------
# 音频读取
# ---------------------------------------------------------------------------

def _check_audio_file(path: Path) -> Path:
    if not path.exists():
        C.error(f"文件不存在：{C.rel(path)}")
    if not path.is_file():
        C.error(f"不是文件：{C.rel(path)}")
    return path


def load_audio(path: Path) -> Tuple[Any, int]:
    """用 ffmpeg 解码成 16k 单声道 wav，再用 wave + numpy 读成 float32 单声道数组。

    返回 (samples, sample_rate)。缺 ffmpeg -> ERROR（提示三种安装方式）。
    """
    np = _np()
    _check_audio_file(path)
    if not C.has_cmd("ffmpeg"):
        C.error("读音频要先用 ffmpeg 解码（不依赖 librosa 之类的重依赖）。\n" + C.FFMPEG_HINT)
    wav_path = _tmp_dir() / (path.stem + ".16k.wav")
    C.ffmpeg("-i", str(path), "-vn", "-acodec", "pcm_s16le", "-ac", "1",
             "-ar", str(ANALYZE_SR), "-f", "wav", str(wav_path))
    if not wav_path.exists() or wav_path.stat().st_size < 44:
        C.error(f"ffmpeg 没能解码出音频：{C.rel(path)}（是不是没有音轨？）")
    try:
        with wave.open(str(wav_path), "rb") as w:
            n_frames = w.getnframes()
            channels = w.getnchannels()
            width = w.getsampwidth()
            sr = w.getframerate()
            raw = w.readframes(n_frames)
    except wave.Error as exc:
        C.error(f"读 WAV 失败（{exc}）：{wav_path}")
    if width != 2:
        C.error(f"解码出来的位深不是 16bit（{width * 8}bit），请把这条报给脚本作者")
    data = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    if channels > 1:
        data = data.reshape(-1, channels).mean(axis=1)
    if data.size == 0:
        C.error(f"{C.rel(path)} 解码后是空的（时长 0？）")
    return data, sr


def _rms_envelope(x: Any, frame: int = ANALYZE_FRAME, hop: int = ANALYZE_HOP) -> Tuple[Any, Any]:
    """逐帧 RMS 包络与每帧起始样点下标。"""
    np = _np()
    n = len(x)
    if n < frame:
        x = np.pad(x, (0, frame - n))
        n = len(x)
    csum = np.concatenate([[0.0], np.cumsum(x.astype(np.float64) ** 2)])
    idx = np.arange(0, n - frame + 1, hop, dtype=np.int64)
    energy = (csum[idx + frame] - csum[idx]) / float(frame)
    return np.sqrt(np.maximum(energy, 0.0)), idx


def _onset_strength(x: Any, sr: int, frame: int = ANALYZE_FRAME, hop: int = ANALYZE_HOP) -> Tuple[Any, float]:
    """起始点强度（onset strength）：RMS 包络的正向差分，再减掉局部均值。返回 (onset, 帧率)。"""
    np = _np()
    env, _ = _rms_envelope(x, frame, hop)
    fps = sr / float(hop)
    onset = np.diff(env, prepend=env[:1])
    onset = np.maximum(onset, 0.0)
    win = 21
    if onset.size > win:
        kernel = np.ones(win) / win
        onset = np.maximum(onset - np.convolve(onset, kernel, mode="same"), 0.0)
    peak = float(onset.max()) if onset.size else 0.0
    if peak > 0:
        onset = onset / peak
    return onset, fps


# ---------------------------------------------------------------------------
# 节拍分析
# ---------------------------------------------------------------------------

def _autocorr(onset: Any) -> Any:
    np = _np()
    o = onset - float(onset.mean())
    ac = np.correlate(o, o, mode="full")[len(o) - 1:]
    if ac[0] > 0:
        ac = ac / ac[0]
    return ac


def _tempo_prior(bpm: float) -> float:
    """速度先验：以 TEMPO_PRIOR_BPM 为中心的对数正态权重（0~1）。

    为什么必须有：点击类曲子的自相关在每个整数倍拍长上都有自己的峰，而 0.5 秒的拍
    在 125 帧/秒的包络上对应非整数 lag（62.5 帧）——整数 lag 125（= 2 拍）反而比 62/63
    更相关，于是「120 BPM 的曲子被判成 60 BPM」。乘上先验后 120 会盖过它的半速孪生兄弟。
    """
    if bpm <= 0:
        return 0.0
    z = math.log2(bpm / TEMPO_PRIOR_BPM) / TEMPO_PRIOR_OCTAVES
    return math.exp(-0.5 * z * z)


def _same_tempo_family(a: float, b: float) -> bool:
    """a 与 b 是不是同一个速度族（整数倍/整数分之一，例如 120 vs 60、120 vs 180）。"""
    return any(abs(a - b * k) < TEMPO_FAMILY_TOL for k in (2.0, 3.0, 0.5, 1 / 3))


def _tempo_candidates(onset: Any, fps: float, top: int = 5) -> List[Tuple[float, float, float]]:
    """返回 [(BPM, 自相关得分, 先验加权得分), ...]，按**加权**得分降序，每个速度族只留一个。

    先排序再加权是关键：同一个速度族里挑加权后最高的那个（120 打赢 60），
    而不是让原始自相关最高的那个（60）先把族里的名额占掉。
    """
    np = _np()
    ac = _autocorr(onset)
    lag_min = max(1, int(round(fps * 60.0 / BPM_MAX)))
    lag_max = min(len(ac) - 2, int(round(fps * 60.0 / BPM_MIN)))
    if lag_max <= lag_min:
        return []
    raw: List[Tuple[float, float, float]] = []
    for lag in range(lag_min, lag_max + 1):
        if ac[lag] >= ac[lag - 1] and ac[lag] >= ac[lag + 1] and ac[lag] >= TEMPO_MIN_SCORE:
            bpm = 60.0 * fps / lag
            if BPM_MIN <= bpm <= BPM_MAX:
                score = float(ac[lag])
                raw.append((round(bpm, 2), score, score * _tempo_prior(bpm)))
    raw.sort(key=lambda t: -t[2])
    out: List[Tuple[float, float, float]] = []
    for bpm, score, weighted in raw:
        if any(_same_tempo_family(bpm, o[0]) for o in out):
            continue
        out.append((bpm, score, weighted))
        if len(out) >= top:
            break
    return out


def _pool(onset: Any, half: int = POOL_HALF) -> Any:
    """把 onset 在 ±half 帧上取最大值。

    为什么需要：包络是 8ms 一帧，起音峰只有 1 帧宽，而 120 BPM 的拍长在 125 帧/秒下是
    62.5 帧——相位网格再怎么调也会有一半的拍落在峰旁边 1~2 帧。不池化的话
    「相位得分」纯粹由取整运气决定，第一个强拍会整拍飘掉。
    """
    np = _np()
    if half <= 0 or onset.size == 0:
        return onset
    out = onset.copy()
    for k in range(1, half + 1):
        out = np.maximum(out, np.concatenate([np.zeros(k, dtype=onset.dtype), onset[:-k]]))
        out = np.maximum(out, np.concatenate([onset[k:], np.zeros(k, dtype=onset.dtype)]))
    return out


def _refine_period(onset: Any, fps: float, period: float) -> float:
    """用抛物线插值把拍长精确到亚帧。

    拍长很少正好落在整数帧上（120 BPM 在 125 帧/秒的包络上 = 62.5 帧），所以先在
    ±1 帧里挑真正的峰值，再对它做插值——直接用 round() 会引入 0.8% 的误差，
    40 拍之后相位就飘掉 0.16 秒。
    """
    np = _np()
    ac = _autocorr(onset)
    lag = int(round(period * fps))
    lo_lag = max(1, lag - 1)
    hi_lag = min(len(ac) - 2, lag + 1)
    if hi_lag < lo_lag:
        return period
    lag = max(range(lo_lag, hi_lag + 1), key=lambda k: float(ac[k]))
    y0, y1, y2 = float(ac[lag - 1]), float(ac[lag]), float(ac[lag + 1])
    denom = y0 - 2 * y1 + y2
    delta = 0.5 * (y0 - y2) / denom if abs(denom) > 1e-12 else 0.0
    delta = max(-1.0, min(1.0, delta))
    return (lag + delta) / fps


def _beat_times(fps: float, period: float, phase: float, n_frames: int,
                offset_beats: float = 0.0, stride_beats: float = 1.0) -> Any:
    """从相位出发按浮点拍长推拍点帧号——拍长是小数时不能拿整数步长去 arange。

    stride_beats=4 表示「每小节取同一拍位」（找 downbeat 时必须这样取，
    否则四个拍位其实都在遍历同一串拍点，平均下来当然全都一样）。
    """
    np = _np()
    t0 = phase + offset_beats * period
    if t0 < 0:
        t0 += period * math.ceil(-t0 / period)
    step = period * stride_beats
    if step <= 0:
        return np.zeros(0, dtype=np.int64)
    span = n_frames / fps - t0
    count = int(span / step) + 1 if span >= 0 else 0
    if count <= 0:
        return np.zeros(0, dtype=np.int64)
    ts = t0 + step * np.arange(count, dtype=np.float64)
    idx = np.round(ts * fps).astype(np.int64)
    return np.clip(idx, 0, n_frames - 1)


def _beat_phase(onset: Any, fps: float, period: float) -> Tuple[float, float]:
    """在 [0, period) 里找让“格点上的 onset 能量”最大的相位。返回 (相位秒, 归一化得分)。"""
    np = _np()
    n = len(onset)
    pooled = _pool(onset)
    step_samples = max(1, int(round(period * fps)))
    best_phi, best_score = 0.0, -1.0
    steps = max(16, step_samples * 4)
    for k in range(steps):
        phi = period * k / steps
        idx = _beat_times(fps, period, phi, n)
        if idx.size < 2:
            continue
        score = float(pooled[idx].mean())
        if score > best_score:
            best_score, best_phi = score, phi
    return best_phi, max(0.0, min(1.0, best_score))


def _phrase_strength(onset: Any, fps: float, period: float, phase: float, offset_beats: float) -> float:
    """某一拍位（0/1/2/3 beat）上的平均 onset 能量——用来找 downbeat。

    注意 stride_beats=BAR_BEATS：只看每个小节里同一拍位的那些拍点。
    """
    np = _np()
    n = len(onset)
    idx = _beat_times(fps, period, phase, n, offset_beats, stride_beats=BAR_BEATS)
    if idx.size == 0:
        return 0.0
    return float(_pool(onset)[idx].mean())


def _consistency(onset: Any, fps: float, period: float, tol: float = 0.25) -> Tuple[float, float, float, int, float]:
    """节拍一致性 = 1 - 标准差/均值（变异系数 CV），基准是「每个间隔反推出的拍长」。

    做法：找 onset 局部峰值 → 相邻峰间隔 → 用自相关估出的拍长把每个间隔归到最接近的
    分数比值（十六分 / 三连 / 八分 / 附点 / 整拍 / 跨 2~4 拍）→ 残差在 ±tol×比值 内的收下 →
    每个间隔都能反推一个拍长估计 `间隔 ÷ 比值` → 算这些估计的 CV。
    返回 (一致性, 反推拍长均值秒, 反推拍长标准差秒, 参与计算的间隔数, 中位比值)。

    两处都是踩过坑才这样写的：
    1. 基准必须用「自相关估出的拍长」，不能用「最小间隔」——鼓组里 hi-hat 打十六分音符、
       峰值检测偶尔冒出相邻帧的双峰，最小间隔会变成 0.02s 这种噪声值，所有间隔都成了它的
       整数倍，CV 被抹平成 0.97：看似通过闸门，其实什么都没测。
    2. 不能直接对原始间隔算 CV——八分音符的 0.25s 和整拍的 0.5s 混在一个均值里，
       一首完全规整的曲子也会算出 0.86（合成点击轨实测）。先除以比值变回「拍长」再算 CV。
    """
    np = _np()
    n = len(onset)
    if n < 4 or period <= 0:
        return 0.0, 0.0, 0.0, 0, 0.0
    thr = float(onset.mean() + 0.5 * onset.std())
    interior = onset[1:-1]
    prev_ = onset[:-2]
    next_ = onset[2:]
    is_peak = (interior > prev_) & (interior >= next_) & (interior > thr)
    peaks = np.nonzero(is_peak)[0] + 1
    if peaks.size < 3:
        return 0.0, 0.0, 0.0, 0, 0.0
    intervals = np.diff(peaks) / fps
    implied: List[float] = []
    ratios: List[float] = []
    for iv in intervals:
        r = float(iv) / period
        best = min(SIMPLE_RATIOS, key=lambda s: abs(r - s))
        if abs(r - best) <= tol * best:
            implied.append(float(iv) / best)   # 这个间隔反推出的「一拍有多长」
            ratios.append(best)
    if len(implied) < 3:
        return 0.0, 0.0, 0.0, len(implied), 0.0
    arr_b = np.asarray(implied, dtype=np.float64)
    arr_r = np.asarray(ratios, dtype=np.float64)
    # 只用「细分级」的间隔评规整度：跨 3~4 拍的多半是段落空档、漏检或间奏
    core = arr_b[arr_r <= 2.0]
    if core.size < 3:
        core = arr_b
    mean = float(core.mean())
    std = float(core.std(ddof=0))
    cv = std / mean if mean > 0 else 1.0
    return max(0.0, 1.0 - cv), mean, std, int(core.size), float(np.median(arr_r))


def _analyze_track(path: Path, use_from: Optional[float] = None, use_to: Optional[float] = None) -> Dict[str, Any]:
    """把一首曲子分析成一份数字结论（analyze / grid 共用）。"""
    np = _np()
    data, sr = load_audio(path)
    total = len(data) / float(sr)
    a = 0.0 if use_from is None else max(0.0, float(use_from))
    b = total if use_to is None else min(total, float(use_to))
    if b - a < 2.0:
        C.error(f"分析区间太短（{C.fmt_sec(b - a)}）：至少要 2 秒才能估出节拍。"
                f"当前区间 {C.fmt_sec(a)} ~ {C.fmt_sec(b)}，曲子总长 {C.fmt_sec(total)}")
    seg = data[int(a * sr):int(b * sr)]
    onset, fps = _onset_strength(seg, sr)
    cands = _tempo_candidates(onset, fps, top=5)
    if not cands:
        C.error(f"{C.rel(path)} 估不出节拍（片段里几乎没有起音）：换一首鼓点更清晰的曲子，或换一段有鼓的区间")
    bpm, score, weighted = cands[0]
    period = _refine_period(onset, fps, 60.0 / bpm)
    bpm = 60.0 / period
    phase, phase_score = _beat_phase(onset, fps, period)
    beat_strengths = [_phrase_strength(onset, fps, period, phase, k) for k in range(int(BAR_BEATS))]
    down_idx = int(max(range(len(beat_strengths)), key=lambda i: beat_strengths[i]))
    # + ONSET_BIAS_SEC：起音包络比真实发声早 (frame-hop) 个样点，不减掉会把 downbeat 报早 24ms
    downbeat = phase + down_idx * period + ONSET_BIAS_SEC
    # 相位搜索可能锁在同一个拍网格的另一个拍上（差整数拍，甚至差整小节），
    # 这里往前退到分析区间里最早的那个小节起点——退整小节不改变网格对齐关系。
    bar_sec = period * BAR_BEATS
    if bar_sec > 0:
        while downbeat - bar_sec >= -DOWNBEAT_WALK_TOL:
            downbeat -= bar_sec
        if downbeat < 0:
            downbeat = 0.0
    consistency, mean_iv, std_iv, n_iv, ratio_iv = _consistency(onset, fps, period)
    # 每秒能量（整首曲子，不只分析区间）
    per_sec = _per_second_rms(data, sr)
    return {
        "path": path,
        "samples": data,
        "sr": sr,
        "duration": total,
        "from": a,
        "to": b,
        "onset": onset,
        "fps": fps,
        "bpm": bpm,
        "period": period,
        "candidates": cands,
        "candidate_score": score,
        "candidate_weighted": weighted,
        "phase": phase,
        "phase_score": phase_score,
        "beat_strengths": beat_strengths,
        "downbeat": downbeat,
        "downbeat_index": down_idx,
        "consistency": consistency,
        "interval_mean": mean_iv,
        "interval_std": std_iv,
        "interval_count": n_iv,
        "interval_ratio": ratio_iv,
        "per_second": per_sec,
    }


def _per_second_rms(x: Any, sr: int) -> Any:
    """每秒 RMS（数组，下标 = 第几秒）。"""
    np = _np()
    n_sec = max(1, int(math.ceil(len(x) / float(sr))))
    out = np.zeros(n_sec, dtype=np.float64)
    for i in range(n_sec):
        chunk = x[i * sr:(i + 1) * sr]
        out[i] = float(np.sqrt(np.mean(chunk.astype(np.float64) ** 2))) if chunk.size else 0.0
    return out


def _db(value: float) -> float:
    return max(DB_FLOOR, 20.0 * math.log10(value) if value > 0 else DB_FLOOR)


def _music_of_timing() -> Dict[str, Any]:
    """读 timing.json 的 music 段；读不到就返回 {}（不退出）。"""
    try:
        timing = C.load_timing()
    except SystemExit:
        return {}
    music = timing.get("music")
    return music if isinstance(music, dict) else {}


# ---------------------------------------------------------------------------
# 子命令：analyze
# ---------------------------------------------------------------------------

def _top_moments(per_sec: Any, count: int = 5, min_gap: int = 2) -> List[int]:
    """每秒能量的前 count 个峰（彼此至少隔 min_gap 秒），按时间排序。"""
    order = sorted(range(len(per_sec)), key=lambda i: -float(per_sec[i]))
    picked: List[int] = []
    for i in order:
        if all(abs(i - j) >= min_gap for j in picked):
            picked.append(i)
        if len(picked) >= count:
            break
    return sorted(picked)


def cmd_analyze(args: argparse.Namespace) -> int:
    np = _np()
    if not C.has_cmd("ffprobe"):
        C.warn("没有 ffprobe，采样率/声道信息只能从解码结果推断"
               "（ffprobe 一般跟 ffmpeg 一起装；只有 ffmpeg 也能继续分析）")
    rc = 0
    for raw in args.files:
        path = _check_audio_file(Path(raw))
        info = _analyze_track(path)
        data, sr, dur = info["samples"], info["sr"], info["duration"]
        C.log("=" * 72)
        C.log(f"文件：{C.rel(path)}")
        meta = "（ffprobe 不可用）"
        if C.has_cmd("ffprobe"):
            try:
                pj = C.ffprobe_json(path)
                st = next((s for s in (pj.get("streams") or []) if s.get("codec_type") == "audio"), {})
                fmt = pj.get("format") or {}
                meta = (f"时长 {C.fmt_sec(fmt.get('duration') or dur)}，"
                        f"源采样率 {st.get('sample_rate', '?')}Hz，声道 {st.get('channels', '?')}，"
                        f"编码 {st.get('codec_name', '?')}")
            except SystemExit:
                meta = "（ffprobe 读不出来）"
        C.log(meta)
        C.log(f"BPM 估算：{info['bpm']:.2f}（拍长 {C.fmt_sec(info['period'])}，"
              f"自相关 {info['candidate_score']:.2f}，按 120 BPM 先验加权后 {info['candidate_weighted']:.2f}）")
        others = "、".join(f"{b:.1f}(自相关 {s:.2f})" for b, s, _ in info["candidates"][1:])
        if others:
            C.log(f"其它候选：{others}  ← 电子乐常出现倍频/半频，用 grid 的网格表核对一下再定")
        # 每秒能量条形图
        per_sec = info["per_second"]
        peak = float(per_sec.max()) if per_sec.size else 0.0
        C.log("")
        C.log(f"每秒能量（相对最强的一秒；每格 = 1/{BAR_CHART_WIDTH}）")
        for i, v in enumerate(per_sec):
            ratio = (float(v) / peak) if peak > 0 else 0.0
            bar = "#" * int(round(ratio * BAR_CHART_WIDTH))
            C.log(f"  {i:3d}s  {_db(float(v)):7.1f} dB  {bar}")
        # 候选 drop
        moments = _top_moments(per_sec, 5, 2)
        C.log("")
        C.log("能量最高的 5 个时刻（候选 drop，都是音乐时间）：")
        for i in moments:
            C.log(f"  第 {i:3d} 秒（{_db(float(per_sec[i])):6.1f} dB，相对最强 {float(per_sec[i]) / peak * 100:5.1f}%）")
        # 第一次爆发是否够早
        first_strong = None
        for i, v in enumerate(per_sec):
            if peak > 0 and float(v) >= 0.5 * peak:
                first_strong = i
                break
        if first_strong is None:
            # rc=1 会让脚本以非零码退出，AGENTS.md 第 7 节要求这种时候必须有一行
            # `ERROR: ...`（code=0 只打印不退出，报告照旧打完）。
            C.error("整首曲子没有明显能量峰值：建议换一首鼓点更清晰的曲子（AGENTS.md 阶段 5）", code=0)
            rc = 1
        else:
            C.log("")
            C.log(f"第一次能量爆发（≥最强值一半）出现在第 {first_strong} 秒。")
            if first_strong > FIRST_DROP_MAX_SEC:
                C.error(f"AGENTS.md 阶段 5 要求最晚第 {FIRST_DROP_MAX_SEC:.0f} 秒完成第一次爆发，"
                        f"这首要等到 {first_strong} 秒：要么从爆发的部分剪起（music.py cut），"
                        f"要么换一首前奏更短的曲子。", code=0)
                rc = 1
            else:
                C.log(f"OK: 符合「最晚第 {FIRST_DROP_MAX_SEC:.0f} 秒完成第一次爆发」")
        if info["consistency"] < CONSISTENCY_WARN:
            C.warn(f"节拍一致性偏低（{info['consistency']:.2f}）：这首曲子节拍不规整，"
                   f"卡点会显得拖。建议换鼓点更清晰的曲子，或改用「按小节能量切镜头」而不是逐拍对齐。")
        C.log("")
        C.log(f"下一步：python scripts/music.py grid {C.rel(path)} --from 0 --to 30   # 拿 beat/bar/downbeat")
    return rc


# ---------------------------------------------------------------------------
# 子命令：grid
# ---------------------------------------------------------------------------

def cmd_grid(args: argparse.Namespace) -> int:
    path = _check_audio_file(Path(args.file))
    info = _analyze_track(path, args.from_sec, args.to_sec)
    period = info["period"]
    bar = period * BAR_BEATS
    music = _music_of_timing()
    drop = args.drop
    drop_src = "--drop"
    if drop is None and music.get("drop") is not None:
        try:
            drop = float(music["drop"])
            drop_src = "src/timing.json 的 music.drop"
        except (TypeError, ValueError):
            drop = None

    C.log(f"文件：{C.rel(path)}（分析区间 {C.fmt_sec(info['from'])} ~ {C.fmt_sec(info['to'])}）")
    C.log("")
    C.log(f"BPM          ：{info['bpm']:.2f}")
    C.log(f"beat（拍长）  ：{period:.4f} 秒   ← 填 timing.json 的 music.beat")
    C.log(f"bar（小节长） ：{bar:.4f} 秒   ← 填 music.bar（= {BAR_BEATS:.0f} beat）")
    C.log(f"第一个强拍    ：{info['downbeat']:.3f} 秒（音乐时间，从区间起点算）")
    C.log(f"节拍一致性    ：{info['consistency']:.2f}（1 - 标准差/均值；"
          f"起音间隔中位 {info['interval_ratio']:.2f} 拍，"
          f"由间隔反推的拍长 {C.fmt_sec(info['interval_mean'])}"
          f"± {C.fmt_sec(info['interval_std'])}，用了 {info['interval_count']} 个间隔）")

    C.log("")
    C.log("4 个拍位的平均起音能量（高的那个就是 downbeat，即每小节第 1 拍）：")
    for k, s in enumerate(info["beat_strengths"]):
        mark = "  <= downbeat" if k == info["downbeat_index"] else ""
        C.log(f"  第 {k + 1} 拍：{s:.3f}{mark}")

    C.log("")
    C.log("前 8 拍的时间轴（音乐时间 → 视频时间 = 音乐时间 + offset）：")
    np = _np()
    onset, fps = info["onset"], info["fps"]
    pooled = _pool(onset)
    for i in range(8):
        t = info["downbeat"] + i * period
        if t > info["to"] - info["from"]:
            break
        # 包络的下标要减掉 ONSET_BIAS_SEC：downbeat 已经补偿过，直接用它会比峰位晚 3 帧、
        # 落在衰减尾巴上（合成轨实测：重音拍显示 0.23 而不是 0.75）。
        idx = int(round((t - ONSET_BIAS_SEC) * fps))
        if 0 <= idx < len(pooled):
            strength = f"{float(pooled[idx]):.3f}"
        else:
            strength = "—（贴区间起点，包络还没开始，测不到）"
        beat_no = i % int(BAR_BEATS) + 1
        C.log(f"  {t:7.3f}s  第 {i + 1:2d} 拍（小节内第 {beat_no} 拍）  强度 {strength}")

    if info["consistency"] < CONSISTENCY_WARN:
        C.warn(f"节拍一致性 {info['consistency']:.2f} < {CONSISTENCY_WARN}：这首曲子节拍不规整。"
               f"建议换鼓点更清晰的曲子；若必须用，把卡点从「逐拍」改成「逐小节」（用 bars 的 DROP? 标记切镜头）。")
    elif info["consistency"] < CONSISTENCY_MIN:
        C.warn(f"节拍一致性 {info['consistency']:.2f} < {CONSISTENCY_MIN}（AGENTS.md 阶段 5 的闸门）："
               f"勉强能用，但别做密集逐拍切换。")

    C.log("")
    C.log("写回 src/timing.json 的 music 段：")
    C.log(f'  "beat": {period:.4f}, "bar": {bar:.4f},')
    C.log(f'  "offset": <下面算出来的>, "drop": <第一次爆发的视频时间，默认看 timing.json 现在是 '
          f'{drop if drop is not None else "未设置"}>')

    C.log("")
    if drop is None:
        C.log("musicOffset 换算（把 --drop 或 timing.json 的 music.drop 给我，我就能算出具体数字）：")
        C.log(f"  offset = drop - downbeat = drop - {info['downbeat']:.3f}")
        C.log("  （drop 是**视频时间**：你想让画面第几秒爆发就填几。把它写成音乐时间是最常见的错，"
              "写完用下面的 bars 复核。）")
    else:
        v = drop - info["downbeat"]
        C.log(f"musicOffset 换算（drop = {drop:.3f} 秒，来自{drop_src}）：")
        C.log(f"  offset = drop - downbeat = {drop:.3f} - {info['downbeat']:.3f} = {v:+.3f}")
        C.log(f"  ↑ 语义：视频时间 = 音乐时间 + offset。混音脚本会把音乐文件摆在视频 {v:+.3f} 秒处起播，"
              f"好让音乐里的那个重拍（音乐 {info['downbeat']:.3f} 秒）正好落在画面第 {drop:.3f} 秒。"
              + ("offset 为负 = 要跳过音乐开头这一小段，写 timing.json 的 music.segments 或让 mix.py 从 "
                 f"{abs(v):.3f} 秒处截。" if v < 0 else ""))
        C.log(f"  填完以后验证：python scripts/music.py bars <曲子> --beat {period:.4f} "
              f"--downbeat {info['downbeat']:.3f} --offset {v:+.3f}，"
              f"看「视频时间」列上第 {drop:.1f} 秒附近是不是 DROP? 那一小节。")
    C.log("")
    C.log("注意：downbeat 是从分析区间起点算的。如果你用 --from 截了一段，"
          "它对应的是整首曲子的第 --from 秒 + 上面这个数。")
    return 0


# ---------------------------------------------------------------------------
# 子命令：bars
# ---------------------------------------------------------------------------

def _band_rms(x: Any, sr: int, band: Optional[Tuple[float, float]]) -> Any:
    """整段信号在某个频段的 RMS；band=None 表示全频。"""
    np = _np()
    if band is None:
        return float(np.sqrt(np.mean(x.astype(np.float64) ** 2)))
    signal = _signal()
    nyq = sr / 2.0
    low = max(1e-6, band[0] / nyq)
    high = min(0.999, band[1] / nyq)
    if low >= high:
        return 0.0
    sos = signal.butter(4, [low, high], btype="bandpass", output="sos")
    y = signal.sosfiltfilt(sos, x.astype(np.float64))
    return float(np.sqrt(np.mean(y ** 2)))


def cmd_bars(args: argparse.Namespace) -> int:
    np = _np()
    path = _check_audio_file(Path(args.file))
    beat = float(args.beat)
    downbeat = float(args.downbeat)
    if beat <= 0:
        C.error(f"--beat 必须 > 0（现在 {beat}）。用 python scripts/music.py grid <文件> 拿这个数")
    beats_per_bar = float(args.beats_per_bar)
    bar = beat * beats_per_bar
    count = int(args.count)
    if count <= 0:
        C.error(f"--count 必须 > 0（现在 {count}）")

    music_cfg = _music_of_timing()
    offset = args.offset
    offset_src = "--offset"
    if offset is None:
        try:
            offset = float(music_cfg.get("offset", 0.0))
            offset_src = "src/timing.json 的 music.offset"
        except (TypeError, ValueError):
            offset = 0.0
            offset_src = "默认 0"

    data, sr = load_audio(path)
    total = len(data) / float(sr)
    C.log(f"文件：{C.rel(path)}（{C.fmt_sec(total)}）")
    C.log(f"beat {C.fmt_sec(beat)} × {beats_per_bar:.0f} = bar {C.fmt_sec(bar)}；"
          f"downbeat {C.fmt_sec(downbeat)}；offset {offset:+.3f}（来自{offset_src}）")
    C.log(f"视频时间 = 音乐时间 + offset；下面每行的「音乐时间」是小节起点")

    rows: List[List[str]] = []
    drop_flags: List[int] = []
    full_dbs: List[float] = []
    for i in range(count):
        t0 = downbeat + i * bar
        t1 = t0 + bar
        s0, s1 = int(t0 * sr), int(t1 * sr)
        if s0 >= len(data):
            break
        chunk = data[max(0, s0):min(len(data), s1)]
        if chunk.size == 0:
            break
        full = _band_rms(chunk, sr, None)
        low = _band_rms(chunk, sr, BAND_LOW)
        high = _band_rms(chunk, sr, BAND_HIGH)
        full_db = _db(full)
        full_dbs.append(full_db)
        prev = full_dbs[max(0, i - 2):i]
        is_drop = bool(prev) and full_db >= (sum(prev) / len(prev)) + DROP_RISE_DB
        if is_drop:
            drop_flags.append(i)
        rows.append([
            str(i + 1),
            f"{t0:.3f}",
            f"{t0 + offset:.3f}",
            f"{full_db:.1f}",
            f"{_db(low):.1f}",
            f"{_db(high):.1f}",
            "DROP?" if is_drop else "",
        ])
    if not rows:
        C.error("一个小节都算不出来：检查 --beat / --downbeat 是不是写反了（downbeat 不该比 beat 大很多）")

    C.log("")
    C.log("  小节   音乐时间   视频时间   全频dB   低频dB   高频dB   ")
    C.log("  ----   ---------  ---------  -------  -------  -------  ------")
    for r in rows:
        C.log("  " + "  ".join([r[0].rjust(4), r[1].rjust(9), r[2].rjust(9),
                                r[3].rjust(7), r[4].rjust(7), r[5].rjust(7), "  " + r[6]]).rstrip())

    C.log("")
    if drop_flags:
        C.log(f"能量突增的小节（比前两小节均值高 ≥{DROP_RISE_DB:.0f}dB）："
              + "、".join(f"第 {i + 1} 小节（视频 {downbeat + i * bar + offset:.2f}s）" for i in drop_flags))
    else:
        C.log(f"没有小节能量突增 ≥{DROP_RISE_DB:.0f}dB：整首比较平，"
              f"建议用下面能量最高的几个小节切镜头，别指望 drop。")
    # 切镜头建议：只推「能量接近全场最高」的小节（离最高值 3dB 以内），
    # 否则平曲子会把 -21dB 的小节也列成「适合切镜头」，等于没建议。
    ranked = sorted(range(len(full_dbs)), key=lambda i: -full_dbs[i])
    best_db = full_dbs[ranked[0]]
    top_cut = [i for i in ranked if full_dbs[i] >= best_db - CUT_BAR_TOLERANCE_DB][:CUT_BAR_MAX]
    padded = False
    if len(top_cut) < 2:
        # 只有一个小节能量突出（典型的「前奏 + 一个大 drop」结构）：补齐 2 个时要说清楚
        # 第二个是凑数的，否则 AI 会以为次高的小节也值得切镜头。
        top_cut = ranked[:min(2, len(ranked))]
        padded = True
    C.log("")
    if padded and len(ranked) > 1:
        C.log(f"适合切镜头的小节：只有第 {ranked[0] + 1} 小节能量真正突出"
              f"（全场最高 {best_db:.1f} dB，其余都比它低 {best_db - full_dbs[ranked[1]]:.1f} dB 以上）；"
              f"下面第 2 个只是次高，切镜头前先听一下：")
    else:
        C.log(f"适合切镜头的小节（离全场最高 {CUT_BAR_TOLERANCE_DB:.0f} dB 以内，镜头切在小节起点上最稳）：")
    for i in sorted(top_cut):
        C.log(f"  第 {i + 1:2d} 小节：音乐 {downbeat + i * bar:.3f}s → 视频 {downbeat + i * bar + offset:.3f}s"
              f"（全频 {full_dbs[i]:.1f} dB，比全场最高低 {best_db - full_dbs[i]:.1f} dB）")
    C.log("")
    C.log("用法：把「视频时间」和场景/镜头的切换点对齐（AGENTS.md 第 4 节的节奏底线：重点镜头 ≥1.2s、"
          "快切 ≥0.5s、CTA ≥2.5s）；能量最高处留给网址/CTA（AGENTS.md 阶段 5）。")
    return 0


# ---------------------------------------------------------------------------
# 子命令：cut
# ---------------------------------------------------------------------------

def cmd_cut(args: argparse.Namespace) -> int:
    src = _check_audio_file(Path(args.input))
    dst = Path(args.output)
    start = float(args.start)
    dur = float(args.dur)
    fade = float(args.fade)
    if start < 0:
        C.error(f"--start 不能是负数（现在 {start}）")
    if dur <= 0:
        C.error(f"--dur 必须 > 0（现在 {dur}）")
    if fade < 0:
        C.error(f"--fade 不能是负数（现在 {fade}）")
    if fade * 2 > dur:
        C.error(f"--fade {fade}s 的淡入+淡出比片段本身还长（--dur {dur}s）。把 --fade 调到 {dur / 2:.2f} 以内")
    C.ensure_dir(dst.parent)

    ext = dst.suffix.lower()
    codec: List[str] = []
    if ext == ".wav":
        codec = ["-acodec", "pcm_s16le"]
    elif ext == ".mp3":
        codec = ["-acodec", "libmp3lame", "-q:a", "2"]
    elif ext in (".m4a", ".aac"):
        codec = ["-acodec", "aac", "-b:a", "192k"]
    elif ext in (".opus", ".ogg"):
        codec = ["-acodec", "libopus", "-b:a", "160k"]
    elif ext == ".flac":
        codec = ["-acodec", "flac"]
    elif ext:
        C.warn(f"不认识的扩展名 {ext}：让 ffmpeg 自己选编码")
    else:
        C.error("输出文件要带扩展名，例如 out/audition-a.mp3")

    filters = []
    if fade > 0:
        filters.append(f"afade=t=in:st=0:d={fade:.3f}")
        filters.append(f"afade=t=out:st={max(0.0, dur - fade):.3f}:d={fade:.3f}")
    cmd = ["-ss", f"{start:.3f}", "-i", str(src), "-t", f"{dur:.3f}", "-vn"]
    if filters:
        cmd += ["-af", ",".join(filters)]
    cmd += codec + [str(dst)]
    C.log(f"剪 {C.fmt_sec(start)} ~ {C.fmt_sec(start + dur)}（{C.fmt_sec(dur)}，淡入淡出 {C.fmt_sec(fade)}）-> {C.rel(dst)}")
    C.ffmpeg(*cmd)
    if not dst.exists() or dst.stat().st_size == 0:
        C.error(f"ffmpeg 没有产出文件：{C.rel(dst)}")
    real = C.probe_duration(dst)
    C.log(f"OK: {C.rel(dst)}  {C.human(dst.stat().st_size)}"
          + (f"  实际时长 {C.fmt_sec(real)}" if real else "（没装 ffprobe，跳过时长复核）"))
    if real is not None and abs(real - dur) > 0.1:
        C.warn(f"实际时长 {C.fmt_sec(real)} 和要求的 {C.fmt_sec(dur)} 差 {abs(real - dur):.2f}s："
               f"是不是 --start 超出了原曲长度？")
    C.log(f"下一步：把候选片段都剪成同样长度，叠上人声给用户试听（AGENTS.md 阶段 5：禁止 AI 自己拍板选曲）")
    return 0


# ---------------------------------------------------------------------------
# 子命令：get
# ---------------------------------------------------------------------------

def _filename_from(resp: Any, url: str, name: Optional[str]) -> str:
    base = ""
    if name:
        base = name.strip()
    else:
        cd = resp.headers.get("Content-Disposition", "") or ""
        m = re.search(r"filename\*?\s*=\s*(?:UTF-8''|\"|')?([^\"';]+)", cd, re.I)
        if m:
            base = unquote(m.group(1).strip())
        if not base:
            base = unquote(os.path.basename(urlparse(url).path)) or "track"
    base = re.sub(r"[\\/:*?\"<>|\x00-\x1f]", "_", base).strip() or "track"
    if not Path(base).suffix or Path(base).suffix.lower() not in AUDIO_EXTS:
        ct = (resp.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        base += EXT_BY_CT.get(ct, ".mp3")
    return base


def cmd_get(args: argparse.Namespace) -> int:
    req = _requests()
    url = args.url
    if not url.lower().startswith(("http://", "https://")):
        C.error(f"get 只接受 http/https 直链，收到的是 {url!r}。"
                f"（本脚本不做任何音乐网站爬虫：请从免费可商用曲库下载，或找官方直链）")
    dest = C.ensure_dir(args.out)
    proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy") or os.environ.get("HTTP_PROXY")
    if proxy:
        C.log(f"走代理：{re.sub(r'//[^@]*@', '//***@', proxy)}（来自 HTTPS_PROXY/HTTP_PROXY 环境变量）")
    try:
        resp = req.get(url, stream=True, timeout=DL_TIMEOUT, allow_redirects=True,
                       headers={"User-Agent": DL_UA, "Accept": "audio/*,*/*;q=0.8"})
    except Exception as exc:
        C.error(f"下载失败：{type(exc).__name__}: {exc}")
    with resp:
        if resp.status_code != 200:
            C.error(f"下载失败：HTTP {resp.status_code} {resp.reason}（{url}）")
        ctype = (resp.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if ctype.startswith("text/") or ctype in ("application/json", "application/xml"):
            C.error(f"这个直链返回的是 {ctype}，不是音频（多半是需要登录的页面或防盗链）。"
                    f"请在浏览器里点开确认能直接播放，再复制真实音频地址。")
        name = _filename_from(resp, url, args.name)
        target = dest / name
        if target.exists() and not args.force:
            C.error(f"{C.rel(target)} 已经存在。要么换个 --name，要么加 --force 覆盖")
        try:
            total = int(resp.headers.get("Content-Length") or 0)
        except ValueError:
            total = 0
        done = 0
        try:
            with open(target, "wb") as fh:
                for chunk in resp.iter_content(chunk_size=DL_CHUNK):
                    if not chunk:
                        continue
                    fh.write(chunk)
                    done += len(chunk)
                    if total:
                        sys.stdout.write(f"\r  下载中 {done * 100.0 / total:5.1f}%  "
                                         f"{C.human(done)} / {C.human(total)}   ")
                    else:
                        sys.stdout.write(f"\r  下载中 {C.human(done)}   ")
                    sys.stdout.flush()
        except Exception as exc:
            try:
                target.unlink(missing_ok=True)
            except OSError:
                pass
            C.error(f"下载中断：{type(exc).__name__}: {exc}")
        sys.stdout.write("\r" + " " * 60 + "\r")
        sys.stdout.flush()
    if done == 0:
        target.unlink(missing_ok=True)
        C.error("下到的文件是空的")
    if done < 20000:
        C.warn(f"文件只有 {C.human(done)}：不像一整首曲子（可能是试听片段或错误页），请确认后再用")
    dur = C.probe_duration(target) if C.has_cmd("ffprobe") else None
    C.log(f"OK: {C.rel(target)}  {C.human(done)}" + (f"  时长 {C.fmt_sec(dur)}" if dur else ""))
    if dur is None and not C.has_cmd("ffprobe"):
        C.warn("没有 ffprobe，没法复核时长（下载本身已经成功，用 analyze 也一样能拿到时长）")
    C.log(f"下一步：python scripts/music.py analyze {C.rel(target)}；"
          f"并把出处与授权写进 调研笔记.md（AGENTS.md 阶段 4/5）")
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="music.py",
        description="配乐分析：BPM / 节拍网格 / 逐小节能量 / 剪段 / 直链下载。只用 numpy+scipy，不需要 librosa。",
        epilog=(
            "例：\n"
            "  python scripts/music.py analyze audio-src/music/track.mp3\n"
            "  python scripts/music.py grid audio-src/music/track.mp3 --from 0 --to 30\n"
            "  python scripts/music.py bars audio-src/music/track.mp3 --beat 0.4615 --downbeat 0.23\n"
            "  python scripts/music.py cut audio-src/music/track.mp3 out/audition-a.mp3 --start 22 --dur 15\n"
            "  python scripts/music.py get \"https://example.com/free-track.mp3\" --name candidate-a\n"
            "\n"
            "选曲来源：本脚本不做任何音乐网站爬虫。请从免费可商用曲库（YouTube Audio Library、Pixabay、\n"
            "Free Music Archive、ccMixter 等）自行下载到 audio-src/music/，或用 get 直链下载你已经获得授权的音频；\n"
            "每首歌的出处、作者、授权写进 调研笔记.md（AGENTS.md 阶段 4/5 硬性要求）。"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--version", action="version", version="music.py 1.0")
    sub = p.add_subparsers(dest="cmd", required=True, metavar="子命令")

    a = sub.add_parser("analyze", help="时长/采样率/声道/BPM/每秒能量/候选 drop",
                       description="用 ffmpeg 解码后做自相关估 BPM，并打印每秒 RMS 的 ASCII 条形图与最猛的 5 个时刻。",
                       epilog="例：python scripts/music.py analyze audio-src/music/track.mp3")
    a.add_argument("files", nargs="+", metavar="文件", help="音频文件（mp3/wav/m4a/flac…）")
    a.set_defaults(func=cmd_analyze)

    g = sub.add_parser("grid", help="拍长/小节长/一致性/downbeat 与 offset 换算",
                       description="估出 beat/bar/BPM/节拍一致性，找第一个强拍（downbeat），并给出 musicOffset 换算与 timing.json 片段。",
                       epilog="例：python scripts/music.py grid audio-src/music/track.mp3 --from 0 --to 30 --drop 3.6")
    g.add_argument("file", metavar="文件", help="音频文件")
    g.add_argument("--from", dest="from_sec", type=float, default=0.0, metavar="秒",
                   help="分析区间起点（秒），默认 0；只分析前奏/主歌能提高准确度")
    g.add_argument("--to", dest="to_sec", type=float, default=30.0, metavar="秒",
                   help="分析区间终点（秒），默认 30")
    g.add_argument("--drop", type=float, default=None, metavar="秒",
                   help="第一次爆发的时间；不给就读 src/timing.json 的 music.drop")
    g.set_defaults(func=cmd_grid)

    b = sub.add_parser("bars", help="逐小节全频/低频/高频能量表",
                       description="按 beat/downbeat 切小节，用 scipy 带通 + RMS 算每小节能量，标出 DROP? 并给切镜头建议。",
                       epilog="例：python scripts/music.py bars audio-src/music/track.mp3 --beat 0.4615 --downbeat 0.23 --count 16")
    b.add_argument("file", metavar="文件", help="音频文件")
    b.add_argument("--beat", type=float, required=True, metavar="秒", help="拍长（秒），来自 grid 的 music.beat")
    b.add_argument("--downbeat", type=float, required=True, metavar="秒", help="第一个强拍（音乐秒），来自 grid")
    b.add_argument("--offset", type=float, default=None, metavar="秒",
                   help="视频时间 = 音乐时间 + offset；不给就读 src/timing.json 的 music.offset")
    b.add_argument("--count", type=int, default=16, metavar="小节数", help="算几个小节，默认 16")
    b.add_argument("--beats-per-bar", type=float, default=BAR_BEATS, metavar="拍数",
                   help=f"每小节几拍，默认 {BAR_BEATS:.0f}（AGENTS.md 第 5 节：bar 通常是 4 beat）")
    b.set_defaults(func=cmd_bars)

    c = sub.add_parser("cut", help="剪一段并加淡入淡出",
                       description="用 ffmpeg 剪出指定的一小段并加 afade 淡入淡出，用来做试听片段。",
                       epilog="例：python scripts/music.py cut audio-src/music/track.mp3 out/audition-a.mp3 --start 22 --dur 15 --fade 0.4")
    c.add_argument("input", metavar="输入音频", help="源音频文件")
    c.add_argument("output", metavar="输出音频", help="输出文件（扩展名决定编码：.wav/.mp3/.m4a/.opus/.flac）")
    c.add_argument("--start", type=float, required=True, metavar="音乐秒", help="从第几秒开始剪（音乐时间）")
    c.add_argument("--dur", type=float, required=True, metavar="秒", help="剪多长")
    c.add_argument("--fade", type=float, default=0.3, metavar="秒", help="淡入淡出秒数，默认 0.3（0 = 不淡）")
    c.set_defaults(func=cmd_cut)

    d = sub.add_parser("get", help="直链下载已授权的音频",
                       description="流式下载 http/https 直链音频，显示进度，支持 HTTPS_PROXY 环境变量。不做网站爬虫。",
                       epilog="例：python scripts/music.py get \"https://example.com/free-track.mp3\" -o audio-src/music --name candidate-a\n"
                              "只下载你已经获得授权的音频；版权与出处要记进 调研笔记.md。")
    d.add_argument("url", metavar="URL", help="音频直链（http/https）")
    d.add_argument("-o", "--out", default="audio-src/music", help="输出目录，默认 audio-src/music")
    d.add_argument("--name", default=None, help="输出文件名（不给就从 Content-Disposition 或 URL 推）")
    d.add_argument("--force", action="store_true", help="同名文件已存在时覆盖")
    d.set_defaults(func=cmd_get)
    return p


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args) or 0)


if __name__ == "__main__":
    sys.exit(main())
