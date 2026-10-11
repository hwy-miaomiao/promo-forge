/**
 * Promo.tsx —— 整支片子的骨架：底色 + 场景序列 + 音乐 + 字幕。
 *
 * 三件事刻意做成"数据驱动"：
 *   1) 场景顺序、每个场景的窗口 → 全部来自 timing.json 的 scenes 数组，组件不写死；
 *   2) 场景组件 → 从 scenes/index.ts 的注册表按 name 查，查不到就渲染醒目的缺场景占位；
 *   3) 音乐 → withAudio 为真时才挂 <Audio>，这样 30fps 预览版可以静音渲染（省一半时间）。
 *
 * ⚠️ 只用 remotion 内置的 Audio。不要 import @remotion/media：
 * 它是可选包，模板的 package.json 里没有声明，一旦引用 check_assets / 安装都会炸。
 */
import type React from "react";
import { AbsoluteFill, Audio, Sequence, staticFile, getStaticFiles, useCurrentFrame, useVideoConfig } from "remotion";
import { TIMING } from "./lib/timing";
import { SCENES, MISSING_SCENE } from "./scenes";
import { Caption } from "./components/Caption";
import { sceneFrames } from "./components/Scene";
import { EASE, ramp } from "./lib/anim";
import "./lib/fonts"; // 副作用导入：注册本地字体（file 为空时什么都不做）

export type PromoProps = {
  /** 是否带上配乐。预览渲染可以传 false，成片必须 true。 */
  withAudio: boolean;
};

/**
 * 配乐文件到底存不存在（public/ 是打包时的快照，所以这里能静态判断）。
 *
 * 为什么需要这一步：模板仓库里**不带音频**（版权 + 体积），`music.file` 指向的文件是
 * `mix.py` 的产物、在 .gitignore 里。新克隆直接渲染时，如果硬挂 <Audio>，Remotion 会去
 * 下载这个不存在的文件并以 404 让**整个渲染任务失败**（报错文本是
 * `Error while downloading http://localhost:3000/public/audio/music.wav`，很难联想到"还没混音"）。
 * 所以：文件不存在就不挂音轨 —— 渲染照样出无声成片，而"缺混音"由渲染前的
 * `check_assets.py`（ERROR: 混音文件）和渲染后的 `check_video.py`（无音轨）明确报出来。
 *
 * 拿不到清单时按"存在"处理（宁可让渲染报错，也不要静默丢音轨）。
 */
const MUSIC_FILE_EXISTS: boolean = (() => {
  const want = TIMING.music.file.replace(/\\/g, "/");
  if (want.trim() === "") {
    return false;
  }
  try {
    return getStaticFiles().some((file) => {
      const name = String(file.name).replace(/\\/g, "/").replace(/^\/?public\//, "");
      const src = String(file.src).replace(/\\/g, "/");
      return name === want || src.endsWith("/" + want) || src.endsWith("/public/" + want);
    });
  } catch {
    return true;
  }
})();

/**
 * 场景之间的交叉淡化：前一场"还没走完"时就开始变淡，后一场叠在上面淡入。
 *
 * 为什么必须在 Promo 这一层做：如果只让每个场景在自己的窗口里淡入淡出，
 * 就会出现"上一场已经淡到 0、下一场还没淡起来"的一瞬 —— 实测抽帧时
 * 场景边界那一帧（示例数据 t=4.5s）是纯黑的，正好落在"任意时刻往后 2 秒内必须有新信息"
 * 的密度要求上。这里把前一场往后多渲染 CROSSFADE 秒并淡出，画面永远不空。
 *
 * ❗这依赖 Scene.tsx 的配合：SceneFrame 的退场淡出走 [duration, duration + fade]，
 * 正好落在这段 tail 里（两边都往 0 淡会让边界那一帧变成 0×0 = 纯黑）。
 * 改 CROSSFADE 时记得 Scene.tsx 的 DEFAULT_FADE 要跟着一起动。
 */
const CROSSFADE = 0.25;

export const Promo: React.FC<PromoProps> = ({ withAudio }) => {
  const { fps } = useVideoConfig();

  return (
    <AbsoluteFill style={{ background: TIMING.brand.colors.bg }}>
      {/* 场景：按 timing.json 里的顺序渲染。窗口判定由这里的 <Sequence> 唯一负责
          （SceneFrame 只做淡入淡出与占位兜底，不再用帧号判第二次）。 */}
      {TIMING.scenes.map((scene, index) => {
        const Component = SCENES[scene.name];
        // 帧数统一用 Math.round(秒 × fps)：60fps 下 4.5 秒是 270 帧，30fps 预览版是 135 帧，
        // 两个 composition 的画面因此是"同一支片子的两种采样"，而不是两套时间轴。
        const { from, duration } = sceneFrames(scene, fps);
        // 不是最后一场才需要多渲染一点，用来给下一场让位（交叉淡化）
        const isLast = index === TIMING.scenes.length - 1;
        const tail = isLast ? 0 : Math.round(CROSSFADE * fps);
        return (
          <Sequence key={scene.name} from={from} durationInFrames={duration + tail} name={scene.name} layout="none">
            <SceneCrossfade duration={duration} tail={tail}>
              {Component ? <Component /> : <MISSING_SCENE name={scene.name} />}
            </SceneCrossfade>
          </Sequence>
        );
      })}

      {/* 字幕层压在场景之上：它不属于任何单个场景，跨场景连续（旁白是连续的） */}
      <Caption />

      {/* 音频：路径来自 timing.json 的 music.file（相对 public/）。混音脚本会写出这个文件。
          没有 withAudio、或文件还没混出来时，整条音轨不挂载（见 MUSIC_FILE_EXISTS 的说明）。

          ⚠️ 这里**不要**再乘 music.gain：这个文件是 mix.py 已经混好、已经母带到 -14 LUFS 的成品轨
          （音乐床电平在混音阶段就用过了）。再乘一次会让成片整体变小 —— 实测 gain=0.6 时整片从
          -15.4 LUFS 掉到 -19.9 LUFS，直接被 check_video.py 的响度闸门判 ERROR。 */}
      {withAudio && MUSIC_FILE_EXISTS ? <Audio src={staticFile(TIMING.music.file)} /> : null}
    </AbsoluteFill>
  );
};

/**
 * 只负责"退场那一下"的淡出。进场淡入仍由 SceneFrame 负责（它自己知道场景起点），
 * 这里补的是超出行程的那 `tail` 帧。注意本组件在 <Sequence> 内部，
 * 所以 useCurrentFrame() 拿到的是**相对帧**（这正是 SceneFrame 里那个坑的同款事实）。
 */
const SceneCrossfade: React.FC<{ duration: number; tail: number; children: React.ReactNode }> = ({ duration, tail, children }) => {
  const frame = useCurrentFrame();
  if (tail <= 0) {
    return <AbsoluteFill>{children}</AbsoluteFill>;
  }
  const fadeMs = Math.round(CROSSFADE * 1000);
  void fadeMs;
  const opacity = ramp(frame, [duration - tail, duration + tail], [1, 0], EASE.in);
  return <AbsoluteFill style={{ opacity }}>{children}</AbsoluteFill>;
};

