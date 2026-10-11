/**
 * anim.ts —— 与时间无关的纯动画函数库。
 *
 * 为什么要有这个文件：
 * Remotion 里所有画面都是 `当前帧 -> 样式` 的纯函数。把常用曲线收敛到这里，
 * 场景文件里就只剩"第几秒该出现什么"，改节奏时不用去动几十处 interpolate。
 *
 * 约定：本文件里所有 `t` 都是**秒**（浮点）。帧转秒由调用方用 `useCurrentFrame()/fps` 完成。
 * 本文件不做任何 React 调用，也不 import 任何 timings，因此可以自由复用与单测。
 */
import { random } from "remotion";
import type { CSSProperties } from "react";

/** 缓动：既接受 CSS cubic-bezier 字符串（浏览器原生最平滑），也接受自定义函数。 */
export type Easing = string | ((t: number) => number);

/**
 * 六条缓动。
 * 为什么不用 remotion 的 Easing 对象：字符串可以同时喂给 CSS `transition-timing-function`，
 * 让"入场用 JS 算、hover/过渡交给 CSS"的写法共用同一条曲线，避免两处手感不一致。
 */
export const EASE = {
  /** 出场快、收尾慢：默认的入场曲线，最像"东西被轻轻放下" */
  out: "cubic-bezier(0.16, 1, 0.3, 1)",
  /** 两头慢中间快：适合位移幅度大的横移/飞入 */
  inOut: "cubic-bezier(0.65, 0, 0.35, 1)",
  /** 极软的收尾：数字滚动、发光呼吸，避免机械感 */
  soft: "cubic-bezier(0.33, 1, 0.68, 1)",
  /** 起步慢、后段加速：退场、被"甩出去"的元素 */
  in: "cubic-bezier(0.5, 0, 0.9, 0.3)",
  /** 过冲回弹：强调"落地"，只在大元素进场时用 */
  back: "cubic-bezier(0.34, 1.56, 0.64, 1)",
  /** 线性：进度条、计数器这类匀速量 */
  linear: "linear",
} as const;

/** 把 cubic-bezier 字符串换算成函数，供不方便走 CSS 的场合（如逐帧数值）使用。 */
const bezierCache = new Map<string, (t: number) => number>();

/** 解析 `cubic-bezier(a,b,c,d)`；解析失败则退化成线性，绝不抛错（渲染期抛错会毁掉整支片子）。 */
const parseCubicBezier = (str: string): ((t: number) => number) | null => {
  const m = /cubic-bezier\(([^)]+)\)/.exec(str);
  if (!m) return null;
  const nums = m[1].split(",").map((s) => Number.parseFloat(s.trim()));
  if (nums.length !== 4 || nums.some((n) => !Number.isFinite(n))) return null;
  const [x1, y1, x2, y2] = nums;

  // 牛顿迭代求 x(u)=t 的 u，再取 y(u)。20 次足够，视觉上看不出误差。
  const bez = (u: number, p1: number, p2: number): number => {
    const v = 1 - u;
    return 3 * v * v * u * p1 + 3 * v * u * u * p2 + u * u * u;
  };
  const bezPrime = (u: number, p1: number, p2: number): number => {
    const v = 1 - u;
    return 3 * v * v * p1 + 6 * v * u * (p2 - p1) + 3 * u * u * (1 - p2);
  };
  return (t: number): number => {
    let u = t;
    for (let i = 0; i < 20; i++) {
      const x = bez(u, x1, x2) - t;
      if (Math.abs(x) < 1e-6) break;
      const d = bezPrime(u, x1, x2);
      if (Math.abs(d) < 1e-6) break;
      u -= x / d;
      u = Math.min(1, Math.max(0, u));
    }
    return bez(u, y1, y2);
  };
};

/** 把 Easing 统一成函数形式（字符串走缓存，避免每帧重新解析正则）。 */
export const ease = (fn?: Easing): ((t: number) => number) => {
  if (typeof fn === "function") return fn;
  if (typeof fn !== "string" || fn === "linear") return (t) => t;
  const hit = bezierCache.get(fn);
  if (hit) return hit;
  const parsed = parseCubicBezier(fn) ?? ((t: number) => t);
  bezierCache.set(fn, parsed);
  return parsed;
};

/** 夹取到 [min, max]；min > max 时自动交换，避免手滑写出空区间。 */
export const clamp = (v: number, min = 0, max = 1): number => {
  const lo = Math.min(min, max);
  const hi = Math.max(min, max);
  return v < lo ? lo : v > hi ? hi : v;
};

/** 线性插值。t 不夹取（需要夹取请用 ramp）。 */
export const lerp = (a: number, b: number, t: number): number => a + (b - a) * t;

/**
 * 区间插值（本项目最常用的一条）。
 * ramp(t, [1.2, 1.8], [0, 1]) —— 1.2 秒前恒为 0，1.8 秒后恒为 1，中间走缓动。
 * 为什么不用 remotion 的 interpolate：它的默认外推行为要显式传两个 extrapolate 参数，
 * 场景里写十几次太吵，而且我们要把缓动字符串内联进来。
 */
export const ramp = (
  t: number,
  range: readonly [number, number],
  out: readonly [number, number],
  easing?: Easing,
): number => {
  const [a, b] = range;
  const [from, to] = out;
  if (b === a) return t < a ? from : to;
  const p = clamp((t - a) / (b - a), 0, 1);
  return lerp(from, to, ease(easing)(p));
};

/** 多段折线：inputs 必须与 outputs 等长，逐段线性（可各自夹取）。常用于关键帧式编排。 */
export const keys = (t: number, inputs: readonly number[], outputs: readonly number[]): number => {
  if (inputs.length === 0 || outputs.length === 0) return 0;
  if (outputs.length === 1 || inputs.length === 1) return outputs[0];
  if (t <= inputs[0]) return outputs[0];
  const last = Math.min(inputs.length, outputs.length) - 1;
  if (t >= inputs[last]) return outputs[last];
  for (let i = 0; i < last; i++) {
    if (t >= inputs[i] && t <= inputs[i + 1]) {
      const span = inputs[i + 1] - inputs[i];
      const p = span === 0 ? 0 : (t - inputs[i]) / span;
      return lerp(outputs[i], outputs[i + 1], p);
    }
  }
  return outputs[last];
};

/**
 * 两段式进出：inA→inB 从 0 涨到 1，outA→outB 从 1 落回 0，中间恒为 1。
 * 用来一次写完"淡入 + 保持 + 淡出"，避免每个元素写两遍 ramp。
 */
export const inOut = (t: number, inA: number, inB: number, outA: number, outB: number): number =>
  ramp(t, [inA, inB], [0, 1], EASE.out) * ramp(t, [outA, outB], [1, 0], EASE.in);

/** 冲量：at 时刻瞬间到 1，之后按 decay 秒指数衰减到 0。用于闪光、震屏、脉冲。 */
export const impulse = (t: number, at: number, decay = 0.35): number => {
  if (t < at) return 0;
  if (decay <= 0) return t === at ? 1 : 0;
  return Math.exp(-(t - at) / decay);
};

/**
 * 多段震屏。hits 是打击时刻数组（秒），amp 是像素幅度，decay 是每次打击的衰减时间。
 *
 * 为什么用 remotion 的 random()：它接收显式 seed，同一次渲染里逐帧可复现，
 * 而 Math.random() 会让每一帧的抖动都不一样 → 抽帧和成片对不上，还会让 Remotion
 * 的并行分段渲染出现接缝。这是本项目明确踩过的坑。
 */
export const shake = (t: number, hits: readonly number[], amp = 8, decay = 0.28): number => {
  let acc = 0;
  for (let i = 0; i < hits.length; i++) {
    const h = hits[i];
    if (t < h) continue;
    const e = impulse(t, h, decay);
    // seed 用整数帧序号，保证同一击在同一帧永远是同一个偏移量
    const seed = i * 1000 + Math.round(t * 240);
    acc += (random(seed) * 2 - 1) * amp * e;
  }
  return acc;
};

/**
 * 打字机：按 start→end 秒把 text 逐字放出来。
 * 返回已显示的字符串 + 光标是否可见（用 0.5 秒方波闪烁）。
 */
export const typed = (
  text: string,
  t: number,
  start: number,
  end: number,
): { text: string; caret: boolean } => {
  const p = ramp(t, [start, end], [0, 1], EASE.linear);
  const chars = Array.from(text); // 用 Array.from 而不是 slice：中文/emoji 按码点切不会切出半个字
  const n = Math.round(p * chars.length);
  return { text: chars.slice(0, n).join(""), caret: t >= start && t < end + 0.05 && t % 1 < 0.6 };
};

/** 第 i 个元素的入场延迟：用于"元素逐个出现，间隔 ≥ stagger"的节奏底线。 */
export const stagger = (i: number, gap: number, base = 0): number => base + i * gap;

/**
 * 上升入场：返回可直接展开进 style 的 translateY + opacity。
 * 场景里写 `style={{ ...riseIn(t, 1.2, 0.4), color: fg }}` 即可。
 */
export const riseIn = (t: number, start: number, dur = 0.45, distance = 28): CSSProperties => {
  const p = ramp(t, [start, start + dur], [0, 1], EASE.out);
  return {
    opacity: p,
    transform: `translateY(${(1 - p) * distance}px)`,
  };
};

/** 缩放弹出：小徽标、贴纸、圆点的入场。 */
export const popIn = (t: number, start: number, dur = 0.35): CSSProperties => {
  const p = ramp(t, [start, start + dur], [0, 1], EASE.back);
  return { opacity: Math.min(1, p * 1.6), transform: `scale(${0.8 + p * 0.2})` };
};

/** 匀速循环相位（0~1）。用于呼吸光、走马灯这类"不需要卡点"的循环动画。 */
export const loop = (t: number, period: number): number => {
  if (period <= 0) return 0;
  const x = (t % period) / period;
  return x < 0 ? x + 1 : x;
};
