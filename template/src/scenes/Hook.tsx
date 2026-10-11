/**
 * scenes/Hook.tsx —— 0~4.5 秒的痛点开场。
 *
 * 节奏依据（AGENTS.md 第 4 节，改时间前先看这张表）：
 *   - 开场 2.0 秒内必须抛出痛点 → 大字问句 1.618 秒（拍点）起浮现，1.998 秒完全出现；
 *   - 元素逐个出现间隔 ≥0.25 秒 → 12 组划记用 stagger(i, TIMING.pace.stagger) = 0.25 秒；
 *   - 出齐后停留 ≥0.6 秒 → ⚠️ 这条在 4.5 秒的窗口里做不到：12 组 × 0.25 = 2.75 秒，
 *     最后一组 3.207 秒才铺满，而下一拍 3.62 秒就是 drop，最多只剩 0.41 秒。
 *     取舍：先保"间隔 ≥0.25 秒"和"划掉要等 12 组铺满"，铺满后 0.09 秒就划线，之后一直留到场景结束；
 *   - 全片第一次能量爆发最晚第 4 秒 → 画面整体在 drop(3.6s) 位置抽一下（scale + 光晕）。
 * 所有切换点都走 `beat.snap(...)`（吃**场景相对秒**），换歌换 BPM 不用改这个文件。
 *
 * ⚠️ 本场景**零外部素材**：格子、计时器、划线全部用代码画。
 * 真实项目要换成产品真实界面（Shot / PhoneFrame + 截图），见 Demo.tsx 的注释。
 *
 * 时间基准：t = useSceneTime("hook") 是**本场景相对秒**；卡点一律用
 * beat.now / beat.at(i) / beat.snap(...)（同样相对），绝对秒只有 beat.abs。
 */
import type React from "react";
import { AbsoluteFill, useVideoConfig } from "remotion";
import { SceneFrame, useSceneTime } from "../components/Scene";
import { Stage } from "../components/Stage";
import { PALETTE, alpha, useType } from "../lib/theme";
import { FONT } from "../lib/fonts";
import { EASE, clamp, impulse, loop, ramp, riseIn, shake, stagger } from "../lib/anim";
import { TIMING } from "../lib/timing";
import { useSceneBeat } from "../lib/beats";

/** 划掉标记（4 根竖线 + 1 根斜线），纯矢量：任何分辨率都不糊。 */
const Tally: React.FC<{ size: number; on: number; color: string; strike?: number }> = ({ size, on, color, strike = 0 }) => {
  const bars = [0, 1, 2, 3];
  return (
    <svg width={size * 1.45} height={size} viewBox={`0 0 ${size * 1.45} ${size}`} style={{ display: "block" }}>
      {bars.map((i) => {
        // 组内四根线依次出现（每根差 0.22 个进度），整体由外层的 on 驱动
        const p = clamp((on - i * 0.22) / 0.4, 0, 1);
        return (
          <rect
            key={i}
            x={i * size * 0.24}
            y={(1 - p) * size}
            width={size * 0.08}
            height={p * size}
            rx={size * 0.04}
            fill={color}
            opacity={0.55 + p * 0.45}
          />
        );
      })}
      {/* 第五笔：斜着划掉这一组。strike 0→1 时从左下走到右上 */}
      {strike > 0 ? (
        <line
          x1={size * 0.02}
          y1={size * (0.96 - 0.02 * strike)}
          x2={size * 0.02 + size * 1.4 * strike}
          y2={size * (0.96 - 0.8 * strike)}
          stroke={color}
          strokeWidth={size * 0.09}
          strokeLinecap="round"
          opacity={0.9}
        />
      ) : null}
    </svg>
  );
};

export const Hook: React.FC = () => {
  const t = useSceneTime("hook");
  const type = useType();
  const { width, height } = useVideoConfig();
  const beat = useSceneBeat("hook");

  // ---- 卡点：一律用 beat.snap() 吸附到拍上（吃场景相对秒，返回的也是相对秒）----
  // 拍点网格：t = 2.619 + n × 0.5005 → 0.1165 / 0.617 / 1.1175 / 1.618 / 2.619 / 3.1195 / 3.62 …
  // ❗12 组划记按 0.25 秒铺要 2.75 秒才铺得满，所以它必须从**最早那一拍**起步，其余元素往后排：
  //   划记 0.1165 → 标题 0.617（"第 1 天"再 +0.25 = 0.867）→ 副句 1.1175 → 问句 1.618
  //   → 划掉 3.1195（最后一组 3.207 铺满后的第一拍）→ drop 脉冲 3.62
  const tTally = beat.snap(0); // 划记页从第一拍就开始铺，后面的元素才有位置
  const tTitle = beat.snap(0.5);
  const tSub = beat.snap(1.0);
  const tQuestion = beat.snap(1.5);
  const tStrike = beat.snap(3.0);
  // drop 是**绝对**秒（3.6），先换成本场景相对秒才能和 t 比 —— 这正是模板第一版翻车的地方
  const tPunch = beat.snap(TIMING.music.drop - beat.start);
  // 拍点脉冲：drop 那一下给整层一个短促的缩放 + 光晕
  const punch = impulse(t, tPunch, 0.32);
  const energy = beat.energy(beat.now);

  // 一整页手写记录：4 列 × 3 行 = 12 组
  const groups = 12;
  const cols = 4;
  const cell = width * 0.135;

  // 计时器：从 0:00 涨到 9:58，把"十几分钟"变成看得见的数字
  const timerP = ramp(t, [tTally, tPunch], [0, 1], EASE.soft);
  const timerSec = Math.round(timerP * 598);
  const timerText = `${Math.floor(timerSec / 60)}:${String(timerSec % 60).padStart(2, "0")}`;

  // 整页划掉：一条从左下到右上的粗斜线
  const strike = ramp(t, [tStrike, tStrike + 0.28], [0, 1], EASE.out);
  // 呼吸：每小节一次，保证两拍之间画面也不是死的
  const breathe = loop(t, TIMING.music.bar);

  return (
    <SceneFrame name="hook">
      <AbsoluteFill style={{ background: PALETTE.bg }} />
      {/* 能量光晕：跟着 drop 的余韵起伏 */}
      <AbsoluteFill
        style={{
          background: `radial-gradient(70% 50% at 50% 38%, ${alpha(PALETTE.accent, 0.05 + energy * 0.1)} 0%, ${alpha(PALETTE.bg, 0)} 72%)`,
        }}
      />
      {/* drop 那一击：整层白闪 + 抽动。shake 用 remotion 的 random()，同一帧永远同一个结果 */}
      <AbsoluteFill
        style={{
          background: alpha(PALETTE.accent, punch * 0.16),
          transform: `translate(${shake(t, [tPunch], 10, 0.3)}px, ${shake(t, [tPunch + 0.02], 8, 0.26)}px) scale(${1 + punch * 0.015})`,
        }}
      />

      <Stage align="center" style={{ gap: height * 0.022 }}>
        {/* 第一行：这是什么 + 现在几秒 */}
        <div style={{ display: "flex", alignItems: "baseline", justifyContent: "space-between", width: "100%" }}>
          <div
            style={{
              ...riseIn(t, tTitle, 0.4, 22),
              fontFamily: FONT.display,
              fontSize: type.subtitle.size,
              fontWeight: 800,
              color: PALETTE.accent,
              letterSpacing: "0.06em",
            }}
          >
            手工记账
          </div>
          <div
            style={{
              ...riseIn(t, tTitle + stagger(1, TIMING.pace.stagger), 0.4, 18),
              fontFamily: FONT.mono,
              fontSize: type.subtitle.size,
              color: PALETTE.dim,
            }}
          >
            第 1 天
          </div>
        </div>

        {/* 一整页假账本：12 组划记 + 一条划掉线 */}
        <div
          style={{
            position: "relative",
            width: "100%",
            padding: `${height * 0.03}px 0`,
            display: "grid",
            gridTemplateColumns: `repeat(${cols}, 1fr)`,
            gap: height * 0.018,
            justifyContent: "center",
            justifyItems: "center",
            background: alpha(PALETTE.fg, 0.04),
            borderRadius: height * 0.022,
            border: `1px solid ${PALETTE.border}`,
          }}
        >
          {Array.from({ length: groups }).map((_, i) => {
            // 每组出现时刻 = tTally + stagger(i, TIMING.pace.stagger)，组内再拆四根线
            const on = clamp((t - (tTally + stagger(i, TIMING.pace.stagger))) / 0.34, 0, 1);
            return <Tally key={i} size={cell * 0.5} on={on} color={on > 0 ? PALETTE.fg : PALETTE.dim} strike={0} />;
          })}

          {/* 划掉整页：颜色用 accent，粗细随宽度缩放，永远覆盖得住 12 组 */}
          {strike > 0 ? (
            <div
              style={{
                position: "absolute",
                left: "-2%",
                top: "50%",
                width: `${strike * 104}%`,
                height: Math.max(6, height * 0.008),
                background: PALETTE.accent,
                borderRadius: 999,
                transform: "rotate(-6deg)",
                transformOrigin: "left center",
                boxShadow: `0 0 ${24 * strike}px ${alpha(PALETTE.accent, 0.6)}`,
              }}
            />
          ) : null}

          {/* 计时器：压在账本右下角，数字滚动 = "时间在流走" */}
          <div
            style={{
              position: "absolute",
              right: width * 0.02,
              bottom: -height * 0.012,
              padding: `${type.tiny.size * 0.5}px ${type.tiny.size * 0.9}px`,
              borderRadius: 999,
              background: PALETTE.bg,
              border: `1px solid ${alpha(PALETTE.accent, 0.5)}`,
              fontFamily: FONT.mono,
              fontSize: type.subtitle.size,
              fontWeight: 700,
              color: timerP > 0.98 ? PALETTE.accent : PALETTE.fg,
              fontVariantNumeric: "tabular-nums",
              opacity: ramp(t, [tTally, tTally + 0.3], [0, 1], EASE.out),
              transform: `scale(${1 + punch * 0.08})`,
            }}
          >
            {timerText}
          </div>
        </div>

        {/* 痛点问句：1.618 秒（拍点）起浮现，+0.38 秒 = 1.998 秒完全落位（≤2.0 秒底线） */}
        <div
          style={{
            ...riseIn(t, tQuestion, 0.38, 26),
            fontFamily: FONT.display,
            fontSize: type.hero.size,
            fontWeight: type.hero.weight,
            lineHeight: type.hero.lineHeight,
            letterSpacing: type.hero.letterSpacing,
            color: PALETTE.fg,
            textAlign: "center",
            textWrap: "balance",
            transform: `translateY(${(1 - ramp(t, [tQuestion, tQuestion + 0.38], [0, 1], EASE.out)) * 26}px) scale(${1 + punch * 0.02})`,
          }}
        >
          每天记账要花十几分钟？
        </div>

        {/* 副问句：把"十几分钟"具体成"一页手写"，让痛点落地 */}
        <div
          style={{
            ...riseIn(t, tSub, 0.4, 18),
            fontFamily: FONT.body,
            fontSize: type.body.size,
            color: PALETTE.dim,
            letterSpacing: "0.02em",
            opacity:
              ramp(t, [tSub, tSub + 0.4], [0, 1], EASE.out) *
              (0.86 + 0.14 * Math.abs(Math.sin(breathe * Math.PI))),
          }}
        >
          拍一张小票就能记完的事，为什么要手写一整页
        </div>
      </Stage>
    </SceneFrame>
  );
};
