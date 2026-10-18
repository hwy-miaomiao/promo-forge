/**
 * web/main.tsx —— 网页版入口。
 *
 * 只做三件事：把 <App> 挂到 #root，按 WEB.title 设置 document.title，
 * 以及把地址栏 theme-color 换成品牌底色
 * （这两个都不写在 index.html 里，是为了让它们和 src/timing.json 保持同一来源 ——
 *  改品牌名/品牌色只需要改一个地方）。
 *
 * R5-TPL-01 教训：整页白屏时浏览器里只剩一句 Uncaught TypeError + 一片黑，
 * 用户根本不知道发生了什么。所以这里做两层兜底：
 *   1) App 用**动态 import** 加载 —— 模块加载阶段（如 getStaticFiles() 在播放器外
 *      返回 undefined 后又被解引用 .length）抛的错会落到 catch，渲染一段可见说明；
 *   2) 组件树外层套 <BootErrorBoundary> —— 运行期渲染错误不再白屏。
 * 这样最坏情况也能看到「网页版加载失败 + 具体报错」，而不是一片空白。
 */
import { Component, type ErrorInfo, type ReactNode } from "react";
import { createRoot } from "react-dom/client";
import "./style.css";

const host = document.getElementById("root");
if (!host) {
  // 正常情况不可能发生（index.html 里就有 #root）；写出来是为了让类型收窄，
  // 而不是用 `as HTMLElement` 把风险藏起来。
  throw new Error("#root 不存在：检查 web/index.html 是否被改动");
}

const errorBoxStyle: React.CSSProperties = {
  maxWidth: "34rem",
  margin: "12vh auto 0",
  padding: "1.2rem 1.4rem",
  border: "1px solid #e1b733",
  borderRadius: 12,
  background: "#1b1b22",
  color: "#ffffff",
  font: "15px/1.6 system-ui, -apple-system, 'Segoe UI', sans-serif",
  textAlign: "left",
};
const errorTitleStyle: React.CSSProperties = {
  margin: "0 0 .6rem",
  fontWeight: 700,
  color: "#e1b733",
};
const errorDetailStyle: React.CSSProperties = {
  margin: "0 0 .8rem",
  wordBreak: "break-word",
  fontFamily: "ui-monospace, Consolas, monospace",
  fontSize: 13,
};
const errorHintStyle: React.CSSProperties = { margin: 0, color: "#b8b8c4", fontSize: 13 };

const BOOT_HINT = "先试 npm run web:build 重新构建；仍失败就把上面这行报错和浏览器 console 一起反馈。";

/** 直观的启动失败提示（替代"黑屏 + 空白页"）：不需要 React，直接写 DOM。 */
const showBootError = (err: unknown): void => {
  const message = err instanceof Error ? `${err.name}: ${err.message}` : String(err);
  const box = document.createElement("div");
  box.setAttribute("role", "alert");
  Object.assign(box.style, errorBoxStyle);
  const title = document.createElement("p");
  title.textContent = "网页版加载失败";
  Object.assign(title.style, errorTitleStyle);
  const detail = document.createElement("p");
  detail.textContent = message;
  Object.assign(detail.style, errorDetailStyle);
  const hint = document.createElement("p");
  hint.textContent = BOOT_HINT;
  Object.assign(hint.style, errorHintStyle);
  box.append(title, detail, hint);
  host.replaceChildren(box);
};

/** 运行期渲染错误兜底：组件抛错时不再留一片空白。 */
class BootErrorBoundary extends Component<{ children: ReactNode }, { error: Error | null }> {
  state: { error: Error | null } = { error: null };

  static getDerivedStateFromError(error: Error): { error: Error } {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo): void {
    console.error("[web] 渲染失败：", error, info);
  }

  render(): ReactNode {
    if (this.state.error) {
      return (
        <div role="alert" style={errorBoxStyle}>
          <p style={errorTitleStyle}>网页版渲染失败</p>
          <p style={errorDetailStyle}>
            {this.state.error.name}: {this.state.error.message}
          </p>
          <p style={errorHintStyle}>{BOOT_HINT}</p>
        </div>
      );
    }
    return this.props.children;
  }
}

const boot = async (): Promise<void> => {
  try {
    // 动态 import：模块加载阶段的错误也能进 catch（静态 import 的错误发生在
    // createRoot 之前，会直接变成浏览器里的 Uncaught error + 白屏）。
    const [{ App }, { WEB }] = await Promise.all([import("./App"), import("./config")]);
    document.title = WEB.title;
    const themeBg = WEB.colors.bg;
    // 地址栏配色 + body 底色跟着品牌底色走。为什么要在 JS 里再设一遍 body：
    // 行内 --bg 定义在 .page 上，html/body 是它的**祖先**，拿不到那个变量，
    // 只会用 :root 的兜底值；body 底色决定了首屏绘制和移动端下拉回弹露出的那块颜色，
    // 不跟着 timing.json 变的话，换品牌色就会在页面边缘看到模板默认的深紫黑。
    document.querySelector('meta[name="theme-color"]')?.setAttribute("content", themeBg);
    document.body.style.backgroundColor = themeBg;
    createRoot(host).render(
      <BootErrorBoundary>
        <App />
      </BootErrorBoundary>,
    );
  } catch (err) {
    console.error("[web] 启动失败：", err);
    showBootError(err);
  }
};

void boot();
