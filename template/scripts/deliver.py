#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""promo-forge · 阶段 10「渲染与交付」之平台交付预检 + 封面候选（deliver.py）。

用途
----
``check_video.py`` 回答的是「这支片子自身合不合格」（规格对不对、响度在不在区间、
有没有空档）。本脚本回答的是下一个问题：**同一个 mp4 文件，发到抖音 / 小红书 /
视频号 / YouTube Shorts，会不会被拒、会不会被平台压糊、哪些值只是推荐值。**

两个子命令：

``check``
    对这一支 mp4 先做两组**全局检查**（所有平台共用）：① 完整解码一遍
    （``ffmpeg -v error -xerror``，ffprobe 只读元数据，不能证明文件可播）；
    ② 旋转元数据（``rotate=90`` 时按播放器显示尺寸判定，并与 ``timing.json``
    声明比对）。然后逐平台打印检查表（时长 / 分辨率与画幅 / 帧率 / 码率 /
    文件体积 / 音轨 / 响度），每项给 ``OK`` / ``WARN`` / ``ERROR`` 与「怎么改」
    的一句话建议。有 ERROR 时非零退出（AGENTS.md 第 7 节语义：发现问题就非零
    退出并打印 ERROR: 行）。

``cover``
    从成片里抽 3 帧候选，每帧生成一张 1080×1920 封面：叠加品牌名 + 一行标题。
    文字用 Pillow 量 bbox，**越界就自动缩小字号重排**，并打印每张封面的实际
    bbox 与安全区判定结果。``--position`` 可把文字块放到安全区的底（默认）/
    中（56%）/ 顶——成片自己已经有字幕或大字时用 ``middle``，否则两层字会叠在一起。
    最后拼一张联系表供人挑。

平台参数的出处（**诚实标注**，见下面的 PLATFORMS 表）
------------------------------------------------------
本脚本里的每个平台参数都带 ``conf``（可信度）与 ``src``（出处 URL）：

``official``
    **平台官方文档直接查到的数字**，可以直接当硬限制用。

``suggestion``
    **平台官方原文用「建议」的推荐值**（例如视频号「建议 10Mbps 及以上」）：数字有官方
    出处，但不是拒收门槛；脚本只做提示、判 WARN，绝不判 ERROR。

``thirdparty``
    **第三方汇总/博客**，方向可信、数字可能过期；脚本按「WARN 而非 ERROR」处理，
    并在输出里明确标出这条不是官方口径。

``unknown``
    **没查到确切数字，脚本不编造**：只打印一行「需人工确认」，不参与判定，
    也不会因此让脚本非零退出。

``project``
    **本项目自己的规范**（AGENTS.md），不是任何平台的参数：例如「成片必须有音轨」
    「必须有视频流」。闸门按硬规矩处理（违反就是 ERROR），但输出里标「本项目规范」
    而不是「平台硬限制（官方文档）」，避免把我们的规矩说成平台要求。

实际查到的来源（2026-10，本机可访问的只有下列这些）：

* 视频号官方帮助中心「视频号发表的素材有什么要求？」
  https://findeross.weixin.qq.com/cgi-bin/mmfindernodelivecrmwebbroker-bin/helper-center/pages/DCbKPGzZkvVHcIr6
  —— 官方原文：格式「暂不支持 HDR 视频，其他不限」；比例「宽高比 0.33 ~ 3.0，
  建议上传标准比例的视频（16:9 或者 9:16）」；时长「手机端 3 秒 ~ 60 分钟，助手端
  3 秒 ~ 8 小时」；体积「手机端最大 2G，助手端最大 20G」；码率「建议 10Mbps 及以上」；
  编码「手机端支持 H.265(HEVC) 和 H.264；Windows 助手端一般仅支持 H.264」。
* YouTube Shorts 官方帮助（**本机网络取不到正文**，仅确认页面存在并引用了时长口径）：
  https://support.google.com/youtube/answer/12779649  （上传 Shorts / 格式）
  https://support.google.com/youtube/answer/15424877  （3 分钟以内的 Shorts）
* 第三方汇总（**不是官方**，只做交叉参考）：
  https://www.toolbox365.cn/tutorials/video-compress-platform-upload-specs-2026/
  —— 抖音 4GB / 15 分钟 / 推荐 9:16 1080×1920；小红书 5GB / 15 分钟 / 推荐
  9:16 或 3:4 1080×1920；视频号 1GB / 60 分钟（与上面的官方页不一致，故视频号一律
  以官方页为准）。

按上面这份原文，视频号的 ``aspect`` / ``vbitrate`` 官方用词是「**建议**」，所以标
``suggestion``、只判 WARN，输出里不写「硬限制 / 要求 / 超了会被拒收」；官方原文没有给
分辨率数字，1080×1920 来自 AGENTS.md 第 1 节的本项目规范，所以标 ``project``。

**没有查到的**（脚本标 ``unknown`` 并打印「需人工确认」，绝不编数字）：
抖音与小红书的**官方**体积上限、时长上限、码率建议——两家的帮助中心正文都是
JS 动态渲染，``web_fetch`` 取回来是空页（抖音开放平台
https://open.douyin.com/platform/resource/docs/openapi/video-management/douyin/create/upload/
与 https://developer.open-douyin.com/docs/resource/zh-CN/dop/develop/openapi/video-management/douyin/create-video/video-part-upload
均取不到正文），''小红书'' 也没检索到官方帮助页。另外 **YouTube Shorts 的体积上限**
与「Shorts 专用画幅硬要求」同样没取到官方正文。

典型调用
--------
    python scripts/deliver.py check out/final-1080p.mp4
    python scripts/deliver.py check out/final-1080p.mp4 --platform all
    python scripts/deliver.py check out/final-1080p.mp4 --platform douyin
    python scripts/deliver.py cover
    python scripts/deliver.py cover out/final-1080p.mp4 --out out/covers --title "小账本 · 示例片"
    python scripts/deliver.py cover --frames 3.6,21.7,43.2

依赖
----
必需：ffmpeg / ffprobe（硬依赖，``check`` 会用 ffmpeg 完整解码一遍成片）、
``src/timing.json``、``scripts/_common.py``。
封面还必需：Pillow（缺了 ``cover`` 直接 ERROR，``check`` 不受影响）。
复用 ``scripts/check_video.py`` 的解析实现（``_parse_fraction`` / ``_parse_ebur128`` /
``_ffprobe``），不重复造轮子；读不到它时退回本文件里的等价实现。

退出码
------
``check``：0 = 所选平台 0 ERROR；1 = 有 ERROR（或缺依赖/文件不存在）。
``cover``：0 = 每张封面都通过安全区判定；1 = 有封面文字压线/放不下，或依赖缺失。

硬约束（对齐 AGENTS.md）
------------------------
* 竖屏安全区：**上 10% / 下 18% / 左 6% / 右 14%**（AGENTS.md 第 4 节）。
* 响度区间 **-16 ~ -12 LUFS**、真峰值安全线 **-0.5 dBTP**（AGENTS.md 阶段 8 / 第 7 节），
  与 ``check_video.py`` 完全一致。
* 不新增任何 pip / npm 依赖：只用标准库 + 已装的 Pillow；ffmpeg / ffprobe 是既有硬依赖。
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import math
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

# ---------------------------------------------------------------------------
# 复用 scripts/_common.py（AGENTS.md 第 6 节的公共底座）
# ---------------------------------------------------------------------------
sys.path.insert(0, str(Path(__file__).resolve().parent))
try:  # pragma: no cover - 只在 _common.py 缺失时走到
    from _common import (  # type: ignore
        PROJECT_ROOT,
        ensure_dir,
        error,
        fmt_sec,
        has_cmd,
        human,
        load_timing,
        log,
        rel,
        system_font_candidates,
        warn,
    )
except ImportError as exc:  # pragma: no cover
    sys.stderr.write(
        "ERROR: 无法导入 scripts/_common.py（%s）。请确认 scripts 目录完整，"
        "并在项目根目录下运行本脚本。\n" % (exc,)
    )
    raise SystemExit(1)


# ---------------------------------------------------------------------------
# 复用 scripts/check_video.py 的解析实现（同一目录，彼此独立可运行）
# ---------------------------------------------------------------------------
_CV: Any = None


def _load_check_video() -> Any:
    """按文件路径加载同级 check_video.py；失败返回 None（退回本文件的等价实现）。

    这里不用 ``import check_video``：它依赖 sys.path 恰好含 scripts/，而本脚本
    已经被别的东西 import 过时 sys.path 未必干净。按路径显式加载最稳。
    """
    global _CV
    if _CV is None:
        path = Path(__file__).resolve().parent / "check_video.py"
        if not path.is_file():
            _CV = False
        else:
            try:
                spec = importlib.util.spec_from_file_location("pf_check_video", path)
                module = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
                spec.loader.exec_module(module)  # type: ignore[union-attr]
                _CV = module
            except Exception:  # noqa: BLE001 - 复用失败不该让交付预检挂掉
                _CV = False
    return _CV or None


# ===========================================================================
# 阈值常量（每条都标注来源；与 check_video.py 保持一致）
# ===========================================================================

#: 响度区间。来源：AGENTS.md 阶段 8 母带目标 -14 LUFS；第 7 节闸门 -16 ~ -12 LUFS。
DEFAULT_LUFS_MIN = -16.0
DEFAULT_LUFS_MAX = -12.0
#: 真峰值安全线。来源：AGENTS.md 阶段 8（目标 -1 dBFS，超过 -0.5 dBTP 有削波风险）。
TRUE_PEAK_WARN_DBTP = -0.5

#: 完整解码校验的超时（秒）。1080p/45s 的成片实测约 1.3s，600s 是给长片留的余量。
#: 注意：ffprobe 只能读元数据，moov 完整但 mdat 被截断的文件照样「看起来正常」，
#: 必须真解码一遍才能发现。见 ``_decode_failure``。
DECODE_TIMEOUT_S = 600

#: 竖屏安全区（相对画面高/宽）。来源：AGENTS.md 第 4 节
#: 「上 10%、下 18%、左 6%、右 14%（右侧留给平台互动按钮）」，
#: 与 references/校验清单.md 的 B1、check_video.py 的提示文字一致。
SAFE_TOP = 0.10
SAFE_BOTTOM = 0.18
SAFE_LEFT = 0.06
SAFE_RIGHT = 0.14

#: 封面尺寸。来源：本脚本契约（1080×1920，与成片同尺寸，平台按原图展示）。
COVER_WIDTH = 1080
COVER_HEIGHT = 1920
#: 封面默认抽帧时刻（秒）。来源：本脚本契约——3.6 hook 爆发 / 21.7 demo 图表 / 43.2 CTA。
DEFAULT_COVER_FRAMES = (3.6, 21.7, 43.2)
#: 字号搜索范围。标题从大到小试，直到文字 bbox 落进安全区。
COVER_TITLE_SIZE_MAX = 108
COVER_TITLE_SIZE_MIN = 34
COVER_TITLE_SIZE_STEP = 2
#: 品牌名字号相对标题的比例（品牌名是配角，永远比标题小）。
COVER_BRAND_RATIO = 0.46
#: 行距倍数（行高 = 字号 × LINE_SPACING）。
COVER_LINE_SPACING = 1.28
#: 文字块距安全区底/顶边的留白（px），避免刚好贴线。
COVER_MARGIN_PX = 24
#: ``--position middle`` 时文字块的垂直锚点（相对封面高）。0.55 落在安全区
#: （192px ~ 1574px）的中段，成片底部那一行大字（通常在 65% 以下）不会被压住。
COVER_MIDDLE_ANCHOR = 0.56
#: 压暗层（scrim）最深处的不透明度。来源：本脚本契约——
#: 真实截图当底图时文字容易和画面撞色，压一层渐变保证对比度（对照校验清单 B3）。
COVER_SCRIM_ALPHA = 210
#: 压暗后仍不满意时可读性兜底：文字描边宽度（px）
COVER_STROKE_PX = 4
#: 封面品牌名的默认强调色（RGB）。来源：模板 timing.json 的 brand.colors.accent（#33E1A1）；
#: 模板把它做成可配置项后，这里只当**缺省兜底**——命令行不再硬编码这个颜色。
DEFAULT_BRAND_ACCENT = (51, 225, 161)
#: 封面标题的行数硬上限。默认 2，与 AGENTS.md 阶段 7「竖屏字幕最多 2 行」一致；
#: 运行时用 timing.json 的 captions.maxLines 覆盖（缺失时用这个值）。
CAPTION_MAX_LINES = 2

#: 撞字探测（成片自己已经有亮色文字的区域，封面文字压上去会两层都读不清）
#: 阈值拿模板示例成片实测标定（三帧 × 三个位置，共 9 个样本）：
#:   真有字的区域 亮像素 5.31~15.40% / 水平突变 2.27~10.20%
#:   干净区域     亮像素  0.00%     / 水平突变 0.00~1.90%
#: 结论：**亮像素占比是可靠信号，水平突变只能做旁证**（UI 边框、渐变都会产生突变）。
#: 所以判定只看亮像素，突变只用来区分「确认有字」与「疑似有字」。
CLASH_BRIGHT_PCT = 4.0
CLASH_EDGE_PCT = 2.0
CLASH_DOWNSCALE = 4

#: 联系表（复用 media.py sheet 的思路：按行等高、每张缩到固定宽、下面写标签）
SHEET_NAME = "_sheet.jpg"
SHEET_COLS = 3
SHEET_THUMB_WIDTH = 380
SHEET_QUALITY = 90
SHEET_BG = (24, 24, 28)
SHEET_BORDER = (72, 74, 86)
SHEET_FG = (236, 236, 242)
SHEET_DIM = (152, 154, 166)

FFPROBE_TIMEOUT_S = 120.0
FFMPEG_TIMEOUT_S = 900.0


# ===========================================================================
# 平台规格表
# ===========================================================================
#
# 字段：
#   name      中文名
#   dur       (最小秒, 最大秒)
#   size      (最大字节, ) —— None = 需人工确认
#   aspect    ((最小宽高比, 最大宽高比), 说明)
#   res       (推荐宽, 推荐高) 或 None
#   vbitrate  (建议最低 bps, 建议最高 bps 或 None) 或 None
#   codecs    可直接上传的编码
#   conf      各字段可信度："official" / "thirdparty" / "unknown"
#   src       出处 URL 列表

PLATFORMS: Dict[str, Dict[str, Any]] = {
    "douyin": {
        "name": "抖音",
        "dur": (3.0, 900.0),
        "size": (4 * 1024 ** 3,),
        "aspect": ((0.33, 3.0), "抖音以 9:16 竖屏为主，横屏会加黑边"),
        "res": (1080, 1920),
        "vbitrate": (6_000_000, None),
        "codecs": ("h264", "hevc"),
        "audio_codecs": ("aac",),
        # 官方帮助中心正文是 JS 动态渲染，web_fetch 取不到；抖音开放平台文档同样取不到正文。
        "src": [
            ("thirdparty", "时长 15 分钟 / 体积 4GB / 推荐 1080×1920（第三方汇总，非官方）",
             "https://www.toolbox365.cn/tutorials/video-compress-platform-upload-specs-2026/"),
            ("unknown", "官方体积上限与码率建议：需人工确认（抖音帮助中心正文抓不到）",
             "https://open.douyin.com/platform/resource/docs/openapi/video-management/douyin/create/upload/"),
        ],
        "conf": {"dur": "thirdparty", "size": "thirdparty", "aspect": "thirdparty",
                 "res": "thirdparty", "vbitrate": "unknown"},
        "advice": "发抖音优先 1080×1920 竖屏 + H.264；抖音二压较狠，源文件码率给足（≥6 Mbps）能少掉细节",
    },
    "xiaohongshu": {
        "name": "小红书",
        "dur": (1.0, 900.0),
        "size": (5 * 1024 ** 3,),
        "aspect": ((0.33, 3.0), "小红书支持 9:16 与 3:4，竖屏流量更好"),
        "res": (1080, 1920),
        "vbitrate": (6_000_000, None),
        "codecs": ("h264", "hevc"),
        "audio_codecs": ("aac",),
        "src": [
            ("thirdparty", "时长 15 分钟 / 体积 5GB / 推荐 9:16 或 3:4 1080×1920（第三方汇总，非官方）",
             "https://www.toolbox365.cn/tutorials/video-compress-platform-upload-specs-2026/"),
            ("unknown", "官方体积上限、时长上限与码率建议：需人工确认（小红书帮助中心未检索到可抓取的官方页）",
             "https://creator.xiaohongshu.com/"),
        ],
        "conf": {"dur": "thirdparty", "size": "thirdparty", "aspect": "thirdparty",
                 "res": "thirdparty", "vbitrate": "unknown"},
        "advice": "小红书支持 3:4，如果 9:16 的上下留白太多，可以另出一版 3:4；封面单独上传，本脚本的 cover 就是干这个的",
    },
    "shipinhao": {
        "name": "视频号",
        # 官方：「手机端支持 3 秒 ~ 60 分钟，助手端 3 秒 ~ 8 小时」——取手机端的 60 分钟当硬限制。
        "dur": (3.0, 3600.0),
        # 官方：手机端最大 2G，助手端最大 20G —— 取更严的手机端 2G。
        "size": (2 * 1024 ** 3,),
        # 官方原文是「建议上传标准比例的视频（16:9 或 9:16）」——是建议，不是拒收门槛。
        "aspect": ((0.33, 3.0), "官方建议上传标准比例（16:9 或 9:16）"),
        # 官方原文没有给分辨率数字；1080×1920 来自 AGENTS.md 第 1 节的本项目规范。
        "res": (1080, 1920),
        # 官方：「建议在 10Mbps 及以上」——是建议，不是拒收门槛。
        "vbitrate": (10_000_000, None),
        "codecs": ("h264", "hevc"),
        # 官方：Windows 助手端一般仅支持 H.264（H.265 需装 HEVC 扩展），故 Windows 上只认 h264。
        "audio_codecs": ("aac",),
        "src": [
            ("official", "官方帮助中心：比例 0.33~3.0（建议 16:9 或 9:16）；时长 手机端 3 秒~60 分钟 / 助手端 3 秒~8 小时；"
                         "体积 手机端 ≤2G / 助手端 ≤20G；码率建议 ≥10Mbps；编码 H.264（Windows 助手端）或 H.265；暂不支持 HDR",
             "https://findeross.weixin.qq.com/cgi-bin/mmfindernodelivecrmwebbroker-bin/helper-center/pages/DCbKPGzZkvVHcIr6"),
        ],
        "conf": {"dur": "official", "size": "official", "aspect": "suggestion",
                 "res": "project", "vbitrate": "suggestion"},
        "advice": "视频号官方建议码率 ≥10 Mbps（官方原文用词是「建议」，不是拒收门槛），"
                  "比抖音/小红书都高；从 out/final-1080p.mp4 直接传通常够，"
                  "如果低于建议值建议用 render:4k 或提高 remotion.config.ts 的码率重渲",
    },
    "shorts": {
        "name": "YouTube Shorts",
        # 官方（3 分钟 Shorts）：≤180 秒才算 Shorts，超过就变成普通视频。
        "dur": (1.0, 180.0),
        "size": None,  # 需人工确认
        "aspect": ((0.5, 0.6), "Shorts 竖屏推荐 9:16；非竖屏不会被当成 Shorts 展示"),
        "res": (1080, 1920),
        "vbitrate": (8_000_000, None),
        "codecs": ("h264", "hevc", "vp9", "av1"),
        "audio_codecs": ("aac", "opus", "mp3"),
        "src": [
            ("official", "官方帮助：3 分钟以内的 YouTube Shorts（≤180 秒才算 Shorts）",
             "https://support.google.com/youtube/answer/15424877"),
            ("official", "官方帮助：上传 Shorts（页面存在；本机网络取不到正文，仅确认入口）",
             "https://support.google.com/youtube/answer/12779649"),
            ("unknown", "Shorts 体积上限与画幅硬要求：需人工确认（support.google.com 本机 fetch 失败）",
             "https://support.google.com/youtube/answer/2853702"),
        ],
        "conf": {"dur": "official", "size": "unknown", "aspect": "thirdparty",
                 "res": "thirdparty", "vbitrate": "unknown"},
        "advice": "45 秒的片子在 Shorts 时限内；Shorts 画面比例不是 9:16 时不会被当作 Shorts 展示，"
                  "所以竖屏成片直接传即可，不要加黑边",
    },
}

PLATFORM_ORDER = ("douyin", "xiaohongshu", "shipinhao", "shorts")

CONF_LABEL = {
    "official": "平台硬限制（官方文档）",
    "suggestion": "官方建议值（非硬限制）",
    "thirdparty": "推荐值（第三方汇总，非官方）",
    "unknown": "需人工确认（没查到确切数字）",
    "project": "本项目规范（AGENTS.md，不是平台限制）",
}

#: 每次 check 结尾打印的口径说明。放在这里（而不是 cmd_check 里硬编码日志）是为了让
#: ``--selftest`` 能断言「CONF_LABEL 的每一档都有对应说明、且建议档不许写成硬限制」。
CONF_LEGEND = (
    "「平台硬限制（官方文档）」= 官方帮助页写明的数字，超了会被拒收；",
    "「官方建议值（非硬限制）」= 官方原文用「建议」的推荐值，不达标不会被拒收，"
    "脚本只提示、不判 ERROR；",
    "「推荐值（第三方汇总，非官方）」= 只做方向参考，脚本按 WARN 处理，"
    "投稿前请到平台页面复核；",
    "「需人工确认（没查到确切数字）」= 脚本不编数字，不参与判定；",
    "「本项目规范（AGENTS.md，不是平台限制）」= 我们自己的硬规矩（如必须有音轨/视频流），"
    "不是平台参数。",
)


# ===========================================================================
# 结果收集（OK / WARN / ERROR，语义与 check_video.py、AGENTS.md 第 7 节一致）
# ===========================================================================

class Check:
    """一条检查结果。``level`` 是 OK / WARN / ERROR / INFO。"""

    __slots__ = ("name", "value", "rule", "level", "conf", "advice")

    def __init__(self, name: str, value: str, rule: str = "-", level: str = "OK",
                 conf: str = "official", advice: str = "") -> None:
        self.name = name
        self.value = value
        self.rule = rule
        self.level = level
        self.conf = conf
        self.advice = advice


ALL_CHECKS: List[Check] = []


def _mk(name: str, value: str, rule: str = "-", level: str = "OK",
        conf: str = "official", advice: str = "") -> Check:
    chk = Check(name, value, rule, level, conf, advice)
    ALL_CHECKS.append(chk)
    return chk


def _force_utf8() -> None:
    """Windows 控制台默认 GBK，中文与 emoji 会直接抛 UnicodeEncodeError。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except Exception:
            pass


def _resolve_project_path(value: str) -> Path:
    """先按当前工作目录解析，再按项目根解析（脚本在项目根跑，但也容错）。"""
    path = Path(value)
    if path.is_file():
        return path
    candidate = PROJECT_ROOT / value
    return candidate if candidate.exists() else path


# ---------------------------------------------------------------------------
# 宽度感知的纯文本表格（沿用 media.py 的排版思路，中文才能对齐）
# ---------------------------------------------------------------------------

def _dwidth(text: str) -> int:
    import unicodedata
    return sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1 for ch in str(text))


def _dtrim(text: str, width: int) -> str:
    if _dwidth(text) <= width:
        return text
    out = ""
    for ch in str(text):
        if _dwidth(out) + _dwidth(ch) > width - 1:
            break
        out += ch
    return out + "…"


def _dpad(text: str, width: int, right: bool = False) -> str:
    text = _dtrim(text, width)
    pad = " " * max(0, width - _dwidth(text))
    return pad + text if right else text + pad


def _table(headers: Sequence[str], rows: Sequence[Sequence[str]],
           widths: Optional[Sequence[int]] = None) -> None:
    ncol = len(headers)
    if widths is None:
        widths = [max(_dwidth(str(headers[i])), *(_dwidth(str(r[i])) for r in rows)) if rows
                  else _dwidth(str(headers[i])) for i in range(ncol)]
    log("  ".join(_dpad(str(headers[i]), widths[i]) for i in range(ncol)))
    log("  ".join("-" * widths[i] for i in range(ncol)))
    for r in rows:
        log("  ".join(_dpad(str(r[i]), widths[i]) for i in range(ncol)))


# ===========================================================================
# 探测：ffprobe + ebur128（复用 check_video.py 的实现）
# ===========================================================================

def _probe(path: Path) -> Dict[str, Any]:
    """ffprobe -show_format -show_streams 的 JSON。优先用 check_video 的实现。"""
    cv = _load_check_video()
    if cv is not None:
        try:
            return cv._ffprobe(path)
        except SystemExit:
            raise
        except Exception:  # noqa: BLE001
            pass
    # 退回等价实现（只用标准库）
    import subprocess
    proc = subprocess.run(
        ["ffprobe", "-v", "quiet", "-print_format", "json",
         "-show_format", "-show_streams", str(path)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=FFPROBE_TIMEOUT_S,
    )
    text = proc.stdout.decode("utf-8", errors="replace").strip()
    if not text:
        error(f"ffprobe 读不出 {path.name} 的信息（退出码 {proc.returncode}）；"
              "文件可能不是有效的视频或已损坏。")
    return json.loads(text)


def _parse_fraction(value: Any) -> float:
    """把 ffprobe 的 '60/1' / '30000/1001' 变成浮点数（check_video 的实现）。"""
    cv = _load_check_video()
    if cv is not None:
        return float(cv._parse_fraction(value))
    text = str(value or "").strip()
    if not text:
        return 0.0
    if "/" in text:
        num, _, den = text.partition("/")
        try:
            num_f, den_f = float(num), float(den)
        except ValueError:
            return 0.0
        return num_f / den_f if den_f else 0.0
    try:
        return float(text)
    except ValueError:
        return 0.0


def _parse_loudnorm(text: str) -> Tuple[Optional[float], Optional[float]]:
    """从 ``loudnorm=print_format=json`` 的 JSON 块里取 (input_i, input_tp)。

    ``input_i`` 是 gated 的整段 Integrated loudness（与 ebur128 的 ``I`` 同一个量），
    ``input_tp`` 是真峰值 dBTP。ffmpeg 会把 JSON 打在 stderr 尾部、前后夹着普通日志行，
    所以按最后一个 ``{`` 起点逐个 ``JSONDecoder.raw_decode``。
    """
    decoder = json.JSONDecoder()
    for index in range(len(text) - 1, -1, -1):
        if text[index] != "{":
            continue
        try:
            data, _end = decoder.raw_decode(text[index:])
        except ValueError:
            continue
        if not isinstance(data, dict) or "input_i" not in data:
            continue
        try:
            integrated = float(data["input_i"])
        except (TypeError, ValueError):
            return None, None
        try:
            true_peak = float(data["input_tp"])
        except (TypeError, ValueError):
            true_peak = None
        return integrated, true_peak
    return None, None


def _loudnorm(path: Path) -> Tuple[Optional[float], Optional[float]]:
    """ebur128 不可用时的兜底：改成跑 loudnorm 量一次响度与真峰值。

    ``-vn`` 是必须的：``-f null -`` 的默认视频编码器 wrapped_avframe 在精简版 ffmpeg
    （Remotion 自带的 compositor 构建）里被去掉了，不带 ``-vn`` 会在**打开输出文件**时
    就失败（``Error opening output files: Encoder not found``），滤镜一行都不会跑。
    """
    import subprocess
    try:
        proc = subprocess.run(
            ["ffmpeg", "-hide_banner", "-nostdin", "-i", str(path), "-vn",
             "-af", "loudnorm=print_format=json", "-f", "null", "-"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=FFMPEG_TIMEOUT_S,
        )
    except (OSError, subprocess.SubprocessError):
        return None, None
    text = proc.stderr.decode("utf-8", errors="replace") + "\n" + proc.stdout.decode("utf-8", errors="replace")
    if proc.returncode != 0 and "input_i" not in text:
        return None, None
    return _parse_loudnorm(text)


def _loudness_filter_of(text: str) -> str:
    """判断这段 ffmpeg 输出是哪个滤镜量的（"ebur128" / "loudnorm"）。

    ``check_video.py`` 的 ``_analyze_audio`` 在本机没有 ebur128 滤镜时会**自动改跑
    loudnorm**（返回值看不出区别），所以不能只看「谁调用的」：loudnorm 的 JSON 块带
    ``normalization_type`` / ``input_i``，ebur128 的 Summary 块带 ``I:`` / ``Peak:``。
    """
    if "normalization_type" in text or '"input_i"' in text:
        return "loudnorm"
    return "ebur128"


def _ebur128(path: Path) -> Tuple[Optional[float], Optional[float], str]:
    """量一次响度，返回 (Integrated LUFS, True peak dBTP, 数据来源)。

    先试 ``ebur128=peak=true``；有些精简版 ffmpeg 根本没编这个滤镜
    （``No such filter: 'ebur128'``，Remotion 自带的 compositor 构建就是这样），
    这时自动回退到 ``loudnorm=print_format=json``，同样能给出 Integrated loudness
    与真峰值（实测两者相差 < 0.2 dB）。

    ⚠️ 坑（check_video.py 里记过，这里直接复用它的解析）：ebur128 会**逐帧**打印读数，
    开头几帧 gating 块没攒够，``I`` 固定是 -70.0 LUFS；必须只在结尾 Summary 块里取。
    """
    integrated: Optional[float] = None
    true_peak: Optional[float] = None
    cv = _load_check_video()
    if cv is not None:
        try:
            text = cv._ffmpeg_af(path, "ebur128=peak=true")
            integrated, true_peak = cv._parse_ebur128(text)
            if integrated is not None:
                return integrated, true_peak, _loudness_filter_of(text)
        except SystemExit:
            raise
        except Exception:  # noqa: BLE001
            integrated, true_peak = None, None

    import subprocess
    proc = subprocess.run(
        ["ffmpeg", "-hide_banner", "-nostdin", "-i", str(path), "-vn", "-af", "ebur128=peak=true",
         "-f", "null", "-"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=FFMPEG_TIMEOUT_S,
    )
    text = proc.stderr.decode("utf-8", errors="replace") + "\n" + proc.stdout.decode("utf-8", errors="replace")
    if "input_i" in text:
        # check_video.py 那条路走不通时这里可能直接拿到 loudnorm 的输出，别再跑一次
        return *_parse_loudnorm(text), "loudnorm"
    block = text.rsplit("Summary:", 1)[-1] if "Summary:" in text else text
    for match in re.finditer(r"\bI:\s*(-?\d+(?:\.\d+)?)\s*LUFS", block):
        integrated = float(match.group(1))
    for match in re.finditer(r"Peak:\s*(-?\d+(?:\.\d+)?)\s*dBFS", block):
        true_peak = float(match.group(1))
    if integrated is not None:
        return integrated, true_peak, "ebur128"

    integrated, true_peak = _loudnorm(path)
    return integrated, true_peak, "loudnorm" if integrated is not None else ""


def _pick_stream(probe: Dict[str, Any], kind: str) -> Optional[Dict[str, Any]]:
    for stream in probe.get("streams", []) or []:
        if stream.get("codec_type") == kind:
            return stream
    return None


def _display_rotation(video: Optional[Dict[str, Any]]) -> int:
    """读视频流的显示旋转角（度）。没写旋转元数据返回 0。

    优先读 ``side_data_list`` 里 displaymatrix 的 ``rotation``（新版 ffmpeg 用这个）；
    老文件只有 ``tags.rotate``，一并兼容。口径与 ``check_video.py`` 一致：
    旋转 90/270 时「编码尺寸」与「播放器显示尺寸」宽高互换。
    """
    if not video:
        return 0
    for item in video.get("side_data_list") or []:
        if not isinstance(item, dict):
            continue
        try:
            value = float(item.get("rotation"))
        except (TypeError, ValueError):
            continue
        if value:
            return int(round(value))
    tags = video.get("tags") or {}
    try:
        value = float(tags.get("rotate"))
    except (TypeError, ValueError):
        return 0
    return int(round(value))


def _decode_verdict(returncode: int, stderr_text: str) -> Optional[str]:
    """把一次完整解码的 (退出码, stderr) 判成 None=通过 / 字符串=失败原因。

    判据：``ffmpeg -v error -xerror`` 下**任何 stderr 都算失败**，不只看退出码。
    实测截断的 mp4：不加 ``-xerror`` 时 ffmpeg 读到 EOF 就正常退出（rc=0），但
    ``-v error`` 仍会打出 ``Invalid NAL unit size`` / ``partial file`` 这类解码错误行；
    完整成片实测 0 行。所以「rc==0 且 stderr 干净」才算解码通过。
    """
    lines = [line.strip() for line in (stderr_text or "").splitlines() if line.strip()]
    if returncode == 0 and not lines:
        return None
    brief = "；".join(_dtrim(line, 160) for line in lines[:3]) or f"ffmpeg 退出码 {returncode}"
    if returncode != 0:
        return f"解码中断（ffmpeg 退出码 {returncode}）：{brief}"
    return f"解码过程中报错：{brief}"


def _decode_failure(path: Path) -> Optional[str]:
    """完整解码一遍成片的视频流与音频流；通过返回 None，失败返回原因。"""
    command = ["ffmpeg", "-hide_banner", "-v", "error", "-xerror", "-nostdin",
               "-i", str(path), "-f", "null", "-"]
    try:
        proc = subprocess.run(
            command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=DECODE_TIMEOUT_S,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return f"解码校验没跑起来（{type(exc).__name__}: {exc}）"
    text = proc.stderr.decode("utf-8", errors="replace")
    return _decode_verdict(proc.returncode, text)


# ===========================================================================
# 成片事实（一次探测，所有平台共用）
# ===========================================================================

class Media:
    """一支 mp4 的可测事实，全部来自 ffprobe / ffmpeg 实测。"""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.probe = _probe(path)
        self.video = _pick_stream(self.probe, "video")
        self.audio = _pick_stream(self.probe, "audio")
        fmt = self.probe.get("format") or {}
        self.format = fmt

        #: 编码尺寸（ffprobe 的 width/height）。
        self.coded_width = int((self.video or {}).get("width") or 0)
        self.coded_height = int((self.video or {}).get("height") or 0)
        #: 显示旋转角；90/270 时播放器会把画面转过来，宽高互换。
        self.rotation = _display_rotation(self.video)
        #: 播放器里观众看到的尺寸（旋转后的显示尺寸）。所有平台判定必须用它：
        #: 用编码尺寸会让「1080×1920 + rotate=90」这种实际横版的文件按 9:16 假绿
        #: （R7 PY-02；check_video.py 从 R6 起就按显示尺寸判，两边口径要一致）。
        if self.rotation % 180 == 90:
            self.width, self.height = self.coded_height, self.coded_width
        else:
            self.width, self.height = self.coded_width, self.coded_height
        #: 有没有视频流。没有视频流的文件（纯音频 mp4、被 ffmpeg -vn 处理过的产物）
        #: 必须直接判 ERROR：它的 width/height 都是 0，画幅 0.0 会越出所有平台的区间，
        #: 于是「越界」这件事只能靠副作用偶然被发现——而抖音/小红书/Shorts 的第三方
        #: 口径只给 WARN，全套检查就会凑出 0 ERROR 并打印「全部平台 0 ERROR」。
        self.has_video = self.video is not None
        self.codec = str((self.video or {}).get("codec_name") or "?")
        self.pix_fmt = str((self.video or {}).get("pix_fmt") or "?")
        self.audio_codec = str((self.audio or {}).get("codec_name") or "")
        self.fps = _parse_fraction((self.video or {}).get("r_frame_rate")) \
            or _parse_fraction((self.video or {}).get("avg_frame_rate"))

        duration = 0.0
        for candidate in ((self.video or {}).get("duration"), fmt.get("duration")):
            duration = _parse_fraction(candidate)
            if duration > 0:
                break
        self.duration = duration

        try:
            self.file_size = int(fmt.get("size") or 0)
        except (TypeError, ValueError):
            self.file_size = 0
        if not self.file_size:
            try:
                self.file_size = path.stat().st_size
            except OSError:
                self.file_size = 0

        # 视频流码率：优先用 stream 的 bit_rate；没有就用 总体码率 - 音频码率；再没有就 体积/时长。
        self.video_bitrate = self._int_or_none((self.video or {}).get("bit_rate"))
        overall = self._int_or_none(fmt.get("bit_rate"))
        audio_br = self._int_or_none((self.audio or {}).get("bit_rate"))
        if overall:
            self.overall_bitrate = overall
        elif self.duration > 0 and self.file_size:
            self.overall_bitrate = int(self.file_size * 8 / self.duration)
        else:
            self.overall_bitrate = 0
        if self.video_bitrate is None:
            if self.overall_bitrate and audio_br:
                self.video_bitrate = max(0, self.overall_bitrate - audio_br)
            else:
                self.video_bitrate = self.overall_bitrate
        self.audio_bitrate = audio_br

        self.lufs: Optional[float] = None
        self.true_peak: Optional[float] = None
        self.loudness_source: Optional[str] = None   # "ebur128" | "loudnorm"

    @staticmethod
    def _int_or_none(value: Any) -> Optional[int]:
        try:
            n = int(float(value))
        except (TypeError, ValueError):
            return None
        return n if n > 0 else None

    @property
    def aspect(self) -> float:
        return (self.width / self.height) if self.height else 0.0

    @property
    def aspect_label(self) -> str:
        if not self.height:
            return "?"
        for label, (rw, rh) in (("9:16", (9, 16)), ("3:4", (3, 4)), ("1:1", (1, 1)),
                                ("4:3", (4, 3)), ("16:9", (16, 9))):
            if abs(self.aspect - rw / rh) / (rw / rh) < 0.005:
                return label
        return f"{self.aspect:.3f}"

    def measure_loudness(self) -> None:
        self.lufs, self.true_peak, self.loudness_source = _ebur128(self.path)


# ===========================================================================
# 检查项
# ===========================================================================

def _fmt_bitrate(bps: Optional[int]) -> str:
    if not bps:
        return "?"
    return f"{bps / 1_000_000:.2f} Mbps"


def _check_streams(m: Media, p: Dict[str, Any]) -> Check:
    """有没有视频流。这是「这是个成片吗」的前置判据，先于所有平台口径。

    没有视频流时后面的分辨率 / 画幅 / 帧率 / 码率全部失去意义（读到的是 0），
    所以这里给 ERROR，其余检查项按「需人工确认」跳过，避免刷出一堆误导性的红字。
    """
    if m.video is not None:
        value = f"{m.codec} / {m.width}×{m.height}"
        if m.rotation % 180 == 90:
            value += (f"（显示尺寸；编码 {m.coded_width}×{m.coded_height}，"
                      f"旋转 {m.rotation}°——判定一律按显示尺寸）")
        return _mk("视频流", value, "必须有", "OK", "project")
    return _mk("视频流", "无", "必须有", "ERROR", "project",
               "这个文件里没有视频流（只有音频流或完全是空的），它不是一支能交付的成片。"
               "常见原因：导出时只写了音频、或误把 mix.py 的产物当成了成片。"
               "请回到 `npm run render:1080` 重新渲染，产物应是 out/final-1080p.mp4")


def _check_decodable(m: Media, detail: Optional[str]) -> Check:
    """完整解码校验：ffprobe 只读元数据，不能证明文件真的能播。

    截断的 mp4 只要 moov 还在，ffprobe 就能报出 45s / 1080×1920 / h264 / aac，
    ``deliver.py check`` 曾因此对「后半段全丢」的文件假绿（R7 PY-01）。这里完整
    解码一遍（视频流+音频流，见 ``_decode_failure``），解码错误或 stderr 非空即 ERROR。
    """
    if not m.has_video:
        return _mk("可解码性", "?", "完整解码 0 错误", "INFO", "unknown",
                   "没有视频流，解码校验无从谈起；先按「视频流」那条 ERROR 重新渲染")
    if detail is None:
        return _mk("可解码性", "完整解码通过", "完整解码 0 错误", "OK", "project")
    return _mk("可解码性", "解码失败", "完整解码 0 错误", "ERROR", "project",
               detail + "。文件可能在渲染、拷贝或上传过程中被截断/损坏；ffprobe 能读出元数据"
               "不代表能播，请重新渲染 out/final-1080p.mp4 后再交付")


def _check_rotation(m: Media, timing: Dict[str, Any]) -> Check:
    """旋转元数据：判定必须用显示尺寸，且显示方向要与 timing.json 声明一致。

    R7 PY-02：``1080×1920 + rotate=90`` 的样本，观众看到的是 1920×1080 横版，
    deliver 旧版按编码尺寸判成 9:16 假绿。check_video.py 自 R6 起按显示尺寸与
    timing.json 比对并判 ERROR；交付预检用同一口径（本项目规范，conf=project）。
    """
    if not m.has_video:
        return _mk("旋转元数据", "?", "按显示尺寸判定", "INFO", "unknown",
                   "没有视频流，旋转元数据无从谈起；先按「视频流」那条 ERROR 重新渲染")
    if m.rotation % 180 == 0:
        note = "无旋转元数据" if not m.rotation else f"旋转 {m.rotation}°（显示尺寸不变）"
        return _mk("旋转元数据", note, "按显示尺寸判定", "OK", "project")
    value = f"rot={m.rotation}° · 显示 {m.width}×{m.height}" \
            f"（编码 {m.coded_width}×{m.coded_height}）"
    try:
        declared = (int(timing.get("width") or 0), int(timing.get("height") or 0))
    except (TypeError, ValueError):
        declared = (0, 0)
    if not declared[0] or not declared[1]:
        return _mk("旋转元数据", value, "按显示尺寸判定", "WARN", "thirdparty",
                   "timing.json 没声明 width/height，无法核对显示尺寸；请人眼确认播放器里的方向正确")
    rule = f"显示尺寸应为 timing.json 声明的 {declared[0]}×{declared[1]}"
    if (m.width, m.height) == declared:
        return _mk("旋转元数据", value, rule, "OK", "project")
    return _mk("旋转元数据", value, rule, "ERROR", "project",
               f"这个文件的编码尺寸是 {m.coded_width}×{m.coded_height}，但带旋转元数据，"
               f"播放器/平台会转成 {m.width}×{m.height} 显示——观众看到的方向与 timing.json "
               f"声明的 {declared[0]}×{declared[1]} 相反（check_video.py 同样按显示尺寸判 ERROR）。"
               "手机拍摄/剪辑软件导出的素材常带这种元数据；请重新渲染成片，不要直接把它当交付件")



def _check_duration(m: Media, p: Dict[str, Any]) -> Check:
    lo, hi = p["dur"]
    conf = p["conf"]["dur"]
    rule = f"{lo:g}~{hi:g}s"
    if conf == "unknown":
        return _mk("时长", f"{m.duration:.2f}s", "需人工确认", "INFO", conf,
                   "平台时长上限没查到确切数字，请以投稿页面的提示为准")
    if lo <= m.duration <= hi:
        return _mk("时长", f"{m.duration:.2f}s", rule, "OK", conf)
    if m.duration < lo:
        # 只有官方硬限制才判 ERROR；第三方汇总的「下限」只是参考（V13：2 秒的片子
        # 曾被按「推荐值（第三方汇总，非官方）」判 ERROR，等于把非官方口径当硬失败）。
        return _mk("时长", f"{m.duration:.2f}s", rule,
                   "ERROR" if conf == "official" else "WARN", conf,
                   f"短于{lo:g}s 下限："
                   + ("这是官方硬限制，投稿会被拒；" if conf == "official"
                      else "这个下限来自第三方汇总（非官方），投稿前请以平台页面提示为准；")
                   + "回到 timing.json 把 duration 与最后一个场景的 end 一起加长")
    return _mk("时长", f"{m.duration:.2f}s", rule,
               "ERROR" if conf == "official" else "WARN", conf,
               f"超过 {hi:g}s（{hi / 60:.0f} 分钟）："
               + ("这是官方硬限制，必须剪短或分段发；" if conf == "official" else "第三方口径，请到投稿页面确认；")
               + "短视频超过 60 秒完播率会明显掉，建议按 AGENTS.md 第 4 节重新卡节奏而不是硬切")


def _check_resolution(m: Media, p: Dict[str, Any]) -> Check:
    res = p.get("res")
    conf = p["conf"].get("res", "unknown")
    if not m.has_video:
        # 0×0 是「没有视频流」的症状，不是分辨率问题——真正的 ERROR 由 _check_streams 给出
        return _mk("分辨率", f"{m.width}×{m.height}", "需人工确认", "INFO", "unknown",
                   "没有视频流，分辨率无从谈起；先按「视频流」那条 ERROR 重新渲染")
    if not res or not m.width:
        return _mk("分辨率", f"{m.width}×{m.height}", "需人工确认", "INFO", "unknown",
                   "推荐分辨率没查到确切数字，请以投稿页面为准")
    rec_w, rec_h = res
    value = f"{m.width}×{m.height}"
    rule = f"{rec_w}×{rec_h} / {rec_h}×{rec_w}"
    # 平台表里写的是一组「推荐宽×高」，但竖版与横版是同一组标准比例的两个方向：
    # 视频号官方原文是「建议上传标准比例的视频（16:9 或者 9:16）」，抖音/小红书的
    # 推荐值同样只是 9:16 那一档。所以 1080×1920 与 1920×1080（以及它们的 2 倍图）
    # 都算命中推荐尺寸——横版不能判 ERROR（AGENTS.md 第 1 节明确允许 1920×1080 横版），
    # 否则「官方允许横版」与「脚本对横版报错」自相矛盾。
    variants = {
        (rec_w, rec_h): "",
        (rec_h, rec_w): "横版是 16:9，平台同样认可；主流量仍是竖屏，投稿前确认清楚即可",
        (rec_w * 2, rec_h * 2): "2 倍图（render:4k 的产物）会走平台的下采样，正常；"
                                "非必要不必传这么大的文件，1080×1920 就够",
        (rec_h * 2, rec_w * 2): "横版 2 倍图（render:4k 的产物）会走平台的下采样，正常",
    }
    if (m.width, m.height) in variants:
        return _mk("分辨率", value, rule, "OK", conf, variants[(m.width, m.height)])
    # 竖屏但尺寸不是推荐值（例如 720×1280）：能发，但清晰度吃亏。
    # conf=project 表示这条数字来自 AGENTS.md 的本项目交付规范（视频号分辨率），
    # 而 AGENTS.md 第 7 节闸门表把「视频号分辨率按 1080×1920」列为硬规矩 → 必须 ERROR。
    # 只判 WARN 会让交付闸门（0 ERROR 才算过）对违反本项目规范的文件假绿。
    if m.height > m.width:
        level = "ERROR" if conf == "project" else "WARN"
        prefix = ("这是本项目交付规范（AGENTS.md 第 1 节：竖屏 1080×1920，"
                  "第 7 节列为硬规矩），交付闸门按硬规矩判 ERROR，不是平台拒收限制；"
                  if level == "ERROR" else "")
        return _mk("分辨率", value, rule, level, conf,
                   prefix + f"竖屏方向对，但推荐值是 {rec_w}×{rec_h}；"
                   "低于它平台会放大（发虚），高于它会被二压。"
                   "建议按 timing.json 的 width/height 重渲（render:1080 出 1080×1920）")
    # 横屏但不是标准 16:9（例如 1280×720 仍是标准比例、只是分辨率低于推荐值）：
    # 横版本身被 AGENTS.md 第 1 节允许（1920×1080 在 variants 里已判 OK），
    # 这里只处理「横版但分辨率不是项目标准的 1920×1080」——conf=project 时同样是硬规矩。
    level = "ERROR" if conf == "project" else "WARN"
    prefix = ("这是横屏画面，但分辨率不是本项目交付规范（AGENTS.md 第 1 节："
              "首选竖屏 1080×1920，可选横版 1920×1080），交付闸门按硬规矩判 ERROR；"
              if level == "ERROR" else "")
    return _mk("分辨率", value, rule, level, conf,
               prefix + f"这是横屏画面，{p['name']} 的推荐值是竖版 {rec_w}×{rec_h}，"
               f"对应横版 {rec_h}×{rec_w}，当前是 {value}。"
               "横版是规范允许的形态（AGENTS.md 第 1 节：首选竖屏，可选 1920×1080 横版），"
               "要竖屏就改 timing.json 的 aspect 为 9:16、width/height 换成 1080/1920 后重渲")


def _check_aspect(m: Media, p: Dict[str, Any]) -> Check:
    (lo, hi), note = p["aspect"]
    conf = p["conf"].get("aspect", "unknown")
    if not m.has_video:
        # aspect 读到 0.0（height=0），越界只是症状；真 ERROR 在 _check_streams
        return _mk("画幅", "?", "需人工确认", "INFO", "unknown",
                   "没有视频流，画幅无从谈起；先按「视频流」那条 ERROR 重新渲染")
    if conf == "unknown":
        return _mk("画幅", f"{m.aspect_label}（{m.aspect:.3f}）", "需人工确认", "INFO", conf,
                   "平台画幅范围没查到确切数字，请以投稿页面为准")
    if lo <= m.aspect <= hi:
        return _mk("画幅", f"{m.aspect_label}（{m.aspect:.3f}）", f"{lo:g}~{hi:g}", "OK", conf)
    return _mk("画幅", f"{m.aspect_label}（{m.aspect:.3f}）", f"{lo:g}~{hi:g}",
               "ERROR" if conf == "official" else "WARN", conf,
               f"宽高比超出 {lo:g}~{hi:g}。{note}；"
               "按 timing.json 的 aspect 重设 width/height 后重渲即可")


def _check_fps(m: Media, p: Dict[str, Any]) -> Check:
    fps = m.fps
    if not m.has_video:
        return _mk("帧率", "?", "≥24fps", "INFO", "unknown",
                   "没有视频流，帧率无从谈起；先按「视频流」那条 ERROR 重新渲染")
    if fps <= 0:
        return _mk("帧率", "?", "≥24fps", "WARN", "thirdparty",
                   "ffprobe 没报出帧率（可能是可变帧率文件）；请重渲成恒定帧率，卡点才会准")
    if fps >= 50:
        return _mk("帧率", f"{fps:g}fps", "≥24fps", "OK", "thirdparty")
    if fps >= 30:
        return _mk("帧率", f"{fps:g}fps", "≥24fps", "OK", "thirdparty",
                   "30fps 平台接受；本模板 timing.json 默认 60fps，预览版才用 30fps，正式交付别用预览版")
    if fps >= 24:
        return _mk("帧率", f"{fps:g}fps", "≥24fps", "WARN", "thirdparty",
                   "低于 30fps 运动镜头会顿；只有纯文字/图表为主的片子才可以接受")
    # 低于 24fps 只 WARN：23.976 / 24 / 25 / 29.97 都是标准帧率，平台不会因此拒收；
    # 而且平台帧率本来就没有官方硬限制出处（本表里 fps 一项是 thirdparty），
    # 按 docstring 的承诺，thirdparty 口径最多 WARN，不许判 ERROR。
    return _mk("帧率", f"{fps:g}fps", "≥24fps", "WARN", "thirdparty",
               "低于 24fps（23.976 / 24 是标准帧率，平台一般照收）；但低于 30fps 运动镜头会顿，"
               "本模板默认 60fps 更稳，建议把 timing.json 的 fps 改成 60 后重渲")


def _check_bitrate(m: Media, p: Dict[str, Any]) -> Check:
    spec = p.get("vbitrate")
    conf = p["conf"].get("vbitrate", "unknown")
    if not m.has_video:
        return _mk("码率", "?", "需人工确认", "INFO", "unknown",
                   "没有视频流，码率无从谈起；先按「视频流」那条 ERROR 重新渲染")
    if not spec:
        return _mk("码率", _fmt_bitrate(m.video_bitrate), "需人工确认", "INFO", "unknown",
                   "平台码率建议没查到确切数字；经验值（非官方）：1080p 给 6~16 Mbps，越高越抗二压")
    lo, hi = spec
    value = _fmt_bitrate(m.video_bitrate)
    if conf == "unknown":
        # conf=unknown 按本文件 docstring 的口径「只提示、不参与判定」：这里只有一个
        # 经验值，没有可引用的来源，不能既说「需人工确认」又拿经验值判 WARN
        # （V11：1.26 Mbps 的成片曾因此每平台刷 1 条 WARN，且 advice 写成「符合建议值」式口吻）。
        return _mk("码率", value, "需人工确认", "INFO", conf,
                   "平台码率建议没查到确切数字，不参与判定；经验值（非官方，仅供参考）："
                   f"1080p 给 {lo / 1_000_000:g}~16 Mbps，码率越高越抗平台二压")
    rule = f"≥{lo / 1_000_000:g} Mbps" + (f"（≤{hi / 1_000_000:g}）" if hi else "")
    if m.video_bitrate >= lo:
        if hi and m.video_bitrate > hi:
            return _mk("码率", value, rule, "WARN", conf,
                       f"高于 {hi / 1_000_000:g} Mbps：平台必定二压，上传也慢；"
                       "1080p 给到 16 Mbps 已经足够，再高是白花体积")
        if conf == "suggestion":
            ok_note = "符合官方建议值（官方原文用「建议」，不是拒收门槛）"
        elif conf == "thirdparty":
            ok_note = "码率给足能少被压糊"
        else:
            ok_note = "符合官方要求"
        return _mk("码率", value, rule, "OK", conf, ok_note)
    shortfall = lo - m.video_bitrate
    if conf == "suggestion":
        low_note = (f"比官方建议值低 {shortfall / 1_000_000:.1f} Mbps"
                    "（官方原文用「建议」，不是拒收门槛）；")
    elif conf == "thirdparty":
        low_note = (f"比第三方汇总的建议值低 {shortfall / 1_000_000:.1f} Mbps"
                    "（非官方口径，只做方向参考）；")
    else:
        low_note = f"比官方要求低 {shortfall / 1_000_000:.1f} Mbps；"
    if m.video_bitrate >= lo * 0.6:
        return _mk("码率", value, rule, "WARN", conf,
                   low_note + "从 out/final-1080p.mp4 直接传通常够，"
                   "确实偏低就在 remotion.config.ts 里调高码率重渲")
    return _mk("码率", value, rule, "WARN", conf,
               low_note + "平台二压后画面会糊；核对该 mp4 是不是预览版或经过二次压缩，"
               "建议拿渲染原始产物投稿")


def _check_size(m: Media, p: Dict[str, Any]) -> Check:
    spec = p.get("size")
    conf = p["conf"].get("size", "unknown")
    value = human(m.file_size) if m.file_size else "?"
    if not spec:
        return _mk("文件体积", value, "需人工确认", "INFO", "unknown",
                   f"没查到 {p['name']} 的确切体积上限；经验值：1080p/45 秒的片子在 50~150 MB，绝大多数平台都能收")
    limit = spec[0]
    rule = f"≤{human(limit)}"
    if conf == "unknown":
        return _mk("文件体积", value, rule, "INFO", conf,
                   "体积上限没查证到官方口径，请以投稿页面为准")
    if m.file_size <= limit:
        ratio = m.file_size / limit
        level = "OK" if ratio < 0.9 else "WARN"
        advice = "" if level == "OK" else "已经很接近上限了，留点余量；必要时用更低码率重渲"
        return _mk("文件体积", value, rule, level, conf, advice)
    # 超限
    if conf == "official":
        return _mk("文件体积", value, rule, "ERROR", conf,
                   "超过平台体积上限，投稿会被直接拒绝。做法（按性价比排序）：① 降低码率重渲"
                   "（1080p 从 16 Mbps 降到 8 Mbps 约省一半，画质基本无感）；② 缩短片长；"
                   "③ 出 720×1280 版本（只在必须按时用）")
    return _mk("文件体积", value, rule, "WARN", conf,
               "第三方口径说超限，请到投稿页面确认；若确实超了，按「降码率 → 缩短片长 → 降分辨率」的顺序处理")


def _check_audio(m: Media, p: Dict[str, Any]) -> Check:
    # 「有没有音轨」是我们自己的硬规矩（AGENTS.md 阶段 10：别把无声片交出去），不是平台参数；
    # 「用哪种音频编码」则多半是第三方汇总，统一按 thirdparty 标，避免把推荐值说成硬限制。
    if m.audio is None:
        return _mk("音轨", "无", "必须有", "ERROR", "project",
                   "这支 mp4 没有音轨（AGENTS.md 阶段 10 明确：别把无声片当成品交出去）。"
                   "先跑 python scripts/mix.py 生成 public/audio/music.wav，确认 Remotion 里用 "
                   "staticFile 挂到了 <Audio> 上，再重新渲染")
    allowed = p.get("audio_codecs") or ()
    desc = (f"{m.audio_codec or '?'} / {m.audio.get('sample_rate', '?')}Hz / "
            f"{m.audio.get('channels', '?')}声道")
    rule = " / ".join(allowed) if allowed else "必须有"
    if m.audio_codec and allowed and m.audio_codec not in allowed:
        return _mk("音轨", desc, rule, "WARN", "thirdparty",
                   f"音频编码 {m.audio_codec} 不在平台常规支持列表里，平台会转码；"
                   "用 AAC 最稳（remotion.config.ts 的 setAudioCodec 默认就是 aac）")
    return _mk("音轨", desc, rule, "OK", "thirdparty")


def _check_loudness(m: Media, p: Dict[str, Any], lufs_min: float, lufs_max: float) -> Check:
    # 响度口径来自本项目自己的母带规范（AGENTS.md 阶段 8 / 第 7 节），不是任何平台的下限，
    # 所以四个平台共用同一个 conf，用 "project" 而不是 "official"——它在表格里会显示成
    # 「本项目规范」，不会被误读成「平台硬限制（官方文档）」。它也不参与「官方 / 第三方」的区分。
    conf = "project"
    rule = f"{lufs_min:g}~{lufs_max:g} LUFS"
    if m.lufs is None:
        return _mk("响度", "测不到", rule, "WARN", conf,
                   "ebur128 与 loudnorm 都解析不出 Integrated loudness：多半是这份 ffmpeg 缺这两个滤镜"
                   "（Remotion 自带的精简构建就常缺）。手工跑 "
                   "`ffmpeg -i <mp4> -vn -af loudnorm=print_format=json -f null -` 看结尾的 JSON 块；"
                   "还是测不到就换一份完整版 ffmpeg 再跑")
    value = f"{m.lufs:.1f} LUFS"
    if m.true_peak is not None:
        value += f" / 峰值 {m.true_peak:.2f} dBTP"
    if m.loudness_source == "loudnorm":
        value += "（loudnorm 兜底测的）"
    if m.lufs > lufs_max:
        return _mk("响度", value, rule, "WARN", conf,
                   f"比 {lufs_max:g} LUFS 响 {m.lufs - lufs_max:.1f} dB。"
                   "母带目标 -14 LUFS（AGENTS.md 阶段 8）；平台会自动压，但压完动态更差。请重跑 python scripts/mix.py")
    if m.lufs < lufs_min:
        return _mk("响度", value, rule, "WARN", conf,
                   f"比 {lufs_min:g} LUFS 轻 {lufs_min - m.lufs:.1f} dB，"
                   "在手机小音量下会听不清；重跑 python scripts/mix.py（它会把母带拉到 -14 LUFS）")
    if m.true_peak is not None and m.true_peak > TRUE_PEAK_WARN_DBTP:
        return _mk("响度", value, rule, "WARN", conf,
                   f"真峰值 {m.true_peak:.2f} dBTP 高于安全线 {TRUE_PEAK_WARN_DBTP} dBTP，"
                   "平台转码后容易削波；跑 python scripts/mix.py --env 看是哪一段顶上去的")
    return _mk("响度", value, rule, "OK", conf)


def _check_codes(m: Media, p: Dict[str, Any]) -> List[Check]:
    out: List[Check] = []
    allowed = p.get("codecs") or ()
    if allowed and m.codec not in allowed:
        out.append(_mk("视频编码", m.codec, " / ".join(allowed), "WARN", "thirdparty",
                       f"{m.codec} 不在 {p['name']} 的常规支持列表里，平台会先转码（多一次损失）；"
                       "remotion.config.ts 默认 h264，保持默认即可"))
    elif allowed:
        out.append(_mk("视频编码", f"{m.codec} / {m.pix_fmt}", " / ".join(allowed), "OK", "thirdparty"))
    if m.pix_fmt != "yuv420p":
        out.append(_mk("像素格式", m.pix_fmt, "yuv420p", "WARN", "thirdparty",
                       "非 yuv420p 的成片部分平台会拒收或二次转码。"
                       "确认 remotion.config.ts 用 setPixelFormat(\"yuv420p\") 且 setVideoImageFormat(\"png\")"))
    return out


def run_platform_checks(m: Media, key: str, lufs_min: float, lufs_max: float) -> List[Check]:
    """对一个平台跑全部检查项，返回这次产生的 Check 列表。"""
    p = PLATFORMS[key]
    start = len(ALL_CHECKS)
    _check_streams(m, p)
    _check_duration(m, p)
    _check_resolution(m, p)
    _check_aspect(m, p)
    _check_fps(m, p)
    _check_bitrate(m, p)
    _check_size(m, p)
    _check_audio(m, p)
    _check_loudness(m, p, lufs_min, lufs_max)
    _check_codes(m, p)
    return ALL_CHECKS[start:]


# ===========================================================================
# 子命令 1：check
# ===========================================================================

TAG = {"OK": "OK  ", "WARN": "WARN", "ERROR": "ERROR", "INFO": "INFO"}


def _print_platform_block(m: Media, key: str, lufs_min: float, lufs_max: float) -> Tuple[int, int]:
    """打印一个平台的检查表，返回 (errors, warnings)。"""
    p = PLATFORMS[key]
    checks = run_platform_checks(m, key, lufs_min, lufs_max)
    errors = sum(1 for c in checks if c.level == "ERROR")
    warns = sum(1 for c in checks if c.level == "WARN")
    verdict = "ERROR" if errors else ("WARN" if warns else "OK")
    log("")
    log("=" * 78)
    log(f"[{p['name']}]  {verdict}   （{errors} ERROR / {warns} WARN）")
    log("=" * 78)
    rows = [[TAG[c.level], c.name, c.value, c.rule, CONF_LABEL.get(c.conf, c.conf)]
            for c in checks]
    _table(["判定", "项", "实测", "平台口径", "口径性质"], rows)
    fixes = [c for c in checks if c.advice and c.level in ("ERROR", "WARN")]
    if fixes:
        log("")
        log("  怎么改：")
        for c in fixes:
            log(f"    · [{c.level}] {c.name}：{c.advice}")
    log("")
    log(f"  一句话建议：{p['advice']}")
    return errors, warns


def cmd_check(args: argparse.Namespace) -> int:
    keys = list(PLATFORM_ORDER) if args.platform == "all" else [args.platform]
    if not has_cmd("ffprobe") or not has_cmd("ffmpeg"):
        error("缺少 ffmpeg / ffprobe（AGENTS.md 第 6 节的硬依赖）。"
              "Windows: winget install Gyan.FFmpeg；macOS: brew install ffmpeg；"
              "Linux: sudo apt install ffmpeg。装完重跑 python scripts/doctor.py 确认。")
    if args.lufs_min >= args.lufs_max:
        error(f"--lufs-min ({args.lufs_min:g}) 必须小于 --lufs-max ({args.lufs_max:g})。")

    path = _resolve_project_path(args.video)
    if not path.is_file():
        error(f"找不到成片 {args.video}。先跑 `npm run render:1080`（输出 out/final-1080p.mp4），"
              "或把正确路径作为第一个参数传进来。")
    if path.stat().st_size == 0:
        error(f"{rel(path)} 是空文件（0 字节），渲染没有真正完成。")

    timing = load_timing()
    log(f"平台交付预检：{rel(path)}")
    log(f"项目：{timing.get('slug', '?')} · {timing.get('title', '?')} · "
        f"timing.json 声明 {timing.get('width')}×{timing.get('height')} / {timing.get('fps')}fps / "
        f"{timing.get('duration')}s")
    log("")
    log("正在探测（ffprobe 读规格 + 完整解码校验 + ffmpeg ebur128 量响度，视片长可能需要十几秒）…")
    m = Media(path)
    decode_detail = _decode_failure(path)
    m.measure_loudness()

    log("")
    log("-" * 78)
    log("成片实测（所有平台共用这一组数字）")
    log("-" * 78)
    if m.lufs is not None:
        loud_text = f"{m.lufs:.1f} LUFS"
    else:
        loud_text = "测不到"
    if m.true_peak is not None:
        loud_text += f" / 真峰值 {m.true_peak:.2f} dBTP"
    _table(["项", "实测值"], [
        ["文件", rel(path)],
        ["文件体积", human(m.file_size) if m.file_size else "?"],
        ["时长", fmt_sec(m.duration)],
        ["分辨率", (f"{m.width}×{m.height}（显示；编码 {m.coded_width}×{m.coded_height}，"
                    f"旋转 {m.rotation}°）" if m.rotation % 180 == 90
                    else f"{m.width}×{m.height}")],
        ["画幅", f"{m.aspect_label}（{m.aspect:.4f}）"],
        ["帧率", f"{m.fps:g}fps" if m.fps else "?"],
        ["视频编码 / 像素格式", f"{m.codec} / {m.pix_fmt}"],
        ["视频码率", f"{_fmt_bitrate(m.video_bitrate)}（总体 {_fmt_bitrate(m.overall_bitrate)}）"],
        ["音轨", (f"{m.audio_codec} / {m.audio.get('sample_rate', '?')}Hz / "
                 f"{m.audio.get('channels', '?')}声道 / {_fmt_bitrate(m.audio_bitrate)}")
                if m.audio else "无（无声片不能交付）"],
        ["响度", loud_text],
        ["响度闸门", f"{args.lufs_min:g} ~ {args.lufs_max:g} LUFS（本项目母带目标 -14 LUFS；"
                    "AGENTS.md 阶段 8 / 第 7 节，与 check_video.py 同口径）"],
    ], widths=[20, 62])

    # 全局检查：与平台无关、只和「这支文件是不是一支能交付的成片」有关。
    # 平台检查各自计数，最后和全局一起汇总（0 ERROR 才算过）。
    global_checks = [_check_rotation(m, timing), _check_decodable(m, decode_detail)]
    log("")
    log("-" * 78)
    log("全局检查（所有平台共用，先于平台口径）")
    log("-" * 78)
    _table(["判定", "项", "实测", "口径", "口径性质"],
           [[TAG[c.level], c.name, c.value, c.rule, CONF_LABEL.get(c.conf, c.conf)]
            for c in global_checks])
    global_advice = [c for c in global_checks if c.advice and c.level in ("ERROR", "WARN")]
    if global_advice:
        log("")
        log("  怎么改：")
        for c in global_advice:
            log(f"    · [{c.level}] {c.name}：{c.advice}")

    total_errors = sum(1 for c in global_checks if c.level == "ERROR")
    total_warns = sum(1 for c in global_checks if c.level == "WARN")
    per_platform: List[Tuple[str, int, int]] = []
    for key in keys:
        e, w = _print_platform_block(m, key, args.lufs_min, args.lufs_max)
        per_platform.append((PLATFORMS[key]["name"], e, w))
        total_errors += e
        total_warns += w

    log("")
    log("=" * 78)
    log("平台参数口径（AGENTS.md 第 7 节：脚本发现问题就非零退出，不静默通过）")
    log("=" * 78)
    for key in keys:
        p = PLATFORMS[key]
        log("")
        log(f"[{p['name']}]")
        for conf, text, url in p["src"]:
            log(f"  · {CONF_LABEL.get(conf, conf)}")
            log(f"    {text}")
            log(f"    出处：{url}")
        log(f"  一句话建议：{p['advice']}")
    log("")
    log("  说明：")
    for line in CONF_LEGEND:
        log(f"    {line}")

    log("")
    log("=" * 78)
    summary = (f"交付预检：{total_errors} ERROR / {total_warns} WARN"
               f"（{len(keys)} 个平台：" +
               "、".join(f"{name} {e}E/{w}W" for name, e, w in per_platform) + "）")
    log(summary)
    if total_errors:
        # AGENTS.md 第 7 节：ERROR 同时写 stderr，保证 AI 工具能从 stderr 抓到
        sys.stderr.write(summary + "\n")
        log("有 ERROR 的平台先按上面的「怎么改」逐条修，改完重跑本命令；"
            "修完再跑 `python scripts/deliver.py cover` 出封面候选。")
        return 1
    log("全部平台 0 ERROR。下一步：`python scripts/deliver.py cover` 出 3 张封面候选挑一张。")
    return 0


# ===========================================================================
# 子命令 2：cover
# ===========================================================================

_PIL: Any = None


def _pil() -> Any:
    """延迟导入 Pillow；缺了直接 ERROR（封面必须要它）。"""
    global _PIL
    if _PIL is None:
        try:
            from PIL import Image, ImageDraw, ImageFont  # noqa: F401
        except ImportError:
            error("缺少 Pillow，封面生成需要它。请运行 pip install -r requirements.txt"
                  "（模板要求 Pillow>=10）；或先只跑 `python scripts/deliver.py check`。")
        _PIL = (Image, ImageDraw, ImageFont)
    return _PIL


_FONT_CACHE: Dict[Tuple[str, int], Any] = {}


_CMAP_CACHE: Dict[str, Optional[set]] = {}


def _font_cmap(font_path: str) -> Optional[set]:
    """用 fontTools 读字体的 cmap 码点集合；读不了返回 None（调用方自行降级）。"""
    if font_path in _CMAP_CACHE:
        return _CMAP_CACHE[font_path]
    codes: Optional[set] = None
    try:
        from fontTools.ttLib import TTFont  # 模板依赖里已有 fontTools
        with TTFont(font_path, lazy=True) as tt:
            codes = set(tt.getBestCmap().keys())
    except Exception:  # noqa: BLE001 - 没装 fontTools 或字体坏掉都不该中断出图
        codes = None
    _CMAP_CACHE[font_path] = codes
    return codes


def _font_supports(font: Any, font_path: str, text: str) -> Optional[str]:
    """字体缺字时返回第一个缺的字，全都有返回 None。

    **必须查 cmap，不能用 `font.getmask(ch).getbbox()`**：Pillow 12 的 getmask 会
    自行做字体回退（font fallback），字体里根本没有这个字时它照样返回一个借来的字形，
    bbox 非空，于是缺字检查形同虚设——实测 `public/fonts/promo-subset-display.ttf`
    的 cmap 里没有「安」「候」，getmask 却都报有字形，最后封面/联系表上印出一片方块。

    项目自己的 `public/fonts/` 里通常是**子集化**字体（只含本项目用到的字符），
    拿它排一句它没收进去的标题，中文会整片变成方块，而 Pillow 不会报任何错。
    读不到 cmap（没装 fontTools）时才退回 getmask 判断。
    """
    codes = _font_cmap(font_path)
    for ch in text or "":
        if ch.isspace():
            continue
        if codes is not None:
            if ord(ch) not in codes:
                return ch
            continue
        try:  # 降级路径：不可靠，只求聊胜于无
            if font.getmask(ch).getbbox() is None:
                return ch
        except Exception:  # noqa: BLE001 - 个别字符量不了就当它没问题
            continue
    return None


def _load_font(size: int, required_text: str = "") -> Tuple[Any, str]:
    """按 fonts.json 的 check.systemCandidates 找中文字体；一个都没有 -> ERROR。

    ``required_text`` 里出现的字，字体必须都有对应的字形（子集字体会缺字，
    所以会顺着候选清单往下换一个能排全的）。候选全部缺字时退回第一个可用字体
    并 WARN（宁可方块字 + 明确警告，也不要整个命令什么都不产出）。

    与 check_video.py 的差别：那里找不到字体只会退回 Pillow 内置位图字体（标签变方块
    还能忍），但封面上的品牌名和标题是**要发出去的正片物料**，方块字绝不能交付，
    所以这里直接 ERROR 并给修复提示。
    """
    _, _, ImageFont = _pil()
    cache_key = (str(required_text), size)
    if cache_key in _FONT_CACHE:
        return _FONT_CACHE[cache_key]

    tried = []
    first_usable: Optional[Tuple[Any, str]] = None
    missing_notes: List[str] = []
    for candidate in system_font_candidates():
        tried.append(str(candidate))
        p = Path(str(candidate)).expanduser()
        if not p.is_file():
            continue
        try:
            font = ImageFont.truetype(str(p), size, index=0)
        except Exception:  # noqa: BLE001 - 单个候选坏掉不该中断
            continue
        if first_usable is None:
            first_usable = (font, str(p))
        miss = _font_supports(font, str(p), required_text)
        if miss is None:
            _FONT_CACHE[cache_key] = (font, str(p))
            return font, str(p)
        missing_notes.append(f"{p.name} 缺字「{miss}」")

    if first_usable is not None:
        if missing_notes:
            warn("字体缺字，但候选里没有能排全的：" + "；".join(missing_notes)
                 + f"。仍用 {Path(first_usable[1]).name} 出图，"
                   "**缺字的封面不要直接交付**——重跑一次 cover 时把 --title 改短，"
                   "或给 fonts.json 的 check.systemCandidates 加一个全字库中文字体"
                   "（Windows: C:/Windows/Fonts/msyh.ttc）")
        _FONT_CACHE[cache_key] = first_usable
        return first_usable

    error(
        "找不到任何可用的中文字体，封面上的中文会变成方块，不能这样交付。\n"
        "试过的路径（来自 fonts.json 的 check.systemCandidates）：\n  "
        + "\n  ".join(tried)
        + "\n修复方式：\n"
        "  · Windows：系统自带微软雅黑，确认 C:\\Windows\\Fonts\\msyh.ttc 存在；"
        "若不存在，装一个 Noto Sans CJK 或把 fonts.json 里的 systemCandidates 改成真实路径\n"
        "  · macOS：系统自带苹方（/System/Library/Fonts/PingFang.ttc），确认该文件存在\n"
        "  · Linux：sudo apt install fonts-noto-cjk（或 fonts-wqy-microhei）\n"
        "  · 也可以把字体文件放进 public/fonts/ 并把路径写进 fonts.json 的 check.systemCandidates 首位"
    )
    raise SystemExit(1)  # pragma: no cover - error() 已经退出


def _split_caption(text: str, max_chars: int) -> List[str]:
    """按最大字符数折行（中文按字计，英文单词尽量不拆开）。

    规则来源：AGENTS.md 阶段 7「竖屏单行 ≤13 字、最多 2 行」+ timing.json 的
    ``captions.maxChars``（默认 13）。这里的 ``max_chars`` 是**每个平台/每个项目的**
    上限，封面沿用同一个数字，保证封面和片内字幕的口径一致。
    """
    text = re.sub(r"\s+", " ", str(text or "")).strip()
    if not text:
        return []
    max_chars = max(2, int(max_chars))
    lines: List[str] = []
    current = ""
    for token in re.findall(r"[A-Za-z0-9][A-Za-z0-9._:/+-]*|\s+|.", text):
        if token.isspace():
            if current and not current.endswith(" "):
                current += " "
            continue
        # 拉丁词整体不拆（除非本身超长）
        if _dwidth(token) > 2 and len(token) > 1 and current and len(current.rstrip()) + len(token) > max_chars:
            lines.append(current.rstrip())
            current = token
            continue
        for ch in token:
            if len(current) >= max_chars:
                lines.append(current.rstrip())
                current = ""
            current += ch
    if current.strip():
        lines.append(current.rstrip())
    return [ln for ln in lines if ln]


def _text_bbox(draw: Any, text: str, font: Any) -> Tuple[int, int, int, int]:
    """量一行文字的像素 bbox（Pillow 的 textbbox 含 ascender/descender 空白，取实测）。"""
    left, top, right, bottom = draw.textbbox((0, 0), text, font=font)
    return int(left), int(top), int(right), int(bottom)


def _safe_rect(w: int, h: int) -> Tuple[float, float, float, float]:
    """安全区左右上下边界（px）。来源：AGENTS.md 第 4 节。"""
    return (w * SAFE_LEFT, h * SAFE_TOP, w * (1.0 - SAFE_RIGHT), h * (1.0 - SAFE_BOTTOM))


def _ink_bbox(layer: Any, threshold: int = 40) -> Optional[Tuple[int, int, int, int]]:
    """量一张 RGBA 文字图层里**真实有色像素**的 bbox；全透明返回 None。

    用 numpy 扫 alpha 通道（numpy 是模板既有依赖，见 requirements.txt）。
    比 Pillow 的 textbbox 准：textbbox 量的是「行盒」，既漏掉 stroke_width 的描边
    外溢，也把 ascender/descender 的空白算进高度。
    """
    import numpy as np

    alpha = np.asarray(layer.getchannel("A"))
    ys, xs = np.nonzero(alpha > threshold)
    if xs.size == 0:
        return None
    return int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1


def inside_rect(bbox: Sequence[float], rect: Sequence[float]) -> bool:
    """bbox 是否完整落在 rect 内（用于安全区判定）。"""
    return (bbox[0] >= rect[0] and bbox[1] >= rect[1]
            and bbox[2] <= rect[2] and bbox[3] <= rect[3])


def _text_clash_stats(base: Any, box: Sequence[float],
                      downscale: int = CLASH_DOWNSCALE) -> Dict[str, float]:
    """估一下某个矩形区域里「成片自己已经有亮色文字」的强度，并给出原始数字。

    做法：把区域降采样成灰度，数接近纯白的亮像素（>232）占比——
    这是**唯一的判定信号**（`ratio` = 亮像素% / CLASH_BRIGHT_PCT，≥1.0 视为有字）。
    旁边再量一个水平相邻突变 >70 的比例（`edge`），只用来把结论分成「确认有字」
    和「疑似有字」：UI 边框、渐变、手机模型的高光都会产生突变，所以突变不能当判据
    ——实测一个只有手机模型、一个字都没有的干净区域，突变高达 5.53%。

    阈值用模板示例成片实测标定（3 帧 × 3 个位置 = 9 个样本）：
    真有字 亮像素 5.31~15.40%，干净区域 亮像素 0.00% —— 4% 这条线把两类完全分开。
    **这是启发式估计，不是 OCR**，只用来给一句「换个位置」的建议，不作通过/失败判据。

    实际用它解决的问题：成片的字幕按规范就落在距底 18% 处（在安全区内），
    bottom 模式的封面文字正好会压上去，两层字谁也读不清（已实测 out/covers/cover-1.png）。
    """
    import numpy as np

    Image, _, _ = _pil()
    w, h = base.size
    x0 = max(0, int(box[0]))
    x1 = min(w, int(math.ceil(box[2])))
    y0 = max(0, int(box[1]))
    y1 = min(h, int(math.ceil(box[3])))
    if x1 - x0 < 8 or y1 - y0 < 8:
        return {"bright": 0.0, "edge": 0.0, "ratio": 0.0, "firm": False}
    small = base.crop((x0, y0, x1, y1)).convert("L")
    if downscale > 1:
        small = small.resize((max(1, small.width // downscale),
                              max(1, small.height // downscale)), Image.BILINEAR)
    gray = np.asarray(small, dtype=np.int16)
    bright = float((gray > 232).mean()) * 100.0
    edge = float((np.abs(np.diff(gray, axis=1)) > 70).mean()) * 100.0
    return {"bright": bright, "edge": edge, "ratio": bright / CLASH_BRIGHT_PCT,
            "firm": bright >= CLASH_BRIGHT_PCT and edge >= CLASH_EDGE_PCT}


def _layout_cover(title: str, brand: str, max_chars: int,
                  position: str = "bottom", max_lines: int = CAPTION_MAX_LINES) -> Dict[str, Any]:
    """求一个「标题 + 品牌名」的排版，使整体 bbox 落进安全区。

    做法：从最大字号往下试，每一档都真的量一遍 Pillow bbox（不估算），
    第一个满足条件的字号就是答案。

    ``position`` 决定文字块贴安全区的哪一边：

    * ``bottom``（默认）：贴安全区底边往上排。成片底部通常有字幕，压暗层也在这儿。
    * ``middle``：垂直居中在幅面 56% 处。**成片自己已经有字幕/大字时用这个**，
      否则封面文字会正好压在画面里的大字上，两个都读不清。
    * ``top``：贴安全区上边往下排。画面下方内容重要时用。

    三种模式都严格受安全区（上 10% / 下 18% / 左 6% / 右 14%）约束。

    ``max_lines`` 是硬上限（来自 timing.json 的 captions.maxLines，AGENTS.md 阶段 7
    规定竖屏字幕最多 2 行）：超出的部分合并到末行并加省略号，同时返回 truncated=True，
    由调用方 WARN 出来——**宁可少放几个字，也不要违反字幕规范或压到画面下方的 CTA**。
    """
    Image, ImageDraw, _ = _pil()
    safe_l, safe_t, safe_r, safe_b = _safe_rect(COVER_WIDTH, COVER_HEIGHT)
    # 描边会在字形左右各外溢最多 COVER_STROKE_PX，可用宽度里先把这部分扣掉，
    # 否则「宽度刚好等于安全区宽」的标题，描边会溢出安全区（实测左溢 2px）。
    avail_w = (safe_r - safe_l) - 2 * COVER_STROKE_PX
    avail_h = safe_b - safe_t

    probe = Image.new("L", (8, 8))
    pdraw = ImageDraw.Draw(probe)

    # 缺字检查要用的字符集：只跟文案有关，跟字号无关，算一次就够。
    # 项目自己的 public/fonts/ 里常有子集化字体，缺字会被 Pillow 静默画成方块。
    required_text = (title or "") + (brand or "")

    # 行长上限只跟字数有关（跟字号无关），先夹一次；折出来的行数当场确定。
    lines = _split_caption(title, max_chars)
    if not lines:
        lines = [""]
    truncated = False
    if max_lines >= 1 and len(lines) > max_lines:
        head = lines[: max_lines - 1]
        tail = "".join(lines[max_lines - 1:])
        lines = head + [_dtrim(tail, max_chars)]
        truncated = True

    attempts: List[Dict[str, Any]] = []
    for size in range(COVER_TITLE_SIZE_MAX, COVER_TITLE_SIZE_MIN - 1, -COVER_TITLE_SIZE_STEP):
        title_font, font_path = _load_font(size, required_text)
        line_h = max(1, int(round(size * COVER_LINE_SPACING)))
        widths = []
        for line in lines:
            l, _t, r, _b = _text_bbox(pdraw, line, title_font)
            widths.append(r - l)
        text_w = max(widths) if widths else 0
        text_h = line_h * len(lines) if lines else 0

        brand_size = max(18, int(round(size * COVER_BRAND_RATIO)))
        brand_font, _ = _load_font(brand_size, required_text)
        bl, bt, br, bb = _text_bbox(pdraw, brand, brand_font) if brand else (0, 0, 0, 0)
        brand_w, brand_h = (br - bl), (bb - bt)
        gap = int(round(size * 0.34)) if brand else 0

        total_h = brand_h + gap + text_h
        total_w = max(text_w, brand_w)

        if position == "top":
            top = safe_t + COVER_MARGIN_PX
            fits_vertically = top + total_h <= safe_b
        elif position == "middle":
            top = COVER_HEIGHT * COVER_MIDDLE_ANCHOR - total_h / 2.0
            fits_vertically = (top >= safe_t) and (top + total_h <= safe_b)
        else:  # bottom
            top = safe_b - COVER_MARGIN_PX - total_h
            fits_vertically = top >= safe_t

        ok = (total_w <= avail_w) and fits_vertically and total_h <= avail_h
        attempts.append({
            "size": size, "lines": lines, "title_font": title_font, "font_path": font_path,
            "line_h": line_h, "text_w": text_w, "text_h": text_h,
            "brand_size": brand_size, "brand_font": brand_font,
            "brand_w": brand_w, "brand_h": brand_h, "gap": gap,
            "rect": (safe_l, top, safe_l + total_w, top + total_h),
            "block_top": top, "ok": ok, "total_h": total_h,
        })
        if ok and position in ("top", "middle"):
            break  # 这两种模式字号越大越容易上下越界，第一个能装下的就是最大可用字号

    chosen = next((a for a in attempts if a["ok"]), None)
    if chosen is None:
        chosen = attempts[-1] if attempts else None
        if chosen is None:
            error("封面排版失败：字号搜索范围为空，请检查 COVER_TITLE_SIZE_MAX/MIN 常量。")
    return {"chosen": chosen, "attempts": attempts, "position": position,
            "safe": (safe_l, safe_t, safe_r, safe_b),
            # 三种位置在这个字号下的文字块顶边，供撞字探测比较。
            # **三个值必须各算各的**：不能写 chosen["block_top"]——那只有在
            # --position bottom 时才等于 bottom 的顶边；跑 --position middle 时
            # chosen["block_top"] 是 middle 的值，再赋给 "bottom" 会让 bottom 的
            # 撞字探测错量到 middle 的带子（实测踩过：bottom 报出与 middle 完全相同的亮像素）。
            "block_tops": {"top": safe_t + COVER_MARGIN_PX,
                           "middle": COVER_HEIGHT * COVER_MIDDLE_ANCHOR - chosen["total_h"] / 2.0,
                           "bottom": safe_b - COVER_MARGIN_PX - chosen["total_h"]},
            "truncated": truncated, "lines_full": _split_caption(title, max_chars)}


def _cover_line_box(position: str, layout: Dict[str, Any]) -> Tuple[float, float, float, float]:
    """某个 --position 下**标题那几行字**实际占的窄带（撞字探测专用）。

    只取标题的宽度与高度，不含品牌名、不含行距，也不把矩形拉到安全区右边。
    这样量到的亮像素才真的来自「压在标题底下的画面」——如果把矩形拉到安全区右边，
    画面两侧的手机模型、UI 卡片都会被算进来，实测就是这么产生误报的。
    """
    chosen = layout["chosen"]
    safe_l, _safe_t, _safe_r, _safe_b = layout["safe"]
    block_top = layout["block_tops"].get(position, chosen["block_top"])
    line_top = block_top + chosen["brand_h"] + chosen["gap"]
    return (safe_l, line_top, safe_l + chosen["text_w"], line_top + chosen["text_h"])


def _draw_scrim(image: Any, text_top: int) -> Any:
    """从文字块顶边往下压一层渐变暗化，保证文字对比度（对照校验清单 B3）。

    压暗区域**跟着文字块走**而不是固定压底部 52%：--position middle / top 时文字
    浮在画面中部，如果只压底部，文字就会落在没压暗的亮区上，白字直接糊掉。
    """
    Image, _, _ = _pil()
    w, h = image.size
    top = max(0, min(int(text_top) - int(h * 0.03), h - 1))
    band = h - top
    gradient = Image.new("L", (1, band))
    for y in range(band):
        # 从文字块顶边往下：先快速上来，再保持；越靠底越强，让底部 CTA 区域更沉
        t = y / max(1, band - 1)
        alpha = COVER_SCRIM_ALPHA * (0.45 + 0.55 * (t ** 0.7))
        gradient.putpixel((0, y), int(max(0, min(255, alpha))))
    mask = gradient.resize((w, band), Image.LANCZOS)
    black = Image.new("RGB", (w, band), (0, 0, 0))
    region = Image.composite(black, image.crop((0, top, w, h)), mask)
    image.paste(region, (0, top))
    return image


def _extract_frame(mp4: Path, second: float, out_path: Path) -> None:
    """用 ffmpeg 抽一帧并缩到封面尺寸（1080×1920，cover 裁切保证不变形）。"""
    import subprocess
    ensure_dir(out_path.parent)
    vf = (f"scale={COVER_WIDTH}:{COVER_HEIGHT}:force_original_aspect_ratio=increase,"
          f"crop={COVER_WIDTH}:{COVER_HEIGHT}")
    # 注意：中间帧必须写 PNG。JPEG 有损编码会在高对比边缘产生亮色 halo，
    # 让「撞字探测」量到的亮像素从 0.00% 虚高到 2%，直接造成误报（实测过）。
    proc = subprocess.run(
        ["ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "error",
         "-ss", f"{second:.3f}", "-i", str(mp4),
         "-vf", vf, "-frames:v", "1", "-y", str(out_path)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=FFMPEG_TIMEOUT_S,
    )
    if not out_path.is_file() or out_path.stat().st_size == 0:
        detail = proc.stderr.decode("utf-8", errors="replace").strip()
        error(f"抽帧失败（{second:.2f}s）：{detail[:400] or 'ffmpeg 没输出文件'}。"
              "请确认该时刻小于片长，且 mp4 可解码。")


def _build_sheet(covers: Sequence[Tuple[Path, str]], out_path: Path) -> Tuple[int, int]:
    """拼联系表（复用 media.py sheet 的思路：等高行、每张缩到固定宽、下面写标签）。"""
    Image, ImageDraw, _ = _pil()
    # required_text 必须把**这张图上会出现的中文**都列全，否则会挑到缺字的子集字体，
    # 联系表上印出一排方块（标题/标签/表头三者用的字都要覆盖）。
    font, _note = _load_font(max(16, int(SHEET_THUMB_WIDTH * 0.055)),
                             "封面候选 张 挑一上传 或重跑 换时间点 文件 时刻 体积 尺寸 "
                             "字号 行数 安全区内 越界 联系表"
                             "0123456789.xsKB·—（）/")
    pad = 14
    header_h = 56
    label_h = 76
    thumbs = []
    for path, label in covers:
        with Image.open(path) as im:
            im = im.convert("RGB")
            thumb = im.copy()
            thumb.thumbnail((SHEET_THUMB_WIDTH, 10 ** 6), Image.LANCZOS)
            thumbs.append((thumb, label))
    if not thumbs:
        error("没有可拼进联系表的封面。")
    cols = max(1, min(SHEET_COLS, len(thumbs)))
    rows = (len(thumbs) + cols - 1) // cols
    cell_w = SHEET_THUMB_WIDTH
    cell_h = max(t.height for t, _ in thumbs) + label_h
    width = pad + cols * (cell_w + pad)
    height = header_h + pad + rows * (cell_h + pad)
    sheet = Image.new("RGB", (width, height), SHEET_BG)
    draw = ImageDraw.Draw(sheet)
    draw.text((pad + 4, 16), f"封面候选 —— {len(thumbs)} 张，挑一张上传（或重跑 --frames 换时间点）",
              fill=SHEET_FG, font=font)
    for index, (thumb, label) in enumerate(thumbs):
        row, col = divmod(index, cols)
        x = pad + col * (cell_w + pad)
        y = header_h + pad + row * (cell_h + pad)
        sheet.paste(thumb, (x, y))
        draw.rectangle([x, y, x + thumb.width - 1, y + thumb.height - 1], outline=SHEET_BORDER)
        for i, line in enumerate(label.splitlines()[:2]):
            draw.text((x + 2, y + thumb.height + 8 + i * int(font.size * 1.35)),
                      line, fill=SHEET_FG if i == 0 else SHEET_DIM, font=font)
    ensure_dir(out_path.parent)
    sheet.save(out_path, format="JPEG", quality=SHEET_QUALITY, optimize=True, progressive=True)
    return sheet.width, sheet.height


def _parse_frames(raw: Optional[str], duration: float) -> List[float]:
    """解析 --frames；给默认值时按片长夹取，保证不会抽到片子外面。

    显式传的时刻如果超出片长，同样会 WARN 并夹回片内——ffmpeg 对越界 seek 只会抛一句
    和真实原因毫无关系的 mjpeg 报错（''Non full-range YUV is non-standard''），
    在脚本这一层就拦下来，用户才知道自己填错了数字。
    """
    def _clamp(value: float) -> float:
        if duration <= 0 or value < duration - 0.05:
            return value
        # 片尾最后一帧附近 seek 容易什么都取不到，留 0.1s 余量
        fixed = max(0.0, duration - 0.1)
        warn(f"成片只有 {duration:.2f}s，{value:.2f}s 超出片长，自动改到 {fixed:.2f}s"
             f"（要精确控制请显式传 --frames）")
        return round(fixed, 2)

    if raw:
        items = [s.strip() for s in str(raw).replace("，", ",").split(",") if s.strip()]
        if not items:
            error("--frames 是空的。写法：--frames 3.6,21.7,43.2")
        out = []
        for item in items:
            try:
                value = float(item)
            except ValueError:
                error(f"--frames 里不是数字：{item!r}。写法：--frames 3.6,21.7,43.2")
            if value < 0:
                error(f"--frames 不能是负数：{item!r}")
            out.append(_clamp(value))
        return out

    out = []
    for second in DEFAULT_COVER_FRAMES:
        if duration > 0 and second >= duration:
            # 片长短于默认时刻时按比例缩到片内（并留 0.1s 余量）
            scaled = max(0.0, duration * (second / 45.0) - 0.1)
            warn(f"成片只有 {duration:.2f}s，默认封面时刻 {second}s 超出片长，"
                 f"自动改到 {scaled:.2f}s（要精确控制请显式传 --frames）")
            out.append(round(scaled, 2))
        else:
            out.append(second)
    return out


def _brand_accent_rgb(brand_cfg: Dict[str, Any]) -> Tuple[int, int, int]:
    """取 ``timing.json`` 里品牌强调色 ``brand.colors.accent`` 的 RGB。

    模板把品牌色写进 timing.json（``#33E1A1`` 是默认值），封面上的品牌名应该跟着它走，
    而不是在脚本里硬编码一个绿色——换了配色（例如 #FF6B35）的片子，封面品牌名还是绿的。
    允许 ``#RRGGBB`` / ``RRGGBB``，大小写不敏感；缺失或写错时回退 ``DEFAULT_BRAND_ACCENT``。
    """
    colors = brand_cfg.get("colors") if isinstance(brand_cfg.get("colors"), dict) else {}
    raw = str(colors.get("accent") or "").strip()
    match = re.fullmatch(r"#?([0-9a-fA-F]{6})", raw)
    if not match:
        return DEFAULT_BRAND_ACCENT
    hex6 = match.group(1)
    return (int(hex6[0:2], 16), int(hex6[2:4], 16), int(hex6[4:6], 16))


def _cover_out_dir(value: str) -> Path:
    """``--out`` 预检：已存在但不是目录时给干净 ``ERROR: ...``，而不是抛 FileExistsError traceback。"""
    path = Path(value)
    if path.exists() and not path.is_dir():
        error(f"--out {value} 已存在且不是目录（是个文件）。换一个 --out，或先把它删掉/改名。")
    return ensure_dir(path)


#: 封面候选的文件名模式（本功能自己的固定产物）。
COVER_CANDIDATE_RE = re.compile(r"^cover-\d+\.png$")

#: 中间帧的文件名模式：``_<mp4 文件名>-frame-<序号>.png``。
#: 匹配**任意** stem：同一个 --out 目录换一支成片重跑时，上一支片子的中间帧也要算本功能产物，
#: 否则旧 stem 的 ``_旧片-frame-1.png`` 会一直堆在目录里（R6 实测换片后残留旧帧）。
COVER_FRAME_RE = re.compile(r"^_.+-frame-\d+\.png$")


def _clean_stale_cover_outputs(out_dir: Path, keep=()) -> List[Path]:
    """删掉输出目录里上一轮留下的本功能产物，``keep`` 里的文件名（本轮刚写的帧）不删。

    为什么需要：``--frames`` 变少时旧的 ``cover-2.png`` / ``cover-3.png`` 不会被本轮覆盖；
    换过标题时它们还是旧文案，换过片子时旧 ``_旧片-frame-*.png`` 也会残留。它们留在目录里
    会被误当成本轮候选（日志与联系表只反映本轮）。``_sheet.jpg`` 同样清掉——它是上一轮的
    联系表，本轮会重建；留着会让「候选已更新、联系表还是旧图」自相矛盾。

    只匹配本功能生成的文件名（固定 ``cover-N.png`` / ``_*-frame-N.png`` / ``_sheet.jpg``），
    用户自己放进目录的其它文件一律不动。
    """
    keep_names = {str(name) for name in keep}
    removed: List[Path] = []
    for path in sorted(out_dir.iterdir()):
        if not path.is_file() or path.name in keep_names:
            continue
        if (COVER_CANDIDATE_RE.match(path.name)
                or COVER_FRAME_RE.match(path.name)
                or path.name == SHEET_NAME):
            try:
                path.unlink()
                removed.append(path)
            except OSError as exc:
                warn(f"清理上一轮残留 {path.name} 失败：{exc}")
    return removed


def cmd_cover(args: argparse.Namespace) -> int:
    _pil()
    if not has_cmd("ffmpeg"):
        error("缺少 ffmpeg（AGENTS.md 第 6 节的硬依赖）。"
              "Windows: winget install Gyan.FFmpeg；macOS: brew install ffmpeg；"
              "Linux: sudo apt install ffmpeg。")

    mp4 = _resolve_project_path(args.video)
    if not mp4.is_file():
        error(f"找不到成片 {args.video}。先跑 `npm run render:1080`（输出 out/final-1080p.mp4），"
              "或把正确路径作为第一个参数传进来。")

    timing = load_timing()
    captions = timing.get("captions") if isinstance(timing.get("captions"), dict) else {}
    try:
        max_chars = int(captions.get("maxChars") or 13)
    except (TypeError, ValueError):
        max_chars = 13
    brand_cfg = timing.get("brand") if isinstance(timing.get("brand"), dict) else {}
    brand = str(brand_cfg.get("name") or "").strip()
    accent = _brand_accent_rgb(brand_cfg)
    title = (args.title if args.title else str(timing.get("title") or "")).strip()
    if not title:
        error("没有标题可排版：timing.json 里没有 title，也没有传 --title。"
              "例：python scripts/deliver.py cover --title \"小账本 · 示例片\"")
    try:
        max_lines = int(captions.get("maxLines") or CAPTION_MAX_LINES)
    except (TypeError, ValueError):
        max_lines = CAPTION_MAX_LINES
    max_lines = max(1, max_lines)

    m = Media(mp4)
    seconds = _parse_frames(args.frames, m.duration)
    base_name = mp4.stem
    out_dir = _cover_out_dir(args.out)

    # 排版跟具体哪一帧无关，先算一次：三张封面字号一致，也不会重复报字体缺字警告
    layout = _layout_cover(title, brand, max_chars, args.position, max_lines)
    chosen = layout["chosen"]
    safe_l, safe_t, safe_r, safe_b = layout["safe"]
    safe_box = (safe_l, safe_t, safe_r, safe_b)
    if layout["truncated"]:
        warn(f"标题按 {max_chars} 字折行后超过 captions.maxLines={max_lines} 行上限"
             f"（原本 {len(layout['lines_full'])} 行），已把多余的字并到末行并截断为「"
             f"{' | '.join(chosen['lines'])}」。"
             "封面标题要短：用 --title 直接给一句短的，或改 timing.json 的 title")

    log(f"封面候选：{rel(mp4)} → {rel(out_dir)}/")
    log(f"品牌名：{brand or '（timing.json 的 brand.name 为空，封面只放标题）'}")
    log(f"品牌色：#{accent[0]:02X}{accent[1]:02X}{accent[2]:02X}"
        "（timing.json 的 brand.colors.accent；缺失时回退 #33E1A1）")
    log(f"标题：{title}（按 captions.maxChars={max_chars} 折行，最多 {max_lines} 行）")
    log(f"文字位置：{args.position}"
        + {"bottom": "（贴安全区底边，成片底部字幕会被压暗层盖住）",
           "middle": f"（垂直居中在 {COVER_MIDDLE_ANCHOR:.0%} 处，避开成片自家的大字/字幕）",
           "top": "（贴安全区上边）"}[args.position])
    log(f"安全区：上 {SAFE_TOP:.0%} / 下 {SAFE_BOTTOM:.0%} / 左 {SAFE_LEFT:.0%} / 右 {SAFE_RIGHT:.0%}"
        f"（AGENTS.md 第 4 节；右侧留给平台互动按钮）")

    # 两阶段：先把本轮所有时间点抽完帧，再清理上一轮残留。
    # 为什么不能先清：抽帧是 cover 最常见的硬失败（成片损坏 / 时刻解不出画面），
    # 先删旧候选会让「上一轮已经验证过的好封面」在失败重跑时被删光（R6 实测：坏 mp4 重跑
    # 把 cover-1..3.png 删了、旧 _sheet.jpg 还留着，自相矛盾）。
    if not seconds:
        error("没有可用的抽帧时刻（--frames 里没有任何有效秒数）。请显式传至少一个时刻，例如 "
              "--frames 3.6,21.7,43.2")
    frames: List[Tuple[int, float, Path]] = []
    for index, second in enumerate(seconds, start=1):
        frame_path = out_dir / f"_{base_name}-frame-{index}.png"
        _extract_frame(mp4, second, frame_path)
        frames.append((index, second, frame_path))

    # 抽帧全部成功后，才清掉上一轮本功能固定命名的残留（旧候选 / 旧中间帧 / 旧联系表）；
    # 本轮刚写的帧用 keep 保护，用户自己放进目录的其它文件一律不动。
    stale = _clean_stale_cover_outputs(out_dir, keep={path.name for _index, _second, path in frames})
    if stale:
        log(f"已清理上一轮残留：{len(stale)} 个文件（"
            + "、".join(path.name for path in stale[:6]) + ("…" if len(stale) > 6 else "") + "）")
    log("")

    results: List[Dict[str, Any]] = []
    failed = 0
    for index, second, frame_path in frames:
        cover_path = out_dir / f"cover-{index}.png"

        Image, ImageDraw, _ = _pil()
        with Image.open(frame_path) as src:
            base = src.convert("RGB").resize((COVER_WIDTH, COVER_HEIGHT), Image.LANCZOS)

        # 撞字探测：成片自己在这个位置已经有亮色文字吗？（脚本没法自动避让，只能提示）
        # 量的是 `_cover_line_box`：**只覆盖标题那几行字**的窄带，不是整个文字块。
        # 为什么不用整个块：块从安全区左边一直拉到安全区右边，比字宽得多，
        # 会把画面两侧的无关内容（手机模型、UI 卡片）算进来，实测就是它造成误报。
        # 必须在压暗层之前量：压暗层会降低亮像素占比，先压暗再量等于自己把误报压下去，
        # 量到的就不是「成片原本有没有字」。
        others = [pos for pos in ("bottom", "middle", "top") if pos != args.position]
        clash_stats = _text_clash_stats(base, _cover_line_box(args.position, layout))
        stats_others = {pos: _text_clash_stats(base, _cover_line_box(pos, layout))
                        for pos in others}
        clash_stats["others"] = stats_others
        clash_here = clash_stats["ratio"]
        clash_others = {pos: stats_others[pos]["ratio"] for pos in others}
        cleanest = min(clash_others, key=lambda k: (clash_others[k], others.index(k)))

        # 压暗层从文字块顶边开始，保证白字无论落在画面哪个高度都有对比度
        base = _draw_scrim(base, chosen["block_top"])

        # 文字块整体从 block_top 开始：品牌名在上，标题在下面几行
        block_top = chosen["block_top"]
        title_top = block_top + chosen["brand_h"] + chosen["gap"]

        # 文字画在一张独立图层上，然后**按真实像素**量 bbox，再贴回底图。
        # 为什么不直接 draw.text 到底图后估算：估算会漏掉两件事——
        #   ① stroke_width 的描边往左/右各溢出一部分；② 每行字在行盒里的实际位置
        #     随字形而上浮（数字/拉丁字母的 ascender 不等于汉字顶边）。
        # 交付判定必须拿真实像素说话（AGENTS.md 总原则 2：一切审美判断落成可测量的数字）。
        layer = Image.new("RGBA", (COVER_WIDTH, COVER_HEIGHT), (0, 0, 0, 0))
        ldraw = ImageDraw.Draw(layer)

        y = title_top
        for line in chosen["lines"]:
            l, t, _r, _b = _text_bbox(ldraw, line, chosen["title_font"])
            ldraw.text((safe_l + COVER_STROKE_PX - l, y - t), line, font=chosen["title_font"],
                       fill=(255, 255, 255, 255), stroke_width=COVER_STROKE_PX,
                       stroke_fill=(0, 0, 0, 255))
            y += chosen["line_h"]
        if brand:
            ldraw.text((safe_l + COVER_STROKE_PX, block_top), brand, font=chosen["brand_font"],
                       fill=accent + (255,), stroke_width=max(2, COVER_STROKE_PX - 2),
                       stroke_fill=(0, 0, 0, 255))

        ink = _ink_bbox(layer)
        if ink is None:
            error(f"封面 {index}：文字图层是空的（标题「{title}」一个字都没画出来）。"
                  "检查 timing.json 的 title 是否为空。")
        # 描边的外溢量由 Pillow 内部决定，量不出来就按真实像素对齐：
        # 把整层上下平移到「墨迹顶边正好落在 block_top」，于是左右上下都精确可判定。
        layer = layer.crop((0, ink[1], COVER_WIDTH, ink[3]))
        if layer.height > chosen["total_h"] + 2:
            warn(f"封面 {index} 文字墨迹高 {layer.height}px > 排版预估 {chosen['total_h']}px"
                 "（描边外溢比预想的多），已按真实高度贴图并重新判定安全区")
        base.paste(layer, (0, int(block_top)), layer)
        bbox = (int(ink[0]), int(block_top), int(ink[2]), int(block_top) + layer.height)

        inside = inside_rect(bbox, safe_box)
        if not inside:
            failed += 1

        # 撞字探测已在压暗层之前完成（见上），这里直接用结果
        cover_path.parent.mkdir(parents=True, exist_ok=True)
        base.save(cover_path, format="PNG", optimize=True)

        results.append({
            "index": index, "second": second, "cover": cover_path, "frame": frame_path,
            "size": chosen["size"], "lines": chosen["lines"], "bbox": bbox,
            "clash": clash_here, "clash_others": clash_others, "cleanest": cleanest,
            "safe": (safe_l, safe_t, safe_r, safe_b), "inside": inside,
            "attempts": len(layout["attempts"]), "total_h": chosen["total_h"],
        })

        log(f"[封面 {index}] {second:.2f}s（{second / max(m.duration, 0.01) * 100:.0f}% 处）"
            f" → {rel(cover_path)}")
        log(f"  底图：{rel(frame_path)}（{COVER_WIDTH}×{COVER_HEIGHT}，从成片抽帧 + 文字块上方起渐变压暗）")
        log(f"  字号：标题 {chosen['size']}px / 品牌名 {chosen['brand_size']}px"
            f"（从 {COVER_TITLE_SIZE_MAX}px 往下试了 {len(layout['attempts'])} 档）")
        log(f"  位置：{args.position}（文字块顶边 y={chosen['block_top']:.0f}px）")
        log(f"  折行：{len(chosen['lines'])} 行 —— " + " | ".join(chosen["lines"]))
        log(f"  文字 bbox：[{bbox[0]}, {bbox[1]}, {bbox[2]}, {bbox[3]}]"
            f"（宽 {bbox[2] - bbox[0]}px，高 {bbox[3] - bbox[1]}px，含描边，按真实像素量的）")
        log(f"  安全区：[{safe_l:.0f}, {safe_t:.0f}, {safe_r:.0f}, {safe_b:.0f}]"
            f"（上 {safe_t:.0f}px / 下 {COVER_HEIGHT - safe_b:.0f}px / "
            f"左 {safe_l:.0f}px / 右 {COVER_WIDTH - safe_r:.0f}px）")
        if inside:
            log(f"  安全区判定：OK —— 文字完整落在安全区内"
                f"（上留 {bbox[1] - safe_t:.0f}px，下留 {safe_b - bbox[3]:.0f}px，"
                f"左留 {bbox[0] - safe_l:.0f}px，右留 {safe_r - bbox[2]:.0f}px）")
        else:
            log(f"  安全区判定：ERROR —— 文字越出安全区！"
                f"（左 {bbox[0] - safe_l:+.0f} / 上 {bbox[1] - safe_t:+.0f} / "
                f"右 {bbox[2] - safe_r:+.0f} / 下 {bbox[3] - safe_b:+.0f}）")
            log(f"    怎么改：标题太长，按 AGENTS.md 阶段 7「先删字再改排版」；"
                f"当前标题 {len(title)} 字、按 {max_chars} 字折成 {len(chosen['lines'])} 行。"
                f"把 --title 改短，或调小 timing.json 的 captions.maxChars 后再跑")
        if clash_here >= 1.0:
            firm = "确认有字" if clash_stats["firm"] else "疑似有字"
            log(f"  撞字提醒：这个位置（{args.position}）成片自己已经有亮色文字，"
                f"两层字会叠在一起"
                f"（{firm}：亮像素 {clash_stats['bright']:.2f}% ≥ {CLASH_BRIGHT_PCT:g}%，"
                f"水平突变 {clash_stats['edge']:.2f}%——启发式估计，非 OCR）")
            log(f"    另外两个位置的亮像素占比："
                + "，".join(f"{pos} {clash_stats['others'][pos]['bright']:.2f}%"
                            for pos in others)
                + f" → 最干净的是 --position {cleanest}")
            if clash_others[cleanest] < clash_here:
                log(f"    试试：python scripts/deliver.py cover --position {cleanest} "
                    f"--frames {second:.1f} --out {rel(out_dir)}")
            else:
                log(f"    三个位置都有字：换时刻 —— python scripts/deliver.py cover "
                    f"--frames {second:.1f} 换成该场景里自家文字较少的瞬间")
        log("")

    if not results:
        error("一张封面都没生成，请检查 --frames 与成片路径。")

    thumbs = [(r["cover"], f"cover-{r['index']}.png  {r['second']:.2f}s\n"
                           f"字号 {r['size']}px / {len(r['lines'])} 行 / "
                           f"{'安全区内' if r['inside'] else '越界'}")
               for r in results]
    sheet_path = out_dir / SHEET_NAME
    sheet_w, sheet_h = _build_sheet(thumbs, sheet_path)

    log("-" * 78)
    log("封面清单")
    log("-" * 78)
    _table(["文件", "时刻", "体积", "尺寸", "字号", "行数", "安全区"], [
        [rel(r["cover"]), f"{r['second']:.2f}s", human(r["cover"].stat().st_size),
         f"{COVER_WIDTH}×{COVER_HEIGHT}", f"{r['size']}px", str(len(r["lines"])),
         "OK" if r["inside"] else "ERROR 越界"]
        for r in results
    ])
    log("")
    log(f"联系表：{rel(sheet_path)}（{sheet_w}×{sheet_h}，{human(sheet_path.stat().st_size)}）")
    log("")
    log("下一步：挑一张，或重跑 `python scripts/deliver.py cover --frames 5.2,20.0,40.5` 换时间点。"
        f"（默认时刻 {','.join(f'{s:g}' for s in DEFAULT_COVER_FRAMES)} 对应 hook 爆发 / demo 图表 / CTA）")
    log("挑之前先看联系表：脚本能保证的是「文字在安全区内」，保证不了「文字不压住成片自己的字」。")
    log("  如果封面文字和画面里的大字/字幕叠在一起，按顺序试：")
    log("    ① 换位置：--position top（画面下方内容重要时）或 --position middle")
    log("    ② 换时刻：--frames 挑该场景里自家文字较少的那个瞬间")
    log("    ③ 换标题：--title \"短一点的标题\"（标题越短，字号越大越醒目）")
    log(f"挑好之后按 references/校验清单.md 的 B1（安全区）和 B4（品牌名拼写，"
        f"品牌名要逐字符对上官网「{brand or 'brand.name'}」）再人眼过一遍。")

    if failed:
        sys.stderr.write(
            f"ERROR: {failed} 张封面的文字越出安全区（上 {SAFE_TOP:.0%} / 下 {SAFE_BOTTOM:.0%} / "
            f"左 {SAFE_LEFT:.0%} / 右 {SAFE_RIGHT:.0%}）。请按上面的提示改短标题或调小 captions.maxChars。\n"
        )
        return 1
    return 0


# ===========================================================================
# 自测（--selftest / scripts/selftest.py）
#
# 大部分是纯离线用例：不读素材、不需要 ffmpeg / ffprobe / Pillow、不联网。
# 覆盖交付线最容易悄悄回归的纯逻辑：平台表 schema、conf 档位 → 文案映射、
# 帧率/码率阈值、--frames 解析与折行、--out 预检、旧封面清理、旋转元数据判定。
# 其中「可解码性 / 旋转元数据」两个用例会用本机 ffmpeg 现场生成 3 秒小样本
# （不依赖仓库里的大文件）；本机没装 ffmpeg/ffprobe 时这两个用例自动跳过，
# 不把「缺工具」误判成测试失败。
# ===========================================================================

class _FakeMedia:
    """给 ``_check_*`` 用的最小 Media 替身（不跑 ffprobe，保证自测不依赖 ffmpeg）。"""

    def __init__(self, width: int = 1080, height: int = 1920, fps: float = 60.0,
                 video_bitrate: Optional[int] = 8_000_000, duration: float = 45.0,
                 codec: str = "h264", pix_fmt: str = "yuv420p",
                 audio_codec: str = "aac", file_size: int = 8_900_000,
                 lufs: Optional[float] = -14.0, true_peak: Optional[float] = -1.0,
                 rotation: int = 0, coded_width: Optional[int] = None,
                 coded_height: Optional[int] = None) -> None:
        self.width = width
        self.height = height
        self.rotation = rotation
        self.coded_width = width if coded_width is None else coded_width
        self.coded_height = height if coded_height is None else coded_height
        self.has_video = width > 0 and height > 0
        self.fps = fps
        self.video_bitrate = video_bitrate
        self.duration = duration
        self.codec = codec
        self.pix_fmt = pix_fmt
        self.audio_codec = audio_codec
        self.file_size = file_size
        self.lufs = lufs
        self.true_peak = true_peak

    @property
    def aspect(self) -> float:
        return (self.width / self.height) if self.height else 0.0

    @property
    def aspect_label(self) -> str:
        for label, ratio in (("9:16", 9 / 16), ("16:9", 16 / 9), ("3:4", 3 / 4),
                             ("4:3", 4 / 3), ("1:1", 1.0)):
            if self.aspect and abs(self.aspect - ratio) / ratio < 0.005:
                return label
        return f"{self.aspect:.3f}"


def _selftest() -> int:
    import contextlib
    import io
    import tempfile

    failures: List[str] = []
    total = 0

    def check(name, func) -> None:
        nonlocal total
        total += 1
        try:
            note = func()
        except AssertionError as exc:
            failures.append(f"{name}：{exc}")
        except SystemExit as exc:  # error() 的预期退出不该在这里冒出来
            failures.append(f"{name}：不该退出的地方退出了（SystemExit {exc.code}）")
        except Exception as exc:  # noqa: BLE001
            failures.append(f"{name}：未预期异常 {type(exc).__name__}: {exc}")
        else:
            print(f"    OK：{name}" + (f" —— {note}" if note else ""))

    def platform_schema() -> str:
        required = ("name", "dur", "size", "aspect", "res", "vbitrate", "codecs",
                    "audio_codecs", "src", "conf", "advice")
        assert set(PLATFORM_ORDER) == set(PLATFORMS), "PLATFORM_ORDER 与 PLATFORMS 不一致"
        for key in PLATFORM_ORDER:
            p = PLATFORMS[key]
            missing = [field for field in required if field not in p]
            assert not missing, f"{key} 缺字段 {missing}"
            assert set(p["conf"]) == {"dur", "size", "aspect", "res", "vbitrate"}, \
                f"{key} 的 conf 缺项或多项：{sorted(p['conf'])}"
            for field, conf in p["conf"].items():
                assert conf in CONF_LABEL, \
                    f"{key}.{field} 的 conf={conf!r} 不在 CONF_LABEL 里"
            for conf, _text, url in p["src"]:
                assert conf in CONF_LABEL, f"{key} 的 src 里有未知 conf={conf!r}"
                assert url.startswith("http"), f"{key} 的 src 出处不是 URL：{url}"
            assert isinstance(p["dur"], tuple) and len(p["dur"]) == 2 and p["dur"][0] < p["dur"][1]
            assert isinstance(p["res"], tuple) and len(p["res"]) == 2
            assert isinstance(p["codecs"], tuple) and p["codecs"]
            assert isinstance(p["audio_codecs"], tuple) and p["audio_codecs"]
            assert isinstance(p["advice"], str) and p["advice"].strip()
        return f"{len(PLATFORMS)} 个平台 schema 齐全、conf 档位合法"

    def conf_text_mapping() -> str:
        assert "建议" in CONF_LABEL["suggestion"]
        for banned in ("平台硬限制", "拒收", "要求"):
            assert banned not in CONF_LABEL["suggestion"], \
                f"「官方建议值」标签里出现了「{banned}」：{CONF_LABEL['suggestion']}"
        legend = "\n".join(CONF_LEGEND)
        for conf, label in CONF_LABEL.items():
            assert label in legend, f"CONF_LEGEND 漏了 {conf} 的说明"
        assert "不会被拒收" in legend, "legend 没说明「建议」不拒收"
        return "conf 标签与 legend 一一对应，建议档不写「平台硬限制/要求」"

    def suggestion_checks() -> str:
        p = PLATFORMS["shipinhao"]
        assert p["conf"]["vbitrate"] == "suggestion", p["conf"]
        assert p["conf"]["aspect"] == "suggestion", p["conf"]
        assert p["conf"]["res"] == "project", p["conf"]
        low = _check_bitrate(_FakeMedia(video_bitrate=1_260_000), p)
        assert low.level == "WARN" and low.conf == "suggestion", \
            f"视频号低码率应为 WARN/suggestion，实际 {low.level}/{low.conf}"
        for banned in ("硬限制", "要求"):
            assert banned not in low.advice, f"建议值文案里出现了「{banned}」：{low.advice}"
        assert "建议" in low.advice, low.advice
        ok = _check_bitrate(_FakeMedia(video_bitrate=12_000_000), p)
        assert ok.level == "OK" and "建议" in ok.advice and "要求" not in ok.advice, ok.advice
        aspect = _check_aspect(_FakeMedia(width=384, height=1920), p)
        assert aspect.level == "WARN" and aspect.conf == "suggestion", \
            f"视频号越界画幅应为 WARN（不是 ERROR），实际 {aspect.level}/{aspect.conf}"
        extra_ok = _check_aspect(_FakeMedia(width=1920, height=1080), p)
        assert extra_ok.level == "OK", f"视频号 16:9 在宽高比 0.33~3.0 内应 OK，实际 {extra_ok.level}"
        res = _check_resolution(_FakeMedia(), p)
        assert res.level == "OK" and res.conf == "project", \
            f"视频号分辨率应为 OK/project，实际 {res.level}/{res.conf}"
        # conf=project 的项目规范（AGENTS.md 第 1 / 7 节：视频号 1080×1920）违反时必须 ERROR，
        # 否则交付闸门（0 ERROR）会对违反本项目规范的文件假绿——R6 实测 720×1280 曾只报 WARN。
        small = _check_resolution(_FakeMedia(width=720, height=1280), p)
        assert small.level == "ERROR" and small.conf == "project", \
            f"视频号 720×1280 违反本项目规范应 ERROR，实际 {small.level}/{small.conf}"
        assert "本项目" in small.advice, small.advice
        small_third = _check_resolution(_FakeMedia(width=720, height=1280), PLATFORMS["douyin"])
        assert small_third.level == "WARN" and small_third.conf == "thirdparty", \
            f"抖音 720×1280 是 thirdparty 口径，只应 WARN，实际 {small_third.level}/{small_third.conf}"
        horiz = _check_resolution(_FakeMedia(width=1280, height=720), p)
        assert horiz.level == "ERROR" and horiz.conf == "project", \
            f"视频号 1280×720（非 1920×1080 横版）违反本项目规范应 ERROR，实际 {horiz.level}/{horiz.conf}"
        return "视频号官方建议值只判 WARN，文案含「建议」不含「硬限制/要求」；project 档违反判 ERROR"

    def fps_thresholds() -> str:
        p = PLATFORMS["shipinhao"]
        cases = ((23.976, "WARN"), (24.0, "WARN"), (25.0, "WARN"),
                 (29.97, "WARN"), (30.0, "OK"), (60.0, "OK"))
        for fps, expected in cases:
            row = _check_fps(_FakeMedia(fps=fps), p)
            assert row.level == expected, f"{fps:g}fps 期望 {expected}，实际 {row.level}"
            assert row.level != "ERROR", f"{fps:g}fps 用 thirdparty 口径判了 ERROR"
            assert row.conf == "thirdparty"
        return "23.976/24/25/29.97fps 只 WARN，30/60fps OK（thirdparty 从不 ERROR）"

    def unknown_conf_info() -> str:
        p = PLATFORMS["douyin"]
        row = _check_bitrate(_FakeMedia(video_bitrate=1_000_000), p)
        assert row.level == "INFO" and row.conf == "unknown", f"{row.level}/{row.conf}"
        size = _check_size(_FakeMedia(), PLATFORMS["shorts"])
        assert size.level == "INFO", f"Shorts 体积 unknown 应 INFO，实际 {size.level}"
        return "conf=unknown 的码率/体积只打 INFO，不参与判定"

    def cover_frames_parse() -> str:
        assert _parse_frames(None, 45.0) == list(DEFAULT_COVER_FRAMES), \
            _parse_frames(None, 45.0)
        assert _parse_frames("3.6,21.7", 45.0) == [3.6, 21.7]
        assert _parse_frames("3.6，21.7", 45.0) == [3.6, 21.7], "中文逗号应兼容"
        assert _parse_frames("999", 45.0) == [44.9], _parse_frames("999", 45.0)
        for bad in ("abc", "-1"):
            with contextlib.redirect_stderr(io.StringIO()):
                try:
                    _parse_frames(bad, 45.0)
                except SystemExit as exc:
                    assert exc.code == 1, f"--frames {bad!r} 的退出码应为 1，实际 {exc.code}"
                else:
                    raise AssertionError(f"--frames {bad!r} 应该 ERROR 退出")
        return "--frames 默认值/中文逗号/越界夹取/非法值退出码都正常"

    def cover_helpers() -> str:
        lines = _split_caption("每天记账要花十几分钟真的很烦人", 13)
        assert len(lines) == 2 and all(len(line) <= 13 for line in lines), lines
        assert _split_caption("", 13) == []
        assert _brand_accent_rgb({"colors": {"accent": "#FF6B35"}}) == (255, 107, 53)
        assert _brand_accent_rgb({}) == DEFAULT_BRAND_ACCENT
        assert _brand_accent_rgb({"colors": {"accent": "bad"}}) == DEFAULT_BRAND_ACCENT
        return "折行与品牌色解析（含兜底）正常"

    def cover_out_precheck() -> str:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            out_dir = _cover_out_dir(str(base / "covers"))
            assert out_dir.is_dir(), f"正常 --out 应建目录，实际 {out_dir}"
            existing_file = base / "not-a-dir.txt"
            existing_file.write_text("x", encoding="utf-8")
            with contextlib.redirect_stderr(io.StringIO()):
                try:
                    _cover_out_dir(str(existing_file))
                except SystemExit as exc:
                    assert exc.code == 1, f"退出码应为 1，实际 {exc.code}"
                else:
                    raise AssertionError("--out 指向文件应该 ERROR 退出（不许抛 traceback）")
        return "--out 已存在且是文件 → 干净 ERROR（不再抛 FileExistsError）"

    def stale_cleanup() -> str:
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp)
            # keep = 本轮刚抽出的中间帧；其它本功能命名（旧候选 / 旧中间帧 / 旧联系表）都该清掉，
            # 包括换片子重跑时上一支片子的中间帧（_other-frame-1.png）。
            keep = ["other.png", "cover-abc.png", "note.txt", "_demo-frame-1.png"]
            stale = ["cover-1.png", "cover-2.png", "cover-9.png", SHEET_NAME,
                     "_demo-frame-2.png", "_other-frame-1.png"]
            for name in keep + stale:
                (out_dir / name).write_bytes(b"x")
            removed = _clean_stale_cover_outputs(out_dir, keep={"_demo-frame-1.png"})
            removed_names = {path.name for path in removed}
            assert removed_names == set(stale), f"清理集合不对：{sorted(removed_names)}"
            remaining = {path.name for path in out_dir.iterdir()}
            assert remaining == set(keep), \
                f"误删了不该动的文件：{sorted(remaining - set(keep))}"
        return "重跑清理只删本功能固定命名的旧候选/中间帧/旧联系表，本轮帧与用户文件不动"

    def system_exit_propagates() -> str:
        """check_video 复用失败可以兜底，但 ``sys.exit()`` 这类真失败绝不能被兜底吞掉。

        R5 复核（music.py:616 同类问题）：``_probe`` / ``_ebur128`` 的
        ``except SystemExit: raise`` 必须真的把退出码传出去，否则下游测不出响度/读不出
        ffprobe 时会静默 fallback 成「成功」，交付预检变成假绿。
        """
        class _FakeCV:
            @staticmethod
            def _ffprobe(path):
                raise SystemExit(7)

            @staticmethod
            def _ffmpeg_af(path, filt):
                raise SystemExit(8)

        original = globals()["_load_check_video"]
        try:
            globals()["_load_check_video"] = lambda: _FakeCV()
            for func, name, expected in ((_probe, "ffprobe", 7), (_ebur128, "ebur128", 8)):
                try:
                    func(Path("unused.mp4"))
                except SystemExit as exc:
                    assert exc.code == expected, f"{name} 的 SystemExit 退出码被改写：{exc.code}"
                else:
                    raise AssertionError(f"{name} 的 SystemExit 被吞了（会假成功、exit 0）")
        finally:
            globals()["_load_check_video"] = original
        return "复用 check_video 抛 SystemExit 时原样传播（不会被兜底吞成假成功）"

    def decode_verdict_matrix() -> str:
        assert _decode_verdict(0, "") is None
        assert _decode_verdict(0, "  \n\t ") is None
        stats = _decode_verdict(
            0, "[h264 @ 0x1] Invalid NAL unit size (2471 > 36)\n"
               "[mov,mp4] stream 0, offset 0x4a0b3: partial file")
        assert stats is not None and "Invalid NAL unit size" in stats, stats
        assert "partial file" in stats, stats
        hard = _decode_verdict(-1094995529, "[mov] corrupt input packet in stream 0")
        assert hard is not None and "退出码" in hard, hard
        return "rc=0 但 stderr 有解码错误也判失败（截断文件不加 -xerror 时正是这种形态）"

    def decode_and_rotation_e2e() -> str:
        """现场生成 3 秒小样本，验证「完整文件通过 / 截断文件 ERROR / rotated 按显示尺寸判」。"""
        if not (has_cmd("ffmpeg") and has_cmd("ffprobe")):
            return "跳过（本机没装 ffmpeg/ffprobe；解码与旋转的 E2E 只在装了 ffmpeg 的机器上跑）"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            src = root / "small.mp4"
            gen = subprocess.run(
                ["ffmpeg", "-hide_banner", "-v", "error", "-nostdin", "-y",
                 "-f", "lavfi", "-i", "testsrc=duration=3:size=320x568:rate=30",
                 "-f", "lavfi", "-i", "sine=frequency=440:duration=3",
                 "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest",
                 "-movflags", "+faststart", str(src)],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=180,
            )
            if gen.returncode != 0 or not src.is_file():
                return "跳过（本机 ffmpeg 生成测试样本失败，可能缺 libx264 编码器）"
            # 正样本：完整文件必须解码通过，可解码性判 OK。
            normal_detail = _decode_failure(src)
            assert normal_detail is None, f"完整样本被误判为坏文件：{normal_detail}"
            normal = Media(src)
            assert normal.rotation == 0, normal.rotation
            assert (normal.coded_width, normal.coded_height) == (320, 568), \
                (normal.coded_width, normal.coded_height)
            assert (normal.width, normal.height) == (320, 568), (normal.width, normal.height)
            normal_rotate = _check_rotation(normal, {"width": 320, "height": 568})
            assert normal_rotate.level == "OK", normal_rotate.level
            ok_row = _check_decodable(normal, normal_detail)
            assert ok_row.level == "OK", f"完整样本可解码性应 OK，实际 {ok_row.level}"
            # 截断样本：只留前 60% 字节（moov 在 faststart 下已在前部，ffprobe 照样读得出
            # 320×568/3s，但后半段 mdat 丢了 → 必须真解码才能发现）。旧版 deliver 对它 0E 假绿。
            broken = root / "broken.mp4"
            broken.write_bytes(src.read_bytes()[: int(src.stat().st_size * 0.6)])
            broken_detail = _decode_failure(broken)
            assert broken_detail is not None, "截断样本竟然解码通过了（假绿回归）"
            bad_row = _check_decodable(normal, broken_detail)
            assert bad_row.level == "ERROR" and bad_row.conf == "project", \
                f"截断样本可解码性应 ERROR/project，实际 {bad_row.level}/{bad_row.conf}"
            # 旋转样本：-display_rotation 90 写 displaymatrix；编码 320×568，显示 568×320。
            rotated = root / "rotated.mp4"
            rot = subprocess.run(
                ["ffmpeg", "-hide_banner", "-v", "error", "-nostdin", "-y",
                 "-display_rotation", "90", "-i", str(src), "-c", "copy", str(rotated)],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=180,
            )
            if rot.returncode != 0 or not rotated.is_file():
                return ("完整样本 / 截断样本通过；旋转样本生成失败（本机 ffmpeg 不支持 "
                        "-display_rotation），旋转判定已由纯逻辑用例覆盖")
            m_rot = Media(rotated)
            assert m_rot.rotation % 180 == 90, m_rot.rotation
            assert (m_rot.coded_width, m_rot.coded_height) == (320, 568), \
                (m_rot.coded_width, m_rot.coded_height)
            assert (m_rot.width, m_rot.height) == (568, 320), \
                f"显示尺寸没按旋转互换：{m_rot.width}×{m_rot.height}"
            rotate_error = _check_rotation(m_rot, {"width": 320, "height": 568})
            assert rotate_error.level == "ERROR" and rotate_error.conf == "project", \
                f"旋转后显示尺寸与 timing 相反应 ERROR/project，实际 " \
                f"{rotate_error.level}/{rotate_error.conf}"
            rotate_ok = _check_rotation(m_rot, {"width": 568, "height": 320})
            assert rotate_ok.level == "OK", f"显示尺寸与声明一致应 OK，实际 {rotate_ok.level}"
        return ("3 秒样本：完整文件解码 OK；截断文件 ERROR；rot90 显示 568×320 "
                "且与 timing 声明相反时 ERROR、一致时 OK")

    named_checks = (
        ("平台表 schema / conf 档位合法", platform_schema),
        ("conf 档位 → 文案映射（建议档不写成硬限制）", conf_text_mapping),
        ("视频号「官方建议值」只判 WARN", suggestion_checks),
        ("帧率阈值：thirdparty 口径从不判 ERROR", fps_thresholds),
        ("conf=unknown 只打 INFO、不参与判定", unknown_conf_info),
        ("--frames 解析与越界夹取", cover_frames_parse),
        ("封面折行 / 品牌色解析", cover_helpers),
        ("cover --out 预检（指向文件时干净报错）", cover_out_precheck),
        ("cover 重跑清理（只删本功能产物）", stale_cleanup),
        ("SystemExit 不被兜底吞掉（复用 check_video 的失败要传播）", system_exit_propagates),
        ("解码判定矩阵：stderr 有错即失败", decode_verdict_matrix),
        ("解码 / 旋转 E2E（现场生成正样本+截断样本+rot90 样本）", decode_and_rotation_e2e),
    )
    for name, func in named_checks:
        check(name, func)

    print("")
    if failures:
        for failure in failures:
            sys.stderr.write(f"ERROR: selftest — {failure}\n")
        sys.stderr.write(f"ERROR: selftest — {len(failures)} 项失败（共 {total} 项）\n")
        return 1
    print(f"OK: selftest — 平台表 / conf 文案 / 帧率码率阈值 / --frames / 折行 / "
          f"cover 预检与清理 / 解码判定 / 解码与旋转 E2E 全部通过（{total} 项）")
    return 0


# ===========================================================================
# CLI
# ===========================================================================

HELP_TEXT = """promo-forge · 平台交付预检 + 封面候选（deliver.py，AGENTS.md 阶段 10）

用法
  python scripts/deliver.py check <mp4> [--platform douyin|xiaohongshu|shipinhao|shorts|all]
  python scripts/deliver.py cover [<mp4>] [--out out/covers] [--title "..."]
                                  [--position bottom|middle|top] [--frames 3.6,21.7,43.2]
  python scripts/deliver.py --selftest
  python scripts/deliver.py --help

check —— 平台交付预检
  先做全局检查（所有平台共用）：完整解码一遍（截断/损坏的文件 ffprobe 读得出元数据，
  但播不了，必须真解码才能发现）+ 旋转元数据（带 rotate=90/270 的文件按播放器显示尺寸
  判定，并与 timing.json 的 width/height 比对）。
  再对这一支 mp4 逐平台打印检查表：时长 / 分辨率与画幅 / 帧率 / 码率 / 文件体积 /
  是否有音轨 / 响度是否在 -16~-12 LUFS。每个平台给 OK / WARN / ERROR 与「怎么改」。
  有 ERROR 时非零退出（AGENTS.md 第 7 节）。
  平台参数区分「平台硬限制（官方文档）」「官方建议值（非硬限制）」
  「推荐值（第三方汇总，非官方）」；没查到确切数字的标「需人工确认」，脚本不编数字；
  「本项目规范（AGENTS.md）」标记的是我们自己的硬规矩（必须有音轨/视频流），不是平台限制。
  官方原文用「建议」的参数（如视频号码率）只做提示、判 WARN，不写成「硬限制 / 要求」。

cover —— 生成 3 张封面候选
  从成片里抽 3 帧（默认 3.6s hook 爆发 / 21.7s demo 图表 / 43.2s CTA），每帧生成一张
  1080×1920 封面：从文字块顶边往下压一层渐变暗化（保证白字有对比度），再叠加品牌名 +
  一行标题（默认取 timing.json 的 brand.name 与 title，标题按 captions.maxChars 折行）。
  文字用 Pillow 量 bbox，**越界就自动缩小字号重排**，并打印每张封面的实际 bbox 与
  安全区判定结果。最后拼一张 out/covers/_sheet.jpg 联系表供人挑。

  --position 决定文字块贴安全区的哪一边：
    bottom（默认）贴底；middle 垂直居中在 56% 处；top 贴上。
  **成片自己已经有字幕或大字时用 middle**，否则封面文字会正好压在画面里的大字上，
  两层字叠在一起谁都读不清。

安全区（AGENTS.md 第 4 节）
  上 10% / 下 18% / 左 6% / 右 14%（右侧留给平台互动按钮）。

自测（--selftest）
  大部分是纯函数自测（不读素材 / 不需要 ffmpeg / 不联网）：平台表 schema、conf 档位 → 文案映射、
  帧率/码率阈值、--frames 解析、封面折行、--out 预检、旧封面清理、解码判定矩阵；
  另有「解码 / 旋转 E2E」用本机 ffmpeg 现场生成 3 秒样本（正样本 + 截断样本 + rot90 样本），
  本机没装 ffmpeg 时这一项自动跳过、不算失败。

退出码
  check：0 = 所选平台 0 ERROR；1 = 有 ERROR（或缺依赖/文件不存在）。
  cover：0 = 每张封面都通过安全区判定；1 = 有封面文字越界，或依赖缺失。
  --selftest：0 = 全部通过；1 = 有断言失败。

例
  python scripts/deliver.py check out/final-1080p.mp4 --platform all
  python scripts/deliver.py check out/final-1080p.mp4 --platform shipinhao
  python scripts/deliver.py cover
  python scripts/deliver.py cover out/final-1080p.mp4 --out out/covers --title "小账本 · 示例片"
  python scripts/deliver.py cover --frames 3.6,21.7,43.2

依赖：ffmpeg / ffprobe（硬依赖）、src/timing.json、scripts/_common.py；封面还需 Pillow。
不新增任何 pip / npm 依赖。
"""


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="deliver.py",
        description=HELP_TEXT,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="不带子命令时会打印上面这段中文帮助。",
    )
    sub = p.add_subparsers(dest="cmd", metavar="子命令")
    p.add_argument("--selftest", action="store_true",
                   help="纯函数自测（不读素材/不需要 ffmpeg）：python scripts/deliver.py --selftest")

    c = sub.add_parser(
        "check", help="平台交付预检（抖音/小红书/视频号/Shorts）",
        description="逐平台打印检查表，每项给 OK/WARN/ERROR 与「怎么改」；有 ERROR 非零退出。",
        epilog="例：python scripts/deliver.py check out/final-1080p.mp4 --platform all",
    )
    c.add_argument("video", help="要检查的 mp4，例如 out/final-1080p.mp4")
    c.add_argument("--platform", choices=(*PLATFORM_ORDER, "all"), default="all",
                   help="只查某一个平台，默认 all（全部四个）")
    c.add_argument("--lufs-min", type=float, default=DEFAULT_LUFS_MIN,
                   help=f"响度下限 LUFS，默认 {DEFAULT_LUFS_MIN:g}（与 check_video.py 一致）")
    c.add_argument("--lufs-max", type=float, default=DEFAULT_LUFS_MAX,
                   help=f"响度上限 LUFS，默认 {DEFAULT_LUFS_MAX:g}")
    c.set_defaults(func=cmd_check)

    v = sub.add_parser(
        "cover", help="生成 3 张封面候选 + 联系表",
        description="抽帧 → 压暗 → 叠品牌名与标题 → 量 bbox → 越界自动缩字号重排；最后拼联系表。",
        epilog="例：python scripts/deliver.py cover --frames 3.6,21.7,43.2",
    )
    v.add_argument("video", nargs="?", default="out/final-1080p.mp4",
                   help="成片路径，默认 out/final-1080p.mp4")
    v.add_argument("--out", default="out/covers", help="封面输出目录，默认 out/covers")
    v.add_argument("--title", default=None,
                   help="封面标题，默认取 timing.json 的 title（按 captions.maxChars 折行）")
    v.add_argument("--position", choices=("bottom", "middle", "top"), default="bottom",
                   help="文字块贴安全区的哪一边：bottom（默认，贴底）/ middle（垂直居中 56%%）/ "
                        "top（贴上）。成片自己已经有字幕或大字时用 middle 或 top，"
                        "避免封面文字压在画面里的大字上")
    v.add_argument("--frames", default=None,
                   help="抽帧时刻（秒，逗号分隔），默认 "
                        + ",".join(f"{s:g}" for s in DEFAULT_COVER_FRAMES)
                        + "（hook 爆发 / demo 图表 / CTA）")
    v.set_defaults(func=cmd_cover)
    return p


def main(argv: Optional[Sequence[str]] = None) -> int:
    _force_utf8()
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    if "--selftest" in raw_argv:
        # 和其它脚本一致：--selftest 走纯函数自测，不解析子命令、不读素材
        return _selftest()
    parser = build_parser()
    args = parser.parse_args(raw_argv)
    if not getattr(args, "cmd", None):
        # AGENTS.md 第 6 节：不带子命令时打印中文帮助（而不是 argparse 的英文 usage 报错）
        log(HELP_TEXT)
        return 0
    return int(args.func(args) or 0)


if __name__ == "__main__":
    raise SystemExit(main())
