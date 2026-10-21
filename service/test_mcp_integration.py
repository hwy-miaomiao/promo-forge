#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""MCP stdio server 集成测试（零第三方依赖，CI 同款）。

不联网、不渲染、不需要 ffmpeg：临时造一个「假项目」（timing.json + 假成片 +
1080×1920 PNG 头封面 + 字幕 + 发布文案 + 决策账本），然后用 Popen 拉起
``mcp_server.py`` 子进程，真正走一遍 initialize / tools.list / tools.call /
错误码分支，验证服务化入口能被子进程环境安全调用。

跑法：``python service/test_mcp_integration.py``（退出码 0 = 全部通过）。
"""

from __future__ import annotations

import json
import os
import struct
import subprocess
import sys
import tempfile
import zlib
from pathlib import Path
from typing import Any, Dict, List, Optional

SERVICE_DIR = Path(__file__).resolve().parent
REPO_ROOT = SERVICE_DIR.parent
SCRIPTS_DIR = REPO_ROOT / "template" / "scripts"
SERVER = SERVICE_DIR / "mcp_server.py"


# ===========================================================================
# 测试脚手架
# ===========================================================================

class Client:
    """按行 JSON-RPC 的极简客户端（一请求一响应）。"""

    def __init__(self, proc: "subprocess.Popen") -> None:
        self.proc = proc
        self._next_id = 1

    def send_raw(self, raw: str) -> None:
        assert self.proc.stdin is not None
        self.proc.stdin.write(raw + "\n")
        self.proc.stdin.flush()

    def send(self, method: str, params: Optional[Dict[str, Any]] = None,
             notify: bool = False) -> Any:
        payload: Dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            payload["params"] = params
        msg_id = None
        if not notify:
            msg_id = self._next_id
            self._next_id += 1
            payload["id"] = msg_id
        self.send_raw(json.dumps(payload, ensure_ascii=False))
        return msg_id

    def recv(self) -> Dict[str, Any]:
        assert self.proc.stdout is not None
        line = self.proc.stdout.readline()
        if not line:
            stderr = ""
            if self.proc.stderr is not None:
                stderr = self.proc.stderr.read()
            raise RuntimeError("服务端没有响应就退出了；stderr=%s" % stderr[-2000:])
        return json.loads(line)

    def request(self, method: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        msg_id = self.send(method, params)
        response = self.recv()
        assert response.get("id") == msg_id, "响应 id 不匹配：%r" % (response,)
        return response

    def call_tool(self, name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        response = self.request("tools/call", {"name": name, "arguments": arguments})
        assert "error" not in response, "工具调用报协议错：%r" % (response,)
        return response["result"]


def _write_lf(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(str(path), "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)


def _png_header(path: Path, width: int, height: int) -> None:
    """写一个只有合法头部的 PNG（publish_check 只读 IHDR，不需要完整像素）。"""
    signature = b"\x89PNG\r\n\x1a\n"
    ihdr_data = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    ihdr = struct.pack(">I", len(ihdr_data)) + b"IHDR" + ihdr_data + \
        struct.pack(">I", zlib.crc32(b"IHDR" + ihdr_data) & 0xFFFFFFFF)
    iend = struct.pack(">I", 0) + b"IEND" + struct.pack(">I", zlib.crc32(b"IEND") & 0xFFFFFFFF)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(signature + ihdr + iend)


def _minimal_mp4() -> bytes:
    """最小合法 MP4 容器（ftyp + 空 moov），只满足发布闸门的盒结构校验。"""
    ftyp_body = b"isom" + (0x200).to_bytes(4, "big") + b"isomiso2mp41"
    ftyp = (8 + len(ftyp_body)).to_bytes(4, "big") + b"ftyp" + ftyp_body
    moov = (8).to_bytes(4, "big") + b"moov"
    return ftyp + moov


def _child_env() -> Dict[str, str]:
    """服务端子进程环境：强制 UTF-8，并把 PATH 置空。

    PATH 置空是刻意的：保证 publish_check 在集成测试里走「本机没有 ffprobe」
    的降级路径（容器结构仍然硬校验），测试结果不依赖跑测试的机器装没装 ffmpeg。
    """
    env = dict(os.environ)
    env["PATH"] = ""
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    return env


def _timing() -> Dict[str, Any]:
    return {
        "slug": "mcp-fixture",
        "title": "一句话做宣传片",
        "fps": 60,
        "width": 1080,
        "height": 1920,
        "aspect": "9:16",
        "duration": 45,
        "captions": {"bottom": 0.20},
        "music": {
            "file": "audio/music.wav",
            "source": "audio-src/music/track.mp3",
            "offset": 0.0,
            "beat": 0.5,
            "bar": 2.0,
            "drop": 3.6,
            "gain": 0.6,
            "segments": [],
        },
        "scenes": [
            {"id": "hook", "start": 0.0, "end": 4.5},
            {"id": "demo", "start": 4.5, "end": 26.0},
            {"id": "proof", "start": 26.0, "end": 36.0},
            {"id": "cta", "start": 36.0, "end": 45.0},
        ],
        "pace": {"minShot": 0.5, "keyShot": 1.2, "sceneMin": 3.0,
                 "stagger": 0.25, "holdAfterReveal": 0.6},
        "voice": {"engine": "edge", "voice": "zh-CN-YunxiNeural", "rate": "+8%"},
    }


def _make_project(root: Path) -> Path:
    project = root / "promo-fixture"
    _write_lf(project / "src" / "timing.json",
              json.dumps(_timing(), ensure_ascii=False, indent=2))
    (project / "out").mkdir(parents=True, exist_ok=True)
    (project / "out" / "final-1080p.mp4").write_bytes(_minimal_mp4())
    _png_header(project / "out" / "covers" / "cover-1.png", 1080, 1920)
    _write_lf(project / "out" / "subtitles" / "zh.srt",
              "1\n00:00:01,000 --> 00:00:03,000\n一句话做宣传片\n")
    _write_lf(project / "publish" / "copy.json", json.dumps({
        "title": "一句话做宣传片",
        "hashtags": ["独立开发", "效率工具"],
        "body": "把这个流程跑通，你也能一键出片。",
    }, ensure_ascii=False, indent=2))
    init = subprocess.run(
        [sys.executable, str(SCRIPTS_DIR / "audit.py"), "init",
         "--root", str(project)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        cwd=str(REPO_ROOT),
    )
    assert init.returncode == 0, "audit.py init 失败：%s" % (init.stderr,)
    # P3 之后「人拍板的需求确认」是必需项：init 之外再补一条 user_requirement。
    append = subprocess.run(
        [sys.executable, str(SCRIPTS_DIR / "audit.py"), "append",
         "--root", str(project), "--kind", "user_requirement",
         "--decision", "确认需求与风格（MCP 集成测试夹具）"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        cwd=str(REPO_ROOT),
    )
    assert append.returncode == 0, "audit.py append 失败：%s" % (append.stderr,)
    return project


def _tool_payload(result: Dict[str, Any]) -> Dict[str, Any]:
    """从 MCP 工具结果里取回结构化 payload。"""
    content = result.get("content") or []
    assert content and content[0].get("type") == "text", "工具结果缺少 text content"
    return json.loads(content[0]["text"])


# ===========================================================================
# 主流程
# ===========================================================================

def main() -> int:
    failures: List[str] = []
    checks = 0

    def check(name: str, got: Any, want: Any) -> None:
        nonlocal checks
        checks += 1
        if got != want:
            failures.append("%s：期望 %r，实际 %r" % (name, want, got))

    with tempfile.TemporaryDirectory(prefix="promo-mcp-it-") as tmpdir:
        project = _make_project(Path(tmpdir))
        proc = subprocess.Popen(
            [sys.executable, str(SERVER)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            cwd=str(REPO_ROOT),
            env=_child_env(),
        )
        try:
            client = Client(proc)

            # 1) 握手
            init = client.request("initialize", {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "promo-forge-ci", "version": "0.0.0"},
            })
            result = init.get("result") or {}
            check("initialize protocolVersion", result.get("protocolVersion"), "2024-11-05")
            check("initialize serverInfo", (result.get("serverInfo") or {}).get("name"),
                  "promo-forge-service")
            check("initialize capabilities.tools", "tools" in (result.get("capabilities") or {}), True)

            # 2) 通知不应有响应；发完 ping 确认管道没乱
            client.send("notifications/initialized", notify=True)
            ping = client.request("ping", {})
            check("ping", ping.get("result"), {})

            # 3) tools/list
            listed = client.request("tools/list", {})["result"]["tools"]
            names = [tool["name"] for tool in listed]
            check("tools/list 数量", len(names), 5)
            check("tools/list 名单", sorted(names), sorted([
                "check_assets", "check_video", "music_analyze",
                "publish_check", "audit_validate",
            ]))
            for tool in listed:
                check("%s 有 inputSchema" % tool["name"],
                      tool["inputSchema"].get("type"), "object")

            # 4) audit_validate：init 出来的零付费账本应通过
            audit = client.call_tool("audit_validate", {"project_root": str(project)})
            audit_payload = _tool_payload(audit)
            check("audit_validate exitCode", audit_payload["exitCode"], 0)
            check("audit_validate isError", audit["isError"], False)
            check("audit_validate ok", (audit_payload.get("structured") or {}).get("ok"), True)

            # 5) publish_check：完整假项目应可发布
            publish = client.call_tool("publish_check", {"project_root": str(project)})
            publish_payload = _tool_payload(publish)
            check("publish_check exitCode", publish_payload["exitCode"], 0)
            check("publish_check isError", publish["isError"], False)
            structured = publish_payload.get("structured") or {}
            check("publish_check publishable", structured.get("publishable"), True)
            check("publish_check 平台数", len(structured.get("platforms") or {}), 4)
            check("publish_check 不写盘", (project / "publish" / "manifest.json").is_file(), False)

            # 5b) P1 回归：把成片换成文本 → 容器校验必须 ERROR（不能只看 size>0）
            video_path = project / "out" / "final-1080p.mp4"
            video_path.write_text("this is not an mp4, just text", encoding="utf-8")
            try:
                bad_publish = client.call_tool("publish_check", {"project_root": str(project)})
            finally:
                video_path.write_bytes(_minimal_mp4())
            bad_payload = _tool_payload(bad_publish)
            bad_structured = bad_payload.get("structured") or {}
            check("文本假 MP4 → publish_check exitCode", bad_payload["exitCode"], 1)
            check("文本假 MP4 → publish_check isError", bad_publish["isError"], True)
            check("文本假 MP4 → 不可发布", bad_structured.get("publishable"), False)
            bad_results = bad_structured.get("results") or []
            check("文本假 MP4 → 成片容器 ERROR",
                  any(row.get("level") == "ERROR" and row.get("item") == "成片容器"
                      for row in bad_results), True)

            # 6) check_assets：假项目素材不全，应是 ERROR（退出码 1）
            assets = client.call_tool("check_assets", {"project_root": str(project)})
            assets_payload = _tool_payload(assets)
            check("check_assets exitCode", assets_payload["exitCode"], 1)
            check("check_assets isError", assets["isError"], True)

            # 7) check_video：假成片 / 本机无 ffmpeg，应干净失败而不是崩溃
            video = client.call_tool("check_video", {"project_root": str(project)})
            video_payload = _tool_payload(video)
            check("check_video isError", video["isError"], True)
            check("check_video 有退出码", isinstance(video_payload["exitCode"], int), True)

            # 8) music_analyze：缺文件应干净失败
            music = client.call_tool("music_analyze",
                                     {"project_root": str(project),
                                      "audio": "audio-src/music/nope.mp3"})
            music_payload = _tool_payload(music)
            check("music_analyze exitCode", music_payload["exitCode"], 1)
            check("music_analyze isError", music["isError"], True)

            # 9) 协议错误分支
            unknown_tool = client.request("tools/call",
                                          {"name": "nope", "arguments": {}})
            check("未知工具 -32602", unknown_tool["error"]["code"], -32602)
            unknown_method = client.request("nope/nothing", {})
            check("未知方法 -32601", unknown_method["error"]["code"], -32601)
            # P6：带 id 的 notifications/* 必须收到 -32601，不能静默丢包导致客户端挂起
            notif_with_id = client.request("notifications/wat", {})
            check("带 id 通知 -32601", notif_with_id["error"]["code"], -32601)
            missing_root = client.request("tools/call",
                                          {"name": "check_assets", "arguments": {}})
            check("缺 project_root -32602", missing_root["error"]["code"], -32602)

            # 10) 坏 JSON → -32700（id 为 null）
            client.send_raw("{not json")
            parse_error = client.recv()
            check("坏 JSON -32700", parse_error["error"]["code"], -32700)
            check("坏 JSON id 为 null", parse_error.get("id"), None)
        finally:
            try:
                proc.terminate()
                proc.wait(timeout=10)
            except Exception:  # noqa: BLE001 - 清理失败不影响判定
                proc.kill()

    if failures:
        for message in failures:
            sys.stderr.write("ERROR: %s\n" % message)
        sys.stderr.write("ERROR: MCP 集成测试 %d/%d 项失败\n" % (len(failures), checks))
        return 1
    print("OK: MCP 集成测试通过（%d 项：initialize / 通知 / tools.list / "
          "audit_validate / publish_check（含 MP4 容器回归） / check_assets / "
          "check_video / music_analyze / 错误码 / 带 id 通知 -32601 / 坏 JSON）" % checks)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
