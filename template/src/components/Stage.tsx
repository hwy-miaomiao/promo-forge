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
  /**
   * 在安全区**之外**再加一段底部空白（px）：底部叠着字幕带时用它。
   * ❗不要再用 `style={{ paddingBottom }}` 来"扩大底部留白"：style 里的 padding 现在与安全区
   * **相加**而不是替换（旧实现把 `...style` 展开在安全区之后，传一个 paddingBottom 就把 18%
   * 的安全区整段替换掉 —— 横版 Demo 实测机身被裁掉 305px/边）。
   */
  bottomExtra?: number;
  style?: React.CSSProperties;
};

/** 把 px 值（数字或 "24px" 这类字符串）与基础值相加；拿不到数字就当 0，不让 NaN 污染布局。 */
const addPx = (base: number, extra: React.CSSProperties["paddingTop"]): number => {
  if (extra === undefined || extra === null || extra === "") return base;
  const n = typeof extra === "number" ? extra : Number.parseFloat(String(extra));
  return Number.isFinite(n) ? base + n : base;
};

export const Stage: React.FC<StageProps> = ({
  children,
  pad = true,
  align = "center",
  justify = "center",
  maxWidth,
  bottomExtra = 0,
  style,
}) => {
  const { width, height } = useVideoConfig();
  const vertical = isVertical(width, height);
  const safe = safePadding(width, height);

  // 最大内容宽度：竖屏 84%，横版 68%（横版太宽会变成一行横幅，读起来累）
  const ratio = maxWidth ?? (vertical ? SAFE.contentWidth.vertical : SAFE.contentWidth.horizontal);
  const limit = Math.round(width * ratio);

  const justifyContent = align === "top" ? "flex-start" : align === "bottom" ? "flex-end" : "center";

  // ❗style 里的 padding 单独取出来与安全区**相加**，不能整段被 `...style` 替换：
  // 旧实现把 `...style` 展开在安全区之后，场景只要传一个 paddingBottom 就会覆盖掉安全区
  // （字幕带、平台按钮区也就不再受保护）。其余属性仍由 style 覆盖。
  const { paddingTop, paddingBottom, paddingLeft, paddingRight, ...restStyle } = style ?? {};

  return (
    <AbsoluteFill
      style={{
        // ❗安全区用百分比算成 px：字幕/网址/CTA 越过它就会被平台的互动按钮压住
        paddingTop: addPx(pad ? safe.top : 0, paddingTop),
        paddingBottom: addPx(pad ? safe.bottom : 0, paddingBottom) + bottomExtra,
        paddingLeft: addPx(pad ? safe.left : 0, paddingLeft),
        paddingRight: addPx(pad ? safe.right : 0, paddingRight),
        display: "flex",
        flexDirection: "column",
        alignItems: justify === "left" ? "flex-start" : "center",
        justifyContent,
        // 文本块内部也要能各自对齐，交给子元素自己声明
        textAlign: justify === "left" ? "left" : "center",
        ...restStyle,
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
