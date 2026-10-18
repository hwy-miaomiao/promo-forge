/**
 * scenes/Cta.tsx —— 36~45 秒的收尾：品牌名 + 网址。
 *
 * 节奏依据（这是全片最不能省的一段）：
 *   - 网址 / CTA 停留 ≥2.5 秒（这里从 38.0 秒一直挂到 45.0 秒，正好 7.0 秒）；
 *   - 放在**全片能量最高处**：drop 之后的余韵 + 本次卡点脉冲一起驱动背景与 URL 的光晕；
 *   - 元素逐个出现间隔 ≥0.25 秒，出齐后停留 ≥0.6 秒 → 品牌(37.0) → 网址(38.0) → 按钮(39.0) → 落款(39.5)。
 *
 * 网址直接读 timing.brand.url，**不要在场景里再写一遍**：
 * 阶段 9 的 check_video.py 会拿成片里的文字和 timing.json 对账，写重了就迟早对不上。
 *
 * 时间基准：`t = useSceneTime("cta")` 是**本场景相对秒**（Promo 的 <Sequence> 已经减掉 36），
 * 卡点比较一律用相对的 `beat.now / beat.at(i) / beat.snap(...)`；绝对秒只在需要看全片能量时用 `beat.abs`。
 */
import type React from "react";
import { AbsoluteFill, Img, staticFile, useVideoConfig } from "remotion";
import { SceneFrame, useSceneTime } from "../components/Scene";
import { Stage } from "../components/Stage";
import { PALETTE, alpha, useType } from "../lib/theme";
import { FONT } from "../lib/fonts";
import { EASE, clamp, impulse, lerp, loop, ramp, riseIn } from "../lib/anim";
import { TIMING } from "../lib/timing";
import { useSceneBeat } from "../lib/beats";

export const Cta: React.FC = () => {
  const t = useSceneTime("cta");
  const type = useType();
  const { height } = useVideoConfig();
  const beat = useSceneBeat("cta");

  // ---- 卡点：beat.snap() 吃场景相对秒，返回的也是场景相对秒 --------------------
  // 以前这里用的是纯函数 snap(秒, TIMING, "beat")：它收的是**全片绝对秒**，
  // 看起来"数值差不多对"，但 cta 的 start 一改四个卡点就整体错位。场景内一律用 beat.snap()。
  const tBang = beat.snap(0.8); // 开场一击（品牌名的落点）
  const tUrl = beat.snap(1.9); // 网址：从这一拍起一直挂在屏上
  const tBtn = beat.snap(2.9);
  const tFoot = beat.snap(3.4);
  // 本场景窗口（从 timing.json 读，别硬编码 36）
  const cta = TIMING.scenes.find((s) => s.name === "cta");
  const ctaLength = cta ? cta.end - cta.start : TIMING.duration;

  const bang = impulse(t, tBang, 0.34);
  // energy 接受场景相对秒（内部会加回场景起点换成全片时间）
  const energy = beat.energy(beat.now);

  const urlP = ramp(t, [tUrl, tUrl + 0.35], [0, 1], EASE.out);
  // 光带扫过网址：每 2 小节一次，给"停留期"提供微动
  const sweep = loop(t, TIMING.music.bar * 2);

  const logo = TIMING.brand.logo.trim();
  const hasLogo = logo !== "";
  // 注：网址在屏时长 = ctaLength - tUrl（AGENTS.md 要求 ≥2.5 秒），但这个值**没有任何消费者**：
  // 字幕/网址的寿命由自己的 ramp 决定，布局也没有按它缩放。原来这里算出来立刻 `void urlHold`
  // 掉、注释却写「改 timing.json 的 cta 窗口后这里会跟着变」，是句空承诺，已删掉死变量。
  // 要真正约束「网址停留 ≥2.5 秒」，得把它用进渲染（或让闸门去判 timing.json 的 cta 窗口）。

  return (
    <SceneFrame name="cta">
      <AbsoluteFill style={{ background: PALETTE.bg }} />
      {/* 能量最高处：整块底被 accent 洗一遍，并随 drop 的余韵呼吸 */}
      <AbsoluteFill
        style={{
          background: `radial-gradient(90% 70% at 50% 42%, ${alpha(PALETTE.accent, 0.16 + energy * 0.14)} 0%, ${alpha(PALETTE.bg, 0)} 68%)`,
          opacity: clamp(0.35 + energy * 0.65, 0, 1),
        }}
      />
      <AbsoluteFill style={{ background: alpha(PALETTE.accent, bang * 0.18), transform: `scale(${1 + bang * 0.03})` }} />

      <Stage align="center" style={{ gap: height * 0.03 }}>
        {/* 品牌标记：有 logo 用 logo（真实项目应该给一个 png/svg），没有就用代码画一个首字方块 */}
        <div style={{ ...riseIn(t, tBang, 0.45, 30), display: "flex", alignItems: "center", gap: height * 0.016 }}>
          {hasLogo ? (
            // 真实 logo 也要守"不压扁"的规矩：objectFit contain + 固定方形盒子
            <Img
              src={staticFile(logo)}
              alt=""
              style={{ width: height * 0.085, height: height * 0.085, objectFit: "contain", display: "block" }}
            />
          ) : (
            <div
              style={{
                width: height * 0.085,
                height: height * 0.085,
                borderRadius: height * 0.024,
                background: PALETTE.accent,
                color: PALETTE.bg,
                display: "flex",
                alignItems: "center",
                justifyContent: "center",
                fontFamily: FONT.display,
                fontWeight: 800,
                fontSize: height * 0.045,
                lineHeight: 1,
              }}
            >
              {Array.from(TIMING.brand.name)[0] ?? "A"}
            </div>
          )}
          <span
            style={{
              fontFamily: FONT.display,
              fontSize: type.heading.size,
              fontWeight: 800,
              color: PALETTE.fg,
              letterSpacing: type.heading.letterSpacing,
            }}
          >
            {TIMING.brand.name}
          </span>
        </div>

        {/* 主标语：一句话说清"这是什么 + 现在就能用" */}
        <div
          style={{
            ...riseIn(t, tBang + TIMING.pace.stagger, 0.45, 26),
            fontFamily: FONT.display,
            fontSize: type.hero.size,
            fontWeight: type.hero.weight,
            lineHeight: type.hero.lineHeight,
            letterSpacing: type.hero.letterSpacing,
            color: PALETTE.fg,
            maxWidth: "94%",
            textWrap: "balance",
          }}
        >
          拍一张，就记完了
        </div>

        {/* 网址：全片最重要的一个元素，给最大的对比度和最长的停留 */}
        <div
          style={{
            opacity: urlP,
            transform: `translateY(${(1 - urlP) * 24}px) scale(${1 + bang * 0.04})`,
            position: "relative",
            overflow: "hidden",
            padding: `${type.subtitle.size * 0.5}px ${type.subtitle.size * 1.1}px`,
            borderRadius: 999,
            border: `${Math.max(2, type.subtitle.size * 0.06)}px solid ${alpha(PALETTE.accent, 0.7)}`,
            background: alpha(PALETTE.accent, 0.1 + energy * 0.06),
            boxShadow: `0 0 ${30 + energy * 40}px ${alpha(PALETTE.accent, 0.35)}`,
          }}
        >
          <span
            style={{
              fontFamily: FONT.display,
              fontSize: type.subtitle.size,
              fontWeight: 800,
              color: PALETTE.fg,
              letterSpacing: "0.02em",
              // 网址用等宽数字/字母更"像地址"，读起来更快
              fontVariantNumeric: "tabular-nums",
            }}
          >
            {TIMING.brand.url}
          </span>
          {/* 光带：每两小节从左扫到右，提示"这里可以点/可以记" */}
          <div
            style={{
              position: "absolute",
              top: 0,
              bottom: 0,
              width: "28%",
              left: `${lerp(-30, 102, sweep)}%`,
              background: `linear-gradient(90deg, ${alpha(PALETTE.fg, 0)} 0%, ${alpha(PALETTE.fg, 0.22)} 50%, ${alpha(PALETTE.fg, 0)} 100%)`,
              pointerEvents: "none",
            }}
          />
        </div>

        {/* 行动按钮：网址出现 1 秒后再给，避免两个重点同时抢注意力（同一镜头多焦点各 ≥1.2 秒） */}
        <div
          style={{
            ...riseIn(t, tBtn, 0.4, 22),
            display: "flex",
            alignItems: "center",
            gap: type.body.size * 0.5,
            padding: `${type.body.size * 0.7}px ${type.body.size * 1.6}px`,
            borderRadius: 999,
            background: PALETTE.fg,
            color: PALETTE.bg,
            fontFamily: FONT.display,
            fontSize: type.subtitle.size,
            fontWeight: 800,
            boxShadow: `0 ${type.body.size * 0.4}px ${type.body.size * 1.4}px ${alpha("#000", 0.55)}`,
          }}
        >
          免费下载
          <span style={{ fontSize: type.body.size * 1.2, lineHeight: 1 }}>→</span>
        </div>

        {/* 落款：把"免费/无广告"这类让人敢点的事实说清楚；出齐后停留到片尾 */}
        <div
          style={{
            ...riseIn(t, tFoot, 0.4, 16),
            fontFamily: FONT.body,
            fontSize: type.body.size,
            color: PALETTE.dim,
            letterSpacing: "0.04em",
          }}
        >
          iOS · Android · 免费
        </div>
      </Stage>

      {/* 片尾压暗：最后 0.6 秒轻微收黑，和混音的淡出对上（AGENTS.md 第 8 节） */}
      <AbsoluteFill
        style={{
          background: "#000",
          opacity: ramp(t, [ctaLength - 0.6, ctaLength], [0, 0.35], EASE.in),
          pointerEvents: "none",
        }}
      />
    </SceneFrame>
  );
};
