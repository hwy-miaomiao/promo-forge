import { Config } from "@remotion/cli/config";

// 中间帧格式决定输出的像素格式：JPEG 更快，但 ffmpeg 会把它写成 **yuvj420p**（full range），
// 部分平台会拒收或重新转码（check_video.py 会为此报 WARN）。成片要直接投平台，所以用 PNG
// 换准确的 yuv420p；只想快速看结构时再用 `--image-format=jpeg` 临时提速。
Config.setVideoImageFormat("png");
Config.setOverwriteOutput(true);
Config.setCodec("h264");
Config.setPixelFormat("yuv420p");
Config.setCrf(14);

// 中文渲染依赖系统字体，Chromium 的默认渲染器在部分 Linux/无头环境下会缺字形
Config.setChromiumOpenGlRenderer("angle");
