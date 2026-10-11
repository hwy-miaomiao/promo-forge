#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""素材整理：抓 Markdown 里的图、拼联系表、按实际裁切预览、限宽转码、列尺寸。

用途
    AGENTS.md 阶段 4（素材调研）与阶段 9（校验闸门）都用它：
      - grab     把 README / 产品文档里的图片一次性抓下来，并登记来源与授权线索
      - sheet    把一批图（例如 out/stills 的抽帧）拼成一张联系表，人一眼看完
      - crop     按视频里真正会用的 cover 裁切方式出预览，并给出 objectPosition
      - prepare  统一转 jpg + 限宽，避免 4K 渲染吃爆内存
      - info     逐张列宽高体积，一眼看出哪些图放大后会糊

典型调用
    python scripts/media.py grab README.md docs/*.md -o out/img --min 800
    python scripts/media.py sheet out/stills --cols 4
    python scripts/media.py sheet out/img --cols 3 --width 520 --minw 700
    python scripts/media.py crop out/check-crop.jpg public/img/*.png --ratio 9:16
    python scripts/media.py prepare ~/Pictures/raw public/img --max 2800
    python scripts/media.py info public/img out/shots

依赖
    Pillow（所有子命令）、requests（只 grab 用）。缺了就打印 ERROR 让你 pip install -r requirements.txt。
    不需要 ffmpeg。

退出码
    0 = 成功（可能有 WARN）
    1 = 缺依赖 / 路径不存在 / 一张图都没找到 / 有下载失败 / 全部图片读不出来
    2 = 命令行参数写错（argparse 的标准行为）
"""

from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import html as html_mod
import os
import re
import shutil
import sys
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.parse import unquote, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _common as C  # noqa: E402

# ---------------------------------------------------------------------------
# 常量（阈值集中在此，注释标注来源）
# ---------------------------------------------------------------------------

#: 认作图片的扩展名（Pillow 能读的）
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".avif", ".tif", ".tiff", ".heic"}
#: 抓到也丢掉：动图在视频里会掉帧、也没法用 Remotion 的 <Img> 稳定渲染。
#: 来源：AGENTS.md 阶段 4「必须用真实素材」+ 阶段 9 审图项（截图清晰度）
SKIP_EXTS = {".gif", ".svg"}
#: 抓图并发数（I/O 为主，8 是够快又不会被 CDN 当攻击的折中）
GRAB_WORKERS = 8
#: grab 默认最小宽度。来源：AGENTS.md 阶段 4「--factor 2 出 2 倍图（1080 宽显示 → 2160 像素）」，
#: 这里取一个宽松下限，低于它的图放大到 1080 宽一定糊。
GRAB_DEFAULT_MIN = 800
#: 单张图下载上限（防止误抓几 GB 的大文件把磁盘塞满）
MAX_DOWNLOAD_BYTES = 40 * 1024 * 1024
#: 下载超时（秒）
GRAB_TIMEOUT = 30
#: 浏览器 UA：不少图床对非浏览器 UA 直接 403
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

#: 联系表默认参数。来源：AGENTS.md 阶段 9 `media.py sheet out/stills --cols 4`
#: 注意 minw 默认取 320：抽帧产物（stills.mjs 默认 --scale 0.5 → 540px 宽）远小于网页素材，
#: 若默认 700 会把整批抽帧全部排除、联系表直接报错。审网页素材时显式加 --minw 700。
SHEET_COLS = 4
SHEET_WIDTH = 480
SHEET_MINW = 320
SHEET_QUALITY = 90
SHEET_BG = (24, 24, 28)
SHEET_THUMB_BORDER = (72, 74, 86)
SHEET_FG = (236, 236, 242)
SHEET_DIM = (152, 154, 166)

#: prepare 默认限宽。来源：AGENTS.md 阶段 4 / 第 7 节闸门（图片宽度 ≥ 显示宽度 ×2 = 2160），
#: 2800 是“4K 渲染时单张图解进内存还不至于爆”的上限。
PREPARE_MAX_WIDTH = 2800
PREPARE_QUALITY = 92

#: 判定“太小”的宽度。来源：本脚本契约（<1000px 标 太小（放大发虚））
MIN_USEFUL_PX = 1000
#: 判定“太大”的宽度。来源：本脚本契约（>3000px 标 太大（4K 渲染吃内存））
MAX_RENDER_PX = 3000

#: Markdown 图片：![alt](url) / ![alt](<url with space>) / ![alt](url "title")
MD_IMG_RE = re.compile(r"!\[[^\]]*\]\(\s*(?:<([^>]+)>|([^\s)]+))(?:\s+[\"'][^\"']*[\"'])?\s*\)")
#: HTML 图片：<img src="url"> / <img src='url'> / <img src=url>
HTML_IMG_RE = re.compile(r"""<img\b[^>]*?\bsrc\s*=\s*(?:"([^"]+)"|'([^']+)'|([^\s>"']+))""", re.I)
#: Markdown 标题（用来在 index.txt 里记“哪一节引用的这张图”）
MD_TITLE_RE = re.compile(r"^\s{0,3}#{1,6}\s+(.+?)\s*#*\s*$")

CONTENT_TYPE_EXT = {
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/bmp": ".bmp",
    "image/avif": ".avif",
    "image/tiff": ".tiff",
    "image/gif": ".gif",
    "image/svg+xml": ".svg",
}


# ---------------------------------------------------------------------------
# 延迟导入（没装依赖也要能看 --help）
# ---------------------------------------------------------------------------

_PIL: Optional[Tuple[Any, Any, Any, Any]] = None


def _pil() -> Tuple[Any, Any, Any, Any]:
    """返回 (Image, ImageDraw, ImageFont, ImageOps)；缺 Pillow -> ERROR。"""
    global _PIL
    if _PIL is None:
        try:
            from PIL import Image, ImageDraw, ImageFont, ImageOps
        except ImportError:
            C.error("缺少 Pillow，请运行 pip install -r requirements.txt（模板要求 Pillow>=10）")
        _PIL = (Image, ImageDraw, ImageFont, ImageOps)
    return _PIL


def _requests() -> Any:
    try:
        import requests
    except ImportError:
        C.error("缺少 requests，请运行 pip install -r requirements.txt（模板要求 requests>=2.31）")
    return requests


# ---------------------------------------------------------------------------
# 小工具：东亚宽度感知的排版（中文文件名对不齐会很难看）
# ---------------------------------------------------------------------------

def _dwidth(text: str) -> int:
    n = 0
    for ch in text:
        n += 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
    return n


def _dtrim(text: str, width: int) -> str:
    if _dwidth(text) <= width:
        return text
    out = ""
    for ch in text:
        if _dwidth(out) + _dwidth(ch) > width - 1:
            break
        out += ch
    return out + "…"


def _dpad(text: str, width: int, right: bool = False) -> str:
    text = _dtrim(text, width)
    pad = " " * max(0, width - _dwidth(text))
    return pad + text if right else text + pad


def _table(headers: Sequence[str], rows: Sequence[Sequence[str]], rights: Sequence[int] = ()) -> None:
    """打印一张宽度感知的纯文本表格。"""
    cols = len(headers)
    widths = [max(_dwidth(str(headers[i])), *(_dwidth(str(r[i])) for r in rows)) if rows
              else _dwidth(str(headers[i])) for i in range(cols)]
    maxw = [60, 22, 22, 24, 30, 40]
    widths = [min(w, maxw[i] if i < len(maxw) else 60) for i, w in enumerate(widths)]
    C.log("  ".join(_dpad(str(headers[i]), widths[i], i in rights) for i in range(cols)))
    C.log("  ".join("-" * widths[i] for i in range(cols)))
    for r in rows:
        C.log("  ".join(_dpad(str(r[i]), widths[i], i in rights) for i in range(cols)))


def _lanczos(Image: Any) -> Any:
    return getattr(getattr(Image, "Resampling", Image), "LANCZOS")


def _collect_images(paths: Iterable[Any], recursive: bool = True) -> List[Path]:
    """把目录/文件参数摊平成图片文件列表（跳过 _sheet.jpg 这类自己生成的产物）。"""
    out: List[Path] = []
    for raw in paths:
        p = Path(str(raw)).expanduser()
        if p.is_dir():
            it = p.rglob("*") if recursive else p.glob("*")
            for f in sorted(it):
                if f.is_file() and f.suffix.lower() in IMAGE_EXTS and not f.name.startswith("_"):
                    out.append(f)
        elif p.is_file():
            if p.suffix.lower() in IMAGE_EXTS:
                out.append(p)
            else:
                C.warn(f"{C.rel(p)} 不是图片（扩展名 {p.suffix}），跳过")
        else:
            C.warn(f"路径不存在：{C.rel(p)}")
    # 去重但保持顺序
    seen = set()
    uniq = []
    for f in out:
        key = str(f.resolve()).lower()
        if key not in seen:
            seen.add(key)
            uniq.append(f)
    return uniq


def _open_image(path: Path) -> Tuple[Any, Tuple[int, int]]:
    """打开图片并按 EXIF 摆正；失败抛异常（调用方转成 WARN）。"""
    Image, _, _, ImageOps = _pil()
    im = Image.open(path)
    im.load()
    try:
        im = ImageOps.exif_transpose(im)
    except Exception:
        pass
    return im, im.size


#: 中文字体候选（先读 fonts.json，再补各平台常见路径）
def _font_candidates() -> List[str]:
    cands = list(C.system_font_candidates())
    if sys.platform.startswith("win"):
        cands += [
            "C:/Windows/Fonts/msyh.ttc",
            "C:/Windows/Fonts/msyhbd.ttc",
            "C:/Windows/Fonts/simhei.ttf",
            "C:/Windows/Fonts/simsun.ttc",
            "C:/Windows/Fonts/Deng.ttf",
        ]
    elif sys.platform == "darwin":
        cands += [
            "/System/Library/Fonts/PingFang.ttc",
            "/System/Library/Fonts/Hiragino Sans GB.ttc",
            "/Library/Fonts/Arial Unicode.ttf",
        ]
    else:
        cands += [
            "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
            "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        ]
    seen, out = set(), []
    for c in cands:
        if c not in seen:
            seen.add(c)
            out.append(c)
    return out


def find_cjk_font() -> Optional[Path]:
    """找一个能画中文的字体文件；一个都没有返回 None（调用方退回默认字体并 WARN）。"""
    for c in _font_candidates():
        try:
            p = Path(str(c)).expanduser()
            if p.is_file():
                return p
        except OSError:
            continue
    return None


def _load_font(size: int) -> Any:
    Image, _, ImageFont, _ = _pil()
    font_path = find_cjk_font()
    if font_path is not None:
        try:
            return ImageFont.truetype(str(font_path), size)
        except Exception as exc:
            C.warn(f"字体 {C.rel(font_path)} 加载失败（{exc}），退回 Pillow 默认字体（中文会变方块）")
    else:
        C.warn(
            "没找到中文字体（试过 fonts.json 的 check.systemCandidates 与各平台常见路径），"
            "标签里的中文会显示成方块。装一个就行：Windows 自带微软雅黑；"
            "macOS 自带苹方；Linux: sudo apt install fonts-noto-cjk"
        )
    try:
        return ImageFont.load_default(size=size)  # Pillow >= 10.1
    except TypeError:
        return ImageFont.load_default()


# ---------------------------------------------------------------------------
# 子命令 1：grab
# ---------------------------------------------------------------------------

@dataclass
class GrabItem:
    url: str
    source: str
    title: str
    seq: int = 0
    path: Optional[Path] = None
    status: str = ""
    note: str = ""
    size: int = 0
    dims: str = ""


def _ext_for(url: str, content_type: str) -> str:
    ext = os.path.splitext(unquote(urlparse(url).path))[1].lower()
    if ext == ".jpeg":
        ext = ".jpg"
    if ext in IMAGE_EXTS or ext in SKIP_EXTS:
        return ext
    ct = (content_type or "").split(";")[0].strip().lower()
    return CONTENT_TYPE_EXT.get(ct, ".jpg")


def _extract_urls(text: str) -> List[str]:
    urls: List[str] = []
    for m in MD_IMG_RE.finditer(text):
        urls.append(m.group(1) or m.group(2))
    for m in HTML_IMG_RE.finditer(text):
        urls.append(m.group(1) or m.group(2) or m.group(3))
    out = []
    for u in urls:
        u = html_mod.unescape(u or "").strip().strip("<>")
        if u:
            out.append(u)
    return out


def _collect_grab_items(md_files: Sequence[Path], match: Optional[str]) -> Tuple[List[GrabItem], int]:
    items: List[GrabItem] = []
    skipped_local = 0
    seen = set()
    for md in md_files:
        try:
            text = md.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            C.error(f"读不到 {C.rel(md)}：{exc}")
        title = ""
        for line in text.splitlines():
            m = MD_TITLE_RE.match(line)
            if m:
                title = m.group(1).strip()
                break
        for url in _extract_urls(text):
            low = url.lower()
            if not low.startswith(("http://", "https://")):
                skipped_local += 1
                continue
            if match and match.lower() not in low:
                continue
            key = low.split("#")[0]
            if key in seen:
                continue
            seen.add(key)
            items.append(GrabItem(url=url.split("#")[0], source=C.rel(md), title=title or "（无标题）"))
    return items, skipped_local


def _download_item(req: Any, item: GrabItem, dest: Path, timeout: int, min_width: int) -> GrabItem:
    url = item.url
    ext = os.path.splitext(unquote(urlparse(url).path))[1].lower()
    if ext in SKIP_EXTS:
        item.note = f"跳过 {ext}（动图/矢量图：动图在视频里会掉帧，矢量图请改成截图）"
        return item
    try:
        resp = req.get(
            url,
            stream=True,
            timeout=timeout,
            allow_redirects=True,
            headers={"User-Agent": UA, "Accept": "image/*,*/*;q=0.8", "Referer": urlparse(url).scheme + "://" + (urlparse(url).netloc or "")},
        )
    except Exception as exc:  # requests 的异常种类很多，统一接住
        item.status = "连接失败"
        item.note = f"{type(exc).__name__}: {exc}"
        return item
    with resp:
        item.status = f"HTTP {resp.status_code}"
        if resp.status_code != 200:
            reason = str(resp.reason or "").strip()
            item.note = reason or "下载失败"
            return item
        real_ext = _ext_for(url, resp.headers.get("Content-Type", ""))
        if real_ext in SKIP_EXTS:
            item.note = f"跳过 {real_ext}（Content-Type 说明是动图/矢量图）"
            return item
        try:
            declared = int(resp.headers.get("Content-Length") or 0)
        except ValueError:
            declared = 0
        if declared > MAX_DOWNLOAD_BYTES:
            item.note = f"文件 {C.human(declared)} 超过上限 {C.human(MAX_DOWNLOAD_BYTES)}，跳过"
            return item
        item.seq = item.seq or 0
        name = f"{hashlib.md5(url.encode('utf-8')).hexdigest()[:6]}_{item.seq:02d}{real_ext}"
        path = dest / name
        written = 0
        try:
            with open(path, "wb") as fh:
                for chunk in resp.iter_content(chunk_size=65536):
                    if not chunk:
                        continue
                    written += len(chunk)
                    if written > MAX_DOWNLOAD_BYTES:
                        fh.close()
                        path.unlink(missing_ok=True)
                        item.note = f"下载超过上限 {C.human(MAX_DOWNLOAD_BYTES)}，已删除"
                        return item
                    fh.write(chunk)
        except Exception as exc:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
            item.note = f"写入失败：{type(exc).__name__}: {exc}"
            return item
    item.size = written
    # 用 Pillow 复核：能读、宽度够
    try:
        im, (w, h) = _open_image(path)
        im.close()
    except Exception as exc:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
        item.note = f"下下来不是能读的图片（{type(exc).__name__}），已删除"
        return item
    item.dims = f"{w}x{h}"
    if w < min_width:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
        item.dims = ""
        item.note = f"只有 {w}px 宽（< {min_width}），已删除：放大到 1080 宽会糊"
        return item
    item.path = path
    return item


def cmd_grab(args: argparse.Namespace) -> int:
    req = _requests()
    md_files: List[Path] = []
    for raw in args.md_files:
        for p in sorted(Path().glob(raw)) if any(ch in raw for ch in "*?[") else [Path(raw)]:
            if p.is_file():
                md_files.append(p)
            else:
                C.warn(f"不是文件，跳过：{raw}")
    if not md_files:
        C.error("没有可读的 Markdown 文件。用法：python scripts/media.py grab README.md docs/*.md")

    items, skipped_local = _collect_grab_items(md_files, args.match)
    if skipped_local:
        C.warn(f"{skipped_local} 个相对路径/本地路径的图片没法下载（脚本不知道 base URL），已跳过："
               f"请把文档里的相对路径换成绝对 URL，或自己在浏览器里截图")
    if not items:
        C.error(f"{len(md_files)} 个 Markdown 里没找到可下载的图片 URL"
                + (f"（--match {args.match!r} 过滤后为空）" if args.match else ""))

    dest = C.ensure_dir(args.out)
    for i, it in enumerate(items, start=1):
        it.seq = i
    C.log(f"从 {len(md_files)} 个文件里找到 {len(items)} 个图片 URL，开始下载（{GRAB_WORKERS} 并发）-> {C.rel(dest)}")

    done: List[GrabItem] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=GRAB_WORKERS) as pool:
        futures = [pool.submit(_download_item, req, it, dest, args.timeout, args.min) for it in items]
        for fut in concurrent.futures.as_completed(futures):
            try:
                done.append(fut.result())
            except Exception as exc:  # 兜底，不让线程池里的意外把整个脚本带崩
                C.warn(f"下载线程异常：{type(exc).__name__}: {exc}")
    done.sort(key=lambda x: x.seq)

    ok = [d for d in done if d.path]
    skipped = [d for d in done if not d.path and d.note.startswith("跳过")]
    bad = [d for d in done if not d.path and not d.note.startswith("跳过")]
    for d in done:
        if d.path:
            C.log(f"OK: {C.rel(d.path)} {d.dims} {C.human(d.size)}  ← {d.url}")
        elif d.note.startswith("跳过"):
            C.warn(f"{d.url}  {d.note}")
        else:
            C.warn(f"{d.url}  {d.status} {d.note}".rstrip())

    index = dest / "index.txt"
    lines = [
        "# promo-forge 素材索引（python scripts/media.py grab 生成）",
        "# 格式：文件名 ← 来源文件 / 章节标题 / URL",
        "# 授权与出处请同步抄进 <输出目录>/调研笔记.md（AGENTS.md 阶段 4 硬性要求）",
        f"# 共 {len(ok)} 张（尝试 {len(done)} 个 URL）",
        "",
    ]
    for d in ok:
        assert d.path is not None
        lines.append(f"{d.path.name} ← {d.source} / {d.title} / {d.url}")
    if bad:
        lines += ["", "# 以下 URL 没拿到文件（状态与原因）"]
        for d in bad:
            lines.append(f"# {d.url}  [{d.status} {d.note}]".rstrip())
    index.write_text("\n".join(lines) + "\n", encoding="utf-8")
    C.log("")
    C.log(f"索引：{C.rel(index)}（{len(ok)} 张）")
    if ok:
        C.log(f"下一步：python scripts/media.py sheet {C.rel(dest)} --cols 4   # 拼一张联系表过一眼")
    if skipped:
        C.log(f"另有 {len(skipped)} 个 URL 按规则跳过（gif/svg/超大文件/Content-Type 不对），不算失败")
    if bad:
        C.error(f"{len(bad)} 个 URL 没拿到图（上面每条 WARN 里都有 URL 与状态码）；已下载的 {len(ok)} 张保留在 {C.rel(dest)}")
    return 0


# ---------------------------------------------------------------------------
# 子命令 2：sheet
# ---------------------------------------------------------------------------

def _render_sheet(files: Sequence[Path], out_path: Path, cols: int, width: int,
                  labels: bool, title_prefix: str) -> Tuple[int, int, int]:
    """拼联系表，返回 (张数, 图宽, 图高)。读不出来的图会被跳过。"""
    Image, ImageDraw, _, _ = _pil()
    resample = _lanczos(Image)
    fs = max(13, int(width * 0.042))
    font = _load_font(fs)
    label_h = int(fs * 2.5) if labels else 0
    pad = max(6, int(width * 0.02))
    header_h = int(fs * 2.2)

    thumbs: List[Tuple[Path, Any, Tuple[int, int], Tuple[int, int]]] = []
    skipped: List[str] = []
    for f in files:
        try:
            im, orig = _open_image(f)
            if im.mode not in ("RGB", "L"):
                bg = Image.new("RGB", im.size, SHEET_BG)
                if "A" in im.getbands():
                    bg.paste(im.convert("RGBA"), mask=im.convert("RGBA").split()[-1])
                else:
                    bg.paste(im.convert("RGB"))
                im = bg
            else:
                im = im.convert("RGB")
            thumb = im.copy()
            thumb.thumbnail((width, 10 ** 6), resample)
            thumbs.append((f, thumb, orig, thumb.size))
        except Exception as exc:
            skipped.append(f"{C.rel(f)}（{type(exc).__name__}: {exc}）")
    if not thumbs:
        C.error("这些路径里没有一张能读出来的图（是不是格式不支持？用 media.py info 复查）")
    for s in skipped:
        C.warn(f"读不出来，已跳过：{s}")

    n = len(thumbs)
    rows = (n + cols - 1) // cols
    row_h: List[int] = []
    for r in range(rows):
        chunk = thumbs[r * cols:(r + 1) * cols]
        row_h.append(max(t[3][1] for t in chunk) + label_h + pad)

    sheet_w = cols * width + pad * (cols + 1)
    sheet_h = header_h + sum(row_h) + pad * rows + pad
    sheet = Image.new("RGB", (sheet_w, sheet_h), SHEET_BG)
    draw = ImageDraw.Draw(sheet)
    draw.text((pad, int(header_h * 0.3)),
              f"{title_prefix} —— {n} 张，缩到 {width}px 宽", font=font, fill=SHEET_FG)

    y = header_h
    for r in range(rows):
        chunk = thumbs[r * cols:(r + 1) * cols]
        for c, (f, thumb, orig, size) in enumerate(chunk):
            x = pad + c * (width + pad)
            tw, th = size
            ox = x + (width - tw) // 2
            sheet.paste(thumb, (ox, y))
            draw.rectangle([ox - 1, y - 1, ox + tw, y + th], outline=SHEET_THUMB_BORDER)
            if labels:
                ly = y + th + int(fs * 0.25)
                draw.text((x, ly), _dtrim(f.name, max(8, int(width / (fs * 0.62)))), font=font, fill=SHEET_FG)
                draw.text((x, ly + fs + int(fs * 0.15)), f"{orig[0]}x{orig[1]}", font=font, fill=SHEET_DIM)
        y += row_h[r]

    C.ensure_dir(out_path.parent)
    sheet.save(out_path, format="JPEG", quality=SHEET_QUALITY, optimize=True, progressive=True)
    if skipped:
        C.warn(f"{len(skipped)} 张读不出来被跳过（上面有明细）")
    return n, sheet_w, sheet_h


def cmd_sheet(args: argparse.Namespace) -> int:
    files = _collect_images(args.paths)
    if not files:
        C.error("没找到图片。用法：python scripts/media.py sheet out/stills --cols 4")
    if args.minw:
        small = [f for f in files if _safe_width(f) is not None and (_safe_width(f) or 0) < args.minw]
        if args.all:
            if small:
                C.warn(f"{len(small)} 张宽度 < {args.minw}px（--all 仍然拼进去了）："
                       + "、".join(C.rel(f) for f in small[:6]))
        else:
            files = [f for f in files if f not in small]
            if small:
                C.warn(f"{len(small)} 张宽度 < {args.minw}px，已排除（图太糊放联系表里也没参考价值）："
                       + "、".join(C.rel(f) for f in small[:6])
                       + "（--all 可强制拼进去；审网页素材建议 --minw 700，审抽帧保持默认即可）")
    if not files:
        C.error(f"排除掉小于 {args.minw}px 的图后没有剩下任何图片（用 --all 或调小 --minw）")

    out_path = Path(args.out) if args.out else (Path(args.paths[0]) / "_sheet.jpg" if Path(args.paths[0]).is_dir()
                                                else Path(args.paths[0]).with_name("_sheet.jpg"))
    title = f"联系表：{C.rel(Path(args.paths[0]))}"
    n, w, h = _render_sheet(files, out_path, args.cols, args.width, args.labels, title)
    size = out_path.stat().st_size
    C.log("")
    C.log(f"OK: {C.rel(out_path)}  {w}x{h}  {C.human(size)}（{n} 张，每行 {args.cols} 张）")
    C.log(f"下一步：打开这张图逐张看（AGENTS.md 阶段 9：抽帧联系表必须逐张看过）")
    return 0


def _safe_width(path: Path) -> Optional[int]:
    try:
        Image, _, _, _ = _pil()
        with Image.open(path) as im:
            return int(im.size[0])
    except Exception:
        return None


# ---------------------------------------------------------------------------
# 子命令 3：crop
# ---------------------------------------------------------------------------

def _parse_ratio(raw: str) -> Tuple[int, int]:
    txt = raw.strip().lower().replace("：", ":").replace("x", ":").replace("*", ":")
    parts = [p for p in txt.split(":") if p.strip()]
    if len(parts) != 2 or raw.strip() in ("", "cover"):
        C.error(f"--ratio 写法是 宽:高，例如 9:16、16:9、1:1；现在给的是 {raw!r}")
    try:
        rw, rh = int(float(parts[0])), int(float(parts[1]))
    except ValueError:
        C.error(f"--ratio 里不是数字：{raw!r}（写法：9:16）")
    if rw <= 0 or rh <= 0:
        C.error(f"--ratio 必须是正数：{raw!r}")
    return rw, rh


def _cover_box(w: int, h: int, rw: int, rh: int, anchor: str) -> Tuple[int, int, int, int, str, str]:
    """算 cover 裁切框，返回 (x0, y0, x1, y1, 方向说明, object-position)。"""
    target = rw / rh
    cur = w / h
    if abs(cur - target) < 1e-6:
        return 0, 0, w, h, "不用裁", "center"
    if cur > target:  # 图比目标宽 -> 裁左右
        new_w = max(1, int(round(h * target)))
        x0 = (w - new_w) // 2
        return x0, 0, x0 + new_w, h, "裁左右", "center"
    new_h = max(1, int(round(w / target)))  # 图比目标高 -> 裁上下
    if anchor == "top":
        y0 = 0
    elif anchor == "bottom":
        y0 = h - new_h
    else:
        y0 = (h - new_h) // 2
    return 0, y0, w, y0 + new_h, "裁上下", anchor


def cmd_crop(args: argparse.Namespace) -> int:
    Image, ImageDraw, _, _ = _pil()
    resample = _lanczos(Image)
    rw, rh = _parse_ratio(args.ratio)
    files = _collect_images(args.images, recursive=False)
    if not files:
        C.error("没找到图片。用法：python scripts/media.py crop out/check-crop.jpg public/img/*.png --ratio 9:16")

    out_path = Path(args.out)
    cell_w = args.cell
    fs = max(13, int(cell_w * 0.05))
    font = _load_font(fs)
    label_h = int(fs * 2.6)
    pad = max(6, int(cell_w * 0.03))
    header_h = int(fs * 2.2)
    cols = args.cols

    cells: List[Tuple[str, Any, str]] = []
    rows_txt: List[List[str]] = []
    for f in files:
        try:
            im, (w, h) = _open_image(f)
        except Exception as exc:
            C.warn(f"读不出来，跳过：{C.rel(f)}（{type(exc).__name__}: {exc}）")
            continue
        x0, y0, x1, y1, how, pos = _cover_box(w, h, rw, rh, args.anchor)
        cropped = im.convert("RGB").crop((x0, y0, x1, y1))
        cw, ch = cropped.size
        thumb = cropped.copy()
        thumb.thumbnail((cell_w, 10 ** 6), resample)
        cells.append((f.name, thumb, f"{cw}x{ch} {how}"))
        rows_txt.append([
            C.rel(f), f"{w}x{h}", f"{cw}x{ch}", how,
            f"object-position: {pos}",
        ])

    if not cells:
        C.error("所有图片都读不出来")

    rows = (len(cells) + cols - 1) // cols
    row_h = max(c[1].size[1] for c in cells) + label_h + pad
    sheet_w = cols * cell_w + pad * (cols + 1)
    sheet_h = header_h + rows * row_h + pad
    sheet = Image.new("RGB", (sheet_w, sheet_h), SHEET_BG)
    draw = ImageDraw.Draw(sheet)
    draw.text((pad, int(header_h * 0.3)),
              f"cover 裁切预览：{args.ratio}（anchor={args.anchor}）—— 共 {len(cells)} 张", font=font, fill=SHEET_FG)
    y = header_h
    for r in range(rows):
        chunk = cells[r * cols:(r + 1) * cols]
        for c, (name, thumb, info) in enumerate(chunk):
            x = pad + c * (cell_w + pad)
            tw, th = thumb.size
            ox = x + (cell_w - tw) // 2
            sheet.paste(thumb, (ox, y))
            draw.rectangle([ox - 1, y - 1, ox + tw, y + th], outline=SHEET_THUMB_BORDER)
            draw.text((x, y + th + int(fs * 0.25)), _dtrim(name, max(8, int(cell_w / (fs * 0.62)))), font=font, fill=SHEET_FG)
            draw.text((x, y + th + fs + int(fs * 0.15)), info, font=font, fill=SHEET_DIM)
        y += row_h
    C.ensure_dir(out_path.parent)
    sheet.save(out_path, format="JPEG", quality=SHEET_QUALITY, optimize=True, progressive=True)

    C.log(f"视频比例：{args.ratio}（anchor={args.anchor}）—— 下面是每张图在 cover 下真正会被看到的范围")
    C.log("")
    _table(["原图", "原尺寸", "裁切后", "裁掉", "给场景用"], rows_txt, rights=())
    C.log("")
    C.log(f"OK: 预览图 {C.rel(out_path)}  {sheet_w}x{sheet_h}")
    C.log("用法：Remotion 里 <Img src=... style={{objectFit:'cover', objectPosition:'...'}} />；"
          "上面的 object-position 就是该填的值（center/top/bottom 等价于 50% 50% / 50% 0% / 50% 100%）。")
    C.log("注意：裁切预览只代表 cover 的默认行为；若组件里用了自定义 transform，请以抽帧为准（阶段 9）。")
    return 0


# ---------------------------------------------------------------------------
# 子命令 4：prepare
# ---------------------------------------------------------------------------

FMT_EXT = {"jpg": ".jpg", "jpeg": ".jpg", "png": ".png", "webp": ".webp"}


def cmd_prepare(args: argparse.Namespace) -> int:
    Image, _, _, _ = _pil()
    resample = _lanczos(Image)
    fmt = args.format.lower()
    if fmt not in FMT_EXT:
        C.error(f"--format 只能是 jpg / png / webp，现在是 {args.format!r}")
    dst = C.ensure_dir(args.dst)
    src_files = _collect_images([args.src])
    if not src_files:
        C.error(f"{args.src} 里没有找到图片（支持 {'/'.join(sorted(e.lstrip('.') for e in IMAGE_EXTS))}）")

    C.log(f"处理 {len(src_files)} 张 -> {C.rel(dst)}（限宽 {args.max}px，统一 {fmt}）")
    rows: List[List[str]] = []
    total_before = total_after = 0
    used_names = set()
    alpha_warned = False
    failed = 0

    for f in src_files:
        before = f.stat().st_size
        total_before += before
        base = f.stem
        name = base + FMT_EXT[fmt]
        k = 1
        while name.lower() in used_names:
            k += 1
            name = f"{base}-{k}{FMT_EXT[fmt]}"
        used_names.add(name.lower())
        target = dst / name
        try:
            im, (w, h) = _open_image(f)
        except Exception as exc:
            C.warn(f"读不出来，跳过：{C.rel(f)}（{type(exc).__name__}: {exc}）")
            failed += 1
            continue

        same = (target.resolve() == f.resolve()) if target.exists() else False
        need_resize = w > args.max
        new_w, new_h = (args.max, max(1, int(round(h * args.max / w)))) if need_resize else (w, h)
        same_format = f.suffix.lower() == FMT_EXT[fmt]
        if same and not need_resize and same_format:
            C.warn(f"{C.rel(f)} 已经符合要求（{w}x{h} {fmt}），跳过")
            total_after += before
            continue
        if (not need_resize) and same_format and not same:
            # 尺寸与格式都不用改：原样复制，避免再压一次掉画质
            shutil.copy2(f, target)
            after = target.stat().st_size
            total_after += after
            rows.append([name, f"{w}x{h}", f"{new_w}x{new_h}", C.human(before), C.human(after), "原样复制"])
            continue

        if im.mode in ("RGBA", "LA", "P") and fmt == "jpg":
            if not alpha_warned:
                C.warn("源图带透明通道，转 jpg 会铺成白底（要保留透明请用 --format png）")
                alpha_warned = True
            rgba = im.convert("RGBA")
            bg = Image.new("RGB", rgba.size, (255, 255, 255))
            bg.paste(rgba, mask=rgba.split()[-1])
            im = bg
        elif fmt == "jpg":
            im = im.convert("RGB")

        if need_resize:
            im = im.resize((new_w, new_h), resample)

        try:
            if fmt == "jpg":
                im.save(target, format="JPEG", quality=args.quality, optimize=True, progressive=True)
            elif fmt == "png":
                im.save(target, format="PNG", optimize=True)
            else:
                im.save(target, format="WEBP", quality=args.quality, method=6)
        except Exception as exc:
            C.warn(f"写不出来，跳过：{C.rel(target)}（{type(exc).__name__}: {exc}）")
            failed += 1
            continue
        finally:
            try:
                im.close()
            except Exception:
                pass
        after = target.stat().st_size
        total_after += after
        saved = ""
        if before > 0:
            pct = (after - before) / before * 100
            saved = f"{pct:+.0f}%"
        rows.append([name, f"{w}x{h}", f"{new_w}x{new_h}", C.human(before), C.human(after) + " " + saved,
                     "缩了" if need_resize else "只转格式"])

    if not rows:
        C.error("没有任何文件被处理（见上面的 WARN）")
    C.log("")
    _table(["输出文件", "原尺寸", "新尺寸", "原体积", "新体积", "动作"], rows)
    C.log("")
    delta = total_after - total_before
    pct = (delta / total_before * 100) if total_before else 0.0
    C.log(f"共 {len(rows)} 张：{C.human(total_before)} -> {C.human(total_after)}（{pct:+.0f}%）")
    if failed:
        C.error(f"{failed} 张处理失败（见上面的 WARN）")
    return 0


# ---------------------------------------------------------------------------
# 子命令 5：info
# ---------------------------------------------------------------------------

def cmd_info(args: argparse.Namespace) -> int:
    files = _collect_images(args.paths)
    if not files:
        C.error("没找到图片。用法：python scripts/media.py info public/img out/shots")

    rows: List[List[str]] = []
    total = 0
    widths: List[int] = []
    too_small: List[str] = []
    too_big: List[str] = []
    unreadable: List[str] = []
    for f in files:
        size = f.stat().st_size
        total += size
        try:
            im, (w, h) = _open_image(f)
            im.close()
        except Exception as exc:
            unreadable.append(C.rel(f))
            rows.append([C.rel(f), "?", "?", C.human(size), f"读不出来（{type(exc).__name__}）"])
            continue
        widths.append(w)
        if w < MIN_USEFUL_PX:
            tag = "太小（放大发虚）"
            too_small.append(C.rel(f))
        elif w > MAX_RENDER_PX:
            tag = "太大（4K 渲染吃内存）"
            too_big.append(C.rel(f))
        else:
            tag = "OK"
        rows.append([C.rel(f), f"{w}x{h}", f"{w / h:.2f}", C.human(size), tag])

    C.log(f"共 {len(files)} 个文件，{C.human(total)}")
    C.log("")
    _table(["文件", "宽x高", "比例", "体积", "判定"], rows)
    C.log("")
    if widths:
        C.log(f"宽度：最小 {min(widths)}px，最大 {max(widths)}px，平均 {sum(widths) / len(widths):.0f}px；"
              f"建议 ≥{MIN_USEFUL_PX}px（放 1080 宽竖屏不虚），≤{MAX_RENDER_PX}px（4K 渲染不爆内存）")
    C.log(f"判定：够用 {len(widths) - len(too_small) - len(too_big)} 张，太小 {len(too_small)} 张，"
          f"太大 {len(too_big)} 张，读不出来 {len(unreadable)} 张")
    if too_small:
        C.warn(f"{len(too_small)} 张宽度 < {MIN_USEFUL_PX}px："
               + "、".join(too_small[:6]) + ("…" if len(too_small) > 6 else "")
               + " —— 用 capture.py --factor 2 重截，或换更大的原图（AGENTS.md 阶段 4）")
    if too_big:
        C.warn(f"{len(too_big)} 张宽度 > {MAX_RENDER_PX}px："
               + "、".join(too_big[:6]) + ("…" if len(too_big) > 6 else "")
               + " —— 跑 python scripts/media.py prepare <源目录> public/img --max 2800 压一下")
    if unreadable:
        C.error(f"{len(unreadable)} 个文件读不出来（可能是坏图、HEIC、或根本不是图片）："
                + "、".join(unreadable[:8]))
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="media.py",
        description="素材整理：抓 Markdown 里的图 / 拼联系表 / 裁切预览 / 限宽转码 / 列尺寸。",
        epilog=(
            "例：\n"
            "  python scripts/media.py grab README.md docs/*.md -o out/img --min 800\n"
            "  python scripts/media.py sheet out/stills --cols 4\n"
            "  python scripts/media.py crop out/check-crop.jpg public/img/*.png --ratio 9:16 --anchor top\n"
            "  python scripts/media.py prepare raw/ public/img --max 2800\n"
            "  python scripts/media.py info public/img out/shots\n"
            "只处理本机文件；grab 只下载文档里已经写明的图片 URL，不做任何站点爬取。"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--version", action="version", version="media.py 1.0")
    sub = p.add_subparsers(dest="cmd", required=True, metavar="子命令")

    g = sub.add_parser("grab", help="抓 Markdown 里所有图片 URL 并下载",
                       description="正则抽出 Markdown/HTML 里的图片 URL，多线程下载到目录，并写 index.txt 记来源。",
                       epilog="例：python scripts/media.py grab README.md -o out/img --min 800")
    g.add_argument("md_files", nargs="+", metavar="md文件", help="要扫的 Markdown 文件（支持通配符）")
    g.add_argument("-o", "--out", default="out/img", help="输出目录，默认 out/img")
    g.add_argument("--match", default=None, help="只下 URL 里含该关键字的图（不区分大小写）")
    g.add_argument("--min", type=int, default=GRAB_DEFAULT_MIN,
                   help=f"小于该宽度的图删掉并 WARN，默认 {GRAB_DEFAULT_MIN}")
    g.add_argument("--timeout", type=int, default=GRAB_TIMEOUT, help=f"单张下载超时秒数，默认 {GRAB_TIMEOUT}")
    g.set_defaults(func=cmd_grab)

    s = sub.add_parser("sheet", help="把一批图拼成联系表",
                       description="每行 cols 张、每张缩到 width 宽保持比例，下面写文件名与像素尺寸。",
                       epilog="例：python scripts/media.py sheet out/stills --cols 4")
    s.add_argument("paths", nargs="+", metavar="目录或图片", help="图片目录（或若干图片文件）")
    s.add_argument("--cols", type=int, default=SHEET_COLS, help=f"每行几张，默认 {SHEET_COLS}")
    s.add_argument("--width", type=int, default=SHEET_WIDTH, help=f"每张缩到多少像素宽，默认 {SHEET_WIDTH}")
    s.add_argument("--out", default=None, help="输出路径，默认 <目录>/_sheet.jpg")
    s.add_argument("--minw", type=int, default=SHEET_MINW,
                   help=f"宽度小于该值的图不拼进去，默认 {SHEET_MINW}（0 = 不过滤）")
    s.add_argument("--labels", dest="labels", action="store_true", default=True, help="显示文件名+尺寸标签（默认开）")
    s.add_argument("--no-labels", dest="labels", action="store_false", help="不显示标签")
    s.add_argument("--all", action="store_true", help="忽略 --minw，把太小的图也拼进去")
    s.set_defaults(func=cmd_sheet)

    c = sub.add_parser("crop", help="按 cover 裁切预览并给出 objectPosition",
                       description="按视频真实会用的 cover 方式裁切，拼成预览图，并打印每张裁切后的尺寸与建议的 objectPosition。",
                       epilog="例：python scripts/media.py crop out/check-crop.jpg public/img/*.png --ratio 9:16")
    c.add_argument("out", metavar="输出图", help="预览拼图的输出路径，例如 out/check-crop.jpg")
    c.add_argument("images", nargs="+", metavar="图片", help="要预览的图片")
    c.add_argument("--ratio", default="9:16", help="目标比例 宽:高，默认 9:16（竖屏）；也支持 16:9 / 1:1")
    c.add_argument("--anchor", choices=["top", "center", "bottom"], default="center",
                   help="裁上下时保留哪一边，默认 center")
    c.add_argument("--cols", type=int, default=3, help="预览图每行几张，默认 3")
    c.add_argument("--cell", type=int, default=360, help="预览图每张的宽度，默认 360")
    c.set_defaults(func=cmd_crop)

    pr = sub.add_parser("prepare", help="统一转 jpg + 限宽",
                        description="把源目录/文件里的图统一成一种格式并限制最大宽度，打印处理清单与省下的体积。",
                        epilog="例：python scripts/media.py prepare raw/ public/img --max 2800")
    pr.add_argument("src", metavar="源目录或文件", help="源目录（递归）或单个图片文件")
    pr.add_argument("dst", metavar="目标目录", help="输出目录")
    pr.add_argument("--max", type=int, default=PREPARE_MAX_WIDTH,
                    help=f"最大宽度，超过就等比缩小，默认 {PREPARE_MAX_WIDTH}（4K 渲染内存保护）")
    pr.add_argument("--format", default="jpg", help="统一成什么格式：jpg / png / webp，默认 jpg")
    pr.add_argument("--quality", type=int, default=PREPARE_QUALITY, help=f"jpg/webp 质量，默认 {PREPARE_QUALITY}")
    pr.set_defaults(func=cmd_prepare)

    i = sub.add_parser("info", help="列每张图的宽高与体积",
                       description="逐张列宽高体积，并按“太小/太大”打标，最后给统计。",
                       epilog="例：python scripts/media.py info public/img out/shots")
    i.add_argument("paths", nargs="+", metavar="目录或文件", help="图片目录或文件（可多个）")
    i.set_defaults(func=cmd_info)
    return p


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args) or 0)


if __name__ == "__main__":
    sys.exit(main())
