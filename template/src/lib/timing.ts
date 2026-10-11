/**
 * timing.ts —— timing.json 的类型定义与纯函数换算。
 *
 * 为什么单独抽一层：
 * 1) timing.json 是整支片子的**唯一事实来源**（Root / 场景 / 动画 / Python 脚本都读它）。
 *    把它收成一个显式接口，任何字段改名都会在 tsc 阶段炸出来，而不是在渲染到一半时白屏。
 * 2) 用 `typeof timing` 派生类型会把 `"9:16"` 这样的字面量类型带出来，导致函数参数不好传。
 *    所以这里用 DeepMutable 把只读与字面量都放宽，换来整条链路的类型可传递性。
 */
import timing from "../timing.json";

/** 去掉 readonly 与字面量窄化，让 timing 能在函数间自由传递。 */
type DeepMutable<T> = T extends readonly (infer U)[]
  ? DeepMutable<U>[]
  : T extends object
    ? { -readonly [K in keyof T]: DeepMutable<T[K]> }
    : T extends string
      ? string
      : T extends number
        ? number
        : T extends boolean
          ? boolean
          : T;

/** 音乐的剪接片段：把音乐时间轴上 [from,to] 的内容挪到视频时间 at 起播放。 */
export type MusicSegment = { at: number; from: number; to: number };

export type Music = {
  /** 相对 public/ 的路径，混音脚本会写出这个文件 */
  file: string;
  source: string;
  /** 视频时间 = 音乐时间 + offset */
  offset: number;
  /** 每拍秒数 */
  beat: number;
  /** 每小节秒数（通常 = 4 × beat） */
  bar: number;
  /** 第一次爆发的**视频时间**（秒） */
  drop: number;
  gain: number;
  segments: MusicSegment[];
};

export type SceneSpec = { name: string; start: number; end: number; note?: string };

export type Pace = {
  minShot: number;
  keyShot: number;
  sceneMin: number;
  stagger: number;
  holdAfterReveal: number;
};

export type VoiceLine = { id: string; scene: string; at: number; text: string };

export type Voice = {
  engine: string;
  voice: string;
  rate: string;
  settings: Record<string, number>;
  rmsDb: number;
  duckDb: number;
  lines: VoiceLine[];
};

export type Captions = { enabled: boolean; maxChars: number; maxLines: number; bottom: number };

export type BrandColors = { bg: string; fg: string; accent: string };

export type Brand = { name: string; url: string; logo: string; colors: BrandColors };

export type Timing = {
  slug: string;
  title: string;
  fps: number;
  width: number;
  height: number;
  aspect: string;
  duration: number;
  music: Music;
  scenes: SceneSpec[];
  pace: Pace;
  voice: Voice;
  captions: Captions;
  brand: Brand;
};

/**
 * 运行时把 JSON 提升成宽松类型。
 * 这里用 `as unknown as Timing` 而不是 `any`：对外暴露的仍是完整接口，
 * 场景里读错字段名照样报错，只是不再被 JSON 的字面量类型卡住。
 */
export const TIMING: Timing = timing as unknown as Timing;

/** 秒 -> 帧。全项目统一走这一个函数，避免有的地方 round、有的地方 floor 造成正负一帧抖动。 */
export const secToFrames = (seconds: number, fps: number): number => Math.round(seconds * fps);

/** 帧 -> 秒。 */
export const framesToSec = (frames: number, fps: number): number => frames / fps;

/** 场景名 -> 场景窗口；名字不存在时返回 null（由调用方决定是报错还是兜底）。 */
export const findScene = (t: Timing, name: string): SceneSpec | null =>
  t.scenes.find((s) => s.name === name) ?? null;

/** 场景时长（秒）；场景不存在时返回 0。 */
export const sceneDuration = (t: Timing, name: string): number => {
  const s = findScene(t, name);
  return s ? s.end - s.start : 0;
};

/** 片长（秒）优先取 timing.duration，缺失时退化成最后一个场景的 end。 */
export const totalSeconds = (t: Timing): number => {
  if (Number.isFinite(t.duration) && t.duration > 0) return t.duration;
  const last = t.scenes[t.scenes.length - 1];
  return last ? last.end : 0;
};
