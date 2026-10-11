#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""promo-forge 公共库：项目根定位、timing.json 读取与校验、日志、ffmpeg/ffprobe 封装、常用小工具。

用途
    ``scripts/`` 下所有脚本共用的底座。其它脚本这样接入::

        import sys
        from pathlib import Path
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import _common as C

        timing = C.load_timing()
        start, end = C.scene_window(timing, "hook")

典型调用
    python -c "import sys; sys.path.insert(0, 'scripts'); import _common as C; print(C.PROJECT_ROOT)"
    （正常由 doctor.py / capture.py / media.py / music.py 间接调用，不单独执行）

依赖
    仅标准库。ffmpeg / ffprobe 只在调用 ``ffmpeg()`` / ``ffprobe_json()`` 时才需要。

退出码
    ``error()`` 打印 ``ERROR: ...`` 到 stderr 并 ``sys.exit(code)``（默认 1）；
    其余函数只抛异常（``ValueError`` / ``KeyError`` 等），由调用方决定是否转成 ERROR 退出。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

# ---------------------------------------------------------------------------
# 常量（阈值集中在此，注释标注来源）
# ---------------------------------------------------------------------------

#: 覆盖项目根目录的环境变量（测试 / 非标准布局用；正常不用管）
ENV_ROOT = "PROMO_ROOT"
#: timing.json 相对项目根的路径（AGENTS.md 第 5 节：整支片子唯一事实来源）
TIMING_REL = Path("src") / "timing.json"
#: 字体角色表相对项目根的路径
FONTS_REL = Path("fonts.json")

#: 旁白句尾留白秒数。来源：本仓库脚本契约 `_common.line_window()`；
#: 对应 AGENTS.md 阶段 7「窗口 = 该场景可用时长」，留一点尾巴让 TTS 收尾不压到下一句。
LINE_TAIL_GAP = 0.15
#: duration 与最后一个场景 end 的允许误差。来源：AGENTS.md 第 7 节闸门表
#: （check_video.py 规格与 timing.json 一致 ±0.2 秒；这里对 timing.json 自身取更严的 ±0.05）
DURATION_TOL = 0.05
#: 场景首尾相接的浮点容差。来源：AGENTS.md 第 5 节「必须按 start 升序、首尾相接、不留缝」
SCENE_TOL = 0.01
#: 子进程失败时回显的 stderr 行数（够 AI 看懂原因，又不至于刷屏）
ERROR_TAIL_LINES = 12

FFMPEG_INSTALL_LINES = (
    "  Windows: winget install Gyan.FFmpeg\n"
    "  macOS:   brew install ffmpeg\n"
    "  Linux:   sudo apt install ffmpeg"
)
FFMPEG_HINT = "缺少 ffmpeg（AGENTS.md 第 6 节的硬依赖）。安装：\n" + FFMPEG_INSTALL_LINES
FFPROBE_HINT = "缺少 ffprobe（它随 ffmpeg 一起安装，装上 ffmpeg 就有了）：\n" + FFMPEG_INSTALL_LINES


# ---------------------------------------------------------------------------
# UTF-8 兜底：Windows 控制台默认 GBK，中文与 emoji 会直接抛 UnicodeEncodeError
# ---------------------------------------------------------------------------

def _force_utf8() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except Exception:
            pass


_force_utf8()


def _detect_project_root() -> Path:
    env = os.environ.get(ENV_ROOT)
    if env:
        return Path(env).expanduser().resolve()
    # scripts/ 的上级目录 = 项目根
    return Path(__file__).resolve().parent.parent


#: 项目根目录（含 src/timing.json 与 scripts/ 的那一层）
PROJECT_ROOT: Path = _detect_project_root()
#: <PROJECT_ROOT>/src/timing.json
TIMING_PATH: Path = PROJECT_ROOT / TIMING_REL
#: <PROJECT_ROOT>/fonts.json
FONTS_PATH: Path = PROJECT_ROOT / FONTS_REL


def rel(path: Any) -> str:
    """转成相对项目根的展示路径（不在项目内则原样返回绝对路径）。"""
    p = Path(path)
    try:
        return p.resolve().relative_to(PROJECT_ROOT).as_posix()
    except (ValueError, OSError):
        return str(p)


# ---------------------------------------------------------------------------
# 日志：log 普通信息 / warn 可继续的问题 / error 致命问题（非零退出）
# ---------------------------------------------------------------------------

def _emit(prefix: str, msg: Any, stream: Any) -> None:
    text = str(msg)
    lines = text.splitlines() or [""]
    pad = " " * len(prefix)
    print(prefix + lines[0], file=stream)
    for line in lines[1:]:
        print(pad + line, file=stream)
    try:
        stream.flush()
    except Exception:
        pass


def log(msg: Any) -> None:
    """打印人类可读的普通信息（stdout）。"""
    _emit("", msg, sys.stdout)


def warn(msg: Any) -> None:
    """打印 WARN: ...（能继续，但需要人知道）。"""
    _emit("WARN: ", msg, sys.stdout)


def error(msg: Any, code: int = 1) -> None:
    """打印 ERROR: ... 并退出。code=0 时只打印不退出（个别脚本需要）。"""
    _emit("ERROR: ", msg, sys.stderr)
    if code:
        sys.exit(code)


# ---------------------------------------------------------------------------
# timing.json
# ---------------------------------------------------------------------------

def load_json(path: Path) -> Any:
    """读 JSON；失败抛 ValueError（调用方自己决定报错方式）。"""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ValueError(f"读不到 {rel(path)}：{exc}") from exc
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{rel(path)} 不是合法 JSON：{exc}") from exc


def load_timing() -> Dict[str, Any]:
    """读 <PROJECT_ROOT>/src/timing.json；缺失或非法 JSON -> ERROR + exit(1)。"""
    try:
        data = load_json(TIMING_PATH)
    except ValueError as exc:
        error(f"{exc}\n（timing.json 是整支片子的唯一事实来源，见 AGENTS.md 第 5 节）")
    if not isinstance(data, dict):
        error(f"{rel(TIMING_PATH)} 的顶层必须是 JSON 对象，实际是 {type(data).__name__}")
    return data


def all_scenes(timing: Dict[str, Any]) -> List[Dict[str, Any]]:
    scenes = timing.get("scenes")
    if not isinstance(scenes, list) or not scenes:
        raise ValueError("timing.json 里没有 scenes（至少要有 1 个场景）")
    return [s for s in scenes if isinstance(s, dict)]


def scene_of(timing: Dict[str, Any], name: str) -> Dict[str, Any]:
    """按名字取场景 -> {"name", "start", "end", "note"}；找不到抛 ValueError。"""
    for s in all_scenes(timing):
        if s.get("name") == name:
            return {
                "name": name,
                "start": float(s.get("start", 0.0)),
                "end": float(s.get("end", 0.0)),
                "note": str(s.get("note", "")),
            }
    known = "、".join(str(s.get("name")) for s in all_scenes(timing))
    raise ValueError(f"timing.json 里没有名为 {name!r} 的场景（现有：{known}）")


def scene_window(timing: Dict[str, Any], name: str) -> Tuple[float, float]:
    """场景的 [start, end]（秒）。"""
    s = scene_of(timing, name)
    return s["start"], s["end"]


def _voice_lines(timing: Dict[str, Any]) -> List[Dict[str, Any]]:
    voice = timing.get("voice") or {}
    lines = voice.get("lines") if isinstance(voice, dict) else None
    if not isinstance(lines, list):
        return []
    return [dict(x) for x in lines if isinstance(x, dict)]


def line_window(timing: Dict[str, Any], line: Dict[str, Any]) -> Tuple[float, float]:
    """该句旁白可用的 [start, end]（秒）。

    规则（脚本契约）：start = line["at"]；
    end = min(所在场景 end, 下一句 at) - LINE_TAIL_GAP；
    end <= start 时抛 ValueError（调用方转成 ERROR 行）。
    """
    if not isinstance(line, dict) or "at" not in line:
        raise ValueError(f"旁白句缺少 at 字段：{line!r}")
    start = float(line["at"])

    scene_name = line.get("scene")
    if scene_name:
        _, scene_end = scene_window(timing, str(scene_name))
    else:
        scene_end = float(timing.get("duration") or 0.0)

    # 找“下一句”：按 (at, 原始顺序) 排序后紧跟其后的那句，且 at 必须严格更大
    raw = _voice_lines(timing)
    order = sorted(range(len(raw)), key=lambda i: (float(raw[i].get("at", 0.0)), i))
    pos: Optional[int] = None
    for i, obj in enumerate(raw):
        if obj == line:
            pos = i
            break
    if pos is None and line.get("id") is not None:
        for i, obj in enumerate(raw):
            if obj.get("id") == line.get("id"):
                pos = i
                break
    if pos is None and raw:
        pos = min(range(len(raw)), key=lambda i: abs(float(raw[i].get("at", 0.0)) - start))

    next_at: Optional[float] = None
    if pos is not None:
        rank = order.index(pos)
        for other in order[rank + 1:]:
            at = float(raw[other].get("at", 0.0))
            if at > start + 1e-9:
                next_at = at
                break

    end = scene_end if next_at is None else min(scene_end, next_at)
    end -= LINE_TAIL_GAP
    if end <= start:
        limit = next_at if next_at is not None else scene_end
        raise ValueError(
            f"旁白窗口不足：at={fmt_sec(start)}，场景 {scene_name!r} 结束于 {fmt_sec(scene_end)}，"
            f"下一句在 {fmt_sec(limit) if limit is not None else '∞'}，"
            f"扣掉 {LINE_TAIL_GAP}s 尾巴后只剩 {fmt_sec(end - start)}。"
            "（AGENTS.md 阶段 7：字数超窗口要改稿，不要硬压）"
        )
    return start, end


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------

def fmt_sec(s: Any) -> str:
    """4.2 -> "4.20s"。"""
    try:
        return f"{float(s):.2f}s"
    except (TypeError, ValueError):
        return f"{s}s"


def human(size_bytes: Any) -> str:
    """字节数转人类可读：1536 -> "1.5 KB"。"""
    try:
        n = float(size_bytes)
    except (TypeError, ValueError):
        return str(size_bytes)
    sign = "-" if n < 0 else ""
    n = abs(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            if unit == "B":
                return f"{sign}{int(n)} B"
            return f"{sign}{n:.1f} {unit}"
        n /= 1024.0
    return f"{sign}{n:.1f} TB"


def ensure_dir(path: Any) -> Path:
    p = Path(path)
    p.mkdir(parents=True, exist_ok=True)
    return p


def _resolve_exe(name: str) -> Optional[str]:
    """把命令名解析成可执行文件路径；找不到返回 None。"""
    raw = str(name)
    if os.sep in raw or "/" in raw or (os.altsep and os.altsep in raw):
        p = Path(raw).expanduser()
        return str(p) if p.is_file() else None
    return shutil.which(raw)


def has_cmd(name: str) -> bool:
    """命令是否可用（跨平台，Windows 下能认出 .cmd/.bat）。"""
    return _resolve_exe(name) is not None


def _batch_prefix(exe: str) -> List[str]:
    """Windows 上 .cmd/.bat（如 npm.cmd）不能直接 CreateProcess，需要套一层 cmd /c。"""
    if os.name == "nt" and exe.lower().endswith((".cmd", ".bat")):
        return [os.environ.get("COMSPEC", "cmd.exe"), "/c"]
    return []


def _fmt_cmd(argv: Sequence[str]) -> str:
    out = []
    for a in argv:
        out.append(f'"{a}"' if (" " in a and not a.startswith('"')) else a)
    return " ".join(out)


def run(
    cmd: Sequence[Any],
    check: bool = True,
    capture: bool = True,
    cwd: Any = None,
    timeout: Optional[float] = None,
    env: Optional[Dict[str, str]] = None,
) -> subprocess.CompletedProcess:
    """跑一条外部命令。

    check=True（默认）：命令不存在 / 超时 / 退出码非 0 -> 打印 ERROR 并 exit(1)。
    check=False：命令不存在 -> 抛 FileNotFoundError；失败 -> 原样返回 CompletedProcess。
    capture=True（默认）：stdout/stderr 收进返回值（UTF-8 解码，坏字节替换）；False 则直接透传到终端。
    """
    argv = [str(c) for c in cmd]
    if not argv:
        raise ValueError("run() 收到空命令")
    exe = _resolve_exe(argv[0])
    if exe is None:
        if check:
            error(f"找不到命令 {argv[0]!r}，请先安装并确认它在 PATH 里。")
        raise FileNotFoundError(f"找不到命令 {argv[0]!r}")
    argv[0] = exe
    full = _batch_prefix(exe) + argv
    try:
        cp = subprocess.run(
            full,
            check=False,
            capture_output=capture,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=str(cwd) if cwd else None,
            timeout=timeout,
            env=env,
        )
    except subprocess.TimeoutExpired:
        if check:
            error(f"命令超时（{timeout}s）：{_fmt_cmd(argv)}")
        raise
    except OSError as exc:
        if check:
            error(f"执行命令失败：{_fmt_cmd(argv)}\n{exc}")
        raise
    if check and cp.returncode != 0:
        tail = ((cp.stderr or "") + (cp.stdout or "")).strip().splitlines()
        tail = tail[-ERROR_TAIL_LINES:]
        detail = "\n".join(tail) if tail else "（没有输出）"
        error(f"命令失败（退出码 {cp.returncode}）：{_fmt_cmd(argv)}\n{detail}")
    return cp


def cmd_version(name: str, *args: Any, timeout: float = 20.0) -> Optional[str]:
    """取命令的版本首行（用于 doctor 这类体检）；命令不存在或报错返回 None。"""
    if not has_cmd(name):
        return None
    try:
        cp = run([name, *args], check=False, timeout=timeout)
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
        return None
    if cp.returncode != 0:
        return None
    for line in (cp.stdout or "").splitlines():
        if line.strip():
            return line.strip()
    return None


# ---------------------------------------------------------------------------
# ffmpeg / ffprobe
# ---------------------------------------------------------------------------

def ffmpeg(*args: Any, check: bool = True) -> subprocess.CompletedProcess:
    """跑 ffmpeg（自动 -hide_banner -nostdin -loglevel error -y）。

    例：``ffmpeg("-i", src, "-ac", "1", "-ar", "16000", out)``
    缺 ffmpeg -> ERROR + exit(1)。
    """
    if not has_cmd("ffmpeg"):
        error(FFMPEG_HINT)
    cmd = ["ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "error", "-y", *[str(a) for a in args]]
    return run(cmd, check=check, capture=True)


def ffprobe_json(path: Any) -> Dict[str, Any]:
    """ffprobe -show_format -show_streams 的 JSON 结果。"""
    if not has_cmd("ffprobe"):
        error(FFPROBE_HINT)
    src = Path(path)
    if not src.exists():
        error(f"文件不存在：{src}")
    cp = run(
        ["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(src)],
        check=True,
    )
    text = cp.stdout or ""
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        error(f"ffprobe 输出不是合法 JSON：{exc}\n{text[:400]}")
    if not isinstance(data, dict):
        error("ffprobe 输出格式异常（顶层不是对象）")
    return data


def probe_duration(path: Any) -> Optional[float]:
    """媒体时长（秒）；探测不到返回 None。"""
    try:
        info = ffprobe_json(path)
    except SystemExit:
        return None
    fmt = info.get("format") or {}
    try:
        return float(fmt.get("duration"))
    except (TypeError, ValueError):
        for st in info.get("streams") or []:
            try:
                return float(st.get("duration"))
            except (TypeError, ValueError):
                continue
    return None


# ---------------------------------------------------------------------------
# fonts.json
# ---------------------------------------------------------------------------

DEFAULT_SYSTEM_FONT_CANDIDATES = [
    "C:/Windows/Fonts/msyh.ttc",
    "C:/Windows/Fonts/simhei.ttf",
    "/System/Library/Fonts/PingFang.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
]


def fonts_config() -> Dict[str, Any]:
    """读 fonts.json；缺失或非法只 WARN 并返回 {}（字体不是硬依赖）。"""
    if not FONTS_PATH.exists():
        warn(f"没找到 {rel(FONTS_PATH)}，字体检查跳过")
        return {}
    try:
        data = load_json(FONTS_PATH)
    except ValueError as exc:
        warn(f"{exc}，字体检查跳过")
        return {}
    return data if isinstance(data, dict) else {}


def system_font_candidates() -> List[str]:
    """fonts.json -> check.systemCandidates（缺项时退回内置清单）。"""
    cfg = fonts_config()
    check = cfg.get("check") if isinstance(cfg.get("check"), dict) else {}
    cands = check.get("systemCandidates")
    if isinstance(cands, list) and cands:
        return [str(c) for c in cands]
    return list(DEFAULT_SYSTEM_FONT_CANDIDATES)


def first_existing_font(candidates: Optional[Iterable[str]] = None) -> Optional[Path]:
    """遍历候选字体，返回第一个真实存在的文件；一个都没有返回 None。"""
    for item in candidates if candidates is not None else system_font_candidates():
        try:
            p = Path(str(item)).expanduser()
        except (OSError, ValueError):
            continue
        try:
            if p.is_file():
                return p
        except OSError:
            continue
    return None
