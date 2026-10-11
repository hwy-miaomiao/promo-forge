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
 *   3) 竖屏视频居中显示并限制最大高度，横屏手机上自动变成"视频在左、文案在右"。
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
  useEffect(() => {
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
  }, []);

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
    <div className="page" style={{ "--ratio": PLAYER.ratio } as CSSProperties}>
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
              ref={playerRef}
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

/** 品牌标记：有 logo 用 logo，没有就用首字方块 —— 空 logo 不能变成裂图。 */
const BrandMark = () => {
  if (WEB.logo.trim() !== "") {
    return <img className="head-logo" src={WEB.logo} alt={WEB.brand} />;
  }
  return <span className="head-mark">{Array.from(WEB.brand)[0] ?? "P"}</span>;
};
