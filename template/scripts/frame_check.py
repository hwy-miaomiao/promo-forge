#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""promo-forge · 阶段 9「静帧机器校验」（frame_check.py）。

为什么要有这个脚本
------------------
AGENTS.md 阶段 9 要求「抽帧联系表必须逐张看过」。以前这句话靠人（和 AI）自觉：
``out/stills/`` 里有没有图、图能不能打开、是不是全黑、尺寸对不对、有没有真的覆盖到
timing.json 的每个场景，全靠肉眼。本脚本把「代理真的看过画面」变成**机器可验证**的一步：
对 ``npm run stills -- --scenes`` 的产物做结构 + 像素级判定，给出 ``ok`` / ``problems``
与退出码，可接进 ``deliver.py check`` / ``publish_check.py`` 的闸门链。

校验项
------
1. **帧数覆盖 timing 场景**：每个 ``timing.json`` 里的场景至少 ``--per-scene`` 帧
   （默认 1 帧；2 帧对应 stills.mjs 的 40% / 80% 两个采样点）；覆盖率低于 ``--coverage`` 即失败。
2. **PNG / JPEG 可解码**：PNG 做完整 chunk 校验（CRC、IHDR、IDAT、IEND）并**真解码像素**；
   JPEG 用纯标准库解析 SOI / SOF / SOS / EOI 结构与尺寸（不解像素，见下）。
3. **像素尺寸**：默认必须等于 timing.json 的 ``width×height``；``--scale`` 对应
   ``stills.mjs --scale``，``--expect WxH`` 可显式指定。
4. **非空白 / 色彩方差**：PNG 解码后按亮度方差判定（全黑 / 全白 / 纯色 = 空白帧 = ERROR）；
   JPEG 因为标准库不解像素，只能按「压缩后体积 / 像素数」启发式判**疑似**空白并 WARN。
5. **每帧时间戳覆盖率**：文件名必须是 stills.mjs 的 ``%06.2f.jpg`` 格式（如 ``0004.20.png``），
   时间戳必须落在某个场景窗口内（不在任何场景里 = WARN）；无法解析时间戳的帧无法核对覆盖。

用法
----
    python scripts/frame_check.py                        # 默认查 out/stills
    python scripts/frame_check.py out/stills --scale 0.5 # 半分辨率抽帧
    python scripts/frame_check.py --per-scene 2          # 每个场景要求 2 帧（--scenes 默认产物）
    python scripts/frame_check.py --json                 # 单行 JSON（给程序 / MCP 用）
    python scripts/frame_check.py --no-timing            # 只验图片本身，不核对场景覆盖
    python scripts/frame_check.py --selftest             # 纯函数自测（不读项目素材）

退出码
------
0 = 0 ERROR（WARN 可接受，逐条说明原因）；1 = 有 ERROR 或缺参数 / 目录不存在。

依赖
----
**纯标准库、零新增依赖**：PNG 用 ``zlib`` 自写最小解码器（8 / 16 bit，非交错；覆盖
Chromium 抽帧的 RGBA8 产物），JPEG 只做头部与结构解析。不需要 Pillow / ffmpeg。

关于 JPEG 与「空白帧」
----------------------
``stills.mjs`` 默认输出 JPEG（quality 85）。纯标准库解 JPEG 像素等于重写一个基线解码器，
不划算；所以本脚本对 JPEG 只保证「结构完整 + 尺寸正确 + 体积启发式」。**要拿到像素级
空白判定，请用 PNG 抽帧**：``npm run stills -- --scenes --format png``。
"""
from __future__ import annotations

import argparse
import json
import math
import re
import struct
import sys
import zlib
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

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------
DEFAULT_STILLS = "out/stills"
DEFAULT_TIMING = "src/timing.json"
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg")
#: stills.mjs 的时间戳文件名格式：``%06.2f``，例如 ``0004.20.jpg`` / ``0012.50.png``。
STAMP_RE = re.compile(r"^(\d{1,6})\.(\d{2})$")
#: 单帧最多解这么多像素；超过就只做结构校验并 WARN（防止误指向 4K/8K 大图时卡死）。
MAX_DECODE_PIXELS = 40_000_000
#: 亮度方差阈值（0~255 亮度尺度）。纯色 / 全黑 / 全白 = 0；有文字或图形的界面远高于 6。
DEFAULT_MIN_VARIANCE = 6.0
#: JPEG「疑似空白」启发式：压缩后字节数 / 像素数。纯色 JPEG ≈0.01~0.02，真实界面 ≥0.05。
DEFAULT_JPEG_SUSPECT_BPP = 0.02
DEFAULT_PER_SCENE = 1
DEFAULT_COVERAGE = 1.0

_PNG_CHANNELS = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}
_PNG_ALLOWED_BIT_DEPTH = {0: (8, 16), 2: (8, 16), 3: (8,), 4: (8, 16), 6: (8, 16)}
_SOF_MARKERS = frozenset(
    {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}
)


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


def _be16(data: bytes, offset: int) -> int:
    return (data[offset] << 8) | data[offset + 1]


def _be32(data: bytes, offset: int) -> int:
    return (data[offset] << 24) | (data[offset + 1] << 16) | (data[offset + 2] << 8) | data[offset + 3]


def _finite(value: Any) -> bool:
    """bool / 非数字 / NaN / Inf 一律 False（与 check_assets.py 的 R6-TPL-03 同口径）。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    return math.isfinite(float(value))


def parse_stamp(name: str) -> Optional[float]:
    """把 stills.mjs 的文件名（去掉扩展名）解析成秒数；不匹配返回 None。"""
    stem = Path(name).stem
    match = STAMP_RE.match(stem)
    if not match:
        return None
    return int(match.group(1)) + int(match.group(2)) / 100.0


def parse_size(text: str) -> Tuple[int, int]:
    """``1080x1920`` → (1080, 1920)；格式不对抛 ValueError（调用方负责转成 ERROR）。"""
    match = re.match(r"^\s*(\d+)\s*[xX]\s*(\d+)\s*$", str(text))
    if not match:
        raise ValueError("尺寸要写成 WxH（例如 1080x1920 或 540x960），现在是 %r" % (text,))
    width, height = int(match.group(1)), int(match.group(2))
    if width <= 0 or height <= 0:
        raise ValueError("尺寸必须是正整数，现在是 %r" % (text,))
    return width, height


def _problem(level: str, message: str) -> Dict[str, str]:
    return {"level": level, "message": message}


# ---------------------------------------------------------------------------
# PNG：chunk 校验 + 最小解码器（纯标准库）
# ---------------------------------------------------------------------------
def _png_chunks(data: bytes) -> Tuple[List[Tuple[bytes, bytes]], Optional[str]]:
    """按 PNG 规范切 chunk 并校验 CRC，返回 (chunks, error)。"""
    if len(data) < 8 + 12:
        return [], "文件太短（%d 字节），不是完整的 PNG" % (len(data),)
    if not data.startswith(PNG_SIGNATURE):
        return [], "PNG 签名不对（文件头不是 \\x89PNG\\r\\n\\x1a\\n）"
    chunks: List[Tuple[bytes, bytes]] = []
    pos = 8
    while pos + 8 <= len(data):
        length = _be32(data, pos)
        ctype = data[pos + 4:pos + 8]
        if length > 0x7FFFFFFF:
            return chunks, "chunk %r 长度字段异常（%d）" % (ctype, length)
        end = pos + 12 + length
        if end > len(data):
            return chunks, ("chunk %r 声明的长度 %d 超出文件末尾（期望到 %d，实际 %d 字节）："
                            "文件被截断" % (ctype, length, end, len(data)))
        payload = data[pos + 8:pos + 8 + length]
        stored_crc = _be32(data, pos + 8 + length)
        actual_crc = zlib.crc32(ctype + payload) & 0xFFFFFFFF
        if stored_crc != actual_crc:
            return chunks, ("chunk %r 的 CRC 校验失败（存储 %08x，实际 %08x）：文件损坏"
                            % (ctype, stored_crc, actual_crc))
        if ctype not in (b"IHDR", b"PLTE", b"IDAT", b"IEND") and not (
                len(ctype) == 4 and ctype[0:1].isalpha()):
            return chunks, "chunk 类型不是 4 个 ASCII 字母：%r" % (ctype,)
        if ctype not in (b"IHDR", b"PLTE", b"IDAT", b"IEND") and ctype[0:1].isupper():
            # PNG 规范：chunk 类型首字母大写 = critical，解码器遇到不认识的 critical
            # chunk 必须报错（以前只要求首字节是字母，未知 critical chunk 会被静默放行）。
            return chunks, ("未知的 critical chunk %r（首字母大写，规范要求解码器拒绝）："
                            "文件可能不是 PNG 或被工具写坏" % (ctype,))
        if ctype == b"IHDR" and chunks:
            return chunks, "PNG 里出现了第二个 IHDR（chunk 链重复/损坏）"
        if ctype == b"IEND" and payload:
            return chunks, "IEND chunk 的长度应为 0，实际 %d 字节：文件结构损坏" % (len(payload),)
        chunks.append((ctype, payload))
        pos = end
        if ctype == b"IEND":
            break
    if pos != len(data):
        if not chunks or chunks[-1][0] != b"IEND":
            return chunks, "chunk 链没有以 IEND 结束（文件被截断）"
        # PNG 规范要求 IEND 是最后一个 chunk；IEND 之后还有字节说明文件被动过。
        return chunks, "IEND 之后还有 %d 字节多余数据（不是合法的 PNG）" % (len(data) - pos,)
    if not chunks:
        return [], "没有任何 PNG chunk"
    return chunks, None


def _paeth(a: int, b: int, c: int) -> int:
    p = a + b - c
    pa = abs(p - a)
    pb = abs(p - b)
    pc = abs(p - c)
    if pa <= pb and pa <= pc:
        return a
    if pb <= pc:
        return b
    return c


def _unfilter(raw: bytes, width: int, height: int, bpp: int) -> Tuple[Optional[List[bytes]], Optional[str]]:
    """逐行反 PNG 行过滤（0 None / 1 Sub / 2 Up / 3 Average / 4 Paeth）。"""
    row_bytes = width * bpp
    if len(raw) != height * (1 + row_bytes):
        return None, ("像素数据长度不符（期望 %d 字节，实际 %d 字节）：IDAT 被截断或尺寸被改过"
                      % (height * (1 + row_bytes), len(raw)))
    rows: List[bytes] = []
    prev = bytearray(row_bytes)
    pos = 0
    for _y in range(height):
        filter_type = raw[pos]
        pos += 1
        row = bytearray(raw[pos:pos + row_bytes])
        pos += row_bytes
        if filter_type == 0:
            pass
        elif filter_type == 1:  # Sub
            for i in range(bpp, row_bytes):
                row[i] = (row[i] + row[i - bpp]) & 0xFF
        elif filter_type == 2:  # Up
            for i in range(row_bytes):
                row[i] = (row[i] + prev[i]) & 0xFF
        elif filter_type == 3:  # Average
            for i in range(row_bytes):
                left = row[i - bpp] if i >= bpp else 0
                row[i] = (row[i] + ((left + prev[i]) >> 1)) & 0xFF
        elif filter_type == 4:  # Paeth
            for i in range(row_bytes):
                left = row[i - bpp] if i >= bpp else 0
                upper = prev[i]
                upper_left = prev[i - bpp] if i >= bpp else 0
                row[i] = (row[i] + _paeth(left, upper, upper_left)) & 0xFF
        else:
            return None, "未知的 PNG 行过滤类型 %d（第 %d 行）" % (filter_type, _y)
        rows.append(bytes(row))
        prev = row
    return rows, None


def _luma_for_pixel(row: bytes, offset: int, color_type: int, sample_bytes: int,
                    palette: Optional[List[Tuple[int, int, int]]],
                    transparency: Optional[Dict[str, Any]] = None) -> Tuple[int, int]:
    """返回 (亮度 0~255, alpha)。只支持 8 / 16 bit（16 bit 取高字节）。

    ``transparency`` 来自 tRNS chunk：调色板图是透明索引集合，灰度/RGB 图是
    全透明的精确采样值。PNG 的 tRNS 只表示「这些像素完全透明」，没有半透明。
    """
    transparent = transparency or {}
    if color_type == 0:  # 灰度
        gray = _be16(row, offset) if sample_bytes == 2 else row[offset]
        alpha = 0 if transparent.get("gray") == gray else 255
        return gray >> 8 if sample_bytes == 2 else gray, alpha
    if color_type == 4:  # 灰度 + alpha
        gray = row[offset]
        alpha = (_be16(row, offset + sample_bytes) if sample_bytes == 2
                 else row[offset + sample_bytes])
        return gray, alpha
    if color_type == 3:  # 调色板
        index = row[offset]
        if palette and 0 <= index < len(palette):
            r, g, b = palette[index]
        else:
            r = g = b = 0
        alpha = 0 if index in transparent.get("palette", ()) else 255
        return (r * 299 + g * 587 + b * 114) // 1000, alpha
    if sample_bytes == 2:
        r16 = _be16(row, offset)
        g16 = _be16(row, offset + 2)
        b16 = _be16(row, offset + 4)
        r, g, b = r16 >> 8, g16 >> 8, b16 >> 8
        rgb_key = transparent.get("rgb")
        if rgb_key is not None and (r16, g16, b16) == rgb_key:
            return (r * 299 + g * 587 + b * 114) // 1000, 0
    else:
        r = row[offset]
        g = row[offset + sample_bytes]
        b = row[offset + 2 * sample_bytes]
        rgb_key = transparent.get("rgb")
        if rgb_key is not None and (r, g, b) == rgb_key:
            return (r * 299 + g * 587 + b * 114) // 1000, 0
    alpha = ((_be16(row, offset + 3 * sample_bytes) if sample_bytes == 2
              else row[offset + 3 * sample_bytes]) if color_type == 6 else 255)
    return (r * 299 + g * 587 + b * 114) // 1000, alpha


def _png_luma_stats(rows: List[bytes], width: int, height: int, color_type: int,
                    bit_depth: int, palette: Optional[List[Tuple[int, int, int]]],
                    transparency: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """对解码后的像素做抽样统计：亮度方差、颜色数、是否全透明。"""
    channels = _PNG_CHANNELS[color_type]
    sample_bytes = bit_depth // 8
    pixel_bytes = channels * sample_bytes
    stride = max(1, int(math.sqrt((width * height) / 200000.0)))
    total = 0
    total_sq = 0
    count = 0
    alpha_nonzero = 0
    colors = set()
    colors_capped = False
    for y in range(0, height, stride):
        row = rows[y]
        for x in range(0, width, stride):
            offset = x * pixel_bytes
            luma, alpha = _luma_for_pixel(row, offset, color_type, sample_bytes, palette,
                                          transparency)
            total += luma
            total_sq += luma * luma
            count += 1
            if alpha:
                alpha_nonzero += 1
            if not colors_capped:
                colors.add((row[offset], row[offset + sample_bytes] if channels >= 3 else row[offset]))
                if len(colors) > 512:
                    colors_capped = True
    if count == 0:
        return {"decoded": False, "error": "没有可采样的像素"}
    mean = total / count
    variance = max(0.0, total_sq / count - mean * mean)
    return {
        "decoded": True,
        "variance": variance,
        "distinct_colors": len(colors) if not colors_capped else 513,
        "samples": count,
        "all_transparent": alpha_nonzero == 0,
        "error": None,
    }


def read_png(path: Path, data: Optional[bytes] = None) -> Dict[str, Any]:
    """校验 PNG 结构并解码像素，返回统一的结果 dict（``problems`` 里是 ERROR/WARN）。"""
    info: Dict[str, Any] = {
        "format": "png", "ok": False, "width": None, "height": None,
        "bit_depth": None, "color_type": None, "interlace": None,
        "decoded": False, "variance": None, "distinct_colors": None,
        "problems": [],
    }
    problems: List[Dict[str, str]] = info["problems"]
    if data is None:
        try:
            data = path.read_bytes()
        except OSError as exc:
            problems.append(_problem("ERROR", "读不了文件：%s" % (exc,)))
            return info
    chunks, error = _png_chunks(data)
    if error:
        problems.append(_problem("ERROR", "PNG 结构校验失败：%s" % (error,)))
        return info
    if not chunks or chunks[0][0] != b"IHDR":
        problems.append(_problem("ERROR", "PNG 的第一个 chunk 不是 IHDR"))
        return info
    ihdr = chunks[0][1]
    if len(ihdr) != 13:
        problems.append(_problem("ERROR", "IHDR 长度应为 13，实际 %d" % (len(ihdr),)))
        return info
    width, height = _be32(ihdr, 0), _be32(ihdr, 4)
    bit_depth, color_type = ihdr[8], ihdr[9]
    compression, filter_method, interlace = ihdr[10], ihdr[11], ihdr[12]
    info.update({"width": width, "height": height, "bit_depth": bit_depth,
                 "color_type": color_type, "interlace": interlace})
    if width <= 0 or height <= 0:
        problems.append(_problem("ERROR", "PNG 尺寸非法：%dx%d" % (width, height)))
        return info
    if color_type not in _PNG_CHANNELS:
        problems.append(_problem("ERROR", "PNG color_type=%d 不认识" % (color_type,)))
        return info
    if bit_depth not in _PNG_ALLOWED_BIT_DEPTH[color_type]:
        problems.append(_problem(
            "ERROR", "PNG bit_depth=%d 与 color_type=%d 不合法（支持 8/16 bit）"
            % (bit_depth, color_type)))
        return info
    if compression != 0 or filter_method != 0:
        problems.append(_problem("ERROR", "PNG 压缩/过滤方法字段非 0（compression=%d, filter=%d）"
                                 % (compression, filter_method)))
        return info
    palette: Optional[List[Tuple[int, int, int]]] = None
    trns_payload: Optional[bytes] = None
    idats = []
    for ctype, payload in chunks[1:]:
        if ctype == b"PLTE":
            if len(payload) % 3 != 0 or not payload:
                problems.append(_problem("ERROR", "PLTE 长度 %d 不是 3 的倍数" % (len(payload),)))
                return info
            palette = [(payload[i], payload[i + 1], payload[i + 2])
                       for i in range(0, len(payload), 3)]
        elif ctype == b"tRNS":
            trns_payload = payload
        elif ctype == b"IDAT":
            idats.append(payload)
    if color_type == 3 and palette is None:
        problems.append(_problem("ERROR", "调色板 PNG（color_type=3）缺少 PLTE"))
        return info
    # tRNS：全透明像素。以前完全不处理，导致「调色板 + tRNS 把所有索引设成 alpha=0」
    # 这种整帧隐形图能通过空白判定（AGENTS.md 承诺「全透明也算空白」）。
    transparency: Optional[Dict[str, Any]] = None
    if trns_payload is not None:
        if color_type == 3:
            if len(trns_payload) > len(palette or []):
                problems.append(_problem(
                    "ERROR", "tRNS 有 %d 个 alpha 值，比 PLTE 的 %d 个颜色还多"
                    % (len(trns_payload), len(palette or []))))
                return info
            transparency = {"palette": {i for i, a in enumerate(trns_payload) if a == 0}}
        elif color_type == 0:
            if len(trns_payload) != 2:
                problems.append(_problem("ERROR", "灰度 PNG 的 tRNS 长度应为 2，实际 %d"
                                         % (len(trns_payload),)))
                return info
            transparency = {"gray": _be16(trns_payload, 0)}
        elif color_type == 2:
            if len(trns_payload) != 6:
                problems.append(_problem("ERROR", "RGB PNG 的 tRNS 长度应为 6，实际 %d"
                                         % (len(trns_payload),)))
                return info
            transparency = {"rgb": (_be16(trns_payload, 0), _be16(trns_payload, 2),
                                    _be16(trns_payload, 4))}
        else:  # color_type 4 / 6 自带 alpha，规范不允许 tRNS
            problems.append(_problem(
                "ERROR", "color_type=%d 的 PNG 自带 alpha 通道，不应再有 tRNS chunk"
                % (color_type,)))
            return info
    if not idats:
        problems.append(_problem("ERROR", "PNG 没有 IDAT（没有像素数据）：文件被截断或只有文件头"))
        return info
    if chunks[-1][0] != b"IEND":
        problems.append(_problem("ERROR", "PNG chunk 链没有以 IEND 结束：文件被截断"))
        return info
    info["chunk_types"] = [ctype.decode("ascii", "replace") for ctype, _ in chunks]
    if width * height > MAX_DECODE_PIXELS:
        problems.append(_problem("WARN", "图太大（%dx%d），跳过像素级空白检测" % (width, height)))
        info["ok"] = True
        return info
    if interlace != 0:
        problems.append(_problem(
            "WARN", "Adam7 交错 PNG：只做了结构/尺寸校验，未解码像素做空白检测"
            "（抽帧产物一般不是交错 PNG）"))
        info["ok"] = True
        return info
    bpp = max(1, (_PNG_CHANNELS[color_type] * bit_depth + 7) // 8)
    expected_raw = height * (1 + width * bpp)
    try:
        engine = zlib.decompressobj()
        raw = engine.decompress(b"".join(idats), expected_raw + 1)
        if engine.unconsumed_tail:
            problems.append(_problem(
                "ERROR", "PNG 解压后的像素数据比 IHDR 声明的 %d 字节还多（IDAT 被追加了别的内容）"
                % (expected_raw,)))
            return info
        if not engine.eof:
            problems.append(_problem(
                "ERROR", "PNG 像素数据流不完整（zlib 流没有结束标记）：文件被截断"))
            return info
        raw += engine.flush()
        if engine.unused_data:
            problems.append(_problem(
                "ERROR", "PNG 像素数据流之后还有 %d 字节多余数据：文件被动过"
                % (len(engine.unused_data),)))
            return info
    except zlib.error as exc:
        problems.append(_problem("ERROR", "IDAT 解压失败（%s）：文件截断或损坏" % (exc,)))
        return info
    rows, unfilter_error = _unfilter(raw, width, height, bpp)
    if unfilter_error:
        problems.append(_problem("ERROR", unfilter_error))
        return info
    stats = _png_luma_stats(rows or [], width, height, color_type, bit_depth, palette,
                            transparency)
    if not stats.get("decoded"):
        problems.append(_problem("ERROR", "PNG 像素统计失败：%s" % (stats.get("error"),)))
        return info
    info.update({
        "decoded": True,
        "variance": stats["variance"],
        "distinct_colors": stats["distinct_colors"],
        "samples": stats["samples"],
        "all_transparent": stats["all_transparent"],
    })
    info["ok"] = True
    return info


# ---------------------------------------------------------------------------
# JPEG：纯标准库结构解析（不解像素）
# ---------------------------------------------------------------------------
def read_jpeg(path: Path, data: Optional[bytes] = None) -> Dict[str, Any]:
    """校验 JPEG 结构（SOI / SOF / SOS / EOI）并读出尺寸；不解像素。"""
    info: Dict[str, Any] = {
        "format": "jpeg", "ok": False, "width": None, "height": None,
        "components": None, "entropy_bytes": None, "bytes_per_pixel": None,
        "problems": [],
    }
    problems: List[Dict[str, str]] = info["problems"]
    if data is None:
        try:
            data = path.read_bytes()
        except OSError as exc:
            problems.append(_problem("ERROR", "读不了文件：%s" % (exc,)))
            return info
    if not data.startswith(b"\xff\xd8"):
        problems.append(_problem("ERROR", "JPEG 签名不对（文件头不是 FFD8）"))
        return info
    pos = 2
    width = height = components = None
    found_sof = False
    found_sos = False
    sos_end = None
    while pos < len(data):
        if data[pos] != 0xFF:
            problems.append(_problem("ERROR", "JPEG 结构异常：offset %d 处不是标记字节 0xFF" % (pos,)))
            return info
        while pos < len(data) and data[pos] == 0xFF:
            pos += 1
        if pos >= len(data):
            break
        marker = data[pos]
        pos += 1
        if marker == 0xD9:  # EOI
            break
        if marker == 0xD8 or 0xD0 <= marker <= 0xD7:  # 无长度字段
            continue
        if pos + 2 > len(data):
            problems.append(_problem("ERROR", "JPEG 截断：标记 FF%02X 后没有长度字段" % (marker,)))
            return info
        segment_length = _be16(data, pos)
        if segment_length < 2:
            problems.append(_problem("ERROR", "JPEG 段长度非法（%d）" % (segment_length,)))
            return info
        if pos + segment_length > len(data):
            problems.append(_problem(
                "ERROR", "JPEG 截断：标记 FF%02X 段声明 %d 字节，超出文件末尾"
                % (marker, segment_length)))
            return info
        if marker in _SOF_MARKERS:
            if segment_length < 8:
                problems.append(_problem("ERROR", "SOF 段太短（%d 字节）" % (segment_length,)))
                return info
            height = _be16(data, pos + 3)
            width = _be16(data, pos + 5)
            components = data[pos + 7]
            found_sof = True
        if marker == 0xDA:  # SOS，之后是熵编码数据
            found_sos = True
            sos_end = pos + segment_length
            break
        pos += segment_length
    if not found_sof:
        problems.append(_problem("ERROR", "JPEG 里找不到 SOF 段（没有图像尺寸信息）"))
        return info
    if not found_sos or sos_end is None:
        problems.append(_problem("ERROR", "JPEG 里找不到 SOS 段（没有扫描数据）"))
        return info
    eoi = data.rfind(b"\xff\xd9")
    if eoi < sos_end:
        problems.append(_problem("ERROR", "JPEG 缺少 EOI 结束标记：文件被截断"))
        return info
    entropy_bytes = eoi - sos_end
    if entropy_bytes < 8:
        problems.append(_problem("ERROR", "JPEG 扫描数据只有 %d 字节，基本可以确定文件被截断"
                                 % (entropy_bytes,)))
        return info
    if not width or not height:
        problems.append(_problem("ERROR", "JPEG 尺寸非法：%dx%d" % (width or 0, height or 0)))
        return info
    pixels = width * height
    info.update({
        "width": width, "height": height, "components": components,
        "entropy_bytes": entropy_bytes,
        "bytes_per_pixel": round(entropy_bytes / float(pixels), 6) if pixels else None,
        "ok": True,
    })
    return info


# ---------------------------------------------------------------------------
# 单帧分析
# ---------------------------------------------------------------------------
def analyze_frame(path: Path, expect: Optional[Tuple[int, int]] = None,
                  min_variance: float = DEFAULT_MIN_VARIANCE,
                  jpeg_suspect_bpp: float = DEFAULT_JPEG_SUSPECT_BPP) -> Dict[str, Any]:
    """分析一帧：格式 / 尺寸 / 可解码性 / 空白判定 / 时间戳。"""
    frame: Dict[str, Any] = {
        "name": path.name,
        "time": parse_stamp(path.name),
        "format": "png" if path.suffix.lower() == ".png" else "jpeg",
        "bytes": None,
        "width": None,
        "height": None,
        "variance": None,
        "blank": False,
        "problems": [],
    }
    problems: List[Dict[str, str]] = frame["problems"]
    try:
        frame["bytes"] = path.stat().st_size
    except OSError as exc:
        problems.append(_problem("ERROR", "读不了文件：%s" % (exc,)))
        return frame
    if frame["bytes"] == 0:
        problems.append(_problem("ERROR", "文件是 0 字节"))
        return frame
    try:
        data = path.read_bytes()
    except OSError as exc:
        problems.append(_problem("ERROR", "读不了文件：%s" % (exc,)))
        return frame
    if frame["format"] == "png":
        info = read_png(path, data)
        frame.update({"width": info.get("width"), "height": info.get("height"),
                      "variance": info.get("variance"),
                      "distinct_colors": info.get("distinct_colors"),
                      "decoded": info.get("decoded", False)})
        problems.extend(info.get("problems") or [])
        if info.get("ok") and info.get("decoded"):
            if info.get("all_transparent"):
                frame["blank"] = True
                problems.append(_problem("ERROR", "整帧全透明（alpha 全 0）：没有可见画面"))
            elif info.get("variance") is not None and float(info["variance"]) < min_variance:
                frame["blank"] = True
                problems.append(_problem(
                    "ERROR",
                    "空白帧：像素亮度方差 %.1f < 阈值 %g（全黑/全白/纯色画面里没有任何可看内容）"
                    % (float(info["variance"]), min_variance)))
    else:
        info = read_jpeg(path, data)
        frame.update({"width": info.get("width"), "height": info.get("height"),
                      "bytes_per_pixel": info.get("bytes_per_pixel"),
                      "decoded": False})
        problems.extend(info.get("problems") or [])
        bpp = info.get("bytes_per_pixel")
        if info.get("ok") and bpp is not None and float(bpp) < jpeg_suspect_bpp:
            problems.append(_problem(
                "WARN",
                "JPEG 体积只有 %.4f 字节/像素（< %g），疑似空白/纯色帧；"
                "纯标准库不解 JPEG 像素，无法给出像素级结论 —— 要机器判定空白请用 "
                "`npm run stills -- --scenes --format png` 出 PNG 静帧"
                % (float(bpp), jpeg_suspect_bpp)))
    if expect is not None and frame.get("width") and frame.get("height"):
        if (int(frame["width"]), int(frame["height"])) != (int(expect[0]), int(expect[1])):
            problems.append(_problem(
                "ERROR", "像素尺寸 %dx%d 与期望 %dx%d 不一致（stills.mjs 的 --scale 要与 "
                "--scale / --expect 对齐）"
                % (int(frame["width"]), int(frame["height"]), int(expect[0]), int(expect[1]))))
    return frame


# ---------------------------------------------------------------------------
# 场景覆盖
# ---------------------------------------------------------------------------
def timing_scenes(timing: Dict[str, Any]) -> Tuple[Optional[List[Dict[str, Any]]], Optional[str]]:
    """从 timing dict 里取场景窗口；结构不合法返回 (None, 错误文案)。"""
    if not isinstance(timing, dict):
        return None, "timing.json 顶层必须是对象"
    scenes = timing.get("scenes")
    if not isinstance(scenes, list) or not scenes:
        return None, "timing.json 的 scenes 必须是非空数组"
    parsed: List[Dict[str, Any]] = []
    for index, scene in enumerate(scenes):
        if not isinstance(scene, dict):
            return None, "scenes[%d] 必须是对象，现在是 %s" % (index, type(scene).__name__)
        name = scene.get("name")
        if not isinstance(name, str) or not name.strip():
            return None, "scenes[%d].name 必须是非空字符串" % (index,)
        for field in ("start", "end"):
            if not _finite(scene.get(field)):
                return None, "scenes[%d].%s 必须是有限数字（现在是 %r）" % (
                    index, field, scene.get(field))
        start = float(scene["start"])
        end = float(scene["end"])
        if end <= start:
            return None, "scenes[%d] 的 end=%g 必须大于 start=%g" % (index, end, start)
        parsed.append({"name": name.strip(), "start": start, "end": end})
    return parsed, None


def _scene_of(scenes: Sequence[Dict[str, Any]], time_s: float,
              duration: Optional[float]) -> Optional[Dict[str, Any]]:
    last = scenes[-1]
    for scene in scenes:
        if scene is last:
            if scene["start"] <= time_s <= scene["end"]:
                return scene
            continue
        if scene["start"] <= time_s < scene["end"]:
            return scene
    if duration is not None and time_s > duration:
        return None
    return None


# ---------------------------------------------------------------------------
# 目录级检查
# ---------------------------------------------------------------------------
def _finalize(result: Dict[str, Any]) -> Dict[str, Any]:
    """补齐 stats 统计口径（早退分支也要有 errors/warnings，CLI 才不会打「0 ERROR」）。"""
    problems = result.get("problems") or []
    stats = result.setdefault("stats", {})
    for key in ("frame_count", "png", "jpeg", "timestamped", "decoded", "blank_frames"):
        stats.setdefault(key, 0)
    stats.setdefault("coverage", None)
    stats.setdefault("covered_scenes", None)
    stats.setdefault("scene_count", None)
    stats.setdefault("min_frame_variance", None)
    stats["errors"] = sum(1 for item in problems if item.get("level") == "ERROR")
    stats["warnings"] = sum(1 for item in problems if item.get("level") == "WARN")
    result["ok"] = stats["errors"] == 0
    return result


def check_frames(
    stills_dir: Path,
    timing: Optional[Dict[str, Any]] = None,
    timing_path: Optional[Path] = None,
    project_root: Optional[Path] = None,
    expect: Optional[Tuple[int, int]] = None,
    scale: float = 1.0,
    per_scene: int = DEFAULT_PER_SCENE,
    coverage_min: float = DEFAULT_COVERAGE,
    min_variance: float = DEFAULT_MIN_VARIANCE,
    jpeg_suspect_bpp: float = DEFAULT_JPEG_SUSPECT_BPP,
) -> Dict[str, Any]:
    """校验一个静帧目录，返回 {ok, frames, problems, scenes, stats, ...}。"""
    project_root = Path(project_root) if project_root is not None else Path(PROJECT_ROOT)
    stills_dir = Path(stills_dir)
    result: Dict[str, Any] = {
        "ok": False,
        "stills": str(stills_dir),
        "timing": str(timing_path) if timing_path is not None else None,
        "expect": ("%dx%d" % expect) if expect is not None else None,
        "scale": scale,
        "per_scene": per_scene,
        "coverage_min": coverage_min,
        "min_variance": min_variance,
        "frames": [],
        "ignored": [],
        "problems": [],
        "scenes": [],
        "stats": {},
    }
    problems: List[Dict[str, str]] = result["problems"]

    if not stills_dir.is_dir():
        problems.append(_problem(
            "ERROR", "找不到静帧目录 %s：先跑 `npm run stills -- --scenes` 抽帧，再复跑本闸门"
            % (stills_dir,)))
        return _finalize(result)

    entries = sorted(stills_dir.iterdir(), key=lambda p: p.name)
    frames_paths: List[Path] = []
    for entry in entries:
        if not entry.is_file():
            continue
        if entry.suffix.lower() in IMAGE_SUFFIXES:
            frames_paths.append(entry)
        else:
            try:
                result["ignored"].append(str(entry.relative_to(stills_dir)))
            except ValueError:
                result["ignored"].append(entry.name)
    if not frames_paths:
        problems.append(_problem(
            "ERROR", "静帧目录 %s 里没有 PNG/JPEG 文件（只有 %d 个其它文件）"
            % (stills_dir, len(result["ignored"]))))
        return _finalize(result)

    # 期望尺寸：显式 --expect 优先；否则从 timing 的 width/height 与 --scale 推。
    # 舍入用「四舍五入一半向上」而不是 Python 的 round()（银行家舍入）：stills.mjs 走
    # JS Math.round，奇数宽度 × 0.5 时 Python round(500.5)=500 会与 JS 的 501 差 1 像素，
    # 真实抽帧反而被误判错尺寸。
    if expect is None and timing is not None:
        width, height = timing.get("width"), timing.get("height")
        if _finite(width) and _finite(height) and float(width) > 0 and float(height) > 0:
            expect = (int(math.floor(float(width) * scale + 0.5)),
                      int(math.floor(float(height) * scale + 0.5)))
            result["expect"] = "%dx%d" % expect
        elif width is not None or height is not None:
            # R9-V-TIMING：width/height 写错类型（"1080"、NaN）时以前静默跳过尺寸核对，
            # 等于让「静帧尺寸对不对」这条检查消失；闸门应报 ERROR 而不是装没看见。
            problems.append(_problem(
                "ERROR", "timing.json 的 width/height 必须是正的有限数字，现在是 %r / %r："
                "无法推算静帧期望尺寸（可用 --expect 显式指定）" % (width, height)))

    scenes: Optional[List[Dict[str, Any]]] = None
    duration: Optional[float] = None
    if timing is not None:
        scenes, scene_error = timing_scenes(timing)
        if scene_error:
            problems.append(_problem("ERROR", scene_error))
            scenes = None
        if _finite(timing.get("duration")) and float(timing["duration"]) > 0:
            duration = float(timing["duration"])

    frames: List[Dict[str, Any]] = []
    for path in frames_paths:
        frame = analyze_frame(path, expect=expect, min_variance=min_variance,
                              jpeg_suspect_bpp=jpeg_suspect_bpp)
        frames.append(frame)
        for item in frame["problems"]:
            problems.append(_problem(item["level"], "%s：%s" % (frame["name"], item["message"])))
    frames.sort(key=lambda f: (f["time"] is None, f["time"] if f["time"] is not None else 0.0,
                               f["name"]))
    result["frames"] = frames

    timestamped = [f for f in frames if f["time"] is not None]
    untimestamped = [f for f in frames if f["time"] is None]
    if untimestamped:
        level = "ERROR" if timing is not None else "WARN"
        problems.append(_problem(
            level,
            "%d 帧的文件名不是 stills.mjs 的时间戳格式（应为 0004.20.jpg 这种 %%06.2f），"
            "无法核对时间戳/场景覆盖：%s"
            % (len(untimestamped), "、".join(f["name"] for f in untimestamped[:5]))))

    scene_rows: List[Dict[str, Any]] = []
    if scenes:
        for scene in scenes:
            inside = [f for f in timestamped
                      if _scene_of(scenes, float(f["time"]), duration) is scene]
            row = {
                "name": scene["name"],
                "start": scene["start"],
                "end": scene["end"],
                "frames": len(inside),
                "expected": per_scene,
                "frame_names": [f["name"] for f in inside],
            }
            if len(inside) < per_scene:
                row["level"] = "ERROR"
                problems.append(_problem(
                    "ERROR", "场景 %s（%.2fs~%.2fs）只有 %d 帧，要求每场景至少 %d 帧"
                    "（stills.mjs -- --scenes 默认每场景 2 帧 40%%/80%%）"
                    % (scene["name"], scene["start"], scene["end"], len(inside), per_scene)))
            else:
                row["level"] = "OK"
            scene_rows.append(row)
        outside = [f for f in timestamped if _scene_of(scenes, float(f["time"]), duration) is None]
        if outside:
            problems.append(_problem(
                "WARN",
                "%d 帧的时间戳不在任何场景窗口内（可能抽错了时间点，或 timing.json 的 scenes 与"
                "抽帧时不一致）：%s"
                % (len(outside), "、".join("%s@%.2fs" % (f["name"], float(f["time"]))
                                          for f in outside[:5]))))
        covered = sum(1 for row in scene_rows if row["frames"] > 0)
        coverage = covered / float(len(scene_rows)) if scene_rows else 0.0
        if coverage < coverage_min - 1e-9:
            problems.append(_problem(
                "ERROR", "场景覆盖率 %.0f%% < 要求 %.0f%%（%d/%d 个场景抽到了帧）"
                % (coverage * 100.0, coverage_min * 100.0, covered, len(scene_rows))))
        result["scenes"] = scene_rows
        result["stats"]["coverage"] = round(coverage, 6)
        result["stats"]["covered_scenes"] = covered
        result["stats"]["scene_count"] = len(scene_rows)
    decoded_variances = [float(f["variance"]) for f in frames
                         if f.get("variance") is not None]
    result["stats"].update({
        "frame_count": len(frames),
        "png": sum(1 for f in frames if f["format"] == "png"),
        "jpeg": sum(1 for f in frames if f["format"] == "jpeg"),
        "timestamped": len(timestamped),
        "decoded": sum(1 for f in frames if f.get("decoded")),
        "blank_frames": sum(1 for f in frames if f.get("blank")),
        "min_variance": min_variance,
        # 实际观测到的最小帧方差（None = 一帧都没解码出来，比如纯 JPEG）。
        # 之前 deliver/publish_check 把「空白阈值 min_variance」错当「最小方差」展示，
        # 打印成 6.0 让人误以为画面方差只有 6.0；统计值放这里，阈值仍叫 min_variance。
        "min_frame_variance": (round(min(decoded_variances), 1)
                               if decoded_variances else None),
    })
    result["stats"].setdefault("coverage", None)
    result["stats"].setdefault("covered_scenes", None)
    result["stats"].setdefault("scene_count", None)
    return _finalize(result)


# ---------------------------------------------------------------------------
# 输出
# ---------------------------------------------------------------------------
def _frame_row(frame: Dict[str, Any]) -> List[str]:
    time_text = "%.2fs" % frame["time"] if frame["time"] is not None else "?"
    size_text = ("%dx%d" % (frame["width"], frame["height"])
                 if frame["width"] and frame["height"] else "?")
    variance = frame.get("variance")
    if variance is None:
        blank_text = "未解码"
    elif frame.get("blank"):
        blank_text = "空白(按像素方差)"
    else:
        blank_text = "方差 %.1f" % (float(variance),)
    verdict = "OK"
    if any(item["level"] == "ERROR" for item in frame["problems"]):
        verdict = "ERROR"
    elif frame["problems"]:
        verdict = "WARN"
    return [frame["name"], time_text, size_text, frame["format"].upper(), blank_text, verdict]


def format_text(result: Dict[str, Any]) -> str:
    """人读表：帧表 + 场景覆盖 + 问题清单 + 汇总。"""
    lines: List[str] = []
    stats = result.get("stats") or {}
    lines.append("静帧校验：%s（%d 帧：PNG %d / JPEG %d）"
                 % (result.get("stills"), stats.get("frame_count", 0),
                    stats.get("png", 0), stats.get("jpeg", 0)))
    if result.get("expect"):
        lines.append("期望尺寸：%s（--scale %g）· 每场景至少 %d 帧 · 空白阈值：亮度方差 < %g"
                     % (result["expect"], result.get("scale", 1.0),
                        result.get("per_scene", 1), result.get("min_variance", DEFAULT_MIN_VARIANCE)))
    else:
        lines.append("期望尺寸：未指定（只校验图片本身，未核对 timing.json 分辨率）")
    if result.get("frames"):
        widths = [4, 8, 11, 6, 18, 5]
        headers = ["序号", "文件", "时间戳", "尺寸", "格式/空白", "判定"]
        lines.append("")
        lines.append("  ".join(_pad(headers[i], widths[i]) for i in range(len(headers))))
        lines.append("  ".join("-" * widths[i] for i in range(len(headers))))
        for index, frame in enumerate(result["frames"], start=1):
            row = _frame_row(frame)
            lines.append("  ".join(_pad(row[i], widths[i]) for i in range(len(row))))
    if result.get("scenes"):
        lines.append("")
        lines.append("场景覆盖：")
        for row in result["scenes"]:
            lines.append("  %s %s（%.2fs~%.2fs）%d/%d 帧 %s"
                         % (_pad(row["name"], 10), "OK" if row["level"] == "OK" else "不足",
                            row["start"], row["end"], row["frames"], row["expected"],
                            "、".join(row["frame_names"])))
    if result.get("ignored"):
        lines.append("")
        lines.append("忽略非图片文件：%s" % ("、".join(result["ignored"][:10]),))
    problems = result.get("problems") or []
    if problems:
        lines.append("")
        lines.append("问题：")
        for item in problems:
            if item["level"] == "ERROR":
                lines.append("  ERROR: %s" % (item["message"],))
            else:
                lines.append("  WARN: %s" % (item["message"],))
    errors = stats.get("errors", 0)
    warnings = stats.get("warnings", 0)
    lines.append("")
    lines.append("汇总：静帧校验 %d ERROR / %d WARN → %s"
                 % (errors, warnings, "通过" if result.get("ok") else "未通过"))
    return "\n".join(lines)


def _pad(text: str, width: int) -> str:
    """按显示宽度补空格（中文占 2 格）。"""
    text = str(text)
    shown = 0
    for char in text:
        shown += 2 if _is_wide(char) else 1
    return text + " " * max(0, width - shown)


def _is_wide(char: str) -> bool:
    import unicodedata
    return unicodedata.east_asian_width(char) in ("W", "F")


# ---------------------------------------------------------------------------
# 命令行
# ---------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="frame_check.py",
        description="静帧机器校验：帧数覆盖 timing 场景 / PNG·JPEG 可解码 / 尺寸 / 非空白 / 时间戳",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例：\n"
            "  python scripts/frame_check.py\n"
            "  python scripts/frame_check.py out/stills --scale 0.5\n"
            "  python scripts/frame_check.py --per-scene 2 --json\n"
            "  python scripts/frame_check.py --no-timing --expect 1080x1920\n"
        ),
    )
    parser.add_argument("stills", nargs="?", default=None,
                        help="静帧目录，默认 out/stills")
    parser.add_argument("--project", default=None, help="项目根目录，默认当前项目（PROMO_ROOT / scripts 上级）")
    parser.add_argument("--timing", default=None,
                        help="timing.json 路径，默认 <项目根>/src/timing.json")
    parser.add_argument("--no-timing", action="store_true",
                        help="不读 timing.json：只校验图片本身，不核对场景覆盖与分辨率")
    parser.add_argument("--expect", default=None, metavar="WxH",
                        help="显式期望像素尺寸（默认从 timing.json × --scale 推）")
    parser.add_argument("--scale", type=float, default=1.0,
                        help="stills.mjs --scale 的同一个值，默认 1.0（stills.mjs 默认 0.5）")
    parser.add_argument("--per-scene", type=int, default=DEFAULT_PER_SCENE,
                        help="每个场景至少几帧，默认 1（stills.mjs --scenes 产 2 帧/场景）")
    parser.add_argument("--coverage", type=float, default=DEFAULT_COVERAGE,
                        help="场景覆盖率下限 0~1，默认 1.0（每个场景都要有帧）")
    parser.add_argument("--min-variance", type=float, default=DEFAULT_MIN_VARIANCE,
                        help="亮度方差空白阈值，默认 %g" % (DEFAULT_MIN_VARIANCE,))
    parser.add_argument("--jpeg-suspect-bpp", type=float, default=DEFAULT_JPEG_SUSPECT_BPP,
                        help="JPEG 疑似空白阈值（字节/像素），默认 %g" % (DEFAULT_JPEG_SUSPECT_BPP,))
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


def _run(args: argparse.Namespace) -> Dict[str, Any]:
    root = Path(args.project) if args.project else Path(PROJECT_ROOT)
    if not root.is_dir():
        raise ValueError("项目根目录不存在：%s" % (root,))
    stills_dir = _resolve(root, args.stills, DEFAULT_STILLS)
    timing: Optional[Dict[str, Any]] = None
    timing_path: Optional[Path] = None
    if not args.no_timing:
        timing_path = _resolve(root, args.timing, DEFAULT_TIMING)
        timing = _read_timing(timing_path)
    expect: Optional[Tuple[int, int]] = None
    if args.expect:
        expect = parse_size(args.expect)
    if not math.isfinite(args.scale) or args.scale <= 0:
        raise ValueError("--scale 必须是正数，现在是 %r" % (args.scale,))
    if args.per_scene < 1:
        raise ValueError("--per-scene 必须 ≥ 1，现在是 %r" % (args.per_scene,))
    if not (0.0 <= args.coverage <= 1.0):
        raise ValueError("--coverage 必须在 0~1 之间，现在是 %r" % (args.coverage,))
    if not math.isfinite(args.min_variance) or args.min_variance < 0:
        raise ValueError("--min-variance 必须是非负有限数，现在是 %r" % (args.min_variance,))
    if not math.isfinite(args.jpeg_suspect_bpp) or args.jpeg_suspect_bpp < 0:
        raise ValueError("--jpeg-suspect-bpp 必须是非负有限数，现在是 %r" % (args.jpeg_suspect_bpp,))
    return check_frames(
        stills_dir,
        timing=timing,
        timing_path=timing_path,
        project_root=root,
        expect=expect,
        scale=args.scale,
        per_scene=args.per_scene,
        coverage_min=args.coverage,
        min_variance=args.min_variance,
        jpeg_suspect_bpp=args.jpeg_suspect_bpp,
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
            errors = (result.get("stats") or {}).get("errors", 0)
            warns = (result.get("stats") or {}).get("warnings", 0)
            sys.stderr.write("ERROR: 静帧校验未通过：%d ERROR / %d WARN\n" % (errors, warns))
    return 0 if result["ok"] else 1


# ---------------------------------------------------------------------------
# 自测（--selftest）：不读项目素材，纯 Python 造夹具
# ---------------------------------------------------------------------------
def _png_bytes(width: int, height: int, rows: List[bytes], color_type: int = 6) -> bytes:
    """把已经带过滤字节的扫描行拼成完整 PNG（8 bit）。"""
    def chunk(ctype: bytes, payload: bytes) -> bytes:
        return (struct.pack(">I", len(payload)) + ctype + payload
                + struct.pack(">I", zlib.crc32(ctype + payload) & 0xFFFFFFFF))

    ihdr = struct.pack(">IIBBBBB", width, height, 8, color_type, 0, 0, 0)
    raw = b"".join(rows)
    return (PNG_SIGNATURE + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(raw))
            + chunk(b"IEND", b""))


def _solid_rgba(width: int, height: int, rgba: Tuple[int, int, int, int]) -> List[bytes]:
    row = b"\x00" + bytes(rgba) * width
    return [row for _ in range(height)]


def _gradient_rgba(width: int, height: int) -> List[bytes]:
    rows = []
    for y in range(height):
        pixels = bytearray()
        for x in range(width):
            pixels.extend(((x * 255) // max(1, width - 1),
                           (y * 255) // max(1, height - 1),
                           (x * y) % 256, 255))
        rows.append(b"\x00" + bytes(pixels))
    return rows


def _jpeg_structure(width: int, height: int, entropy_len: int = 4000) -> bytes:
    app0 = b"JFIF\x00" + bytes([1, 1, 0]) + struct.pack(">HH", 1, 1) + b"\x00\x00"
    app0_seg = b"\xff\xe0" + struct.pack(">H", len(app0) + 2) + app0
    sof_payload = bytes([8]) + struct.pack(">HH", height, width) + bytes([1, 1, 0x11, 0])
    sof_seg = b"\xff\xc0" + struct.pack(">H", len(sof_payload) + 2) + sof_payload
    sos_payload = bytes([1, 1, 0x00, 0, 63, 0])
    sos_seg = b"\xff\xda" + struct.pack(">H", len(sos_payload) + 2) + sos_payload
    return b"\xff\xd8" + app0_seg + sof_seg + sos_seg + b"\x11" * entropy_len + b"\xff\xd9"


def _assert(condition: Any, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def _write(path: Path, data: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(str(path), "wb") as handle:
        handle.write(data)
    return path


def _levels(result: Dict[str, Any], level: str) -> List[str]:
    return [item["message"] for item in result["problems"] if item["level"] == level]


def _selftest() -> int:
    import tempfile

    checks = 0

    def ok(condition: Any, message: str) -> None:
        nonlocal checks
        _assert(condition, message)
        checks += 1

    try:
        # 1) 文件名时间戳解析
        ok(parse_stamp("0004.20.jpg") == 4.2, "0004.20.jpg 应解析成 4.2s")
        ok(parse_stamp("0012.50.png") == 12.5, "0012.50.png 应解析成 12.5s")
        ok(parse_stamp("0004.2.jpg") is None, "0004.2.jpg（只有一位小数）不应解析成时间戳")
        ok(parse_stamp("sheet.jpg") is None, "sheet.jpg 不应解析成时间戳")
        # 2) 尺寸解析
        ok(parse_size("1080x1920") == (1080, 1920), "1080x1920 应解析成 (1080,1920)")
        ok(parse_size(" 540 X 960 ") == (540, 960), "允许空格与大写 X")
        for bad in ("1080", "1080*1920", "0x1920", "axb"):
            try:
                parse_size(bad)
            except ValueError:
                pass
            else:
                raise AssertionError("parse_size(%r) 应抛 ValueError" % (bad,))
        checks += 1

        with tempfile.TemporaryDirectory(prefix="frame-check-selftest-") as tmp:
            tmp_path = Path(tmp)

            # 3) PNG 解码：非空白 / 空白 / 截断 / CRC 坏 / 垃圾
            good_png = _write(tmp_path / "good.png",
                              _png_bytes(40, 60, _gradient_rgba(40, 60)))
            info = read_png(good_png)
            ok(info["ok"] and info["decoded"], "正常 PNG 应通过结构校验并解码")
            ok(info["width"] == 40 and info["height"] == 60, "正常 PNG 尺寸应为 40x60")
            ok(float(info["variance"]) > DEFAULT_MIN_VARIANCE,
               "渐变 PNG 的亮度方差应大于空白阈值，实际 %r" % (info["variance"],))

            blank_png = _write(tmp_path / "blank.png",
                               _png_bytes(40, 60, _solid_rgba(40, 60, (255, 255, 255, 255))))
            blank_info = read_png(blank_png)
            ok(blank_info["ok"] and blank_info["decoded"], "纯白 PNG 结构应合法")
            ok(float(blank_info["variance"]) < 1e-9, "纯白 PNG 的亮度方差应为 0")

            transparent_png = _write(
                tmp_path / "transparent.png",
                _png_bytes(40, 60, _solid_rgba(40, 60, (0, 0, 0, 0))))
            transparent_info = read_png(transparent_png)
            ok(transparent_info["all_transparent"], "全透明 PNG 应标记 all_transparent")

            # R9-V-tRNS：调色板 PNG 用 tRNS 把索引设成 alpha=0，整帧不可见。以前完全不
            # 处理 tRNS，这种隐形帧的亮度方差不为 0，能骗过空白闸门。
            def _chunk(ctype: bytes, payload: bytes) -> bytes:
                return (struct.pack(">I", len(payload)) + ctype + payload
                        + struct.pack(">I", zlib.crc32(ctype + payload) & 0xFFFFFFFF))

            def _palette_png(trns: bytes, index: int = 0, checker: bool = False) -> bytes:
                ihdr3 = struct.pack(">IIBBBBB", 20, 10, 8, 3, 0, 0, 0)
                rows = []
                for _y in range(10):
                    if checker:
                        rows.append(b"\x00" + bytes(x % 2 for x in range(20)))
                    else:
                        rows.append(b"\x00" + bytes([index] * 20))
                return (PNG_SIGNATURE + _chunk(b"IHDR", ihdr3)
                        + _chunk(b"PLTE", bytes([255, 0, 0, 0, 255, 0]))
                        + _chunk(b"tRNS", trns)
                        + _chunk(b"IDAT", zlib.compress(b"".join(rows)))
                        + _chunk(b"IEND", b""))

            palette_trns_path = _write(tmp_path / "palette_trns.png",
                                      _palette_png(bytes([0, 0])))
            palette_trns_info = read_png(palette_trns_path)
            ok(palette_trns_info["ok"] and palette_trns_info["decoded"], "调色板 + tRNS PNG 应能解码")
            ok(palette_trns_info["all_transparent"],
               "调色板 + tRNS 把所有索引设成 alpha=0：整帧应标记 all_transparent")
            palette_trns_frame = analyze_frame(palette_trns_path, expect=(20, 10))
            ok(palette_trns_frame["blank"], "调色板 + tRNS 隐形帧应被判为空白帧")
            palette_opaque_path = _write(tmp_path / "palette_opaque.png",
                                         _palette_png(bytes([255, 255]), checker=True))
            palette_opaque_info = read_png(palette_opaque_path)
            palette_opaque_frame = analyze_frame(palette_opaque_path, expect=(20, 10))
            ok(not palette_opaque_info["all_transparent"],
               "调色板 + tRNS 全不透明（alpha=255）不应标记 all_transparent")
            ok(not palette_opaque_frame["blank"],
               "调色板 + tRNS 全不透明且画面有对比时不应被误判空白")

            # R9-V-chunk：未知 critical chunk / IEND 后多余数据 / 重复 IHDR /
            # IDAT 里 zlib 流后追加垃圾，都应报 ERROR（与 publish_check 口径一致）。
            ihdr_normal = struct.pack(">IIBBBBB", 4, 4, 8, 6, 0, 0, 0)
            idat_normal = zlib.compress(b"".join(_solid_rgba(4, 4, (10, 20, 30, 255))))

            def _raw_png(middle: bytes, idat_payload: bytes = idat_normal,
                         trailing: bytes = b"") -> bytes:
                return (PNG_SIGNATURE + _chunk(b"IHDR", ihdr_normal) + middle
                        + _chunk(b"IDAT", idat_payload) + _chunk(b"IEND", b"")
                        + trailing)

            cases = [
                ("未知 critical chunk", _raw_png(_chunk(b"ABCD", b"x")), "critical"),
                ("IEND 后多余字节", _raw_png(b"", trailing=b"junk"), "多余数据"),
                ("重复 IHDR", _raw_png(_chunk(b"IHDR", ihdr_normal)), "IHDR"),
                ("IDAT 里 zlib 流后垃圾", _raw_png(b"", idat_normal + b"trailing"), "多余数据"),
            ]
            for label, data, keyword in cases:
                bad_info = read_png(_write(tmp_path / ("bad_" + str(len(label)) + ".png"), data))
                ok(not bad_info["ok"] and any(
                    keyword in msg for msg in _levels({"problems": bad_info["problems"]}, "ERROR")),
                   "%s 应被判坏 PNG（含「%s」）" % (label, keyword))
            ok(read_png(_write(tmp_path / "raw_ok.png", _raw_png(b"")))["ok"],
               "同样构造但结构合法的 PNG 应通过（证明上面几条不是误伤）")

            raw_good = _png_bytes(40, 60, _gradient_rgba(40, 60))
            truncated = _write(tmp_path / "truncated.png", raw_good[:40])
            truncated_info = read_png(truncated)
            ok(not truncated_info["ok"], "截断的 PNG 不应通过结构校验")
            ok(any("截断" in msg or "CRC" in msg for msg in _levels(
                {"problems": truncated_info["problems"]}, "ERROR")),
               "截断 PNG 的错误文案应说明截断/CRC")

            crc_bad = bytearray(raw_good)
            crc_bad[29] ^= 0xFF  # IHDR 的 CRC 字段
            crc_info = read_png(_write(tmp_path / "badcrc.png", bytes(crc_bad)))
            ok(not crc_info["ok"], "IHDR CRC 被改坏的 PNG 不应通过")

            garbage_info = read_png(_write(
                tmp_path / "garbage.png", b"not a png at all, definitely not the right magic bytes"))
            ok(not garbage_info["ok"] and any("签名" in msg for msg in _levels(
                {"problems": garbage_info["problems"]}, "ERROR")),
               "非 PNG 文件应报签名错误")

            # 4) JPEG 结构
            jpeg_good = read_jpeg(_write(tmp_path / "good.jpg",
                                         _jpeg_structure(40, 60, entropy_len=4000)))
            ok(jpeg_good["ok"], "结构完整的 JPEG 应通过")
            ok(jpeg_good["width"] == 40 and jpeg_good["height"] == 60, "JPEG 尺寸应为 40x60")
            ok(float(jpeg_good["bytes_per_pixel"]) > DEFAULT_JPEG_SUSPECT_BPP,
               "4000 字节熵数据的 JPEG 不应触发疑似空白")

            jpeg_no_eoi = _jpeg_structure(40, 60, entropy_len=4000)[:-2]
            jpeg_no_eoi_info = read_jpeg(_write(tmp_path / "noeoi.jpg", jpeg_no_eoi))
            ok(not jpeg_no_eoi_info["ok"], "缺 EOI 的 JPEG 不应通过")
            ok(any("EOI" in msg for msg in _levels(
                {"problems": jpeg_no_eoi_info["problems"]}, "ERROR")),
               "缺 EOI 的错误文案应提到 EOI")

            jpeg_tiny = read_jpeg(_write(tmp_path / "tiny.jpg",
                                         _jpeg_structure(40, 60, entropy_len=40)))
            ok(jpeg_tiny["ok"], "体积很小的 JPEG 结构仍应合法（空白判定只给 WARN）")

            fake_jpeg = read_jpeg(_write(tmp_path / "fake.jpg", b"\x89PNG\r\n\x1a\n" + b"\x00" * 40))
            ok(not fake_jpeg["ok"], "扩展名是 .jpg 但内容不是 JPEG 的文件不应通过")

            # 5) 单帧分析：空白 / 尺寸不符 / 无效时间戳
            frame_blank = analyze_frame(blank_png, expect=(40, 60))
            ok(frame_blank["blank"], "纯白 PNG 应被判为空白帧")
            ok(any("空白" in item["message"] for item in frame_blank["problems"]),
               "空白帧应给出「空白」原因")
            frame_good = analyze_frame(good_png, expect=(40, 60))
            ok(not frame_good["blank"] and not any(
                item["level"] == "ERROR" for item in frame_good["problems"]),
               "渐变 PNG 在尺寸匹配时不应有 ERROR")
            frame_wrong = analyze_frame(good_png, expect=(108, 192))
            ok(any("像素尺寸" in item["message"] for item in frame_wrong["problems"]),
               "尺寸不符应报「像素尺寸」ERROR")
            frame_nostamp = analyze_frame(good_png)
            ok(frame_nostamp["time"] is None, "good.png 没有时间戳")

            # 6) 目录级：好目录（2 场景 × 1 帧）应通过
            project = tmp_path / "proj"
            (project / "src").mkdir(parents=True)
            timing = {
                "slug": "selftest",
                "width": 40,
                "height": 60,
                "duration": 5.0,
                "scenes": [
                    {"name": "hook", "start": 0.0, "end": 2.5},
                    {"name": "cta", "start": 2.5, "end": 5.0},
                ],
            }
            _write(project / "src" / "timing.json",
                   json.dumps(timing, ensure_ascii=False).encode("utf-8"))
            stills = project / "out" / "stills"
            _write(stills / "0001.00.png", _png_bytes(40, 60, _gradient_rgba(40, 60)))
            _write(stills / "0003.50.png", _png_bytes(40, 60, _gradient_rgba(40, 60)))
            _write(stills / "notes.txt", b"ignore me")
            good = check_frames(stills, timing=timing,
                                timing_path=project / "src" / "timing.json",
                                project_root=project)
            ok(good["ok"], "两场景各一帧的好目录应 0 ERROR，实际：%r" % (
                _levels(good, "ERROR"),))
            ok(good["stats"]["frame_count"] == 2
               and good["stats"]["min_frame_variance"] is not None
               and good["stats"]["min_frame_variance"] >= DEFAULT_MIN_VARIANCE,
               "好目录应有 2 帧，且 stats.min_frame_variance 必须是实测值（>= 空白阈值），实际 %r"
               % (good["stats"].get("min_frame_variance"),))
            ok(good["stats"]["scene_count"] == 2 and good["stats"]["covered_scenes"] == 2,
               "好目录应覆盖 2/2 场景")
            ok(float(good["stats"]["coverage"]) == 1.0, "好目录场景覆盖率应为 1.0")
            ok("notes.txt" in good["ignored"], "非图片文件应进 ignored 列表")
            ok(good["expect"] == "40x60", "期望尺寸应从 timing.json 推出 40x60")
            payload = json.loads(json.dumps(good, ensure_ascii=False))
            ok(payload["ok"] is True and len(payload["frames"]) == 2
               and isinstance(payload["problems"], list),
               "--json 载荷应可序列化且含 ok/frames/problems")
            text = format_text(good)
            ok("汇总：静帧校验 0 ERROR / 0 WARN → 通过" in text,
               "人读输出应打印通过汇总，实际：%r" % (text[-80:],))

            # 7) 缺场景 = ERROR
            _write(stills / "0003.50.png", _png_bytes(40, 60, _gradient_rgba(40, 60)))
            (stills / "0003.50.png").unlink()
            missing = check_frames(stills, timing=timing, project_root=project)
            ok(not missing["ok"], "缺一个场景的帧应失败")
            ok(any("只有 0 帧" in msg for msg in _levels(missing, "ERROR")),
               "缺场景应报「只有 0 帧」")

            # 8) 空白帧、错尺寸、场景外时间戳
            _write(stills / "0003.50.png", _png_bytes(40, 60, _solid_rgba(40, 60, (0, 0, 0, 255))))
            blank_result = check_frames(stills, timing=timing, project_root=project)
            ok(not blank_result["ok"], "空白帧应让目录校验失败")
            ok(any("空白帧" in msg for msg in _levels(blank_result, "ERROR")),
               "空白帧应报「空白帧」")
            ok(blank_result["stats"]["blank_frames"] == 1
               and float(blank_result["stats"]["min_frame_variance"] or 0.0) < DEFAULT_MIN_VARIANCE,
               "应统计到 1 个空白帧，且 min_frame_variance 必须反映实测的空白帧方差")

            _write(stills / "0003.50.png", _png_bytes(20, 30, _gradient_rgba(20, 30)))
            wrong = check_frames(stills, timing=timing, project_root=project)
            ok(not wrong["ok"], "尺寸不符应让目录校验失败")
            ok(any("像素尺寸" in msg for msg in _levels(wrong, "ERROR")),
               "尺寸不符应报「像素尺寸」")

            _write(stills / "0003.50.png", _png_bytes(40, 60, _gradient_rgba(40, 60)))
            _write(stills / "0006.00.png", _png_bytes(40, 60, _gradient_rgba(40, 60)))
            outside = check_frames(stills, timing=timing, project_root=project)
            ok(any("不在任何场景" in msg for msg in _levels(outside, "WARN")),
               "场景外时间戳应给 WARN")
            ok(outside["ok"], "仅场景外时间戳（WARN）不应让校验失败")
            (stills / "0006.00.png").unlink()

            # 9) 缺时间戳文件名：有 timing 时是 ERROR
            _write(stills / "sheet.png", _png_bytes(40, 60, _gradient_rgba(40, 60)))
            no_stamp = check_frames(stills, timing=timing, project_root=project)
            ok(not no_stamp["ok"], "有 timing 时缺时间戳的帧应失败")
            ok(any("时间戳" in msg for msg in _levels(no_stamp, "ERROR")),
               "缺时间戳应报时间戳错误")
            (stills / "sheet.png").unlink()

            # 10) --no-timing 目录模式：只校验图片本身
            no_timing = check_frames(stills, timing=None, project_root=project)
            ok(no_timing["ok"], "--no-timing 模式（0 ERROR）应通过")
            ok(no_timing["stats"]["scene_count"] is None, "--no-timing 不应有场景统计")
            no_timing_text = format_text(no_timing)
            ok("期望尺寸：未指定" in no_timing_text, "无 timing 时人读输出应说明未核对分辨率")

            # 11) 目录不存在 = ERROR
            absent = check_frames(project / "out" / "nope", timing=timing, project_root=project)
            ok(not absent["ok"], "静帧目录不存在应失败")
            ok(any("找不到静帧目录" in msg for msg in _levels(absent, "ERROR")),
               "目录不存在应报「找不到静帧目录」")

            # 12) per_scene=2：每场景只有 1 帧 = ERROR
            need_two = check_frames(stills, timing=timing, project_root=project, per_scene=2)
            ok(not need_two["ok"], "--per-scene 2 时只有 1 帧/场景应失败")
            ok(any("至少 2 帧" in msg for msg in _levels(need_two, "ERROR")),
               "--per-scene 2 应报「至少 2 帧」")

            # 13) timing scenes 结构坏：干净 ERROR，不抛异常
            bad_timing = {"scenes": [{"name": "hook", "start": 0, "end": "2.5"}]}
            bad = check_frames(stills, timing=bad_timing, project_root=project)
            ok(not bad["ok"], "坏 scenes 结构应失败")
            ok(any("有限数字" in msg for msg in _levels(bad, "ERROR")),
               "坏 scenes 应报「有限数字」")

            # 13b) width/height 类型坏：不许静默跳过尺寸核对
            bad_size_timing = dict(timing)
            bad_size_timing["width"] = "1080"
            bad_size = check_frames(stills, timing=bad_size_timing, project_root=project)
            ok(not bad_size["ok"] and any(
                "width/height" in msg for msg in _levels(bad_size, "ERROR")),
               "timing.json width 是字符串时应报 ERROR（而不是静默跳过尺寸核对），实际：%r"
               % (_levels(bad_size, "ERROR"),))

            # 14) scale：期望尺寸跟着缩放
            scaled = check_frames(stills, timing=timing, project_root=project, scale=0.5)
            ok(not scaled["ok"] and scaled["expect"] == "20x30",
               "scale=0.5 时期望尺寸应为 20x30（实际 %r）" % (scaled["expect"],))

            # 15) 奇数尺寸 × 0.5：舍入必须和 stills.mjs 的 JS Math.round 一致
            # （1001*0.5=500.5 / 601*0.5=300.5，JS 得 501x301；Python round() 会得
            # 500x300 —— 之前真实抽帧会被这条尺寸断言误判成 ERROR）。
            odd_project = tmp_path / "odd_proj"
            (odd_project / "src").mkdir(parents=True)
            (odd_project / "src" / "timing.json").write_text(json.dumps({
                "width": 1001, "height": 601, "duration": 1.0,
                "scenes": [{"name": "only", "start": 0.0, "end": 1.0}],
            }), encoding="utf-8")
            odd_stills = odd_project / "out" / "stills"
            odd_stills.mkdir(parents=True)
            _write(odd_stills / "0000.00.png",
                   _png_bytes(501, 301, _gradient_rgba(501, 301)))
            odd = check_frames(odd_stills, timing=json.loads(
                (odd_project / "src" / "timing.json").read_text(encoding="utf-8")),
                project_root=odd_project, scale=0.5)
            ok(odd["expect"] == "501x301",
               "奇数尺寸 × 0.5 的期望尺寸应四舍五入为 501x301（实际 %r）" % (odd["expect"],))
            ok(odd["ok"], "501x301 的真实抽帧不应因舍入口径被误判：%r"
               % (odd["problems"][:3],))

    except AssertionError as exc:
        print("FAILED: frame_check.py --selftest：%s" % (exc,))
        return 1
    except Exception as exc:  # noqa: BLE001 - 自测里任何异常都算失败，但不该裸崩
        import traceback
        traceback.print_exc()
        print("FAILED: frame_check.py --selftest 异常：%s" % (exc,))
        return 1
    print("OK: selftest — 静帧校验 %d 项断言通过（时间戳/尺寸解析、PNG 解码、空白/截断/CRC、"
          "JPEG 结构、场景覆盖、--no-timing、--scale、坏 timing 干净报错）" % (checks,))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
