/**
 * Caption.tsx —— 屏幕大字（旁白字幕层）。
 *
 * 三条硬性要求来自 AGENTS.md 第 7 节，实现方式都在这个文件里：
 *   1) 竖屏单行 ≤ timing.captions.maxChars 字、最多 maxLines 行；
 *      超出的**在进场前就折行/截断**，绝不允许靠 CSS 溢出把字画到安全区外。
 *      ——CSS 的 `-webkit-line-clamp` 在不同 Chromium 版本上对 CJK 断行不一致，
 *        抽帧时才会发现多出一行，所以在 JS 里按字数断行是唯一稳的做法。
 *   2) 大字最多比旁白早 1.5 秒出现，停留 ≥ 字数/8 + 0.4 秒（见 holdFor）。
 *   3) 底衬/描边保证在任何画面上都能读：半透明底 + 描边 + 阴影三层，
 *      因为真实截图（Shot）的明暗不可控，纯白字压在浅色截图上会看不见。
 */
import type React from "react";
import { AbsoluteFill, useVideoConfig } from "remotion";
import { TIMING, type VoiceLine } from "../lib/timing";
import { useAbsTime } from "./Scene";
import { PALETTE, alpha, useType } from "../lib/theme";
import { FONT } from "../lib/fonts";
import { EASE, ramp } from "../lib/anim";

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
  Math.min(sceneEnd - CAPTION_TAIL, line.at + Math.max(holdFor(line.text), Array.from(line.text).length / VO_CHARS_PER_SEC + 0.35));

/**
 * 当前该显示哪一句。
 * 取"at ≤ now 的最后一句"，并保证它不会跨出自己所属场景的窗口 ——
 * 配音是被人为约束在场景窗口内的（tts.py audit 会检查），字幕跟着场景走才不会串场。
 */
export const activeLine = (now: number, lines: readonly VoiceLine[], scenes: readonly { name: string; start: number; end: number }[]): VoiceLine | null => {
  let found: VoiceLine | null = null;
  for (const line of lines) {
    if (line.at <= now) {
      const scene = scenes.find((s) => s.name === line.scene);
      const sceneEnd = scene ? scene.end : Number.POSITIVE_INFINITY;
      // 还没念完（收起时刻按语速估）/ 还在场景里 → 候选；否则视为已经过期
      if (now <= captionUntil(line, sceneEnd)) found = line;
    }
  }
  return found;
};

/**
 * 按字数折行。
 * 先算"塞进 maxLines 行每行最多几个字"，再从后往前丢弃多余的字 ——
 * 这样短句保持自然断行，长句只会被截尾并加省略号，不会出现一行挤 20 个字。
 */
export const wrapCJK = (text: string, maxChars: number, maxLines: number, ellipsis = "…"): string[] => {
  const chars = Array.from(text);
  const perLine = Math.max(1, maxChars);
  const capacity = perLine * Math.max(1, maxLines);
  const shown =
    chars.length <= capacity ? chars : [...chars.slice(0, Math.max(0, capacity - 1)), ellipsis];

  const lines: string[] = [];
  for (let i = 0; i < shown.length; i += perLine) {
    lines.push(shown.slice(i, i + perLine).join(""));
  }
  return lines;
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

  // 底部距离：用比例而不是像素，换画幅时字幕自动跟着走
  const bottomPx = Math.round(height * (bottom ?? cfg.bottom));

  return (
    <AbsoluteFill
      style={{
        // 字幕层不吃点击、不参与布局，纯覆盖；zIndex 高于画面内容但低于 URL/CTA 的强调层
        pointerEvents: "none",
        justifyContent: "flex-end",
        alignItems: "center",
        paddingBottom: bottomPx,
        zIndex: 40,
      }}
    >
      <div
        style={{
          maxWidth: "84%",
          opacity: p,
          transform: `translateY(${rise}px)`,
          display: "flex",
          flexDirection: "column",
          alignItems: "center",
          gap: "0.18em",
        }}
      >
        {lines.map((text, i) => (
          <span
            key={`${line.id}-${i}`}
            style={{
              fontFamily: FONT.display,
              fontSize: type.caption.size,
              fontWeight: type.caption.weight,
              lineHeight: type.caption.lineHeight,
              letterSpacing: type.caption.letterSpacing,
              color: PALETTE.fg,
              // 三层可读性保障：底衬 + 描边 + 投影。任何一层单独都可能被浅色截图吃掉。
              background: alpha(PALETTE.bg, 0.62),
              borderRadius: width * 0.018,
              padding: `${type.caption.size * 0.22}px ${type.caption.size * 0.5}px`,
              WebkitTextStroke: `${Math.max(1, type.caption.size * 0.022)}px ${alpha(PALETTE.bg, 0.55)}`,
              // paint-order 让描边画在字形外侧，中文字形不会因为描边变糊
              paintOrder: "stroke fill",
              textShadow: `0 ${type.caption.size * 0.05}px ${type.caption.size * 0.25}px ${alpha(PALETTE.bg, 0.85)}`,
              whiteSpace: "pre",
            }}
          >
            {text}
          </span>
        ))}
      </div>
    </AbsoluteFill>
  );
};
