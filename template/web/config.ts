/**
 * web/config.ts —— 网页播放版的配置入口（唯一需要手改的网页文件）。
 *
 * 为什么单独开一个文件：
 *   网页版的文案和视频里的文案**不一样**。视频讲 45 秒，网页要长期挂着被人点开，
 *   所以"发布前要改哪些"必须集中在一处，而不是散在 App.tsx 的 JSX 里找。
 *
 * 默认值全部从 `src/timing.json` 推导 —— 改了片子的品牌名/网址，网页跟着变，
 * 不用改两遍（改两遍就一定会有一遍忘记改）。
 */
import timing from "../src/timing.json";

export type WebLink = {
  /** 按钮文案 */
  label: string;
  /** 跳转地址。留空字符串 = 不渲染这个按钮 */
  url: string;
};

export type WebConfig = {
  /** 浏览器标题，main.tsx 会 document.title = WEB.title */
  title: string;
  /** 页面顶部的品牌名 */
  brand: string;
  /** 品牌 logo（相对 public/ 的路径）。空串 = 用代码画的首字方块兜底 */
  logo: string;
  /**
   * 品牌三色。**唯一事实来源是 src/timing.json 的 brand.colors** —— 片子里用的就是这三个值，
   * 网页版必须跟着走：换了品牌色只改 timing.json，不能要求用户再去手改 style.css。
   * 这里的字面量只是"timing.json 没写这个键"时的兜底（与模板默认配色一致）。
   */
  colors: Required<typeof timing.brand.colors>;
  /** 海报态的一句话（点播放之前给人看的"这片子讲什么"） */
  posterNote: string;
  /** 播放结束卡片上的一句话 */
  endNote: string;
  /** 主 CTA（通常是下载/试用） */
  primary: WebLink;
  /** 次 CTA（通常是官网/文档）。url 为空则不显示 */
  secondary: WebLink;
  /** 页面底部小字（版权、备案、许可等） */
  footer: string;
};

/**
 * ❗发布前必须逐条确认的四个地方：
 *   1) `title` / `brand` —— 搜索引擎和分享卡片里显示的就是它们；
 *   2) `primary.url` —— 默认取 timing.json 的 brand.url（没有协议头），
 *      真正要跳转时这里应该写**完整 URL**（https://…），否则会当成站内相对路径；
 *   3) `secondary.url` —— 不需要第二个按钮就留空串；
 *   4) `footer` —— 商用素材的授权声明、备案号放这里。
 * 其余字段不改也能发布：它们都由 timing.json 兜底。
 */
export const WEB: WebConfig = {
  title: `${timing.title} · 网页版`,
  brand: timing.brand.name,
  logo: timing.brand.logo,
  colors: {
    bg: timing.brand.colors?.bg || "#0b0b12",
    fg: timing.brand.colors?.fg || "#ffffff",
    accent: timing.brand.colors?.accent || "#33e1a1",
  },
  // 画幅文案从 timing.json 推导，别写死"竖屏"：横版项目（1920×1080）会显示成错的
  posterNote: `一句话做一支${timing.height >= timing.width ? "竖屏" : "横版"} ${timing.aspect} 宣传片 —— 这是用 promo-forge 模板渲出来的成品。`,
  endNote: "想给自己的产品也做一支？从这里开始。",
  // 没填 brand.url 时保持空串（App.tsx 里 url 为空就不渲染这个按钮），
  // 而不是拼出一个 "https://" 的死链
  primary: { label: "免费下载", url: timing.brand.url ? `https://${timing.brand.url.replace(/^https?:\/\//, "")}` : "" },
  secondary: { label: "了解模板", url: "" },
  footer: `© ${timing.brand.name} · 由 promo-forge 生成`,
};

/**
 * 播放器参数也从 timing.json 推导：
 * 网页版的时长/帧率**必须和成片一致**，否则旁白和画面会错位。
 * （预览版 composition 是 30fps，网页版仍用正式帧率 —— 网页不需要省那点解码。）
 */
export const PLAYER = {
  fps: timing.fps,
  durationInFrames: Math.round(timing.duration * timing.fps),
  width: timing.width,
  height: timing.height,
  /** 画幅文案（"9:16" / "16:9"），页面提示语和海报文案共用，避免各自写死 */
  aspect: timing.aspect,
  /** CSS 用的宽高比，给容器撑出正确高度，避免加载完成后页面跳动 */
  ratio: `${timing.width} / ${timing.height}`,
} as const;
