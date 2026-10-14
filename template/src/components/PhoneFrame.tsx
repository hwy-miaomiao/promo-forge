/**
 * PhoneFrame.tsx —— 手机外壳（竖屏内容的第一外壳）。
 *
 * 为什么用代码画外壳而不是贴一张 PNG：
 *   1) 外壳必须是**矢量**的，1080×1920 与 2160×3840 下都不能有锯齿；
 *   2) 贴图外壳在放大时会跟着模糊，而圆角/灵动岛在截图边缘很容易出现白边；
 *   3) 换品牌色时外壳颜色要跟着变 —— 代码画的可以直接读 PALETTE。
 *
 * 布局约定（最容易踩的坑）：
 *   外壳宽度用"占舞台宽度比例"给，高度由 `ratio`（机身宽高比，默认 9:19.5）算出，
 *   内部内容**按比例缩放**（screen 里所有尺寸都写成 % 或 em），
 *   这样同一个假界面在任意尺寸的外壳里都不会破版。
 */
import type React from "react";
import { AbsoluteFill, Img, staticFile } from "remotion";
import { PALETTE, alpha } from "../lib/theme";

export type PhoneFrameProps = {
  /** 机身宽度占父容器宽度的比例，0~1。默认 0.62（竖屏下约 560px，视觉上像握在手里） */
  widthRatio?: number;
  /** 机身宽高比（宽/高）。默认 9 / 19.5（接近 iPhone 15 Pro） */
  ratio?: number;
  /** 屏幕内容 */
  children?: React.ReactNode;
  /** 装真实截图时用这个（相对 public/ 的路径）；有 children 时忽略它 */
  src?: string;
  /** 机身圆角（px），默认按宽度算 */
  radius?: number;
  /** 是否画状态栏（时间 + 信号 + 电量） */
  statusBar?: boolean;
  /** 灵动岛 */
  island?: boolean;
  /** 机身颜色，默认取调色板浮层色 */
  bodyColor?: string;
  /** 屏幕底色 */
  screenColor?: string;
  /** 外面再包一层，方便摆位置 */
  style?: React.CSSProperties;
  /** 屏幕内容的自定义样式（例如改成浅色界面） */
  screenStyle?: React.CSSProperties;
};

const STATUS_TIME = "9:41"; // 苹果发布会同款时间，纯装饰

export const PhoneFrame: React.FC<PhoneFrameProps> = ({
  widthRatio = 0.62,
  ratio = 9 / 19.5,
  children,
  src,
  radius,
  statusBar = true,
  island = true,
  bodyColor = PALETTE.panelStrong,
  screenColor = PALETTE.bg,
  style,
  screenStyle,
}) => {
  const frameRadius = radius ?? undefined;

  return (
    <div
      style={{
        width: `${Math.round(widthRatio * 100)}%`,
        // 高度由宽高比推出，不用固定像素 —— 横竖屏都成立。
        // CSS 的 aspect-ratio 就是"宽 / 高"，所以直接填 ratio（9/19.5 ≈ 0.46 → 竖着的机身）。
        // ❗写成 1/ratio 机身会横躺成扁盒子，里面的界面被压成一条缝（模板第一版就是这样）。
        aspectRatio: `${ratio}`,
        position: "relative",
        borderRadius: frameRadius ? `${frameRadius}px` : "12%",
        background: bodyColor,
        // ❗机身是"按宽度算比例"的容器：里面的状态栏字号必须用容器查询单位（cqw），
        // 不能用百分比 —— CSS 的百分比 font-size 是相对**父元素 font-size**（默认 16px），
        // `3.6%` 只有 0.576px：浏览器把文本钳到最小 6px、而 em 子元素（信号柱/电池）
        // 仍按 0.576px 解析，实测整条状态栏不可见（1080 画布上「9:41」只有 6px 高、
        // 信号柱 0.078px 宽）。声明成 inline-size 容器后，里面的 3.6cqw = 机身内容宽的 3.6%。
        containerType: "inline-size",
        // 两层阴影：外阴影把手机"抬起来"，内阴影模拟机身与屏幕的接缝。
        // ❗box-shadow 的偏移/模糊/扩散只能吃 <length>：写百分比（原来的 `0 2.4% 6% …
        //   inset 0 0 0 0.5%`）是非法值，**整条声明**（包含合法的内阴影）会被浏览器丢掉 ——
        //   实测 getComputedStyle().boxShadow === "none"，机身完全没有抬起感。
        //   改 px：Remotion 的 --scale 整体缩放栅格，px 在 1080 与 4K 下比例一致；
        //   10px / 24px / 2px ≈ 旧写法在机身宽 397px 时的 2.4% / 6% / 0.5%。
        boxShadow: `0 10px 24px ${alpha("#000000", 0.55)}, inset 0 0 0 2px ${alpha(PALETTE.fg, 0.12)}`,
        padding: "2.2%",
        boxSizing: "border-box",
        // ❗机身高度由 aspect-ratio 决定，**不许被父级 flex 压扁**。
        // 默认 flex-shrink:1 时，只要父容器（Stage 的纵向 flex）高度不够，浏览器就会
        // 把机身从 aspect-ratio 算出的 903px 压到约 630px（14.0s 抽帧实测，机身宽 417px
        // 却只有 630px 高，比例 1.51 而不是 2.17）。内屏里的假界面是按**机身宽度**换算
        // 缩放的（Demo 的 mockScale），所以内容不会被压扁，而是被 overflow:hidden 切掉
        // 下半截、机身底部露出大片空白（20.0s 抽帧实测：五条账目下方的分类条只剩空槽）。
        flexShrink: 0,
        ...style,
      }}
    >
      <div
        style={{
          position: "relative",
          width: "100%",
          height: "100%",
          borderRadius: frameRadius ? `${frameRadius * 0.82}px` : "10%",
          background: screenColor,
          overflow: "hidden",
          ...screenStyle,
        }}
      >
        {/* 屏幕内容层 */}
        <AbsoluteFill>{src ? <FillImage src={src} /> : children}</AbsoluteFill>

        {/* 灵动岛：放在屏幕内容之上，真实手机就是这样遮住一部分界面的 */}
        {island ? (
          <div
            style={{
              position: "absolute",
              top: "1.6%",
              left: "50%",
              transform: "translateX(-50%)",
              width: "30%",
              height: "3.2%",
              borderRadius: 999,
              background: "#000",
            }}
          />
        ) : null}

        {/* 状态栏：时间、信号、电量都用代码画，避免依赖图标字体 */}
        {statusBar ? (
          <div
            style={{
              position: "absolute",
              top: "1.4%",
              left: "7%",
              right: "7%",
              display: "flex",
              justifyContent: "space-between",
              alignItems: "center",
              color: PALETTE.fg,
              // 机身内容宽的 3.6%（机身 397px 时约 13.7px）：容器查询单位，
              // 为什么不能用百分比见机身 style 里的说明。
              fontSize: "3.6cqw",
              fontWeight: 700,
              letterSpacing: "0.02em",
              mixBlendMode: "difference", // 深色/浅色界面上都能看清
            }}
          >
            <span>{STATUS_TIME}</span>
            <div style={{ display: "flex", gap: "0.5em", alignItems: "center" }}>
              {/* 信号：四根递增的柱子 */}
              <div style={{ display: "flex", gap: "0.12em", alignItems: "flex-end" }}>
                {[0.35, 0.55, 0.75, 1].map((h, i) => (
                  <span
                    key={i}
                    style={{ width: "0.14em", height: `${h}em`, background: "currentColor", borderRadius: "0.04em", display: "block" }}
                  />
                ))}
              </div>
              {/* 电量：外框 + 内芯 */}
              <div
                style={{
                  width: "1.6em",
                  height: "0.8em",
                  border: "0.1em solid currentColor",
                  borderRadius: "0.22em",
                  padding: "0.1em",
                  display: "flex",
                }}
              >
                <span style={{ width: "72%", background: "currentColor", borderRadius: "0.1em", display: "block" }} />
              </div>
            </div>
          </div>
        ) : null}
      </div>
    </div>
  );
};

/** 装真实截图：同样遵守"不压扁"原则，用 object-fit 控制裁切而不是拉伸。 */
const FillImage: React.FC<{ src: string }> = ({ src }) => (
  // 用 <Img> 而不是原生 <img>：Remotion 的 Img 会在渲染时阻塞到图片加载完成，
  // 避免抽帧抽出"空白屏"。它**不会**自己走 staticFile —— 路径必须显式过一层 staticFile()。
  <Img
    src={staticFile(src)}
    alt=""
    style={{ width: "100%", height: "100%", objectFit: "cover", objectPosition: "top center", display: "block" }}
  />
);
