/**
 * beats.ts —— 卡点（节奏）库。
 *
 * 为什么必须有这一层：
 * AGENTS.md 的硬性要求是"画面切换必须落在 beat 或 bar 上"。如果每个场景自己写
 * `Math.round(t / 0.5)`，一旦换歌（beat/bar 变了）就得改十几个文件。这里把音乐网格
 * 收敛成一组查询函数，场景只写"这一节的切换点落在**小节**上"，换歌不用改场景。
 *
 * 时间基准（本项目最容易搞错的一处）：
 *   timing.json 里的 beat / bar 是**音乐文件内部**的网格，offset 才是两者的换算：
 *       视频时间 = 音乐时间 + offset
 *   所以给定视频时间 t，音乐内部时间 = t - offset，取整拍/整小节后再 +offset 换回视频时间。
 *   混音脚本给出的经验公式是 offset = drop - downbeat（**爆发时刻 − 重拍时刻**，见 AGENTS.md 阶段 5）。
 */
import { useCurrentFrame, useVideoConfig } from "remotion";
import { TIMING, findScene, framesToSec, type Timing } from "./timing";

/** 音乐时间 <-> 视频时间：视频时间 = 音乐时间 + offset。 */
export const musicToVideo = (musicTime: number, timing: Timing): number => musicTime + timing.music.offset;
export const videoToMusic = (videoTime: number, timing: Timing): number => videoTime - timing.music.offset;

/**
 * 场景内第 i 个拍点对应的**绝对**视频秒。
 * i 可以为负，也可以超出场景窗口——场景负责把自己限制在窗口内，本函数不做夹取，
 * 因为"节拍在场景开始前就已经在走"是常态（例如场景 4.5s 开始，第 0 拍其实在 4.0s）。
 */
export const beatAt = (timing: Timing, sceneName: string, i: number): number => {
  const scene = findScene(timing, sceneName);
  return beatAtFrom(timing, scene ? scene.start : 0, i);
};

/** 同上，但直接给场景起点（绝对秒）：给拿不到场景名、只有 start 的地方用。 */
export const beatAtFrom = (timing: Timing, start: number, i: number): number => {
  const beat = timing.music.beat > 0 ? timing.music.beat : 0.5;
  // ❗i 是"场景起点之后的第 i 拍"，不是"音乐时间轴上第 i 拍"。
  // 先把场景起点在音乐网格上的位置**吸附**到最近一拍，再往后数 i 拍：
  //   start=26（proof） → round(26/0.5)=52 → i=0 得 26.0s、i=4 得 28.0s。
  // 早先写成 `start + (i - fractionalBeatIndex(start)) * beat` 时，i=0 会算成
  // 26 + (0-52)*0.5 = 0s —— 卡片的时间点落回片头，`t >= at` 恒成立但所有卡挤在同一帧，
  // 整个 proof 场景只剩字幕（cta 同理）。这个坑在模板第一版里真实发生过。
  const snappedBeat = Math.round(videoToMusic(start, timing) / beat) + i;
  return musicToVideo(snappedBeat * beat, timing);
};

/**
 * 场景内第 i 个小节对应的绝对视频秒。
 * 小节网格是"全局"的（从音乐第 0 小节起算），不是从场景起点起算，
 * 这样同一支片子里不同场景的切换点永远落在同一条小节线上。
 */
export const barAt = (timing: Timing, sceneName: string, i: number): number => {
  const scene = findScene(timing, sceneName);
  return barAtFrom(timing, scene ? scene.start : 0, i);
};

/** 同上，但直接给场景起点（绝对秒）。 */
export const barAtFrom = (timing: Timing, start: number, i: number): number => {
  const bar = timing.music.bar > 0 ? timing.music.bar : 2;
  const musicStart = videoToMusic(start, timing);
  return musicToVideo(Math.round(musicStart / bar) * bar + i * bar, timing);
};

/** 绝对视频时间内，第几拍（含小数）。用于 barAt 与 phase 的取整。 */
export const beatIndexAt = (videoTime: number, timing: Timing): number => {
  const beat = timing.music.beat > 0 ? timing.music.beat : 0.5;
  return videoToMusic(videoTime, timing) / beat;
};

/** 同上但返回整数拍序号（四舍五入到最近的一拍）。 */
const fractionalBeatIndex = (videoTime: number, timing: Timing): number => Math.round(beatIndexAt(videoTime, timing));

/** 拍内相位 0~1：0 = 正好在拍上，0.5 = 两拍正中间。抖动、呼吸光都从这里取。 */
export const phase = (videoTime: number, timing: Timing): number => {
  const idx = beatIndexAt(videoTime, timing);
  const x = idx - Math.floor(idx);
  return x < 0 ? x + 1 : x;
};

/** 是否踩在拍上（tol 单位是秒，默认 0.03 ≈ 60fps 下 2 帧）。 */
export const onBeat = (videoTime: number, timing: Timing, tol = 0.03): boolean => {
  const beat = timing.music.beat > 0 ? timing.music.beat : 0.5;
  const idx = beatIndexAt(videoTime, timing);
  return Math.abs(idx - Math.round(idx)) * beat <= tol;
};

/**
 * 把时间吸附到最近的节拍网格（unit 传 "beat" 或 "bar"）。
 * 用途：场景的切换点不要写死 11.5，而是 `snap(11.5, TIMING, "bar")`，
 * 这样即使换歌导致 bar 从 2.0 变成 1.846，切换点也会自动贴到新的小节线上。
 */
export const snap = (videoTime: number, timing: Timing, unit: "beat" | "bar" = "beat"): number => {
  const step = unit === "bar" ? (timing.music.bar > 0 ? timing.music.bar : 2) : timing.music.beat > 0 ? timing.music.beat : 0.5;
  const musicTime = videoToMusic(videoTime, timing);
  return musicToVideo(Math.round(musicTime / step) * step, timing);
};

/** 能量 0~1：给"爆发/平静"用的粗略包络，只依赖 timing.json 的 drop 与 segments，无需读音频。 */
export const energy = (videoTime: number, timing: Timing): number => {
  const { drop, beat, segments } = timing.music;
  const beatLen = beat > 0 ? beat : 0.5;

  // 起手不是 0：片头也需要一点推进感，但要比 drop 之后明显低一档
  const base =
    videoTime < drop
      ? 0.22 + 0.28 * Math.max(0, 1 - (drop - videoTime) / 2.5) // drop 前 2.5 秒开始蓄力
      : Math.max(0, 0.3 - Math.min(1, (videoTime - drop) / 16) * 0.15); // drop 之后缓慢回落但不归零

  // 每次 drop 给一个 2 小节的"高能余韵"
  const afterDrop =
    videoTime >= drop && videoTime - drop < timing.music.bar * 2
      ? 0.42 * Math.exp(-(videoTime - drop) / (timing.music.bar * 1.4))
      : 0;

  // 拍内脉冲：越靠近拍点越高，用平方让峰值更"尖"，视觉上更像心跳
  const pulse = Math.pow(1 - phase(videoTime, timing), 2) * (videoTime >= drop ? 0.16 : 0.09);

  // 剪接片段处再补一次能量（换段落 = 听觉上的新开始）。
  // ❗segments 在 AGENTS.md 第 5 节里是**可选**字段：缺省 / null / 写错类型时都要兜底成
  //   "没有剪接片段"，不许崩。早先直接 `segments.reduce(...)`，缺字段会抛
  //   `TypeError: Cannot read properties of undefined (reading 'reduce')`；而 energy()
  //   被 4 个场景每帧调用（Hook/Cta/Demo/Proof），一抛就是整场渲染失败（不是降级）。
  const segList = Array.isArray(segments) ? segments : [];
  const segHit = segList.reduce((acc, s) => {
    if (!s || typeof s.at !== "number" || !Number.isFinite(s.at)) return acc;
    return Math.max(acc, Math.exp(-Math.max(0, videoTime - s.at) / (beatLen * 2)) * 0.3);
  }, 0);

  return Math.min(1, base + afterDrop + pulse + segHit);
};

/** 从绝对视频时间取当前拍号与它离我们还有多久（负数表示已经过去）。 */
export const beatInfo = (videoTime: number, timing: Timing): { index: number; nextAt: number; since: number } => {
  const idx = beatIndexAt(videoTime, timing);
  const nearest = Math.round(idx);
  const at = musicToVideo(nearest * (timing.music.beat > 0 ? timing.music.beat : 0.5), timing);
  return { index: nearest, nextAt: at, since: videoTime - at };
};

export type BeatApi = {
  /** 每拍秒数 */
  beat: number;
  /** 每小节秒数 */
  bar: number;
  /** 视频时间 = 音乐时间 + offset */
  offset: number;
  /** 本场景起点（绝对视频秒），从 timing.json 直接读 —— 不依赖 React context */
  start: number;
  /** 当前**场景相对**秒：和 useSceneTime() 同一套基准，场景动画直接用这个 */
  now: number;
  /** 当前**绝对**视频秒（全片时间轴）：只有 energy() 这类"看全片"的计算才需要 */
  abs: number;
  /**
   * 场景内第 i 拍 → **场景相对**秒（可为负：节拍在场景开始前就在走）。
   * ❗场景里的 `t`（useSceneTime）就是相对秒，所以卡点比较一律用这个，
   * 不要用下面的 beatAt()（那个是绝对秒，是给它计算用的）。
   */
  at: (i: number) => number;
  /** 场景内第 i 小节 → **场景相对**秒 */
  barAt: (i: number) => number;
  /** 场景内第 i 拍 → **绝对**秒。给需要全片时间轴的场合用；和 t 比较前记得减 start。 */
  beatAt: (sceneName: string, i: number) => number;
  /** 拍内相位 0~1（参数是**场景相对**秒） */
  phase: (t: number) => number;
  /** 是否踩在拍上（参数是**场景相对**秒） */
  onBeat: (t: number, tol?: number) => boolean;
  /** 能量 0~1（参数是**场景相对**秒） */
  energy: (t: number) => number;
  /** 把场景相对秒吸附到最近的拍/小节 → **场景相对**秒 */
  snap: (t: number, unit?: "beat" | "bar") => number;
};

/**
 * 场景里取卡点信息的 hook。
 *
 * ❗时间基准只有一套：**场景相对秒**（和 `useSceneTime()` 完全一致）。
 * 早期版本这里返回的是绝对秒，而场景里的 `t` 是相对秒，两者一比较永远不成立
 * （proof 实测 t=5 而 at=26），整场画面全黑只剩字幕 —— 所以现在
 * at/barAt/snap/energy/phase 全部按相对秒给出，绝对秒只留 `abs` 与 `beatAt`。
 *
 * ❗start 直接来自 timing.json 的 scenes[]，**不走 React context**：
 * 打包后 context 会因为出现两份 React 实例而读成 null（实测 probe:null），
 * 从数据算反而更稳，而且 SceneFrame 之外调用也不会静默拿到 0。
 */
export const useBeat = (): BeatApi => {
  const local = framesToSec(useCurrentFrame(), useVideoConfig().fps);
  return makeBeatApi(TIMING, local);
};

/**
 * 用 timing + **场景相对秒** 造一套 BeatApi。抽出来是为了让 scenes 之外的
 * 地方（比如脚本预览页）也能复用同一套换算。
 */
export const makeBeatApi = (timing: Timing, local: number, sceneName?: string): BeatApi => {
  const scene = sceneName ? findScene(timing, sceneName) : null;
  const start = scene ? scene.start : 0;
  const abs = start + local;
  return {
    beat: timing.music.beat,
    bar: timing.music.bar,
    offset: timing.music.offset,
    start,
    now: local,
    abs,
    at: (i) => beatAtFrom(timing, start, i) - start,
    barAt: (i) => barAtFrom(timing, start, i) - start,
    beatAt: (name, i) => beatAt(timing, name, i),
    phase: (t) => phase(t + start, timing),
    onBeat: (t, tol) => onBeat(t + start, timing, tol),
    energy: (t) => energy(t + start, timing),
    snap: (t, unit = "beat") => snap(t + start, timing, unit) - start,
  };
};

/**
 * 场景版本的 useBeat()：告诉它你在哪个场景，at()/barAt()/snap() 都会自动
 * 用这个场景的起点换算，不用再手动减 sceneStart。
 * 场景里**优先用这个**：`const beat = useSceneBeat("proof")`。
 */
export const useSceneBeat = (sceneName: string): BeatApi => {
  const local = framesToSec(useCurrentFrame(), useVideoConfig().fps);
  return makeBeatApi(TIMING, local, sceneName);
};
