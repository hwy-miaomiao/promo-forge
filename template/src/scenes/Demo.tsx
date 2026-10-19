/**
 * scenes/Demo.tsx —— 4.5~26 秒的主体演示：拍照记账 / 自动分类 / 月报。
 *
 * ⚠️ 这里的三块手机界面**全部是代码画的假界面**（零外部素材，模板必须能直接渲）。
 * 真实项目要做的替换：
 *   1) 用 `python scripts/capture.py <url> <name> --factor 2` 抓真实界面（2 倍图）；
 *   2) 把 <PhoneFrame>{假界面}</PhoneFrame> 换成
 *      `<PhoneFrame src="img/demo-capture.png" />`（PhoneFrame 内部用 <Img> + objectFit: cover，
 *      不会把截图压扁）；
 *   3) 长页面的功能展示改用 <Shot scroll={0~1} /> 做纵向滚动。
 * 三小节的入口时间**不要动**：它们落在小节线（bar = 2.0 秒）上。
 *
 * 一个小技巧：**只用一个 PhoneFrame，里面叠三块绝对定位的内容切换**。
 * 外壳位置固定不动，观众才会认出"这是同一个 App 的三个功能"，
 * 而不是三段互不相干的素材。
 *
 * 时间基准：t = useSceneTime("demo") 是**本场景相对秒**（4.5 秒会被减成 0），
 * 卡点一律用 beat.at(i) / beat.barAt(i) / beat.snap(...)，它们同样是场景相对秒。
 */
import React from "react"; // 这里要的是"值"而不是类型：下面用了 <React.Fragment>
import { AbsoluteFill, useVideoConfig } from "remotion";
import { SceneFrame, useSceneTime } from "../components/Scene";
import { Stage } from "../components/Stage";
import { PhoneFrame } from "../components/PhoneFrame";
import { PALETTE, SAFE, alpha, captionBandHeight, captionBottomPx, isVertical, safePadding, useType } from "../lib/theme";
import { FONT } from "../lib/fonts";
import { EASE, clamp, impulse, loop, ramp, riseIn, stagger } from "../lib/anim";
import { TIMING } from "../lib/timing";
import { useSceneBeat } from "../lib/beats";

/**
 * ❗小节切换点不要自己拿 `i × TIMING.music.bar` 算：那个式子算的是「场景里的第 i 小节」，
 * 而不是音乐里的第 i 条小节线 —— 两者差一个场景起点相位（demo 从 4.5 秒才开始，音乐却从 0 秒就在走），
 * 算出来会比真小节线晚半个小节（8.0 / 16.0 ≠ 真小节线 7.5 / 15.5）。
 * 切换点一律用 `beat.barAt(i)`（返回**场景相对秒**，内部按「音乐时间 / bar 取整 + i × bar」换算），
 * 换歌、换 offset 都不用改这里。
 */

/** 顶部三步进度条：让观众知道"还有几步"。 */
const Steps: React.FC<{ type: ReturnType<typeof useType>; active: number; small: boolean }> = ({ type, active, small }) => (
  <div style={{ display: "flex", alignItems: "center", gap: type.tiny.size * 0.6, opacity: small ? 0.9 : 1 }}>
    {[0, 1, 2].map((i) => (
      <React.Fragment key={i}>
        <span
          style={{
            width: type.tiny.size * 1.5,
            height: Math.max(4, type.tiny.size * 0.26),
            borderRadius: 999,
            background: i <= active ? PALETTE.accent : alpha(PALETTE.fg, 0.22),
            transition: "none",
          }}
        />
        {i < 2 ? <span style={{ width: type.tiny.size * 0.5, height: 1, background: alpha(PALETTE.fg, 0.2) }} /> : null}
      </React.Fragment>
    ))}
  </div>
);

/** 小标签（分类 chip / OCR 字段名）。 */
const Chip: React.FC<{ label: string; color: string; type: ReturnType<typeof useType>; filled?: boolean }> = ({ label, color, type, filled = false }) => (
  <span
    style={{
      padding: `${type.tiny.size * 0.22}px ${type.tiny.size * 0.55}px`,
      borderRadius: 999,
      border: `1px solid ${alpha(color, filled ? 1 : 0.45)}`,
      background: filled ? color : alpha(color, 0.12),
      color: filled ? PALETTE.bg : color,
      fontFamily: FONT.body,
      fontSize: type.tiny.size * 0.86,
      fontWeight: 700,
      whiteSpace: "nowrap",
    }}
  >
    {label}
  </span>
);

/** 一笔账目行：图标方块 + 商户 + 分类 chip + 金额。 */
const LedgerRow: React.FC<{
  type: ReturnType<typeof useType>;
  p: number;
  cat: string;
  name: string;
  amount: string;
  color: string;
}> = ({ type, p, cat, name, amount, color }) => (
  <div
    style={{
      display: "flex",
      alignItems: "center",
      gap: type.tiny.size * 0.5,
      opacity: p,
      transform: `translateX(${(1 - p) * 18}px)`,
      padding: `${type.tiny.size * 0.34}px 0`,
      borderBottom: `1px solid ${alpha(PALETTE.fg, 0.07)}`,
    }}
  >
    <span style={{ width: type.tiny.size * 1.5, height: type.tiny.size * 1.5, borderRadius: type.tiny.size * 0.4, background: alpha(color, 0.22), border: `1px solid ${alpha(color, 0.5)}`, flexShrink: 0 }} />
    <span style={{ fontFamily: FONT.body, fontSize: type.tiny.size, color: PALETTE.fg, whiteSpace: "nowrap" }}>{name}</span>
    <span style={{ marginLeft: "auto" }}>
      <Chip label={cat} color={color} type={type} />
    </span>
    <span style={{ fontFamily: FONT.mono, fontSize: type.tiny.size, color: PALETTE.dim, whiteSpace: "nowrap", width: type.tiny.size * 3.4, textAlign: "right" }}>{amount}</span>
  </div>
);

/** 小节 1：拍照记账。取景框 + 假小票 + 扫描线 + 快门白闪 + OCR 结果逐行出现。 */
const CaptureMock: React.FC<{ type: ReturnType<typeof useType>; t: number; w: number; h: number }> = ({ type, t, w, h }) => {
  const shotAt = 2.0; // 快门（约 1 拍后）
  // 快门白闪只亮 0.18 秒：ramp 是 t 的纯函数（窗口外自动取端点），同一帧永远算出同一个值，
  // 不会因为渲染顺序或机器不同而闪法不同；这里不用取整帧，因为 t 已经是帧时间。
  const flash = ramp(t, [shotAt, shotAt + 0.18], [0.9, 0], EASE.in);
  const gone = t >= shotAt + 0.18;
  const scan = loop(t, 1.2);
  const lines = [
    { k: "商户", v: "7-11 便利店" },
    { k: "金额", v: "¥38.50" },
    { k: "分类", v: "餐饮" },
    { k: "状态", v: "已存为一笔账" },
  ];
  return (
    <div style={{ position: "absolute", inset: 0, padding: h * 0.055, display: "flex", flexDirection: "column", gap: h * 0.02 }}>
      {/* 取景框 */}
      <div style={{ position: "relative", flex: 1, borderRadius: h * 0.03, overflow: "hidden", background: "#0E0E16", border: `1px solid ${alpha(PALETTE.accent, 0.4)}` }}>
        {/* 假小票 */}
        <div style={{ position: "absolute", left: "16%", top: "14%", width: "68%", background: "#F4F2EC", borderRadius: h * 0.008, padding: h * 0.02, display: "flex", flexDirection: "column", gap: h * 0.008 }}>
          <div style={{ height: h * 0.012, width: "52%", background: "#1B1B22", borderRadius: 2 }} />
          {[0, 1, 2, 3].map((i) => (
            <div key={i} style={{ display: "flex", justifyContent: "space-between", gap: h * 0.01 }}>
              <div style={{ height: h * 0.007, width: `${34 + i * 7}%`, background: "#8A8A93", borderRadius: 2 }} />
              <div style={{ height: h * 0.007, width: `${14 + (i % 2) * 8}%`, background: "#8A8A93", borderRadius: 2 }} />
            </div>
          ))}
          <div style={{ height: h * 0.012, width: "40%", background: "#1B1B22", borderRadius: 2, marginTop: h * 0.004 }} />
        </div>
        {/* 四角取景标记（矢量，不依赖素材） */}
        {[
          { top: "6%", left: "6%", bt: 1, bl: 1 },
          { top: "6%", right: "6%", bt: 1, br: 1 },
          { bottom: "6%", left: "6%", bb: 1, bl: 1 },
          { bottom: "6%", right: "6%", bb: 1, br: 1 },
        ].map((c, i) => (
          <div
            key={i}
            style={{
              position: "absolute",
              width: h * 0.05,
              height: h * 0.05,
              borderTop: c.bt ? `3px solid ${PALETTE.accent}` : undefined,
              borderBottom: c.bb ? `3px solid ${PALETTE.accent}` : undefined,
              borderLeft: c.bl ? `3px solid ${PALETTE.accent}` : undefined,
              borderRight: c.br ? `3px solid ${PALETTE.accent}` : undefined,
              top: c.top,
              bottom: c.bottom,
              left: c.left,
              right: c.right,
            }}
          />
        ))}
        {/* 扫描线：消失后才扫，表示"拍完了正在读" */}
        {gone && t < shotAt + 0.9 ? (
          <div style={{ position: "absolute", left: 0, right: 0, top: `${scan * 100}%`, height: 3, background: PALETTE.accent, boxShadow: `0 0 18px ${PALETTE.accent}` }} />
        ) : null}
        {/* 快门白闪 */}
        {flash > 0.01 ? <div style={{ position: "absolute", inset: 0, background: "#fff", opacity: flash }} /> : null}
      </div>

      {/* OCR 结果：一行一行落下（间隔 = pace.stagger） */}
      <div style={{ display: "flex", flexDirection: "column", gap: h * 0.006 }}>
        {lines.map((l, i) => {
          const at = shotAt + 0.35 + stagger(i, TIMING.pace.stagger);
          const p = ramp(t, [at, at + 0.3], [0, 1], EASE.out);
          return (
            <div key={l.k} style={{ display: "flex", alignItems: "baseline", gap: type.tiny.size * 0.4, opacity: p, transform: `translateY(${(1 - p) * 8}px)` }}>
              <span style={{ fontFamily: FONT.body, fontSize: type.tiny.size * 0.9, color: PALETTE.dim, width: w * 0.14 }}>{l.k}</span>
              <span style={{ fontFamily: i === 3 ? FONT.display : FONT.body, fontSize: type.tiny.size, fontWeight: i === 3 ? 800 : 400, color: i === 3 ? PALETTE.accent : PALETTE.fg }}>{l.v}</span>
            </div>
          );
        })}
      </div>
      {/* 快门提示 */}
      <div style={{ position: "absolute", left: 0, right: 0, bottom: h * 0.3, display: "flex", justifyContent: "center", opacity: ramp(t, [0.15, 0.45], [0, 1], EASE.out) * (t < shotAt ? 0.9 : 0) }}>
        <span style={{ fontFamily: FONT.body, fontSize: type.tiny.size * 0.9, color: PALETTE.dim }}>对准小票，按一下</span>
      </div>
    </div>
  );
};

/** 小节 2：自动分类。5 行账目依次落位 + 分类汇总条。 */
const ClassifyMock: React.FC<{ type: ReturnType<typeof useType>; t: number; w: number; h: number }> = ({ type, t, w, h }) => {
  const rows = [
    { cat: "餐饮", name: "7-11 便利店", amount: "-38.50", color: PALETTE.accent },
    { cat: "交通", name: "地铁 4 号线", amount: "-6.00", color: "#7AA2FF" },
    { cat: "餐饮", name: "楼下咖啡", amount: "-28.00", color: PALETTE.accent },
    { cat: "生活", name: "洗衣液", amount: "-49.90", color: "#FFB86B" },
    { cat: "收入", name: "工资入账", amount: "+8,600", color: "#FF7AA2" },
  ];
  const bars = [
    { label: "餐饮", pct: 0.42, color: PALETTE.accent },
    { label: "交通", pct: 0.16, color: "#7AA2FF" },
    { label: "生活", pct: 0.26, color: "#FFB86B" },
    { label: "其他", pct: 0.16, color: alpha(PALETTE.fg, 0.45) },
  ];
  return (
    <div style={{ position: "absolute", inset: 0, padding: h * 0.055, display: "flex", flexDirection: "column", gap: h * 0.014 }}>
      <div style={{ display: "flex", alignItems: "baseline", justifyContent: "space-between" }}>
        <span style={{ fontFamily: FONT.display, fontSize: type.tiny.size * 1.1, fontWeight: 800, color: PALETTE.fg }}>本月账目</span>
        <span style={{ fontFamily: FONT.mono, fontSize: type.tiny.size * 0.9, color: PALETTE.dim }}>5 笔</span>
      </div>
      {rows.map((r, i) => (
        <LedgerRow key={r.name} type={type} p={ramp(t, [0.25 + stagger(i, TIMING.pace.stagger), 0.55 + stagger(i, TIMING.pace.stagger)], [0, 1], EASE.out)} cat={r.cat} name={r.name} amount={r.amount} color={r.color} />
      ))}
      {/* 分类汇总条：每类一条，宽度随占比推进 */}
      <div style={{ marginTop: "auto", display: "flex", flexDirection: "column", gap: h * 0.008 }}>
        <span style={{ fontFamily: FONT.body, fontSize: type.tiny.size * 0.86, color: PALETTE.dim }}>自动归类</span>
        {bars.map((b, i) => {
          const at = 2.2 + i * 0.12 * 2;
          const p = ramp(t, [at, at + 0.5], [0, 1], EASE.out);
          return (
            <div key={b.label} style={{ display: "flex", alignItems: "center", gap: type.tiny.size * 0.4 }}>
              <span style={{ fontFamily: FONT.body, fontSize: type.tiny.size * 0.86, color: PALETTE.dim, width: w * 0.1 }}>{b.label}</span>
              <span style={{ flex: 1, height: Math.max(6, h * 0.012), borderRadius: 999, background: alpha(PALETTE.fg, 0.1) }}>
                <span style={{ display: "block", width: `${b.pct * 100 * p}%`, height: "100%", borderRadius: 999, background: b.color }} />
              </span>
              <span style={{ fontFamily: FONT.mono, fontSize: type.tiny.size * 0.86, color: PALETTE.fg, width: w * 0.12, textAlign: "right" }}>{Math.round(b.pct * 100 * p)}%</span>
            </div>
          );
        })}
      </div>
    </div>
  );
};

/** 小节 3：月报。柱状图 + 滚动总额 + 同比。 */
const ReportMock: React.FC<{ type: ReturnType<typeof useType>; t: number; w: number; h: number }> = ({ type, t, w, h }) => {
  const bars = [0.42, 0.58, 0.35, 0.72, 0.5, 0.66, 0.44, 0.8, 0.62, 0.9, 0.7, 0.86];
  const total = ramp(t, [0.35, 1.5], [0, 4612]);
  const up = ramp(t, [1.1, 1.7], [0, 1], EASE.out);
  return (
    <div style={{ position: "absolute", inset: 0, padding: h * 0.055, display: "flex", flexDirection: "column", gap: h * 0.018 }}>
      <span style={{ fontFamily: FONT.body, fontSize: type.tiny.size, color: PALETTE.dim }}>本月支出</span>
      <div style={{ display: "flex", alignItems: "baseline", gap: type.tiny.size * 0.3 }}>
        <span style={{ fontFamily: FONT.mono, fontSize: type.tiny.size * 1.9, fontWeight: 700, color: PALETTE.fg, fontVariantNumeric: "tabular-nums" }}>¥{Math.round(total).toLocaleString("zh-CN")}</span>
        <span style={{ fontFamily: FONT.display, fontSize: type.tiny.size, fontWeight: 800, color: PALETTE.accent, opacity: up }}>↓ 12%</span>
      </div>
      {/* 12 根柱子：逐根长起来，落在拍点上（0.25 秒一根） */}
      <div style={{ flex: 1, display: "flex", alignItems: "flex-end", gap: h * 0.008, paddingTop: h * 0.02 }}>
        {bars.map((v, i) => {
          const p = ramp(t, [0.4 + i * 0.085, 0.9 + i * 0.085], [0, 1], EASE.out);
          const inMonth = i >= 6;
          return (
            <span
              key={i}
              style={{
                flex: 1,
                height: `${v * 100 * p}%`,
                borderRadius: h * 0.006,
                background: inMonth ? PALETTE.accent : alpha(PALETTE.fg, 0.22),
                boxShadow: inMonth ? `0 0 12px ${alpha(PALETTE.accent, 0.35)}` : undefined,
              }}
            />
          );
        })}
      </div>
      <div style={{ display: "flex", justifyContent: "space-between" }}>
        <span style={{ fontFamily: FONT.body, fontSize: type.tiny.size * 0.82, color: PALETTE.dim }}>1 月</span>
        <span style={{ fontFamily: FONT.body, fontSize: type.tiny.size * 0.82, color: PALETTE.accent }}>本月 · 持平</span>
        <span style={{ fontFamily: FONT.body, fontSize: type.tiny.size * 0.82, color: PALETTE.dim }}>12 月</span>
      </div>
      <div style={{ display: "flex", alignItems: "center", gap: type.tiny.size * 0.4, opacity: ramp(t, [1.8, 2.3], [0, 1], EASE.out) }}>
        <Chip label="已生成" color={PALETTE.accent} type={type} filled />
        <span style={{ fontFamily: FONT.body, fontSize: type.tiny.size * 0.86, color: PALETTE.dim }}>月底自动发到手机</span>
      </div>
    </div>
  );
};

export const Demo: React.FC = () => {
  const t = useSceneTime("demo");
  const type = useType();
  const { width, height } = useVideoConfig();
  const beat = useSceneBeat("demo");

  // ---- 三小节：入口全部落在**音乐小节线（bar）**上，每节 2 小节 ≈ 4 秒 ----------
  // barAt(i) 返回的是**场景相对秒**（不是绝对秒）：场景 4.5 秒开始、bar = 2.0，
  // 第 4 条小节线在音乐时间 12.0 秒（全片绝对 12.0，因为 offset = 0），减掉场景起点 4.5 → 7.5 秒；
  // 第 8 条同理 = 20.0 绝对 / 15.5 相对。旧写法 i × bar 算成场景内第 4 小节 = 8.0 秒（绝对 12.5），
  // 比真小节线晚半拍，画面切在拍子缝里。
  const s1 = 0;
  const s2 = beat.barAt(4);
  const s3 = beat.barAt(8);
  const sections = [s1, s2, s3];

  // ❗下面 vis() 的第二个参数是**场景相对秒**，所以这里必须取「本场景的绝对终点 − 场景起点」：
  // t = useSceneTime("demo") 从 0 数到 21.5，直接给 vis(s3, TIMING.duration)（=45，全片时长）
  // 会让第三个假界面永远到不了 end - 0.3，永远不淡出（实测：t=21.5 时 opacity 仍是 1）。
  const demoSce = TIMING.scenes.find((s) => s.name === "demo");
  const demoSec = demoSce ? demoSce.end - demoSce.start : TIMING.duration;

  const active = t >= s3 ? 2 : t >= s2 ? 1 : 0;

  /** 小节可见度：淡入 0.4 秒、退场 0.3 秒，避免两节同时半透明。 */
  const vis = (at: number, end: number): number =>
    Math.min(ramp(t, [at, at + 0.4], [0, 1], EASE.out), ramp(t, [end - 0.3, end], [1, 0], EASE.in));

  // 小节切换的"顿挫"：切换瞬间给整个手机壳一个短促的缩放
  const sectionHit = Math.max(...sections.map((at) => impulse(t, at, 0.4)));

  // 手机壳的**真实**像素尺寸：宽 = Stage 内容宽 × widthRatio，高 = 宽 ÷ 机身宽高比（PhoneFrame 默认 9/19.5）。
  // ❗里面三块假界面拿 h 当 padding/gap 的基准，绝不能再传 height * 0.62 * 2.16（=2571px，比真机高两倍多）：
  // 0.055 × 2571 ≈ 141px 的双边 padding 会把账目行整行挤出屏幕，抽帧看到的就只剩一块空面板。
  const safe = safePadding(width, height);
  const stageW = Math.min(
    width - safe.left - safe.right,
    Math.round(width * (isVertical(width, height) ? SAFE.contentWidth.vertical : SAFE.contentWidth.horizontal)),
  );
  // 0.46 只是**宽度基准**：标题 + 手机 + 小结要整块待在**字幕带之上**（见 Caption.tsx 的 captions.bottom），
  // 机身高了就一定会被字幕压住。只按宽度算会在横版 16:9 上翻车：1920 宽时 0.46 × 1305.6 ≈ 600.8px，
  // 机身高 600.8 ÷ (9/19.5) ≈ 1301.7px，而 Stage 的内容高只有 1080 − 上 6% − 下 8% ≈ 929px ——
  // 机身被上下各裁掉 300 多 px（pr-b5 横版 13.1s 帧）。所以必须再按**可用高**夹一次：
  // 手机高度不能超过「内容盒高 − 标题 − 小结 − 三步条 − 字幕带」。
  // 字幕带高度按 Caption.tsx 的口径算（底边 captions.bottom + maxLines 行 + 行距），不再写死 0.3 屏高。
  // 字号与硬字幕 ASS 同源（lib/theme 的 captionBandHeight / captionFontSize，R5-TPL-03）。
  const captionBottom = TIMING.captions.enabled ? captionBottomPx(height, TIMING.captions.bottom) : 0;
  const capBand = TIMING.captions.enabled
    ? captionBottom + captionBandHeight(width, height, TIMING.captions.maxChars, TIMING.captions.maxLines)
    : 0;
  // 字幕带顶边已经在安全区内时不再额外让位；+1.2% 屏高是字幕与内容之间的呼吸位。
  const bottomExtra = Math.max(0, Math.round(capBand - safe.bottom + height * 0.012));
  const stageGap = height * 0.024; // 与下面 <Stage style={{ gap }}> 一致
  const titleH = type.title.size * 1.3; // 与标题的 minHeight 一致
  const bodyH = type.body.size * 1.6; // 与小结的 minHeight 一致
  const stepsH = type.tiny.size * 1.4;
  const availH = height - safe.top - safe.bottom - bottomExtra - titleH - bodyH - stepsH - stageGap * 3;
  const phoneW = Math.min(stageW * 0.46, (availH * 9) / 19.5);
  // PhoneFrame 把 widthRatio 取整到 1%，这里按取整后的真实宽度换算 mock 的缩放比（否则内屏会差几个像素）
  const phoneWRatio = phoneW / stageW;
  const phoneWActual = stageW * (Math.round(phoneWRatio * 100) / 100);

  // 假界面按"设计尺寸"排版，再整体缩放到手机壳的真实内屏（scale 见下面的 mockScale）：
  // 三块 mock 里的字号是定值（type.tiny.size = 22px），padding/gap 才是按 h 缩放的 ——
  // 手机一改小，字号不动、内宽变窄，账目行就会横向溢出被 overflow:hidden 切掉。
  // 所以让它们永远按 536×1161（0.62 机身宽时的内屏，配色和行距都是在这个尺度下定的）排版。
  const MOCK_DESIGN_W = 536;
  const MOCK_DESIGN_H = 1161;
  // PhoneFrame 机身有 2.2% 的内边距 → 内屏宽 = 机身宽 × (1 − 2 × 2.2%)
  const mockScale = (phoneWActual * 0.956) / MOCK_DESIGN_W;

  return (
    <SceneFrame name="demo">
      <AbsoluteFill style={{ background: PALETTE.bg }} />
      {/* 背景随能量呼吸；三小节各自有自己的色调，暗示"换了功能" */}
      <AbsoluteFill
        style={{
          background: `radial-gradient(70% 50% at 50% 45%, ${alpha(PALETTE.accent, 0.05 + beat.energy(beat.now) * 0.08)} 0%, ${alpha(PALETTE.bg, 0)} 72%)`,
          opacity: 0.4 + beat.energy(beat.now) * 0.6,
        }}
      />

      {/* bottomExtra 给底部字幕带让位（数值按上面 capBand 算）。❗不要再写成 style={{ paddingBottom }}：
          Stage 的 style 是在安全区 padding 之后展开的，那样等于把安全区**替换**掉而不是叠加，
          横版 16:9 下内容区高度会算错（pr-b5 横版帧里机身被裁 300px 就是这么来的）。 */}
      <Stage align="center" bottomExtra={bottomExtra} style={{ gap: stageGap }}>
        {/* 标题：随小节换，让"新信息"每 2 秒内必到（AGENTS.md 密度要求） */}
        <div style={{ display: "flex", alignItems: "center", gap: type.tiny.size * 0.8 }}>
          <Steps type={type} active={active} small={false} />
          <span style={{ fontFamily: FONT.mono, fontSize: type.tiny.size, color: PALETTE.dim }}>
            0{active + 1} / 03
          </span>
        </div>

        <div
          style={{
            fontFamily: FONT.display,
            fontSize: type.title.size,
            fontWeight: type.title.weight,
            letterSpacing: type.title.letterSpacing,
            color: PALETTE.fg,
            textAlign: "center",
            minHeight: type.title.size * 1.3,
          }}
        >
          {["拍一张小票", "每一笔自动分类", "月底自动出月报"][active]}
        </div>

        {/* 手机壳位置固定，里面三块内容切换 —— 观众看到的是"同一个 App 的三个功能" */}
        <div style={{ position: "relative", width: "100%", display: "flex", justifyContent: "center", transform: `scale(${1 + sectionHit * 0.02})` }}>
          <PhoneFrame widthRatio={phoneWRatio}>
            <div style={{ position: "absolute", inset: 0, opacity: vis(s1, s2) }}>
              <div style={{ width: MOCK_DESIGN_W, height: MOCK_DESIGN_H, transform: `scale(${mockScale})`, transformOrigin: "top left" }}>
                <CaptureMock type={type} t={t - s1} w={MOCK_DESIGN_W} h={MOCK_DESIGN_H} />
              </div>
            </div>
            <div style={{ position: "absolute", inset: 0, opacity: vis(s2, s3) }}>
              <div style={{ width: MOCK_DESIGN_W, height: MOCK_DESIGN_H, transform: `scale(${mockScale})`, transformOrigin: "top left" }}>
                <ClassifyMock type={type} t={t - s2} w={MOCK_DESIGN_W} h={MOCK_DESIGN_H} />
              </div>
            </div>
            <div style={{ position: "absolute", inset: 0, opacity: vis(s3, demoSec) }}>
              <div style={{ width: MOCK_DESIGN_W, height: MOCK_DESIGN_H, transform: `scale(${mockScale})`, transformOrigin: "top left" }}>
                <ReportMock type={type} t={t - s3} w={MOCK_DESIGN_W} h={MOCK_DESIGN_H} />
              </div>
            </div>
          </PhoneFrame>
        </div>

        {/* 一句人话总结：每个小节换一次，配合旁白（timing.voice.lines 里 demo 有 2 句） */}
        <div
          style={{
            fontFamily: FONT.body,
            fontSize: type.body.size,
            color: PALETTE.dim,
            letterSpacing: "0.02em",
            textAlign: "center",
            minHeight: type.body.size * 1.6,
          }}
        >
          {["剩下的交给它", "不用自己选分类", "打开手机就能看"][active]}
        </div>
      </Stage>
    </SceneFrame>
  );
};
