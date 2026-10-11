/**
 * fonts.ts —— 字体角色解析与本地字体注册。
 *
 * 设计原则：**没有字体文件也必须能跑**。
 * 模板默认 `fonts.json` 里三个角色的 `file` 都是空串，靠系统字体栈（思源/苹方/微软雅黑）出字。
 * 只有用户把字体文件放进 `public/fonts/` 并填了 `file` 字段，才用 @remotion/fonts 注册真正的
 * webfont —— 这么做是因为 Chromium 渲染时的字体回退在 Linux 无头环境下会缺字形，
 * 但强制要求用户准备字体文件又会把"零付费开箱可用"这条给废掉，所以做成可选。
 *
 * ❗ timing.json 的文案是中文：如果填了 `file` 却只放了拉丁字库，中文会退回系统字体，
 * 造成同一行里两种字形，看起来像 bug。放字体前先在 `check_assets.py` 里过一次缺字检查。
 */
import { loadFont } from "@remotion/fonts";
import { staticFile } from "remotion";
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

/** 注册后的家族名。必须和系统字体名不一样，否则 CSS 无法区分"加载失败的 webfont"和"系统回退"。 */
const loadedFamily: Record<FontRole, string> = {
  display: "PromoDisplay",
  body: "PromoBody",
  mono: "PromoMono",
};

/** 是否需要外部字体文件（供场景做轻微的排版退让，例如行高微调）。 */
export const HAS_LOCAL_FONT: Record<FontRole, boolean> = {
  display: false,
  body: false,
  mono: false,
};

/** 把角色解析成最终 CSS font-family 字符串。 */
const buildFamily = (role: FontRole): string => {
  const { stack, file } = resolveRole(role);
  const system = stack.map((name) => (name.includes(" ") ? `"${name}"` : name)).join(", ");
  if (file.trim() === "") return system;
  // ❗ 先写系统字体再写 webfont：webfont 没加载完时先用系统字体顶着，
  // 避免首帧空白（Remotion 抽帧正好抽到空白帧就白干了）。
  return `"${loadedFamily[role]}", ${system}`;
};

/**
 * 注册本地字体。放在模块顶层（而不是组件里）：
 * 一个页面只需要注册一次，放 effect 里会在每次场景重挂载时重复注册。
 *
 * @remotion/fonts 内部用 delayRender/continueRender 阻塞渲染，
 * 文件不存在时只会让 tsc 之外的工具（check_assets.py）报警告，不会让画面崩掉。
 */
const register = (role: FontRole): void => {
  const { file, weight } = resolveRole(role);
  const trimmed = file.trim();
  if (trimmed === "") return; // 空 = 用系统字体栈，什么都不做
  loadFont({
    family: loadedFamily[role],
    url: staticFile(trimmed),
    weight: String(weight),
    // display: "block" —— 字体没到位时先用回退字体画，宁可字形不同也不要空白。
    // （选项名就是 display，不是 CSS 的 font-display；@remotion/fonts 的 LoadFontOptions 里
    //   没有 fontDisplay 这个键，写错了 TS 会直接报 unknown property。）
    display: "block",
  }).catch((err: unknown) => {
    // 字体加载失败不该毁掉整支片子：打印出来，继续用系统字体渲染
    console.warn(`[fonts] ${role} 字体加载失败，回退系统字体：`, err);
  });
  HAS_LOCAL_FONT[role] = true;
};

FONT_ROLES.forEach(register);

/**
 * 场景里直接 `FONT.display` 当 fontFamily 用。
 * 注意：本地字体加载是异步的，同一次渲染内可能先出系统字形；
 * 需要严格一致时先在 Studio 里跑一遍预热，或在 timing.json 之外固定字体文件。
 */
export const FONT: Record<FontRole, string> = {
  display: buildFamily("display"),
  body: buildFamily("body"),
  mono: buildFamily("mono"),
};

/** 字重也一并导出，省得场景里再写一遍 magic number。 */
export const FONT_WEIGHT: Record<FontRole, number> = {
  display: resolveRole("display").weight,
  body: resolveRole("body").weight,
  mono: resolveRole("mono").weight,
};
