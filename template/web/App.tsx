/**
 * web/App.tsx —— 网页播放页（移动端优先）。
 *
 * 为什么要"预取完资源再挂播放器"：
 *   <Player> 是边播边拉的。配乐没下完就点播放，会出现"画面在走、声音是空的"，
 *   而观众只会得出一个结论：这片子做得不行。所以先用 prefetch(..., { method: "blob-url" })
 *   把 ASSETS（配乐 + logo）整包下完，显示"加载中 n%"，完成后再挂载。
 *
 * 为什么关掉 Player 自带的控件：
 *   自带的控制条是给"检查器"用的（拖时间轴、看帧号），不是给观众用的。
 *   这里自己画：海报态 / 播放中细进度条 / 结束卡片 —— 三种状态各只有一件事要用户做。
 *
 * 响应式没上任何 UI 框架，靠三件事：
 *   1) `aspect-ratio: var(--ratio)` + `container-type: inline-size` + `cqw` 单位：
 *      所有尺寸都相对**容器宽**，手机上字小、桌面上字大，一套数值通吃；
 *   2) 页面本身是纵向 flex，高度不足时滚动，不做固定视口高度（iOS 地址栏会吃掉 vh）；
 *   3) 竖屏视频居中显示并限制最大高度；横屏手机（`orientation: landscape` 且高 ≤560px）
 *      由 `style.css` 末尾那条 media query 改成"视频在左、文案在右"两栏 ——
 *      单列布局在 844×390 这类横屏里会把文案和页脚整个挤出首屏。
 */
import { useCallback, useEffect, useRef, useState, type CSSProperties } from "react";
import { Player, type PlayerRef } from "@remotion/player";
import { prefetch } from "remotion";
import { Promo } from "../src/Promo";
import { ASSETS, assetUrl } from "../src/assets";
import { PLAYER, WEB } from "./config";

/** 播放器状态机。用联合类型而不是几个 boolean，避免出现"既在加载又在播"的鬼状态。 */
type Phase = "loading" | "poster" | "playing" | "ended";

export const App = () => {
  const playerRef = useRef<PlayerRef>(null);
  // 播放器**挂上了没有**。<Player> 只在预取完成后才渲染（见下面 `phase !== "loading"`），
  // 而"注册玩家事件"那个 effect 如果只依赖 []，第一次跑时 playerRef.current 还是 null、
  // 直接 return，之后**永远不会再跑** —— play / ended / frameupdate 一个都注册不上，
  // 点封面后 phase 卡在 "poster"：海报层继续盖着、进度条和结束卡片永不出现（播放整体失效）。
  // 所以用一个回调 ref 把"挂载完成"翻成 state，再把它当 effect 的依赖：挂载时注册、
  // 卸载时清理，播放器被换掉（null → 新实例）也会重新注册。
  const [playerReady, setPlayerReady] = useState(false);
  const [phase, setPhase] = useState<Phase>("loading");
  const [progress, setProgress] = useState(0); // 0~1
  const [failed, setFailed] = useState<string | null>(null);
  const [frame, setFrame] = useState(0);

  // ---- 1. 预取资源 ---------------------------------------------------------
  useEffect(() => {
    let alive = true;
    const prefetchAll = async (): Promise<void> => {
      const total = Math.max(1, ASSETS.length);
      let done = 0;
      for (const asset of ASSETS) {
        try {
          await prefetch(assetUrl(asset), { method: "blob-url" });
        } catch (err) {
          // 单个资源失败（例如项目暂时没有配乐）不应该卡死整个页面：
          // 记下来提示，但继续走完剩下的预取。
          console.warn("[web] 预取失败：", asset, err);
          if (alive) setFailed(asset);
        }
        done += 1;
        if (alive) setProgress(done / total);
      }
      if (alive) setPhase("poster");
    };
    void prefetchAll();
    return () => {
      alive = false;
    };
  }, []);

  // ---- 2. 玩家事件 → 状态机 ------------------------------------------------
  // 回调 ref：Player 挂载/卸载时同时更新 playerRef 和 playerReady。
  // 不能只写 `<Player ref={playerRef}>`：那样没人知道"挂上了没有"，
  // 下面那个 effect 就只能跑一次，而它跑的时候播放器还没挂载。
  const handlePlayerRef = useCallback((node: PlayerRef | null) => {
    playerRef.current = node;
    setPlayerReady(node !== null);
  }, []);

  useEffect(() => {
    // playerReady=false：播放器还没挂载（资源预取中），没有可以注册监听的对象
    if (!playerReady) return;
    const player = playerRef.current;
    if (!player) return;
    const onPlay = () => setPhase("playing");
    const onEnded = () => setPhase("ended");
    // frameupdate 每帧触发：只用来画那条细进度条，不做任何重计算
    const onFrame = (e: { detail: { frame: number } }) => setFrame(e.detail.frame);
    player.addEventListener("play", onPlay);
    player.addEventListener("ended", onEnded);
    player.addEventListener("frameupdate", onFrame);
    return () => {
      player.removeEventListener("play", onPlay);
      player.removeEventListener("ended", onEnded);
      player.removeEventListener("frameupdate", onFrame);
    };
    // 依赖 playerReady（而不是 []）：播放器挂载完成后这个 effect 才会真正跑一次（见上）
  }, [playerReady]);

  const play = useCallback(() => {
    setFrame(0);
    playerRef.current?.play();
  }, []);

  const replay = useCallback(() => {
    setFrame(0);
    playerRef.current?.seekTo(0);
    playerRef.current?.play();
  }, []);

  const pct = Math.round(progress * 100);
  const played = PLAYER.durationInFrames > 0 ? Math.min(1, frame / PLAYER.durationInFrames) : 0;

  return (
    <div
      className="page"
      style={
        {
          "--ratio": PLAYER.ratio,
          // 品牌色跟着 timing.json 走（style.css 里那几个 :root 变量只是模板默认值的兜底）
          "--bg": WEB.colors.bg,
          "--fg": WEB.colors.fg,
          "--accent": WEB.colors.accent,
        } as CSSProperties
      }
    >
      <header className="head">
        <BrandMark />
        <span className="head-name">{WEB.brand}</span>
        <span className="head-tag">网页版</span>
      </header>

      <main className="stage">
        {/* 外层容器负责"给播放器留出正确的位置"，Player 本体铺满它。
            这样即使播放器还没挂载，页面高度也是稳定的，不会因为加载完成而跳动。 */}
        <div className="screen">
          {phase !== "loading" ? (
            <Player
              ref={handlePlayerRef}
              component={Promo}
              inputProps={{ withAudio: true }}
              durationInFrames={PLAYER.durationInFrames}
              fps={PLAYER.fps}
              compositionWidth={PLAYER.width}
              compositionHeight={PLAYER.height}
              style={{ width: "100%", height: "100%" }}
              // 自带控件全部关掉：自己画的海报/进度条/结束卡片才是给观众看的
              controls={false}
              clickToPlay={false}
              doubleClickToFullscreen={false}
              spaceKeyToPlayOrPause={false}
              acknowledgeRemotionLicense
            />
          ) : (
            <div className="placeholder">
              <div className="spinner" aria-hidden />
              <div className="placeholder-text">
                加载中 {pct}%
                {failed ? <span className="warn">（{failed} 没找到，已跳过）</span> : null}
              </div>
            </div>
          )}

          {/* 海报态：盖在播放器上的一层，点一下才开始播 */}
          {phase === "poster" ? (
            <button type="button" className="cover" onClick={play}>
              <span className="cover-note">{WEB.posterNote}</span>
              <span className="play-btn" aria-label="播放">
                <span className="play-tri" />
              </span>
              <span className="cover-hint">点一下播放 · 约 {Math.round(PLAYER.durationInFrames / PLAYER.fps)} 秒</span>
            </button>
          ) : null}

          {/* 播放中：顶部一条细进度条。不用 Player 自带的，因为它会占掉画面高度 */}
          {phase === "playing" ? (
            <div className="progress" aria-hidden>
              <div className="progress-fill" style={{ transform: `scaleX(${played})` }} />
            </div>
          ) : null}

          {/* 结束卡片：一句话 + 主/次 CTA，点重播回到海报态 */}
          {phase === "ended" ? (
            <div className="ended">
              <p className="ended-note">{WEB.endNote}</p>
              <div className="cta-row">
                {WEB.primary.url ? (
                  <a className="btn btn-primary" href={WEB.primary.url} target="_blank" rel="noreferrer">
                    {WEB.primary.label}
                  </a>
                ) : null}
                {WEB.secondary.url ? (
                  <a className="btn btn-secondary" href={WEB.secondary.url} target="_blank" rel="noreferrer">
                    {WEB.secondary.label}
                  </a>
                ) : null}
                <button type="button" className="btn btn-ghost" onClick={replay}>
                  重播
                </button>
              </div>
            </div>
          ) : null}
        </div>

        <p className="caption-note">
          {/* 竖屏片子在桌面浏览器里会被压得很高，提示一句，减少"怎么这么小"的疑惑 */}
          竖屏 9:16 · 建议用手机全屏观看
        </p>
      </main>

      <footer className="foot">{WEB.footer}</footer>
    </div>
  );
};

/**
 * 品牌标记：有 logo 用 logo，没有就用首字方块 —— 空 logo 不能变成裂图。
 * src 必须走 assetUrl()：timing.json 里写的是**相对 public/ 的路径**（如 "img/logo.png"），
 * 直接用会被当成"相对当前页"解析 —— 部署到子路径（/promo/）时会取成 /promo/img/logo.png，
 * 和上面预取用的 /img/logo.png 变成两个口径，logo 就 404 了。
 */
const BrandMark = () => {
  if (WEB.logo.trim() !== "") {
    return <img className="head-logo" src={assetUrl(WEB.logo)} alt={WEB.brand} />;
  }
  return <span className="head-mark">{Array.from(WEB.brand)[0] ?? "P"}</span>;
};
