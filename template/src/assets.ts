/**
 * assets.ts —— 网页播放器需要预取的静态资源清单。
 *
 * 为什么要有这个文件：
 * 浏览器里的 <Player> 是边播边拉的，音乐没下完就会出现"画面在走、声音是空的"。
 * 网页版（web/App.tsx）会在挂载 Player **之前**用 `prefetch(..., { method: "blob-url" })`
 * 把这些资源一次性下完，再放行播放，所以清单必须在这里集中维护，而不是散在组件里。
 *
 * 约定：这里的路径一律是**相对 public/ 的路径**（和 timing.json 里的写法一致），
 * 交给 remotion 的 staticFile() 时由它负责拼成当前环境下的 URL；
 * 网页版里也用同一份相对路径（Vite 的 publicDir 已指向 ../public）。
 */

import { TIMING } from "./lib/timing";

/** 去掉开头多余的 `/`，统一成 "audio/music.wav" 这种形状。 */
const normalize = (p: string): string => p.trim().replace(/^\.?\//, "").replace(/^public\//, "");

const list: string[] = [];

/** 1. 配乐：混音脚本会写出 public/<music.file>，这是最不能缺的一项。 */
if (TIMING.music.file.trim() !== "") list.push(normalize(TIMING.music.file));

/**
 * 2. 品牌 logo：只有非空才加。
 * 示例项目的 logo 是空串 —— 空串会被 staticFile 解析成 public/ 目录本身，
 * 预取时直接 404 甚至报错，所以这里必须显式判空（场景里同理）。
 */
if (TIMING.brand.logo.trim() !== "") list.push(normalize(TIMING.brand.logo));

/**
 * 3. 配音分轨（可选）。
 * tts.py 会把逐句配音写到 public/audio/vo/ 下；混音脚本 mix.py 把成品写到 music.file。
 * 如果项目改成"网页版直接播人声而不是成品混音"，把下面这行打开即可：
 *
 *   TIMING.voice.lines.forEach((line) => list.push(`audio/vo/${line.id}.mp3`));
 *
 * 默认注释掉：文件不存在时 prefetch 会整条失败，宁可少预取也不要让网页版白屏。
 */

/** 去重 + 冻结：防止调用方不小心 push 进去导致每次 render 引用变化。 */
export const ASSETS: string[] = Object.freeze([...new Set(list)]) as string[];

/**
 * 把清单里的相对路径变成浏览器能直接取的 URL（相对 public/ 的根）。
 *
 * 为什么不直接用 remotion 的 staticFile()：
 *   staticFile() 在 Remotion CLI（Studio / render）里会拼成 `http://localhost:3000/...`，
 *   但在 @remotion/player 的网页版里，资源是通过 Vite 的 publicDir 提供的，
 *   根路径就是站点根 —— 两者语义不同。这里统一返回**站点根开头的绝对路径**，
 *   Vite 的 base 是默认的 "/"，因此两边都对得上。
 *
 * ⚠️ 部署到子路径（例如 GitHub Pages 的 /promo/）时，要用构建时的 base 前缀，
 * 把下面这行改成 `import.meta.env.BASE_URL` 参与拼接；模板默认按站点根部署。
 */
export const assetUrl = (path: string): string => `/${normalize(path)}`;
