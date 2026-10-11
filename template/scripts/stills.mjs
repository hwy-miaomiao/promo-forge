#!/usr/bin/env node
/**
 * promo-forge · 阶段 9「校验闸门」之抽帧（stills.mjs）。
 *
 * 用途
 * ----
 * 用 Remotion 自己的渲染管线把成片里指定的若干时刻抽成 JPEG，供人肉眼审图
 * （AGENTS.md 阶段 9：抽帧联系表**必须逐张看过**）。因为走的是真渲染，抽出来的
 * 画面和成片完全一致，不会像"播放器截图"那样有色差或时间偏移。
 *
 * 典型调用
 * --------
 *   node scripts/stills.mjs --scenes                 # 每个场景取 40% 与 80% 两个时刻
 *   node scripts/stills.mjs 0.5 3.2 12.5             # 指定秒数
 *   node scripts/stills.mjs --scenes --scale 0.5     # 半分辨率快速自查（推荐）
 *   node scripts/stills.mjs --scenes --keep          # 保留 out/stills 里已有的图
 *   node scripts/stills.mjs --comp Promo --quality 85
 *
 * 参数
 * ----
 *   <秒...>            要抽的时刻（视频时间，秒）
 *   --scenes           对 timing.json 的每个场景取 40% 与 80% 两个时刻
 *   --scale <n>        传给 renderStill 的 scale，默认 0.5（1080×1920 → 540×960）
 *   --comp <id>        composition id，默认 Promo（见 src/index.ts 的 Root）
 *   --out <dir>        输出目录，默认 out/stills
 *   --keep             不清空输出目录
 *   --quality <n>      JPEG 质量，默认 85
 *   --entry <path>     入口文件，默认 src/index.ts
 *   --public <dir>     publicDir，默认 public
 *   --timeout <ms>     单帧渲染超时，默认 120000
 *   -h, --help         显示帮助
 *
 * 依赖
 * ----
 * Node ≥18、已 `npm install`（或 `pnpm install`）的 node_modules：
 * `@remotion/bundler` 与 `@remotion/renderer`，以及 Remotion 需要的 Chrome Headless Shell
 * （缺了会自动下载；卡住可用 `npx remotion browser ensure` 手动装）。
 * 可选：`src/timing.json`（只有 --scenes 才需要）。
 *
 * 退出码
 * ------
 * 0 成功；1 出错（一律打印 `ERROR: ...` 到 stderr）。可继续但需注意的打印 `WARN: ...`。
 *
 * 文件名
 * ------
 * 固定宽度的时间戳 `%06.2f.jpg`，例如 `0004.20.jpg`、`0012.50.jpg`，
 * 方便 `python scripts/media.py sheet out/stills --cols 4` 与 check_video.py 拼联系表时排序。
 */

import fs from "node:fs";
import path from "node:path";
import process from "node:process";
import { fileURLToPath } from "node:url";

// ---------------------------------------------------------------------------
// 常量（每条都对应 AGENTS.md 或 template 里的一处约定）
// ---------------------------------------------------------------------------

const DEFAULT_COMP = "Promo";                 // src/index.ts 里注册的 composition id
const DEFAULT_OUT = "out/stills";             // 与 package.json 的 npm run sheet 一致
const DEFAULT_SCALE = 0.5;                    // 自查用半分辨率，够看清构图
const DEFAULT_QUALITY = 85;
const DEFAULT_ENTRY = "src/index.ts";
const DEFAULT_PUBLIC = "public";
const DEFAULT_TIMEOUT_MS = 120000;
const BUNDLE_DIR = "out/.remotion-bundle";    // 打包产物，out/ 已被 .gitignore 忽略
const SCENE_PCTS = [0.4, 0.8];                // AGENTS.md 阶段 9：每场景 40% 与 80% 各一帧
const CHROMIUM_GL = "angle";                  // 与 remotion.config.ts 的 setChromiumOpenGlRenderer 一致

const HELP = `promo-forge 抽帧工具（stills.mjs）

用法：
  node scripts/stills.mjs --scenes [选项]
  node scripts/stills.mjs <秒...> [选项]

选项：
  --scenes          对 timing.json 的每个场景取 40% 与 80% 两个时刻
  --scale <n>       renderStill 的缩放，默认 ${DEFAULT_SCALE}
  --comp <id>       composition id，默认 ${DEFAULT_COMP}
  --out <dir>       输出目录，默认 ${DEFAULT_OUT}
  --keep            不清空输出目录
  --quality <n>     JPEG 质量 1-100，默认 ${DEFAULT_QUALITY}
  --entry <path>    入口文件，默认 ${DEFAULT_ENTRY}
  --public <dir>    publicDir，默认 ${DEFAULT_PUBLIC}
  --timeout <ms>    单帧渲染超时，默认 ${DEFAULT_TIMEOUT_MS}
  -h, --help        显示本帮助

退出码：0 成功；1 出错（打印 ERROR: ...）。
`;

// ---------------------------------------------------------------------------
// 输出小工具（Windows 控制台默认 GBK，中文容易乱码；这里只管往 stdout/stderr 写）
// ---------------------------------------------------------------------------

function log(message) {
  process.stdout.write(String(message) + "\n");
}

function warn(message) {
  process.stdout.write("WARN: " + String(message) + "\n");
}

function fail(message) {
  process.stderr.write("ERROR: " + String(message) + "\n");
  process.exit(1);
}

function rel(target) {
  const relative = path.relative(process.cwd(), target);
  return (relative && !relative.startsWith("..") ? relative : target).split(path.sep).join("/");
}

function fmtDuration(ms) {
  return ms < 1000 ? `${Math.round(ms)}ms` : `${(ms / 1000).toFixed(2)}s`;
}

/** 固定宽度的时间戳文件名：4.2 → "0004.20"。 */
function stamp(seconds) {
  const text = Math.max(0, seconds).toFixed(2);
  const [intPart, fracPart] = text.split(".");
  return `${intPart.padStart(6, "0")}.${fracPart}`;
}

// ---------------------------------------------------------------------------
// 参数解析
// ---------------------------------------------------------------------------

function parseArgs(argv) {
  const options = {
    seconds: [],
    scenes: false,
    scale: DEFAULT_SCALE,
    comp: DEFAULT_COMP,
    out: DEFAULT_OUT,
    keep: false,
    quality: DEFAULT_QUALITY,
    entry: DEFAULT_ENTRY,
    publicDir: DEFAULT_PUBLIC,
    timeout: DEFAULT_TIMEOUT_MS,
    help: false,
  };

  const needValue = (flag, index) => {
    const value = argv[index + 1];
    if (value === undefined || value.startsWith("--")) {
      fail(`${flag} 后面要跟一个值。用 -h 看用法。`);
    }
    return value;
  };

  for (let i = 0; i < argv.length; i += 1) {
    const arg = argv[i];
    if (arg === "-h" || arg === "--help") {
      options.help = true;
    } else if (arg === "--scenes") {
      options.scenes = true;
    } else if (arg === "--keep") {
      options.keep = true;
    } else if (arg === "--scale") {
      const value = Number(needValue(arg, i));
      i += 1;
      if (!Number.isFinite(value) || value <= 0) fail(`--scale 要是正数，收到 ${argv[i]}`);
      options.scale = value;
    } else if (arg === "--quality") {
      const value = Number(needValue(arg, i));
      i += 1;
      if (!Number.isFinite(value) || value < 1 || value > 100) {
        fail(`--quality 要在 1~100 之间，收到 ${argv[i]}`);
      }
      options.quality = Math.round(value);
    } else if (arg === "--timeout") {
      const value = Number(needValue(arg, i));
      i += 1;
      if (!Number.isFinite(value) || value <= 0) fail(`--timeout 要是正数毫秒，收到 ${argv[i]}`);
      options.timeout = Math.round(value);
    } else if (arg === "--comp") {
      options.comp = needValue(arg, i);
      i += 1;
    } else if (arg === "--out") {
      options.out = needValue(arg, i);
      i += 1;
    } else if (arg === "--entry") {
      options.entry = needValue(arg, i);
      i += 1;
    } else if (arg === "--public") {
      options.publicDir = needValue(arg, i);
      i += 1;
    } else if (arg.startsWith("--")) {
      fail(`不认识的参数 ${arg}。用 -h 看用法。`);
    } else {
      const value = Number(arg);
      if (!Number.isFinite(value) || value < 0) {
        fail(`"${arg}" 不是合法的秒数。要抽指定时刻就写成 3.2 12.5 这样；要逐场景抽帧用 --scenes。`);
      }
      options.seconds.push(value);
    }
  }
  return options;
}

// ---------------------------------------------------------------------------
// 读取 timing.json（只有 --scenes 需要）
// ---------------------------------------------------------------------------

function readTiming(projectRoot) {
  const timingPath = path.join(projectRoot, "src", "timing.json");
  if (!fs.existsSync(timingPath)) {
    fail(
      `找不到 ${rel(timingPath)}。--scenes 需要它来确定每个场景的时间窗；` +
        `换成直接指定秒数（例如 node scripts/stills.mjs 0.5 6.0）也可以。`,
    );
  }
  let timing;
  try {
    timing = JSON.parse(fs.readFileSync(timingPath, "utf8"));
  } catch (error) {
    fail(`解析 ${rel(timingPath)} 失败：${error.message}。请修好 JSON 再重跑。`);
  }
  return timing;
}

function sceneMarks(timing) {
  const scenes = Array.isArray(timing && timing.scenes) ? timing.scenes : [];
  if (scenes.length === 0) {
    fail("timing.json 的 scenes 是空的，没法按场景抽帧。请先填好 scenes 再跑 --scenes。");
  }
  const marks = [];
  for (const scene of scenes) {
    const name = String((scene && scene.name) || "?");
    const start = Number(scene && scene.start);
    const end = Number(scene && scene.end);
    if (!Number.isFinite(start) || !Number.isFinite(end) || end <= start) {
      warn(`场景「${name}」的 start/end 不合法（${scene && scene.start} ~ ${scene && scene.end}），跳过`);
      continue;
    }
    for (const pct of SCENE_PCTS) {
      marks.push({
        seconds: start + (end - start) * pct,
        label: `场景「${name}」${Math.round(pct * 100)}%`,
      });
    }
  }
  if (marks.length === 0) fail("一个可抽帧的场景都没有，请检查 timing.json 的 scenes。");
  return marks;
}

// ---------------------------------------------------------------------------
// 依赖加载
// ---------------------------------------------------------------------------

async function loadRemotion() {
  let bundler;
  let renderer;
  try {
    bundler = await import("@remotion/bundler");
  } catch (error) {
    failWithInstallHint("@remotion/bundler", error);
  }
  try {
    renderer = await import("@remotion/renderer");
  } catch (error) {
    failWithInstallHint("@remotion/renderer", error);
  }
  return { bundle: bundler.bundle, selectComposition: renderer.selectComposition, renderStill: renderer.renderStill };
}

function failWithInstallHint(pkg, error) {
  const code = error && error.code ? `（${error.code}）` : "";
  fail(
    `缺少 npm 依赖 ${pkg}${code}。请先在项目根目录安装依赖：\n` +
      `       pnpm install        # 没装 pnpm 就 npm install\n` +
      `    国内网络慢可以设镜像后重试：npm config set registry https://registry.npmmirror.com\n` +
      `    原始错误：${error && error.message ? error.message : error}`,
  );
}

// ---------------------------------------------------------------------------
// main
// ---------------------------------------------------------------------------

async function main() {
  const options = parseArgs(process.argv.slice(2));
  if (options.help) {
    process.stdout.write(HELP);
    return 0;
  }

  const projectRoot = process.cwd();
  const entryPoint = path.resolve(projectRoot, options.entry);
  const publicDir = path.resolve(projectRoot, options.publicDir);
  const outDir = path.resolve(projectRoot, options.out);

  if (!fs.existsSync(entryPoint)) {
    fail(
      `找不到入口文件 ${rel(entryPoint)}。请在项目根目录（含 src/timing.json 与 node_modules）下运行本脚本，` +
        `或用 --entry 指定入口。`,
    );
  }
  if (!fs.existsSync(publicDir)) {
    warn(`publicDir ${rel(publicDir)} 不存在，画面里 staticFile() 引用的素材会读不到；继续打包。`);
  }

  const { bundle, selectComposition, renderStill } = await loadRemotion();

  // ---- 要抽哪些时刻 ----
  let marks = options.seconds.map((seconds) => ({ seconds, label: "" }));
  if (options.scenes) {
    const timing = readTiming(projectRoot);
    marks = marks.concat(sceneMarks(timing).map((mark) => ({ seconds: mark.seconds, label: mark.label })));
  }
  if (marks.length === 0) {
    fail("没有指定要抽的时刻。用法：node scripts/stills.mjs --scenes，或 node scripts/stills.mjs 0.5 3.2。");
  }

  // 同一时刻只抽一次（--scenes 与手写秒数可能重叠）
  const seen = new Map();
  for (const mark of marks) {
    const key = mark.seconds.toFixed(2);
    const existing = seen.get(key);
    if (existing) {
      if (mark.label && !existing.label.includes(mark.label)) {
        existing.label = existing.label ? `${existing.label} / ${mark.label}` : mark.label;
      }
      continue;
    }
    seen.set(key, mark);
  }
  const unique = [...seen.values()].sort((a, b) => a.seconds - b.seconds);

  // ---- 输出目录 ----
  if (fs.existsSync(outDir) && !options.keep) {
    for (const entry of fs.readdirSync(outDir)) {
      fs.rmSync(path.join(outDir, entry), { recursive: true, force: true });
    }
  }
  fs.mkdirSync(outDir, { recursive: true });

  const inputProps = { withAudio: false };
  const bundleDir = path.resolve(projectRoot, BUNDLE_DIR);

  log(`抽帧：${rel(entryPoint)} → ${rel(outDir)}`);
  log(`  composition=${options.comp}  scale=${options.scale}  quality=${options.quality}  ${unique.length} 帧`);
  log("");

  // ---- 1. 打包（Remotion 把整个工程打成一个可服务的 bundle） ----
  let serveUrl;
  const bundleStarted = Date.now();
  try {
    let lastLine = "";
    serveUrl = await bundle({
      entryPoint,
      publicDir,
      outDir: bundleDir,
      onProgress: (progress) => {
        const line = `打包中 ${Math.round(progress * 100)}%`;
        if (line !== lastLine) {
          lastLine = line;
          process.stdout.write(`\r${line}   `);
        }
      },
    });
  } catch (error) {
    process.stdout.write("\n");
    fail(
      `打包失败：${error && error.message ? error.message : error}\n` +
        `    常见原因：src/index.ts 语法错误、引用了不存在的模块、或 public/ 下有读不了的文件。` +
        `先跑 \`npm run lint\` 看类型错误。`,
    );
  }
  process.stdout.write("\r");
  log(`打包完成（${fmtDuration(Date.now() - bundleStarted)}）→ ${rel(serveUrl)}`);

  // ---- 2. 选 composition（拿真实 fps 与尺寸，秒 → 帧换算以它为准） ----
  let composition;
  try {
    composition = await selectComposition({ serveUrl, id: options.comp, inputProps });
  } catch (error) {
    fail(
      `选不到 composition「${options.comp}」：${error && error.message ? error.message : error}\n` +
        `    请确认 src/index.ts 的 <Composition id="${options.comp}"> 存在，或用 --comp 指定正确的 id。`,
    );
  }
  const fps = Number(composition.fps) || 30;
  log(
    `composition「${composition.id}」${composition.width}×${composition.height} ` +
      `${fps}fps ${composition.durationInFrames} 帧（${(composition.durationInFrames / fps).toFixed(2)}s）`,
  );
  log("");

  // ---- 3. 逐帧渲染 ----
  const results = [];
  let failures = 0;
  for (const mark of unique) {
    const frame = Math.max(0, Math.min(composition.durationInFrames - 1, Math.round(mark.seconds * fps)));
    const output = path.join(outDir, `${stamp(mark.seconds)}.jpg`);
    const started = Date.now();
    try {
      await renderStill({
        composition,
        serveUrl,
        output,
        inputProps,
        frame,
        scale: options.scale,
        imageFormat: "jpeg",
        jpegQuality: options.quality,
        overwrite: true,
        chromiumOptions: { gl: CHROMIUM_GL },
        timeoutInMilliseconds: options.timeout,
      });
    } catch (error) {
      failures += 1;
      warn(
        `${mark.label || `${mark.seconds.toFixed(2)}s`} 抽帧失败：` +
          `${error && error.message ? error.message : error}`,
      );
      continue;
    }
    const took = Date.now() - started;
    results.push({ mark, output, frame, took });
    log(
      `  ${rel(output)}  ← ${mark.seconds.toFixed(2)}s (第 ${frame} 帧)` +
        `${mark.label ? `  ${mark.label}` : ""}  ${fmtDuration(took)}`,
    );
  }

  log("");
  if (results.length === 0) {
    fail(`一帧都没抽出来（${failures} 个失败）。如果是浏览器缺失，跑 \`npx remotion browser ensure\` 后重试。`);
  }
  const total = results.reduce((sum, item) => sum + item.took, 0);
  log(`抽帧完成：${results.length}/${unique.length} 帧 → ${rel(outDir)}（合计 ${fmtDuration(total)}）`);
  if (failures > 0) {
    warn(`${failures} 帧失败，见上面的 WARN 行。`);
  }
  log(`下一步：python scripts/media.py sheet ${rel(outDir)} --cols 4，然后逐张审图对照 references/校验清单.md`);
  log("成片渲染后再跑 python scripts/check_video.py out/final-1080p.mp4 —— 它会出带场景标签的联系表并复查响度/空档。");
  return 0;
}

main()
  .then((code) => process.exit(code))
  .catch((error) => {
    fail(`未预料的错误：${error && error.stack ? error.stack : error}`);
  });
