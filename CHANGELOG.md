# Changelog

本文件记录对 AI 工作规范与脚本的可观察变更。格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)；日期为 UTC+8。

## [Unreleased]

## [2026-10-22] 修复轮（task-9）：按独立验证报告修 6 major + 5 minor

独立验证对融合改动做了复现核对（报告在工作区 `similar-projects/verification-report.md`），
本轮按 P1–P11 全部修复。核心原则：**闸门不能「无法判定就放过」**——文本假成片必须拦住，
文档不许比实现更强，带 id 的协议请求不许静默丢包。

### Changed

- **P1 成片校验升级**：`publish_check.py` 新增 MP4 盒结构校验（`ftyp` + `moov` 等 magic，
  逐顶层 box 校验尺寸与类型，文本/半截 MP4 直接 ERROR）；有 `ffprobe` 时再校验容器/编码/时长/分辨率，
  没有 `ffprobe` 时显式 WARN（只降级深检，不放过容器校验）。
- **P3 决策账本落实「人拍板 + 付费记账」**：缺 `user_requirement`（需求确认）记录 = ERROR；
  `cost_cny > 0` 但 `paid_service` 为空 = ERROR（花了钱必须写清服务名）；`user_audio_review` /
  `user_fact_check` 明确为按项目条件校验，AGENTS.md 措辞与实现对齐。
- **P5 存量兼容**：`check_assets.py` / `check_video.py` 遇到缺 `decisions.json` 改报 **WARN** 并提示
  `audit.py init`（老项目不直接打红）；结构坏、缺需求确认、付费未记账、超预算仍是 ERROR。
- **P6 MCP 协议**：带 `id` 的 `notifications/*` 请求返回 `-32601` 错误响应，不再静默丢弃；
  只有真正无 `id` 的通知才无响应。集成测试加断言。
- **P8 UTF-8**：`mcp_server.py` 的 `main()` 入口统一 `_force_utf8_stdio()`，`--selftest` 在 GBK/ASCII
  管道下不再乱码。
- **P11 摘要解析**：`SUMMARY_RE` 扩展支持「决策审计」「发布合规检查」两种汇总行，
  `audit_validate` / `publish_check` 工具的 `stats` 不再恒为 None。
- **P10 执行顺序**：`check_video.py` 把零依赖的 `check_audit` 提到 ffmpeg/ffprobe 可用性检查之前，
  没装 ffmpeg 也能先报决策审计结果。
- 文档同步：AGENTS.md 阶段 3/9/10、§6 脚本表、§7 闸门表；CHANGELOG 本条；`service/README.md`
  与集成测试夹具同步 MP4 容器校验与新断言。

### Fixed

- **P2 `--strict` manifest 自相矛盾**：`build_manifest` 改为只从最终 `results` 派生 `entries` /
  `summary` / `ready_platforms`（不再用 package 重算），WARN 升级后 summary 与每平台 `publishable` 同源。
- **P4 话题数越界级别**：由 WARN 升为 ERROR，与标题/正文/时长口径一致（文档同步）。
- **P7 预算覆盖**：`audit.py validate --budget N --format json` 的 summary 使用最终预算
  （`audit_summary(data, budget_cny=...)`），不再回显账本里的旧值。
- **P9 报告数字**：`fusion-promo.md` 的行数改为统一命令统计的「约 N 行」，并删除不实的
  「已清 `__pycache__`」表述；交付前实测清理。
- 修复轮自测暴露并修掉的问题：`publish_check.py` selftest 夹具原用文本假 mp4（正是 P1 漏网原因）、
  `strict` 用例需补「summary 与平台条目一致」断言、`check_assets.py` selftest 断言把 `Report.rows`
  当 dict 用（`TypeError: string indices must be integers`）。

### 升级须知（已有项目）

- 账本里**还没有 `user_requirement` 记录**的项目，升级后 `audit.py validate` / 两个 check 脚本会报 ERROR；
  按提示补一条即可：`python scripts/audit.py append --kind user_requirement --decision "确认需求与风格"`。
- 从未建过 `decisions.json` 的存量项目：只报 WARN，不阻塞；建议补跑 `python scripts/audit.py init`。
- 有付费支出但旧记录没写 `paid_service` 的项目：现在会报 ERROR，补上服务名。
- `publish_check.py` 现在会校验 MP4 容器结构：用文本/占位文件当 `out/final-1080p.mp4` 的测试流程会失败（预期）。
- `--strict` 下 `publish/manifest.json` 的 `summary` 与每平台 `publishable` 现在严格同源，
  旧清单里「summary 不可发布但平台可发布」的矛盾不会再出现。

## [2026-10-21] 融合竞品能力：发布合规闸门 / 决策成本审计 / MCP 服务化入口

调研同类项目（OpenMontage、MoneyPrinterTurbo、liyupi/ai-promo-video-maker、short-video-maker、claude-remotion-skill）后，
把三项对「能发出去」真正有约束力的机制做进本仓库。调研原文与对照表见工作区 `similar-projects/promo-similar.md`。

### Added

- **发布合规闸门 `template/scripts/publish_check.py`**：按抖音 / 快手 / 小红书 / 视频号逐平台检查标题长度、话题数、
  正文长度、时长、封面数量与尺寸（1080×1920）、字幕安全区（`captions.bottom ≥ 0.18`），输出
  `publish/manifest.json`（每平台 `publishable` 结论 + 逐项检查明细）。`--strict` 把 WARN 升为 ERROR；
  `--format json` 供外部编排器解析。**只检查、不登录、不自动发布**（账号风控与平台合规由人负责）。
- **决策与成本账本 `template/scripts/audit.py`**：`decisions.json` 记录「谁拍板 / 何时 / 花了多少 / 用没用付费服务」，
  默认预算 0 元（零付费路线），支持 `PROMO_BUDGET_CNY` 与 `--budget` 覆盖；`init` / `append` / `validate` 三个子命令，
  `validate --format json` 供 MCP 调用。缺文件 / 结构坏 / 人拍板记录缺失 / 付费未记账 / 累计超预算均为 ERROR。
- **MCP 服务化入口 `service/`**：零第三方依赖的 stdio server（JSON-RPC 2.0，按行 framing，一行一条 UTF-8 JSON），
  暴露 `check_assets` / `check_video` / `music_analyze` / `publish_check` / `audit_validate` 五个工具；
  `service/README.md` 含工具 schema、请求示例、错误码与 Docker 说明；`service/Dockerfile` 只装校验依赖（未在本机实测）。
  集成测试 `service/test_mcp_integration.py` 起真实子进程走 `initialize` / `tools/list` / `tools/call`，CI 同款。
- **闸门接入**：`check_assets.py` 与 `check_video.py` 在原有检查后新增决策账本校验（缺文件提示 `audit.py init`，
  零付费默认通过，不增加正常路径负担）；`check_video.py` 新增 `--decisions` / `--budget-cny` 参数；
  `deliver.py check` 在 0 ERROR 时自动生成 `publish/manifest.json` 发布合规检查单。
- **文档**：AGENTS.md 阶段 3 加 `audit.py init`、阶段 9 加账本与发布检查、阶段 10 加 `publish_check.py --strict`、
  阶段 11 加「只改发布文案」一行、第 6 节脚本表与第 7 节闸门表补两行并更新 selftest 计数；README 补目录树、
  脚本表、服务化入口与「和 OpenMontage / MoneyPrinterTurbo 的分工」；SKILL.md 铁律 3 增加交付前的两项闸门。
- **CI**：`compileall` 覆盖 `service/`，新增 MCP 集成测试步骤；两份 `.gitignore` 忽略 `decisions.json` 与 `publish/` 产物。

### Fixed

- `publish_check.py` / `check_assets.py` 自测与夹具里有 4 处 `write_text(..., newline="\n")`，Python 3.9 会抛
  `TypeError`（3.10+ 才有 `newline` 参数）。改为 `open(..., newline="\n")` 写盘；`selftest.py` 的 AST 扫描范围
  从 `template/scripts` 扩到 `template/scripts + service` 后由 CI 同款检查直接抓到并修复。
- `audit.py` 自测里有一句 `... if False else True` 死代码导致「预算边界」用例永远通过；删除该行并修正夹具
  （2 元预算花 3 元），用例现在真正覆盖超预算拦截。
- `service/mcp_server.py` 在 Windows 下继承 GBK stdout，输出中文 JSON 时抛
  `UnicodeEncodeError: 'gbk' codec can't encode character '\xa5'` 并崩溃；`serve` 启动时把 stdin/stdout/stderr
  `reconfigure(encoding="utf-8")`，服务端在任何控制台代码页下都能输出 UTF-8。
- `check_video.py` 的 `--decisions` / `--budget-cny` 帮助文本首次写入时语序混乱，已修正为可读中文。

### Not done

- **S1 STT 逐句反查升为默认闸门**：`tts.py stt` 已有能力，但本机无 ffmpeg / 无 Whisper 依赖，无法做端到端验收；
  且会把「可选付费/可选依赖」变成默认路径，与零付费原则冲突——保留为可选，未升默认。
- **S3 参考视频驱动**：需要下载并分析外部视频（yt-dlp + 抽帧 + 转写），本机无 ffmpeg 且任务要求不联网，
  无法验证；只在报告里记录为后续方向，未落代码。
- 真渲染 / ffprobe / LUFS 实测：本机 PATH 无 ffmpeg，未跑 `check_video.py` 对真实成片的检查与 Remotion 渲染；
  相关逻辑均以纯函数自测与假产物覆盖（injection 部分用临时目录 + 假 mp4/PNG 头）。
