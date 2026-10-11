/**
 * web/main.tsx —— 网页版入口。
 *
 * 只做两件事：把 <App> 挂到 #root，以及按 WEB.title 设置 document.title
 * （标题不写在 index.html 里，是为了让它和 src/timing.json 保持同一来源 ——
 *  改品牌名只需要改一个地方）。
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

createRoot(host).render(<App />);
