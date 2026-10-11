# promo-forge · 一句话做一支竖屏宣传片

**把产品名或网址丢给 AI 编程工具，它会自己调研、提问、出样帧让你挑、截真实界面、按拍卡点、配音配乐、逐项校验，最后交给你一支 1080×1920 的竖屏宣传片和一个能在浏览器里播放的网页版。**

写给抖音 / 小红书 / 视频号 / B站竖屏 / YouTube Shorts 这类**竖屏短视频**：30~60 秒、最晚第 4 秒完成第一次能量爆发、真实产品画面必须占主角。

**默认零付费**：配音用免费的 `edge-tts`，配乐用免费可商用曲库 + 本地节拍分析，渲染用 Remotion（个人与 3 人以内公司免费）。ElevenLabs 是可选升级项，不是必需品。

---

## 它解决什么问题

做一支像样的产品短视频，通常要：写脚本 → 找素材 → 定风格 → 截图 → 剪卡点 → 配音 → 混音 → 校验 → 导出。
外包一支几百到几千元；自己做要学一整套软件。

promo-forge 把这件事拆成两半：

- **给人看的部分**：`AGENTS.md` 是一份 AI 能照着执行的工作规范，`references/` 是每个阶段的方法论。
- **能跑的部分**：`template/` 是一个可直接复制的 Remotion 工程 + 一组独立脚本（截图、配乐分析、配音、混音、抽帧、校验）。

你只需要说一句话，然后在两个地方拍板：**需求确认**和**试听声音**。其余交给 AI。

## 和同类工具的区别

| | 常见做法 | promo-forge |
| --- | --- | --- |
| 画幅 | 横屏 16:9 优先 | **竖屏 1080×1920 优先**，含平台 UI 安全区（上 10% / 下 18% / 右 14%） |
| 配音 | 依赖付费 TTS API | **默认 edge-tts 免费本地配音**，ElevenLabs 可选 |
| 配乐 | 靠感觉挑一首 | 本地分析 BPM / beat / bar / downbeat / 逐小节能量，**卡点靠算不靠猜** |
| 画面 | AI 生成图凑 | **真实界面 2 倍图截图**（1080 宽画布需要 2160 像素），AI 图只做包装 |
| 质量 | 渲完就交 | 渲染前后各有**自动闸门**（缺文件、规格、响度、空档、渲染日志报错）+ 逐场景抽帧审图 |
| 判断 | "感觉节奏太快/太慢" | 全部量化：快切 ≥0.5 秒、网址 ≥2.5 秒、人声高于音乐 ≥6 dB、母带 -14 LUFS |

## 快速开始

### 前置条件

- **Node.js ≥ 18**、**Python ≥ 3.9**、**ffmpeg / ffprobe**（三样缺一不可）
- 可选：`ELEVENLABS_API_KEY`（想要更自然的音色再装）

```bash
git clone https://github.com/hwy-miaomiao/promo-forge.git
cd promo-forge
pip install -r template/requirements.txt
python -m playwright install chromium   # Windows 上必须走 python -m
cd template && pnpm install && cd ..   # 没装 pnpm 用 npm install
python template/scripts/doctor.py     # 体检：只检查、不安装，缺什么给你装什么
```

### 用法一：克隆仓库直接用

用 Cursor / Claude Code / Codex 打开这个目录，然后说：

```text
帮我给 https://example.com 做一支竖屏宣传片
```

成片会落在 `outputs/<项目名>/`。

### 用法二：装成 Skill，在你自己的项目里用

```bash
npx skills add hwy-miaomiao/promo-forge    # 装到全局加 -g
```

装好后，在任意项目里说「给这个项目做一支宣传片」，AI 会自动调用；它能直接读你的源码和 README，拿到最准确的一手素材。成片落在当前项目的 `宣传片/` 目录（记得把它加进 `.gitignore`）。

### 一句话之后会发生什么

1. **AI 先自己调研**你的产品，再去查缺的信息。
2. **两轮提问**：画幅时长、目标受众、风格方向、开场方式、配音音色、素材与版权边界 —— 每题都带一个针对你产品的推荐项。
3. **出 3~4 张不同风格的样帧**给你挑（也可以混搭，比如"A 的配色 + C 的构图"）。选择写进 `需求确认.md`，你说"准确，开始制作"才开工。
4. 之后全自动：截图 → 配乐卡点 → 逐场景制作 → 配音 → 混音 → 校验 → 渲染 → 交付。
5. 中途只在三种情况找你：需要你登录某个网站、需要你试听配乐/音色、遇到只有你能核实的事实。

## 工作流程

```mermaid
flowchart LR
    A[一句话需求] --> B[预调研]
    B --> C{需求问询<br/>风格样帧}
    C -->|你确认| D[建项目]
    D --> E[素材调研<br/>真实截图]
    E --> F[配乐与卡点]
    F --> G[逐场景制作]
    G --> H[配音与字幕]
    H --> I[混音]
    I --> J{校验闸门}
    J -->|有 ERROR| G
    J -->|0 ERROR| K[渲染交付]
    K -.->|提意见| L[改版<br/>只重做必要步骤]
    L --> G
```

改版时 AI 会判断影响范围：只换字幕就不动音频；只换音色就替换成片音轨而不重渲画面。

## 目录结构

```text
promo-forge/
├── AGENTS.md        # AI 工作规范（整个项目的核心，也是唯一权威）
├── SKILL.md         # 装成 Agent Skill 时的入口
├── CLAUDE.md        # 让 Claude Code 也读 AGENTS.md
├── references/      # 八份方法论：需求问询 / 风格方向 / 素材与截图 / 配乐与卡点 / 配音与字幕 / 校验清单 / 踩坑记录 / 案例复盘
├── template/        # 复制到项目里的 Remotion 工程模板
│   ├── src/         #   timing.json（唯一事实来源）+ Root/Promo + 组件 + 场景
│   ├── scripts/     #   doctor / capture / media / music / tts / mix / subtitles / fonts / deliver / check_assets / check_video / stills / sync_template
│   ├── web/         #   基于 @remotion/player 的网页播放版
│   └── public/      #   素材与音频（截图、混音结果）
└── outputs/         # 克隆用法下的成片目录（git 忽略）
```

每个项目目录里除了成片，还会有 `需求确认.md`（你拍板的决定 + 每轮修改记录）、`调研笔记.md`（每个事实与素材的出处）、`timing.json`、抽帧校验图。

## 脚本速查

| 脚本 | 作用 |
| --- | --- |
| `scripts/doctor.py` | 环境体检：Node / ffmpeg / Python 包 / Playwright / 中文字体 / 配置，逐项给修复命令 |
| `scripts/capture.py` | Playwright 真实网页截图：设备预设、分段滚动、整页长图、隐藏弹窗、登录态复用，默认 2 倍图 |
| `scripts/media.py` | 素材整理：抓 Markdown 里的图、拼联系表、按实际裁切预览、限宽、列尺寸 |
| `scripts/music.py` | 配乐：BPM / beat / bar / downbeat / 逐小节能量分析、剪段、直链下载 |
| `scripts/tts.py` | 配音：免费 edge-tts（或 ElevenLabs）、多音色试听、时长与拖音审核、可选 STT 反查 |
| `scripts/mix.py` | 混音：人声归一化、ducking、转场音效、两遍 loudnorm 母带到 -14 LUFS |
| `scripts/subtitles.py` | 字幕：导出 SRT / ASS（样式与画面一致），或把字幕烧进画面（软字幕可选） |
| `scripts/fonts.py` | 字体子集化：把片子里用到的字从系统字体抽成 300 KB 左右的子集并内嵌，换机器不缺字 |
| `scripts/deliver.py` | 交付：按抖音 / 小红书 / 视频号 / Shorts 预检规格，并出三张候选封面 |
| `scripts/check_assets.py` | 渲染前闸门：引用文件、图片尺寸、字体缺字、旁白窗口 |
| `scripts/check_video.py` | 成片闸门：规格、音轨、响度、空档、渲染日志报错 + 逐场景抽帧联系表 |
| `scripts/stills.mjs` | 按秒或按场景抽帧（Remotion renderStill） |
| `scripts/sync_template.py` | 用新版模板更新已有项目（`--diff` / `--apply`；`--force` 连「本地比模板新」的文件也覆盖） |

`timing.json` 是整支片子的唯一事实来源：帧率、画幅、时长、场景窗口、beat/bar/drop、旁白逐句配置、品牌配色全在里面，TypeScript 与 Python 两侧共读它，改一处全链路生效。

## 常见问题

**需要花多少钱？** 项目本身免费。配音的免费路线（edge-tts）不花钱；配乐请从免费可商用曲库下载。唯一要留意的是 Remotion 的授权：个人和 3 人以内的公司免费，更大的团队需要购买公司授权（见 [Remotion 授权说明](https://www.remotion.dev/license)）。

**要做多久？** 大部分时间花在 AI 自己调研、制作与校验上；你需要参与的只有开头的问询和中途的试听。渲染时间取决于画面复杂度，1080×1920 通常在几分钟内。

**能用 Windows 吗？** 可以，本项目就是在 Windows 上开发验证的。脚本会自动选用微软雅黑 / Noto Sans CJK。macOS 与 Linux 同样支持。

**能做横版吗？** 可以。把 `timing.json` 的 `width/height/aspect` 改成 1920×1080 即可，组件按安全区自适应，同一套场景不破版。

**没有 ElevenLabs 能做吗？** 能。默认就是免费的 edge-tts，音色虽然不如付费的自然，但完全可用；也可以先用免费路线把片子做出来，之后再换成付费音色（只重做音频，不重渲画面）。

**可以只用其中一部分吗？** 可以。`template/` 是独立工程，截图、节拍分析、混音、成片检测这些脚本都能单独用。

**直接渲染出来的片子没有声音？** 模板里不含音频：`timing.json` 的 `music.file` 指向的文件由 `scripts/mix.py` 生成。
没混音时模板会自动静音渲染（不会报错），但 `check_assets.py`（渲染前）和 `check_video.py`（渲染后）都会把缺音轨报出来 ——
交片前先跑一次 `python scripts/mix.py`。

**换一台机器渲染出来是方块字 / 缺字？** 跑一次 `python scripts/fonts.py subset`：它把片子里真正用到的字
（示例工程约 341 字）从系统中文字体里抽出来，生成 300 KB 左右的子集字体放进 `public/fonts/`，渲染直接用这份字体，
换机器就不再依赖渲染机上装没装中文字体。子集字体**不入库**（字体授权 + 体积考虑，见 `.gitignore`），
所以克隆后要自己跑一次；它也是按当前文案生成的，**改了文案要重跑**，否则新字会变成方块。

**怎么把字幕烧进画面？** 平台不认软字幕、或者要发给别人直接看时：先
`npm run render:1080:nosubs` 渲一版**不带画面字幕**的成片，再
`python scripts/subtitles.py burn out/final-1080p-nosubs.mp4 -o out/final-1080p-hard.mp4`。
直接烧会把画面自带的字幕和烧进去的字幕叠成两行糊字。只要字幕文件的话用 `python scripts/subtitles.py srt`（SRT）或 `ass`（带样式）。

**怎么知道能不能投抖音 / 小红书？** `python scripts/deliver.py check out/final-1080p.mp4 --platform all`
会按平台报时长、画幅、帧率、码率、体积、音轨、响度。抖音和小红书的官方口径本项目抓不到，那几条按「需人工确认」的 WARN 处理；
封面用 `python scripts/deliver.py cover --title "你的标题"` 一次出三张候选（1080×1920）+ 联系表，记得逐张看图。

## 参与贡献

用了它做出满意的片子，欢迎在 Issue 里分享。踩到新坑，欢迎把「现象 / 原因 / 解决」补进 `references/踩坑记录.md` 并发 PR —— 下一个使用者的 AI 就会提前避开。

## 致谢

站在这些工具的肩膀上：[Remotion](https://www.remotion.dev)（渲染）、[edge-tts](https://github.com/rany2/edge-tts)（免费配音）、[ElevenLabs](https://elevenlabs.io)（可选配音与作曲）、[Playwright](https://playwright.dev)（截图）、[FFmpeg](https://ffmpeg.org)（音视频处理）、[Pillow](https://python-pillow.org) / [fontTools](https://fonttools.readthedocs.io)（图像与字体）。

思路上受到「把视频制作流程写成 AI 可执行规范」这一社区实践的启发（参考项目：[liyupi/ai-promo-video-maker](https://github.com/liyupi/ai-promo-video-maker)，本项目为独立实现，聚焦竖屏短视频与零付费路线）。

## 开源协议

[MIT](LICENSE)
