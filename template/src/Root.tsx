/**
 * Root.tsx —— 注册 composition。
 *
 * 为什么是两个而不是一个：
 *   - `Promo`：正式版，用 timing.json 里的 fps（一般是 60）。渲染慢、体积大，是交付用的。
 *   - `PromoPreview`：固定 30fps 的预览版。**同一套场景代码、同一段时间轴（秒）**，
 *     只是采样率低一半 —— 所以预览看到的结构和成片一致，但渲染时间大约是一半。
 *     做分镜自查时先渲这个，别拿 1080p60 试错。
 *
 * durationInFrames 一律 Math.round(秒 × fps)（契约第 1 条），不要在别处另算一套帧数。
 */
import type React from "react";
import { Composition } from "remotion";
import { Promo } from "./Promo";
import { TIMING } from "./lib/timing";

/** 预览版的帧率。30 是"能看清动作、又足够快"的平衡点。 */
export const PREVIEW_FPS = 30;

export const RemotionRoot: React.FC = () => (
  <>
    <Composition
      id="Promo"
      component={Promo}
      durationInFrames={Math.round(TIMING.duration * TIMING.fps)}
      fps={TIMING.fps}
      width={TIMING.width}
      height={TIMING.height}
      defaultProps={{ withAudio: true }}
    />
    {/* 预览默认静音：npm run render:preview 不传 props，这里才是实际生效的值 ——
        30fps 结构预览不需要音轨，和 Promo.tsx 顶部第 3 条注释一个意思。 */}
    <Composition
      id="PromoPreview"
      component={Promo}
      durationInFrames={Math.round(TIMING.duration * PREVIEW_FPS)}
      fps={PREVIEW_FPS}
      width={TIMING.width}
      height={TIMING.height}
      defaultProps={{ withAudio: false }}
    />
  </>
);
