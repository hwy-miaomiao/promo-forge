/**
 * theme.ts —— 调色板、安全区、字号阶梯、字体栈。
 *
 * 三条设计决策（都有原因，改之前先读）：
 *
 * 1) 颜色只允许来自 timing.brand.colors 的 bg/fg/accent 三个值，其余（dim/panel/border）
 *    在这里用混色推导出来。品牌方只要给三个色，整片视觉就是自洽的；场景里写死一个
 *    灰蓝色，换品牌时就会留下一块不属于新配色的脏斑。
 *
 * 2) 字号**只和画面宽度挂钩**（竖屏以 1080 为基准、横版以 1280 为基准）。
 *    ❗千万不要用 `min(vw, vh)`：1080×1920 与 1920×1080 的 min 都是 1080，
 *    结果横版标题会和竖屏一样大甚至更挤。宽高比优先的基准宽度才能"同一套场景不破版"。
 *
 * 3) 安全区是 AGENTS.md 第 4 节的硬要求（竖屏上 10% / 下 18% / 左 6% / 右 14%，
 *    右边留给平台的点赞评论按钮）。字幕、网址、CTA 一律不许越过它。
 */
import { useVideoConfig } from "remotion";
import { TIMING, type BrandColors } from "./timing";
import { FONT } from "./fonts";

/* ------------------------------------------------------------------ 颜色 */

type Rgb = { r: number; g: number; b: number };

/** 支持 #RGB / #RRGGBB / #RRGGBBAA；解析失败返回黑色，绝不抛错（渲染期抛错会毁掉整支片子）。 */
const parseHex = (hex: string): Rgb => {
  const s = hex.trim().replace(/^#/, "");
  const full = s.length === 3 ? s.split("").map((c) => c + c).join("") : s;
  if (!/^[0-9a-fA-F]{6}([0-9a-fA-F]{2})?$/.test(full)) return { r: 0, g: 0, b: 0 };
  return {
    r: Number.parseInt(full.slice(0, 2), 16),
    g: Number.parseInt(full.slice(2, 4), 16),
    b: Number.parseInt(full.slice(4, 6), 16),
  };
};

const toCss = (c: Rgb): string =>
  `#${[c.r, c.g, c.b].map((n) => Math.round(Math.min(255, Math.max(0, n))).toString(16).padStart(2, "0")).join("")}`;

/** 线性混色：t=0 取 a，t=1 取 b。 */
export const mix = (a: string, b: string, t: number): string => {
  const ca = parseHex(a);
  const cb = parseHex(b);
  const k = Math.min(1, Math.max(0, t));
  return toCss({ r: ca.r + (cb.r - ca.r) * k, g: ca.g + (cb.g - ca.g) * k, b: ca.b + (cb.b - ca.b) * k });
};

/** 带透明度的颜色（panel / border / 底衬都用它，保证任何底色上都能读）。 */
export const alpha = (hex: string, a: number): string => {
  const c = parseHex(hex);
  return `rgba(${Math.round(c.r)}, ${Math.round(c.g)}, ${Math.round(c.b)}, ${Math.min(1, Math.max(0, a))})`;
};

/** 相对亮度 0~1，用来决定叠在某个色块上的文字该用深色还是浅色。 */
export const luminance = (hex: string): number => {
  const c = parseHex(hex);
  return (0.2126 * c.r + 0.7152 * c.g + 0.0722 * c.b) / 255;
};

/** 在给定底色上挑一个可读的前景色。 */
export const readableOn = (bg: string): string => (luminance(bg) > 0.55 ? "#0B0B12" : "#FFFFFF");

export type Palette = {
  /** 画面底色 */
  bg: string;
  /** 主前景 */
  fg: string;
  /** 次级文字（正文说明、注释） */
  dim: string;
  /** 品牌强调色 */
  accent: string;
  /** 卡片 / 面板底色（比 bg 亮一档） */
  panel: string;
  /** 更亮的浮层面板 */
  panelStrong: string;
  /** 描边 */
  border: string;
  /** 强调色的浅色变体，用于发光 */
  accentSoft: string;
  /** 强调色的深色变体，用于按下/次要状态 */
  accentDeep: string;
};

/**
 * 调色板。bg 若是深色，fg 会自然成为亮色；若品牌选了浅底，panel/border 会自动反向推导，
 * 因此浅色主题的产品也能直接用同一套场景（AGENTS.md 明确禁止"所有产品都做成同一套深色科技风"）。
 */
export const PALETTE: Palette = ((): Palette => {
  const c: BrandColors = TIMING.brand.colors;
  const dark = luminance(c.bg) < 0.5;
  return {
    bg: c.bg,
    fg: c.fg,
    dim: mix(c.fg, c.bg, dark ? 0.42 : 0.48),
    accent: c.accent,
    panel: mix(c.bg, c.fg, dark ? 0.08 : 0.05),
    panelStrong: mix(c.bg, c.fg, dark ? 0.16 : 0.1),
    border: mix(c.bg, c.fg, dark ? 0.22 : 0.16),
    accentSoft: mix(c.accent, c.bg, dark ? 0.55 : 0.7),
    accentDeep: mix(c.accent, dark ? "#000000" : "#FFFFFF", 0.35),
  };
})();

/* ---------------------------------------------------------------- 安全区 */

/**
 * 归一化安全区（占画面宽/高的比例）。
 * 竖屏右侧 14% 是留给平台互动按钮的——把网址或字幕排到那儿，用户永远看不全。
 */
export const SAFE_INSET = {
  vertical: { top: 0.1, bottom: 0.18, left: 0.06, right: 0.14 },
  horizontal: { top: 0.06, bottom: 0.08, left: 0.06, right: 0.06 },
} as const;

export type SafeInset = { top: number; bottom: number; left: number; right: number };

/** 当前画面该用哪套安全区。竖屏判定优先看 aspect，再退回宽高比，两个都不可信时默认竖屏。 */
export const isVertical = (width: number, height: number): boolean => {
  if (TIMING.aspect === "16:9") return false;
  if (TIMING.aspect === "9:16") return true;
  return height >= width;
};

export const pickSafeInset = (width: number, height: number): SafeInset =>
  isVertical(width, height) ? SAFE_INSET.vertical : SAFE_INSET.horizontal;

/** 纯函数版：给定画面尺寸直接算出四边 padding（px）。脚本或非组件环境也能用。 */
export const safePadding = (width: number, height: number): SafeInset => {
  const i = pickSafeInset(width, height);
  return {
    top: Math.round(height * i.top),
    bottom: Math.round(height * i.bottom),
    left: Math.round(width * i.left),
    right: Math.round(width * i.right),
  };
};

/** 组件里用这个：按 useVideoConfig() 的宽高算好安全区 padding。 */
export const useSafeArea = (): SafeInset & { vertical: boolean; width: number; height: number } => {
  const { width, height } = useVideoConfig();
  const pad = safePadding(width, height);
  return { ...pad, vertical: isVertical(width, height), width, height };
};

/** 兼容写法：`SAFE.vertical.top` 这类读取方式。 */
export const SAFE = {
  ...SAFE_INSET,
  /** 安全区内的最大内容宽度（占画面宽比例）：竖屏 84%，横版 68% 居中 */
  contentWidth: { vertical: 0.84, horizontal: 0.68 },
} as const;

/* ------------------------------------------------------------------ 字号 */

const TYPE_BASE_WIDTH = { vertical: 1080, horizontal: 1280 } as const;

type TypeStep = { size: number; weight: number; lineHeight: number; letterSpacing: string };

const typeScale = (base: number): Record<string, TypeStep> => ({
  /** 只在片尾品牌名/开场超级大字上用 */
  hero: { size: base * 0.089, weight: 800, lineHeight: 1.04, letterSpacing: "-0.02em" },
  /** 场景主标题 */
  title: { size: base * 0.0667, weight: 800, lineHeight: 1.12, letterSpacing: "-0.01em" },
  /** 小节标题 */
  heading: { size: base * 0.048, weight: 700, lineHeight: 1.2, letterSpacing: "0" },
  /** 副标题 / 说明句 */
  subtitle: { size: base * 0.0407, weight: 600, lineHeight: 1.35, letterSpacing: "0" },
  /** 正文 */
  body: { size: base * 0.0315, weight: 400, lineHeight: 1.5, letterSpacing: "0.01em" },
  /** 屏上大字 / 字幕：比正文大一档，手机上一眼能读完 */
  caption: { size: base * 0.0481, weight: 800, lineHeight: 1.28, letterSpacing: "0.01em" },
  /** 标签、角标 */
  label: { size: base * 0.0241, weight: 600, lineHeight: 1.3, letterSpacing: "0.08em" },
  /** 极小辅助文字（页脚、注释） */
  tiny: { size: base * 0.0204, weight: 400, lineHeight: 1.4, letterSpacing: "0.06em" },
});

export type TypeScale = ReturnType<typeof typeScale>;

/** 默认字号阶梯（竖屏基准）。场景里优先用 useType()，跑马灯等处可直接读它。 */
export const TYPE: TypeScale = typeScale(TYPE_BASE_WIDTH.vertical);

/**
 * 按当前画面算出字号阶梯。横版用 1280 而不是 1920 做基准：
 * 1920 宽的画面通常被当桌面网页看，视距更近，字反而要收敛一点。
 */
export const useType = (): TypeScale => {
  const { width, height } = useVideoConfig();
  const base = isVertical(width, height) ? width || TYPE_BASE_WIDTH.vertical : TYPE_BASE_WIDTH.horizontal;
  return typeScale(base);
};

/** 兼容按宽度取字号：脚本或 Shot 内部需要"和画面等比"的数值时用它。 */
export const typeFor = (width: number, height: number): TypeScale =>
  typeScale(isVertical(width, height) ? width || TYPE_BASE_WIDTH.vertical : TYPE_BASE_WIDTH.horizontal);

/* ------------------------------------------------------------------ 字体 */

export { FONT };
