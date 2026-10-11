/**
 * Stage.tsx —— 内容舞台：安全区 + 最大内容宽度 + 对齐方式。
 *
 * 为什么所有场景都必须经过它排版：
 * AGENTS.md 要求"同一套场景在 9:16 与 16:9 下都不破版"。破版的根源通常是两件事：
 *   1) 有人用绝对值（比如 left: 120px）排版 —— 换尺寸就歪；
 *   2) 有人让内容铺满整宽 —— 竖屏上会被平台按钮挡住，横版上会拉成一行横幅。
 * 把安全区与最大宽度收进这一个组件后，场景只写"纵向怎么排"，横竖屏差异由这里吸收。
 *
 * 命名由来：早期版本叫 Frame，和 Remotion 的 <Frame> 概念撞名，改成 Stage（舞台）。
 */
import type React from "react";
import { AbsoluteFill, useVideoConfig } from "remotion";
import { SAFE, isVertical, safePadding } from "../lib/theme";

export type StageProps = {
  children: React.ReactNode;
  /** 是否自动套安全区内边距。默认 true；画满屏幕的底纹块可以传 false 自己控制出血。 */
  pad?: boolean;
  /** 纵向对齐：内容少的时候决定它靠上/居中/靠下 */
  align?: "top" | "center" | "bottom";
  /** 内容整体水平对齐；默认居中，CTA 之类需要左对齐时传 left */
  justify?: "center" | "left";
  /** 覆盖最大内容宽度（占画面宽的比例），不传则用 SAFE.contentWidth */
  maxWidth?: number;
  style?: React.CSSProperties;
};

export const Stage: React.FC<StageProps> = ({
  children,
  pad = true,
  align = "center",
  justify = "center",
  maxWidth,
  style,
}) => {
  const { width, height } = useVideoConfig();
  const vertical = isVertical(width, height);
  const safe = safePadding(width, height);

  // 最大内容宽度：竖屏 84%，横版 68%（横版太宽会变成一行横幅，读起来累）
  const ratio = maxWidth ?? (vertical ? SAFE.contentWidth.vertical : SAFE.contentWidth.horizontal);
  const limit = Math.round(width * ratio);

  const justifyContent = align === "top" ? "flex-start" : align === "bottom" ? "flex-end" : "center";

  return (
    <AbsoluteFill
      style={{
        // ❗安全区用百分比算成 px：字幕/网址/CTA 越过它就会被平台的互动按钮压住
        paddingTop: pad ? safe.top : 0,
        paddingBottom: pad ? safe.bottom : 0,
        paddingLeft: pad ? safe.left : 0,
        paddingRight: pad ? safe.right : 0,
        display: "flex",
        flexDirection: "column",
        alignItems: justify === "left" ? "flex-start" : "center",
        justifyContent,
        // 文本块内部也要能各自对齐，交给子元素自己声明
        textAlign: justify === "left" ? "left" : "center",
        ...style,
      }}
    >
      <div
        style={{
          width: "100%",
          maxWidth: limit,
          display: "flex",
          flexDirection: "column",
          alignItems: justify === "left" ? "flex-start" : "center",
        }}
      >
        {children}
      </div>
    </AbsoluteFill>
  );
};
