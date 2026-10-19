#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""promo-forge · 阶段 7「配音与字幕」的配音生成与审核。

用途
----
按 ``src/timing.json`` 的 ``voice.lines`` 逐句合成旁白：

1. 调 TTS 引擎（默认免费 ``edge-tts``；可选 ElevenLabs）生成原始音频；
2. 用 ffmpeg 切掉首尾静音，并把判为拖音的尾部低能量真正截掉（留 15ms 淡出）；
3. 测量时长并与该句窗口 ``line_window()`` 比对，超出就最多加速 12%（超过就报 ``TOO_LONG``，
   **必须回 timing.json 删字改稿**，不允许硬压）；
4. 按 ``references/配音与字幕.md`` 的口径测尾部低能量拖音（人声平均电平 -40 dB），
   > 250ms 判 ``ECHO`` 并自动截断；截断量写进 report.json 的 ``tail_trimmed_ms``；
5. 结果写进 ``audio-src/vo/report.json``，缓存写进 ``audio-src/vo/cache.json``。

AI 听不到声音，所以音色必须由用户试听拍板：``preview`` 子命令就是为这件事准备的。

典型调用
--------
    python scripts/tts.py all                       # 全部句子（默认 edge，免费）
    python scripts/tts.py all --only 01,03          # 只做 01、03（改稿后局部重生成）
    python scripts/tts.py all --force               # 忽略缓存全部重生成（换音色后用）
    python scripts/tts.py all --engine eleven       # ElevenLabs（需 ELEVENLABS_API_KEY）
    python scripts/tts.py one 02                    # 只做一句
    python scripts/tts.py audit                     # 复核时长/拖音，TOO_LONG/MISSING → 退出码 1
    python scripts/tts.py voices --lang zh          # 列出候选音色
    python scripts/tts.py preview --text "试试这句" # 多音色试听，交用户选，写回 voice.voice
    python scripts/tts.py stt audio-src/vo/01.mp3   # 可选：转写回读校验（需 faster-whisper）

依赖
----
必需：ffmpeg / ffprobe（``doctor.py`` 会体检）。
edge 引擎：``pip install edge-tts``；eleven 引擎：``pip install requests`` + ``ELEVENLABS_API_KEY``
（环境变量，或项目根 ``.env`` 文件）。
可选：``pip install faster-whisper`` —— 只有 ``stt`` 子命令需要。``stt`` 是**校验**命令：
没装依赖 / 转写失败 / 找不到对照稿，都算「没校验成」，一律打印 ``ERROR:`` 并以退出码 1 结束。

退出码
------
0 成功（含 ``ECHO``，它只是提醒人工确认）；
1 有 ERROR：依赖缺失、生成失败、``TOO_LONG``、``MISSING``、参数错误。
"""

from __future__ import annotations

import argparse
import array
import asyncio
import difflib
import hashlib
import json
import math
import os
import re
import subprocess
import sys
import time
import unicodedata
from pathlib import Path

# ---------------------------------------------------------------------------
# 导入公共库 scripts/_common.py（由另一位维护者提供，本脚本不修改它）
# ---------------------------------------------------------------------------
sys.path.insert(0, str(Path(__file__).resolve().parent))
try:  # pragma: no cover - 只在 _common.py 缺失时走到
    from _common import (  # type: ignore
        PROJECT_ROOT,
        ensure_dir,
        error,
        ffprobe_json,
        fmt_sec,
        has_cmd,
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
# 阈值常量（每条都对应 AGENTS.md 的一处硬性要求，改这里前先改文档）
# ===========================================================================

# AGENTS.md 阶段 7：「必要时最多加速 12%」——超过就必须改稿，不许硬压。
MAX_TEMPO = 1.12

# 复用旧音频的宽松度：实测时长 / 当前窗口 ≤ 这个比值才复用。
# atempo 与时长的测量精度在毫秒级，2% 的余量（约 50ms，远小于字幕收起留的 0.15s）
# 避免每跑一次 all 都因为零点几毫秒的误差重新合成。
REUSE_FIT_TOLERANCE = 1.02

# AGENTS.md 阶段 7 / 校验闸门：尾部低能量拖音 > 250ms 判 ECHO。
TAIL_ECHO_MS = 250.0
# 判定「低能量」的参考线：低于**语音帧平均电平** TAIL_REF_DB dB 即视为拖音。
# 旧实现用「峰值帧 - 25 dB」，比 references/配音与字幕.md 写的「人声平均电平 -40 dB」
# 严得多：实测峰值 -9.0 / 平均 -14.1 dBFS 的信号，尾音 -36 dB 会被旧线判成 1000ms
# 拖音并报 ECHO（按文档线 -54.1 dB 根本不算）。现在与文档对齐。
TAIL_REF_DB = 40.0

# RMS 包络的帧长（毫秒）。10ms 足够分辨拖音，又不会太慢。
FRAME_MS = 10
# 计算 RMS 时统一重采样到的采样率，越小越快，语音够用。
ENV_SR = 16000

# AGENTS.md 阶段 7：语速按 5.5~6.5 字/秒写，脚本按 6.5 字/秒保守预估。
CHARS_PER_SEC = 6.5

# 首尾静音切割阈值：低于 -45dB 视为静音（与 check_video.py 的空档阈值无关）。
SILENCE_DB = -45

# 网络：最多 3 次、指数退避（AGENTS.md 总原则「失败要看得懂」的要求）。
NET_RETRIES = 3
NET_BACKOFF_S = 2.0
NET_TIMEOUT_S = 60.0

# 单条语音最长允许的合成时间（秒）；edge-tts 偶发卡死时兜底。
SYNTH_TIMEOUT_S = 120.0

VO_DIRNAME = "audio-src/vo"
REPORT_NAME = "report.json"
CACHE_NAME = "cache.json"
PREVIEW_DIRNAME = "out/vo-preview"

# 合成时统一重编码的码率（VO 是人声，192k 足够且不浪费空间）。
VO_BITRATE = "192k"

# `voices` 子命令的网络失败兜底清单：AGENTS.md 要求至少覆盖这几个常用中文音色。
FALLBACK_VOICES = [
    ("zh-CN-XiaoxiaoNeural", "Female", "zh-CN", "温暖亲和的女声，通用旁白首选"),
    ("zh-CN-XiaoyiNeural", "Female", "zh-CN", "活泼年轻的女声，适合消费类产品"),
    ("zh-CN-YunxiNeural", "Male", "zh-CN", "清亮年轻的男声，科技/工具类产品常用"),
    ("zh-CN-YunjianNeural", "Male", "zh-CN", "沉稳有力的男声，适合解说与体育"),
    ("zh-CN-YunyangNeural", "Male", "zh-CN", "标准新闻播报男声，正式稳重"),
    ("zh-CN-liaoning-XiaobeiNeural", "Female", "zh-CN-liaoning", "东北口音女声，接地气"),
    ("zh-TW-HsiaoChenNeural", "Female", "zh-TW", "台湾国语女声，柔和"),
    ("zh-CN-YunxiaNeural", "Male", "zh-CN", "少年感男声，轻快"),
    ("zh-CN-XiaohanNeural", "Female", "zh-CN", "温柔知性女声"),
]

# `preview` 默认候选音色（3~5 个，风格差异要拉得开才方便用户选）。
DEFAULT_PREVIEW_VOICES = [
    "zh-CN-XiaoxiaoNeural",
    "zh-CN-YunxiNeural",
    "zh-CN-YunjianNeural",
    "zh-CN-YunyangNeural",
    "zh-CN-XiaoyiNeural",
]

# 转写校验的相似度门槛（difflib.SequenceMatcher.ratio()）。
STT_WARN_RATIO = 0.85
STT_ERROR_RATIO = 0.60


# ===========================================================================
# 小工具（_common 里没有的，按约定在本文件内用局部私有函数实现）
# ===========================================================================

_PCM_CACHE: dict = {}


def _force_utf8() -> None:
    """Windows 控制台默认 GBK，中文/emoji 会炸；这里兜底成 UTF-8。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except Exception:
            pass


def _disp_width(text: str) -> int:
    """等宽终端下的显示宽度：中日韩全角字符算 2 列。"""
    width = 0
    for ch in text:
        width += 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
    return width


def _pad(text: str, width: int) -> str:
    return text + " " * max(0, width - _disp_width(text))


def _clip(text: str, limit: int) -> str:
    text = (text or "").replace("\n", " ")
    return text if _disp_width(text) <= limit else text[: max(1, limit // 2)] + "…"


def _rel(path: Path) -> str:
    """相对项目根的路径（写进 report.json 用，跨机器可读）。"""
    try:
        return path.resolve().relative_to(PROJECT_ROOT.resolve()).as_posix()
    except Exception:
        return path.as_posix()


def _sha1(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def _cache_key(text: str, engine: str, voice: str, rate: str,
               settings=None, model: str = "") -> str:
    """AGENTS.md 阶段 7：文本/音色/引擎/语速任一变化都要重生成。

    ElevenLabs 还要把 ``voice.settings`` 与 ``model`` 算进去：它们和音色一样决定声音，
    不进键的话改 stability / similarity_boost / model 后仍会静默复用旧音频（用户以为
    参数生效了，实际听到的还是上一版；eleven 是付费接口，白花一次调用钱）。
    edge 的键保持旧算法不变——否则已有项目升级后缓存全部失效、被无谓地重跑一遍。
    """
    parts = [text or "", engine or "", voice or "", rate or ""]
    if (engine or "").strip().lower() == "eleven":
        parts.append(json.dumps(settings or {}, sort_keys=True, ensure_ascii=False))
        parts.append(model or "")
    return _sha1("\x00".join(parts))


def _load_dotenv() -> None:
    """把项目根 ``.env`` 里的键值读进 os.environ（不覆盖已有环境变量）。

    只支持 ``KEY=VALUE`` / ``export KEY=VALUE`` 两种写法，避免引入 python-dotenv 依赖。
    """
    env_path = PROJECT_ROOT / ".env"
    if not env_path.is_file():
        return
    try:
        raw = env_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export ") :]
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def _net_hint() -> str:
    """网络失败时的中文排查提示。"""
    proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy") or ""
    if proxy:
        return f"当前代理 HTTPS_PROXY={proxy}，请确认代理可用，或临时 unset 后重试"
    return (
        "请检查网络连通性；如需代理请设置 HTTPS_PROXY=http://127.0.0.1:7890 后重试"
        "（Windows: set HTTPS_PROXY=...，macOS/Linux: export HTTPS_PROXY=...）"
    )


def _with_retry(what: str, fn):
    """最多 NET_RETRIES 次、指数退避。SDK 自带的超时由调用方在 fn 内处理。"""
    last_exc = None
    for attempt in range(1, NET_RETRIES + 1):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 - 网络层异常类型五花八门
            last_exc = exc
            if attempt < NET_RETRIES:
                delay = NET_BACKOFF_S ** (attempt - 1)
                warn(f"{what} 第 {attempt}/{NET_RETRIES} 次失败：{exc}；{delay:.0f} 秒后重试")
                time.sleep(delay)
    raise RuntimeError(f"{what} 连续 {NET_RETRIES} 次失败：{last_exc}")


def _forget_pcm(path: Path) -> None:
    """原地替换某个音频文件后，清掉它所有版本的 PCM 缓存。"""
    prefix = str(path)
    for key in [k for k in _PCM_CACHE if k[0] == prefix]:
        _PCM_CACHE.pop(key, None)


def _pcm_mono16k(path: Path):
    """解码成 16kHz 单声道 s16 样本（array('h')），失败返回 None。

    缓存键 = (路径, mtime_ns, 大小)：``_trim_silence`` / ``_apply_tempo`` 会用
    ``os.replace`` 原地替换同一个路径。只按路径缓存时，替换前解码的波形会一直命中，
    替换后的 ``_tail_ms`` 拿到的还是旧音频（审计实测：1.3s 文件换成 0.5s 后仍读到
    20800 个样本，正确值 8000）——cache.json / report.json 的 tail_ms、ECHO 判定
    因此和最终音频对不上。解码失败（None）不缓存：文件可能稍后才写好。
    """
    try:
        st = path.stat()
    except OSError:
        return None
    key = (str(path), st.st_mtime_ns, st.st_size)
    if key in _PCM_CACHE:
        return _PCM_CACHE[key]
    if not has_cmd("ffmpeg"):
        return None
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error",
        "-i", str(path), "-vn", "-map", "0:a:0",
        "-ac", "1", "-ar", str(ENV_SR), "-f", "s16le", "-",
    ]
    try:
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except OSError:
        return None
    if proc.returncode != 0:
        return None
    raw = proc.stdout[: len(proc.stdout) // 2 * 2]
    samples = array.array("h")
    samples.frombytes(raw)
    if sys.byteorder == "big":
        samples.byteswap()
    _PCM_CACHE[key] = samples
    return samples


def _duration_of(path: Path) -> float:
    """音频时长（秒）。优先 ffprobe，失败再数样本。"""
    try:
        info = ffprobe_json(str(path))
        dur = info.get("format", {}).get("duration")
        if dur is not None and float(dur) > 0:
            return float(dur)
        for stream in info.get("streams", []):
            if stream.get("codec_type") == "audio" and stream.get("duration"):
                return float(stream["duration"])
    except Exception:
        pass
    samples = _pcm_mono16k(path)
    if samples:
        return len(samples) / float(ENV_SR)
    return 0.0


def _rms_envelope(path: Path):
    """返回逐 FRAME_MS 的 RMS 包络（dBFS，静音为 -120）。"""
    samples = _pcm_mono16k(path)
    if not samples:
        return []
    step = max(1, int(ENV_SR * FRAME_MS / 1000))
    levels = []
    for i in range(0, len(samples) - step + 1, step):
        chunk = samples[i : i + step]
        acc = 0
        for value in chunk:
            acc += value * value
        rms = math.sqrt(acc / len(chunk)) / 32768.0
        levels.append(20.0 * math.log10(rms) if rms > 1e-6 else -120.0)
    return levels


def _speech_avg_db(levels):
    """语音帧（高于静音门限 SILENCE_DB）的平均电平；没有语音帧返回 None。"""
    speech = [level for level in levels if level > SILENCE_DB]
    if not speech:
        return None
    return sum(speech) / float(len(speech))


def _tail_ms(path: Path) -> float:
    """尾部低能量拖音时长（毫秒）。

    做法：先算语音帧平均电平，参考线 = 平均电平 - TAIL_REF_DB（40 dB，与
    references/配音与字幕.md 的「人声平均电平 -40 dB」一致）；找最后一个「不低于参考线」
    的帧，它之后的全部帧都算拖音。正常 TTS 收尾只有几十毫秒，带混响/回声的会明显更长。

    旧实现用「峰值帧 - 25 dB」，比文档严 15 dB：稍微正常一点的收尾就可能被判 ECHO。
    """
    levels = _rms_envelope(path)
    if not levels:
        return 0.0
    peak = max(levels)
    if peak <= -60.0:
        return 0.0
    speech_avg = _speech_avg_db(levels)
    if speech_avg is None:
        return 0.0
    ref = speech_avg - TAIL_REF_DB
    last_loud = -1
    for index, level in enumerate(levels):
        if level >= ref:
            last_loud = index
    if last_loud < 0:
        return 0.0
    return float((len(levels) - 1 - last_loud) * FRAME_MS)


def _trim_silence(src: Path, dst: Path) -> float:
    """切掉首尾静音，并把「判为拖音」的尾部低能量真正截掉；返回截掉的毫秒数。

    流程：
      1. ``silenceremove`` + ``areverse`` 切掉低于 -45dB 的首尾静音（与旧实现一致）；
      2. 用 ``_tail_ms`` 再量一遍：> ``TAIL_ECHO_MS`` 的尾部低能量用 ``atrim`` 截掉，
         并留 10~20ms 淡出避免"啪"的爆音，最后 ``os.replace`` 原子替换。

    旧实现只做第 1 步：文档承诺的「切掉并标记」实际只做到"标记"（ECHO），拖音一直留在
    音频里；而且 -45dB 绝对门限切不掉"低于平均电平 40dB、又高于 -45dB"的那一段拖音。
    """
    chain = (
        f"silenceremove=start_periods=1:start_duration=0.02:start_threshold={SILENCE_DB}dB,"
        "areverse,"
        f"silenceremove=start_periods=1:start_duration=0.02:start_threshold={SILENCE_DB}dB,"
        "areverse"
    )
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-i", str(src), "-af", chain,
        "-ac", "1", "-ar", "48000", "-b:a", VO_BITRATE,
        str(dst),
    ]
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if proc.returncode != 0:
        detail = proc.stderr.decode("utf-8", "replace").strip() or "（ffmpeg 没有输出错误信息）"
        raise RuntimeError(f"ffmpeg 切静音失败：{detail}")

    duration = _duration_of(dst)
    tail_ms = _tail_ms(dst)
    if duration <= 0.0 or tail_ms <= TAIL_ECHO_MS or tail_ms >= duration * 1000.0:
        return 0.0
    keep = max(0.05, duration - tail_ms / 1000.0)
    if keep >= duration - 0.005:
        return 0.0
    fade = min(0.015, max(0.005, keep * 0.2))
    af = "atrim=0:%.4f,afade=t=out:st=%.4f:d=%.4f" % (
        keep, max(0.0, keep - fade), fade)
    cut_path = dst.with_name(dst.stem + ".tailcut" + dst.suffix)
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-i", str(dst), "-af", af,
        "-ac", "1", "-ar", "48000", "-b:a", VO_BITRATE,
        str(cut_path),
    ]
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if proc.returncode != 0:
        detail = proc.stderr.decode("utf-8", "replace").strip() or "（ffmpeg 没有输出错误信息）"
        try:
            cut_path.unlink(missing_ok=True)
        except OSError:
            pass
        warn("尾部拖音 %.0fms 截断失败（ffmpeg：%s），保留原音频" % (tail_ms, detail))
        return 0.0
    try:
        os.replace(str(cut_path), str(dst))
    except OSError as exc:
        warn("尾部拖音截断后替换文件失败（%s），保留原音频" % (exc,))
        return 0.0
    _forget_pcm(dst)
    return tail_ms


def _apply_tempo(src: Path, factor: float) -> None:
    """原地变速（atempo），factor 必须 ≤ MAX_TEMPO，调用方负责保证。"""
    # 临时名必须是 `01.tempo.mp3` 这种**以 .mp3 结尾**的形式：写成 `01.mp3.tempo` 时
    # ffmpeg 从扩展名推不出封装格式（命令行又没给 -f），会直接
    # `Unable to choose an output format` 失败——而这条路径正是「最多加速 12%」的
    # 唯一实现，等于该补救手段 100% 不可用。两个都做上：名字合法 + 显式 -f mp3。
    tmp = src.with_name(f"{src.stem}.tempo{src.suffix}")
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-i", str(src), "-filter:a", f"atempo={factor:.6f}",
        "-ac", "1", "-ar", "48000", "-b:a", VO_BITRATE,
        "-f", "mp3",
        str(tmp),
    ]
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if proc.returncode != 0:
        err = proc.stderr.decode("utf-8", "replace").strip()
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise RuntimeError(f"ffmpeg atempo={factor:.3f} 失败：{err}")
    tmp.replace(src)
    _forget_pcm(src)


def _status_of(base: str, tail_ms: float) -> str:
    """状态优先级：ERROR > MISSING > TOO_LONG > ECHO > OK。"""
    if base in ("ERROR", "MISSING", "TOO_LONG"):
        return base
    return "ECHO" if tail_ms > TAIL_ECHO_MS else "OK"


def _needed_ratio(duration: float, window: float) -> float:
    """实测时长 / 当前窗口：> 1 表示这一版音频塞不进窗口。"""
    return duration / window if window > 0 else 1.0


def _window_status(needed: float) -> str:
    """按「实测时长 / 当前窗口」判是否塞得进：超过最大加速比就是 TOO_LONG。

    生成、缓存复用、audit 三条路径共用同一个判据。report.json / cache.json 里的
    status 只是「上次生成时」窗口下的结论，timing.json 的 at / 场景边界改过之后
    窗口会变窄，再信旧结论就会把超长句静默放行（AGENTS.md 第 7 节禁止静默通过）。
    """
    return "TOO_LONG" if needed > MAX_TEMPO else "OK"


def _read_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception:
        return default


def _write_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _require_ffmpeg() -> None:
    missing = [name for name in ("ffmpeg", "ffprobe") if not has_cmd(name)]
    if missing:
        error(
            "缺少 %s。装好后重跑：Windows `winget install Gyan.FFmpeg` / "
            "macOS `brew install ffmpeg` / Ubuntu `sudo apt install ffmpeg`"
            % ("、".join(missing),)
        )


# ===========================================================================
# TTS 引擎
# ===========================================================================

class EdgeEngine:
    """免费默认引擎（edge-tts）。"""

    name = "edge"

    def __init__(self, voice: str, rate: str):
        self.voice = voice or "zh-CN-XiaoxiaoNeural"
        self.rate = rate or "+0%"
        if not re.fullmatch(r"[+-]\d{1,3}%", self.rate):
            warn(f"timing.json 的 voice.rate=\"{self.rate}\" 不是 \"+8%\" 这种格式，已按 +0% 处理")
            self.rate = "+0%"

    def _check_dep(self) -> None:
        try:
            import edge_tts  # noqa: F401
        except ImportError:
            error(
                "未安装 edge-tts。执行 `pip install edge-tts`（或 `pip install -r requirements.txt`）后重试"
            )

    def synth(self, text: str, out_path: Path) -> None:
        self._check_dep()
        import edge_tts

        async def _save() -> None:
            communicate = edge_tts.Communicate(text, self.voice, rate=self.rate)
            await asyncio.wait_for(communicate.save(str(out_path)), timeout=SYNTH_TIMEOUT_S)

        def _once() -> None:
            asyncio.run(_save())

        try:
            _with_retry(f"edge-tts 合成「{_clip(text, 20)}」", _once)
        except RuntimeError as exc:
            raise RuntimeError(f"{exc}。{_net_hint()}") from exc

    @staticmethod
    def list_voices():
        import edge_tts

        return asyncio.run(asyncio.wait_for(edge_tts.list_voices(), timeout=NET_TIMEOUT_S))


class ElevenEngine:
    """可选引擎（ElevenLabs REST）。需要 ELEVENLABS_API_KEY。"""

    name = "eleven"
    API = "https://api.elevenlabs.io/v1"

    def __init__(self, voice: str, rate: str, settings: dict, model: str):
        self.voice = (voice or "").strip()
        self.rate = rate or "+0%"
        self.settings = settings or {}
        self.model = model or "eleven_multilingual_v2"

    # -- 依赖与鉴权 -------------------------------------------------------
    def _key(self) -> str:
        key = os.environ.get("ELEVENLABS_API_KEY", "").strip()
        if not key:
            error(
                "engine=eleven 需要 ELEVENLABS_API_KEY。请设置环境变量，"
                "或把项目根的 .env.example 复制成 .env 并填上 `ELEVENLABS_API_KEY=xxx`。"
                "不想付费就改用默认的免费引擎：python scripts/tts.py all --engine edge"
            )
        return key

    def _requests(self):
        try:
            import requests
        except ImportError:
            error("未安装 requests。执行 `pip install requests` 后重试")
        return requests

    def _headers(self) -> dict:
        return {"xi-api-key": self._key(), "Accept": "audio/mpeg", "Content-Type": "application/json"}

    # -- 音色 id 解析（用户可能直接写了名字） ------------------------------
    def _resolve_voice_id(self) -> str:
        if re.fullmatch(r"[A-Za-z0-9]{16,}", self.voice):
            return self.voice
        requests = self._requests()

        def _fetch():
            resp = requests.get(
                f"{self.API}/voices", headers=self._headers(), timeout=NET_TIMEOUT_S
            )
            resp.raise_for_status()
            return resp.json()

        try:
            data = _with_retry("ElevenLabs 拉取音色列表", _fetch)
        except RuntimeError as exc:
            raise RuntimeError(
                f"voice.voice=\"{self.voice}\" 看起来不是 ElevenLabs 的 voice_id，"
                f"尝试按名字解析又失败：{exc}。{_net_hint()}"
            ) from exc
        wanted = self.voice.lower()
        for item in data.get("voices", []):
            if str(item.get("name", "")).lower() == wanted:
                return str(item["voice_id"])
        names = ", ".join(str(v.get("name")) for v in data.get("voices", [])[:12])
        raise RuntimeError(
            f"ElevenLabs 里找不到叫「{self.voice}」的音色。可用名字（前 12 个）：{names}；"
            "请把 voice.voice 改成 voice_id 或上面列出的名字"
        )

    # -- 合成 -------------------------------------------------------------
    def synth(self, text: str, out_path: Path) -> None:
        requests = self._requests()
        voice_id = self._resolve_voice_id()
        payload = {
            "text": text,
            "model_id": self.model,
            "voice_settings": {
                "stability": float(self.settings.get("stability", 0.45)),
                "similarity_boost": float(self.settings.get("similarity_boost", 0.75)),
            },
        }

        def _once():
            resp = requests.post(
                f"{self.API}/text-to-speech/{voice_id}",
                headers=self._headers(),
                json=payload,
                timeout=NET_TIMEOUT_S,
            )
            if resp.status_code == 401:
                raise RuntimeError("ElevenLabs 返回 401：ELEVENLABS_API_KEY 无效或已过期")
            if resp.status_code == 429:
                raise RuntimeError("ElevenLabs 返回 429：额度/频率受限，稍后重试或换免费 edge 引擎")
            resp.raise_for_status()
            return resp.content

        try:
            audio = _with_retry(f"ElevenLabs 合成「{_clip(text, 20)}」", _once)
        except RuntimeError as exc:
            raise RuntimeError(f"{exc}。{_net_hint()}") from exc
        if not audio:
            raise RuntimeError("ElevenLabs 返回了空音频，请检查音色 id 与账号额度")
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(audio)


def _make_engine(timing: dict, cli_engine):
    voice_cfg = timing.get("voice", {}) or {}
    engine_name = (cli_engine or voice_cfg.get("engine") or "edge").strip().lower()
    voice = str(voice_cfg.get("voice", "") or "")
    rate = str(voice_cfg.get("rate", "+0%") or "+0%")
    if engine_name == "edge":
        return EdgeEngine(voice or "zh-CN-XiaoxiaoNeural", rate)
    if engine_name == "eleven":
        return ElevenEngine(
            voice,
            rate,
            voice_cfg.get("settings", {}) or {},
            str(voice_cfg.get("model", "") or voice_cfg.get("elevenModel", "") or ""),
        )
    error(f"未知引擎「{engine_name}」，只支持 edge（免费）或 eleven（需 key）")


# ===========================================================================
# 句子处理
# ===========================================================================

def _voice_lines(timing: dict):
    lines = (timing.get("voice", {}) or {}).get("lines", []) or []
    return [line for line in lines if isinstance(line, dict)]


def _select_lines(lines, only: str):
    if not only:
        return list(lines)
    wanted = {token.strip() for token in only.split(",") if token.strip()}
    picked = [line for line in lines if str(line.get("id", "")).strip() in wanted]
    missing = wanted - {str(line.get("id", "")).strip() for line in lines}
    if missing:
        warn("timing.json 的 voice.lines 里没有这些 id：%s" % ("、".join(sorted(missing)),))
    if not picked:
        error("--only 指定的句子一个都没匹配上，请检查 id（例如 --only 01,03）")
    return picked


def _process_line(timing: dict, line: dict, engine, force: bool, cache: dict) -> dict:
    """生成/复检一句，返回 report.json 里的一条记录。"""
    vo_dir = PROJECT_ROOT / VO_DIRNAME
    vo_dir.mkdir(parents=True, exist_ok=True)

    line_id = str(line.get("id", "")).strip()
    text = str(line.get("text", "") or "")
    out_path = vo_dir / f"{line_id}.mp3"

    entry = {
        "id": line_id,
        "scene": str(line.get("scene", "") or ""),
        "at": round(float(line.get("at", 0.0) or 0.0), 3),
        "text": text,
        "window": [0.0, 0.0],   # 窗口算出来了再覆盖；算不出来就保持 0，配合 status=ERROR 表达「没有窗口」
        "duration": 0.0,
        "status": "OK",
        "took_speedup": False,
        "tail_ms": 0,
        "tail_trimmed_ms": 0.0,   # 本轮自动截掉的尾部拖音（0 = 没截）
        "file": _rel(out_path),
    }

    try:
        start, end = line_window(timing, line)
    except Exception as exc:
        # 与 cmd_audit 同口径：窗口算不出来就直接判这句 ERROR，**不要**退回「at 起 8 秒」
        # 的假窗口——假窗口会让人以为这句塞得进，生成出来的音频却盖到下一句上。
        # 注意 entry 必须先构造好再用：在这里裸引用一个还没赋值的名字会抛 UnboundLocalError，
        # 那会让 `tts.py one/all` 直接崩掉 traceback，连 report.json 都写不出来。
        entry["status"] = "ERROR"
        entry["error"] = str(exc)
        _err_line(
            f"第 {line_id} 句取不到合法窗口（{exc}）：已判 ERROR，"
            "请改 timing.json 的 at / 场景边界后重跑本命令"
        )
        return entry
    start = float(start)
    end = float(end)
    window = round(end - start, 3)
    entry["window"] = [round(start, 3), round(end, 3)]

    if not line_id or not text:
        _err_line(f"timing.json 的 voice.lines 里有缺 id 或 text 的句子，已标记 ERROR：{line!r}")
        entry["status"] = "ERROR"
        return entry

    if window <= 0.05:
        warn(
            f"第 {line_id} 句窗口只有 {window:.2f} 秒（at={start:.2f}s，下一句/场景结束 {end:.2f}s）："
            "窗口太窄，请检查 timing.json 的 at 与场景边界"
        )

    key = _cache_key(
        text, engine.name, engine.voice, engine.rate,
        getattr(engine, "settings", None), getattr(engine, "model", ""),
    )
    cached = cache.get(line_id) if isinstance(cache, dict) else None
    reusable = (
        not force
        and out_path.is_file()
        and out_path.stat().st_size > 0
        and isinstance(cached, dict)
        and cached.get("hash") == key
    )

    if reusable:
        duration = _duration_of(out_path)
        needed = _needed_ratio(duration, window)
        if needed > REUSE_FIT_TOLERANCE and str(cached.get("status", "OK")) != "TOO_LONG":
            # 缓存只保证「文本/音色/语速没变」，不保证「还塞得进当前窗口」：改了 at /
            # 场景边界 / 下一句之后窗口会变窄，这一版音频必须按新窗口重做（重新生成时
            # 会自己加速到 ≤12%）。本来就是 TOO_LONG 的句子重新生成也救不回来，照旧复用。
            log(
                "  第 %s 句 旧音频 %.3fs 塞不进当前窗口 %.3fs，重新生成"
                % (_pad(line_id, 4), duration, window)
            )
        else:
            tail_ms = _tail_ms(out_path)
            entry["duration"] = round(duration, 3)
            entry["tail_ms"] = round(tail_ms, 1)
            entry["took_speedup"] = bool(cached.get("took_speedup", False))
            entry["status"] = _status_of(_window_status(needed), tail_ms)
            entry["needed_speedup"] = round(needed, 4)
            entry["skipped"] = True
            log(
                "  第 %s 句 跳过（文本/音色未变） %.3fs / 窗口 %.3fs  %s"
                % (_pad(line_id, 4), duration, window, entry["status"])
            )
            return entry

    # ---- 真正生成 ----
    tmp_dir = vo_dir / ".tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    raw_path = tmp_dir / f"{line_id}.raw.mp3"
    try:
        engine.synth(text, raw_path)
        tail_trimmed_ms = _trim_silence(raw_path, out_path)
        duration = _duration_of(out_path)
        took_speedup = False
        needed = _needed_ratio(duration, window)
        if needed > 1.001:
            factor = min(needed, MAX_TEMPO)
            _apply_tempo(out_path, factor)
            duration = _duration_of(out_path)
            took_speedup = True
        tail_ms = _tail_ms(out_path)
        entry["tail_trimmed_ms"] = round(tail_trimmed_ms, 1)
        if tail_trimmed_ms > 0:
            warn(
                f"第 {line_id} 句尾部拖音 {tail_trimmed_ms:.0f}ms 已自动截掉（留 15ms 淡出）："
                "句长因此变短，字幕停留时间会跟着变，请试听确认截得自然"
            )
        base_status = _window_status(needed)
        if base_status == "TOO_LONG":
            cut = max(1, int(math.ceil(len(text) - CHARS_PER_SEC * window)))
            warn(
                f"第 {line_id} 句即使加速到 {MAX_TEMPO:.2f}× 仍然 {duration:.2f}s > 窗口 {window:.2f}s："
                f"请回 timing.json 删字（大约删 {cut} 个字）后重跑 tts.py"
            )
        entry["duration"] = round(duration, 3)
        entry["tail_ms"] = round(tail_ms, 1)
        entry["took_speedup"] = took_speedup
        entry["status"] = _status_of(base_status, tail_ms)
        entry["needed_speedup"] = round(needed, 4)
        cache[line_id] = {
            "hash": key,
            "status": base_status,
            "took_speedup": took_speedup,
            "duration": round(duration, 3),
            "tail_ms": round(tail_ms, 1),
            "tail_trimmed_ms": round(tail_trimmed_ms, 1),
            "text": text,
            "engine": engine.name,
            "voice": engine.voice,
            "rate": engine.rate,
            "updated": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        log(
            "  第 %s 句 生成 %.3fs / 窗口 %.3fs%s  %s"
            % (
                _pad(line_id, 4),
                duration,
                window,
                "（已加速 %.1f%%）" % ((needed - 1) * 100) if took_speedup else "",
                entry["status"],
            )
        )
        if entry["status"] == "ECHO":
            warn(f"第 {line_id} 句尾部有 {tail_ms:.0f}ms 低能量拖音，请人工试听确认是否要重生成")
    except Exception as exc:  # noqa: BLE001 - 单句失败不拖垮整批
        entry["status"] = "ERROR"
        entry["error"] = str(exc)
        sys.stderr.write("ERROR: 第 %s 句配音生成失败 — %s\n" % (line_id, exc))
    finally:
        try:
            raw_path.unlink(missing_ok=True)
        except OSError:
            pass
    return entry


def _write_report(entries, timing: dict, engine_name: str = "") -> None:
    vo_dir = PROJECT_ROOT / VO_DIRNAME
    # engine 写**本次实际用的**引擎（CLI --engine 可以覆盖 timing.json 的 voice.engine）：
    # 旧实现照抄 timing.json，用 `--engine eleven` 生成的音频在报告里仍写 edge，
    # 复盘/交付说明会被误导。
    report = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "engine": engine_name or (timing.get("voice", {}) or {}).get("engine", "edge"),
        "voice": (timing.get("voice", {}) or {}).get("voice", ""),
        "slug": timing.get("slug", ""),
        "lines": entries,
    }
    _write_json(vo_dir / REPORT_NAME, report)


def _read_report():
    path = PROJECT_ROOT / VO_DIRNAME / REPORT_NAME
    data = _read_json(path, None)
    if not isinstance(data, dict) or not isinstance(data.get("lines"), list):
        return None, path
    return data, path


def _err_line(msg: str) -> None:
    """打印一条 ``ERROR:`` 行但**不退出**。

    ``all`` 要跑完所有句子、把 report.json 写出来之后才统一收口退出，所以这里用
    ``error(msg, code=0)``（只打印、不 ``sys.exit``）。但「不退出」不等于「降级成 WARN」：
    ``cmd_one`` 失败时会提示「见上面的 ERROR 行」，上面就必须真有 ERROR 行，
    否则用户按提示往上翻只看得到 WARN，找不到真正的原因。
    """
    error(msg, code=0)


def _merge_into_report(entries: list, timing: dict, engine_name: str = "",
                       new_first: bool = False) -> None:
    """把本次生成的记录**合并**进已有 report.json，保留别的句子。

    ``tts.py all --only 02`` 只重做 02：如果直接整份覆盖，01/03 的记录就没了，
    之后 ``audit`` 和交付说明会以为那两句从来没生成过（旧版本就是这么丢的）。
    ``cmd_one`` 复用同一条路径（``new_first=True``：报告里原本没有这一句时放到最前面，
    保持它以前的位置）。``engine_name`` 透传给 _write_report，保证 engine 写实际引擎。
    """
    previous, _ = _read_report()
    merged: list = []
    fresh = {str(item.get("id")): item for item in entries}
    if previous:
        for item in previous["lines"]:
            key = str(item.get("id"))
            if key in fresh:
                merged.append(fresh.pop(key))
            else:
                merged.append(item)
    rest = list(fresh.values())
    _write_report(rest + merged if new_first else merged + rest, timing, engine_name)


# ===========================================================================
# 子命令
# ===========================================================================

def cmd_all(args) -> int:
    _require_ffmpeg()
    _load_dotenv()
    timing = load_timing()
    lines = _voice_lines(timing)
    if not lines:
        error(
            "timing.json 的 voice.lines 是空的。请先写旁白稿：每句带 id/scene/at/text，"
            "语速按 5.5~6.5 字/秒（AGENTS.md 阶段 7）"
        )
    todo = _select_lines(lines, args.only)
    engine = _make_engine(timing, args.engine)

    vo_dir = PROJECT_ROOT / VO_DIRNAME
    cache_path = vo_dir / CACHE_NAME
    cache = _read_json(cache_path, {})
    if not isinstance(cache, dict):
        cache = {}

    log(
        f"配音生成：引擎 {engine.name}，音色 {engine.voice}，共 {len(todo)}/{len(lines)} 句"
        f"{'（--force 全部重生成）' if args.force else ''}"
    )
    entries = []
    for line in todo:
        entries.append(_process_line(timing, line, engine, args.force, cache))

    # 合并进已有报告：`all --only 01,03` 这种局部重生成以前会整份覆盖，
    # 把没重做的句子从 report.json 里抹掉（audit 只看得到本次这几句）。
    _merge_into_report(entries, timing, engine.name)
    _write_json(cache_path, cache)

    ok = sum(1 for e in entries if e["status"] == "OK")
    echo = sum(1 for e in entries if e["status"] == "ECHO")
    long_ = sum(1 for e in entries if e["status"] == "TOO_LONG")
    real_errors = [e for e in entries if e["status"] == "ERROR"]
    log("")
    log(f"完成：OK {ok} / ECHO {echo} / TOO_LONG {long_} / ERROR {len(real_errors)}")
    log(f"报告：{_rel(PROJECT_ROOT / VO_DIRNAME / REPORT_NAME)}")
    if long_:
        error(
            f"有 {long_} 句超出窗口且加速到上限 {MAX_TEMPO:.2f}× 也塞不下。"
            "请回 timing.json 删字改稿（不要靠脚本文速硬压），然后重跑 `python scripts/tts.py all`"
        )
    if real_errors:
        error(
            f"有 {len(real_errors)} 句生成失败（见上面的 ERROR 行）。"
            "多为网络问题：" + _net_hint()
        )
    if echo:
        warn(f"有 {echo} 句疑似尾部拖音（ECHO），请试听后决定是否重生成：python scripts/tts.py one <id> --force")
    return 0


def cmd_one(args) -> int:
    _require_ffmpeg()
    _load_dotenv()
    timing = load_timing()
    lines = _voice_lines(timing)
    target = None
    for line in lines:
        if str(line.get("id", "")).strip() == str(args.id).strip():
            target = line
            break
    if target is None:
        ids = "、".join(str(line.get("id", "")) for line in lines) or "（空）"
        error(f"timing.json 里没有 id={args.id} 的句子。现有 id：{ids}")

    engine = _make_engine(timing, args.engine)
    vo_dir = PROJECT_ROOT / VO_DIRNAME
    cache_path = vo_dir / CACHE_NAME
    cache = _read_json(cache_path, {})
    if not isinstance(cache, dict):
        cache = {}

    force = True if args.force else True  # one 的语义就是「重新做这一句」
    entry = _process_line(timing, target, engine, force, cache)
    _write_json(cache_path, cache)

    # 合并进已有报告（保留其它句子的记录，audit 才有全貌）
    _merge_into_report([entry], timing, engine.name, new_first=True)

    if entry["status"] in ("ERROR", "TOO_LONG"):
        if entry["status"] == "TOO_LONG":
            error(f"第 {entry['id']} 句仍然超窗口（{entry['duration']:.2f}s > {entry['window'][1] - entry['window'][0]:.2f}s），请改稿删字")
        error(f"第 {entry['id']} 句生成失败，见上面的 ERROR 行")
    log(f"第 {entry['id']} 句完成：{entry['duration']:.3f}s（窗口 {entry['window'][1] - entry['window'][0]:.3f}s）{entry['status']}")
    if entry["status"] == "ECHO":
        warn(f"尾部拖音 {entry['tail_ms']:.0f}ms，请人工试听确认")
    return 0


def _audit_tip(rows, window_details) -> str:
    """审核不通过时给出「先改哪里」的提示。

    旧实现只挑 ``window/real`` 余量最大的那一句（TOO_LONG 或 MISSING 都算）给
    「删 N 个字」：MISSING 的余量是负数（窗口 < 0），永远排不到第一 → 只有 MISSING
    时会算出 ``cut = max(1, 负数)`` 的「删约 1 个字」。但 MISSING 的根因是「还没生成
    音频」，删字完全无效，正确动作是先跑 ``python scripts/tts.py all``。
    现在 TOO_LONG 与 MISSING 分开提示，各自给对的改法。
    """
    tips = []
    too_long = [r for r in rows if r[5] == "TOO_LONG"]
    if too_long:
        longest = max(too_long, key=lambda r: r[3] - r[2])
        cut = max(1, int(math.ceil(len(longest[1]) - CHARS_PER_SEC * longest[2])))
        tips.append(f"TOO_LONG 从第 {longest[0]} 句改起（建议删掉约 {cut} 个字）")
    missing = [r for r in rows if r[5] == "MISSING"]
    if missing:
        ids = "、".join(str(r[0]) for r in missing[:5])
        more = " 等" if len(missing) > 5 else ""
        tips.append(
            f"MISSING（第 {ids}{more} 句）是还没生成音频，先跑 `python scripts/tts.py all`，"
            "删字对 MISSING 无效"
        )
    if window_details:
        tips.append("窗口非法先改 at（太密）或把这段文案挪到更长的场景")
    if not tips:
        return ""
    return "改稿建议：" + "；".join(tips) + "。"


def cmd_audit(args) -> int:
    _require_ffmpeg()
    timing = load_timing()
    lines = _voice_lines(timing)
    if not lines:
        error("timing.json 的 voice.lines 是空的，没有可审核的句子")

    vo_dir = PROJECT_ROOT / VO_DIRNAME
    rows = []
    errors = 0
    echoes = 0
    stale = 0
    window_errors = 0   # 窗口非法（line_window 抛错）的句数，最后按审核不通过处理
    window_details: list = []   # [(句号, 原因)]，汇总时报全，不要只报第一句
    unreadable = 0      # 音频读不出来（ffprobe/ffmpeg 失败）的句数，同样审核不通过
    for line in lines:
        line_id = str(line.get("id", "")).strip()
        text = str(line.get("text", "") or "")
        path = vo_dir / f"{line_id}.mp3"
        try:
            start, end = line_window(timing, line)
        except Exception as exc:
            # 窗口不合法就**不能**再来一个「at 起 8 秒」的假窗口：窗口被人为放大之后
            # needed 会变小，本来 TOO_LONG 的句子会变成 OK，这个命令就会打印
            # 「审核通过」并退 0——正是 AGENTS.md 第 7 节禁止的静默通过。
            # 一律按 ERROR 记账（`_common.line_window` 的 docstring 也是这么约定的），
            # 走完表格再统一报错退 1。
            window_errors += 1
            window_details.append((line_id, str(exc)))
            rows.append((line_id, text, 0.0, 0.0, 0.0, "ERROR"))
            # 这里**不能**用 error()：error() 会立刻 sys.exit()，循环就断了——
            # 多句非法时只打印第一句，后面本该一起报出来的句子永远看不到，
            # 而且上面的 window_errors 聚合也成了死代码。逐句记下来，走完表格统一报。
            warn(
                f"第 {line_id} 句取不到合法窗口：{exc}"
                "（窗口 = 所在场景结束与下一句 at 中更早的那个，再扣 0.15s 尾巴；"
                "不合法说明 at 写得太密或排到了场景结束之后，请改 timing.json）"
            )
            continue
        window = float(end) - float(start)

        if not path.is_file() or path.stat().st_size == 0:
            rows.append((line_id, text, window, 0.0, -window, "MISSING"))
            errors += 1
            continue
        try:
            duration = _duration_of(path)
            tail_ms = _tail_ms(path)
        except SystemExit:
            # _duration_of 先走 ffprobe_json，它失败时会先打 ERROR 行再 sys.exit()；
            # SystemExit 不是 Exception，原来没人接住 → 一句坏音频就让整个 audit 表都
            # 打不出来（实测：03.mp3 换成非音频内容后，stderr 只有 ffprobe 的 ERROR，
            # 表格/汇总全消失）。这里记一行 ERROR 继续审下一句，走完表格再统一退 1。
            unreadable += 1
            rows.append((line_id, text, window, 0.0, -window, "ERROR"))
            warn(
                f"第 {line_id} 句音频读不出来（ffprobe/ffmpeg 失败，见上面的 ERROR 行）："
                f"{_rel(path)}；这句要重做：python scripts/tts.py all --only {line_id}"
            )
            continue
        margin = window - duration
        needed = _needed_ratio(duration, window)
        # 一律按实测判：report.json 里的 status 只是「上次生成时那个窗口」下的结论，
        # 改了 at / 场景边界 / 下一句之后它已经过期，信它就会把超长句静默放行。
        status = _status_of(_window_status(needed), tail_ms)
        if status == "TOO_LONG":
            errors += 1
        elif needed > REUSE_FIT_TOLERANCE:
            # 塞不进当前窗口，但加速 ≤12% 就能救：不用改稿，是磁盘上这版音频过期了。
            stale += 1
            warn(
                f"第 {line_id} 句音频 {duration:.2f}s 比当前窗口 {window:.2f}s 长"
                f"（要加速 {(needed - 1) * 100:.1f}% 才塞得进）：这一版是按旧窗口生成的，"
                "请重跑 `python scripts/tts.py all`（会自动加速重做），不用删字"
            )
        elif status == "ECHO":
            echoes += 1
        rows.append((line_id, text, window, duration, margin, status))

    header = (
        _pad("句号", 6)
        + _pad("文本", 26)
        + _pad("窗口", 9)
        + _pad("实际", 9)
        + _pad("余量", 9)
        + "状态"
    )
    log("")
    log(header)
    log("-" * (_disp_width(header) + 2))
    for line_id, text, window, duration, margin, status in rows:
        log(
            _pad(line_id, 6)
            + _pad(_clip(text, 24), 26)
            + _pad(f"{window:.2f}s" if window else "—", 9)
            + _pad(f"{duration:.2f}s" if duration else "—", 9)
            + _pad(f"{margin:+.2f}s" if duration else "—", 9)
            + status
        )
    log("")

    if errors or window_errors or unreadable:
        if window_details:
            # 把每一句的窗口原因都打在 ERROR 汇总里（上面只有 WARN 逐句行，
            # 有些 CI 只抓 stderr 的 ERROR 行看，所以这里必须带全）。
            log("")
            for line_id, why in window_details:
                log(f"  窗口非法 第 {line_id} 句：{why}")
        error(
            f"审核不通过：{errors} 句 TOO_LONG/MISSING、{window_errors} 句窗口非法、"
            f"{unreadable} 句音频读不出来"
            f"（共 {errors + window_errors + unreadable} 句，上面每句都有对应行）。"
            f"字数超窗口必须改稿删字（AGENTS.md 阶段 7），不允许靠加速硬压；"
            f"窗口非法说明 at 太密或排到了场景结束 / 下一句之后；"
            f"音频读不出来说明文件损坏，重跑 `python scripts/tts.py all` 重做。"
            f"{_audit_tip(rows, window_details)}"
            f"改完重跑 `python scripts/tts.py all`"
        )
    # 走到这里说明 0 ERROR / 0 窗口非法。（error() 已经 sys.exit，不会再往下走。）
    if echoes:
        warn(f"{echoes} 句疑似尾部拖音（ECHO），请人工试听确认：这是 AI 听不出来的项")
    if stale:
        warn(f"{stale} 句音频是按旧窗口生成的（塞不进当前窗口），请重跑 `python scripts/tts.py all` 重做")
    notes = []
    if echoes:
        notes.append(f"{echoes} 句 ECHO 待人工确认")
    if stale:
        notes.append(f"{stale} 句需重跑 tts.py all")
    log(f"审核通过：{len(rows)} 句没有 TOO_LONG/MISSING" + (f"（{'；'.join(notes)}）" if notes else ""))
    return 0


def cmd_voices(args) -> int:
    lang = (args.lang or "").strip().lower()
    voices = None
    try:
        raw = _with_retry("拉取 edge-tts 音色列表", EdgeEngine.list_voices)
        voices = []
        for item in raw:
            short = str(item.get("ShortName", ""))
            if not short:
                continue
            if lang and not short.lower().startswith(lang):
                continue
            tag = item.get("VoiceTag", {}) or {}
            scene = "、".join(list(tag.get("VoicePersonalities", []) or [])[:3]) or "—"
            voices.append(
                (short, str(item.get("Gender", "?")), str(item.get("Locale", "")), scene)
            )
    except Exception as exc:  # noqa: BLE001
        warn(f"在线拉取音色列表失败（{exc}）；{_net_hint()}")
        warn("已回退到内置的常用中文音色清单（可能不是最新）")
        voices = [v for v in FALLBACK_VOICES if (not lang or v[2].lower().startswith(lang))]

    if not voices:
        warn(f"没有匹配 --lang {args.lang} 的音色，下面是全部内置候选")
        voices = list(FALLBACK_VOICES)

    header = _pad("音色 ShortName", 34) + _pad("性别", 8) + _pad("地区", 14) + "适用场景"
    log(header)
    log("-" * (_disp_width(header) + 2))
    for short, gender, locale, scene in sorted(voices):
        log(_pad(short, 34) + _pad(gender, 8) + _pad(locale, 14) + scene)
    log("")
    log("挑好之后：把 ShortName 写进 src/timing.json 的 voice.voice，再跑 preview 让用户试听拍板")
    return 0


def cmd_preview(args) -> int:
    _require_ffmpeg()
    _load_dotenv()
    timing = load_timing()
    text = (args.text or "").strip()
    if not text:
        error('请提供试听文本，例如：python scripts/tts.py preview --text "拍一张小票，自动记好账"')

    if args.voices:
        candidates = [v.strip() for v in args.voices.split(",") if v.strip()]
    else:
        candidates = list(DEFAULT_PREVIEW_VOICES)
    if len(candidates) < 3:
        warn(f"只给了 {len(candidates)} 个音色，建议 3~5 个才能听出差异")
    if len(candidates) > 5:
        warn(f"给了 {len(candidates)} 个音色，只做前 5 个（试听不宜超过 5 个）")
        candidates = candidates[:5]

    engine_name = (args.engine or (timing.get("voice", {}) or {}).get("engine") or "edge").lower()
    outdir = Path(args.out) if args.out else (PROJECT_ROOT / PREVIEW_DIRNAME)
    if not outdir.is_absolute():
        outdir = PROJECT_ROOT / outdir
    # 统一走 _common.ensure_dir：-o 指向一个已存在的文件时，旧实现直接
    # mkdir(exist_ok=True) 抛 FileExistsError 裸 traceback（审计实测）。
    outdir = ensure_dir(outdir)

    voice_cfg = timing.get("voice", {}) or {}
    rate = str(voice_cfg.get("rate", "+0%") or "+0%")
    settings = voice_cfg.get("settings", {}) or {}

    log(f"多音色试听：{len(candidates)} 个候选，文本「{text}」")
    results = []
    for name in candidates:
        if engine_name == "eleven":
            engine = ElevenEngine(name, rate, settings, str(voice_cfg.get("model", "") or ""))
        else:
            engine = EdgeEngine(name, rate)
        out_path = outdir / f"{name}.mp3"
        try:
            engine.synth(text, out_path)
            duration = _duration_of(out_path)
            results.append((name, out_path, duration, ""))
            log(f"  OK   {_pad(name, 34)} {duration:.2f}s  {_rel(out_path)}")
        except Exception as exc:  # noqa: BLE001
            results.append((name, out_path, 0.0, str(exc)))
            sys.stderr.write(f"ERROR: 音色 {name} 试听生成失败 — {exc}\n")

    ok = [r for r in results if not r[3]]
    log("")
    if not ok:
        error("所有候选音色都生成失败了。先解决网络/依赖问题（" + _net_hint() + "）")
    log(f"已生成 {len(ok)} 个试听文件到 {_rel(outdir)}：")
    for name, path, duration, _ in results:
        if duration:
            log(f"  {_rel(path)}   （{duration:.2f}s）")
    log("")
    log("下一步（必须由用户拍板，AI 听不到声音）：")
    log("  1. 让用户逐个试听上面这些文件，选一个最合适的音色；")
    log("  2. 把那句音色名写进 src/timing.json 的 voice.voice，例如 \"zh-CN-YunxiNeural\"；")
    log("  3. 重跑 python scripts/tts.py all --force，再跑 python scripts/mix.py。")
    return 0 if len(ok) == len(results) else 1


def cmd_stt(args) -> int:
    audio = Path(args.audio)
    if not audio.is_absolute():
        audio = PROJECT_ROOT / audio
    if not audio.is_file():
        error(f"找不到音频文件：{audio}")

    try:
        from faster_whisper import WhisperModel  # type: ignore
    except ImportError:
        # stt 是校验命令：没装依赖 = 没校验成，必须 ERROR + 非零退出。
        # 以前这里打一条 WARN 就 return 0，等于「校验没做」也算通过（AGENTS.md 第 7 节：
        # 发现问题就要非零退出并打印 ERROR 行，不要静默通过）。
        error(
            "未安装 faster-whisper，没做转写回读校验："
            "`pip install faster-whisper` 之后重跑本命令（首次运行会下载模型）。"
            "不想校验就别跑 `stt`，别把「没校验」当成「校验通过」"
        )

    timing = load_timing()
    lines = _voice_lines(timing)
    line_id = (args.id or audio.stem).strip()
    reference = ""
    for line in lines:
        if str(line.get("id", "")).strip() == line_id:
            reference = str(line.get("text", "") or "")
            break

    log(f"转写校验：{_rel(audio)}（模型 {args.model}，首次运行会下载模型，需要网络）")
    try:
        model = WhisperModel(args.model, device="cpu", compute_type="int8")
        segments, info = model.transcribe(str(audio), language=args.lang, beam_size=5)
        hypothesis = "".join(seg.text for seg in segments).strip()
    except Exception as exc:  # noqa: BLE001
        # 转写失败同样是「没校验成」：以前 WARN + return 0 会让 `stt` 在网络不通时
        # 也报「通过」，比缺依赖那次更危险（依赖在、人以为校验真的跑了）。
        error(f"faster-whisper 转写失败（{exc}）；{_net_hint()} —— 这次没校验成，请修好后重跑")

    log(f"识别结果：{hypothesis}")
    if not reference:
        # 没有对照稿就比不了 —— 同样是「没校验成」，不能 return 0 当通过。
        error(f"timing.json 里没有 id={line_id} 的句子，无法比对（可用 --id 指定）—— 这次没校验成")
    log(f"稿件原文：{reference}")

    normalize = lambda s: re.sub(r"[\s，。！？：；、,.!?:;…—\-—「」『』（）()《》]", "", s)  # noqa: E731
    ref_norm = normalize(reference)
    hyp_norm = normalize(hypothesis)
    ratio = difflib.SequenceMatcher(None, ref_norm, hyp_norm).ratio()
    log(f"相似度：{ratio:.3f}")

    differ = []
    matcher = difflib.SequenceMatcher(None, ref_norm, hyp_norm)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        differ.append(f"  {tag}: 稿件「{ref_norm[i1:i2]}」 ↔ 识别「{hyp_norm[j1:j2]}」")
    if differ:
        log("差异：")
        for item in differ:
            log(item)
    else:
        log("差异：无（逐字一致）")

    if ratio < STT_ERROR_RATIO:
        error(
            f"转写相似度 {ratio:.3f} < {STT_ERROR_RATIO}：音频内容与 timing.json 的稿子明显不符，"
            "请确认音色/文本是否对得上，必要时重跑 tts.py --force"
        )
    if ratio < STT_WARN_RATIO:
        warn(
            f"转写相似度 {ratio:.3f} < {STT_WARN_RATIO}：可能是识别误差（数字、专有名词常见），"
            "也可能确实读错了，建议人工试听确认"
        )
    return 0


# ===========================================================================
# main
# ===========================================================================

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tts.py",
        description="promo-forge 配音生成与审核（AGENTS.md 阶段 7）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "例：\n"
            "  python scripts/tts.py all --only 01,03\n"
            "  python scripts/tts.py audit\n"
            "  python scripts/tts.py preview --text \"拍一张小票，自动记好账\"\n"
            "退出码：0 通过；1 有 ERROR（生成失败 / TOO_LONG / MISSING）。"
        ),
    )
    sub = parser.add_subparsers(dest="cmd", metavar="<子命令>")

    p_all = sub.add_parser("all", help="按 timing.json 的 voice.lines 逐句生成配音")
    p_all.add_argument("--engine", choices=["edge", "eleven"], default=None,
                       help="TTS 引擎；默认读 timing.json 的 voice.engine（edge 免费）")
    p_all.add_argument("--only", default=None, help="只处理这些句子，逗号分隔，例如 01,03")
    p_all.add_argument("--force", action="store_true", help="忽略缓存，全部重新生成")
    p_all.set_defaults(func=cmd_all)

    p_one = sub.add_parser("one", help="只生成一句（改稿或换音色后局部重做）")
    p_one.add_argument("id", help="句子 id，例如 02")
    p_one.add_argument("--engine", choices=["edge", "eleven"], default=None, help="TTS 引擎")
    p_one.add_argument("--force", action="store_true", help="忽略缓存强制重生成（默认就是强制）")
    p_one.set_defaults(func=cmd_one)

    p_audit = sub.add_parser("audit", help="复核时长/拖音；TOO_LONG 或 MISSING 退出码 1")
    p_audit.set_defaults(func=cmd_audit)

    p_voices = sub.add_parser("voices", help="列出可用音色（离线时回退到内置清单）")
    p_voices.add_argument("--lang", default="zh", help="按地区前缀过滤，默认 zh；传空串列全部")
    p_voices.set_defaults(func=cmd_voices)

    p_prev = sub.add_parser("preview", help="多音色试听：同一句话用 3~5 个音色各生成一份")
    p_prev.add_argument("--text", required=True, help="试听用的文本（建议用片子里最长的那句）")
    p_prev.add_argument("--voices", default=None, help="逗号分隔的候选音色，默认 5 个常用中文音色")
    p_prev.add_argument("-o", "--out", default=None, help=f"输出目录，默认 {PREVIEW_DIRNAME}")
    p_prev.add_argument("--engine", choices=["edge", "eleven"], default=None, help="TTS 引擎")
    p_prev.set_defaults(func=cmd_preview)

    p_stt = sub.add_parser("stt", help="可选：把音频转写成文字并与稿子比对（需 faster-whisper）")
    p_stt.add_argument("audio", help="音频文件，例如 audio-src/vo/01.mp3")
    p_stt.add_argument("--id", default=None, help="比对哪一句，默认取文件名（不含扩展名）")
    p_stt.add_argument("--model", default="small", help="whisper 模型规格，默认 small")
    p_stt.add_argument("--lang", default="zh", help="语言，默认 zh")
    p_stt.set_defaults(func=cmd_stt)

    return parser


def main(argv=None) -> int:
    _force_utf8()
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "cmd", None):
        parser.print_help()
        return 0
    try:
        return int(args.func(args) or 0)
    except KeyboardInterrupt:
        sys.stderr.write("ERROR: 用户中断\n")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
