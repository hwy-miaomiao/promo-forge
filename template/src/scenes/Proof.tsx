/**
 * scenes/Proof.tsx —— 26~36 秒的"真实作品"快切蒙太奇。
 *
 * ⚠️⚠️ 这个文件是**必须被替换**的：AGENTS.md 阶段 4 的硬性配额写明
 * "必须有一段由 ≥6 张真实作品组成的快切蒙太奇"，而这里现在画的是**代码占位卡片**，
 * 只为让模板零素材也能渲出画面。真实项目要做的事：
 *
 *   1) 用 scripts/capture.py --factor 2 抓 6 张以上 2 倍图，放进 public/img/；
 *   2) 把 WorkCard 里 <CodeCardArt> 那一块整体换成
 *      `<Shot src="img/work-01.png" ratio={4 / 3} fit="cover" />`；
 *   3) 卡片的出现时间**不要动** —— 它们已经落在 beat 网格上（间隔 ≥0.5 秒）。
 *
 * 节奏依据：快切镜头 ≥0.5 秒（这里每张 1.0~1.25 秒，超过底线而不是贴着底线，
 * 因为要留时间给观众认出"这是个真实作品"）；切换点全部吸附到 beat。
 *
 * 时间基准：t = useSceneTime("proof") 是**本场景相对秒**，而 timing.json 里的
 * scenes[].start 是绝对秒。卡点一律用 `beat.at(i)`（自动减掉 26），
 * 直接把纯函数 `beatAt()` 的绝对秒拿来和 t 比大小，就会得到"整场只剩字幕"的黑屏。
 */
import type React from "react";
import { AbsoluteFill, useVideoConfig } from "remotion";
import { SceneFrame, useSceneTime } from "../components/Scene";
import { Stage } from "../components/Stage";
import { PALETTE, SAFE, alpha, isVertical, safePadding, useType } from "../lib/theme";
import { FONT } from "../lib/fonts";
import { EASE, clamp, impulse, ramp, riseIn, stagger } from "../lib/anim";
import { TIMING } from "../lib/timing";
import { snap, useSceneBeat } from "../lib/beats";

type ProofItem = {
  /** 作品名（将来就是截图里那个产品/项目名） */
  title: string;
  /** 一句话说明它是什么 */
  kind: string;
  /** 网格行，2×2 或 1×2 */
  span: string;
  /** 底色，暗/亮交替，让快切有节奏感而不只是"换了个字" */
  tone: "panel" | "accent";
  /** 占位用的假"作品画面"轮廓（真实项目删掉） */
  art: number[][];
};

/** 6 张作品：交替 2×2 与 1×2，避免"同一版式连用"。 */
const ITEMS: ProofItem[] = [
  { title: "溪木民宿", kind: "品牌落地页", span: "span 2 / span 2", tone: "panel", art: [[0, 0, 6, 4], [6, 0, 6, 2], [6, 2, 3, 2], [9, 2, 3, 2], [0, 4, 12, 2]] },
  { title: "小满日历", kind: "iOS 应用", span: "span 1 / span 2", tone: "accent", art: [[0, 0, 12, 3], [0, 3, 5, 3], [5, 3, 7, 3]] },
  { title: "拾光咖啡", kind: "小程序点单", span: "span 1 / span 2", tone: "panel", art: [[0, 0, 12, 2], [0, 2, 6, 4], [6, 2, 6, 4]] },
  { title: "棱镜数据", kind: "后台仪表盘", span: "span 2 / span 2", tone: "accent", art: [[0, 0, 12, 1], [0, 1, 3, 5], [3, 1, 9, 3], [3, 4, 9, 2]] },
  { title: "海风笔记", kind: "写作工具", span: "span 1 / span 2", tone: "panel", art: [[0, 0, 12, 5], [2, 1, 8, 3]] },
  { title: "纸鸢旅行", kind: "行程分享页", span: "span 1 / span 2", tone: "accent", art: [[0, 0, 12, 2], [0, 2, 4, 4], [4, 2, 8, 4]] },
];

/** 卡片出现的节拍索引：前 3 张每 2 拍（1.0 秒），后 3 张每 2 拍但整体右移半拍，形成"加速感"。 */
const BEAT_INDEX = [0, 2, 4, 6.5, 8.5, 10.5];

/** 代码画的占位"作品画面"：用 art 里的矩形拼一张抽象的界面缩略图。 */
const CodeCardArt: React.FC<{ art: number[][]; bright: boolean }> = ({ art, bright }) => (
  <div style={{ display: "grid", gridTemplateColumns: "repeat(12, 1fr)", gridTemplateRows: "repeat(6, 1fr)", gap: "0.6%", width: "100%", height: "100%" }}>
    {art.map(([x, y, w, h], i) => (
      <div
        key={i}
        style={{
          gridColumn: `${x + 1} / span ${w}`,
          gridRow: `${y + 1} / span ${h}`,
          borderRadius: "6%",
          background: bright ? alpha(PALETTE.bg, 0.18 + (i % 3) * 0.12) : alpha(PALETTE.fg, 0.08 + (i % 3) * 0.06),
        }}
      />
    ))}
  </div>
);

/**
 * 一张作品卡。
 * 尺寸用**算出来的像素值**，而不是 CSS transform 缩放，也不是 useMeasure 量 DOM：
 *   - transform: scale 会把边框和文字一起放大，看起来就像"这不是真实截图，是放大过的图"；
 *   - 量 DOM 在 Remotion 里每个场景要多测一次布局，还会在首帧拿不到值（而且
 *     remotion 4.0.529 并没有导出 useMeasure）。
 * Stage 的内容宽度、安全区、卡片区行高都能从 timing.json + useVideoConfig() 算出来，
 * 所以直接算，稳定又可复现。
 */
const WorkCard: React.FC<{
  width: number;
  height: number;
  p: number;
  hit: number;
  scale: number;
  item: ProofItem;
}> = ({ width, height, p, hit, scale, item }) => {
  const type = useType();
  const bright = item.tone === "accent";
  return (
    <div
      style={{
        gridArea: item.span,
        borderRadius: height * 0.045,
        padding: height * 0.03,
        display: "flex",
        flexDirection: "column",
        justifyContent: "space-between",
        background: bright ? PALETTE.accentDeep : PALETTE.panel,
        border: `1px solid ${bright ? alpha(PALETTE.accent, 0.45) : PALETTE.border}`,
        // 快切卡片用"从卡片方向推进"的入场：不是纯透明度，让切换更有方向感
        opacity: p,
        transform: `perspective(900px) rotateY(${(1 - p) * 14}deg) scale(${scale})`,
        boxShadow: `0 ${height * 0.02}px ${height * 0.05}px ${alpha("#000", 0.5 + hit * 0.2)}`,
        overflow: "hidden",
      }}
    >
      {/* ↓↓↓ 真实项目：把这一块整体换成 <Shot src="img/work-01.png" fit="cover" /> */}
      <div style={{ position: "relative", flex: 1, borderRadius: height * 0.028, overflow: "hidden", background: alpha(PALETTE.bg, 0.55) }}>
        <AbsoluteFill style={{ padding: height * 0.035 }}>
          <CodeCardArt art={item.art} bright={bright} />
        </AbsoluteFill>
      </div>
      {/* ↑↑↑ 替换到此为止，下面的署名栏保留（真实作品也需要标题） */}

      <div style={{ marginTop: height * 0.03, display: "flex", alignItems: "baseline", justifyContent: "space-between", gap: width * 0.02 }}>
        <span style={{ fontFamily: FONT.display, fontSize: type.heading.size, fontWeight: 800, color: bright ? PALETTE.bg : PALETTE.fg, whiteSpace: "nowrap" }}>
          {item.title}
        </span>
        <span style={{ fontFamily: FONT.body, fontSize: type.tiny.size, color: bright ? alpha(PALETTE.bg, 0.75) : PALETTE.dim, whiteSpace: "nowrap" }}>
          {item.kind}
        </span>
      </div>
    </div>
  );
};

export const Proof: React.FC = () => {
  const t = useSceneTime("proof");
  const type = useType();
  const { width, height } = useVideoConfig();
  const beat = useSceneBeat("proof");

  // ---- 卡片尺寸：全部算出来，不量 DOM ------------------------------------
  // Stage 的内容宽度 = 画面宽 - 左右安全区，再夹到 SAFE.contentWidth（竖屏 84% / 横版 68%）
  const inner = safePadding(width, height);
  const contentW = Math.min(
    width - inner.left - inner.right,
    width * (isVertical(width, height) ? SAFE.contentWidth.vertical : SAFE.contentWidth.horizontal),
  );
  const gap = height * 0.018;
  // 卡片区固定成"舞台高度的一半"：竖屏 1920 的一半约 960px，两行两列后每格约 470×470，
  // 正好是 1:1 的方形卡 —— 真实截图里方形缩略图最耐看，也最容易裁。
  const gridW = contentW;
  const gridH = height * 0.5;
  const cellW = (gridW - gap) / 2;
  const cellH = (gridH - gap) / 2;

  // 换真实截图时的出图尺寸（check_assets.py 要求图片宽度 ≥ 显示宽度 ×2）：
  // 打开下面这行会在渲染日志里打印出来，配图前先跑一次，照着数字用
  // `python scripts/capture.py <url> <name> --factor 2` 出图。
  // console.log(`[proof] 建议出图尺寸（2 倍）：${Math.round(cellW * 2)}×${Math.round(cellH * 2)}`);

  // 标题稍微早于第一张卡出现，让观众知道"接下来要看作品"
  const tLead = beat.snap(0.35); // 相对秒；纯函数 snap() 吃绝对秒，场景里别用

  return (
    <SceneFrame name="proof">
      <AbsoluteFill style={{ background: PALETTE.bg }} />
      {/* 快切蒙太奇期间底色随拍点闪，制造"一直在换画面"的错觉 */}
      <AbsoluteFill
        style={{
          background: `radial-gradient(80% 60% at 50% 40%, ${alpha(PALETTE.accent, 0.04 + beat.energy(beat.now) * 0.07)} 0%, ${alpha(PALETTE.bg, 0)} 70%)`,
        }}
      />

      <Stage align="top" style={{ gap: height * 0.02 }}>
        <div style={{ display: "flex", alignItems: "baseline", justifyContent: "space-between", width: "100%" }}>
          <div
            style={{
              ...riseIn(t, tLead, 0.4, 20),
              fontFamily: FONT.display,
              fontSize: type.title.size,
              fontWeight: type.title.weight,
              letterSpacing: type.title.letterSpacing,
              color: PALETTE.fg,
            }}
          >
            真实作品
          </div>
          <div
            style={{
              ...riseIn(t, tLead + TIMING.pace.stagger, 0.4, 20),
              fontFamily: FONT.body,
              fontSize: type.body.size,
              color: PALETTE.dim,
            }}
          >
            近 30 天交付 · 6 例
          </div>
        </div>

        {/* 卡片区：2×2 网格。行高**写死**成 gridH 而不是让 flex 撑开 ——
            卡片尺寸必须和上面算出来的 cellW/cellH 严格一致，否则真实截图的
            "图片宽度 ≥ 显示宽度×2" 这条校验就无从谈起（布局一变，需要的出图尺寸就变了）。 */}
        <div
          style={{
            width: gridW,
            height: gridH,
            display: "grid",
            gridTemplateColumns: "1fr 1fr",
            gridTemplateRows: "1fr 1fr",
            gap,
          }}
        >
          {ITEMS.map((item, i) => {
            // ❗beat.at() 给的是**场景相对**秒（内部已经减掉 proof 的起点 26.0），
            // 所以可以直接和 t 比。用绝对秒比大小 → t 永远小于 at → 整场黑屏。
            const at = beat.at(BEAT_INDEX[i]);
            // 每张卡只在自己该出现之后才挂载：真实截图替换后不会提前加载、也不会闪空帧
            if (t < at - 0.001) return null;
            const p = ramp(t, [at, at + 0.3], [0, 1], EASE.out);
            // 出场瞬间的脉冲：scale 从 1.03 落到 1，模拟"镜头到位"
            const hit = impulse(t, at, 0.26);
            const scale = 1 + hit * 0.028;
            return <WorkCard key={item.title} width={cellW} height={cellH} p={p} hit={hit} scale={scale} item={item} />;
          })}
        </div>

        {/* 底部横条：6 张卡片的进度指示 + 一句"这就是真实的东西"的总结 */}
        <div style={{ display: "flex", alignItems: "center", gap: type.tiny.size, width: "100%", justifyContent: "space-between" }}>
          <div style={{ display: "flex", gap: type.tiny.size * 0.5 }}>
            {ITEMS.map((item, i) => (
              <span
                key={item.title}
                style={{
                  width: type.tiny.size * 2.4,
                  height: Math.max(3, type.tiny.size * 0.22),
                  borderRadius: 999,
                  background: t >= beat.at(BEAT_INDEX[i]) ? PALETTE.accent : alpha(PALETTE.fg, 0.18),
                }}
              />
            ))}
          </div>
          <div
            style={{
              ...riseIn(t, tLead + stagger(2, TIMING.pace.stagger), 0.4, 18),
              fontFamily: FONT.body,
              fontSize: type.body.size,
              color: PALETTE.dim,
              // 慢呼吸：保证画面即使在两拍之间也不是完全静止
              opacity: clamp(
                ramp(t, [tLead, tLead + 0.5], [0, 1], EASE.out) * (0.8 + 0.2 * Math.abs(Math.sin((t / TIMING.music.bar) * Math.PI))),
                0,
                1,
              ),
            }}
          >
            每一项都能点开看
          </div>
        </div>
      </Stage>
    </SceneFrame>
  );
};
