/**
 * index.ts —— Remotion 入口。`remotion studio src/index.ts` / `remotion render src/index.ts Promo` 都从这里进。
 *
 * 只做一件事：registerRoot。所有 composition 定义在 Root.tsx，样式在 index.css。
 */
import { registerRoot } from "remotion";
import { RemotionRoot } from "./Root";
import "./index.css";

registerRoot(RemotionRoot);
