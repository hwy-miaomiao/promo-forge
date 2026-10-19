/**
 * scenes/Hook.tsx —— 0~4.5 秒的痛点开场。
 *
 * 节奏依据（AGENTS.md 第 4 节，改时间前先看这张表）：
 *   - 开场 2.0 秒内必须抛出痛点 → 大字问句 tQuestion = 1.5 秒（第 3 拍）起浮现，+0.38 秒 = 1.88 秒完全出现；
 *   - 元素逐个出现间隔 ≥0.25 秒 → 12 组划记用 stagger(i, TIMING.pace.stagger) = 0.25 秒；
 *   - 出齐后停留 ≥0.6 秒 → ⚠️ 这条在 4.5 秒的窗口里做不到：12 组 × 0.25 = 2.75 秒，加上最后一组
 *     第 4 根线画满要再 0.63 秒，整页 3.38 秒才铺满（tTally=0 起算），而 tStrike 这一拍是 3.0 秒、
 *     下一个 drop 拍是 3.5 秒 —— 铺满后只剩 0.12 秒。取舍：先保"间隔 ≥0.25 秒"和"划掉要等
 *     12 组铺满"，剩下的窗口全留给划线后的画面；
 *   - 全片第一次能量爆发最晚第 4 秒 → 画面整体在 drop 那一拍（绝对 3.6s → 相对 3.5s）抽一下（scale + 光晕）。
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
import { PALETTE, SAFE, alpha, captionBandHeight, captionBottomPx, isVertical, safePadding, useType } from "../lib/theme";
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
        // 组内四根线依次出现：i 越大起步越晚，但 **on=1（这一组铺满）时四根必须同时到 1**。
        // 步长 0.21 + 爬升 0.37 → 第 4 根（i=3）在 on = 3×0.21 + 0.37 = 1.00 时正好画满。
        // 旧写法 `(on - i * 0.22) / 0.4` 对 i=3 需要 on = 1.06，而外层 clamp(...,0,1) 的上限是 1.00，
        // 于是第 4 根永远只画到 (1.00-0.66)/0.4 = 85%，每一组的最后一笔都是缺的。
        const p = clamp((on - i * 0.21) / 0.37, 0, 1);
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
  // 当前 timing.json：beat=0.5 / bar=2.0 / music.offset=0、hook 的 start=0，
  // 所以 beat.snap() 就是「最近的 0.5 秒整数倍」——下面五个卡点吸附后分别是：
  //   tTally 0 → tTitle 0.5 → tSub 1.0 → tQuestion 1.5 → tStrike 3.0 → tPunch 3.5
  // 划记的铺满时刻按 stagger 实算（TIMING.pace.stagger=0.25，组内第 4 根线在 on=1 时画满）：
  //   第 i 组起 tTally + i×0.25，第 4 根线在 +0.63 秒到顶 ⇒ 第 12 组 3.38 秒铺满，
  // 所以 tStrike 取 3.0——它是**最后一拍（3.0）**，而 3.38 秒的完全铺满落在 3.0~3.5 之间，
  // 划掉的动作接着铺满发生，不会切在半成品上。
  //   （这段注释早先照抄的是 beat=0.4615 / offset≈0.12 那一版 timing 的拍点网格，与本文件
  //    现在读到的 timing.json 对不上；已按当前读数重算。）
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

  // ---- 尺寸：只按宽度算会在横版 16:9 上翻车（pr-b5 横版 1.8s 帧：hero 压在字幕上）----
  // 竖屏内容高够用，横版高度只有 1080、字幕带却吃掉近 0.39 屏高（字号按 1280 基准放大），
  // 所以横版必须①按高度夹格子尺寸、②把内容拆成两栏、③给底部字幕带让位。竖屏保持原样。
  const vertical = isVertical(width, height);
  const horizontal = !vertical;
  const safe = safePadding(width, height);
  const stageW = Math.min(
    width - safe.left - safe.right,
    Math.round(width * (vertical ? SAFE.contentWidth.vertical : SAFE.contentWidth.horizontal)),
  );
  // 字幕带高度按 Caption.tsx 的口径算：底边 captions.bottom + maxLines 行 + 行距。
  // 字号与硬字幕 ASS 同源（lib/theme 的 captionBandHeight / captionFontSize，R5-TPL-03），
  // 字幕字号一改这里自动跟着变，不会再出现"字号改了但让位没改"的压字。
  const captionBottom = TIMING.captions.enabled ? captionBottomPx(height, TIMING.captions.bottom) : 0;
  const capBand = TIMING.captions.enabled
    ? captionBottom + captionBandHeight(width, height, TIMING.captions.maxChars, TIMING.captions.maxLines)
    : 0;
  // 竖屏内容在字幕带之上本来就放得下（原样保留，避免视觉回归）；横版必须让出字幕带
  const bottomExtra = vertical ? 0 : Math.max(0, Math.round(capBand - safe.bottom + height * 0.012));

  // 一整页手写记录：4 列 × 3 行 = 12 组
  const groups = 12;
  const cols = 4;
  // 横版两栏时左栏宽度（放标题 + 账本）、右栏（问句）占剩下的；46/54 是试出来的：
  // 左栏再宽一点，问句会被挤成 3 行、断在词中间（"每天记 / 账要花十 / 几分钟?"）。
  const ledgerW = horizontal ? stageW * 0.46 : stageW;
  const gapX = height * 0.018;
  const colW = (ledgerW - (cols - 1) * gapX) / cols;
  // Tally 的 svg 宽 = size × 1.45（size = cell / 2），先按列宽夹一次，再按"高度基准"夹一次：
  // 横版 min(width, height*1.7) = 1836 → 247.9，竖屏 min(1080, 3264) = 1080 → 145.8（与原值一致）
  const cell = Math.min(width * 0.135, Math.min(width, height * 1.7) * 0.135, colW * 1.379);

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

      <Stage align="center" bottomExtra={bottomExtra} style={{ gap: height * 0.022 }}>
        {/* 横版（16:9）把四个块拆成两栏：左栏「标题 + 账本」、右栏「问句」。
            竖屏保持原来的单列竖排 —— 竖屏内容高够用，改了反而会动到已经调好的构图。 */}
        <div
          style={{
            display: "flex",
            flexDirection: horizontal ? "row" : "column",
            alignItems: "center",
            justifyContent: "center",
            gap: height * 0.022,
            width: "100%",
          }}
        >
          <div
            style={{
              display: "flex",
              flexDirection: "column",
              alignItems: "center",
              gap: height * 0.022,
              width: horizontal ? "46%" : "100%",
            }}
          >
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
          </div>

          <div
            style={{
              flex: horizontal ? 1 : undefined,
              display: "flex",
              flexDirection: "column",
              alignItems: "center",
              gap: height * 0.022,
            }}
          >
        {/* 痛点问句：tQuestion = 1.5 秒（第 3 拍）起浮现，+0.38 秒 = 1.88 秒完全落位（≤2.0 秒底线） */}
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
          </div>
        </div>
      </Stage>
    </SceneFrame>
  );
};
