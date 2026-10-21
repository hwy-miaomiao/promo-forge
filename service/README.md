# service/ · promo-forge 服务化入口（MCP stdio）

把 `template/scripts/` 里的确定性校验脚本包成 **MCP 工具**，让 n8n、Claude Code、
Cursor、自建 agent 等调用方不必读 AGENTS.md，也能直接问「这版能不能发」。
零第三方依赖（只用 Python 标准库），不需要 Node、不需要联网。

> 这是「检查服务」，不是「渲染服务」：渲染仍在模板工程里跑 `npm run render:*`。
> 服务只负责读项目产物、跑闸门、返回结构化结论。

## 启动

```bash
# 直接跑（stdin/stdout 就是 JSON-RPC 通道）
python service/mcp_server.py

# 离线自测（不联网、不依赖 ffmpeg）
python service/mcp_server.py --selftest

# 集成测试：临时假项目 + 子进程走完整协议
python service/test_mcp_integration.py
```

客户端配置示例（Claude Code / Cursor 的 MCP server 配置）：

```json
{
  "mcpServers": {
    "promo-forge": {
      "command": "python",
      "args": ["F:/DeepSeek Harness/promo-forge/service/mcp_server.py"]
    }
  }
}
```

## 协议与 framing

- JSON-RPC 2.0 over stdio，**按行 framing**：每条消息一行 UTF-8 JSON（`\n` 分隔）。
- stdout 只写响应；子脚本的日志被捕获后放进工具结果的 `stdoutTail`/`stderrTail`。
- 支持方法：`initialize`、`notifications/initialized`（通知，无响应）、`ping`、
  `tools/list`、`tools/call`。
- 通知与请求的边界：只有**不带 `id`** 的 `notifications/*` 才按通知处理（无响应）；
  带 `id` 的 `notifications/*` 不是合法请求，返回 `-32601` 错误响应（客户端不会一直等）。
- 错误码：坏 JSON `-32700`、未知方法 `-32601`、坏参数/未知工具 `-32602`、
  服务端异常 `-32603`。工具**执行失败**不算协议错误，而是返回
  `{"content":[…],"isError":true}`，与 MCP 约定一致。

工具结果 `content[0].text` 是一段 JSON：

```json
{
  "tool": "check_assets",
  "exitCode": 1,
  "timedOut": false,
  "stats": {"errors": 3, "warns": 1},
  "structured": {},
  "stdoutTail": "…",
  "stderrTail": "…"
}
```

`publish_check` / `audit_validate` 会把脚本的 `--format json` 结果解析进
`structured`（含 `publishable`、`platforms`、`summary` 等）。

## 工具

| 工具 | 作用 | 关键参数 | 依赖 |
| --- | --- | --- | --- |
| `check_assets` | 渲染前闸门：素材引用、真实截图配额、timing 结构、字体、音频 | `project_root`、`strict` | 无 |
| `check_video` | 成片闸门：规格、音轨、响度 -16~-12 LUFS、空档、渲染日志、抽帧 | `project_root`、`video`、`no_log`（默认 true）、`expect`、`lufs_min`、`lufs_max` | ffmpeg/ffprobe |
| `music_analyze` | 配乐分析：时长/采样率/BPM/每秒能量/候选 drop | `project_root`、`audio` | numpy+scipy |
| `publish_check` | 四平台发布合规检查单：标题/话题/正文/时长/封面/安全区 + 成片 MP4 容器结构（`ftyp`+`moov`，文本假视频 ERROR） | `project_root`、`platform`、`title`、`hashtags`、`body`、`strict` | 无（有 ffprobe 时深检，无则 WARN） |
| `audit_validate` | 决策与成本账本：需求确认必填、付费服务记账（含服务名）、预算上限；存量项目缺账本只 WARN | `project_root`、`budget_cny` | 无 |

`project_root` 是宣传片项目根目录（含 `src/timing.json`）。服务会把它作为
`PROMO_ROOT` 传给子脚本，所有相对路径都按项目根解析。

## 示例（当前项目根 = outputs/demo/）

```json
{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"demo","version":"0"}}}
{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}
{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"audit_validate","arguments":{"project_root":"outputs/demo"}}}
{"jsonrpc":"2.0","id":4,"method":"tools/call","params":{"name":"publish_check","arguments":{"project_root":"outputs/demo","platform":"xiaohongshu"}}}
```

## Docker

`service/Dockerfile` 是「校验服务」镜像：`python:3.9-slim` + ffmpeg + Noto CJK 字体
+ `template/requirements.txt`。说明：

- **只装校验需要的东西**：渲染（Remotion/Node）和真实截图（Playwright 浏览器）
  建议放在宿主机或独立渲染镜像，避免镜像膨胀。
- 需要 `capture.py` 真实截图时，取消 Dockerfile 里 Playwright 两行的注释再构建。
- CI 不构建镜像（不在 CI 里下载几百 MB 依赖）；本机也**未实测构建**，只保证
  Dockerfile 语法与路径正确。

```bash
docker build -f service/Dockerfile -t promo-forge-service .
echo '{"jsonrpc":"2.0","id":1,"method":"ping"}' | docker run --rm -i promo-forge-service
```

## 与 AGENTS.md 的关系

服务只是同一套闸门的「远程可调用入口」，判定标准与 `selftest.py`、`check_assets.py`、
`check_video.py`、`publish_check.py`、`audit.py` 完全一致：**脚本退出码非零就代表
有 ERROR**，服务原样透传，不做二次美化。CI 只保证「协议与分发正确」；真实判定
仍以脚本为准（所以 CI 不跑真渲染、不装 ffmpeg）。
