/**
 * web/main.tsx —— 网页版入口。
 *
 * 只做三件事：把 <App> 挂到 #root，按 WEB.title 设置 document.title，
 * 以及把地址栏 theme-color 换成品牌底色
 * （这两个都不写在 index.html 里，是为了让它们和 src/timing.json 保持同一来源 ——
 *  改品牌名/品牌色只需要改一个地方）。
 */
import { createRoot } from "react-dom/client";
import { App } from "./App";
import { WEB } from "./config";
import "./style.css";

const host = document.getElementById("root");
if (!host) {
  // 正常情况不可能发生（index.html 里就有 #root）；写出来是为了让类型收窄，
  // 而不是用 `as HTMLElement` 把风险藏起来。
  throw new Error("#root 不存在：检查 web/index.html 是否被改动");
}

document.title = WEB.title;
// 地址栏配色 + body 底色跟着品牌底色走。为什么要在 JS 里再设一遍 body：
// 行内 --bg 定义在 .page 上，html/body 是它的**祖先**，拿不到那个变量，
// 只会用 :root 的兜底值；body 底色决定了首屏绘制和移动端下拉回弹露出的那块颜色，
// 不跟着 timing.json 变的话，换品牌色就会在页面边缘看到模板默认的深紫黑。
const themeBg = WEB.colors.bg;
document.querySelector('meta[name="theme-color"]')?.setAttribute("content", themeBg);
document.body.style.backgroundColor = themeBg;

createRoot(host).render(<App />);
