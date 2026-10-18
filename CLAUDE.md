# CLAUDE.md

本项目的全部工作规范在 [`AGENTS.md`](./AGENTS.md)。请在开始任何工作之前完整读取它，并严格按其中的阶段与校验闸门执行；
各阶段细节在 `references/` 目录，按 AGENTS.md 第 8 节的指引按需读取。

速记：竖屏 1080×1920 优先 · 免费配音（edge-tts）默认 · `timing.json` 是唯一事实来源 ·
`check_assets.py` 与 `check_video.py` 必须 0 ERROR · 需求没确认不许开工。

本地自测：`python template/scripts/selftest.py`（纯函数自测，不渲染、不需要 ffmpeg；GitHub Actions 也跑这一套）。
改了 `template/scripts/` 下的脚本，先跑它再跑对应的真实闸门。
