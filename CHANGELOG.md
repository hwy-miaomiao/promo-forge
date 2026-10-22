# Changelog

本文件记录对 AI 工作规范与脚本的可观察变更。格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)；日期为 UTC+8。

## [Unreleased]

### Fixed（第 9 轮独立验证 V9-P）

- `frame_check.py`：PNG chunk 链收紧——未知 critical chunk / `IEND` 后尾随数据 / `IEND` payload 非空 / 重复 `IHDR` /
  `IDAT` 解压后 zlib 流尾随垃圾 = ERROR（以前全部放行，与 `publish_check.py` 口径不一致）；`tRNS` 透明像素参与空白判定
  （调色板/灰度/RGB 全透明隐形帧以前被判成「有画面」）；`--scale` 舍入改成 JS `Math.round` 口径（奇数边不再误判尺寸）；
  `timing.width/height` 是字符串/NaN 时 = ERROR（以前静默跳过尺寸核对）；提前返回路径补 `stats`（CLI 不再打「0 ERROR」）。
- `transcript_check.py`：`--ref srt` 时也从 `timing.json` 读品牌词做逐字校验（以前静默关闭，品牌错字可过闸）；
  提前返回路径补 `stats`。
- `check_assets.py`：`brand.logo` / `fonts.json` 的 `roles.*.file` 路径结构违规（绝对路径 / 盘符 / UNC / `..` 越界 /
  冒号数据流 / 控制字符）从 WARN 升为 ERROR（文档承诺 R9-CA-04 的 ERROR，以前只 WARN）；「文件不存在」仍 WARN。
- `publish_check.py`：封面 PNG 补 `IHDR` 长度/尺寸/`color_type`/`bit_depth` 合法组合、重复 `IHDR`、zlib `unused_data` 校验。
- `mcp_server.py`：`publish_check` 的 `covers`/`video`/`subtitles`/`copy`/`title`/`body` 传非字符串时返回 -32602（以前静默忽略、
  按默认值检查却报告成功）；`audit_validate` 的 `budget_cny` 必须是数字（不再接受 `"--help"` 之类开关字符串）。
- 自测：`frame_check.py` 71 项、`transcript_check.py` 44 项；文档（AGENTS.md / README.md / references/校验清单.md）同步。

## [2026-10-22] 第 9 轮：证据闸门（真的看过画面 / 配音字幕对得上）+ 闸门漏洞修复

本轮先把「代理看过画面」和「配音与字幕对得上」从口头承诺变成可跑的机器证据，再回头修独立审计发现的
「坏输入静默通过闸门」缺陷。两个新脚本都是纯标准库、确定性、Windows 安全，带自测并进 CI。

### Added

- **静帧证据闸门 `template/scripts/frame_check.py`**：逐帧完整解码 PNG（自实现 zlib 解压 + filter 0–4 反滤波，不依赖
  Pillow）、量化亮度方差判空白帧、像素尺寸==timing 分辨率（`--scale` 可缩放）、按文件名时间戳（`stills.mjs` 的
  `%06.2f` 命名）与 `timing.scenes` 核对每场景帧覆盖；JPEG 只解析头与段结构（明示不做像素解码，空白判定降级 WARN）。
  人读表 + `--json {ok, frames, problems}`，有 ERROR 退出码 1；`--selftest` 71 项断言（正常/空白/截断/CRC 坏/未知
  critical chunk/`tRNS` 全透明/错尺寸/缺场景覆盖/坏 timing/`--no-timing`/`--scale` 舍入）。实测 ffmpeg 7.1 合成 3 帧：
  正常帧亮度方差 4227.5 / 2107.1，纯白帧 0.0 判 ERROR。
- **转写反查闸门 `template/scripts/transcript_check.py`**：零依赖 STT 替代（对标 MoneyPrinterTurbo 的 STT 反查思路，
  差异化是用户提供转写 + 纯 stdlib difflib + 可离线 + CI 可跑）。txt 逐行 / SRT 自动识别，与 `voice.lines`
  （或 `--ref srt`）保序对齐；漏念 / 低相似度 / 覆盖率不足 / 顺序错位 / 品牌名与网址错字 = ERROR，多余转写行 = WARN；
  `--threshold`（默认 0.85）、`--min-coverage`（默认 1.0）可配。`--selftest` 44 项断言。
  实测：模板 5 句逐字转写 100% / 相似度全 1.00 / 0 ERROR；只念一句的转写 6 ERROR、字符覆盖率 2.94%。
- **闸门接入**：`deliver.py check` 默认验证 `out/stills`（没有目录只 WARN；`--no-frames` 可跳过），
  `publish_check.py --frames <dir>` / `--transcript <file>` 显式传入才并入发布检查单（不破坏既有产物）；
  CI 两个脚本单独跑 `--selftest`。
- 文档：README 差异化表补「证据」行、脚本表与 FAQ 补两项闸门及实测数字；AGENTS.md 第 6/7 节；
  `references/校验清单.md`；SKILL.md 铁律 3；`selftest.py` 覆盖 12 个脚本（15/15 项）；MCP 集成测试 44 项。

### Fixed

- **`publish_check.py` 的 NaN / Infinity / 字符串 timing 字段静默通过**：`numeric()` 加 `math.isfinite` 并把坏字段
  报 ERROR——之前 `duration: NaN` 让 `3≤d≤900` 全为假、`width/height: NaN` 让 `width>=height` 为假、
  `captions.bottom: NaN` 让 `≥0.18` 为假，三项都打印 OK 并判定可发布。
- **`publish_check.py` 封面 PNG 只验 24 字节头**：截断/CRC 坏的封面能通过；现完整走 chunk 链、逐 chunk CRC、
  IDAT 解压长度必须等于 IHDR 推导值。
- **`check_assets.py` 放行空/非字符串旁白**：`voice.lines[].text=""` / `123` / 全空白以前 0 ERROR（该字段是 tts、
  字幕、混音的唯一来源），现要求非空字符串。
- **`check_assets.py` 不校验 `brand` 结构**：TS 侧无条件读 `TIMING.brand.colors` / `brand.logo` / `name` / `url`，
  缺整块会在渲染期 `Cannot read properties of undefined`；现校验 brand、name/url/logo、colors(bg,fg,accent)（logo 允许空串）。
- **路径穿越**：`voice.lines[].id`（文件名 / tts 缓存键）、`music.file`（mix.py 会写入 `public/<file>` 并自动建父目录）、
  `brand.logo`、`fonts.json` 的 `roles.*.file` 现在拒绝绝对路径、盘符与 `..` 越界；id 还拒绝 Windows 保留名与首尾点/空格。
- **MCP 工具参数注入**：`check_video` 的 `video="--selftest"`、`music_analyze` 的 `audio="--help"` 会被 argparse 当成开关，
  「什么都没查」却返回 `isError=false`；现路径类位置参数拒绝以 `-` 开头并加 `--` 分隔。
- **MCP `check_video.expect` schema 与实现不符**：schema 声明 number「期望时长秒数」，`--expect` 实际是 WxH 分辨率；
  现 schema 改 string + pattern，传数字/非法值返回 -32602。
- **MCP 协议**：通知（无 id）带非对象 params 时不再返回错误响应；深嵌套 JSON 的 `RecursionError` 不再打死服务（返回 -32700）。

### 升级须知（已有项目）

- 新脚本不是硬依赖：`deliver.py check` 没有 `out/stills` 只 WARN，`publish_check.py` 不传 `--frames`/`--transcript`
  时行为和以前一致；想显式跳过静帧检查用 `deliver.py check --no-frames`。
- 自建 `timing.json`（不是从模板复制的）若缺 `brand` 结构，`check_assets.py` 现在会报 ERROR——模板自带的 `brand` 完整
  （`logo` 允许空字符串）。
- `publish_check.py` 现在会拦 `NaN` / `Infinity` / 字符串形式的 `duration`、`width`、`height`、`captions.bottom`。

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
