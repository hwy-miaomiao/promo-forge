/**
 * Caption.tsx —— 屏幕大字（旁白字幕层）。
 *
 * 三条硬性要求来自 AGENTS.md 第 7 节，实现方式都在这个文件里：
 *   1) 竖屏单行 ≤ timing.captions.maxChars 字、最多 maxLines 行；
 *      超出的**在进场前就折行/截断**，绝不允许靠 CSS 溢出把字画到安全区外。
 *      ——CSS 的 `-webkit-line-clamp` 在不同 Chromium 版本上对 CJK 断行不一致，
 *        抽帧时才会发现多出一行，所以在 JS 里按字数断行是唯一稳的做法。
 *      折行算法与 scripts/subtitles.py 的 `_fold_text()` 同一套（见 lib/captionText.ts）：
 *      避头尾、优先标点边界、拉丁词/URL/数字不拆、最后一行不会只剩标点或单字。
 *   2) 大字最多比旁白早 1.5 秒出现，停留 ≥ 字数/8 + 0.4 秒（见 holdFor）。
 *   3) 底衬/描边保证在任何画面上都能读：半透明底 + 描边 + 阴影三层，
 *      因为真实截图（Shot）的明暗不可控，纯白字压在浅色截图上会看不见。
 *
 * 字号与左右安全区跟**硬字幕 ASS 同一口径**（R5-TPL-03）：不用 useType().caption.size
 * （那是按屏宽缩放的 51.9px），而是用 lib/theme 的 captionFontSize() / captionSideInsets()，
 * 与 scripts/subtitles.py 的 `_build_ass()` 用同一公式（1080×1920 → 66px；1920×1080 → 41px）。
 */
import type React from "react";
import { AbsoluteFill, useVideoConfig } from "remotion";
import { TIMING, type VoiceLine } from "../lib/timing";
import { useAbsTime } from "./Scene";
import {
  CAPTION_GAP,
  CAPTION_PAD_Y,
  PALETTE,
  alpha,
  captionBottomPx,
  captionFontSize,
  captionSideInsets,
  useType,
} from "../lib/theme";
import { visLen, wrapCJK } from "../lib/captionText";
import { FONT } from "../lib/fonts";
import { EASE, ramp } from "../lib/anim";

/** 兼容旧引用：折行实现已移到 lib/captionText.ts（可单独打包做断言测试）。 */
export { wrapCJK };

/** 字幕淡入时长（秒）。0.15 秒≈9 帧，短到不像动画，但足以避免"啪"地跳出来。 */
export const CAPTION_FADE = 0.15;

/** 按字数算这句字幕**至少要**停留多久：字数 / 8 + 0.4 秒（AGENTS.md 阶段 7 的下限）。 */
export const holdFor = (text: string): number => Array.from(text).length / 8 + 0.4;

/**
 * 念白语速估算（字/秒）：AGENTS.md 阶段 7 要求中文旁白按 5.5~6.5 字/秒写，这里取中值 6.0。
 * 真实时长以 audio-src/vo/report.json 为准（tts.py 生成后才有），但那个文件只有脚本侧能读，
 * 画面里拿不到，所以只能按语速估。+0.35 是收尾留白（换气 + 尾音）。
 */
export const VO_CHARS_PER_SEC = 6.0;
/** 字幕必须在本场景结束前这么早就收起，免得压到下一场的画面。 */
export const CAPTION_TAIL = 0.15;

/**
 * 这句字幕该收起的时刻（绝对秒）。
 * = min(本场末 − 0.15, line.at + max(holdFor(text), 字数 / 6.0 + 0.35))
 *
 * ❗不能拿 holdFor() 当"消失时刻"：它是**下限**，只保证不短于它。示例里 16 字的第 01 句
 * 按 字数/8+0.4 只有 2.4 秒，而真人念完要 ~3.0 秒 —— 字幕会比语音早半秒消失。
 */
export const captionUntil = (line: VoiceLine, sceneEnd: number): number =>
  Math.min(
    sceneEnd - CAPTION_TAIL,
    line.at + Math.max(holdFor(line.text), Array.from(line.text).length / VO_CHARS_PER_SEC + 0.35),
  );

/**
 * 当前该显示哪一句。
 * 在「at ≤ now 且还没收起」的候选里取 **at 最大**的一句 —— 不能取数组里的最后一项：
 * `voice.lines` 不保证按 at 升序（脚本只校验每句 at 落在所属场景窗口内），
 * 一旦数据没排序而两句字幕窗口又重叠，画面会退回显示较早那句（R7-TS-RO3）。
 * 同时保证字幕不跨出自己所属场景的窗口 —— 配音被人为约束在场景窗口内
 * （tts.py audit 会检查），字幕跟着场景走才不会串场。
 */
export const activeLine = (
  now: number,
  lines: readonly VoiceLine[],
  scenes: readonly { name: string; start: number; end: number }[],
): VoiceLine | null => {
  let found: VoiceLine | null = null;
  for (const line of lines) {
    if (line.at <= now) {
      const scene = scenes.find((s) => s.name === line.scene);
      const sceneEnd = scene ? scene.end : Number.POSITIVE_INFINITY;
      // 还没念完（收起时刻按语速估）/ 还在场景里 → 候选；否则视为已经过期
      if (now <= captionUntil(line, sceneEnd) && (found === null || line.at >= found.at)) found = line;
    }
  }
  return found;
};

export type CaptionProps = {
  /** 覆盖默认的底部距离比例（占画面高），不传则读 timing.captions.bottom */
  bottom?: number;
};

export const Caption: React.FC<CaptionProps> = ({ bottom }) => {
  const { width, height } = useVideoConfig();
  const type = useType();
  const now = useAbsTime();
  const cfg = TIMING.captions;

  // captions.enabled=false 时整层不渲染（用户可以用旁白不带字幕做一版）
  if (!cfg.enabled) return null;

  const line = activeLine(now, TIMING.voice.lines, TIMING.scenes);
  if (!line) return null;

  const lines = wrapCJK(line.text, cfg.maxChars, cfg.maxLines);
  const p = ramp(now, [line.at, line.at + CAPTION_FADE], [0, 1], EASE.out);
  const rise = (1 - p) * 18; // 轻微上移，陪衬淡入，避免"贴纸感"

  // 字号 / 左右留白与硬字幕 ASS 同一口径（R5-TPL-03）：见 lib/theme 的 captionFontSize 注释。
  const size = captionFontSize(width, height, cfg.maxChars);
  const insets = captionSideInsets(width);
  const availW = Math.max(1, width - insets.left - insets.right);

  // 底部距离：用比例而不是像素，换画幅时字幕自动跟着走。
  // 与硬字幕的 MarginV 同口径（R6）：非法值 / 落在安全区外的值会被夹回安全区，
  // 不能像以前那样直接把 bottom=1.5 乘成 2880px 把整层字幕推出画面（渲染却退出 0）。
  const bottomPx = captionBottomPx(height, bottom ?? cfg.bottom);

  return (
    <AbsoluteFill
      style={{
        // 字幕层不吃点击、不参与布局，纯覆盖；zIndex 高于画面内容但低于 URL/CTA 的强调层
        pointerEvents: "none",
        justifyContent: "flex-end",
        paddingBottom: bottomPx,
        // 与 ASS 的 MarginL / MarginR 对齐：Alignment=2 是在「左右留白之间」居中，
        // 不是整幅画面居中（竖屏左右留白不对称：6% / 14%）。不加这层 padding 的话，
        // 13 个全角字 × 66px 会越到右侧安全区外面（R5-TPL-03）。
        paddingLeft: insets.left,
        paddingRight: insets.right,
        zIndex: 40,
      }}
    >
      <div
        style={{
          width: "100%",
          opacity: p,
          transform: `translateY(${rise}px)`,
          display: "flex",
          flexDirection: "column",
          alignItems: "center",
          // ❗必须用 px：写成 `0.18em` 时 em 相对的是这个 div 继承来的 16px，
          // 实际只有 2.9px，跟 Hook/Demo 里按「0.18 × 字幕字号」预留的间距对不上。
          gap: size * CAPTION_GAP,
        }}
      >
        {lines.map((text, i) => {
          // 底衬左右内边距：13 字 × 66px 正好卡满 864px 安全区（只剩 6px），
          // 不能再像小字号时那样用 0.5em 把底衬撑出去。按这一行的估计宽度夹一次，
          // 底衬与字形都留在安全区内；字号本身不变（R5-TPL-03）。
          // 1.05 是保守系数：半角字形的实际前进宽度比 0.5em 略宽。
          const estimated = Math.ceil(visLen(text) * size * 1.05);
          const padX = Math.max(2, Math.min(Math.round(size * 0.5), Math.floor((availW - estimated) / 2)));
          return (
            <span
              key={`${line.id}-${i}`}
              style={{
                fontFamily: FONT.display,
                fontSize: size,
                fontWeight: type.caption.weight,
                lineHeight: type.caption.lineHeight,
                letterSpacing: type.caption.letterSpacing,
                color: PALETTE.fg,
                // 三层可读性保障：底衬 + 描边 + 投影。任何一层单独都可能被浅色截图吃掉。
                background: alpha(PALETTE.bg, 0.62),
                borderRadius: width * 0.018,
                padding: `${size * CAPTION_PAD_Y}px ${padX}px`,
                WebkitTextStroke: `${Math.max(1, size * 0.022)}px ${alpha(PALETTE.bg, 0.55)}`,
                // paint-order 让描边画在字形外侧，中文字形不会因为描边变糊
                paintOrder: "stroke fill",
                textShadow: `0 ${size * 0.05}px ${size * 0.25}px ${alpha(PALETTE.bg, 0.85)}`,
                whiteSpace: "pre",
              }}
            >
              {text}
            </span>
          );
        })}
      </div>
    </AbsoluteFill>
  );
};
