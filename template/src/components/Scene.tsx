/**
 * Scene.tsx —— 场景外壳：淡入淡出 + 占位兜底。
 *
 * ❗责任边界（很重要）：
 * 场景组件**不要**自己判断"我该不该显示"。窗口判定只由 Promo.tsx 那一层
 * `<Sequence from durationInFrames>` 负责（唯一按 timing.json 裁帧的地方），
 * SceneFrame 只做淡入淡出与占位兜底，场景只管排自己的内容。这样改窗口只需要动
 * timing.json，场景文件一行都不用改；反过来，如果让四个场景各写一遍 if，早晚会出现
 * "timing.json 改了而某个场景忘了改"的黑帧。
 *
 * ❗和 AGENTS.md 阶段 6 的对齐：那一句"组件自己按 timing.json 的窗口判断显示与否，
 * 窗口外必须 return null"说的是**语义责任**（窗口外画面确实什么都不该有），
 * **不是**要求在这里再包一层同窗口的 Sequence、或用帧号再比一次大小 —— 见 SceneFrame
 * 的长注释，模板第一版正是这么错的。
 */
import type React from "react";
import { createContext, useContext } from "react";
import { AbsoluteFill, useCurrentFrame, useVideoConfig } from "remotion";
import { TIMING, findScene, framesToSec, type SceneSpec } from "../lib/timing";
import { ramp, EASE } from "../lib/anim";

/** 默认淡入淡出时长（秒）。0.25 秒≈半拍，肉眼看是"接上"而不是"切换"。 */
export const DEFAULT_FADE = 0.25;

export type SceneFrameProps = {
  /** 场景名，必须对应 timing.json scenes[].name */
  name: string;
  children: React.ReactNode;
  /** 淡入淡出时长（秒），传 0 表示硬切 */
  fade?: number;
};

/**
 * 当前场景起点（绝对秒）。
 * ❗为什么需要这个：见下面 SceneFrame 的长注释 —— 场景里的 useCurrentFrame() 是
 * **相对 Promo 里那个 Sequence** 的帧，而不是全片帧。必须先知道"我这一场从第几秒开始"，
 * 才能把场景内的相对时间和 timing.json 里的绝对时间（beatAt 的返回值、字幕的 at）对上。
 */
const SceneOffsetContext = createContext<number | null>(null);

/**
 * 取场景内的**相对**秒数（从本场景的第 0 秒算起）。
 * 就是 useCurrentFrame()/fps：Promo 已经为每个场景开了 Sequence，
 * 这里**不要**再减 scene.start，否则会二次偏移（第一版就是这么错的）。
 */
export const useSceneTime = (name: string): number => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();
  void name;
  return framesToSec(frame, fps);
};

/** 全片绝对秒（从 0 开始，一直到 timing.duration）。SceneFrame 里外都能用。 */
export const useAbsTime = (): number => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();
  const offset = useContext(SceneOffsetContext);
  return framesToSec(frame, fps) + (offset ?? 0);
};

/** 本场景起点（绝对秒）；不在 SceneFrame 里时返回 0。 */
export const useSceneStart = (): number => useContext(SceneOffsetContext) ?? 0;

/** 场景窗口（帧），统一用 Math.round(秒 × fps) 换算，避免 60fps 下出现半帧。 */
export const sceneFrames = (scene: SceneSpec, fps: number): { from: number; duration: number } => ({
  from: Math.round(scene.start * fps),
  duration: Math.round((scene.end - scene.start) * fps),
});

/**
 * 窗口内的相对秒。
 * Promo 里的 Sequence 已经把帧换成"场景内第几帧"，所以这里只要换算成秒 —— 不再减 start。
 */
const useLocalTime = (startSeconds: number): number => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();
  void startSeconds;
  return framesToSec(frame, fps);
};

/**
 * 淡入淡出透明度。抽出来是为了让 SceneFrame 里的 hook 调用顺序保持固定。
 *
 * ❗退场淡出写在 [duration, duration + fade]，也就是**本场窗口结束之后**那一段：
 * Promo 会给非末场多渲染 tail（= CROSSFADE = DEFAULT_FADE）帧，正好落在这里。
 * 按直觉写成 [duration - fade, duration] 就和 Promo 的 SceneCrossfade 撞在一起 ——
 * 两条都往 0 走的曲线相乘，边界那一帧（示例数据 t=4.5s）透明度是 0×0：
 * 实测抽帧 4.50s / 26.00s 两帧是纯底色，前后各暗三四帧。
 * 末场没有 tail，也就没有退场淡出（画面停在成片最后一帧，不再淡黑）。
 */
const useFadeOpacity = (durationSeconds: number, fade: number, startSeconds: number): number => {
  const t = useLocalTime(startSeconds);
  if (fade <= 0) return 1;
  const inPart = ramp(t, [0, fade], [0, 1], EASE.out);
  const outPart = ramp(t, [durationSeconds, durationSeconds + fade], [1, 0], EASE.in);
  return Math.min(inPart, outPart);
};

/**
 * 场景外壳：淡入淡出 + 占位兜底。
 *
 * ❗❗ 这里**绝对不能再套一层 <Sequence>**（模板第一版踩过的大坑）：
 * Promo.tsx 已经为每个场景开了一个 `<Sequence from={start} durationInFrames={...}>`，
 * 而 Remotion 的 `useCurrentFrame()` 返回的是**相对于最近一层 Sequence** 的帧。
 * 再套一层同 from 的 Sequence，帧就会被减两次：proof（26s 起）在第 29 秒拿到的
 * `useCurrentFrame()` 是 29×60 − 26×60 × 2 = 420（=7 秒）而不是 180（=3 秒），
 * 于是"卡片出现时间"这种比较全部错位 —— 现象是 proof / cta 两场只剩字幕，
 * 画面一片黑，而且没有任何报错（因为 `t >= at` 只是恒不成立，渲染器不会抱怨）。
 *
 * 同理，窗口判定**只保留一层**：Promo 的 Sequence 已经按 timing.json 把帧裁到
 * [start, end)，这里再用帧号比一次大小就是第二次判定，只会误杀 Promo 特意多渲染的
 * tail 帧（交叉淡化那几帧），对画面没有任何好处。窗口外"什么都不出现"由 Sequence 保证，
 * 这就是 AGENTS.md 阶段 6 那句"窗口外必须 return null"的语义。
 */
export const SceneFrame: React.FC<SceneFrameProps> = ({ name, children, fade = DEFAULT_FADE }) => {
  const scene = findScene(TIMING, name);
  // ⚠️ hook 必须在任何 return 之前调用：Remotion 渲染时会逐帧重跑组件，
  // 一旦 hook 数量随分支变化，React 会直接抛 "Rendered fewer hooks than expected"。
  const durationSeconds = scene ? scene.end - scene.start : 0;
  const startSeconds = scene ? scene.start : 0;
  const opacity = useFadeOpacity(durationSeconds, fade, startSeconds);

  // ---- 占位兜底 ----------------------------------------------------------
  // 只有"组件写了 timing.json 里没有的场景名"才会走到这里 —— Promo 是遍历 TIMING.scenes
  // 渲染的，所以"timing.json 写了名字但没注册组件"那条路根本到不了这里，它由
  // src/scenes/index.ts 的 MISSING_SCENE 负责（那边才是唯一会铺满画面 + 铺底色的占位）。
  // ❗这个占位**不许铺满**：全屏 AbsoluteFill 会把同一帧里其它场景的画面糊掉，
  // 排查时看不出是谁在报错，所以这里只渲染居中一行小字、不设背景。
  if (!scene) {
    return (
      <div style={{ position: "absolute", left: 0, right: 0, top: "50%", display: "flex", justifyContent: "center", pointerEvents: "none" }}>
        <div
          style={{
            padding: "12px 20px",
            borderRadius: 12,
            border: `1px dashed ${TIMING.brand.colors.fg}55`,
            color: TIMING.brand.colors.fg,
            opacity: 0.7,
            fontFamily: "monospace",
            fontSize: 24,
          }}
        >
          timing.json 里的场景缺少窗口定义：{name}
        </div>
      </div>
    );
  }

  return (
    <SceneOffsetContext.Provider value={scene.start}>
      <AbsoluteFill style={{ opacity }}>{children}</AbsoluteFill>
    </SceneOffsetContext.Provider>
  );
};