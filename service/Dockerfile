# promo-forge 校验服务镜像（MCP stdio）
#
# 定位：只装「校验」需要的东西，把仓库里的确定性闸门包成 MCP 工具。
# 渲染（Remotion/Node）与真实截图（Playwright 浏览器）不建议塞进本镜像：
#   - 渲染：在宿主机或独立 render 镜像里跑 `npm run render:1080`；
#   - 截图：需要 Playwright + Chromium（约 300MB），按需取消下面两行注释。
#
# 注意：CI 不构建本镜像，本机也未实测构建；这里只锁定依赖与入口。

FROM python:3.9-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONIOENCODING=utf-8 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# ffmpeg/ffprobe：check_video 的硬依赖；Noto CJK：字幕/抽帧中文不落方块
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg fonts-noto-cjk \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 先只拷依赖清单，利用 Docker 层缓存
COPY template/requirements.txt /app/template/requirements.txt
RUN pip install --no-cache-dir -r /app/template/requirements.txt

# 再拷完整仓库（含 template/scripts 与 service/）
COPY . /app

# Playwright 浏览器层（仅 capture.py 真实截图需要，默认关闭）
# RUN pip install --no-cache-dir playwright==1.40.0 \
#     && python -m playwright install --with-deps chromium

# MCP stdio server：stdin/stdout 即 JSON-RPC 通道，不要在 ENTRYPOINT 里加日志前缀
ENTRYPOINT ["python", "service/mcp_server.py"]
