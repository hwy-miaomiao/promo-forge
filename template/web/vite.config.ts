/**
 * web/vite.config.ts —— 网页播放版的构建配置。
 *
 * 三个"必须这么写"的地方：
 *   1) `root: __dirname` —— vite 的 root 是 index.html 所在目录。web/ 和 src/ 是兄弟目录，
 *      不显式指定的话 vite 会以项目根为 root，`/main.tsx` 就找不到了；
 *   2) `publicDir: ../public` —— 视频工程的 public/ 才是素材所在（audio/、img/、fonts/）。
 *      网页版和 Studio **共用同一份素材**，不复制第二份，避免"改了 Studio 忘了网页"；
 *   3) `build.outDir: ../out/web` + `emptyOutDir: true` —— 产物和其它输出（预览、成片）
 *      统一放在 out/ 下，一条 out/web 就能整个删掉重来。
 *
 * 端口定成 5199（不是 5173）：Remotion Studio 常用 3000 段，把网页版隔开，
 * 两个一起开着的时候不用猜哪个是哪个。`npm run web:serve` 用的是 18765。
 */
import path from "node:path";
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  root: __dirname,
  // 素材目录指向视频工程的 public/，网页版与 Studio 共用同一份文件
  publicDir: path.resolve(__dirname, "../public"),
  plugins: [react()],
  server: {
    port: 5199,
    // 开发时允许局域网手机访问，方便真机看竖屏效果
    host: true,
    open: false,
  },
  build: {
    outDir: path.resolve(__dirname, "../out/web"),
    emptyOutDir: true,
    // 播放器体积不小，但这是自托管的播放页，不做花哨的分包策略：
    // 只把 react 单独切出来方便浏览器缓存。
    // ⚠️ 用函数而不是对象字面量：对象写法里如果某个包被别的 chunk 吃掉了，
    // rollup 会生成**空 chunk** 并以非零码退出（`Generated an empty chunk: "react"`），
    // 把一条本来成功的构建变成 CI 里的失败。
    rollupOptions: {
      output: {
        manualChunks(id: string) {
          if (id.includes("node_modules/react") || id.includes("node_modules/scheduler")) return "react";
          return undefined;
        },
      },
    },
  },
});
