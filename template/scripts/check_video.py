#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""promo-forge · 阶段 9「校验闸门」之成片检查（check_video.py）。

用途
----
渲染**之后**对成片做机器可判定的全项体检，把「AI 看不到画面、听不到声音」的部分全部
变成可测量的数字。检查项：

1. **规格**：分辨率 = ``timing.json`` 的 width×height（``--expect`` 可覆盖；``--scale 2``
   出的 4K 版按整数倍通过）、fps 与 ``timing.fps`` 一致（±0.01）、时长与 ``timing.duration``
   一致（±0.2 秒）、编码与像素格式、必须存在音轨；
2. **渲染日志**：``out/render.log`` 里出现 ``cannot be decoded`` / ``Error:`` /
   ``failed to load`` / ``Timeout`` / ``ERROR`` 一律按失败处理（AGENTS.md 阶段 9）。
   日志文件**不存在**时不是「跳过」而是 ``WARN``（``--strict`` 时升为 ``ERROR``）：
   没有日志这条闸门等于不存在，交付前必须能挡住。模板的三条 render script 会把
   stdout+stderr 重定向到 ``out/render.log``，正常流程下这个文件本来就有；
3. **响度**：``ebur128=peak=true`` 测 Integrated loudness 与 True peak，
   不在 ``[--lufs-min, --lufs-max]`` 区间 → ERROR；真峰值 > -0.5 dBTP → WARN；
4. **空档**：``silencedetect=noise=-45dB:d=<--silence>``，忽略片头 0.3 秒与片尾 0.6 秒
   （AGENTS.md 阶段 8 规定这两处本来就是淡入淡出），中间任何 ≥ 阈值的静音段 → ERROR；
5. **抽帧审图**：每个场景取 40% 与 80% 两个时刻抽帧，拼成 4 列带标签联系表
   ``out/check-<slug>.jpg``，提示人工逐张审图（AGENTS.md 阶段 9 的人工闸门）。

典型调用
--------
    python scripts/check_video.py out/final-1080p.mp4
    python scripts/check_video.py out/final-4k.mp4 --expect 2160x3840
    python scripts/check_video.py out/preview.mp4 --fps 30 --log out/render-preview.log
    python scripts/check_video.py out/final-1080p.mp4 --strict   # 交付前：WARN 也算失败

依赖
----
必需：ffmpeg / ffprobe（硬依赖）、``src/timing.json``、``scripts/_common.py``。
可选：Pillow（拼联系表；缺了只跳过抽帧审图并 WARN）。

退出码
------
0 通过（0 ERROR）；1 有 ERROR（``--strict`` 时 WARN 也升级成 ERROR）。输出格式固定为
``OK/WARN/ERROR: <检查项> — <说明>``，最后一行 ``成片检查：X ERROR / Y WARN（<规格摘要>）``。
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
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
        has_cmd,
        human,
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

# AGENTS.md 第 7 节闸门表：规格与 timing.json 一致（时长 ±0.2 秒）。
DURATION_TOLERANCE_S = 0.2

# AGENTS.md 第 5 节：timing.fps 与成片帧率一致；这里给 ±0.01 的浮点容差。
FPS_TOLERANCE = 0.01

# AGENTS.md 阶段 8 / 第 7 节：母带 -14 LUFS，闸门区间 -16 ~ -12 LUFS。
DEFAULT_LUFS_MIN = -16.0
DEFAULT_LUFS_MAX = -12.0

# AGENTS.md 阶段 8：真峰值 -1 dBFS；超过 -0.5 dBTP 有削波风险，给 WARN。
TRUE_PEAK_WARN_DBTP = -0.5

# 响度测量的两个滤镜：首选 EBU R128 官方的 ebur128；精简版 ffmpeg（Remotion 自带的
# compositor 构建就是）没编 ebur128，退到 loudnorm 的 JSON 测量 —— input_i / input_tp
# 与 ebur128 用的是同一套 EBU R128 门控算法，可直接当 Integrated / True peak 用。
LOUDNESS_FILTER = "ebur128=peak=true"
LOUDNORM_MEASURE_FILTER = "loudnorm=print_format=json"

# AGENTS.md 阶段 8：除转场瞬间外不允许 ≥1.0 秒空档；片头 0.3s 淡入、片尾 0.6s 淡出不算。
DEFAULT_SILENCE_S = 1.0
FADE_IN_S = 0.3
FADE_OUT_S = 0.6
SILENCE_NOISE_DB = -45

# AGENTS.md 第 7 节：渲染日志里出现这些字样一律按失败处理。
LOG_ERROR_PATTERNS = (
    "cannot be decoded",
    "Error:",
    "failed to load",
    "Timeout",
    "ERROR",
)
LOG_MAX_HITS = 20

# 模板 remotion.config.ts 的编码设置；不一致不算硬失败，但要提醒平台兼容性。
EXPECTED_CODEC = "h264"
EXPECTED_PIX_FMT = "yuv420p"

# 抽帧审图（AGENTS.md 阶段 9）。
SHEET_COLS = 4
FRAME_DIRNAME = "out/check-frames"
SHEET_PREFIX = "out/check-"
FRAME_PCTS = (0.40, 0.80)
THUMB_WIDTH = 480
LABEL_HEIGHT = 44
SHEET_PAD = 14
SHEET_BG = (17, 17, 22)
SHEET_FG = (238, 238, 242)
SHEET_DIM = (150, 150, 160)
SHEET_QUALITY = 88
LABEL_FONT_SIZE = 22

DEFAULT_LOG_PATH = "out/render.log"

FFPROBE_TIMEOUT_S = 120.0
FFMPEG_TIMEOUT_S = 900.0


# ===========================================================================
# 结果收集
# ===========================================================================

class Report:
    """收集 OK/WARN/ERROR；ERROR 立刻写 stderr，保证 AI 工具能从 stderr 抓到。

    ``strict=True`` 时 WARN 升级为 ERROR（交付前的严格模式，语义与 check_assets.py 一致）。
    渲染日志缺失这类「闸门自己没生效」的情况必须能在交付前挡住。
    """

    def __init__(self, strict: bool = False):
        self.strict = strict
        self.errors = 0
        self.warnings = 0

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
        return Path(path).resolve().relative_to(PROJECT_ROOT.resolve()).as_posix()
    except Exception:
        return Path(path).as_posix()


def _resolve_project_path(value: str) -> Path:
    """先按当前工作目录解析，再按项目根解析（脚本在项目根跑，但也容错）。"""
    path = Path(value)
    if path.is_file():
        return path
    candidate = PROJECT_ROOT / value
    return candidate if candidate.exists() else path


# ===========================================================================
# ffmpeg / ffprobe 调用
# ===========================================================================

def _ensure_tools() -> None:
    missing = [name for name in ("ffmpeg", "ffprobe") if not has_cmd(name)]
    if missing:
        error(
            "找不到 %s。ffmpeg/ffprobe 是本脚本的硬依赖，请先安装并确保在 PATH 里："
            "Windows `winget install Gyan.FFmpeg`（或从 ffmpeg.org 下 zip 后把 bin 加进 PATH）；"
            "macOS `brew install ffmpeg`；Linux `sudo apt install ffmpeg`。装完重跑 "
            "`python scripts/doctor.py` 确认。"
            % ("、".join(missing),)
        )


def _run_binary(cmd, what: str, timeout: float):
    """跑一个外部命令并返回 CompletedProcess；启动失败/超时统一给出中文 ERROR。"""
    try:
        return subprocess.run(
            [str(part) for part in cmd],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
        )
    except FileNotFoundError:
        error(f"执行 {what} 时找不到可执行文件 {cmd[0]}。请确认它已安装并在 PATH 里。")
    except subprocess.TimeoutExpired:
        error(f"{what} 超过 {timeout:.0f} 秒还没结束，已放弃。请检查文件是否损坏或磁盘是否卡住。")
    except OSError as exc:
        error(f"执行 {what} 失败：{exc}")
    raise SystemExit(1)  # pragma: no cover - error() 已经退出


def _ffprobe(path: Path) -> dict:
    proc = _run_binary(
        [
            "ffprobe", "-v", "quiet", "-print_format", "json",
            "-show_format", "-show_streams", str(path),
        ],
        f"ffprobe {path.name}",
        FFPROBE_TIMEOUT_S,
    )
    stdout = proc.stdout.decode("utf-8", errors="replace").strip()
    if not stdout:
        stderr = proc.stderr.decode("utf-8", errors="replace").strip()
        error(f"ffprobe 读不出 {path.name} 的信息（退出码 {proc.returncode}）："
              f"{stderr or '文件可能不是有效的视频，或者已损坏'}")
    try:
        return json.loads(stdout)
    except json.JSONDecodeError as exc:
        error(f"ffprobe 返回的 JSON 解析失败（{exc}）。请确认 ffprobe 版本正常。")


def _decode_output(proc) -> str:
    """ffmpeg 的输出混在 stderr 与 stdout 两边，合并成一段文本再解析。"""
    return (
        proc.stderr.decode("utf-8", errors="replace")
        + "\n"
        + proc.stdout.decode("utf-8", errors="replace")
    )


def _filter_unavailable(text: str) -> bool:
    """ffmpeg 报「没有这个滤镜」（不同版本措辞不同）。"""
    return "No such filter" in text or "Filter not found" in text


def _analyze_audio(path: Path, audio_filter: str):
    """跑一次只做音频分析的 ffmpeg（-f null -），返回 (returncode, stderr+stdout 文本)。

    ⚠️ 坑一（实测踩过）：命令必须带 `-vn`。`-f null -` 的默认视频编码器是
    wrapped_avframe，精简版 ffmpeg（Remotion 自带的 compositor 构建把它从
    `--disable-encoders` 白名单里去掉了）会在**打开输出文件**时就失败：
    `Error opening output files: Encoder not found` —— 滤镜一行都没跑，输出里既没有
    Summary 块也没有 silence_start。老写法没带 -vn，于是「空档」被读成「全片没有静音」、
    「响度」被读成「解析不出」，两条硬闸门一起静默失效。音频侧不需要额外挑编码器
    （pcm_s16le 是各种构建都保留的默认值）。
    ⚠️ 坑二：同样因为精简构建，有些 ffmpeg 根本没编 `ebur128` 滤镜
    （`No such filter: 'ebur128'`）。这种情况下自动改跑 loudnorm 量一次响度。
    """
    proc = _run_binary(
        ["ffmpeg", "-hide_banner", "-nostdin", "-i", str(path), "-vn",
         "-af", audio_filter, "-f", "null", "-"],
        f"ffmpeg -af {audio_filter.split('=')[0]}",
        FFMPEG_TIMEOUT_S,
    )
    text = _decode_output(proc)
    if proc.returncode != 0 and audio_filter.startswith("ebur128") and _filter_unavailable(text):
        fallback = _run_binary(
            ["ffmpeg", "-hide_banner", "-nostdin", "-i", str(path), "-vn",
             "-af", LOUDNORM_MEASURE_FILTER, "-f", "null", "-"],
            "ffmpeg -af loudnorm",
            FFMPEG_TIMEOUT_S,
        )
        if fallback.returncode == 0:
            return fallback.returncode, _decode_output(fallback)
    return proc.returncode, text


def _ffmpeg_af(path: Path, audio_filter: str) -> str:
    """兼容壳：只返回分析文本（deliver.py 也 import 本模块调它）。"""
    return _analyze_audio(path, audio_filter)[1]


def _parse_fraction(value) -> float:
    """把 ffprobe 的 '60/1' / '30000/1001' 变成浮点数。"""
    text = str(value or "").strip()
    if not text:
        return 0.0
    if "/" in text:
        num, _, den = text.partition("/")
        try:
            num_f, den_f = float(num), float(den)
        except ValueError:
            return 0.0
        return num_f / den_f if den_f else 0.0
    try:
        return float(text)
    except ValueError:
        return 0.0


# ===========================================================================
# 1. 规格
# ===========================================================================

def _pick_video_stream(probe: dict):
    for stream in probe.get("streams", []) or []:
        if stream.get("codec_type") == "video":
            return stream
    return None


def _pick_audio_stream(probe: dict):
    for stream in probe.get("streams", []) or []:
        if stream.get("codec_type") == "audio":
            return stream
    return None


def check_spec(report: Report, probe: dict, timing: dict, path: Path,
               expect: str | None, fps_override: float | None):
    """返回 (width, height, fps, duration) 供总结行使用。"""
    item = "视频规格"
    video = _pick_video_stream(probe)
    if video is None:
        report.fail(item, f"{_rel(path)} 里没有视频流，这不是一个能交付的成片")
        return 0, 0, 0.0, 0.0

    width = int(video.get("width") or 0)
    height = int(video.get("height") or 0)

    # --- 分辨率 ---
    if expect:
        match = re.match(r"^\s*(\d+)\s*[x×*]\s*(\d+)\s*$", expect)
        if not match:
            error(f"--expect 格式不对：{expect!r}。请写成 WxH，例如 1080x1920 或 2160x3840。")
        target_w, target_h = int(match.group(1)), int(match.group(2))
        source = "--expect"
    else:
        try:
            target_w, target_h = int(timing.get("width")), int(timing.get("height"))
        except (TypeError, ValueError):
            report.fail(item, "timing.json 的 width/height 不是整数，无法比对分辨率")
            target_w = target_h = 0
        source = "timing.json"

    if target_w and target_h:
        if (width, height) == (target_w, target_h):
            report.ok(item, f"分辨率 {width}×{height} = {source} 的 {target_w}×{target_h}")
        elif (width % target_w == 0 and height % target_h == 0
              and width // target_w == height // target_h and width // target_w >= 1):
            factor = width // target_w
            report.ok(
                item,
                f"分辨率 {width}×{height} 是 {source} 的 {factor} 倍缩放"
                f"（--scale {factor} 的正常产物）",
            )
        else:
            report.fail(
                item,
                f"分辨率 {width}×{height} 与 {source} 的 {target_w}×{target_h} 不一致，"
                f"也不是它的整数倍缩放。横竖屏搞反或尺寸写错都会导致平台二压变形；"
                f"请核对 timing.json 的 width/height，或显式传 --expect {width}x{height}",
            )

    # --- 像素格式与编码 ---
    codec = str(video.get("codec_name") or "?")
    pix_fmt = str(video.get("pix_fmt") or "?")
    if codec == EXPECTED_CODEC and pix_fmt == EXPECTED_PIX_FMT:
        report.ok(item, f"编码 {codec} / 像素格式 {pix_fmt}（remotion.config.ts 的交付设置）")
    else:
        problems = []
        if codec != EXPECTED_CODEC:
            problems.append(f"编码是 {codec}，期望 {EXPECTED_CODEC}")
        if pix_fmt != EXPECTED_PIX_FMT:
            problems.append(f"像素格式是 {pix_fmt}，期望 {EXPECTED_PIX_FMT}")
        report.warn(
            item,
            "；".join(problems) + "。非 h264/yuv420p 的成片部分平台会拒收或二次转码。"
            "先确认 remotion.config.ts 的 setCodec/setPixelFormat 没被改过、且 setVideoImageFormat(\"png\")；"
            "JPEG 中间帧会被写成 yuvj420p（full range），临时提速可以用 --image-format=jpeg，但成片请用 PNG 重渲",
        )

    # --- 帧率 ---
    actual_fps = _parse_fraction(video.get("r_frame_rate")) or _parse_fraction(video.get("avg_frame_rate"))
    if fps_override is not None:
        target_fps, fps_source = float(fps_override), "--fps"
    else:
        fps_source = "timing.json"
        try:
            target_fps = float(timing.get("fps"))
        except (TypeError, ValueError):
            target_fps = 0.0
            report.fail(item, "timing.json 的 fps 不是数字，无法比对帧率")
    if target_fps:
        if actual_fps <= 0:
            report.warn(item, "ffprobe 没报出帧率，跳过帧率比对（可能是可变帧率文件）")
        elif abs(actual_fps - target_fps) <= FPS_TOLERANCE:
            report.ok(item, f"帧率 {actual_fps:g}fps = {fps_source} 的 {target_fps:g}fps")
        else:
            report.fail(
                item,
                f"帧率 {actual_fps:g}fps 与 {fps_source} 的 {target_fps:g}fps 不一致"
                f"（容差 ±{FPS_TOLERANCE}）。卡点全部按 timing.json 算帧，帧率不对整片都会错位；"
                "预览版请用 --fps 30 显式声明",
            )

    # --- 时长 ---
    duration = 0.0
    for candidate in (video.get("duration"), (probe.get("format") or {}).get("duration")):
        duration = _parse_fraction(candidate)
        if duration > 0:
            break
    try:
        target_duration = float(timing.get("duration"))
    except (TypeError, ValueError):
        target_duration = 0.0
        report.fail(item, "timing.json 的 duration 不是数字，无法比对时长")
    if target_duration:
        if duration <= 0:
            report.warn(item, "ffprobe 没报出时长，跳过时长比对")
        elif abs(duration - target_duration) <= DURATION_TOLERANCE_S:
            report.ok(item, f"时长 {duration:.2f}s ≈ timing.duration {target_duration:g}s"
                            f"（容差 ±{DURATION_TOLERANCE_S}s）")
        else:
            report.fail(
                item,
                f"时长 {duration:.2f}s 与 timing.duration {target_duration:g}s 差 "
                f"{abs(duration - target_duration):.2f}s（容差 ±{DURATION_TOLERANCE_S}s）。"
                "常见原因：最后一个场景的 end 与 duration 没对齐、渲染被中断、或漏渲了尾部",
            )

    # --- 音轨 ---
    audio = _pick_audio_stream(probe)
    if audio is None:
        report.fail(
            item,
            f"{_rel(path)} 没有音轨。先跑 `python scripts/mix.py` 生成 public/audio/music.wav（即 timing.json 的 music.file），"
            "并确认 Remotion 里用 staticFile 把它挂到了 <Audio> 上，再重新渲染",
        )
    else:
        report.ok(
            item,
            "有音轨（编码 %s，%s Hz，%s 声道）"
            % (
                audio.get("codec_name", "?"),
                audio.get("sample_rate", "?"),
                audio.get("channels", "?"),
            ),
        )

    return width, height, actual_fps, duration


# ===========================================================================
# 2. 渲染日志
# ===========================================================================

def check_render_log(report: Report, log_path: Path) -> None:
    item = "渲染日志"
    if not log_path.is_file():
        # 以前这里只打一行 log 就 return：等于「渲染日志无报错」这条闸门默认不存在，
        # 交付时最容易被忽略的一类事故（素材没读到、组件抛错但渲染仍然"成功结束"）
        # 就这么漏过去了。现在至少 WARN，--strict 直接 ERROR。
        report.warn(
            item,
            f"找不到 {_rel(log_path)}，没法确认渲染过程有没有报错。"
            "AGENTS.md 阶段 9 规定「渲染日志里出现 cannot be decoded / Error: / failed to load "
            "一律按失败处理」，没有日志这条闸门等于不存在。"
            "正常流程下这个文件是渲染脚本自己写的：模板的三条 render script 已经把 stdout+stderr "
            f"都重定向到 {DEFAULT_LOG_PATH}（Windows / macOS / Linux 都一样，因为重定向在 npm 脚本里，"
            "由 npm 调用的 shell 执行，不依赖你手边是 cmd 还是 bash）。"
            "直接手跑 remotion 时才需要自己重定向：Windows cmd 用 "
            "`remotion render ... > out\\render.log 2>&1`，macOS / Linux 用 "
            "`remotion render ... 2>&1 | tee out/render.log`"
            "（PowerShell 的 `>` 默认写 UTF-16，别用它，否则日志读出来是乱码）。"
            "日志确实在别处就传 --log <路径>；要让缺日志直接算失败，加 --strict",
        )
        return
    try:
        text = log_path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        report.warn(item, f"读不了 {_rel(log_path)}：{exc}，跳过渲染日志扫描")
        return

    hits = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        lowered = line.lower()
        for pattern in LOG_ERROR_PATTERNS:
            if pattern.lower() in lowered:
                hits.append((lineno, pattern, line.strip()))
                break

    if hits:
        for lineno, pattern, line in hits[:LOG_MAX_HITS]:
            report.fail(item, f"{_rel(log_path)}:{lineno} 命中「{pattern}」：{line[:200]}")
        if len(hits) > LOG_MAX_HITS:
            report.fail(item, f"渲染日志里还有 {len(hits) - LOG_MAX_HITS} 处匹配没打印（同类问题）")
        report.fail(
            item,
            f"渲染日志共 {len(hits)} 处报错字样。AGENTS.md 阶段 9：即使渲染「成功结束」也按失败处理——"
            "大部分情况下是素材没读到或某个场景组件抛错了，画面会缺一块",
        )
    else:
        report.ok(item, f"{_rel(log_path)} 没有报错字样（{len(text.splitlines())} 行）")


# ===========================================================================
# 3. 响度 / 4. 空档
# ===========================================================================

def _parse_loudnorm_json(text: str):
    """从 loudnorm 的 JSON 块里取 (input_i, input_tp)，没有就返回 (None, None)。

    loudnorm 的 JSON 后面还跟着 ffmpeg 自己的统计行，不能整段 json.loads，
    所以先把 `{ ... "input_i" ... }` 这一小块抠出来。
    """
    match = re.search(r"\{[^{}]*\"input_i\"[^{}]*\}", text, re.DOTALL)
    if not match:
        return None, None
    try:
        data = json.loads(match.group(0))
    except (json.JSONDecodeError, ValueError):
        return None, None
    try:
        integrated = float(data.get("input_i"))
    except (TypeError, ValueError):
        integrated = None
    try:
        true_peak = float(data.get("input_tp"))
    except (TypeError, ValueError):
        true_peak = None
    return integrated, true_peak


def _parse_ebur128(text: str):
    """从 ffmpeg 的 ebur128 输出里取 Integrated loudness 与 True peak。

    ⚠️ 坑（实测踩过）：`ebur128` 除了结尾的 Summary 块，还会**逐帧**打印读数，
    形如 `t: 0.0999792  TARGET:-23 LUFS  M:-120.7 S:-120.7  I: -70.0 LUFS  LRA: 0.0 LU
    FTPK: -inf dBFS  TPK: -inf dBFS`。片子开头那几帧还没攒够 gating 块，`I` 固定是
    -70.0 LUFS —— 于是「取**第一个** `I:` 匹配」的写法会把任何片子都读成 -70.0 LUFS：
    一条实测 -15.0 LUFS 的成片照样被判成「不在 [-16, -12] 区间内」直接 ERROR，
    闸门等于把每一支合格的片子都拦下来。所以这里只在结尾的 Summary 块里找，
    块内也取**最后一个**匹配（Summary 里 I 只出现一次）。

    行式差异（不同 ffmpeg 版本 / 工具）：除了 `I: -15.6 LUFS`，也认
    `Integrated loudness: -15.6 LUFS`（只在 `I:` 找不到时才用，避免两处读数打架）；
    读数是 `-inf` 时按负无穷处理 —— 静音片的 `I: -inf LUFS` 必须落进「不在区间内」
    报 ERROR，而不是被当成「解析不出」静默放行。
    本机精简 ffmpeg 没有 ebur128 时，`_analyze_audio` 会用 loudnorm 兜底测量，
    所以这里也认 loudnorm 的 JSON（`input_i` / `input_tp`）。
    """
    block = text.rsplit("Summary:", 1)[-1] if "Summary:" in text else text
    number = r"(-?\d+(?:\.\d+)?|-?inf)"
    integrated = None
    true_peak = None
    for match in re.finditer(r"\bI:\s*" + number + r"\s*LUFS", block, re.IGNORECASE):
        integrated = float(match.group(1))
    if integrated is None:
        match = re.search(
            r"Integrated\s+loudness:?\s*" + number + r"\s*LUFS", block, re.IGNORECASE
        )
        if match:
            integrated = float(match.group(1))
    for match in re.finditer(r"Peak:\s*" + number + r"\s*dBFS", block, re.IGNORECASE):
        true_peak = float(match.group(1))
    if integrated is None or true_peak is None:
        fallback_i, fallback_tp = _parse_loudnorm_json(text)
        if integrated is None:
            integrated = fallback_i
        if true_peak is None:
            true_peak = fallback_tp
    return integrated, true_peak


def check_loudness(report: Report, path: Path, lufs_min: float, lufs_max: float):
    item = "响度"
    returncode, text = _analyze_audio(path, LOUDNESS_FILTER)
    integrated, true_peak = _parse_ebur128(text)
    if integrated is None:
        # 量不出来 = 这条硬闸门没生效。旧写法在这里只给 WARN，于是「-16~-12 LUFS」在
        # 任何量不出响度的机器上都是一条不存在的闸门（0 ERROR 放行一支响度超标的片子）；
        # 按 AGENTS.md「发现问题就非零退出，不要静默通过」，这里必须算 ERROR。
        detail = "；".join(
            line.strip() for line in text.strip().splitlines()[-3:] if line.strip()
        )
        report.fail(
            item,
            f"量不出 Integrated loudness（ffmpeg 退出码 {returncode}），-16~-12 LUFS 这条硬闸门"
            "无法验证。本机 ffmpeg 可能既没有 ebur128 滤镜也没有 loudnorm；"
            "请装完整版 ffmpeg（Windows `winget install Gyan.FFmpeg`）后重跑。"
            "手工排查：`ffmpeg -i <mp4> -vn -af ebur128=peak=true -f null -` 看结尾 Summary 块，"
            f"或 `-af loudnorm=print_format=json` 看 input_i。ffmpeg 最后几行：{detail[:300]}",
        )
        return None, None
    if lufs_min <= integrated <= lufs_max:
        report.ok(item, f"Integrated {integrated:.1f} LUFS 落在 [{lufs_min:g}, {lufs_max:g}] 区间内")
    else:
        report.fail(
            item,
            f"Integrated {integrated:.1f} LUFS 不在 [{lufs_min:g}, {lufs_max:g}] 区间内"
            "（AGENTS.md 阶段 8 的母带目标是 -14 LUFS）。"
            "请回阶段 8 检查 mix.py 的 loudnorm 是否跑通，或音乐/人声增益是否被改过",
        )
    if true_peak is None:
        report.warn(item, "解析不出 True peak，无法判断削波风险")
    elif true_peak > TRUE_PEAK_WARN_DBTP:
        report.warn(
            item,
            f"真峰值 {true_peak:.2f} dBTP 高于 {TRUE_PEAK_WARN_DBTP} dBTP，"
            "平台转码后容易削波；AGENTS.md 阶段 8 的目标是 -1 dBFS",
        )
    else:
        report.ok(item, f"真峰值 {true_peak:.2f} dBTP（目标 -1 dBFS，安全线 {TRUE_PEAK_WARN_DBTP}）")
    return integrated, true_peak


def _parse_silence(text: str, total_duration: float):
    """解析 silencedetect 输出，返回 [(start, end, duration)]，未闭合段按片尾补齐。"""
    segments = []
    pending = None
    for line in text.splitlines():
        match = re.search(r"silence_start:\s*(-?\d+(?:\.\d+)?)", line)
        if match:
            pending = float(match.group(1))
            continue
        match = re.search(r"silence_end:\s*(-?\d+(?:\.\d+)?)(?:\s*\|\s*silence_duration:\s*(-?\d+(?:\.\d+)?))?", line)
        if match and pending is not None:
            end = float(match.group(1))
            duration = float(match.group(2)) if match.group(2) else (end - pending)
            segments.append((pending, end, duration))
            pending = None
    if pending is not None:
        end = total_duration if total_duration > pending else pending
        segments.append((pending, end, end - pending))
    return segments


def check_silence(report: Report, path: Path, threshold_s: float, total_duration: float) -> None:
    item = "空档"
    filter_text = f"silencedetect=noise={SILENCE_NOISE_DB}dB:d={threshold_s:g}"
    returncode, text = _analyze_audio(path, filter_text)
    if returncode != 0:
        # 旧写法不检查退出码：ffmpeg 一条 silence_start 都没打出来时，下面 `not segments`
        # 会打出「全片没有 ≥1 秒的静音段」—— 那是假通过（实测本机 ffmpeg 因缺
        # wrapped_avframe 编码器根本没跑滤镜）。跑不起来就必须报 ERROR。
        detail = "；".join(
            line.strip() for line in text.strip().splitlines()[-3:] if line.strip()
        )
        report.fail(
            item,
            f"silencedetect 没跑起来（ffmpeg 退出码 {returncode}），空档闸门无法判定。"
            "AGENTS.md 阶段 8 不允许 ≥1.0 秒空档，这条检查不能跳过。"
            f"ffmpeg 最后几行：{detail[:300]}",
        )
        return
    segments = _parse_silence(text, total_duration)
    if not segments:
        report.ok(
            item,
            f"全片没有 ≥{threshold_s:g} 秒的静音段（判定阈值 {SILENCE_NOISE_DB}dB，"
            f"已忽略片头 {FADE_IN_S}s 与片尾 {FADE_OUT_S}s 的淡入淡出）",
        )
        return

    body_start = FADE_IN_S
    body_end = total_duration - FADE_OUT_S if total_duration > 0 else float("inf")
    offenders = []
    for start, end, _raw in segments:
        clipped_start = max(start, body_start)
        clipped_end = min(end, body_end)
        clipped = clipped_end - clipped_start
        if clipped >= threshold_s:
            offenders.append((clipped_start, clipped_end, clipped))
    if not offenders:
        report.ok(
            item,
            f"检测到 {len(segments)} 段静音，但都落在片头/片尾的淡入淡出区，不算空档",
        )
        return

    for start, end, length in offenders:
        report.fail(
            item,
            f"{start:.2f}s ~ {end:.2f}s 有 {length:.2f} 秒空档（≥{threshold_s:g} 秒，"
            f"判定阈值 {SILENCE_NOISE_DB}dB）",
        )
    report.fail(
        item,
        f"全片 {len(offenders)} 处空档。AGENTS.md 阶段 8：除转场瞬间外不允许 ≥{threshold_s:g} 秒空档；"
        "一般是某句旁白没生成成功、音乐没铺满、或 segments 剪接留下了空隙。"
        "先跑 `python scripts/mix.py --env` 看响度包络定位",
    )


# ===========================================================================
# 5. 抽帧 + 联系表
# ===========================================================================

def _pick_label_font(size: int):
    """按 fonts.json 的 systemCandidates 找中文标签字体，找不到返回 (None, 说明)。"""
    fonts_path = PROJECT_ROOT / "fonts.json"
    candidates = []
    try:
        cfg = json.loads(fonts_path.read_text(encoding="utf-8"))
        candidates = list((cfg.get("check", {}) or {}).get("systemCandidates", []) or [])
    except Exception:
        candidates = []

    try:
        from PIL import ImageFont  # type: ignore
    except ImportError:
        return None, "未安装 Pillow"

    for candidate in candidates:
        path = Path(str(candidate)).expanduser()
        if not path.is_absolute():
            # fonts.json 里的候选是相对项目根的（如 public/fonts/promo-subset-display.ttf），
            # 按进程 CWD 解析时，从项目根之外调用会找不到字体，联系表标签静默退成 Pillow 位图字体。
            path = PROJECT_ROOT / path
        try:
            if path.is_file():
                return ImageFont.truetype(str(path), size, index=0), path.name
        except Exception:
            continue
    try:
        return ImageFont.load_default(size=size), "Pillow 内置位图字体"
    except Exception:
        try:
            return ImageFont.load_default(), "Pillow 内置位图字体"
        except Exception:
            return None, "Pillow 内置位图字体"


def _slugify(text: str) -> str:
    cleaned = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff_-]+", "-", str(text or "")).strip("-")
    return cleaned or "scene"


def extract_frames(report: Report, path: Path, timing: dict):
    """每个场景取 40% 与 80% 时刻抽帧，返回 [(scene_name, second, frame_path, label)]。"""
    scenes = [s for s in (timing.get("scenes") or []) if isinstance(s, dict)]
    if not scenes:
        report.warn("抽帧审图", "timing.json 没有 scenes，跳过抽帧")
        return []

    frames_dir = PROJECT_ROOT / FRAME_DIRNAME
    if frames_dir.exists():
        shutil.rmtree(frames_dir, ignore_errors=True)
    frames_dir.mkdir(parents=True, exist_ok=True)

    slug = str(timing.get("slug", "promo") or "promo")
    frames = []
    for scene in scenes:
        name = str(scene.get("name", "?"))
        try:
            start, end = float(scene.get("start")), float(scene.get("end"))
        except (TypeError, ValueError):
            report.warn("抽帧审图", f"场景「{name}」的 start/end 不是数字，跳过抽帧")
            continue
        for index, pct in enumerate(FRAME_PCTS, start=1):
            second = start + (end - start) * pct
            if second <= start:
                second = start + 0.05
            output = frames_dir / f"{slug}-{_slugify(name)}-{index}-{second:08.2f}.jpg"
            proc = _run_binary(
                [
                    "ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "error",
                    "-ss", f"{second:.3f}", "-i", str(path),
                    "-frames:v", "1", "-q:v", "2", "-y", str(output),
                ],
                f"抽帧 {second:.2f}s",
                FFMPEG_TIMEOUT_S,
            )
            if not output.is_file() or output.stat().st_size == 0:
                stderr = proc.stderr.decode("utf-8", errors="replace").strip()
                report.warn(
                    "抽帧审图",
                    f"场景「{name}」{second:.2f}s 抽帧失败：{stderr[:200] or 'ffmpeg 没输出文件'}",
                )
                continue
            frames.append((name, second, output, f"{name}  {second:.2f}s（{int(pct * 100)}%）"))

    if not frames:
        report.fail("抽帧审图", "一帧都没抽出来，无法人工审图；请检查 mp4 是否可解码")
        return []
    report.ok("抽帧审图", f"抽出 {len(frames)} 帧 → {_rel(frames_dir)}/")
    return frames


def build_contact_sheet(report: Report, timing: dict, frames) -> None:
    item = "联系表"
    if not frames:
        return
    try:
        from PIL import Image, ImageDraw  # type: ignore
    except ImportError:
        report.warn(
            item,
            "未安装 Pillow，跳过联系表拼接。请 `pip install Pillow` 后重跑，"
            "或手工打开 out/check-frames/ 里的帧逐张看",
        )
        return

    font, font_note = _pick_label_font(LABEL_FONT_SIZE)
    if font_note != "Pillow 内置位图字体" and font is None:
        report.warn(item, f"没有可用的中文标签字体（{font_note}），联系表标签可能显示成方块")
    elif font_note == "Pillow 内置位图字体":
        report.warn(
            item,
            "fonts.json 的 systemCandidates 里一个字体都没找到，标签回退到 Pillow 内置位图字体"
            "（中文会变方块）。建议装 Noto Sans CJK / 微软雅黑后重跑",
        )

    thumbs = []
    for name, second, frame_path, label in frames:
        try:
            with Image.open(frame_path) as image:
                image = image.convert("RGB")
                ratio = THUMB_WIDTH / float(image.width)
                thumb = image.resize(
                    (THUMB_WIDTH, max(1, int(round(image.height * ratio)))),
                    Image.LANCZOS,
                )
                thumbs.append((thumb, label))
        except Exception as exc:  # noqa: BLE001
            report.warn(item, f"读不出抽帧 {_rel(frame_path)}：{exc}")

    if not thumbs:
        report.fail(item, "所有抽出的帧都读不出来，无法拼联系表")
        return

    cols = max(1, SHEET_COLS)
    rows = (len(thumbs) + cols - 1) // cols
    cell_w = THUMB_WIDTH
    cell_h = max(t.height for t, _ in thumbs) + LABEL_HEIGHT
    header_h = 64
    width = SHEET_PAD + cols * (cell_w + SHEET_PAD)
    height = header_h + SHEET_PAD + rows * (cell_h + SHEET_PAD)

    sheet = Image.new("RGB", (width, height), SHEET_BG)
    draw = ImageDraw.Draw(sheet)
    slug = str(timing.get("slug", "promo") or "promo")
    title = f"{slug} · 审图联系表 · {len(thumbs)} 帧 · 每场景 40% / 80%"
    draw.text((SHEET_PAD + 4, 18), title, fill=SHEET_FG, font=font)

    for index, (thumb, label) in enumerate(thumbs):
        row, col = divmod(index, cols)
        x = SHEET_PAD + col * (cell_w + SHEET_PAD)
        y = header_h + SHEET_PAD + row * (cell_h + SHEET_PAD)
        sheet.paste(thumb, (x, y))
        draw.rectangle([x, y, x + thumb.width - 1, y + thumb.height - 1], outline=(70, 70, 82))
        draw.text((x + 4, y + thumb.height + 8), label, fill=SHEET_DIM, font=font)

    out_path = PROJECT_ROOT / f"{SHEET_PREFIX}{slug}.jpg"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        sheet.save(out_path, "JPEG", quality=SHEET_QUALITY, optimize=True)
    except OSError as exc:
        report.fail(item, f"联系表写不进 {_rel(out_path)}：{exc}")
        return
    report.ok(item, f"{_rel(out_path)}（{sheet.width}×{sheet.height}，{human(out_path.stat().st_size)}）")
    log("")
    log(f"请打开这张图逐张审图：{out_path}")
    log("对照 references/校验清单.md 打勾：构图、字幕安全区（上 10% / 下 18% / 左 6% / 右 14%）、"
        "截图清晰度、品牌名与网址拼写。")


# ===========================================================================
# main
# ===========================================================================

def main(argv=None) -> int:
    _force_utf8()
    parser = argparse.ArgumentParser(
        prog="check_video.py",
        description="promo-forge 成片闸门（AGENTS.md 阶段 9）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "输出格式：OK/WARN/ERROR: <检查项> — <说明>；最后一行统计。\n"
            "退出码：0 通过（0 ERROR）；1 有 ERROR（--strict 时 WARN 也算 ERROR）。"
        ),
    )
    parser.add_argument("video", help="要检查的 mp4，例如 out/final-1080p.mp4")
    parser.add_argument("--expect", default=None,
                        help="期望分辨率 WxH，例如 1080x1920；默认取 timing.json 的 width×height。"
                             "渲染 4K 版时传 2160x3840，或让它按整数倍自动通过")
    parser.add_argument("--log", default=DEFAULT_LOG_PATH,
                        help=f"渲染日志路径（默认 {DEFAULT_LOG_PATH}；文件不存在会 WARN，"
                             "--strict 下算 ERROR）")
    parser.add_argument("--strict", action="store_true",
                        help="把所有 WARN 也当 ERROR（交付前的严格模式，语义同 check_assets.py）；"
                             "缺渲染日志、真峰值偏高、拼不了联系表都会变成失败")
    parser.add_argument("--fps", type=float, default=None,
                        help="期望帧率；默认取 timing.json 的 fps（预览版是 30fps，传 30）")
    parser.add_argument("--lufs-min", type=float, default=DEFAULT_LUFS_MIN,
                        help=f"响度下限 LUFS（默认 {DEFAULT_LUFS_MIN:g}）")
    parser.add_argument("--lufs-max", type=float, default=DEFAULT_LUFS_MAX,
                        help=f"响度上限 LUFS（默认 {DEFAULT_LUFS_MAX:g}）")
    parser.add_argument("--silence", type=float, default=DEFAULT_SILENCE_S,
                        help=f"空档判定阈值秒数（默认 {DEFAULT_SILENCE_S:g}）")
    args = parser.parse_args(argv)

    report = Report(strict=args.strict)
    _ensure_tools()

    video_path = _resolve_project_path(args.video)
    log(f"成片检查：{_rel(video_path)}")
    if not video_path.is_file():
        error(
            f"找不到成片 {args.video}。先跑 `npm run render:1080`（输出 out/final-1080p.mp4），"
            "或把正确路径作为第一个参数传进来。"
        )
    if video_path.stat().st_size == 0:
        error(f"{_rel(video_path)} 是空文件（0 字节），渲染没有真正完成。")

    if args.lufs_min >= args.lufs_max:
        error(f"--lufs-min ({args.lufs_min:g}) 必须小于 --lufs-max ({args.lufs_max:g})。")

    timing = load_timing()
    probe = _ffprobe(video_path)
    log("")

    width, height, fps, duration = check_spec(report, probe, timing, video_path, args.expect, args.fps)
    check_render_log(report, _resolve_project_path(args.log))
    integrated, true_peak = check_loudness(report, video_path, args.lufs_min, args.lufs_max)
    check_silence(report, video_path, args.silence, duration)

    log("")
    frames = extract_frames(report, video_path, timing)
    build_contact_sheet(report, timing, frames)

    spec = f"{width}×{height} · {fps:g}fps · {duration:.2f}s"
    if integrated is not None:
        spec += f" · {integrated:.1f} LUFS"
    if true_peak is not None:
        spec += f" · {true_peak:.1f} dBTP"
    summary = f"成片检查：{report.errors} ERROR / {report.warnings} WARN（{spec}）"
    if args.strict:
        summary += "（--strict 模式）"
    log("")
    log(summary)
    if report.errors:
        sys.stderr.write(summary + "\n")
        return 1
    log("成片闸门通过。最后一步是人工：把上面那张联系表逐张看完，对照 references/校验清单.md 打勾。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
