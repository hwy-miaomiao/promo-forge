#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""promo-forge 脚本自测总入口（AGENTS.md 第 7 节闸门之一）。

跑法（仓库根或 template/ 下都行，任意 Python ≥ 3.9）：

    python template/scripts/selftest.py
    python scripts/selftest.py

只测脚本里**纯函数级**的逻辑：注释剥离、日志强信号判定、时间/响度解析、
折行、版本解析、缓存键……不读素材、不跑 ffmpeg、不联网、不需要 node，
因此可以放心放进 CI 的 Python 矩阵里。

每个脚本自己的 ``--selftest`` 是唯一事实来源；本文件只负责逐个跑、
汇总、把失败原样带出来（有失败就非零退出）。
"""
from __future__ import annotations

import ast
import os
import subprocess
import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent

#: 带 --selftest 的脚本，按固定顺序跑（输出稳定，方便对照）
TARGETS = (
    "check_assets.py",
    "check_video.py",
    "doctor.py",
)

#: Python 3.9 写盘 smoke：``Path.write_text(newline=...)`` 是 3.10+ 的参数，而降级测试
#: 抓不到「只在写文件时才炸」的 TypeError（subtitles.py 曾因此在 3.9 下写 SRT/ASS 崩）。
#: 这里在 Python 3.9 下真的导一次 subtitles 并写一个 SRT，确保写盘路径被覆盖。
WRITE_SMOKE_CODE = r'''
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, ".")
import subtitles  # noqa: E402  （模块级导入即可；不渲染、不联网）

cues = [SimpleNamespace(lines=["第一行", "second line"], start=0.0, end=1.25)]
with tempfile.TemporaryDirectory() as td:
    srt = Path(td) / "nested" / "a.srt"
    subtitles._write_srt(cues, srt)
    data = srt.read_bytes()
    assert b"\r\n" in data, "SRT 应使用 CRLF"
    assert "第一行".encode("utf-8") in data, "SRT 内容缺中文"
print("write-smoke OK：_write_srt 在 Python %d.%d 下写盘成功" % sys.version_info[:2])
'''

#: 静态兜底：仓库脚本里不许出现 ``.write_text(..., newline=...)``（3.10+ 参数）。
#: 用 AST 扫真正的调用（不扫字符串/注释），避免把「解释这条限制的文档」当成违规。
BANNED_WRITE_METHOD = "write_text"
BANNED_WRITE_KEYWORD = "newline"


def _run_one(path: Path) -> tuple:
    """跑一个脚本的 --selftest，返回 (退出码, 合并后的输出)。"""
    env = os.environ.copy()
    # 子进程输出统一 UTF-8，避免 Windows 控制台代码页把中文搞成乱码
    env["PYTHONIOENCODING"] = "utf-8"
    try:
        proc = subprocess.run(
            [sys.executable, str(path), "--selftest"],
            cwd=str(SCRIPTS_DIR),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=120,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 1, f"跑不起来：{exc}"
    text = (proc.stdout or b"").decode("utf-8", errors="replace")
    return proc.returncode, text.strip()


def _force_utf8() -> None:
    """和仓库其它脚本一样：输出固定 UTF-8，避免 Windows 控制台代码页乱码。"""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):
                pass


def _run_write_smoke() -> tuple:
    """在**当前解释器**下真的导 subtitles 并写一个 SRT，返回 (退出码, 输出)。

    Python 3.12 跑只证明语法对；CI 的 3.9 格跑它才会暴露 ``write_text(newline=)``
    这类 3.10+ 参数。所以这一步和 ``--selftest`` 一样是「跑在 CI 的 Python 矩阵里」的。
    """
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    try:
        proc = subprocess.run(
            [sys.executable, "-c", WRITE_SMOKE_CODE],
            cwd=str(SCRIPTS_DIR),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=120,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 1, f"跑不起来：{exc}"
    return proc.returncode, (proc.stdout or b"").decode("utf-8", errors="replace").strip()


def _check_write_api() -> tuple:
    """AST 扫 scripts/*.py：出现 ``x.write_text(..., newline=...)`` 即失败。"""
    hits = []
    checked = 0
    for path in sorted(SCRIPTS_DIR.glob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (OSError, SyntaxError) as exc:
            return 1, f"读不了/解析不了 {path.name}：{exc}"
        checked += 1
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not (isinstance(func, ast.Attribute) and func.attr == BANNED_WRITE_METHOD):
                continue
            if any(kw.arg == BANNED_WRITE_KEYWORD for kw in node.keywords):
                hits.append(f"{path.name}:{node.lineno}: .write_text(..., newline=)")
    for hit in hits:
        print(f"    {hit}")
    if hits:
        return 1, (f"发现 {len(hits)} 处 3.10+ 写法 `.write_text(..., newline=)`；"
                  "Python 3.9 会抛 TypeError，改用 open(..., newline=)")
    print(f"    OK：扫了 {checked} 个脚本，0 处 3.10+ 写盘参数")
    return 0, ""


def main(argv=None) -> int:
    _force_utf8()
    args = list(sys.argv[1:] if argv is None else argv)
    if args and args[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    unknown = [arg for arg in args if arg not in ("-h", "--help")]
    if unknown:
        sys.stderr.write(f"ERROR: 不认识这些参数：{' '.join(unknown)}（本脚本没有参数）\n")
        return 1

    print(f"promo-forge 脚本自测：{SCRIPTS_DIR}")
    print()
    failed = []
    for index, name in enumerate(TARGETS, start=1):
        path = SCRIPTS_DIR / name
        if not path.is_file():
            print(f"[{index}/{len(TARGETS)}] {name} — ERROR: 文件不存在")
            failed.append(name)
            continue
        code, output = _run_one(path)
        status = "OK" if code == 0 else f"FAILED（退出码 {code}）"
        print(f"[{index}/{len(TARGETS)}] {name} --selftest — {status}")
        for line in output.splitlines():
            print(f"    {line}")
        print()
        if code != 0:
            failed.append(name)

    total = len(TARGETS)
    passed = total - len(failed)
    checks = (
        ("Python 3.9 写盘 smoke（subtitles._write_srt）", _run_write_smoke),
        ("Python 3.9 兼容静态兜底（scripts/*.py 无 write_text(newline=)）", _check_write_api),
    )
    total += len(checks)
    for index, (name, func) in enumerate(checks, start=len(TARGETS) + 1):
        code, output = func()
        status = "OK" if code == 0 else f"FAILED（退出码 {code}）"
        print(f"[{index}/{total}] {name} — {status}")
        for line in output.splitlines():
            print(f"    {line}")
        print()
        if code == 0:
            passed += 1
        else:
            failed.append(name)

    if failed:
        sys.stderr.write(
            f"ERROR: 脚本自测 {passed}/{total} 通过，失败：{'、'.join(failed)}\n"
        )
        return 1
    print(f"脚本自测全部通过：{passed}/{total}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
