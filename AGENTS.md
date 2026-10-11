# promo-forge 工作规范

> 这是本仓库的核心文件。AI 编程工具（Claude Code / Cursor / Codex / Gemini CLI 等）打开本目录后，
> 必须**先把本文件从头读到尾**，再按下面的阶段推进。各阶段的详细方法在 `references/`，按需读取。

## 0. 这个仓库是什么

把「做一支能发出去的竖屏产品宣传片」这件事，写成 AI 可以照着执行的规范（本文件）+ 一个 Remotion
工程模板（`template/`）+ 一组可独立运行的脚本（`template/scripts/`）。

交付物：一支 **1080×1920 的竖屏 MP4**（首选）、可选 1920×1080 横版，外加一个可在线播放的网页版。

目标形态是 **30~60 秒、能被算法推荐、能被人看完** 的短视频，不是企业形象片。默认零付费：
配音用免费的 edge-tts，配乐用免费可商用曲库 + 本地节拍分析，渲染用 Remotion（个人与 3 人以内团队免费）。

### 四条总原则

1. **真实产品才是主角。** AI 画的图和代码画的图形只负责包装、转场和留白；功能展示必须是真实界面截图或真实作品图。
2. **一切审美判断都要落成可测量的数字。** AI 听不到声音、看不到画面对不对味，所以把要求写成秒数、dB、LUFS、
   像素、相似度，交给脚本判定，而不是交给"感觉"。
3. **需要人拍板的只有三类事**：需求确认、试听声音、只有用户能核实的事实。其余全自动。
4. **禁止套模板。** 风格要从产品的受众和调性推导出来，每个项目都要出样帧给用户挑，不允许所有产品都做成同一套深色科技风。

## 1. 两种用法与路径约定

| 用法 | `<工具目录>` | `<工作区>` | `<输出目录>` |
| --- | --- | --- | --- |
| 克隆仓库 | 本仓库根目录 | 本仓库根目录 | `outputs/<slug>/` |
| 安装成 Skill | `SKILL.md` 所在目录 | 用户当前项目根目录 | `<工作区>/宣传片/<slug>/` |

- `<slug>` 是项目短名，全小写 + 连字符，例如 `acme-app`。一个 slug 对应一个目录，永不混用。
- 新项目 = 复制 `<工具目录>/template/` 到 `<输出目录>`，再在副本里改。**永远不要改 `<工具目录>/template/` 本体**。
- Skill 用法下，提醒用户把 `<工作区>/宣传片/` 加进 `.gitignore`（工程文件可能几百 MB）。

## 2. 目录结构

```text
promo-forge/
├── AGENTS.md            # 本文件：工作规范（唯一权威）
├── SKILL.md             # 安装成 Agent Skill 时的入口
├── CLAUDE.md            # 让 Claude Code 也读 AGENTS.md
├── README.md            # 给人看的说明
├── references/          # 各阶段方法论（需求问询/风格方向/素材与截图/配乐与卡点/配音与字幕/校验清单/踩坑记录/案例复盘）
├── template/            # 复制到输出目录的工程模板
└── outputs/             # 克隆用法下的成片目录（git 忽略）
```

每个输出目录里除视频外，还必须有：

- `需求确认.md`：用户拍板的每一项决定 + 每一轮修改意见（原文）与处理方式。
- `调研笔记.md`：每个事实、每张素材、每首歌的出处与授权。
- `timing.json`：**整支片子的唯一事实来源**（见第 5 节）。
- `out/stills/`、`out/check-*.jpg`：校验用截图与联系表。

## 3. 流程总览

```mermaid
flowchart LR
    A[一句话需求] --> B[阶段1 预调研]
    B --> C{阶段2 需求问询<br/>样帧对比}
    C -->|用户确认| D[阶段3 建项目]
    D --> E[阶段4 素材调研<br/>真实截图]
    E --> F[阶段5 配乐与卡点]
    F --> G[阶段6 分镜制作]
    G --> H[阶段7 配音与字幕]
    H --> I[阶段8 混音]
    I --> J{阶段9 校验闸门}
    J -->|有 ERROR| G
    J -->|0 ERROR| K[阶段10 渲染交付]
    K -.->|用户提意见| L[阶段11 改版]
    L --> G
```

### 阶段 0 · 环境体检

```bash
python template/scripts/doctor.py      # 只检查、不安装；退出码 1 表示缺东西
```

缺什么就按脚本给出的命令装什么，装完重跑一次到没有 MISSING 再开工。没有 ffmpeg 不要往下走。

### 阶段 1 · 预调研（不打扰用户）

先用现有信息把能查的查完：产品官网/仓库 README/应用商店页/社媒主页。产出一页纸的初步结论：
它解决什么问题、谁在用、最值得炫耀的 3 个点、有没有现成的界面截图或视频。

### 阶段 2 · 需求问询（**唯一强制卡点**）

一轮 5~8 题、最多两轮问完，每题**首项必须是带理由的推荐项**，最后一题永远是"还有别的偏好吗"。
必问项：竖屏还是横版（默认竖屏 9:16）、时长（默认 **45 秒 ±10**）、发布平台、目标受众、
风格方向、开场方式、配音音色、素材来源与版权边界。

同时给出 **3~4 个差异明显的风格方向，每个方向画一张同尺寸样帧**（用 AI 绘图或代码绘制），
让用户直观看"这个风格用在我的产品上是什么样"。方法见 `references/风格方向.md`。

把结论写进 `<输出目录>/需求确认.md`，**等用户明确说"准确，开始制作"之后才允许进入阶段 3**。

### 阶段 3 · 建项目

```bash
cp -r template <输出目录>
cd <输出目录>
pnpm install            # 没装 pnpm 就 npm install（首次会下载 Remotion 依赖）
python scripts/doctor.py               # 副本内再体检一次
```

复制后立刻：改 `src/timing.json` 的 `slug/title/aspect/duration/scenes`；`src/scenes/` 里的
`Hook / Demo / Proof / Cta` 是可直接运行的示例场景（零外部素材也能渲出画面），照着它们写自己的场景，
不需要的示例文件可以删掉，记得同步 `src/scenes/index.ts` 的注册表。

### 阶段 4 · 素材调研与真实截图

- 优先级：**用户提供的一手素材 > 真实页面截图 > AI 生成图 > 网络图片**。用到的每张图都要记进 `调研笔记.md`（来源 + 授权）。
- 真实界面截图用本仓库脚本，`--factor 2` 出 2 倍图（1080 宽显示 → 2160 像素）；不要用 AI 工具自带浏览器的截图功能，
  它们通常只有 1 倍分辨率，放进视频就是糊的。
- 需要登录的页面用 `--login` 弹一个真实窗口让用户登录一次，登录态存 `out/.browser-profile/`。
- **硬性配额：每个功能段落至少 1 张真实截图；片长 ≤45 秒时全片真实素材 ≥6 张、>45 秒时 ≥8 张；
  必须有一段由 ≥6 张真实作品组成的快切蒙太奇。** 数量不够就先补素材，不要用 AI 图凑数。

### 阶段 5 · 配乐与卡点

- 免费路线：从免费可商用曲库取 1~3 首候选放进 `audio-src/music/`，用 `music.py analyze/grid/bars` 分析出
  BPM、beat、bar、downbeat、drop 和逐小节能量；写回 `timing.json` 的 `music.beat/bar/drop/offset`。
- 音乐与画面必须对齐到 beat 或 bar：`musicOffset = drop - downbeat`（**爆发时刻 − 重拍时刻**；正负号含义与三种典型情形见 `references/配乐与卡点.md`）。
- **硬性要求：全片 ≥2 次能量爆发；最晚在第 4 秒完成第一次爆发；节拍一致性 ≥0.9；候选统一到 -14 LUFS 后再给用户试听。**
- 试听必须交给用户：把候选剪成同样的 15 秒片段，叠上人声给用户听，让他选（AI 听不到声音，禁止自己拍板）。

### 阶段 6 · 分镜制作

- 一个场景 = 一个组件，组件自己按 `timing.json` 的窗口判断显示与否，窗口外必须 `return null`。
- **时间基准只有两套，混用必翻车**：`timing.json` 里的 `scenes[].start/end`、`voice.lines[].at` 是**全片绝对秒**；场景组件里的 `t = useSceneTime(name)` 是**本场景相对秒**（Promo 的 `<Sequence>` 已经减掉场景起点）。所以在场景里取卡点一律用 `useBeat()`：`beat.now / beat.at(i) / beat.barAt(i) / beat.snap(t, unit)` 全是**场景相对秒**（`energy/phase/onBeat` 也接受相对秒），要绝对秒就显式取 `beat.abs`。把 `snap()` / `beatAt()` 返回的绝对秒直接和 `t` 比大小，症状是**整场只剩字幕、画面全黑且没有任何报错**（示例模板第一版真的这么翻车过）。
- 所有画面尺寸用相对单位（`useVideoConfig()` 的宽高比、百分比），保证同一套场景在 9:16 与 16:9 下都不破版。
- 遵循第 4 节的节奏底线与密度要求；每个场景做完用 `npm run stills -- --scenes` 出图自查。

### 阶段 7 · 配音与字幕

```bash
python scripts/tts.py all        # 免费 edge-tts（默认）；--engine eleven 走 ElevenLabs（可选）
python scripts/tts.py audit      # 检查每句时长是否落在窗口内、有没有尾部拖音
```

- 旁白稿写进 `timing.json` 的 `voice.lines`，每句带 `id/scene/at/text`。语速按 **5.5~6.5 字/秒** 写，
  窗口 = 该场景可用时长；脚本会自动切静音、必要时最多加速 12%，并把结果写进 `audio-src/vo/report.json`。
- 字数超窗口的句子必须**改稿**而不是硬压：`tts.py audit` 报 `TOO_LONG` 就回去删字。
- 字幕：竖屏单行 ≤13 字、最多 2 行，屏上大字**最多比旁白早 1.5 秒**出现；停留时长 ≥ `字数 / 6 + 0.35` 秒
  （配音 5.5~6.5 字/秒，**字幕必须活到人声读完**，否则句尾还在念、字幕已经消失）。模板组件取
  `max(字数/8+0.4, 字数/6+0.35)`，并在场景结束前 0.15 秒收掉，避免和下一场景的字幕叠在一起。

### 阶段 8 · 混音

```bash
python scripts/mix.py            # 生成 public/audio/music.wav（= timing.json 的 music.file）
python scripts/mix.py --env      # 打印成品响度包络，检查人声是否被音乐盖住
```

硬性要求：每句人声比该处音乐高 **≥6 dB**（脚本里的 `vo_margin`）；母带 **-14 LUFS / 真峰值 -1 dBFS**；
片头 0.3 秒淡入、片尾 0.6 秒淡出；除转场瞬间外不允许出现 ≥1.0 秒的空档。

**母带的两种模式会让「打印的余量」和「成片的余量」不一致**：逐句余量是在**母带前**总线上量的，
而 -14 LUFS / 真峰值 -1 dBFS 只允许 13 dB 的 PLR（真峰值 − 响度）。母带前真峰值贴着 0 dBFS
（PLR > 13 dB）时，ffmpeg 的 `loudnorm` 会静默回落成**动态**模式，母带不再是整体增益，成片余量
会比打印值低 1~2 dB（`mix.py` 会打印一条 WARN 说这件事）。
- **光降 `music.gain` 不一定管用**：峰值大多是人声总线顶上去的。实测（模板示例、真实 edge-tts 人声）
  `gain` 从 1.0 降到 0.6，母带前响度只从 -18.0 变成 -19.25 LUFS，PLR 仍是 19.2 dB。
- 真正管用的两条路：① 给母带前总线过一遍限制器，把真峰值压到 -6 dBFS 以下；
  ② 接受这 1~2 dB 误差，**按打印余量再留 2 dB**（打印 ≥8 dB 才当安全交付）。
- 别为了凑 PLR 去压人声本身：人声小了，闸门和实听都会先出问题。

### 阶段 9 · 校验闸门（**不许跳步**）

```bash
python scripts/check_assets.py                      # 渲染前：缺文件/图片过小/字体缺字/混音不存在
npm run stills -- --scenes                          # 每场景抽 40% 与 80% 两帧
python scripts/media.py sheet out/stills --cols 4   # 拼成联系表
python scripts/check_video.py out/final-1080p.mp4   # 渲染后：规格/响度/空档/渲染日志报错 + 联系表
```

- **`check_assets.py` 与 `check_video.py` 必须 0 ERROR 才算通过**；WARN 要逐条说明为什么不影响交付。
- 抽帧联系表**必须逐张看过**，对照 `references/校验清单.md` 的审图项打勾（构图、字幕安全区、截图清晰度、品牌名拼写）。
- 渲染日志里出现 `cannot be decoded` / `Error:` / `failed to load` 一律按失败处理，即使渲染"成功结束"。

### 阶段 10 · 渲染与交付

```bash
npm run render:preview     # 30fps 预览，先给人看结构
npm run render:1080        # 1080×1920 正式版
npm run render:4k          # --scale=2 出 2160×3840（平台会二压，非必要可跳过）
npm run web:build && npm run web:serve    # 网页播放版
```

**模板仓库不带音频**：`timing.json` 里 `music.file` 指向的文件是 `mix.py` 的产物（在 `.gitignore` 里）。
没跑过混音就渲染，模板会**自动跳过音轨、渲出一支无声片**（不会报错，见 `src/Promo.tsx` 的
`MUSIC_FILE_EXISTS`）—— 缺音轨由渲染前的 `check_assets.py`（ERROR: 混音文件）和渲染后的
`check_video.py`（ERROR: 无音轨）拦下来。**别把无声片当成品交出去。**

交付时告诉用户三件事：成片路径、网页版地址、以及"哪里还可以改、改一处要重做哪些步骤"。

### 阶段 11 · 改版

把用户的意见**原话**记进 `需求确认.md`，判断影响范围，只重做必要步骤：

| 改动 | 需要重做 |
| --- | --- |
| 只改字幕文案 | 场景组件 → 重新渲染（不动音频） |
| 只换配音音色 | `tts.py` → `mix.py` → 替换成片的音轨（画面不重渲） |
| 改配乐 | `music.py` 重新分析 → 更新 `timing.json` 的 beat/bar/drop → 场景卡点微调 → 重渲 |
| 换风格 | 回到阶段 2 重出样帧 → 场景组件大改 → 全流程重跑 |

## 4. 节奏：底线与密度

**底线（保证看得清）**

| 项 | 数值 |
| --- | --- |
| 开场出钩子 | ≤2.0 秒内出现痛点或亮点 |
| 重点镜头 | ≥1.2 秒 |
| 快切镜头 | ≥0.5 秒 |
| 信息型场景 | ≥3.0 秒 |
| 网址 / CTA | ≥2.5 秒，且放在全片能量最高处 |
| 元素逐个出现 | 间隔 ≥0.25 秒，出齐后停留 ≥0.6 秒 |
| 同一镜头多个焦点 | 每个焦点 ≥1.2 秒 |

**密度（保证不拖沓）**

| 项 | 数值 |
| --- | --- |
| 新信息间隔 | 任意时刻往后 2.0 秒内必须有新信息 |
| 纯转场 / 飞镜 | ≤0.5 秒 |
| 同一版式 | 最多连用 2 次 |
| 单场景小节数 | ≤2 个（约 4 秒） |

竖屏安全区（相对画面高宽）：上 10%、下 18%、左 6%、右 14%（右侧留给平台互动按钮）。

## 5. `src/timing.json` 字段契约（**整支片子唯一事实来源**）

TS 侧（Root / Promo / 场景 / 动画库）与 Python 侧（tts / mix / music / check_*）**都读这一个文件**，
禁止任何一端硬编码时长、帧率或音色。字段含义如下，改动必须同步本表。

```jsonc
{
  "slug": "acme-app",
  "title": "Acme 记账",
  "fps": 60,
  "width": 1080,          // 竖屏 1080×1920；横版改 1920×1080
  "height": 1920,
  "aspect": "9:16",       // "9:16" | "16:9"
  "duration": 45,         // 秒，必须等于最后一个场景的 end
  "music": {
    "file": "audio/music.wav",  // 相对 public/；混音脚本会写出这个文件
    "source": "audio-src/music/track.mp3",
    "offset": 0.0,        // 视频时间 = 音乐时间 + offset
    "beat": 0.4615,       // 每拍秒数
    "bar": 1.846,         // 每小节秒数（通常 4 beat）
    "drop": 3.69,         // 第一次爆发的视频时间
    "gain": 0.6,             // 音乐床电平；人声余量不足先降它（PLR 过高多是人声峰值顶的，见阶段 8）。只在 mix.py 混音时生效，成片播放不要再乘一次
    "segments": [ { "at": 22.0, "from": 12.0, "to": 30.0 } ]  // 可选：剪接片段
  },
  "scenes": [             // 必须按 start 升序、首尾相接、不留缝
    { "name": "hook",  "start": 0.0,  "end": 4.5,  "note": "痛点开场" },
    { "name": "demo",  "start": 4.5,  "end": 26.0, "note": "三个真实界面" },
    { "name": "proof", "start": 26.0, "end": 36.0, "note": "真实作品蒙太奇" },
    { "name": "cta",   "start": 36.0, "end": 45.0, "note": "品牌 + 网址" }
  ],
  "pace": { "minShot": 0.5, "keyShot": 1.2, "sceneMin": 3.0, "stagger": 0.25, "holdAfterReveal": 0.6 },
  "voice": {
    "engine": "edge",                  // "edge"（免费）| "eleven"（需 key）
    "voice": "zh-CN-YunxiNeural",
    "rate": "+8%",                     // edge-tts 语速；eleven 用 stability 等参数
    "settings": { "stability": 0.45, "similarity_boost": 0.75 },
    "rmsDb": -13.0,
    "duckDb": -9.0,
    "lines": [
      { "id": "01", "scene": "hook", "at": 0.6, "text": "每天记账要花十分钟？" }
    ]
  },
  "captions": { "enabled": true, "maxChars": 13, "maxLines": 2, "bottom": 0.16 },
  "brand": { "name": "Acme", "url": "acme.com", "logo": "img/logo.png",
             "colors": { "bg": "#0B0B12", "fg": "#FFFFFF", "accent": "#33E1A1" } }
}
```

约定：`at`、`start`、`end`、`drop` 一律是**秒**（浮点）；`beat/bar` 是**秒**；帧数由脚本或组件用 `Math.round(秒 × fps)` 换算。

## 6. 技术栈与脚本速查

Node ≥18、Python ≥3.9、ffmpeg/ffprobe 是硬依赖。模板依赖：Remotion 4 + React 19 + Vite（网页播放版）；不引入 Tailwind、GSAP 等额外框架，画面用内联样式 + 少量全局 CSS。

| 脚本 | 用途 | 关键参数 |
| --- | --- | --- |
| `scripts/doctor.py` | 环境体检，只查不装 | 无 |
| `scripts/capture.py` | Playwright 真实网页截图（默认 2 倍图） | `<url> <name> [--factor 2] [--at 0,900,1800] [--full 3600] [--device iphone] [--hide 选择器] [--login] [--out out/shots]` |
| `scripts/media.py` | 素材整理：抓 Markdown 里全部图片、拼联系表、按实际裁切预览、限宽、列尺寸 | `grab <md...> -o <dir>` / `sheet <dir> [--cols 4]` / `crop <out> <file...> [--ratio 9:16]` / `prepare <src> <dst> [--max 2800]` / `info <dir>` |
| `scripts/music.py` | 配乐：下载/分析/找节拍/逐小节能量/剪段 | `analyze <file...>` / `grid <file> [--from --to]` / `bars <file> --beat --downbeat --offset` / `cut <in> <out> --start --dur` |
| `scripts/tts.py` | 配音：生成、多音色试听、时长与拖音审核 | `all` / `one <id>` / `audit` / `voices [--lang zh]` / `preview --text "..."` / `stt <file>`（需可选依赖） |
| `scripts/mix.py` | 混音：放人声、ducking、音效、母带 | `[--no-vo]` / `--env` |
| `scripts/check_assets.py` | 渲染前闸门 | `[--strict]` |
| `scripts/check_video.py` | 成片闸门 | `<mp4> [--expect 1080x1920] [--log out/render.log]` |
| `scripts/stills.mjs` | 抽帧 | `<秒...>` 或 `--scenes` `[--scale 0.5] [--keep]` |
| `scripts/sync_template.py` | 用新版模板更新已有项目 | `[--diff] [--apply]` |

## 7. 校验闸门与退出码

| 闸门 | 通过条件 |
| --- | --- |
| `doctor.py` | 无 MISSING（WARN 可接受） |
| `check_assets.py` | 0 ERROR；图片宽度在护栏内（<1000px 或 >3000px 会 WARN；「≥ 显示宽度 ×2」要人工按画面占比核对）；无缺字 |
| `check_video.py` | 规格与 `timing.json` 一致（±0.2 秒）；有音轨；响度 -16~-12 LUFS；无 ≥1.0 秒空档；渲染日志无报错 |
| `tts.py audit` | 无 `TOO_LONG` / `MISSING`（`ECHO` 需人工确认） |
| 人工审图 | 联系表逐张看过，`references/校验清单.md` 全部打勾 |

脚本一律：**发现问题就非零退出并打印 `ERROR: ...` 行**，不要静默通过。

## 8. 参考文档

| 文档 | 什么时候读 |
| --- | --- |
| `references/需求问询.md` | 阶段 2 开始前 |
| `references/风格方向.md` | 阶段 2 出样帧前 |
| `references/素材与截图.md` | 阶段 4 |
| `references/配乐与卡点.md` | 阶段 5 |
| `references/配音与字幕.md` | 阶段 7 |
| `references/校验清单.md` | 阶段 9，以及交付前 |
| `references/踩坑记录.md` | 开工前通读一遍，避免重复踩坑 |
| `references/案例复盘.md` | 开工前读，只学方法，不抄风格 |
