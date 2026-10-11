/**
 * Shot.tsx —— 装"真实截图"的组件（本项目的核心：真实产品才是主角）。
 *
 * ❗这个文件存在的唯一理由：**图片永远不能被压扁**。
 * 浏览器默认样式里有 `img { max-width: 100% }`，一旦配合 `width:100%` + `height:100%`
 * 或者被 transform 缩放，Chromium 会把截图的宽高比改掉 —— 界面截图看起来就像被横向挤扁了。
 * 所以这里做三件事：
 *   1) img 上永远带 `objectFit: cover|contain`（不设 object-fit 时 img 默认会拉伸填充）；
 *   2) 显示层的尺寸由容器按比例算出，img 只吃 `width/height: 100%`，不做 max-width 那套；
 *   3) index.css 里再补一条 `img, video { max-width: none; }` 兜底（那个坑踩过）。
 *
 * 四个可动参数各管一件事，别混用：
 *   zoom/focusX/focusY —— **静态**取景，指"放大后看哪一块"（焦点会停在画面正中）；
 *   scroll(0~1)        —— 长截图纵向滚动，0 是顶部、1 是底部；
 *   progress(0~1)      —— Ken Burns 缓慢推近，由时间驱动，负责"让静图不死"。
 */
import type React from "react";
import { Img, staticFile } from "remotion";
import { clamp, lerp } from "../lib/anim";

export type ShotProps = {
  /** 相对 public/ 的图片路径；组件内部会先过一层 staticFile() 再交给 <Img> */
  src: string;
  /** 期望的显示宽高比（宽/高）。不传 = 交给图片自然比例（截图最安全的默认）。 */
  ratio?: number;
  /** cover 铺满并裁切；contain 完整显示并留边。两者都不会拉伸。 */
  fit?: "cover" | "contain";
  /** 放大倍数，≥1 */
  zoom?: number;
  /** 放大后要看的横向位置 0~1（0 左 1 右），焦点会被摆到画面中央 */
  focusX?: number;
  /** 放大后要看的纵向位置 0~1（0 上 1 下） */
  focusY?: number;
  /** Ken Burns 推近进度 0~1（通常喂 ramp(t,[a,b],[0,1])） */
  progress?: number;
  /** 长截图滚动进度 0~1（内部按显示层高度的 80% 行程算，避免滚到露白） */
  scroll?: number;
  /** Ken Burns 的总推近量，默认 0.08（8% 足够，超过 15% 会晕） */
  kenBurns?: number;
  style?: React.CSSProperties;
  /** 圆角（px）。和外壳组件保持一致时，截图看起来才像"长在界面里"。 */
  radius?: number;
  alt?: string;
};

export const Shot: React.FC<ShotProps> = ({
  src,
  ratio,
  fit = "cover",
  zoom = 1,
  focusX = 0.5,
  focusY = 0.5,
  progress = 1,
  scroll = 0,
  kenBurns = 0.08,
  style,
  radius = 0,
  alt = "",
}) => {
  const z = Math.max(1, zoom);
  const kb = lerp(1, 1 + kenBurns, clamp(progress, 0, 1));

  // 焦点偏移：把焦点摆到画面中央。因为取景层用 object-fit 会按容器中心对齐，
  // 所以焦点偏离中心多少，就要反向平移多少（× 显示层尺寸）。
  const shiftX = -(clamp(focusX, 0, 1) - 0.5) * 100 * z;
  const shiftY = -(clamp(focusY, 0, 1) - 0.5) * 100 * z;
  // 滚动行程 80%：留 20% 余量，滚到底时不会把画面上边缘拉进缺口
  const scrollY = -clamp(scroll, 0, 1) * 80;

  const transform = [
    `translate(${shiftX}%, ${shiftY}%)`,
    `scale(${(z * kb).toFixed(4)})`,
    scrollY !== 0 ? `translateY(${scrollY}%)` : "",
  ]
    .filter(Boolean)
    .join(" ");

  return (
    <div
      style={{
        position: "relative",
        overflow: "hidden",
        borderRadius: radius,
        // ❗不给 display 层设 maxWidth/maxHeight —— 那正是把截图压扁的来源
        ...(ratio !== undefined
          ? { width: "100%", aspectRatio: `${ratio}`, height: "auto" }
          : { width: "100%", height: "100%" }),
        ...style,
      }}
    >
      <div
        style={{
          position: "absolute",
          inset: 0,
          width: "100%",
          height: "100%",
          transform,
          transformOrigin: "center center",
          willChange: "transform",
          // 放大后边缘采样更软，交给浏览器做高质量缩放
          imageRendering: "auto",
        }}
      >
        <Img
          src={staticFile(src)}
          alt={alt}
          // objectFit 决定"不拉伸"：cover 裁切、contain 留边，两者都保持比例
          style={{ width: "100%", height: "100%", objectFit: fit, display: "block" }}
        />
      </div>
    </div>
  );
};
