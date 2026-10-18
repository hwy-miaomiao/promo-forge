#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Playwright 真实网页截图：默认 2 倍图，给竖屏宣传片用。

用途
    AGENTS.md 阶段 4：功能展示必须是真实界面截图，且分辨率要够（1080 宽显示 → 2160 像素）。
    本脚本用 Playwright 起真实 Chromium：先做懒加载滚动，再按指定 Y 位置逐张截图，
    或把视口拉高截整页长图；需要登录时弹一个真实窗口让你登一次，登录态持久化复用。

典型调用
    python scripts/capture.py https://acme.app acme-home
    python scripts/capture.py https://acme.app acme-home --factor 2 --at 0,900,1800
    python scripts/capture.py https://acme.app acme-long --full 3600
    python scripts/capture.py https://acme.app acme-app --device iphone
    python scripts/capture.py https://acme.app/dashboard dash --login --hide ".cookie-banner" --eval "document.querySelector('.tab2').click()"

依赖
    playwright（pip install playwright && playwright install chromium）。缺了会打印 ERROR 而不是崩栈。

退出码
    0 = 截图完成（可能有 WARN）
    1 = 缺 playwright / 浏览器、打不开页面、一张都没截成、有 --eval 执行失败、
        --login 在非交互终端里跑（没法等你按回车，见 V5）
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _common as C  # noqa: E402

# ---------------------------------------------------------------------------
# 常量（阈值集中在此，注释标注来源）
# ---------------------------------------------------------------------------

#: 设备预设：视口、deviceScaleFactor、User-Agent
DEVICES: Dict[str, Dict[str, Any]] = {
    "iphone": {
        "viewport": (390, 844),
        "dsf": 3.0,
        "ua": ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 "
               "(KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1"),
    },
    "android": {
        "viewport": (412, 915),
        "dsf": 2.625,
        "ua": ("Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 "
               "(KHTML, like Gecko) Chrome/126.0.0.0 Mobile Safari/537.36"),
    },
    "desktop": {
        "viewport": (1440, 900),
        "dsf": 2.0,
        "ua": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
               "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"),
    },
    "tablet": {
        "viewport": (834, 1112),
        "dsf": 2.0,
        "ua": ("Mozilla/5.0 (iPad; CPU OS 17_5 like Mac OS X) AppleWebKit/605.1.15 "
               "(KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1"),
    },
}
#: 不指定 --device 时的视口：与成片竖屏 1080×1920 同尺寸，方便直接铺满画面
DEFAULT_VIEWPORT = (1080, 1920)
#: 默认 deviceScaleFactor。来源：AGENTS.md 阶段 4「--factor 2 出 2 倍图」
DEFAULT_FACTOR = 2.0
#: networkidle 之后的额外等待（秒）。来源：命令行默认值 --wait 1.5
DEFAULT_WAIT = 1.5
#: JPEG 质量
DEFAULT_QUALITY = 92
#: 截图宽度低于该值就 WARN。来源：AGENTS.md 阶段 4「1080 宽显示 → 2160 像素」，
#: 低于 1440 的图放进视频放大后一定糊。
MIN_USEFUL_WIDTH = 1440
#: 手机预设的清晰度下限。手机截图在竖屏片子里只占约 300~500 逻辑像素宽（手机模型内），
#: 2 倍图 600~1000px 就够：iphone 390×3=1170、android 412×2.625=1082，都不该被报警。
#: 来源：pr-b4#F4（旧实现用固定 1440 把两种手机预设全判成 WARN，还建议换 desktop）。
MIN_USEFUL_WIDTH_PHONE = 1000
#: 各设备预设对应的清晰度下限；不在这张表里的（默认视口 / desktop / tablet）用 MIN_USEFUL_WIDTH。
MIN_USEFUL_WIDTH_BY_DEVICE = {
    "iphone": MIN_USEFUL_WIDTH_PHONE,
    "android": MIN_USEFUL_WIDTH_PHONE,
}
#: 懒加载滚动：每步等 0.3 秒、最多 60 步（防止无限滚动页面把脚本挂住）。
#: 来源：AGENTS.md 阶段 4 要求截图前触发懒加载；0.3s 是实测大多数图片解码完成的间隔。
LAZY_STEP_WAIT = 0.3
LAZY_MAX_STEPS = 60
#: 页面打开超时 / networkidle 等待上限（秒）
NAV_TIMEOUT_MS = 60_000
NETWORKIDLE_TIMEOUT_MS = 15_000
#: 每次滚动定位后、截图前的稳定等待（秒）
SHOT_SETTLE = 0.5
#: 默认隐藏的“右上角头像类”元素选择器（--keep-avatar 可关掉）。
#: 来源：AGENTS.md 阶段 4 截图要给人看产品，个人头像属于隐私噪音。
AVATAR_SELECTORS = [
    'img[alt*="头像"]',
    'img[alt*="avatar" i]',
    'img[class*="avatar" i]',
    '[class*="avatar" i]',
    '[class*="profile-pic" i]',
    '[class*="userinfo" i]',
    '[class*="user-info" i]',
    '[data-testid*="avatar" i]',
]
#: 头像判定的几何区域：视口右上角（宽度比例、高度比例）
AVATAR_ZONE = (0.55, 0.16)

#: 懒加载触发脚本：滚到底再回顶（步长 = 视口高，每步等 0.3s）
LAZY_LOAD_JS = """
() => new Promise((resolve) => {
  const step = Math.max(200, window.innerHeight);
  const max = %d;
  let y = 0, n = 0;
  const tick = () => {
    y += step; n += 1;
    window.scrollTo(0, y);
    const h = Math.max(document.documentElement.scrollHeight, document.body ? document.body.scrollHeight : 0);
    if (y < h && n < max) { setTimeout(tick, %d); }
    else { window.scrollTo(0, 0); setTimeout(() => resolve(h), %d); }
  };
  setTimeout(tick, %d);
});
""" % (LAZY_MAX_STEPS, int(LAZY_STEP_WAIT * 1000), int(LAZY_STEP_WAIT * 1000), int(LAZY_STEP_WAIT * 1000))

#: 隐藏指定选择器（传数组进去）
HIDE_JS = """
(selectors) => {
  const hit = [];
  for (const sel of selectors) {
    let n = 0;
    try { document.querySelectorAll(sel).forEach((el) => { el.style.visibility = 'hidden'; n += 1; }); }
    catch (e) { hit.push(sel + ' (选择器非法: ' + e.message + ')'); continue; }
    if (n === 0) { hit.push(sel + ' (0 个匹配)'); } else { hit.push(sel + ' x' + n); }
  }
  return hit;
}
"""

#: 隐藏右上角头像类元素（选择器 + 几何区域双重判定）
AVATAR_JS = """
(args) => {
  const [selectors, zw, zh] = args;
  const vw = window.innerWidth, vh = window.innerHeight;
  let n = 0;
  for (const sel of selectors) {
    let list = [];
    try { list = Array.from(document.querySelectorAll(sel)); } catch (e) { continue; }
    for (const el of list) {
      const r = el.getBoundingClientRect();
      if (r.width < 8 || r.height < 8) continue;
      if (r.left < vw * zw || r.top > vh * zh) continue;   // 只看右上角
      el.style.visibility = 'hidden';
      n += 1;
    }
  }
  return n;
}
"""


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------

def _jpeg_size(path: Path) -> Optional[Tuple[int, int]]:
    """只读文件头解析 JPEG 宽高（不依赖 Pillow）。

    按段长跳过 APP0/APP1/ICC 等元数据段，只读到 SOF 为止：真实截图一般几百字节就够，
    而相机/手机导出的图可能把 EXIF + ICC 塞在 SOF 前面上百 KB，所以不能「一口气读 256KB
    再在缓冲区内扫」——那样 SOF 落在缓冲区外就返回 None，宽度护栏会被静默跳过。
    """
    try:
        with open(path, "rb") as fh:
            if fh.read(2) != b"\xff\xd8":
                return None
            while True:
                # 标记可以带 0xFF 填充字节
                b = fh.read(1)
                while b == b"\xff":
                    b = fh.read(1)
                if not b:
                    return None
                marker = b[0]
                if marker == 0x01 or 0xD0 <= marker <= 0xD8:
                    continue                       # 无参数标记，后面直接跟下一个标记
                head = fh.read(2)
                if len(head) < 2:
                    return None
                seg_len = int.from_bytes(head, "big")
                if seg_len < 2:                    # 段长非法：seek 会后退，可能死循环
                    return None
                if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
                    body = fh.read(5)              # 精度(1) + 高(2) + 宽(2)
                    if len(body) < 5:
                        return None
                    return (int.from_bytes(body[3:5], "big"), int.from_bytes(body[1:3], "big"))
                if marker == 0xDA:                 # SOS：后面是压缩数据，不可能再有 SOF
                    return None
                fh.seek(seg_len - 2, 1)
    except (OSError, ValueError):
        return None


def _parse_at(raw: Optional[str]) -> List[float]:
    if not raw:
        return []
    out: List[float] = []
    for part in raw.replace("，", ",").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            y = float(part)
        except ValueError:
            C.error(f"--at 里 {part!r} 不是数字。写法：--at 0,900,1800")
        if y < 0:
            C.error(f"--at 里的 {y} 是负数。写法：--at 0,900,1800")
        out.append(y)
    return out


def _scroll_to(page, y: float) -> None:
    """滚动到 y，**回读真实位置后**再说话。

    页面不够高、内容在自绘容器里滚、或站点自己接管了滚动时，``window.scrollTo()``
    不报错但页面根本没动。不回读就会在日志里谎报「已滚动到 y=…」，
    拿到手的截图其实是另一段内容 —— 所以这里只报读到的事实。
    """
    page.evaluate("(y) => window.scrollTo(0, y)", y)
    try:
        actual = float(page.evaluate("() => Math.round(window.scrollY)"))
    except Exception:
        actual = None
    if actual is None:
        C.log(f"    （请求滚动到 y={y:.0f}，没读到实际位置，无法确认）")
    elif abs(actual - y) <= 1:
        C.log(f"    （已滚动到 y={actual:.0f}）")
    else:
        C.warn(
            f"    目标 y={y:.0f}，页面实际停在 y={actual:.0f}"
            "（页面不够高，或者滚动被页面自己接管；这张图可能不是你以为的那一段）"
        )


def _flatten(groups: Optional[Sequence[Sequence[str]]]) -> List[str]:
    out: List[str] = []
    for g in groups or []:
        item = g if isinstance(g, (list, tuple)) else [g]
        out.extend(str(x) for x in item)
    return out


def _report(path: Path, factor: float, min_width: int, device: Optional[str]) -> bool:
    """打印一张截图的实际像素尺寸；宽度不够返回 False。

    ``min_width`` 由设备预设决定（手机预设的下限低得多，见 MIN_USEFUL_WIDTH_PHONE），
    不再拿统一的 1440 去卡手机截图，WARN 文案也不会一律建议换 desktop（V4）。
    """
    size = _jpeg_size(path)
    if size is None:
        C.log(f"OK: {C.rel(path)} （截到了，但读不出像素尺寸）")
        return True
    w, h = size
    C.log(f"OK: {C.rel(path)} {w}x{h}（{factor:g} 倍图）")
    if w < min_width:
        if device in MIN_USEFUL_WIDTH_BY_DEVICE:
            C.warn(f"{C.rel(path)} 只有 {w}px 宽（< {min_width}，{device} 预设的清晰度下限）："
                   "片子里手机截图只占约 300~500 逻辑像素宽，正常不会糊；"
                   "如果你打算把这张图放大铺满画面，把 --factor 调大（例如 4）再截。")
        else:
            C.warn(f"{C.rel(path)} 只有 {w}px 宽（< {min_width}）：放进 1080 宽的竖屏再放大就会糊。"
                   "换 --device desktop，或调大 --factor，或截更窄的容器。")
        return False
    return True


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="capture.py",
        description="用 Playwright 截真实网页图（默认 2 倍图，竖屏 1080×1920 优先）。",
        epilog=(
            "例：\n"
            "  python scripts/capture.py https://acme.app acme-home\n"
            "  python scripts/capture.py https://acme.app acme-home --at 0,900,1800\n"
            "  python scripts/capture.py https://acme.app acme-long --full 3600\n"
            "  python scripts/capture.py https://acme.app/app acme-app --device iphone\n"
            "  python scripts/capture.py https://acme.app/dash dash --login --hide '.cookie' --eval \"document.querySelector('.tab2').click()\"\n"
            "截图默认落在 out/shots/，登录态存在 out/.browser-profile/（下次自动复用）。"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("url", help="要截的网页地址（http/https）")
    p.add_argument("name", help="截图名前缀，例如 acme-home")
    p.add_argument("--factor", type=float, default=None,
                   help=f"deviceScaleFactor，默认 {DEFAULT_FACTOR:g}（2 倍图）；显式指定会覆盖 --device 预设")
    p.add_argument("--device", choices=sorted(DEVICES), default=None,
                   help="设备预设（视口 + UA）：iphone 390x844@3 / android 412x915@2.625 / desktop 1440x900@2 / tablet 834x1112@2")
    p.add_argument("--at", default=None,
                   help="滚动到这些 Y 位置各截一张，逗号分隔，例如 0,900,1800")
    p.add_argument("--full", type=float, default=None,
                   help="把视口拉高到该像素后截整页长图，例如 3600（输出为 <name>-full.jpg）")
    p.add_argument("--hide", action="append", nargs="+", default=None, metavar="选择器",
                   help="隐藏元素（CSS 选择器），可多次/多个")
    p.add_argument("--eval", action="append", nargs="+", default=None, metavar="JS",
                   help="截图前执行 JS，可多次/多个")
    p.add_argument("--login", action="store_true",
                   help="用 out/.browser-profile 持久化上下文，打开有头窗口等你登录，按回车后继续")
    p.add_argument("--out", default="out/shots", help="输出目录，默认 out/shots")
    p.add_argument("--wait", type=float, default=DEFAULT_WAIT,
                   help=f"networkidle 之后额外等待秒数，默认 {DEFAULT_WAIT:g}")
    p.add_argument("--quality", type=int, default=DEFAULT_QUALITY, help=f"JPEG 质量，默认 {DEFAULT_QUALITY}")
    p.add_argument("--keep-avatar", action="store_true",
                   help="保留右上角头像类元素（默认会隐藏，避免把个人信息放进宣传片）")
    return p


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)

    # --login 要等你在真实终端里按回车。非交互终端（CI / IDE 后台 / 管道）里过去
    # 只 WARN 就继续，最后以退出码 0 交出一整套登录页截图，check_assets 查不出来（V5）。
    # 这里直接失败：宁可一条都不截，也不要产出「看起来成功、其实是登录页」的素材。
    if args.login and not sys.stdin.isatty():
        C.error("标准输入不是交互终端，没法等你按回车登录：--login 需要在真实终端里跑。"
                "CI / IDE 后台执行请去掉 --login；先在本地终端登录一次，"
                "登录态会存进 out/.browser-profile，后续命令自动复用。")

    # playwright 延迟导入：没装也要能看 --help
    try:
        from playwright.sync_api import TimeoutError as PWTimeout
        from playwright.sync_api import sync_playwright
    except ImportError:
        C.error("缺少 playwright，请运行 pip install playwright && playwright install chromium")

    url = args.url
    if not url.lower().startswith(("http://", "https://")):
        url = "https://" + url.lstrip("/")

    if args.device:
        preset = DEVICES[args.device]
        viewport = tuple(preset["viewport"])
        ua = preset["ua"]
        factor = float(args.factor) if args.factor is not None else float(preset["dsf"])
    else:
        viewport = DEFAULT_VIEWPORT
        ua = DEVICES["desktop"]["ua"]
        factor = float(args.factor) if args.factor is not None else DEFAULT_FACTOR
    if factor <= 0:
        C.error(f"--factor 必须 > 0，现在是 {factor}")
    # 清晰度下限按设备预设选：手机截图在片子里只占 300~500 逻辑像素宽，
    # 拿 1440 卡 1170/1082 会误报（V4）。见 MIN_USEFUL_WIDTH_BY_DEVICE。
    min_width = MIN_USEFUL_WIDTH_BY_DEVICE.get(args.device or "", MIN_USEFUL_WIDTH)

    at_list = _parse_at(args.at)
    hides = _flatten(args.hide)
    evals = _flatten(args.eval)
    out_dir = C.ensure_dir(args.out)
    profile_dir = C.PROJECT_ROOT / "out" / ".browser-profile"

    C.log(f"页面：{url}")
    C.log(f"视口：{viewport[0]}x{viewport[1]} @{factor:g}x -> "
          f"{int(viewport[0] * factor)}x{int(viewport[1] * factor)}"
          f"{'（设备 ' + args.device + '）' if args.device else '（默认，与成片竖屏同尺寸）'}")
    shots: List[Path] = []
    eval_failures: List[str] = []
    too_narrow = 0

    with sync_playwright() as pw:
        reuse_profile = profile_dir.is_dir() and any(profile_dir.iterdir())
        headed = bool(args.login)
        ctx = None
        browser = None
        try:
            if args.login or reuse_profile:
                C.ensure_dir(profile_dir)
                if reuse_profile and not args.login:
                    C.log(f"复用登录态：{C.rel(profile_dir)}")
                ctx = pw.chromium.launch_persistent_context(
                    user_data_dir=str(profile_dir),
                    headless=not headed,
                    viewport={"width": viewport[0], "height": viewport[1]},
                    device_scale_factor=factor,
                    user_agent=ua,
                    locale="zh-CN",
                    args=["--hide-scrollbars", "--disable-blink-features=AutomationControlled"]
                    + ([f"--window-size={min(viewport[0], 1280)},{min(viewport[1], 900)}"] if headed else []),
                )
                page = ctx.pages[0] if ctx.pages else ctx.new_page()
            else:
                browser = pw.chromium.launch(headless=True, args=["--hide-scrollbars"])
                ctx = browser.new_context(
                    viewport={"width": viewport[0], "height": viewport[1]},
                    device_scale_factor=factor,
                    user_agent=ua,
                    locale="zh-CN",
                )
                page = ctx.new_page()
        except Exception as exc:
            C.error(
                "启动 Chromium 失败：{}: {}\n"
                "多半是浏览器没装：python -m playwright install chromium\n"
                "（国内网络可先设 PLAYWRIGHT_DOWNLOAD_HOST=https://npmmirror.com/mirrors/playwright）".format(
                    type(exc).__name__, exc)
            )

        try:
            try:
                page.goto(url, wait_until="load", timeout=NAV_TIMEOUT_MS)
            except PWTimeout:
                C.warn(f"{NAV_TIMEOUT_MS // 1000} 秒内页面没 load 完，继续按已渲染的内容截图")

            try:
                page.wait_for_load_state("networkidle", timeout=NETWORKIDLE_TIMEOUT_MS)
            except PWTimeout:
                C.warn("页面没有进入 networkidle（可能有长轮询/埋点），继续截图")

            if args.wait > 0:
                page.wait_for_timeout(args.wait * 1000)

            # ① --login：先登录，再跑 --eval / --hide / 截图（V2）。
            # 过去先跑 --eval 再等登录：--eval 在登录页上执行必然抛错，收尾把它判成
            # exit 1，而截出来又是登录后页面，看起来像成功。登录完成后重新 goto 一次
            # 目标页：登录流程可能跳到别的 URL，登录后的页面也值得重新等 networkidle。
            if args.login:
                print("")
                print(">>> 浏览器窗口已打开，请手动登录。登录完成后回到这里按回车继续截图。")
                try:
                    input()
                except (EOFError, KeyboardInterrupt):
                    C.warn("没读到回车，直接继续")
                C.log("登录完成，重新打开目标页（带上登录态）…")
                try:
                    page.goto(url, wait_until="load", timeout=NAV_TIMEOUT_MS)
                except PWTimeout:
                    C.warn(f"{NAV_TIMEOUT_MS // 1000} 秒内页面没 load 完，继续按已渲染的内容截图")
                try:
                    page.wait_for_load_state("networkidle", timeout=NETWORKIDLE_TIMEOUT_MS)
                except PWTimeout:
                    C.warn("页面没有进入 networkidle（可能有长轮询/埋点），继续截图")
                if args.wait > 0:
                    page.wait_for_timeout(args.wait * 1000)

            # ② 懒加载触发（--login 时是登录后的页面，滚到的才是真实内容）
            try:
                height = page.evaluate(LAZY_LOAD_JS)
                C.log(f"OK: 已触发懒加载（页面总高约 {height}px）")
            except Exception as exc:
                C.warn(f"懒加载脚本执行失败（继续截图）：{type(exc).__name__}: {exc}")

            # ③ 用户自定义 JS（--login 时登录后执行，登录页上执行必然失败）
            for js in evals:
                try:
                    page.evaluate(js)
                    C.log(f"OK: 已执行 --eval {js[:60]}{'...' if len(js) > 60 else ''}")
                except Exception as exc:
                    msg = f"--eval 执行失败：{js[:80]} -> {type(exc).__name__}: {exc}"
                    C.warn(msg)
                    eval_failures.append(msg)

            # ④ 隐藏元素
            def apply_hides() -> None:
                if hides:
                    result = page.evaluate(HIDE_JS, hides)
                    for item in result:
                        if "0 个匹配" in item or "非法" in item:
                            C.warn(f"--hide {item}")
                        else:
                            C.log(f"OK: 已隐藏 --hide {item}")
                if not args.keep_avatar:
                    n = page.evaluate(AVATAR_JS, [AVATAR_SELECTORS, AVATAR_ZONE[0], AVATAR_ZONE[1]])
                    if n:
                        C.log(f"OK: 已隐藏 {n} 个右上角头像元素（--keep-avatar 可保留）")

            apply_hides()

            def shoot(suffix: str, label: str) -> None:
                path = out_dir / f"{args.name}-{suffix}.jpg"
                page.screenshot(path=str(path), type="jpeg", quality=args.quality)
                shots.append(path)
                if not _report(path, factor, min_width, args.device):
                    nonlocal too_narrow
                    too_narrow += 1

            # ⑤ --at 逐张
            for idx, y in enumerate(at_list, start=1):
                _scroll_to(page, y)
                page.wait_for_timeout(SHOT_SETTLE * 1000)
                apply_hides()
                shoot(str(idx), f"y={y:.0f}")

            # ⑥ --full 长图
            if args.full is not None:
                full_h = int(round(args.full))
                if full_h < viewport[1]:
                    C.warn(f"--full {full_h} 比视口高 {viewport[1]} 还小，等于没截全；建议给 3000 以上")
                page.evaluate("() => window.scrollTo(0, 0)")
                page.set_viewport_size({"width": viewport[0], "height": full_h})
                page.wait_for_timeout(SHOT_SETTLE * 1000)
                apply_hides()
                shoot("full", "整页")
                page.set_viewport_size({"width": viewport[0], "height": viewport[1]})

            # ⑦ 都没指定 -> 首屏一张
            if not at_list and args.full is None:
                page.evaluate("() => window.scrollTo(0, 0)")
                page.wait_for_timeout(SHOT_SETTLE * 1000)
                apply_hides()
                shoot("1", "首屏")

        except Exception as exc:
            C.error(f"截图过程出错：{type(exc).__name__}: {exc}")
        finally:
            try:
                if browser is not None:
                    browser.close()
                elif ctx is not None:
                    ctx.close()
            except Exception:
                pass

    if not shots:
        C.error("一张截图都没生成")

    C.log("")
    C.log(f"完成：{len(shots)} 张 -> {C.rel(out_dir)}")
    if too_narrow:
        C.warn(f"其中 {too_narrow} 张宽度低于该设备预设的清晰度下限 {min_width}px，放大后会糊（见上面的 WARN）")
    if args.login or profile_dir.exists():
        C.log(f"登录态目录：{C.rel(profile_dir)}（下次直接跑同一条命令即可复用，不用再登）")
    C.log(f"下一步：python scripts/media.py info {C.rel(out_dir)}    # 自查清晰度")
    if eval_failures:
        C.error(f"{len(eval_failures)} 个 --eval 脚本执行失败（截图已保留，但页面状态可能不是你要的）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
