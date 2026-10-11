/**
 * scenes/index.ts —— 场景注册表。
 *
 * 唯一的"名字 → 组件"映射表：Promo.tsx 按 timing.json 的 scenes[].name 在这里查组件。
 * 为什么要有它还坚持用注册表：
 *   - timing.json 是唯一事实来源，场景的**顺序与窗口**都在数据里；
 *   - 但数据里不能放函数，所以需要一层显式映射把名字接回组件；
 *   - 好处是新增/删除场景只改这一个文件 + timing.json，Promo 一行都不用动。
 *
 * ⚠️ 删掉某个示例场景时，记得同时删掉这里的一行 —— 留着的话 Promo 会渲染一个空组件，
 * 屏幕上就是一段纯黑，抽帧时才发现。
 *
 * ⚠️ 这个文件是 .ts 而不是 .tsx（仓库文档里写的路径就是 `src/scenes/index.ts`），
 * 所以占位组件用 createElement 而不是 JSX —— 想改成 JSX 就得把文件名改成 index.tsx，
 * 同时更新 references/踩坑记录.md 里的那处引用。两者选一个，别只改一半。
 */
import type React from "react";
import { createElement } from "react";
import { AbsoluteFill } from "remotion";
import { Hook } from "./Hook";
import { Demo } from "./Demo";
import { Proof } from "./Proof";
import { Cta } from "./Cta";
import { PALETTE } from "../lib/theme";

/** 场景组件不接收 props：一切数据都从 timing.json 与 useBeat() 来。 */
export const SCENES: Record<string, React.FC> = {
  hook: Hook, // 0~4.5s   痛点开场
  demo: Demo, // 4.5~26s  三个真实界面
  proof: Proof, // 26~36s  真实作品蒙太奇
  cta: Cta, // 36~45s  品牌 + 网址
};

/**
 * timing.json 里写了名字但注册表里没有时的占位块。
 * **故意画得显眼**：与其静默留黑屏，不如让它在预览里一眼可见 ——
 * 这样"改了 timing.json 忘了建场景"这种错不会带进成片。
 */
export const MISSING_SCENE: React.FC<{ name: string }> = ({ name }) =>
  createElement(
    AbsoluteFill,
    { style: { alignItems: "center", justifyContent: "center", background: PALETTE.bg } },
    createElement(
      "div",
      {
        style: {
          padding: "28px 36px",
          borderRadius: 20,
          border: `2px dashed ${PALETTE.accent}`,
          background: PALETTE.panel,
          color: PALETTE.fg,
          fontFamily: "monospace",
          fontSize: 30,
          textAlign: "center",
          lineHeight: 1.5,
        },
      },
      `未实现的场景：${name}`,
      createElement("br"),
      createElement("span", { style: { opacity: 0.6, fontSize: 22 } }, "请在 src/scenes/index.ts 注册"),
    ),
  );
