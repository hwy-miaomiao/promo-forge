#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""promo-forge 服务化入口：零依赖 MCP stdio server（融合项 C）。

把仓库里已有的确定性校验脚本包装成 MCP 工具，让 n8n / Claude Code / Cursor /
自建 agent 之类的调用方不用读 AGENTS.md，也能直接问「这版能不能发」。

协议
----
JSON-RPC 2.0 over stdio，**按行 framing**：stdin/stdout 每条消息一行 UTF-8
JSON（以 ``\\n`` 分隔），stdout 里不会出现日志行（子脚本的日志被捕获后塞进
工具结果），所以调用方可以安全地按行读响应。没有使用 Content-Length 头，
因为按行最简单、对 Windows / 管道 / 单行日志最友好。

方法
----
- ``initialize`` → protocolVersion / capabilities / serverInfo / instructions
- ``notifications/initialized`` → 无响应（通知）
- ``ping`` → ``{}``
- ``tools/list`` → 5 个工具的 JSON Schema
- ``tools/call`` → 执行脚本，返回 ``{content:[{type:"text",text}], isError}``
- 未知方法 → ``-32601``；坏 JSON → ``-32700``；坏参数/未知工具 → ``-32602``

工具（全部确定性、不联网；参数是路径，子进程会设置 PROMO_ROOT）
--------------------------------------------------------------
``check_assets``      渲染前素材/结构闸门（template/scripts/check_assets.py）
``check_video``       成片规格/响度/空档/日志/抽帧闸门（需要 ffmpeg）
``music_analyze``     配乐 BPM/beat/downbeat 分析（需要 numpy+scipy）
``publish_check``     四平台发布合规检查单（不发布、默认不写盘）
``audit_validate``    决策与成本账本校验（零付费默认通过）

退出码
------
0 = 正常处理到 EOF；1 = ``--selftest`` 失败；2 = 参数错误。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "template" / "scripts"
PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "promo-forge-service"
SERVER_VERSION = "0.1.0"
DEFAULT_TIMEOUT_S = 600
TAIL_LIMIT = 4000
SUMMARY_RE = re.compile(
    r"(?:检查完成|成片检查|交付预检|决策审计|发布合规检查)"
    r"[：:]\s*(\d+)\s*ERROR\s*/\s*(\d+)\s*WARN"
)


class RpcError(Exception):
    """JSON-RPC 错误（带 code/message/data），由 handle_message 转成错误响应。"""

    def __init__(self, code: int, message: str, data: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data


# ===========================================================================
# 子进程执行
# ===========================================================================

def _child_env(project_root: Optional[Path]) -> Dict[str, str]:
    """子进程环境：强制 UTF-8 输出；project_root 存在时注入 PROMO_ROOT。"""
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env.setdefault("PYTHONUTF8", "1")
    if project_root is not None:
        env["PROMO_ROOT"] = str(project_root)
    return env


def _run_script(
    script_name: str,
    argv: List[str],
    project_root: Optional[Path] = None,
    timeout: int = DEFAULT_TIMEOUT_S,
) -> Dict[str, Any]:
    """跑 template/scripts/<script>，捕获输出；不抛异常，状态都进返回值。"""
    script = SCRIPTS_DIR / script_name
    if not script.is_file():
        return {
            "exitCode": 127,
            "stdout": "",
            "stderr": "服务端找不到脚本 %s（安装不完整）" % script,
            "timedOut": False,
        }
    cmd = [sys.executable, str(script)] + argv
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(project_root) if project_root is not None else str(REPO_ROOT),
            env=_child_env(project_root),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
        return {
            "exitCode": proc.returncode,
            "stdout": proc.stdout or "",
            "stderr": proc.stderr or "",
            "timedOut": False,
        }
    except subprocess.TimeoutExpired as exc:
        return {
            "exitCode": 124,
            "stdout": (exc.stdout or "") if isinstance(exc.stdout, str) else "",
            "stderr": "超时（>%ds）：%s" % (timeout, " ".join(argv)),
            "timedOut": True,
        }
    except OSError as exc:
        return {
            "exitCode": 127,
            "stdout": "",
            "stderr": "启动子进程失败：%s" % exc,
            "timedOut": False,
        }


def _tail(text: str, limit: int = TAIL_LIMIT) -> str:
    if len(text) <= limit:
        return text
    return "…（前 %d 字符已截断）…\n" % (len(text) - limit) + text[-limit:]


def _extract_summary(text: str) -> Optional[Dict[str, int]]:
    match = SUMMARY_RE.search(text)
    if not match:
        return None
    return {"errors": int(match.group(1)), "warns": int(match.group(2))}


def _last_json_line(text: str) -> Optional[Dict[str, Any]]:
    """取 stdout 里最后一条能解析成对象的 JSON 行（--format json 的输出）。"""
    for line in reversed(text.splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            data = json.loads(line)
        except ValueError:
            continue
        if isinstance(data, dict):
            return data
    return None


# ===========================================================================
# 工具实现
# ===========================================================================

def _require_project_root(arguments: Dict[str, Any]) -> Path:
    raw = arguments.get("project_root")
    if not isinstance(raw, str) or not raw.strip():
        raise RpcError(-32602, "缺少参数 project_root（宣传片项目根目录，含 src/timing.json）")
    root = Path(raw).expanduser().resolve()
    if not root.is_dir():
        raise RpcError(-32602, "project_root 不是目录：%s" % root)
    return root


#: check_video.py 的 ``--expect`` 是**分辨率**（WxH），不是时长；这里和脚本同口径。
_RESOLUTION_RE = re.compile(r"^\d{3,5}[xX]\d{3,5}$")

#: publish_check.py 的 --platform 合法取值（与脚本 argparse choices 同源）。
_PLATFORMS = ("douyin", "kuaishou", "xiaohongshu", "shipinhao", "all")


def _safe_positional(value: str, label: str) -> str:
    """位置参数校验（R9-MCP-02）：以 ``-`` 开头会被 argparse 当成开关。

    以前 ``check_video`` 的 video 是 argv[0]：调用方传 ``video="--selftest"``，
    子进程直接跑自测并 exit 0 —— 什么都没查却返回 ``isError=false``，
    等于让「坏输入」静默通过闸门。这里显式拒绝开关形态的值。
    """
    if "\x00" in value:
        raise RpcError(-32602, "%s 含 NUL 字节，不是合法路径：%r" % (label, value))
    if value.lstrip().startswith("-"):
        raise RpcError(
            -32602,
            "%s 不能以 - 开头（会被命令行当成开关，让脚本跑别的东西）：%r" % (label, value),
        )
    return value


def _tool_result(tool: str, result: Dict[str, Any], structured: Dict[str, Any],
                 is_error: bool) -> Dict[str, Any]:
    payload = {
        "tool": tool,
        "exitCode": result["exitCode"],
        "timedOut": result["timedOut"],
        "stats": _extract_summary(result["stdout"]),
        "structured": structured,
        "stdoutTail": _tail(result["stdout"]),
        "stderrTail": _tail(result["stderr"]),
    }
    return {
        "content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}],
        "isError": bool(is_error),
    }


def _tool_check_assets(arguments: Dict[str, Any]) -> Dict[str, Any]:
    root = _require_project_root(arguments)
    argv = ["--strict"] if arguments.get("strict") else []
    result = _run_script("check_assets.py", argv, project_root=root)
    return _tool_result("check_assets", result, {}, result["exitCode"] != 0)


def _tool_check_video(arguments: Dict[str, Any]) -> Dict[str, Any]:
    root = _require_project_root(arguments)
    video = arguments.get("video")
    if isinstance(video, str) and video.strip():
        video_arg = _safe_positional(video, "video")
    else:
        video_arg = "out/final-1080p.mp4"
    # flag 全部放在位置参数之前；video 已做开关形态校验，双保险。
    argv = ["--no-log"] if arguments.get("no_log", True) else []
    expect = arguments.get("expect")
    if expect is not None:
        if not isinstance(expect, str) or not _RESOLUTION_RE.match(expect.strip()):
            # R9-MCP-01：以前 schema 把它写成「期望时长秒数(number)」，与 check_video.py
            # 的 --expect（分辨率 WxH，见 check_video.py 的 WxH 报错文案）完全不符，
            # 按 schema 传数字必然让工具报错。这里按脚本真实口径做参数校验。
            raise RpcError(
                -32602,
                "expect 必须是分辨率字符串 WxH，例如 1080x1920（check_video.py 的 "
                "--expect 不接收时长）：%r" % (expect,),
            )
        argv += ["--expect", expect.strip()]
    for key, flag in (("lufs_min", "--lufs-min"), ("lufs_max", "--lufs-max")):
        value = arguments.get(key)
        if value is not None:
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise RpcError(-32602, "%s 必须是数字（LUFS）：%r" % (key, value))
            argv += [flag, str(value)]
    argv.append(video_arg)
    result = _run_script("check_video.py", argv, project_root=root)
    return _tool_result("check_video", result, {}, result["exitCode"] != 0)


def _tool_music_analyze(arguments: Dict[str, Any]) -> Dict[str, Any]:
    root = _require_project_root(arguments)
    audio = arguments.get("audio")
    if not isinstance(audio, str) or not audio.strip():
        raise RpcError(-32602, "缺少参数 audio（音频文件路径，相对 project_root 或绝对路径）")
    # R9-MCP-02：audio 是 music.py 的位置参数，"--help"/"--selftest" 会被当成开关。
    audio = _safe_positional(audio, "audio")
    result = _run_script("music.py", ["analyze", "--", audio], project_root=root)
    return _tool_result("music_analyze", result, {}, result["exitCode"] != 0)


def _tool_publish_check(arguments: Dict[str, Any]) -> Dict[str, Any]:
    root = _require_project_root(arguments)
    argv = ["--project", str(root), "--format", "json", "--no-write"]
    for key, flag in (("video", "--video"), ("covers", "--covers"),
                      ("subtitles", "--subtitles"), ("copy", "--copy")):
        value = arguments.get(key)
        if isinstance(value, str) and value.strip():
            argv += [flag, _safe_positional(value, key)]
    for key, flag in (("title", "--title"), ("body", "--body")):
        value = arguments.get(key)
        if isinstance(value, str) and value.strip():
            argv += [flag, value]
    hashtags = arguments.get("hashtags")
    if isinstance(hashtags, list) and hashtags:
        argv += ["--hashtags", ",".join(str(tag) for tag in hashtags)]
    platform = arguments.get("platform")
    if isinstance(platform, str) and platform.strip():
        argv += ["--platform", platform]
    if arguments.get("strict"):
        argv.append("--strict")
    result = _run_script("publish_check.py", argv, project_root=root)
    structured = _last_json_line(result["stdout"]) or {}
    return _tool_result("publish_check", result, structured,
                        result["exitCode"] != 0)


def _tool_audit_validate(arguments: Dict[str, Any]) -> Dict[str, Any]:
    root = _require_project_root(arguments)
    argv = ["validate", "--root", str(root), "--format", "json"]
    budget = arguments.get("budget_cny")
    if budget is not None:
        argv += ["--budget", str(budget)]
    result = _run_script("audit.py", argv, project_root=root)
    structured = _last_json_line(result["stdout"]) or {}
    return _tool_result("audit_validate", result, structured,
                        result["exitCode"] != 0)


TOOL_SPECS: List[Dict[str, Any]] = [
    {
        "name": "check_assets",
        "description": "渲染前闸门：素材引用/真实截图配额/timing 结构/字体/音频等。返回退出码与 ERROR/WARN 统计。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_root": {"type": "string", "description": "宣传片项目根目录（含 src/timing.json）"},
                "strict": {"type": "boolean", "description": "WARN 也当 ERROR（默认 false）"},
            },
            "required": ["project_root"],
        },
        "handler": _tool_check_assets,
    },
    {
        "name": "check_video",
        "description": "成片闸门：规格/音轨/响度 -16~-12 LUFS/空档/渲染日志/抽帧联系表（需要 ffmpeg）。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_root": {"type": "string", "description": "宣传片项目根目录"},
                "video": {"type": "string", "description": "成片路径，默认 out/final-1080p.mp4"},
                "no_log": {"type": "boolean", "description": "不读 out/render.log（默认 true）"},
                "expect": {
                    "type": "string",
                    "pattern": "^[0-9]{3,5}[xX][0-9]{3,5}$",
                    "description": "期望分辨率 WxH（check_video.py 的 --expect 口径），例如 1080x1920；不接受时长",
                },
                "lufs_min": {"type": "number", "description": "响度下限，默认 -16"},
                "lufs_max": {"type": "number", "description": "响度上限，默认 -12"},
            },
            "required": ["project_root"],
        },
        "handler": _tool_check_video,
    },
    {
        "name": "music_analyze",
        "description": "配乐分析：时长/采样率/BPM/每秒能量/候选 drop（本地 numpy+scipy，不联网）。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_root": {"type": "string", "description": "宣传片项目根目录"},
                "audio": {"type": "string", "description": "音频路径（相对 project_root 或绝对路径）"},
            },
            "required": ["project_root", "audio"],
        },
        "handler": _tool_music_analyze,
    },
    {
        "name": "publish_check",
        "description": "发布合规检查单：四平台标题/话题/正文/时长/封面/安全区。只检查不发布，默认不写盘。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_root": {"type": "string", "description": "宣传片项目根目录"},
                "platform": {"type": "string", "description": "douyin/kuaishou/xiaohongshu/shipinhao/all"},
                "video": {"type": "string", "description": "成片路径，默认 out/final-1080p.mp4"},
                "covers": {"type": "string", "description": "封面目录，默认 out/covers"},
                "subtitles": {"type": "string", "description": "字幕路径，默认 out/subtitles/zh.srt"},
                "copy": {"type": "string", "description": "发布文案 JSON，默认 publish/copy.json"},
                "title": {"type": "string", "description": "标题（覆盖 copy.json / timing.title）"},
                "hashtags": {"type": "array", "items": {"type": "string"}, "description": "话题列表"},
                "body": {"type": "string", "description": "正文"},
                "strict": {"type": "boolean", "description": "WARN 也当 ERROR（默认 false）"},
            },
            "required": ["project_root"],
        },
        "handler": _tool_publish_check,
    },
    {
        "name": "audit_validate",
        "description": "决策与成本账本校验：结构/人拍板三类事/付费记账/预算上限（零付费默认通过）。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_root": {"type": "string", "description": "宣传片项目根目录"},
                "budget_cny": {"type": "number", "description": "临时覆盖预算上限（元）"},
            },
            "required": ["project_root"],
        },
        "handler": _tool_audit_validate,
    },
]

TOOLS_BY_NAME = {spec["name"]: spec for spec in TOOL_SPECS}


def _tool_definitions() -> List[Dict[str, Any]]:
    return [
        {
            "name": spec["name"],
            "description": spec["description"],
            "inputSchema": spec["inputSchema"],
        }
        for spec in TOOL_SPECS
    ]


# ===========================================================================
# JSON-RPC 分发
# ===========================================================================

def dispatch(method: str, params: Dict[str, Any]) -> Any:
    """执行方法，返回 result；通知返回 None；错误抛 RpcError。"""
    if method == "initialize":
        return {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            "instructions": (
                "promo-forge 服务化入口：把竖屏宣传片的确定性校验脚本包成 MCP 工具。"
                "先跑 audit_validate 与 check_assets，渲染后跑 check_video 与 publish_check；"
                "所有工具只读检查，不会自动发布、不会调用付费服务。"
            ),
        }
    if method == "ping":
        return {}
    if method in ("notifications/initialized", "initialized"):
        return None
    if method == "tools/list":
        return {"tools": _tool_definitions()}
    if method == "tools/call":
        name = params.get("name")
        if not isinstance(name, str) or name not in TOOLS_BY_NAME:
            raise RpcError(-32602, "未知工具：%r" % (name,))
        arguments = params.get("arguments")
        if arguments is None:
            arguments = {}
        if not isinstance(arguments, dict):
            raise RpcError(-32602, "arguments 必须是对象")
        return TOOLS_BY_NAME[name]["handler"](arguments)
    if method.startswith("notifications/"):
        return None
    raise RpcError(-32601, "Method not found: %s" % method)


def handle_message(message: Any) -> Optional[Dict[str, Any]]:
    """处理一条 JSON-RPC 消息，返回响应 dict 或 None（通知）。"""
    if not isinstance(message, dict):
        return {
            "jsonrpc": "2.0", "id": None,
            "error": {"code": -32600, "message": "Invalid Request：消息必须是 JSON 对象"},
        }
    msg_id = message.get("id")
    is_notification = "id" not in message
    method = message.get("method")
    if not isinstance(method, str):
        if is_notification:
            return None
        return {
            "jsonrpc": "2.0", "id": msg_id,
            "error": {"code": -32600, "message": "Invalid Request：缺少 method"},
        }
    params = message.get("params")
    if params is None:
        params = {}
    if not isinstance(params, dict):
        # JSON-RPC：通知（无 id）即使参数坏了也不能回响应，否则客户端会收到一条
        # 无人认领的 id=null 错误（R9-MCP-03）。
        if is_notification:
            return None
        return {
            "jsonrpc": "2.0", "id": msg_id,
            "error": {"code": -32602, "message": "Invalid params：params 必须是对象"},
        }
    if method.startswith("notifications/") and not is_notification:
        # P6：带 id 的 notifications/* 不是合法通知，也不是已知请求 → 必须回 -32601，
        # 而不是因为前缀判断永远沉默（客户端会一直等响应）。
        return {
            "jsonrpc": "2.0", "id": msg_id,
            "error": {"code": -32601, "message": "Method not found: %s" % method},
        }
    try:
        result = dispatch(method, params)
    except RpcError as exc:
        if is_notification:
            return None
        error: Dict[str, Any] = {"code": exc.code, "message": exc.message}
        if exc.data is not None:
            error["data"] = exc.data
        return {"jsonrpc": "2.0", "id": msg_id, "error": error}
    except Exception as exc:  # noqa: BLE001 - 服务端兜底，避免单条请求打死整个进程
        if is_notification:
            return None
        return {
            "jsonrpc": "2.0", "id": msg_id,
            "error": {"code": -32603, "message": "Internal error: %s" % exc},
        }
    if result is None or is_notification:
        return None
    return {"jsonrpc": "2.0", "id": msg_id, "result": result}


def _force_utf8_stdio() -> None:
    """强制 stdio 用 UTF-8：Windows 控制台/管道默认 GBK，写中文 JSON 会崩。"""
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            pass


def serve(stdin, stdout) -> int:
    """按行读 JSON-RPC、按行写响应，直到 EOF。"""
    _force_utf8_stdio()
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except (ValueError, RecursionError) as exc:
            # R9-MCP-04：Python 的 json 解析是递归的，`[[[[…` 深嵌套会抛 RecursionError；
            # 以前只捕获 ValueError，一条请求就能把服务进程打死（stdin 循环直接退出）。
            response: Optional[Dict[str, Any]] = {
                "jsonrpc": "2.0", "id": None,
                "error": {"code": -32700, "message": "Parse error: %s" % exc},
            }
        else:
            response = handle_message(message)
        if response is not None:
            stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
            stdout.flush()
    return 0


# ===========================================================================
# selftest：不联网、不依赖 ffmpeg，只验证协议/schema/分发
# ===========================================================================

def _selftest() -> int:
    failures: List[str] = []

    def check(name: str, got: Any, want: Any) -> None:
        if got != want:
            failures.append("%s：期望 %r，实际 %r" % (name, want, got))

    names = [spec["name"] for spec in TOOL_SPECS]
    check("工具数 5", len(TOOL_SPECS), 5)
    check("工具名不重复", len(set(names)), len(names))
    check("工具名清单", names,
          ["check_assets", "check_video", "music_analyze",
           "publish_check", "audit_validate"])
    for spec in TOOL_SPECS:
        schema = spec["inputSchema"]
        check("%s schema 是 object" % spec["name"], schema.get("type"), "object")
        check("%s 要求 project_root" % spec["name"],
              "project_root" in schema.get("required", []), True)
        check("%s 有 project_root 属性" % spec["name"],
              "project_root" in schema.get("properties", {}), True)
        check("%s 可调用" % spec["name"], callable(spec["handler"]), True)

    check("dispatch ping", dispatch("ping", {}), {})
    init = dispatch("initialize", {})
    check("initialize protocolVersion", init["protocolVersion"], PROTOCOL_VERSION)
    check("initialize serverInfo", init["serverInfo"]["name"], SERVER_NAME)
    check("tools/list 数量", len(dispatch("tools/list", {})["tools"]), 5)
    check("通知无响应", dispatch("notifications/initialized", {}), None)
    try:
        dispatch("nope/nothing", {})
        check("未知方法抛 RpcError", False, True)
    except RpcError as exc:
        check("未知方法错误码", exc.code, -32601)
    try:
        dispatch("tools/call", {"name": "nope", "arguments": {}})
        check("未知工具抛 RpcError", False, True)
    except RpcError as exc:
        check("未知工具错误码", exc.code, -32602)

    check("summary 解析", _extract_summary("检查完成：2 ERROR / 3 WARN"),
          {"errors": 2, "warns": 3})
    check("summary 解析成片", _extract_summary("成片检查：0 ERROR / 1 WARN（规格…）"),
          {"errors": 0, "warns": 1})
    check("summary 解析决策审计", _extract_summary("决策审计：1 ERROR / 2 WARN（账本）"),
          {"errors": 1, "warns": 2})
    check("summary 解析发布合规", _extract_summary("发布合规检查：3 ERROR / 0 WARN（4 个平台口径）"),
          {"errors": 3, "warns": 0})
    check("summary 无匹配", _extract_summary("hello"), None)
    check("JSON 行解析", _last_json_line('log line\n{"ok": true}\n'),
          {"ok": True})
    check("JSON 行坏数据", _last_json_line("not json"), None)
    check("_tail 短文本", _tail("abc", 10), "abc")
    check("_tail 长文本截断", _tail("x" * 50, 10).endswith("x" * 10), True)

    bad = handle_message("not-an-object")
    check("坏消息 -32600", bad["error"]["code"], -32600)
    unknown = handle_message({"jsonrpc": "2.0", "id": 9, "method": "x/y"})
    check("未知方法响应码", unknown["error"]["code"], -32601)
    call = handle_message({"jsonrpc": "2.0", "id": 10, "method": "tools/call",
                           "params": {"name": "audit_validate",
                                      "arguments": {"project_root": "/definitely/missing"}}})
    check("坏 project_root -32602", call["error"]["code"], -32602)
    note = handle_message({"jsonrpc": "2.0", "method": "notifications/initialized"})
    check("通知不产生响应", note, None)
    # P6：带 id 的 notifications/* 必须得到 -32601 错误响应，不能静默挂起。
    notif_with_id = handle_message(
        {"jsonrpc": "2.0", "id": 7, "method": "notifications/wat"})
    check("带 id 通知 → -32601", notif_with_id["error"]["code"], -32601)
    notif_with_id2 = handle_message(
        {"jsonrpc": "2.0", "id": 8, "method": "notifications/initialized"})
    check("带 id initialized → -32601", notif_with_id2["error"]["code"], -32601)
    unknown_notif = handle_message({"jsonrpc": "2.0", "method": "notifications/wat"})
    check("无 id 未知通知仍不响应", unknown_notif, None)

    # R9-MCP-01：check_video 的 expect 是分辨率 WxH，不是时长（schema 以前写反了）
    expect_spec = TOOLS_BY_NAME["check_video"]["inputSchema"]["properties"]["expect"]
    check("check_video.expect schema 是 string", expect_spec.get("type"), "string")
    check("check_video.expect schema 有 WxH pattern",
          bool(expect_spec.get("pattern")), True)
    try:
        _tool_check_video({"project_root": tempfile.gettempdir(), "expect": 45})
        check("expect=45（时长）抛 RpcError", False, True)
    except RpcError as exc:
        check("expect=45 错误码", exc.code, -32602)

    # R9-MCP-02：位置参数以 - 开头会被 argparse 当开关（--selftest 让脚本自测 exit 0）
    try:
        _tool_check_video({"project_root": tempfile.gettempdir(),
                           "video": "--selftest"})
        check("video='--selftest' 抛 RpcError", False, True)
    except RpcError as exc:
        check("video='--selftest' 错误码", exc.code, -32602)
    try:
        _tool_music_analyze({"project_root": tempfile.gettempdir(),
                             "audio": "--help"})
        check("audio='--help' 抛 RpcError", False, True)
    except RpcError as exc:
        check("audio='--help' 错误码", exc.code, -32602)
    check("_safe_positional 正常路径", _safe_positional("out/x.mp4", "video"),
          "out/x.mp4")

    # R9-MCP-03：通知（无 id）携带非对象 params 时不得回响应
    bad_params_notif = handle_message(
        {"jsonrpc": "2.0", "method": "notifications/initialized", "params": []})
    check("通知 + 坏 params 不响应", bad_params_notif, None)

    # R9-MCP-04：深嵌套 JSON 抛 RecursionError，不能打死服务
    import io as _io
    deep = "[" * 4000 + "]" * 4000
    out = _io.StringIO()
    code = serve(_io.StringIO(deep + "\n"
                              + '{"jsonrpc":"2.0","id":1,"method":"ping"}\n'), out)
    replies = [json.loads(line) for line in out.getvalue().splitlines()]
    check("深嵌套 JSON → -32700 且继续服务",
          [replies[0]["error"]["code"], replies[1]["id"], code], [-32700, 1, 0])

    env = _child_env(Path(tempfile.gettempdir()))
    check("子进程 PROMO_ROOT", env.get("PROMO_ROOT"), str(Path(tempfile.gettempdir())))
    check("子进程 UTF-8", env.get("PYTHONIOENCODING"), "utf-8")

    if failures:
        for message in failures:
            sys.stderr.write("ERROR: selftest — %s\n" % message)
        sys.stderr.write("ERROR: selftest — %d 项失败\n" % len(failures))
        return 1
    print("OK: selftest — 工具 schema / initialize / tools.list / 分发 / "
          "错误码 / 摘要解析（含决策审计·发布合规）/ 带 id 通知 -32601 / "
          "expect 为 WxH 分辨率 / 位置参数开关注入拦截 / 通知坏 params 不响应 / "
          "深嵌套 JSON -32700 / 子进程环境 全部通过")
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    # P8：--selftest 也走 stdio；先统一 UTF-8，否则 Windows 管道下中文输出是 GBK 乱码。
    _force_utf8_stdio()
    parser = argparse.ArgumentParser(
        prog="mcp_server.py",
        description="promo-forge MCP stdio server（按行 JSON-RPC 2.0，零依赖）",
    )
    parser.add_argument("--selftest", action="store_true", help="离线自测协议与工具 schema")
    parser.add_argument("--version", action="version",
                        version="%(prog)s " + SERVER_VERSION + " / protocol " + PROTOCOL_VERSION)
    args = parser.parse_args(argv)
    if args.selftest:
        return _selftest()
    return serve(sys.stdin, sys.stdout)


if __name__ == "__main__":
    raise SystemExit(main())
