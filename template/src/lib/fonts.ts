/**
 * fonts.ts —— 字体角色解析 + 内嵌子集字体注册。
 *
 * 两条路线，启动时二选一，并在控制台打印用的是哪一条：
 *
 *   1. **内嵌子集字体**（推荐，换机器渲染也不缺字）
 *      `python scripts/fonts.py subset` 把全片字符从系统字体里切出来、放进 `public/fonts/`，
 *      并把路径写进 `fonts.json`。这里用 @remotion/fonts 注册成 webfont —— 字体随项目走，
 *      渲染机装没装中文字体都不影响出字。
 *   2. **系统字体栈**（模板默认，零配置）
 *      `fonts.json` 里没有可用的字体文件时，`FONT.*` 直接就是 `roles.*.stack` 那串 CSS 字体栈
 *      （苹方 / 微软雅黑 / 思源黑体…）。能不能出字，取决于渲染那台机器装了什么字体。
 *
 * 为什么必须先静态判断文件在不在：@remotion/fonts 的 loadFont() 加载失败时会调
 * `cancelRender(err)`（见 node_modules/@remotion/fonts/dist/esm/index.mjs），
 * 也就是**让整个渲染任务失败**，不是在页面里悄悄回退。而 `public/fonts/*` 在 .gitignore 里
 * —— 新克隆的项目里 fonts.json 记着子集字体、文件却不在，直接 loadFont 就会渲染失败。
 * 所以这里先用 `getStaticFiles()`（public/ 的打包快照）判断，文件不在就压根不调用 loadFont，
 * 直接用系统字体栈渲染，并在控制台说清原因。
 *
 * ❗ timing.json 的文案是中文：如果配了只含拉丁字库的字体，中文会退回系统字体，
 * 造成同一行里两种字形，看起来像 bug。放字体前先跑 `python scripts/fonts.py check`。
 */
import { loadFont } from "@remotion/fonts";
import { getStaticFiles, staticFile } from "remotion";
import fontsConfig from "../../fonts.json";

/** 三个角色。用 as const 是为了让 keys 在类型层面就是字面量，避免手滑写出 "titel"。 */
export const FONT_ROLES = ["display", "body", "mono"] as const;
export type FontRole = (typeof FONT_ROLES)[number];

type RawRole = { stack: string[]; weight: number; file: string };

/** fonts.json 是外部输入，做一次类型守卫，避免 JSON 被手改坏以后在渲染期炸开。 */
const isRawRole = (value: unknown): value is RawRole => {
  if (typeof value !== "object" || value === null) return false;
  const v = value as Record<string, unknown>;
  return (
    Array.isArray(v.stack) &&
    v.stack.every((s): s is string => typeof s === "string") &&
    typeof v.weight === "number" &&
    typeof v.file === "string"
  );
};

const rawRoles: Record<string, unknown> = fontsConfig.roles as Record<string, unknown>;

const resolveRole = (role: FontRole): RawRole => {
  const raw = rawRoles[role];
  if (isRawRole(raw)) return raw;
  // 字段缺失时给一个能出字的安全默认值（系统无衬线），而不是崩掉
  return { stack: ["system-ui", "sans-serif"], weight: role === "display" ? 800 : 400, file: "" };
};

/**
 * `fonts.json` 的 `subset.roles.<role>`（由 `python scripts/fonts.py subset` 写入）。
 * 这里读它有两个用处：① 手填的 `roles.<role>.file` 被清空时仍然知道子集字体在哪；
 * ② 拿到子集**真实的字面字重** —— display 的子集切自 "Microsoft YaHei Bold"（usWeightClass=700），
 * 声明成 700 后 CSS 的 `font-weight: 800` 才会命中这个粗体面，而不是拿常规面去合成假粗体。
 */
type RawSubsetRole = { file: string; faceWeight: number | null };

const rawSubsetRoles: Record<string, unknown> = (() => {
  const subset = (fontsConfig as unknown as Record<string, unknown>).subset;
  if (typeof subset !== "object" || subset === null) return {};
  const roles = (subset as Record<string, unknown>).roles;
  if (typeof roles !== "object" || roles === null) return {};
  return roles as Record<string, unknown>;
})();

const resolveSubsetRole = (role: FontRole): RawSubsetRole | null => {
  const raw = rawSubsetRoles[role];
  if (typeof raw !== "object" || raw === null) return null;
  const v = raw as Record<string, unknown>;
  const file = typeof v.file === "string" ? v.file.trim() : "";
  if (file === "") return null;
  const faceWeight =
    typeof v.faceWeight === "number" && Number.isFinite(v.faceWeight) && v.faceWeight > 0
      ? v.faceWeight
      : null;
  return { file, faceWeight };
};

/** 注册后的家族名。必须和系统字体名不一样，否则 CSS 无法区分"加载失败的 webfont"和"系统回退"。 */
const loadedFamily: Record<FontRole, string> = {
  display: "PromoDisplay",
  body: "PromoBody",
  mono: "PromoMono",
};

/** 是否真的注册了本地字体文件（供场景做轻微的排版退让，例如行高微调）。 */
export const HAS_LOCAL_FONT: Record<FontRole, boolean> = {
  display: false,
  body: false,
  mono: false,
};

const systemFamily = (role: FontRole): string => {
  const stack = resolveRole(role).stack;
  const list = stack.length > 0 ? stack : ["system-ui", "sans-serif"];
  return list.map((name) => (name.includes(" ") ? `"${name}"` : name)).join(", ");
};

/**
 * public/ 里的文件清单（打包时的快照）→ 相对 public/ 的路径 → 字节数。
 * 拿不到清单时是 null，`fileExists()` 会按"存在"处理 —— 与 Promo.tsx 的 MUSIC_FILE_EXISTS
 * 同一个取舍：宁可让渲染报错，也不要静默换成系统字体（那会安静地产出一支字形不对的片子）。
 */
const staticFiles: Map<string, number> | null = (() => {
  try {
    const map = new Map<string, number>();
    getStaticFiles().forEach((file) => {
      const name = String(file.name).replace(/\\/g, "/").replace(/^\/?public\//, "");
      const size = typeof file.sizeInBytes === "number" ? file.sizeInBytes : Number.NaN;
      map.set(name, size);
    });
    return map;
  } catch {
    return null;
  }
})();

const fileExists = (relPath: string): boolean => {
  if (staticFiles === null) return true; // 拿不到清单：按存在处理（同 MUSIC_FILE_EXISTS）
  const key = relPath.replace(/\\/g, "/").replace(/^\/?public\//, "");
  const size = staticFiles.get(key);
  if (size === undefined) return false;
  return Number.isNaN(size) ? true : size > 0; // 0 字节 = 占位文件（.gitkeep 之类），当不存在
};

type FontRoute = {
  /** 要注册的字体文件（相对 public/）；null = 这一路用系统字体栈 */
  file: string | null;
  /** 注册时声明的字重 */
  weight: number;
  /** 这个决定是怎么来的（打印用，排查时一眼看出为什么没用子集字体） */
  why: string;
};

/**
 * 决定一个角色走哪条路。优先级：手填的 `roles.<role>.file` → `subset.roles.<role>.file`
 * （`fonts.py subset` 生成的）→ 系统字体栈。前两个都要求文件真的在 public/ 里。
 */
const decide = (role: FontRole): FontRoute => {
  const roleWeight = resolveRole(role).weight;
  const manual = resolveRole(role).file.trim();
  const generated = resolveSubsetRole(role);
  const subsetFile = generated === null ? null : generated.file;

  const isSubset = (file: string): boolean =>
    subsetFile !== null && file.replace(/\\/g, "/") === subsetFile.replace(/\\/g, "/");

  const candidates: { file: string; why: string }[] = [];
  if (manual !== "") {
    candidates.push({
      file: manual,
      why: isSubset(manual)
        ? "fonts.py subset 生成（fonts.json 的 roles.<role>.file 记的就是它）"
        : "fonts.json roles.<role>.file（手填的字体）",
    });
  }
  if (subsetFile !== null && subsetFile !== manual) {
    candidates.push({ file: subsetFile, why: "fonts.py subset 生成（subset.roles.<role>.file）" });
  }

  for (const candidate of candidates) {
    if (!fileExists(candidate.file)) continue;
    const weight = isSubset(candidate.file) && generated?.faceWeight ? generated.faceWeight : roleWeight;
    const extra = isSubset(candidate.file) && generated?.faceWeight ? "（子集字体的真实字重）" : "";
    return { file: candidate.file, weight, why: `${candidate.why}${extra}` };
  }

  if (candidates.length > 0) {
    const missing = candidates.map((candidate) => candidate.file).join(" / ");
    return { file: null, weight: roleWeight, why: `记录的字体文件不在 public/ 里：${missing}` };
  }
  return { file: null, weight: roleWeight, why: "没有配置本地字体文件" };
};

const ROUTES: Record<FontRole, FontRoute> = {
  display: decide("display"),
  body: decide("body"),
  mono: decide("mono"),
};

/**
 * 场景里直接 `FONT.display` 当 fontFamily 用。
 * 内嵌字体永远写在**最前面**，系统字体栈跟在后面兜底：webfont 还没加载完时先用系统字体顶着，
 * 避免首帧空白（Remotion 抽帧正好抽到空白帧就白干了）。
 */
export const FONT: Record<FontRole, string> = {
  display:
    ROUTES.display.file === null
      ? systemFamily("display")
      : `"${loadedFamily.display}", ${systemFamily("display")}`,
  body:
    ROUTES.body.file === null
      ? systemFamily("body")
      : `"${loadedFamily.body}", ${systemFamily("body")}`,
  mono:
    ROUTES.mono.file === null
      ? systemFamily("mono")
      : `"${loadedFamily.mono}", ${systemFamily("mono")}`,
};

/**
 * 注册内嵌字体。放在模块顶层（而不是组件里）：一个页面只需要注册一次，
 * 放 effect 里会在每次场景重挂载时重复注册。loadFont 内部用 delayRender/continueRender
 * 阻塞渲染，字体没到位不会出空白帧。
 */
const register = (role: FontRole): void => {
  const route = ROUTES[role];
  if (route.file === null) return; // 系统字体栈：什么都不做
  HAS_LOCAL_FONT[role] = true;
  loadFont({
    family: loadedFamily[role],
    url: staticFile(route.file),
    weight: String(route.weight),
    // display: "block" —— 字体没到位时先用回退字体画，宁可字形不同也不要空白。
    // （选项名就是 display，不是 CSS 的 font-display；@remotion/fonts 的 LoadFontOptions 里
    //   没有 fontDisplay 这个键，写错了 TS 会直接报 unknown property。）
    display: "block",
  }).catch((err: unknown) => {
    // 走到这里说明文件在 public/ 里、但字节层面加载不了（截断 / 不是字体 / 解码失败）。
    // loadFont 内部已经 cancelRender 了，所以这次渲染多半会报错退出 —— 这是故意的：
    // 坏字体比缺字体更该被拦下来。这里把家族名也换回系统栈，并留下能对上号的日志。
    console.warn(
      `[fonts] ${role} 内嵌字体加载失败（${route.file}），回退系统字体栈：`,
      err,
      "\n        重新生成：python scripts/fonts.py subset",
    );
    FONT[role] = systemFamily(role);
  });
};

FONT_ROLES.forEach(register);

/**
 * 把"当前走的是哪一路"打印一次。模块顶层每次渲染页加载只跑一次，所以正好一次。
 * 抽帧/渲染日志里出现 [fonts] 就能确认这一版到底用的是内嵌子集还是系统字体。
 */
const reportRoute = (): void => {
  const embedded = FONT_ROLES.filter((role) => ROUTES[role].file !== null);
  const label =
    embedded.length === 0
      ? "系统字体栈（依赖渲染机的系统字体）"
      : embedded.length === FONT_ROLES.length
        ? `内嵌子集字体 ${embedded.length}/${FONT_ROLES.length}`
        : `内嵌子集字体 ${embedded.length}/${FONT_ROLES.length} + 系统字体栈`;
  console.info(`[fonts] 当前路线：${label}`);
  FONT_ROLES.forEach((role) => {
    const route = ROUTES[role];
    console.info(
      route.file === null
        ? `[fonts]   ${role}：系统字体栈 —— ${route.why}`
        : `[fonts]   ${role}：${route.file}（声明字重 ${route.weight}）—— ${route.why}`,
    );
  });
  if (staticFiles === null) {
    console.info("[fonts]   （拿不到 public/ 文件清单，按「存在」处理，见 src/lib/fonts.ts 注释）");
  }
};

reportRoute();

/** 字重也一并导出，省得场景里再写一遍 magic number（这是设计意图，不是字体文件的字重）。 */
export const FONT_WEIGHT: Record<FontRole, number> = {
  display: resolveRole("display").weight,
  body: resolveRole("body").weight,
  mono: resolveRole("mono").weight,
};
