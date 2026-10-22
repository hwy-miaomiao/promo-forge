#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""promo-forge 发布合规闸门（AGENTS.md 阶段 9 / 10 / 第 7 节闸门之一）。

成片渲染完不等于能发：标题长度、话题数量、封面尺寸、时长、字幕安全区、
导出包完整性（成片 + 封面 + 字幕 + 文案清单）每家平台的口径都不一样。
本脚本把「发布前要知道的事」落成可重复执行的检查单，输出
``publish/manifest.json``：人照着清单发；**不自动发布**（账号风控与平台合规
必须由人负责）。

平台规则是 2026-10 的保守整理（抖音 / 快手 / 小红书 / 视频号），平台政策会变，
发布前请人工复核；规则常量集中在 ``PLATFORM_RULES``，改一处即可。

成片校验分两层（P1）：
1. 纯 stdlib 读 MP4 顶层 box，必须有 ``ftyp`` 与 ``moov``——文本/半截文件会直接
   ERROR，不再只看「文件存在且非 0 字节」；
2. 本机有 ``ffprobe`` 时再深检容器/视频流/时长/分辨率（失败 = ERROR）；没有
   ffprobe 时只做第 1 层并显式 WARN 说明降级（不静默放过）。

两个可选证据闸门（只在显式传参时启用，避免旧项目产物被新口径拦死）：
- ``--frames out/stills``：调 ``frame_check.py`` 机器验证「代理真的看过画面」
  （帧数覆盖 timing 每个场景、PNG 可解码、像素尺寸 == timing、非空白）；
- ``--transcript 转写.txt|.srt``：调 ``transcript_check.py`` 做零依赖 STT 反查
  （配音逐句对上字幕源；漏行 / 错序 / 品牌名错一个字 = ERROR）。

用法：
    python scripts/publish_check.py                     # 默认四平台全查
    python scripts/publish_check.py --platform douyin   # 只查一个平台
    python scripts/publish_check.py --strict            # WARN 也当 ERROR（更严）
    python scripts/publish_check.py --frames out/stills --transcript 转写.txt
    python scripts/publish_check.py --format json       # 单行 JSON（MCP 用）
    python scripts/publish_check.py --selftest          # 纯函数自测，不读真实项目

退出码：0 通过（0 ERROR）；1 有 ERROR / 文件缺失 / 参数错误。
"""
from __future__ import annotations

import argparse
import datetime as _dt
import importlib.util
import json
import math
import re
import shutil
import subprocess
import sys
import zlib
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

# ---------------------------------------------------------------------------
# 导入公共库 scripts/_common.py
# ---------------------------------------------------------------------------
sys.path.insert(0, str(Path(__file__).resolve().parent))
try:  # pragma: no cover - 只在 _common.py 缺失时走到
    from _common import PROJECT_ROOT, ensure_dir, log  # type: ignore
except ImportError as exc:  # pragma: no cover
    sys.stderr.write(
        "ERROR: 无法导入 scripts/_common.py（%s）。请确认 scripts 目录完整，"
        "并在项目根目录下运行本脚本。\n" % (exc,)
    )
    raise SystemExit(1)


# ===========================================================================
# 常量
# ===========================================================================

DEFAULT_VIDEO = "out/final-1080p.mp4"
DEFAULT_COVER_DIR = "out/covers"
DEFAULT_SUBTITLE = "out/subtitles/zh.srt"
DEFAULT_COPY = "publish/copy.json"
DEFAULT_MANIFEST = "publish/manifest.json"
SCHEMA_VERSION = 1
COVER_WIDTH = 1080
COVER_HEIGHT = 1920
COVER_MAX_CANDIDATES = 3
SAFE_BOTTOM = 0.18

RULE_SOURCE = "2026-10 保守整理；平台政策会变，发布前人工复核"

PLATFORM_ORDER = ("douyin", "kuaishou", "xiaohongshu", "shipinhao")
PLATFORM_RULES = {
    "douyin": {
        "name": "抖音",
        "title_min": 1, "title_max": 30,
        "hashtags_min": 1, "hashtags_max": 5,
        "body_max": 1000,
        "duration_min_s": 3, "duration_max_s": 900,
        "cover_min": 1, "cover_max": 3,
    },
    "kuaishou": {
        "name": "快手",
        "title_min": 1, "title_max": 30,
        "hashtags_min": 1, "hashtags_max": 5,
        "body_max": 1000,
        "duration_min_s": 3, "duration_max_s": 900,
        "cover_min": 1, "cover_max": 3,
    },
    "xiaohongshu": {
        "name": "小红书",
        "title_min": 1, "title_max": 20,
        "hashtags_min": 1, "hashtags_max": 10,
        "body_max": 1000,
        "duration_min_s": 3, "duration_max_s": 900,
        "cover_min": 1, "cover_max": 3,
    },
    "shipinhao": {
        "name": "视频号",
        "title_min": 1, "title_max": 30,
        "hashtags_min": 1, "hashtags_max": 10,
        "body_max": 1000,
        "duration_min_s": 3, "duration_max_s": 1800,
        "cover_min": 1, "cover_max": 3,
    },
}

_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_HASHTAG_SPLIT_RE = re.compile(r"[,，;；\s]+")
_DIGITS_RE = re.compile(r"(\d+)")

#: MP4/QuickTime 常见的顶层 box；其余不可打印类型会被判成「不是 MP4」。
_MP4_TOP_BOXES = (
    b"ftyp", b"moov", b"mdat", b"free", b"skip", b"wide", b"moof",
    b"mfra", b"styp", b"pdin", b"uuid", b"meta",
)


# ===========================================================================
# 基础工具（纯函数）
# ===========================================================================

def _res(level: str, item: str, message: str) -> Dict[str, str]:
    return {"level": level, "item": item, "message": message}


def now_iso() -> str:
    return _dt.datetime.now().astimezone().isoformat(timespec="seconds")


def _resolve(base: Path, raw: Any, default: str) -> Path:
    """把 CLI 传入的相对路径解析到项目根；空值用默认相对路径。"""
    text = str(raw) if raw not in (None, "") else default
    candidate = Path(text).expanduser()
    if candidate.is_absolute():
        return candidate
    return Path(base) / candidate


def _rel(base: Path, path: Path) -> str:
    try:
        return path.resolve().relative_to(base.resolve()).as_posix()
    except ValueError:
        return path.as_posix()


def load_json_file(path: Path) -> Tuple[Optional[Any], Optional[str]]:
    target = Path(path)
    if not target.is_file():
        return None, "找不到 %s" % (target,)
    try:
        return json.loads(target.read_text(encoding="utf-8-sig")), None
    except (OSError, UnicodeDecodeError) as exc:
        return None, "读不了 %s：%s" % (target, exc)
    except json.JSONDecodeError as exc:
        return None, "%s 不是合法 JSON：%s" % (target, exc)


#: PNG 解压后允许的最大扫描线字节数：超过就拒绝继续校验（防 zip 炸弹）。
_PNG_MAX_RAW = 400 * 1024 * 1024
#: PNG color_type → 每像素通道数（0 灰度 2 RGB 3 调色板 4 灰度+alpha 6 RGBA）。
_PNG_CHANNELS = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}


def _png_expected_raw_size(ihdr: bytes, width: int, height: int) -> Optional[int]:
    """按 IHDR 估算解压后的字节数（每行 1 个 filter 字节）；不支持组合返回 None。"""
    if len(ihdr) != 13:
        return None
    bit_depth, color_type, interlace = ihdr[8], ihdr[9], ihdr[12]
    channels = _PNG_CHANNELS.get(color_type)
    if channels is None or interlace != 0 or bit_depth not in (1, 2, 4, 8, 16):
        return None
    if color_type not in (0, 3) and bit_depth < 8:
        return None
    row_bytes = (width * channels * bit_depth + 7) // 8
    expected = height * (1 + row_bytes)
    return expected if expected <= _PNG_MAX_RAW else None


def _png_problems(data: bytes) -> Optional[str]:
    """校验 PNG 结构完整性（纯 stdlib）：chunk 链 / CRC / IDAT / 解压后长度自洽。

    只读 24 字节头只能判断「是不是 PNG」：截断、CRC 坏、像素数据被破坏的图照样能
    骗过尺寸检查，投平台时才发现封面打不开。这里把 chunk 链走完、逐个校验 CRC，
    再把 IDAT 拼起来解压一次、按 IHDR 核对扫描线字节数。
    """
    total = len(data)
    offset = 8
    first = True
    seen_iend = False
    idat = bytearray()
    expected: Optional[int] = None
    while offset + 12 <= total:
        length = int.from_bytes(data[offset:offset + 4], "big")
        chunk_type = data[offset + 4:offset + 8]
        if not all(32 <= byte <= 126 for byte in chunk_type):
            return "PNG 第 %d 字节处的 chunk 类型不合法" % (offset,)
        end = offset + 12 + length
        if end > total:
            return ("PNG 的 %s chunk 被截断（声明 %d 字节，文件只剩 %d 字节）"
                    % (chunk_type.decode("ascii", "replace"), length, total - offset - 12))
        crc = int.from_bytes(data[offset + 8 + length:end], "big")
        actual = zlib.crc32(bytes(chunk_type) + data[offset + 8:offset + 8 + length]) & 0xFFFFFFFF
        if crc != actual:
            return "PNG 的 %s chunk CRC 校验失败（像素数据损坏或文件被动过）" % (
                chunk_type.decode("ascii", "replace"),)
        if chunk_type not in (b"IHDR", b"PLTE", b"IDAT", b"IEND") and chunk_type[0:1].isupper():
            return ("PNG 含未知 critical chunk %r（首字母大写，规范要求解码器拒绝）"
                    % (chunk_type.decode("ascii", "replace"),))
        if first:
            if chunk_type != b"IHDR":
                return "PNG 第一个 chunk 不是 IHDR"
            if length != 13:
                return "PNG 的 IHDR 长度应为 13（实际 %d）" % (length,)
            ihdr_payload = data[offset + 8:offset + 8 + length]
            bit_depth, color_type = ihdr_payload[8], ihdr_payload[9]
            width = int.from_bytes(data[16:20], "big")
            height = int.from_bytes(data[20:24], "big")
            if width <= 0 or height <= 0:
                return "PNG 尺寸不合法：%dx%d" % (width, height)
            if color_type not in _PNG_CHANNELS:
                return "PNG 的 color_type=%d 不认识（合法值 0/2/3/4/6）" % (color_type,)
            if bit_depth not in (1, 2, 4, 8, 16):
                return "PNG 的 bit_depth=%d 不合法" % (bit_depth,)
            if color_type not in (0, 3) and bit_depth < 8:
                return ("PNG 的 bit_depth=%d 与 color_type=%d 组合不合法"
                        % (bit_depth, color_type))
            expected = _png_expected_raw_size(ihdr_payload, width, height)
            first = False
        elif chunk_type == b"IHDR":
            return "PNG 里出现了重复的 IHDR chunk"
        elif chunk_type == b"IDAT":
            idat.extend(data[offset + 8:offset + 8 + length])
        elif chunk_type == b"IEND":
            seen_iend = True
            if length != 0:
                return "PNG 的 IEND chunk 长度不是 0"
            offset = end
            break
        offset = end
    if not first and not seen_iend:
        return "PNG 缺少 IEND（文件被截断，不是完整 PNG）"
    if not idat:
        return "PNG 没有 IDAT（没有像素数据，不是可显示的图）"
    if offset != total:
        return "PNG 的 IEND 之后还有 %d 字节多余数据" % (total - offset,)
    try:
        engine = zlib.decompressobj()
        raw = engine.decompress(bytes(idat), _PNG_MAX_RAW + 1)
        if engine.unconsumed_tail:
            return "PNG 像素数据超过 %d MB，拒绝继续校验" % (_PNG_MAX_RAW // 1024 // 1024,)
        if not engine.eof:
            return "PNG 像素数据流不完整（zlib 流没有结束标记，文件被截断）"
        raw += engine.flush()
        if engine.unused_data:
            return ("PNG 像素数据流之后还有 %d 字节多余数据（IDAT 被追加了别的内容）"
                    % (len(engine.unused_data),))
    except zlib.error as exc:
        return "PNG 的像素数据解压失败（%s）——文件不完整或已损坏" % (exc,)
    if expected is not None and len(raw) != expected:
        return "PNG 像素数据长度 %d 与 IHDR 声明不符（应为 %d）" % (len(raw), expected)
    return None


def png_size(path: Path) -> Tuple[Optional[Tuple[int, int]], Optional[str]]:
    """读 PNG 取宽高，并校验结构完整性（不依赖 Pillow）。返回 ((w,h), 错误说明)。"""
    target = Path(path)
    try:
        data = target.read_bytes()
    except OSError as exc:
        return None, "读不了 %s：%s" % (target, exc)
    if len(data) < 33 or data[:8] != _PNG_SIGNATURE:
        return None, "不是 PNG 文件"
    if data[12:16] != b"IHDR":
        return None, "PNG 头缺少 IHDR"
    width = int.from_bytes(data[16:20], "big")
    height = int.from_bytes(data[20:24], "big")
    if width <= 0 or height <= 0:
        return None, "PNG 宽高不合法"
    problem = _png_problems(data)
    if problem:
        return None, problem
    return (width, height), None


def image_size(path: Path) -> Tuple[Optional[Tuple[int, int]], Optional[str]]:
    """取图片宽高：PNG 用头解析；其他格式尝试 Pillow（可选依赖）。"""
    if Path(path).suffix.lower() == ".png":
        return png_size(path)
    try:  # pragma: no cover - 本机 Pillow 已装，保留可选降级
        from PIL import Image  # type: ignore

        with Image.open(str(path)) as image:
            return image.size, None
    except Exception as exc:  # noqa: BLE001 - 任何失败都降级成「读不到尺寸」
        return None, "读不到图片尺寸（%s）" % (exc,)


def normalize_hashtags(value: Any) -> List[str]:
    """把话题整理成 ``['#foo', '#bar']``：去重、去空、自动补 #、保留顺序。"""
    if value is None:
        return []
    if isinstance(value, str):
        raw_items = _HASHTAG_SPLIT_RE.split(value)
    elif isinstance(value, (list, tuple)):
        raw_items = []
        for item in value:
            if isinstance(item, str):
                raw_items.extend(_HASHTAG_SPLIT_RE.split(item))
    else:
        return []
    result: List[str] = []
    for raw in raw_items:
        text = str(raw).strip().lstrip("#").strip()
        if not text:
            continue
        tag = "#" + text
        if tag not in result:
            result.append(tag)
    return result


def _is_box_type(raw: bytes) -> bool:
    """box 类型必须是 4 个可打印 ASCII 字符（MP4 规范要求）。"""
    return len(raw) == 4 and all(32 <= byte <= 126 for byte in raw)


def mp4_structure(path: Path) -> Dict[str, Any]:
    """纯 stdlib 读 MP4 顶层 box，返回 {ok, boxes, error}。

    只做结构校验（``ftyp`` + ``moov`` 必须存在、box 长度自洽），不解析编码；
    用来拦住「文本改名成 .mp4」这类假成片。不依赖 ffprobe / Pillow。
    """
    info: Dict[str, Any] = {"ok": False, "boxes": [], "error": None}
    target = Path(path)
    try:
        file_size = target.stat().st_size
    except OSError as exc:
        info["error"] = "读不了文件：%s" % (exc,)
        return info
    if file_size < 24:
        info["error"] = "文件只有 %d 字节，连 MP4 头都不够" % (file_size,)
        return info
    boxes: List[str] = []
    offset = 0
    try:
        with open(str(target), "rb") as handle:
            while offset < file_size:
                handle.seek(offset)
                head = handle.read(8)
                if len(head) < 8:
                    info["error"] = "文件在偏移 %d 处截断（box 头不完整）" % (offset,)
                    return info
                box_size = int.from_bytes(head[:4], "big")
                box_type = head[4:8]
                header_size = 8
                if box_size == 1:
                    largesize = handle.read(8)
                    if len(largesize) < 8:
                        info["error"] = "文件在偏移 %d 处截断（largesize 不完整）" % (offset,)
                        return info
                    box_size = int.from_bytes(largesize, "big")
                    header_size = 16
                elif box_size == 0:
                    box_size = file_size - offset
                if not _is_box_type(box_type):
                    info["error"] = (
                        "偏移 %d 处的 box 类型 %r 不是可打印 ASCII，不是 MP4"
                        % (offset, box_type))
                    return info
                if box_size < header_size or offset + box_size > file_size:
                    info["error"] = (
                        "偏移 %d 处 %s box 长度 %d 越界（文件共 %d 字节）"
                        % (offset, box_type.decode("ascii"), box_size, file_size))
                    return info
                name = box_type.decode("ascii")
                if name not in boxes:
                    boxes.append(name)
                offset += box_size
                if len(boxes) > 64:
                    break
    except OSError as exc:
        info["error"] = "读不了文件：%s" % (exc,)
        return info
    info["boxes"] = boxes
    if "ftyp" not in boxes:
        info["error"] = "没有 ftyp box（不是 MP4/QuickTime 容器）"
        return info
    if "moov" not in boxes:
        info["error"] = "有 ftyp 但缺 moov box（文件损坏或没封装完成）"
        return info
    info["ok"] = True
    return info


def probe_video(path: Path) -> Dict[str, Any]:
    """可选 ffprobe 深检：没装 ffprobe 时 available=False（上层降级 WARN）。"""
    info: Dict[str, Any] = {
        "available": False, "error": None, "has_video": None,
        "duration_s": None, "width": None, "height": None,
    }
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        return info
    info["available"] = True
    try:
        proc = subprocess.run(
            [ffprobe, "-v", "error", "-print_format", "json",
             "-show_format", "-show_streams", str(path)],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        info["error"] = "ffprobe 执行失败：%s" % (exc,)
        return info
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip().splitlines()
        info["error"] = "ffprobe 解析失败：%s" % (
            detail[-1] if detail else "退出码 %d" % (proc.returncode,))
        return info
    try:
        data = json.loads(proc.stdout or "{}")
    except ValueError:
        info["error"] = "ffprobe 输出不是 JSON"
        return info
    if not isinstance(data, dict):
        info["error"] = "ffprobe 输出顶层不是对象"
        return info
    streams = data.get("streams")
    streams = streams if isinstance(streams, list) else []
    video_streams = [
        stream for stream in streams
        if isinstance(stream, dict) and stream.get("codec_type") == "video"
    ]
    info["has_video"] = bool(video_streams)
    fmt = data.get("format")
    if isinstance(fmt, dict) and fmt.get("duration") is not None:
        try:
            info["duration_s"] = float(fmt["duration"])
        except (TypeError, ValueError):
            pass
    if video_streams:
        first = video_streams[0]
        for key in ("width", "height"):
            try:
                info[key] = int(first[key]) if first.get(key) is not None else None
            except (TypeError, ValueError):
                pass
    return info


def read_timing(root: Path) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """读项目 src/timing.json，返回 (timing, 错误说明)。"""
    data, err = load_json_file(Path(root) / "src" / "timing.json")
    if err is not None:
        return None, err
    if not isinstance(data, dict):
        return None, "src/timing.json 顶层不是 JSON 对象"
    return data, None


def discover_covers(cover_dir: Path) -> List[Path]:
    """按 cover-1 / cover-2 … 的自然顺序列出候选封面。"""
    if not Path(cover_dir).is_dir():
        return []
    candidates = [p for p in Path(cover_dir).glob("cover-*.png") if p.is_file()]

    def sort_key(path: Path):
        match = _DIGITS_RE.search(path.stem)
        return (int(match.group(1)) if match else 10 ** 6, path.name)

    return sorted(candidates, key=sort_key)


# ===========================================================================
# 组装导出包
# ===========================================================================

def build_package(
    project_root: Optional[Path] = None,
    video: Optional[str] = None,
    covers: Optional[Sequence[str]] = None,
    subtitles: Optional[str] = None,
    copy_path: Optional[str] = None,
    title: Optional[str] = None,
    hashtags: Any = None,
    body: Optional[str] = None,
) -> Dict[str, Any]:
    """读取项目产物，组装成导出包描述（不写盘、不改文件）。

    ``video/covers/subtitles/copy_path`` 可以是相对项目根的路径；``covers``
    为空时自动从 ``out/covers/cover-*.png`` 发现。标题、话题、正文的优先级：
    CLI 参数 > ``publish/copy.json``（可含按平台的 ``platforms`` 覆盖）>
    ``src/timing.json`` 的 title > 空。
    """
    root = Path(project_root) if project_root is not None else Path(PROJECT_ROOT)
    timing, timing_err = read_timing(root)

    video_path = _resolve(root, video, DEFAULT_VIDEO)
    subtitle_path = _resolve(root, subtitles, DEFAULT_SUBTITLE)
    copy_file = _resolve(root, copy_path, DEFAULT_COPY)
    cover_paths = (
        [_resolve(root, item, DEFAULT_COVER_DIR) for item in (covers or [])]
        if covers
        else discover_covers(root / DEFAULT_COVER_DIR)
    )

    copy_data, copy_err = (None, None)
    if copy_file.is_file():
        copy_data, copy_err = load_json_file(copy_file)
        if copy_err is not None:
            copy_data = None

    global_copy = copy_data if isinstance(copy_data, dict) else {}
    platform_copy = global_copy.get("platforms")
    platform_copy = platform_copy if isinstance(platform_copy, dict) else {}

    resolved_title = (
        (title.strip() if isinstance(title, str) and title.strip() else None)
        or (global_copy.get("title") if isinstance(global_copy.get("title"), str) else None)
        or (timing.get("title") if isinstance(timing, dict) and isinstance(timing.get("title"), str) else None)
        or ""
    )
    resolved_hashtags = normalize_hashtags(
        hashtags if hashtags not in (None, "")
        else global_copy.get("hashtags")
    )
    resolved_body = (
        body if isinstance(body, str)
        else (global_copy.get("body") if isinstance(global_copy.get("body"), str) else "")
    )

    width = timing.get("width") if isinstance(timing, dict) else None
    height = timing.get("height") if isinstance(timing, dict) else None
    duration = timing.get("duration") if isinstance(timing, dict) else None
    captions = timing.get("captions") if isinstance(timing, dict) else None
    captions_bottom = captions.get("bottom") if isinstance(captions, dict) else None

    invalid_numeric: List[str] = []

    def numeric(value: Any, field: str) -> Optional[float]:
        """只接受有限 JSON 数字；字符串 / bool / NaN / Infinity 记进 invalid_numeric。"""
        if value is None:
            return None
        if (isinstance(value, bool) or not isinstance(value, (int, float))
                or not math.isfinite(float(value))):
            invalid_numeric.append(field)
            return None
        return float(value)

    cover_rows: List[Dict[str, Any]] = []
    for path in cover_paths:
        row: Dict[str, Any] = {"path": _rel(root, path), "exists": path.is_file()}
        if path.is_file():
            size, size_err = image_size(path)
            row["width"] = size[0] if size else None
            row["height"] = size[1] if size else None
            row["error"] = size_err
        else:
            row["width"] = None
            row["height"] = None
            row["error"] = "文件不存在"
        cover_rows.append(row)

    video_exists = video_path.is_file()
    video_info: Dict[str, Any] = {
        "path": _rel(root, video_path),
        "exists": video_exists,
        "size": video_path.stat().st_size if video_exists else 0,
    }
    if video_exists:
        # P1：成片结构校验（纯 stdlib）。ffprobe 深检在 main 里按本机能力追加。
        video_info["container"] = mp4_structure(video_path)

    package: Dict[str, Any] = {
        "project": str(root),
        "generated_at": now_iso(),
        "timing_error": timing_err,
        "video": video_info,
        "duration_s": numeric(duration, "duration"),
        "width": numeric(width, "width"),
        "height": numeric(height, "height"),
        "aspect": (timing.get("aspect") if isinstance(timing, dict) else None),
        "covers": cover_rows,
        "cover_max_candidates": COVER_MAX_CANDIDATES,
        "subtitles": {
            "path": _rel(root, subtitle_path),
            "exists": subtitle_path.is_file(),
            "size": subtitle_path.stat().st_size if subtitle_path.is_file() else 0,
        },
        "copy": {
            "path": _rel(root, copy_file),
            "exists": copy_file.is_file(),
            "error": copy_err,
        },
        "title": resolved_title,
        "hashtags": resolved_hashtags,
        "body": resolved_body,
        "platforms_copy": platform_copy,
        "captions": {"bottom": numeric(captions_bottom, "captions.bottom")},
        "invalid_numeric_fields": invalid_numeric,
    }
    return package


# ===========================================================================
# 校验（纯函数）
# ===========================================================================

def _payload_for(package: Dict[str, Any], platform: str) -> Dict[str, Any]:
    """某平台的文案：全局值 + copy.json 里该平台的覆盖字段。"""
    overrides = package.get("platforms_copy") or {}
    override = overrides.get(platform) if isinstance(overrides, dict) else None
    override = override if isinstance(override, dict) else {}
    title = override.get("title")
    body = override.get("body")
    hashtags = override.get("hashtags")
    return {
        "title": title.strip() if isinstance(title, str) and title.strip() else package.get("title", ""),
        "body": body if isinstance(body, str) else package.get("body", ""),
        "hashtags": normalize_hashtags(hashtags) if hashtags is not None
        else list(package.get("hashtags") or []),
    }


def _probe_results(package: Dict[str, Any], probe: Dict[str, Any]) -> List[Dict[str, str]]:
    """ffprobe 深检结果的判定：没有 ffprobe 显式 WARN 降级，解析失败才 ERROR。"""
    results: List[Dict[str, str]] = []
    if not probe.get("available"):
        results.append(_res(
            "WARN", "成片深检",
            "本机没有 ffprobe：只校验了 MP4 容器结构（ftyp/moov），"
            "建议在装配 ffmpeg 的机器上复跑以校验容器/编码/时长/分辨率"))
        return results
    if probe.get("error"):
        results.append(_res("ERROR", "成片深检", "%s" % (probe["error"],)))
        return results
    if probe.get("has_video") is False:
        results.append(_res("ERROR", "成片深检", "ffprobe 没发现视频流（容器里只有音频或已损坏）"))
        return results
    duration = probe.get("duration_s")
    duration_text = "%.2fs" % (duration,) if isinstance(duration, (int, float)) else "未知"
    results.append(_res("OK", "成片深检", "ffprobe 解析通过（视频流存在，时长 %s）" % (duration_text,)))
    expected_duration = package.get("duration_s")
    if all(isinstance(value, (int, float)) and not isinstance(value, bool)
           for value in (duration, expected_duration)):
        if abs(float(duration) - float(expected_duration)) > 1.0:
            results.append(_res(
                "WARN", "成片深检",
                "ffprobe 时长 %.2fs 与 timing.json %.2fs 相差超过 1s（建议核对渲染时长）"
                % (float(duration), float(expected_duration))))
    probed_w, probed_h = probe.get("width"), probe.get("height")
    expected_w, expected_h = package.get("width"), package.get("height")
    if all(isinstance(value, (int, float)) and not isinstance(value, bool)
           for value in (probed_w, probed_h, expected_w, expected_h)):
        if (int(probed_w), int(probed_h)) != (int(expected_w), int(expected_h)):
            results.append(_res(
                "WARN", "成片深检",
                "ffprobe 分辨率 %dx%d 与 timing.json %gx%g 不一致（4K 交付/平台变体请人工确认）"
                % (int(probed_w), int(probed_h), float(expected_w), float(expected_h))))
    return results


def _common_results(package: Dict[str, Any]) -> List[Dict[str, str]]:
    """与平台无关的导出包完整性 / 成片规格检查。"""
    results: List[Dict[str, str]] = []

    if package.get("timing_error"):
        results.append(_res("ERROR", "项目", "读不到 src/timing.json（%s）"
                            % (package.get("timing_error"),)))

    invalid_numeric = package.get("invalid_numeric_fields") or []
    if invalid_numeric:
        # NaN / Infinity 的比较全是 False：不显式拦下就会静默通过所有「≥ / ≤」口径。
        results.append(_res(
            "ERROR", "timing 字段",
            "timing.json 这些字段必须是有限的 JSON 数字（字符串 / null / NaN / Infinity 都不行）：%s"
            "——NaN/Infinity 会让大小比较全部为假，等于把坏数据静默放行"
            % ("、".join(sorted(set(invalid_numeric))),)))

    video = package.get("video") or {}
    if not video.get("exists"):
        results.append(_res("ERROR", "成片", "找不到 %s（先渲染 deliver check 的成片）"
                            % (video.get("path"),)))
    elif not isinstance(video.get("size"), int) or video.get("size", 0) <= 0:
        results.append(_res("ERROR", "成片", "%s 是 0 字节" % (video.get("path"),)))
    else:
        size_mb = video.get("size", 0) / 1024.0 / 1024.0
        container = video.get("container")
        if isinstance(container, dict) and not container.get("ok"):
            results.append(_res(
                "ERROR", "成片容器",
                "%s 不是有效的 MP4：%s" % (video.get("path"), container.get("error") or "结构校验失败")))
        elif isinstance(container, dict):
            results.append(_res(
                "OK", "成片",
                "%s（%.1f MB，MP4 容器：%s）"
                % (video.get("path"), size_mb, "/".join(container.get("boxes") or []))))
        else:  # 兼容旧 package dict（没有 container 字段）
            results.append(_res("OK", "成片", "%s（%.1f MB）"
                                % (video.get("path"), size_mb)))
        probe = video.get("probe")
        if isinstance(probe, dict):
            results.extend(_probe_results(package, probe))

    width, height = package.get("width"), package.get("height")
    if isinstance(width, (int, float)) and isinstance(height, (int, float)):
        if width >= height:
            results.append(_res(
                "ERROR", "画幅",
                "成片是 %gx%g（横版）——本闸门面向竖屏平台（1080x1920 9:16）"
                % (width, height)))
        else:
            results.append(_res("OK", "画幅", "竖屏 %gx%g（%s）"
                                % (width, height, package.get("aspect") or "9:16")))
    else:
        results.append(_res("ERROR", "画幅", "timing.json 里没有合法的 width / height"))

    covers = package.get("covers") or []
    cover_errors = 0
    for index, cover in enumerate(covers, start=1):
        label = "封面 %d" % (index,)
        if not cover.get("exists"):
            results.append(_res("ERROR", label, "找不到 %s" % (cover.get("path"),)))
            cover_errors += 1
            continue
        cw, ch = cover.get("width"), cover.get("height")
        if cw is None or ch is None:
            results.append(_res("ERROR", label, "读不到尺寸：%s（%s）"
                                % (cover.get("path"), cover.get("error") or "未知原因")))
            cover_errors += 1
            continue
        if (cw, ch) != (COVER_WIDTH, COVER_HEIGHT):
            results.append(_res("ERROR", label, "%s 是 %dx%d，必须是 %dx%d"
                                % (cover.get("path"), cw, ch, COVER_WIDTH, COVER_HEIGHT)))
            cover_errors += 1
        else:
            results.append(_res("OK", label, "%s（%dx%d）"
                                % (cover.get("path"), cw, ch)))
    if not covers:
        results.append(_res("ERROR", "封面", "没有候选封面：先跑 python scripts/deliver.py cover"))
    elif cover_errors == 0 and len(covers) > COVER_MAX_CANDIDATES:
        results.append(_res("WARN", "封面", "候选封面 %d 张，发布时仍只挑 1 张（建议保留 ≤%d 张）"
                            % (len(covers), COVER_MAX_CANDIDATES)))

    subtitles = package.get("subtitles") or {}
    if not subtitles.get("exists"):
        results.append(_res(
            "WARN", "字幕",
            "没找到 %s（若用画面内 <Caption> 硬字幕可忽略；否则先跑 subtitles.py srt）"
            % (subtitles.get("path"),)))
    elif subtitles.get("size", 0) <= 0:
        results.append(_res("WARN", "字幕", "%s 是空文件" % (subtitles.get("path"),)))
    else:
        results.append(_res("OK", "字幕", "%s" % (subtitles.get("path"),)))

    copy_info = package.get("copy") or {}
    if not copy_info.get("exists"):
        results.append(_res(
            "WARN", "文案清单",
            "没找到 %s（标题/话题/正文将退回 timing.json 标题，建议写一份平台文案）"
            % (copy_info.get("path"),)))
    elif copy_info.get("error"):
        results.append(_res("WARN", "文案清单", copy_info.get("error")))
    else:
        results.append(_res("OK", "文案清单", "%s" % (copy_info.get("path"),)))

    captions = package.get("captions") or {}
    bottom = captions.get("bottom")
    if isinstance(bottom, (int, float)) and not isinstance(bottom, bool):
        if float(bottom) < SAFE_BOTTOM - 1e-9:
            results.append(_res(
                "ERROR", "字幕安全区",
                "timing.json captions.bottom=%.3f，落在底部 %.0f%% 安全区里（平台按钮会挡住）"
                % (float(bottom), SAFE_BOTTOM * 100)))
        else:
            results.append(_res("OK", "字幕安全区", "captions.bottom=%.3f ≥ %.2f" % (float(bottom), SAFE_BOTTOM)))
    else:
        results.append(_res(
            "WARN", "字幕安全区",
            "timing.json 没声明 captions.bottom，无法自动核验底部 18% 安全区（请人工核对）"))

    return results


def _platform_results(package: Dict[str, Any], platform: str) -> List[Dict[str, str]]:
    """单平台约束：标题长度、话题数、正文长度、时长、封面数。"""
    rules = PLATFORM_RULES[platform]
    name = rules["name"]
    results: List[Dict[str, str]] = []
    payload = _payload_for(package, platform)
    title = payload["title"]
    hashtags = payload["hashtags"]
    body = payload["body"]

    if not title.strip():
        results.append(_res("ERROR", "%s：标题" % name, "标题为空"))
    elif len(title.strip()) > rules["title_max"]:
        results.append(_res(
            "ERROR", "%s：标题" % name,
            "%d 字超过 %d 字上限：%r" % (len(title.strip()), rules["title_max"], title.strip())))
    elif len(title.strip()) < rules["title_min"]:
        results.append(_res("ERROR", "%s：标题" % name, "标题太短"))
    else:
        results.append(_res("OK", "%s：标题" % name,
                            "%d 字（≤%d）" % (len(title.strip()), rules["title_max"])))

    if len(hashtags) < rules["hashtags_min"]:
        # P4：话题数与标题/正文一样属于「平台口径」，0 ERROR 才允许发布。
        results.append(_res("ERROR", "%s：话题" % name,
                            "只有 %d 个话题，平台要求 %d~%d 个"
                            % (len(hashtags), rules["hashtags_min"], rules["hashtags_max"])))
    elif len(hashtags) > rules["hashtags_max"]:
        results.append(_res("ERROR", "%s：话题" % name,
                            "%d 个话题超过 %d 个上限"
                            % (len(hashtags), rules["hashtags_max"])))
    else:
        results.append(_res("OK", "%s：话题" % name,
                            "%d 个（%s）" % (len(hashtags), " ".join(hashtags))))

    if len(body) > rules["body_max"]:
        results.append(_res("ERROR", "%s：正文" % name,
                            "%d 字超过 %d 字上限" % (len(body), rules["body_max"])))
    else:
        results.append(_res("OK", "%s：正文" % name, "%d 字（≤%d）"
                            % (len(body), rules["body_max"])))

    duration = package.get("duration_s")
    if not isinstance(duration, (int, float)) or isinstance(duration, bool):
        results.append(_res("WARN", "%s：时长" % name, "timing.json 没有 duration，无法核对"))
    elif duration < rules["duration_min_s"] or duration > rules["duration_max_s"]:
        results.append(_res(
            "ERROR", "%s：时长" % name,
            "%gs 超出 %d~%ds 区间" % (duration, rules["duration_min_s"], rules["duration_max_s"])))
    else:
        results.append(_res("OK", "%s：时长" % name,
                            "%gs（%d~%ds）" % (duration, rules["duration_min_s"], rules["duration_max_s"])))

    cover_count = sum(1 for cover in package.get("covers") or [] if cover.get("exists"))
    if cover_count < rules["cover_min"]:
        results.append(_res("ERROR", "%s：封面" % name, "没有可用封面"))
    else:
        results.append(_res("OK", "%s：封面" % name, "%d 张候选" % (cover_count,)))

    return results


def validate_package(
    package: Dict[str, Any], platforms: Optional[Sequence[str]] = None
) -> List[Dict[str, str]]:
    """校验导出包与各平台约束，返回 ERROR / WARN / OK 行。"""
    selected = list(platforms) if platforms else list(PLATFORM_ORDER)
    if "all" in selected:
        selected = list(PLATFORM_ORDER)
    unknown = [p for p in selected if p not in PLATFORM_RULES]
    if unknown:
        return [_res("ERROR", "平台", "不认识的平台：%s（允许：%s）"
                     % (", ".join(unknown), " / ".join(PLATFORM_ORDER)))]
    results = _common_results(package)
    for platform in selected:
        results.extend(_platform_results(package, platform))
    return results


#: 可选闸门脚本（frame_check / transcript_check）的模块缓存。
_SIBLING_MODULES: Dict[str, Any] = {}


def _load_sibling(filename: str, module_name: str) -> Optional[Any]:
    """按文件路径加载同级脚本；失败返回 None（可选闸门显式报 ERROR，不静默跳过）。"""
    if module_name in _SIBLING_MODULES:
        return _SIBLING_MODULES[module_name]
    path = Path(__file__).resolve().parent / filename
    module: Optional[Any] = None
    if path.is_file():
        try:
            spec = importlib.util.spec_from_file_location(module_name, str(path))
            loaded = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
            spec.loader.exec_module(loaded)  # type: ignore[union-attr]
            module = loaded
        except Exception:  # noqa: BLE001 - 加载失败由调用方转成显式 ERROR
            module = None
    _SIBLING_MODULES[module_name] = module
    return module


def _problem_rows(item: str, problems: Sequence[Dict[str, Any]]) -> List[Dict[str, str]]:
    rows: List[Dict[str, str]] = []
    for problem in problems or ():
        level = "ERROR" if str(problem.get("level", "")).upper() == "ERROR" else "WARN"
        rows.append(_res(level, item, str(problem.get("message") or problem)))
    return rows


def _frames_gate_rows(root: Path, frames: Optional[str]) -> List[Dict[str, str]]:
    """可选闸门：机器验证「代理真的看过画面」（frame_check.py）。

    只有显式传 ``--frames`` 时才启用，默认不跑 —— 旧项目没跑过 ``npm run stills``
    时不应被这条新闸门拦死（deliver.py check 有自己的自动降级口径）。
    """
    frame_check = _load_sibling("frame_check.py", "pf_frame_check")
    if frame_check is None:
        return [_res("ERROR", "静帧校验",
                     "找不到同级 scripts/frame_check.py，无法校验静帧（scripts 目录不完整？）")]
    target = _resolve(root, frames, "out/stills")
    if not target.is_dir():
        return [_res("ERROR", "静帧校验",
                     "找不到静帧目录 %s（先跑 npm run stills -- --scenes，再逐张看过联系表）"
                     % (_rel(root, target),))]
    try:
        timing_data, _timing_error = read_timing(Path(root))
        result = frame_check.check_frames(
            target, timing=timing_data,
            timing_path=Path(root) / "src" / "timing.json",
            project_root=Path(root))
    except Exception as exc:  # noqa: BLE001 - 可选闸门失败要显式 ERROR，不能静默
        return [_res("ERROR", "静帧校验", "frame_check 执行失败：%s" % (exc,))]
    rows = _problem_rows("静帧校验", result.get("problems") or [])
    stats = result.get("stats") or {}
    if not rows:
        rows.append(_res(
            "OK", "静帧校验",
            "%d 帧 / %d 场景 · 覆盖率 %.0f%% · 最小帧方差 %s（尺寸与 timing 一致）"
            % (stats.get("frame_count") or 0, stats.get("scene_count") or 0,
               (stats.get("coverage") or 0.0) * 100.0,
               ("%.1f" % float(stats["min_frame_variance"]))
               if stats.get("min_frame_variance") is not None
               else "n/a（JPEG 未解码像素）")))
    elif result.get("ok"):
        rows.append(_res("OK", "静帧校验", "%d 帧通过（有 WARN，见上）"
                         % (stats.get("frame_count", 0),)))
    return rows


def _transcript_gate_rows(root: Path, transcript: Optional[str]) -> List[Dict[str, str]]:
    """可选闸门：转写反查（transcript_check.py），只在显式传 ``--transcript`` 时启用。

    这是「零依赖 STT 替代」：用户提供 txt/srt 转写，与 timing.json 的 voice.lines
    逐句比对；漏行 / 顺序错位 / 字符覆盖率不足 / 品牌名错一个字都 ERROR。
    """
    if not transcript:
        return []
    transcript_check = _load_sibling("transcript_check.py", "pf_transcript_check")
    if transcript_check is None:
        return [_res("ERROR", "转写校验",
                     "找不到同级 scripts/transcript_check.py，无法做转写反查（scripts 目录不完整？）")]
    path = _resolve(root, transcript, transcript)
    try:
        result = transcript_check.check_transcript(path, project_root=Path(root))
    except Exception as exc:  # noqa: BLE001 - 可选闸门失败要显式 ERROR
        return [_res("ERROR", "转写校验", "transcript_check 执行失败：%s" % (exc,))]
    rows = _problem_rows("转写校验", result.get("problems") or [])
    stats = result.get("stats") or {}
    if not rows:
        rows.append(_res(
            "OK", "转写校验",
            "%d 行字幕 / %d 行转写 · 字符覆盖率 %.1f%% · 行覆盖率 %.1f%% · 平均相似度 %.2f"
            % (stats.get("ref_lines", 0), stats.get("transcript_lines", 0),
               (result.get("coverage") or 0.0) * 100.0,
               (result.get("line_coverage") or 0.0) * 100.0,
               result.get("mean_similarity") or 0.0)))
    elif result.get("ok"):
        rows.append(_res("OK", "转写校验", "转写与字幕源一致（有 WARN，见上）"))
    return rows


def _platform_of_item(item: str) -> Optional[str]:
    """按检查项名前缀判断它属于哪个平台（``抖音：标题`` → ``douyin``）。"""
    for platform, rules in PLATFORM_RULES.items():
        if item.startswith(rules["name"] + "："):
            return platform
    return None


def _apply_strict(results: List[Dict[str, str]]) -> List[Dict[str, str]]:
    """``--strict``：把 WARN 升级成 ERROR（manifest 与退出码必须看同一份结果）。"""
    return [
        _res("ERROR", row["item"], row["message"] + "（--strict：WARN 升级为 ERROR）")
        if row["level"] == "WARN" else row
        for row in results
    ]


def build_manifest(
    package: Dict[str, Any],
    results: List[Dict[str, str]],
    platforms: Optional[Sequence[str]] = None,
) -> Dict[str, Any]:
    """把导出包与**最终**检查结果整理成 publish/manifest.json 的内容。

    P2：entries、summary、ready_platforms 全部从同一份 ``results`` 派生
    （``--strict`` 升级后的结果必须先传到本函数），不再用 package 重算，
    避免出现「summary 不可发布、platforms 却 publishable=true」的自相矛盾。
    """
    selected = list(platforms) if platforms else list(PLATFORM_ORDER)
    if "all" in selected:
        selected = list(PLATFORM_ORDER)
    common_rows = [row for row in results if _platform_of_item(row["item"]) is None]
    common_errors = sum(1 for row in common_rows if row["level"] == "ERROR")
    common_warnings = sum(1 for row in common_rows if row["level"] == "WARN")

    platform_entries: Dict[str, Any] = {}
    for platform in selected:
        name = PLATFORM_RULES[platform]["name"]
        payload = _payload_for(package, platform)
        platform_rows = [row for row in results if row["item"].startswith(name + "：")]
        errors = common_errors + sum(1 for row in platform_rows if row["level"] == "ERROR")
        warnings = common_warnings + sum(1 for row in platform_rows if row["level"] == "WARN")
        cover = next((c["path"] for c in package.get("covers") or [] if c.get("exists")), None)
        platform_entries[platform] = {
            "name": name,
            "title": payload["title"],
            "hashtags": payload["hashtags"],
            "body": payload["body"],
            "cover": cover,
            "checks": [row["message"] for row in common_rows if row["level"] == "OK"]
                      + [row["message"] for row in platform_rows if row["level"] == "OK"],
            "errors": errors,
            "warnings": warnings,
            "publishable": errors == 0,
        }

    ready = [p for p, entry in platform_entries.items() if entry["publishable"]]
    total_errors = sum(1 for row in results if row["level"] == "ERROR")
    total_warnings = sum(1 for row in results if row["level"] == "WARN")
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": now_iso(),
        "generator": "publish_check.py",
        "rule_source": RULE_SOURCE,
        "project": package.get("project"),
        "package": {
            "video": package.get("video"),
            "duration_s": package.get("duration_s"),
            "width": package.get("width"),
            "height": package.get("height"),
            "aspect": package.get("aspect"),
            "covers": package.get("covers"),
            "subtitles": package.get("subtitles"),
            "copy": package.get("copy"),
            "title": package.get("title"),
            "hashtags": package.get("hashtags"),
            "body": package.get("body"),
        },
        "platforms": platform_entries,
        "summary": {
            "errors": total_errors,
            "warnings": total_warnings,
            "publishable": total_errors == 0,
            "ready_platforms": ready,
        },
        "note": "不自动发布：账号风控与平台合规由人负责；本清单只做发布前检查",
    }


def write_manifest(
    project_root: Optional[Path],
    manifest: Dict[str, Any],
    out: Optional[str] = None,
) -> Path:
    """写 publish/manifest.json（UTF-8 / LF / 中文不转义），返回路径。"""
    root = Path(project_root) if project_root is not None else Path(PROJECT_ROOT)
    target = _resolve(root, out, DEFAULT_MANIFEST)
    ensure_dir(target.parent)
    text = json.dumps(manifest, ensure_ascii=False, indent=2) + "\n"
    with open(str(target), "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
    return target


# ===========================================================================
# 输出
# ===========================================================================

def _split_levels(results: List[Dict[str, str]]) -> Tuple[int, int]:
    return (
        sum(1 for row in results if row["level"] == "ERROR"),
        sum(1 for row in results if row["level"] == "WARN"),
    )


def _emit(level: str, item: str, message: str) -> None:
    print("%s: %s — %s" % (level, item, message), flush=True)


def _print_results(results: List[Dict[str, str]], package: Dict[str, Any],
                   manifest_path: Optional[Path]) -> None:
    log("发布合规检查：%s" % (package.get("project"),))
    for row in results:
        _emit(row["level"], row["item"], row["message"])
    errors, warnings = _split_levels(results)
    print("", flush=True)
    print("发布合规检查：%d ERROR / %d WARN（%d 个平台口径）"
          % (errors, warnings, len(PLATFORM_ORDER)), flush=True)
    if manifest_path is not None:
        print("清单：%s（不自动发布：账号风控与平台合规由人负责）"
              % (manifest_path,), flush=True)
    if errors == 0:
        print("0 ERROR：导出包与平台口径检查通过，可以人工发布。", flush=True)


def _json_payload(results: List[Dict[str, str]], package: Dict[str, Any],
                  manifest: Dict[str, Any], platforms: Sequence[str],
                  manifest_path: Optional[Path]) -> str:
    errors, warnings = _split_levels(results)
    payload = {
        "ok": errors == 0,
        "errors": errors,
        "warnings": warnings,
        "project": package.get("project"),
        "platforms": list(platforms),
        "ready_platforms": manifest["summary"]["ready_platforms"],
        "publishable": manifest["summary"]["publishable"],
        "manifest": str(manifest_path) if manifest_path is not None else None,
        "summary": manifest["summary"],
        "results": results,
    }
    return json.dumps(payload, ensure_ascii=False)


# ===========================================================================
# 自测（纯函数 + 临时目录夹具；不渲染、不联网、不依赖 ffmpeg）
# ===========================================================================

def _write_text_lf(path: Path, text: str) -> None:
    """UTF-8 + LF 写文本（Python 3.9 兼容：不用 3.10+ 的 write_text(newline=)）。"""
    with open(str(path), "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)


def _write_png(path: Path, width: int = COVER_WIDTH, height: int = COVER_HEIGHT,
               color: Tuple[int, int, int] = (0, 0, 0), pattern: bool = False) -> None:
    """写一张真实可解码的 RGB PNG（纯 stdlib，自测夹具用）。

    以前这里只写 24 字节 PNG 头 + IHDR，够 ``png_size`` 读尺寸，但那正是
    「截断封面也能过闸门」的 bug 成因。现在写完整 PNG：IHDR + IDAT + IEND，
    每个 chunk 带 CRC，解压后扫描线字节数与 IHDR 自洽。
    ``pattern=True`` 时像素有梯度（给静帧空白判定用；默认纯色）。
    """
    rows: List[bytes] = []
    for y in range(int(height)):
        if pattern:
            row = bytearray()
            for x in range(int(width)):
                row += bytes(((x * 7 + y * 3) % 256, (x * 3 + y * 11) % 256,
                              (x * 13 + y * 5) % 256))
            rows.append(b"\x00" + bytes(row))
        else:
            rows.append(b"\x00" + bytes(color) * int(width))
    raw = b"".join(rows)

    def chunk(kind: bytes, payload: bytes) -> bytes:
        return (len(payload).to_bytes(4, "big") + kind + payload
                + (zlib.crc32(kind + payload) & 0xFFFFFFFF).to_bytes(4, "big"))

    ihdr = (int(width).to_bytes(4, "big") + int(height).to_bytes(4, "big")
            + bytes([8, 2, 0, 0, 0]))
    data = (_PNG_SIGNATURE + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(raw, 9))
            + chunk(b"IEND", b""))
    ensure_dir(Path(path).parent)
    Path(path).write_bytes(data)


def _minimal_mp4() -> bytes:
    """最小 MP4 容器结构：``ftyp`` + 空 ``moov``。

    只满足本闸门的结构校验（不是可播放视频）；测试夹具用它替代文本假成片，
    避免「文本文件也能通过」的 P1 回归。
    """
    ftyp_body = b"isom" + (0x200).to_bytes(4, "big") + b"isomiso2mp41"
    ftyp = (8 + len(ftyp_body)).to_bytes(4, "big") + b"ftyp" + ftyp_body
    moov = (8).to_bytes(4, "big") + b"moov"
    return ftyp + moov


def _selftest() -> int:
    """纯函数自测；全部通过返回 0，任一失败打印 ERROR 并返回 1。"""
    failures = []

    def check(name: str, got, want) -> None:
        if got != want:
            failures.append("%s: 期望 %r，实际 %r" % (name, want, got))

    # --- 话题归一化 ---
    check("话题字符串", normalize_hashtags("独立开发，效率工具 # 独立开发"),
          ["#独立开发", "#效率工具"])
    check("话题数组", normalize_hashtags(["#a", "b", ""]), ["#a", "#b"])
    check("话题空值", normalize_hashtags(None), [])
    check("话题非法类型", normalize_hashtags(12), [])

    # --- P1 ffprobe 深检判定（纯函数，不依赖本机有没有 ffprobe）---
    fake_probe = {"available": False}
    check("无 ffprobe → 深检 WARN",
          [row["level"] for row in _probe_results({"width": 1080, "height": 1920}, fake_probe)],
          ["WARN"])
    bad_probe = {"available": True, "error": "ffprobe 解析失败：Invalid data found"}
    check("ffprobe 失败 → 深检 ERROR",
          [row["level"] for row in _probe_results({"width": 1080, "height": 1920}, bad_probe)],
          ["ERROR"])
    ok_probe = {"available": True, "error": None, "has_video": True,
                "duration_s": 45.0, "width": 1080, "height": 1920}
    ok_rows = _probe_results({"duration_s": 45.0, "width": 1080, "height": 1920}, ok_probe)
    check("ffprobe 正常 → 深检 OK", [row["level"] for row in ok_rows], ["OK"])

    # --- PNG 尺寸（走真实临时文件）---
    import tempfile

    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        good_png = root / "cover.png"
        _write_png(good_png, 1080, 1920)
        check("png_size 正常", png_size(good_png), ((1080, 1920), None))
        bad_png = root / "bad.png"
        bad_png.write_bytes(b"not a png")
        size, err = png_size(bad_png)
        check("png_size 非 PNG 报错", (size is None, err is not None), (True, True))
        check("png_size 缺文件报错", png_size(root / "none.png")[0] is None, True)

        # --- 回归 BUG-P2：只有 PNG 头 / 缺 IEND / CRC 坏的封面必须报错（以前只读 24 字节头）---
        header_only = root / "header-only.png"
        header_only.write_bytes(_PNG_SIGNATURE + (13).to_bytes(4, "big") + b"IHDR"
                                + (1080).to_bytes(4, "big") + (1920).to_bytes(4, "big")
                                + b"\x08\x02\x00\x00\x00")
        check("只有 PNG 头的截断封面 → 报错", png_size(header_only)[0] is None, True)
        truncated_png = root / "truncated.png"
        truncated_png.write_bytes(good_png.read_bytes()[:-12])
        size, err = png_size(truncated_png)
        check("PNG 缺 IEND → 报错", (size is None, err is not None), (True, True))
        corrupt_bytes = bytearray(good_png.read_bytes())
        corrupt_bytes[50] ^= 0xFF  # IDAT 数据区，改一个字节让 CRC / 解压失败
        corrupt_png = root / "corrupt.png"
        corrupt_png.write_bytes(bytes(corrupt_bytes))
        size, err = png_size(corrupt_png)
        check("PNG CRC 坏 → 报错", (size is None, err is not None), (True, True))

        # --- R9-V-PNG：publish_check 与 frame_check 的 PNG 结构口径要对齐 ---
        def _png_chunk(kind: bytes, payload: bytes) -> bytes:
            return (len(payload).to_bytes(4, "big") + kind + payload
                    + (zlib.crc32(kind + payload) & 0xFFFFFFFF).to_bytes(4, "big"))

        small_ihdr = ((8).to_bytes(4, "big") + (8).to_bytes(4, "big")
                      + bytes([8, 2, 0, 0, 0]))
        small_raw = b"".join(b"\x00" + bytes((1, 2, 3)) * 8 for _ in range(8))
        small_z = zlib.compress(small_raw)
        small_base = _PNG_SIGNATURE + _png_chunk(b"IHDR", small_ihdr)
        valid_small = root / "small-ok.png"
        valid_small.write_bytes(small_base + _png_chunk(b"IDAT", small_z)
                                + _png_chunk(b"IEND", b""))
        check("小尺寸合法 PNG → 通过", png_size(valid_small), ((8, 8), None))
        bad_ct5 = (_PNG_SIGNATURE
                   + _png_chunk(b"IHDR", (8).to_bytes(4, "big") + (8).to_bytes(4, "big")
                                + bytes([8, 5, 0, 0, 0]))
                   + _png_chunk(b"IDAT", small_z) + _png_chunk(b"IEND", b""))
        png_variants = {
            "未知 critical chunk": small_base + _png_chunk(b"ABCD", b"x")
                                    + _png_chunk(b"IDAT", small_z) + _png_chunk(b"IEND", b""),
            "IDAT 里 zlib 流后尾随垃圾": small_base + _png_chunk(b"IDAT", small_z + b"junk")
                                         + _png_chunk(b"IEND", b""),
            "非法 color_type=5": bad_ct5,
            "重复 IHDR": small_base + _png_chunk(b"IHDR", small_ihdr)
                          + _png_chunk(b"IDAT", small_z) + _png_chunk(b"IEND", b""),
        }
        for index, (label, payload) in enumerate(png_variants.items()):
            variant_path = root / ("variant-%d.png" % index)
            variant_path.write_bytes(payload)
            check("%s → 报错" % (label,), png_size(variant_path)[0] is None, True)

        # --- 组装一个完整假项目 ---
        project = root / "proj"
        (project / "src").mkdir(parents=True)
        (project / "publish").mkdir(parents=True)
        (project / "out" / "covers").mkdir(parents=True)
        (project / "out" / "subtitles").mkdir(parents=True)
        _write_text_lf(project / "src" / "timing.json", json.dumps({
            "title": "小账本 · 示例片",
            "fps": 60, "width": 1080, "height": 1920, "aspect": "9:16",
            "duration": 45, "captions": {"bottom": 0.20},
        }, ensure_ascii=False))
        (project / "out" / "final-1080p.mp4").write_bytes(_minimal_mp4())
        _write_png(project / "out" / "covers" / "cover-1.png")
        _write_png(project / "out" / "covers" / "cover-2.png")
        _write_text_lf(project / "out" / "subtitles" / "zh.srt",
                       "1\n00:00:01,000 --> 00:00:03,000\n示例\n")
        _write_text_lf(project / "publish" / "copy.json", json.dumps({
            "title": "一句话做宣传片",
            "hashtags": ["独立开发", "效率工具"],
            "body": "把这个流程跑通，你也能一键出片。",
            "platforms": {"xiaohongshu": {"title": "小红书专用短标题"}},
        }, ensure_ascii=False))

        package = build_package(project)
        check("成片被识别", package["video"]["exists"], True)
        check("成片容器结构合法（ftyp+moov）",
              package["video"]["container"]["ok"], True)
        check("自动发现 2 张封面", len(package["covers"]), 2)
        check("标题优先 copy.json", package["title"], "一句话做宣传片")
        check("话题归一化", package["hashtags"], ["#独立开发", "#效率工具"])
        check("时长来自 timing", package["duration_s"], 45.0)

        results = validate_package(package)
        errors, warnings = _split_levels(results)
        check("完整假项目 0 ERROR", errors, 0)
        check("完整假项目 0 WARN", warnings, 0)
        manifest = build_manifest(package, results)
        check("manifest 可发布", manifest["summary"]["publishable"], True)
        check("manifest 四平台就绪", len(manifest["summary"]["ready_platforms"]), 4)
        check("manifest 平台标题覆盖",
              manifest["platforms"]["xiaohongshu"]["title"], "小红书专用短标题")
        manifest_path = write_manifest(project, manifest)
        written = json.loads(manifest_path.read_text(encoding="utf-8"))
        check("manifest 写盘可解析", written["summary"]["publishable"], True)
        check("manifest 写明不自动发布", "不自动发布" in written["note"], True)

        # --- P1：文本假 MP4 / 缺 moov 的半截 MP4 → ERROR ---
        (project / "out" / "final-1080p.mp4").write_bytes(
            b"this is not an mp4 file, just text" * 4)
        text_video = build_package(project)
        check("文本假 MP4 → 成片容器 ERROR",
              any(row["level"] == "ERROR" and row["item"] == "成片容器"
                  for row in validate_package(text_video)), True)
        check("文本假 MP4 → 不可发布",
              build_manifest(text_video, validate_package(text_video))["summary"]["publishable"],
              False)
        (project / "out" / "final-1080p.mp4").write_bytes(_minimal_mp4()[:24])
        truncated = build_package(project)
        check("缺 moov 的半截 MP4 → 成片容器 ERROR",
              any(row["level"] == "ERROR" and row["item"] == "成片容器"
                  for row in validate_package(truncated)), True)
        (project / "out" / "final-1080p.mp4").write_bytes(_minimal_mp4())

        # --- 缺成片 → ERROR ---
        (project / "out" / "final-1080p.mp4").unlink()
        missing_video = build_package(project)
        check("缺成片 → ERROR",
              any(row["level"] == "ERROR" and row["item"] == "成片"
                  for row in validate_package(missing_video)), True)
        (project / "out" / "final-1080p.mp4").write_bytes(_minimal_mp4())

        # --- 横版 → ERROR ---
        landscape = build_package(project)
        landscape["width"], landscape["height"] = 1920.0, 1080.0
        check("横版 → 画幅 ERROR",
              any(row["level"] == "ERROR" and row["item"] == "画幅"
                  for row in validate_package(landscape)), True)

        # --- 封面尺寸错误 → ERROR ---
        _write_png(project / "out" / "covers" / "cover-2.png", 1080, 1080)
        wrong_cover = build_package(project)
        check("封面尺寸错 → ERROR",
              any(row["level"] == "ERROR" and row["item"] == "封面 2"
                  for row in validate_package(wrong_cover)), True)
        _write_png(project / "out" / "covers" / "cover-2.png")

        # --- 小红书标题超 20 字 → 只在小红书 ERROR ---
        long_title = build_package(project)
        long_title["title"] = "这是一个特别特别长的标题超过二十个字的小红书标题"
        long_title["platforms_copy"] = {}
        title_results = validate_package(long_title)
        check("小红书标题超长 → ERROR",
              any(row["level"] == "ERROR" and row["item"] == "小红书：标题"
                  for row in title_results), True)
        check("抖音标题同长度不报错（≤30）",
              not any(row["level"] == "ERROR" and row["item"] == "抖音：标题"
                      for row in title_results), True)

        # --- 话题越界 → ERROR（P4：与标题/正文同属发布口径）---
        many_tags = build_package(project)
        many_tags["hashtags"] = ["#%d" % index for index in range(1, 8)]
        check("话题 >5 → 抖音话题 ERROR",
              any(row["level"] == "ERROR" and row["item"] == "抖音：话题"
                  for row in validate_package(many_tags)), True)
        no_tags = build_package(project)
        no_tags["hashtags"] = []
        check("话题 0 个 → 抖音话题 ERROR",
              any(row["level"] == "ERROR" and row["item"] == "抖音：话题"
                  for row in validate_package(no_tags)), True)

        # --- 超时长 → ERROR ---
        too_long = build_package(project)
        too_long["duration_s"] = 1000.0
        check("超平台时长 → ERROR",
              any(row["level"] == "ERROR" and "时长" in row["item"]
                  for row in validate_package(too_long)), True)

        # --- 字幕安全区越界 → ERROR ---
        unsafe = build_package(project)
        unsafe["captions"] = {"bottom": 0.10}
        check("字幕落入底部安全区 → ERROR",
              any(row["level"] == "ERROR" and row["item"] == "字幕安全区"
                  for row in validate_package(unsafe)), True)

        # --- 缺字幕 / 缺文案清单 → WARN 不拦（话题用 CLI 参数补齐，P4 后话题缺失是 ERROR）---
        (project / "out" / "subtitles" / "zh.srt").unlink()
        (project / "publish" / "copy.json").unlink()
        bare = build_package(project, hashtags="独立开发,效率工具", body="流程跑通就能出片。")
        bare_results = validate_package(bare)
        check("缺字幕 + 缺文案 → 0 ERROR", _split_levels(bare_results)[0], 0)
        check("缺字幕 → WARN",
              any(row["level"] == "WARN" and row["item"] == "字幕" for row in bare_results), True)
        check("缺文案清单 → WARN",
              any(row["level"] == "WARN" and row["item"] == "文案清单" for row in bare_results), True)

        # --- P2：--strict 升级后 manifest 必须与最终结果自洽 ---
        strict_results = _apply_strict(bare_results)
        strict_manifest = build_manifest(bare, strict_results)
        strict_errors, strict_warnings = _split_levels(strict_results)
        check("严格模式：WARN 全部升级", strict_warnings, 0)
        check("严格模式：summary.errors 与结果同源",
              strict_manifest["summary"]["errors"], strict_errors)
        check("严格模式：summary 不可发布",
              strict_manifest["summary"]["publishable"], False)
        check("严格模式：平台条目全部不可发布",
              [entry["publishable"] for entry in strict_manifest["platforms"].values()],
              [False] * 4)
        check("严格模式：ready_platforms 为空",
              strict_manifest["summary"]["ready_platforms"], [])
        check("严格模式：平台 errors 与 publishable 自洽",
              all(entry["publishable"] == (entry["errors"] == 0)
                  for entry in strict_manifest["platforms"].values()), True)

        # --- 回归 BUG-P1：NaN / Infinity / 字符串冒充数字 → ERROR（以前会静默通过）---
        nan_proj = root / "nan_proj"
        (nan_proj / "src").mkdir(parents=True)
        for case_name, payload in (
            ("duration=NaN", {"duration": float("nan")}),
            ("duration=Infinity", {"duration": float("inf")}),
            ("width/height=NaN", {"width": float("nan"), "height": float("nan")}),
            ("duration=字符串", {"duration": "45"}),
        ):
            timing_data = {"title": "t", "fps": 60, "width": 1080, "height": 1920,
                           "duration": 45, "captions": {"bottom": 0.2}}
            timing_data.update(payload)
            _write_text_lf(nan_proj / "src" / "timing.json",
                           json.dumps(timing_data, ensure_ascii=False))
            nan_rows = validate_package(build_package(nan_proj))
            check("NaN/Inf/字符串 timing 字段（%s）→ ERROR" % (case_name,),
                  any(row["level"] == "ERROR" and row["item"] == "timing 字段"
                      for row in nan_rows), True)

        # --- Phase2 闸门：静帧校验（frame_check.py 接入，显式 --frames 才启用）---
        frames_proj = root / "frames_proj"
        (frames_proj / "src").mkdir(parents=True)
        (frames_proj / "out" / "stills").mkdir(parents=True)
        _write_text_lf(frames_proj / "src" / "timing.json", json.dumps({
            "title": "f", "fps": 30, "width": 60, "height": 90, "duration": 6,
            "scenes": [{"name": "hook", "start": 0, "end": 3},
                       {"name": "cta", "start": 3, "end": 6}],
        }, ensure_ascii=False))
        _write_png(frames_proj / "out" / "stills" / "0001.00.png", 60, 90, pattern=True)
        _write_png(frames_proj / "out" / "stills" / "0004.00.png", 60, 90, pattern=True)
        frame_rows = _frames_gate_rows(frames_proj, "out/stills")
        check("静帧闸门：正常 2 帧 → 0 ERROR",
              [row for row in frame_rows if row["level"] == "ERROR"], [])
        _write_png(frames_proj / "out" / "stills" / "0002.50.png", 60, 90)
        check("静帧闸门：空白帧 → ERROR",
              any(row["level"] == "ERROR" for row in _frames_gate_rows(frames_proj, "out/stills")),
              True)
        check("静帧闸门：目录缺失 → ERROR",
              any(row["level"] == "ERROR"
                  for row in _frames_gate_rows(frames_proj, "out/nope")), True)

        # --- Phase2 闸门：转写反查（transcript_check.py 接入，只有 --transcript 才启用）---
        transcript_proj = root / "transcript_proj"
        (transcript_proj / "src").mkdir(parents=True)
        _write_text_lf(transcript_proj / "src" / "timing.json", json.dumps({
            "brand": {"name": "青柚记账", "url": "qingyou.app"},
            "voice": {"lines": [
                {"id": "01", "at": 0.5, "text": "每天记账要花十分钟"},
                {"id": "02", "at": 4.0, "text": "打开青柚记账就有答案"},
            ]},
        }, ensure_ascii=False))
        _write_text_lf(transcript_proj / "good.txt",
                       "每天记账要花十分钟\n打开青柚记账就有答案\n")
        _write_text_lf(transcript_proj / "brand-typo.txt",
                       "每天记账要花十分钟\n打开青柚记帐就有答案\n")
        check("转写闸门：未传 transcript 不启用",
              _transcript_gate_rows(transcript_proj, None), [])
        check("转写闸门：逐字一致 → 0 ERROR",
              [row for row in _transcript_gate_rows(transcript_proj, "good.txt")
               if row["level"] == "ERROR"], [])
        check("转写闸门：品牌名错一个字 → ERROR",
              any(row["level"] == "ERROR"
                  for row in _transcript_gate_rows(transcript_proj, "brand-typo.txt")), True)

        # --- 未知平台 → ERROR ---
        check("未知平台 → ERROR",
              validate_package(package, platforms=["weibo"])[0]["level"], "ERROR")

    if failures:
        for message in failures:
            sys.stderr.write("ERROR: selftest — %s\n" % (message,))
        sys.stderr.write("ERROR: selftest — %d 项失败\n" % (len(failures),))
        return 1
    print("OK: selftest — 话题归一化 / PNG 完整解码（截断·CRC·IHDR 拦截） / "
          "MP4 容器结构（含文本假成片拦截） / 导出包完整性 / 四平台标题·话题·正文·时长·封面 / "
          "安全区 / NaN·Infinity·字符串 timing 字段拦截 / 静帧闸门 / 转写反查闸门 / "
          "--strict manifest 自洽 / manifest 写盘 全部通过")
    return 0


# ===========================================================================
# main
# ===========================================================================

def _force_utf8() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):
                pass


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="publish_check.py",
        description="promo-forge 发布合规闸门：四平台发布前检查单（不自动发布）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "输出格式：LEVEL: <检查项> — <说明>；最后一行统计并写 %s。\n"
            "退出码：0 通过（0 ERROR）；1 有 ERROR。\n"
            "平台规则：%s" % (DEFAULT_MANIFEST, RULE_SOURCE)
        ),
    )
    parser.add_argument("--selftest", action="store_true", help="只跑纯函数自测")
    parser.add_argument("--project", default=None, help="项目根目录，默认当前项目（PROMO_ROOT / scripts 上级）")
    parser.add_argument("--video", default=None, help="成片路径，默认 %s" % DEFAULT_VIDEO)
    parser.add_argument("--covers", nargs="*", default=None,
                        help="候选封面路径（可多个），默认自动发现 %s/cover-*.png" % DEFAULT_COVER_DIR)
    parser.add_argument("--subtitles", default=None, help="字幕文件，默认 %s" % DEFAULT_SUBTITLE)
    parser.add_argument("--copy", dest="copy_path", default=None,
                        help="平台文案清单，默认 %s" % DEFAULT_COPY)
    parser.add_argument("--title", default=None, help="覆盖标题（默认取 copy.json / timing.json）")
    parser.add_argument("--hashtags", default=None, help="话题，逗号或空格分隔（默认取 copy.json）")
    parser.add_argument("--body", default=None, help="正文（默认取 copy.json）")
    parser.add_argument("--frames", default=None,
                        help="静帧目录：显式传入才启用 frame_check.py 静帧闸门（默认不查），例如 out/stills")
    parser.add_argument("--transcript", default=None,
                        help="转写文件（txt / srt）：显式传入才启用 transcript_check.py 配音字幕反查（默认不查）")
    parser.add_argument("--platform", default="all",
                        choices=(*PLATFORM_ORDER, "all"),
                        help="只查一个平台，默认 all（四平台全查）")
    parser.add_argument("--strict", action="store_true", help="WARN 也当 ERROR（更严的发布口径）")
    parser.add_argument("--out", default=None, help="清单输出路径，默认 %s" % DEFAULT_MANIFEST)
    parser.add_argument("--no-write", action="store_true", help="只检查，不写 publish/manifest.json")
    parser.add_argument("--format", choices=("text", "json"), default="text",
                        help="输出格式：text（人看）或 json（程序 / MCP 用）")
    return parser


def main(argv=None) -> int:
    _force_utf8()
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    if "--selftest" in raw_argv:
        return _selftest()
    parser = build_parser()
    args = parser.parse_args(raw_argv)

    root = Path(args.project) if args.project else Path(PROJECT_ROOT)
    if not root.is_dir():
        sys.stderr.write("ERROR: 项目根目录不存在：%s\n" % (root,))
        return 1

    package = build_package(
        root,
        video=args.video,
        covers=args.covers,
        subtitles=args.subtitles,
        copy_path=args.copy_path,
        title=args.title,
        hashtags=args.hashtags,
        body=args.body,
    )
    video_path = _resolve(root, args.video, DEFAULT_VIDEO)
    if video_path.is_file() and video_path.stat().st_size > 0:
        # P1 第二层：按本机能力做 ffprobe 深检；没有 ffprobe 时 validate 会显式 WARN。
        package["video"]["probe"] = probe_video(video_path)
    platforms = list(PLATFORM_ORDER) if args.platform == "all" else [args.platform]
    results = validate_package(package, platforms=platforms)
    # Phase2 可选闸门：默认不启用（旧项目没有静帧 / 转写也不该被拦），显式传参才跑。
    if args.frames:
        results.extend(_frames_gate_rows(root, args.frames))
    if args.transcript:
        results.extend(_transcript_gate_rows(root, args.transcript))
    if args.strict:
        # P2：先把 WARN 升级成 ERROR，再让 manifest 与退出码共用这份最终结果。
        results = _apply_strict(results)

    manifest = build_manifest(package, results, platforms=platforms)
    manifest_path: Optional[Path] = None
    if not args.no_write:
        try:
            manifest_path = write_manifest(root, manifest, out=args.out)
        except OSError as exc:
            sys.stderr.write("ERROR: 写不了发布清单：%s\n" % (exc,))
            return 1

    if args.format == "json":
        print(_json_payload(results, package, manifest, platforms, manifest_path), flush=True)
    else:
        _print_results(results, package, manifest_path)

    errors, _warnings = _split_levels(results)
    return 0 if errors == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
