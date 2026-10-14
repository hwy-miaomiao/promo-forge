/**
 * BrowserFrame.tsx —— 浏览器窗口外壳（网页类产品的第一外壳）。
 *
 * 和 PhoneFrame 同样的取舍：外壳全部用内联样式画（圆角 + 三个圆点 + 地址栏），
 * 不依赖任何图片素材。原因是 check_assets.py 会把"引用了不存在的素材"判成 ERROR，
 * 而外壳这种纯装饰的东西，用代码画既零素材又能在任意分辨率下保持锐利。
 *
 * 地址栏文本是可选的：真实项目里应该填**真实域名**（例如 acme.com/dashboard），
 * 用户会下意识核对它 —— 填 example.com 会显得很假。
 */
import type React from "react";
import { Img, staticFile } from "remotion";
import { PALETTE, alpha, mix } from "../lib/theme";
import { FONT } from "../lib/fonts";

export type BrowserFrameProps = {
  /** 装真实截图时用这个（相对 public/ 的路径）；有 children 时忽略它 */
  src?: string;
  children?: React.ReactNode;
  /** 窗口宽度占父容器的比例，默认铺满（横版常见） */
  widthRatio?: number;
  /** 内容区宽高比（宽/高）。不传则由内容自己撑开。 */
  ratio?: number;
  /** 地址栏文本，空则不显示地址 */
  url?: string;
  /** 窗口圆角（px）；不传按宽度百分比给 */
  radius?: number;
  /** 是否画顶部工具条 */
  chrome?: boolean;
  /** 窗口底色 */
  bodyColor?: string;
  style?: React.CSSProperties;
  contentStyle?: React.CSSProperties;
};

const DOTS = ["#FF5F57", "#FEBC2E", "#28C840"]; // 红黄绿，纯装饰

export const BrowserFrame: React.FC<BrowserFrameProps> = ({
  src,
  children,
  widthRatio = 1,
  ratio,
  url = "",
  radius,
  chrome = true,
  bodyColor = mix(PALETTE.bg, PALETTE.fg, 0.06),
  style,
  contentStyle,
}) => (
  <div
    style={{
      width: `${Math.round(widthRatio * 100)}%`,
      borderRadius: radius !== undefined ? `${radius}px` : "3%",
      background: bodyColor,
      border: `1px solid ${PALETTE.border}`,
      // ❗box-shadow 的偏移/模糊只能吃 <length>，写百分比是非法值，整条声明会被 CSS 丢掉
      //   （实测 getComputedStyle().boxShadow === "none"，窗口完全没有抬起感）。
      //   改回 px：Remotion 的 --scale 会整体缩放成片栅格，px 在 1080 与 4K 下比例一致；
      //   18px / 54px ≈ 旧写法在窗口宽 600px 时的 3% / 9%。
      //   （不要写 cqw：容器查询单位在**容器自己身上**会用最近的外层容器或视口来解析，
      //    实测 3cqw 被算成 42px = 视口宽的 3%，不是窗口宽的 3%。）
      boxShadow: `0 18px 54px ${alpha("#000000", 0.5)}`,
      overflow: "hidden",
      ...style,
    }}
  >
    {chrome ? (
      <div
        style={{
          height: "2.6em",
          display: "flex",
          alignItems: "center",
          gap: "0.9em",
          padding: "0 1.1em",
          borderBottom: `1px solid ${PALETTE.border}`,
          background: mix(bodyColor, PALETTE.fg, 0.05),
        }}
      >
        {/* 三个圆点：画出来而不是用图标字体，避免字体缺失时变成豆腐块 */}
        <div style={{ display: "flex", gap: "0.45em" }}>
          {DOTS.map((c) => (
            <span key={c} style={{ width: "0.72em", height: "0.72em", borderRadius: 999, background: c, display: "block" }} />
          ))}
        </div>
        {url.trim() !== "" ? (
          <div
            style={{
              flex: 1,
              height: "1.5em",
              display: "flex",
              alignItems: "center",
              justifyContent: "center",
              gap: "0.4em",
              borderRadius: 999,
              background: mix(bodyColor, PALETTE.bg, 0.5),
              border: `1px solid ${PALETTE.border}`,
              color: PALETTE.dim,
              fontFamily: FONT.mono,
              fontSize: "0.78em",
              letterSpacing: "0.02em",
              overflow: "hidden",
              whiteSpace: "nowrap",
            }}
          >
            {/* 小锁图标用文字符号，不引素材；🔒 这类 emoji 在部分 Linux 无头环境下会缺字形 */}
            <span style={{ opacity: 0.7 }}>https://</span>
            <span style={{ color: PALETTE.fg, opacity: 0.9 }}>{url.replace(/^https?:\/\//, "")}</span>
          </div>
        ) : (
          <div style={{ flex: 1 }} />
        )}
      </div>
    ) : null}

    <div
      style={{
        position: "relative",
        width: "100%",
        ...(ratio !== undefined ? { aspectRatio: `${ratio}`, height: "auto" } : {}),
        overflow: "hidden",
        ...contentStyle,
      }}
    >
      {src ? (
        // object-fit 决定"不拉伸"：截图可以裁切、可以留边，但绝不允许被压扁
        <Img src={staticFile(src)} alt="" style={{ width: "100%", height: "100%", objectFit: "cover", objectPosition: "top center", display: "block" }} />
      ) : (
        children
      )}
    </div>
  </div>
);
