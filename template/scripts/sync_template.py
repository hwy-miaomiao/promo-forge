#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""promo-forge · 用新版模板更新已有项目（sync_template.py）。

用途
----
`template/` 是会被复制成项目的工作模板。项目开工之后你会在副本里改场景、改风格，
但**脚本与公共库**是会随模板一起迭代的——本脚本负责把「与项目风格无关的通用文件」
从新版模板同步回已有项目，同时**绝不碰**属于项目自有的文件。

- **会比对/同步**：`scripts/**/*.py|*.mjs|*.js`、`src/components/*.tsx`、`web/App.tsx`、
  `web/main.tsx`、`web/index.html`、`web/vite.config.ts`、`src/index.ts`、`src/Root.tsx`、
  `src/assets.ts`、`src/lib/anim.ts`、`src/lib/beats.ts`、`src/lib/captionText.ts`、
  `src/lib/timing.ts`、`src/lib/fonts.ts`。
- **永不触碰**（各项目自有）：`src/timing.json`、`src/scenes/**`、`src/Promo.tsx`、
  `src/lib/theme.ts`、`src/index.css`、`fonts.json`、`web/config.ts`、`web/style.css`、
  `remotion.config.ts`。
- 模板里若有既不在同步范围、也不在项目自有清单的文件，`--diff` 会单独打印「未纳入比对」清单，
  不会再出现「模板更新了、报告却说全部一致」的静默漏同步。

典型调用
--------
    python scripts/sync_template.py                 # 只报告差异（安全，默认）
    python scripts/sync_template.py --diff          # 差异 + 文本 diff 摘要
    python scripts/sync_template.py --apply         # 覆盖本地通用文件（会先备份）
    python scripts/sync_template.py --apply --force # 连「本地比模板新」的文件也覆盖（危险）
    python scripts/sync_template.py --template D:/promo-forge --apply
    python scripts/sync_template.py --selftest      # 纯函数自测（不读模板/不写文件）

模板目录的定位顺序
------------------
1. `--template <路径>`（可以指 skill 根目录，也可以直接指 `template/`）
2. 环境变量 `PROMO_FORGE_TEMPLATE`
3. 相对当前项目 `../..`（克隆用法：`<仓库>/outputs/<slug>/` → `<仓库>/template`）
4. 用户目录下常见 skills 目录里的 `template/`：
   `~/.claude/skills/promo-forge`、`~/.codex/skills/promo-forge`、`~/.cursor/skills/promo-forge`、
   `~/.gemini/skills/promo-forge`、`~/.agents/skills/promo-forge`

依赖
----
仅标准库。

退出码
------
0 比较/同步正常完成（**有差异不算失败**——这个脚本的日常工作就是报告差异）；
1 出错（找不到模板、写入失败等），或者 `--apply` 时跳过了「本地比模板新 / 修改时间相同」的文件
  —— 这两种情况都会打印 `ERROR: ...`：说明这一轮没同步完，看清 WARN 再决定要不要加 `--force`。
需要提醒的打印 `WARN: ...`。
"""

from __future__ import annotations

import argparse
import difflib
import filecmp
import os
import shutil
import sys
from datetime import datetime
from pathlib import Path
from typing import List, Tuple

# ---------------------------------------------------------------------------
# 导入公共库 scripts/_common.py（由另一位维护者提供，本脚本不修改它）
# ---------------------------------------------------------------------------
sys.path.insert(0, str(Path(__file__).resolve().parent))
try:  # pragma: no cover - 只在 _common.py 缺失时走到
    from _common import PROJECT_ROOT, error, human, log, warn  # type: ignore

    _HAVE_COMMON = True
    _IMPORT_ERROR: Exception | None = None
except ImportError as exc:  # pragma: no cover
    _HAVE_COMMON = False
    _IMPORT_ERROR = exc
    # 本脚本只做文件比对，顺手提供一份最小兜底实现，保证 _common.py 还没就位时也能跑。
    PROJECT_ROOT = Path(__file__).resolve().parent.parent

    def log(message="") -> None:  # type: ignore[misc]
        sys.stdout.write(f"{message}\n")

    def warn(message) -> None:  # type: ignore[misc]
        sys.stdout.write(f"WARN: {message}\n")

    def error(message, code: int = 1):  # type: ignore[misc]
        sys.stderr.write(f"ERROR: {message}\n")
        raise SystemExit(code)

    def human(size_bytes) -> str:  # type: ignore[misc]
        try:
            value = float(size_bytes)
        except (TypeError, ValueError):
            return "?"
        for unit in ("B", "KB", "MB", "GB"):
            if value < 1024 or unit == "GB":
                return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
            value /= 1024
        return f"{value:.1f} GB"

    sys.stderr.write(
        "WARN: 读不到 scripts/_common.py（%s），改用最小内置实现继续。"
        "脚本仍可用，但请确认 scripts 目录完整。\n" % (_IMPORT_ERROR,)
    )


# ===========================================================================
# 常量
# ===========================================================================

# 需要比对的「通用契约文件」——只依赖 timing.json 的字段与模板契约，与项目风格无关。
# 分类标准（每条都要能说出理由；新模板文件要么进这里、要么进 PROJECT_OWNED，不许悬空）：
#   · web/App.tsx         网页版播放器外壳，只读 timing / public，不含项目文案
#   · web/main.tsx        React 挂载入口，读 WEB 配置，无项目样式
#   · web/vite.config.ts  Vite 构建配置（publicDir 指向 ../public、端口），与项目无关
#   · web/index.html      网页版 HTML 壳（viewport / theme-color 占位），模板统一维护
#   · src/index.ts        Remotion 入口（只 registerRoot），无项目内容
#   · src/Root.tsx        composition 注册（Promo / PromoPreview 的时长与 fps 契约）
#   · src/assets.ts       预取资源清单（从 timing.json 派生，网页版防「画面在走声音没到」）
#   · src/lib/timing.ts   timing.json 的类型定义与换算（契约层，字段改名要 tsc 抓到）
#   · src/lib/fonts.ts    字体角色解析 + 内嵌子集字体注册（跨机渲染一致性）
#   · src/lib/captionText.ts  字幕/大字的折行与截断纯函数（片内与封面共用同一套口径）
#   · src/lib/anim.ts     动画原语库（spring/interpolate 封装，与项目风格无关）
#   · src/lib/beats.ts    卡点换算库（拍/小节 → 帧，纯函数）
COMMON_FILES = (
    "web/App.tsx",
    "web/main.tsx",
    "web/index.html",
    "web/vite.config.ts",
    "src/index.ts",
    "src/Root.tsx",
    "src/assets.ts",
    "src/lib/anim.ts",
    "src/lib/beats.ts",
    "src/lib/captionText.ts",
    "src/lib/fonts.ts",
    "src/lib/timing.ts",
)

# 需要比对的目录与后缀。
#   · scripts/            全部脚本（.py/.mjs/.js）
#   · src/components/     通用组件外壳（Stage/Scene/Shot/Caption/PhoneFrame/BrowserFrame）：
#                         它们实现 AGENTS.md 的安全区/折行/截图不变形等硬规矩，属于契约层
COMMON_GLOBS = (
    ("scripts", (".py", ".mjs", ".js")),
    ("src/components", (".tsx",)),
)

# 永不触碰的项目自有文件（写在这里是为了让使用者一眼看到边界，也让 AI 不要自作聪明）。
# 理由：这些文件承载项目的内容 / 配色 / 字体栈 / 场景编排，模板覆盖会把项目改没。
PROJECT_OWNED = (
    "src/timing.json",
    "src/scenes/**",
    "src/Promo.tsx",
    # index.css 装着项目自己的底色 / 字体栈，模板不能覆盖它；但它是 check_index_css() 的检查对象，
    # 所以显式列在这里，让「它的处理方式」在 --diff / --help 里都看得见（见 check_index_css）。
    "src/index.css",
    "src/lib/theme.ts",
    "fonts.json",
    "web/config.ts",
    "web/style.css",
    "remotion.config.ts",
)

# 模板目录的特征文件（用来判断一个路径到底是 skill 根目录还是 template/ 本身）。
TEMPLATE_MARKERS = ("package.json", "src/timing.json")

# 常见 skills 安装目录（相对用户主目录）。
SKILL_HOMES = (
    ".claude/skills/promo-forge",
    ".codex/skills/promo-forge",
    ".cursor/skills/promo-forge",
    ".gemini/skills/promo-forge",
    ".agents/skills/promo-forge",
    ".config/promo-forge",
)

# 覆盖前的备份目录（相对项目根；out/ 已被模板的 .gitignore 忽略）。
BACKUP_ROOT = "out/.sync-backup"

# `--diff` 每次最多打印多少行差异。
DIFF_MAX_LINES = 20

# 本地 src/index.css 必须包含的声明（缺了 Remotion 播放器会把竖屏截图压扁）。
CSS_REQUIRED_SNIPPET = "max-width: none"
CSS_PATH = "src/index.css"


# ===========================================================================
# 小工具
# ===========================================================================

def _force_utf8() -> None:
    """Windows 控制台默认 GBK，中文会炸；这里兜底成 UTF-8。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except Exception:
            pass


def _rel_to(path: Path, base: Path) -> str:
    try:
        return Path(path).resolve().relative_to(Path(base).resolve()).as_posix()
    except Exception:
        return Path(path).as_posix()


def _stamp(path: Path) -> str:
    try:
        stat = path.stat()
    except OSError:
        return "（读不到）"
    when = datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M")
    return f"{human(stat.st_size)} ({when})"


def _is_template_dir(path: Path) -> bool:
    return path.is_dir() and all((path / marker).exists() for marker in TEMPLATE_MARKERS)


def _normalize_template_candidate(raw: str):
    """把一个候选路径变成真正的 template/ 目录；不是就返回 None。"""
    if not raw:
        return None
    candidate = Path(raw).expanduser()
    try:
        candidate = candidate.resolve()
    except OSError:
        return None
    if _is_template_dir(candidate):
        return candidate
    nested = candidate / "template"
    if _is_template_dir(nested):
        return nested
    return None


def locate_template(cli_value):
    """按文档里的顺序定位模板目录，返回 (Path, 来源说明)。"""
    attempts = []

    if cli_value:
        found = _normalize_template_candidate(cli_value)
        if found:
            return found, "--template 参数"
        attempts.append(f"--template {cli_value}")

    env_value = os.environ.get("PROMO_FORGE_TEMPLATE")
    if env_value:
        found = _normalize_template_candidate(env_value)
        if found:
            return found, "环境变量 PROMO_FORGE_TEMPLATE"
        attempts.append(f"PROMO_FORGE_TEMPLATE={env_value}")

    # 克隆用法：<仓库>/outputs/<slug>/ → <仓库>/template
    sibling = PROJECT_ROOT.parent.parent / "template"
    found = _normalize_template_candidate(str(sibling))
    if found:
        return found, "相对当前项目 ../..（克隆用法）"
    attempts.append(str(sibling))

    home = Path.home()
    for relative in SKILL_HOMES:
        found = _normalize_template_candidate(str(home / relative))
        if found:
            return found, f"用户目录 {relative}"
        attempts.append(str(home / relative))

    error(
        "找不到 promo-forge 模板目录。试过这些位置：\n  - "
        + "\n  - ".join(attempts)
        + "\n请用 --template <路径> 指定 skill 根目录（或它的 template/ 子目录），"
        "或设置环境变量 PROMO_FORGE_TEMPLATE。"
    )
    raise SystemExit(1)  # 兜底：_common.error 会退出，这里保证任何实现下都不会返回 None


# ===========================================================================
# 收集要比较的文件
# ===========================================================================

def collect_common_files(base: Path):
    """返回 {相对路径: Path}，只含「通用文件」。"""
    found = {}
    for relative in COMMON_FILES:
        path = base / relative
        if path.is_file():
            found[relative] = path
    for directory, suffixes in COMMON_GLOBS:
        root = base / directory
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*")):
            if not path.is_file() or path.suffix.lower() not in suffixes:
                continue
            if any(part in {"__pycache__", "node_modules", ".git"} for part in path.parts):
                continue
            found[_rel_to(path, base)] = path
    return found


def _matches_project_owned(relative: str) -> bool:
    """相对路径是否落在 PROJECT_OWNED 清单里（支持 ``dir/**`` 形式）。"""
    relative = str(relative).replace("\\", "/")
    for pattern in PROJECT_OWNED:
        if relative == pattern:
            return True
        # "src/scenes/**" → 前缀 "src/scenes"；不要再拼 "/"（pattern[:-3] 已去掉 "/**"）
        if pattern.endswith("/**") and relative.startswith(pattern[:-3] + "/"):
            return True
    return False


def find_uncovered_template_files(template_dir: Path, common_relpaths) -> List[str]:
    """模板里存在、但既不在同步范围、也不在 PROJECT_OWNED 的文件（相对路径）。

    为什么要单独找出来：同步范围是白名单。模板以后新加了通用文件（比如新的组件），
    没人更新白名单时它会**永远不同步**，而旧版报告只会说「N 一致」，用户看不出来。
    这里把这类文件列进「未纳入比对」，让漏同步暴露在报告里而不是静默发生。
    """
    covered = set(common_relpaths)
    uncovered: List[str] = []
    for root_name in ("scripts", "src", "web"):
        root = template_dir / root_name
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*")):
            if not path.is_file():
                continue
            if any(part in {"__pycache__", "node_modules", ".git"} for part in path.parts):
                continue
            relative = _rel_to(path, template_dir)
            if relative in covered or _matches_project_owned(relative):
                continue
            uncovered.append(relative)
    return uncovered


# ===========================================================================
# 比较
# ===========================================================================

STATUS_NEW = "新版"
STATUS_OLD = "旧版"
STATUS_TIE = "时间相同"
STATUS_MISSING = "缺失"
STATUS_SAME = "一致"
STATUS_EXTRA = "多余"

STATUS_ORDER = (STATUS_NEW, STATUS_OLD, STATUS_TIE, STATUS_MISSING, STATUS_EXTRA, STATUS_SAME)


def compare(template_dir: Path, project_dir: Path):
    """返回 [(状态, 相对路径, 模板文件或 None, 本地文件或 None)]。"""
    template_files = collect_common_files(template_dir)
    project_files = collect_common_files(project_dir)

    rows = []
    for relative in sorted(set(template_files) | set(project_files)):
        template_path = template_files.get(relative)
        project_path = project_files.get(relative)
        if template_path and project_path:
            try:
                same = filecmp.cmp(template_path, project_path, shallow=False)
            except OSError:
                same = False
            if same:
                rows.append((STATUS_SAME, relative, template_path, project_path))
                continue
            try:
                template_mtime = template_path.stat().st_mtime
                project_mtime = project_path.stat().st_mtime
            except OSError:
                # 拿不到修改时间就别猜（保持旧行为：按模板更新处理）。
                rows.append((STATUS_NEW, relative, template_path, project_path))
                continue
            if template_mtime == project_mtime:
                # 修改时间一模一样、内容却不同：多半是 cp -r / 同步工具把模板的 mtime 一起带过来了，
                # 光看时间判断不了谁新谁旧。这时**不许判模板获胜**（那会静默覆盖本地改动）。
                rows.append((STATUS_TIE, relative, template_path, project_path))
            elif template_mtime > project_mtime:
                rows.append((STATUS_NEW, relative, template_path, project_path))
            else:
                rows.append((STATUS_OLD, relative, template_path, project_path))
        elif template_path:
            rows.append((STATUS_MISSING, relative, template_path, None))
        else:
            rows.append((STATUS_EXTRA, relative, None, project_path))
    return rows


def print_diff(relative: str, template_path: Path, project_path: Path) -> None:
    def read_lines(path: Path):
        try:
            return path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError as exc:
            return [f"<读不了这个文件：{exc}>"]

    diff = list(
        difflib.unified_diff(
            read_lines(project_path),
            read_lines(template_path),
            fromfile=f"本地/{relative}",
            tofile=f"模板/{relative}",
            lineterm="",
            n=1,
        )
    )
    if not diff:
        log("    （二进制不同但按文本比对无差异，可能是换行符或编码差异）")
        return
    shown = diff[:DIFF_MAX_LINES]
    for line in shown:
        log(f"    {line}")
    if len(diff) > DIFF_MAX_LINES:
        log(f"    …（还有 {len(diff) - DIFF_MAX_LINES} 行差异没打印）")


def print_report(rows, template_dir: Path, project_dir: Path, want_diff: bool) -> None:
    log(f"模板目录：{template_dir}")
    log(f"当前项目：{project_dir}")
    log(f"比对范围：{len(rows)} 个通用文件（项目自有的 timing.json / scenes / 风格文件不会被动）")
    log("")

    for status in STATUS_ORDER:
        bucket = [row for row in rows if row[0] == status]
        if not bucket:
            continue
        for _status, relative, template_path, project_path in bucket:
            template_desc = _stamp(template_path) if template_path else "—"
            project_desc = _stamp(project_path) if project_path else "—"
            log(f"[{status}] {relative}")
            log(f"    模板：{template_desc}")
            log(f"    本地：{project_desc}")
            if status == STATUS_TIE:
                log("    注：两侧修改时间完全相同、内容却不一样（多半是复制时把 mtime 一起带过来了），"
                    "判断不了谁新谁旧；--apply 默认不动它，确认要放弃本地改动再加 --force。")
            if want_diff and status in (STATUS_NEW, STATUS_OLD, STATUS_TIE) and template_path and project_path:
                print_diff(relative, template_path, project_path)
        log("")

    counts = {status: sum(1 for row in rows if row[0] == status) for status in STATUS_ORDER}
    log(
        "统计："
        + " / ".join(f"{counts[status]} {status}" for status in STATUS_ORDER if counts[status])
    )


# ===========================================================================
# 应用
# ===========================================================================

def apply_changes(rows, template_dir: Path, project_dir: Path, force: bool = False) -> int:
    pending = [
        (status, relative, template_path, project_path)
        for status, relative, template_path, project_path in rows
        if status in (STATUS_NEW, STATUS_OLD, STATUS_TIE, STATUS_MISSING) and template_path
    ]
    if not pending:
        log("本地通用文件与模板一致，没有需要覆盖的文件。")
        return 0

    # [旧版] = 内容不一样、而本地 mtime 比模板晚：多半是你在项目里按需改过它（改脚本、调参数、
    # 改动画库）。[时间相同] = 内容不一样、两侧 mtime 却完全相同：多半是复制时把模板的 mtime 一起
    # 带过来了，光看时间判断不了谁新谁旧。这两种默认都不能覆盖（模板会把你的改动冲掉），
    # 要覆盖必须显式加 --force。
    backlog = []
    if not force:
        backlog = [item for item in pending if item[0] in (STATUS_OLD, STATUS_TIE)]
        pending = [item for item in pending if item[0] not in (STATUS_OLD, STATUS_TIE)]
    if not pending:
        log("没有需要覆盖的文件。")

    backup_dir = project_dir / BACKUP_ROOT / datetime.now().strftime("%Y%m%d-%H%M%S")
    copied = 0
    backed_up = 0
    failed = 0
    backed_up_local: List[Tuple[str, Path]] = []
    for _status, relative, template_path, project_path in pending:
        destination = project_dir / relative
        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
            if project_path and project_path.is_file():
                backup_target = backup_dir / relative
                backup_target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(project_path, backup_target)
                backed_up += 1
                backed_up_local.append((relative, backup_target))
            shutil.copy2(template_path, destination)
            copied += 1
            log(f"  已同步 {relative}")
        except OSError as exc:
            failed += 1
            warn(f"覆盖 {relative} 失败：{exc}")

    log("")
    log(f"同步完成：覆盖 {copied} 个文件" + (f"，备份 {backed_up} 个到 {_rel_to(backup_dir, project_dir)}/"
                                            if backed_up else "（本地没有同名文件，无需备份）"))
    if failed:
        error(f"{failed} 个文件没能覆盖（见上面的 WARN 行）。最常见原因是文件被编辑器或其他程序占用，关掉后重跑。")
    if backlog:
        # 先把这些文件备份好再报错退出：用户加 --force 之前不该有任何东西被丢掉。
        for _status, relative, _template_path, project_path in backlog:
            if project_path and project_path.is_file():
                try:
                    backup_target = backup_dir / relative
                    backup_target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(project_path, backup_target)
                    backed_up_local.append((relative, backup_target))
                except OSError as exc:
                    warn(f"备份 {relative} 失败：{exc}")
        log("")
        older = [item for item in backlog if item[0] == STATUS_OLD]
        tied = [item for item in backlog if item[0] == STATUS_TIE]
        if older:
            warn(
                f"有 {len(older)} 个文件**本地比模板新**，已跳过，没有覆盖：\n"
                + "\n".join(f"       {relative}" for _s, relative, _t, _p in older[:10])
                + (f"\n       …（还有 {len(older) - 10} 个）" if len(older) > 10 else "")
                + "\n    这一栏多半是你自己按需改过的（改脚本、调参数、改动画库），"
                  "直接用模板覆盖会把你的改动冲掉，所以默认不动它们。\n"
                  "    先看差异：python scripts/sync_template.py --diff\n"
                  "    确认可以放弃本地改动，再加 --force 覆盖。"
            )
        if tied:
            warn(
                f"有 {len(tied)} 个文件**修改时间与模板完全相同、内容却不一样**，已跳过，没有覆盖：\n"
                + "\n".join(f"       {relative}" for _s, relative, _t, _p in tied[:10])
                + (f"\n       …（还有 {len(tied) - 10} 个）" if len(tied) > 10 else "")
                + "\n    这种情况多半是 cp -r / 同步工具把模板的修改时间一起带过来了，"
                  "光看时间判断不了谁新谁旧，所以默认不动它们（以前这里会当成「模板更新」直接覆盖）。\n"
                  "    先看差异：python scripts/sync_template.py --diff\n"
                  "    确认可以放弃本地改动，再加 --force 覆盖。"
            )
        if backed_up_local:
            log("")
            log(f"（本地当前内容已备份 {len(backed_up_local)} 个到 {_rel_to(backup_dir, project_dir)}/，"
                "真被覆盖了也能从这里回滚）")
        error(
            f"{len(backlog)} 个文件本地比模板新（或与模板的修改时间完全相同），默认不覆盖。"
            "确认要放弃这些本地改动请加 --force 重跑。"
        )
    return copied


# ===========================================================================
# 额外检查：src/index.css 的 max-width
# ===========================================================================

def check_index_css(project_dir: Path) -> None:
    log(f"{CSS_PATH}：项目自有文件，不参与同步（列在 PROJECT_OWNED 里）；这里只检查它是否含"
        f"「{CSS_REQUIRED_SNIPPET}」。")
    path = project_dir / CSS_PATH
    if not path.is_file():
        warn(
            f"本地没有 {CSS_PATH}，无法检查「{CSS_REQUIRED_SNIPPET}」。"
            "缺了这条声明，Remotion 播放器 / 网页版会把竖屏截图按容器宽度压扁，"
            "预览时看到的构图和成片不一致"
        )
        return
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        warn(f"读不了 {CSS_PATH}：{exc}，跳过「{CSS_REQUIRED_SNIPPET}」检查")
        return
    normalized = "".join(text.split())
    if "max-width:none" in normalized:
        log(f"{CSS_PATH} 含「{CSS_REQUIRED_SNIPPET}」，截图不会被压扁。")
    else:
        warn(
            f"{CSS_PATH} 里找不到「{CSS_REQUIRED_SNIPPET}」。"
            "请给图片 / 视频元素加上这条声明，否则预览页和网页版会把竖屏截图按容器宽度压扁，"
            "你会误判构图。"
        )


# ===========================================================================
# 自测（--selftest / scripts/selftest.py）
#
# 纯离线：不定位模板、不读真实项目、不联网；临时文件都建在 tempfile 里。
# 守卫：同步分类不重叠、通用文件集合稳定、路径/模板目录归一化、compare 六种状态、
# --diff 只读、--apply 的安全语义（备份 / 跳过本地较新 / 不删多余 / 幂等）。
# ===========================================================================

def _selftest() -> int:
    import contextlib
    import io
    import tempfile

    failures = []
    total = 0

    def check(name, func) -> None:
        nonlocal total
        total += 1
        try:
            note = func()
        except AssertionError as exc:
            failures.append(f"{name}：{exc}")
        except SystemExit as exc:
            failures.append(f"{name}：不该退出的地方退出了（SystemExit {exc.code}）")
        except Exception as exc:  # noqa: BLE001
            failures.append(f"{name}：未预期异常 {type(exc).__name__}: {exc}")
        else:
            print(f"    OK：{name}" + (f" —— {note}" if note else ""))

    def classification() -> str:
        assert len(COMMON_FILES) == len(set(COMMON_FILES)), "COMMON_FILES 有重复项"
        for relative in COMMON_FILES:
            assert "/" in relative and "\\" not in relative, \
                f"COMMON_FILES 必须写 posix 相对路径：{relative}"
            assert not _matches_project_owned(relative), \
                f"{relative} 同时在通用文件清单和 PROJECT_OWNED 里（分类冲突）"
        for directory, suffixes in COMMON_GLOBS:
            assert "\\" not in directory and not directory.startswith("/"), \
                f"COMMON_GLOBS 目录要写项目内 posix 相对路径：{directory}"
            assert not _matches_project_owned(directory), \
                f"同步目录 {directory} 与 PROJECT_OWNED 冲突"
            assert suffixes and all(str(s).startswith(".") for s in suffixes), \
                f"{directory} 的后缀必须带点：{suffixes}"
        assert len(PROJECT_OWNED) == len(set(PROJECT_OWNED)), "PROJECT_OWNED 有重复项"
        return f"{len(COMMON_FILES)} 个通用文件 + {len(COMMON_GLOBS)} 个目录，分类不重叠"

    def common_set_stable() -> str:
        expected = (
            "web/App.tsx",
            "web/main.tsx",
            "web/index.html",
            "web/vite.config.ts",
            "src/index.ts",
            "src/Root.tsx",
            "src/assets.ts",
            "src/lib/anim.ts",
            "src/lib/beats.ts",
            "src/lib/captionText.ts",
            "src/lib/fonts.ts",
            "src/lib/timing.ts",
        )
        assert tuple(COMMON_FILES) == expected, (
            "COMMON_FILES 集合变了：如果是有意增删通用文件，请同步更新本 selftest 的 expected、"
            "sync_template.py 的 docstring/--help，以及 AGENTS.md 的同步范围说明"
        )
        # R5 修复的回归守卫：通用组件目录必须在同步范围里（以前 components/* 漏同步）
        assert ("src/components", (".tsx",)) in COMMON_GLOBS, "src/components 不在同步范围里"
        for owned in ("src/scenes/**", "src/Promo.tsx", "src/lib/theme.ts",
                      "web/style.css", "web/config.ts", "src/timing.json"):
            assert owned in PROJECT_OWNED, f"PROJECT_OWNED 少了 {owned}"
        # ``dir/**`` 前缀匹配必须真的生效（曾经写成 dir//、导致 scenes 反而进了「未纳入比对」）
        for relative in ("src/scenes/Cta.tsx", "src/scenes/index.ts"):
            assert _matches_project_owned(relative), f"PROJECT_OWNED 没匹配到 {relative}"
        assert not _matches_project_owned("src/scenesX/a.ts"), "src/scenes/** 误匹配了 src/scenesX/"
        assert _matches_project_owned("src/timing.json") and _matches_project_owned("web/config.ts")
        return "通用文件集合与项目自有边界都有 selftest 守卫"

    def path_normalization() -> str:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            (base / "a").mkdir()
            (base / "a" / "b.txt").write_text("x", encoding="utf-8")
            assert _rel_to(base / "a" / "b.txt", base) == "a/b.txt", _rel_to(base / "a" / "b.txt", base)
            # 模板目录归一化：template/ 本体与 skill 根都能认，非模板返回 None
            tpl = base / "skill" / "template"
            tpl.mkdir(parents=True)
            for marker in TEMPLATE_MARKERS:
                target = tpl / marker
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text("{}", encoding="utf-8")
            assert _normalize_template_candidate(str(tpl)) == tpl
            assert _normalize_template_candidate(str(base / "skill")) == tpl
            assert _normalize_template_candidate(str(base / "nope")) is None
            assert _normalize_template_candidate("") is None
        return "相对路径与模板目录归一化正常"

    def compare_states() -> str:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            tpl = base / "template"
            proj = base / "project"
            pairs = (
                # (相对路径, 模板内容, 模板 mtime, 本地内容, 本地 mtime, 期望状态)
                ("src/lib/anim.ts", "tpl-anim\n", 2_000_000, "local-anim\n", 1_000_000, STATUS_NEW),
                ("src/lib/beats.ts", "same\n", 1_000_000, "same\n", 1_000_000, STATUS_SAME),
                ("src/lib/timing.ts", "tpl-timing\n", 5_000_000, "local-timing\n", 5_000_000, STATUS_TIE),
                ("src/lib/fonts.ts", "tpl-fonts\n", 1_000_000, "local-fonts\n", 2_000_000, STATUS_OLD),
            )
            for relative, tpl_text, tpl_mtime, proj_text, proj_mtime, _expected in pairs:
                tpl_path = tpl / relative
                proj_path = proj / relative
                for path, text, mtime in ((tpl_path, tpl_text, tpl_mtime),
                                          (proj_path, proj_text, proj_mtime)):
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(text, encoding="utf-8")
                    os.utime(path, (mtime, mtime))
            # 仅模板有 → 缺失；仅本地有 → 多余
            (tpl / "scripts").mkdir(parents=True, exist_ok=True)
            (proj / "scripts").mkdir(parents=True, exist_ok=True)
            (tpl / "scripts" / "only_template.py").write_text("tpl\n", encoding="utf-8")
            (proj / "scripts" / "only_local.py").write_text("local\n", encoding="utf-8")
            rows = {relative: status for status, relative, _t, _p in compare(tpl, proj)}
            for relative, _tpl_text, _tm, _proj_text, _pm, expected in pairs:
                assert rows.get(relative) == expected, \
                    f"{relative} 期望 {expected}，实际 {rows.get(relative)}"
            assert rows.get("scripts/only_template.py") == STATUS_MISSING, \
                rows.get("scripts/only_template.py")
            assert rows.get("scripts/only_local.py") == STATUS_EXTRA, \
                rows.get("scripts/only_local.py")
        return "新版 / 旧版 / 时间相同 / 缺失 / 多余 / 一致 六种状态判定正确"

    def diff_is_read_only() -> str:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            tpl = base / "template"
            proj = base / "project"
            for root in (tpl, proj):
                (root / "src" / "lib").mkdir(parents=True, exist_ok=True)
            target_tpl = tpl / "src" / "lib" / "anim.ts"
            target_proj = proj / "src" / "lib" / "anim.ts"
            target_tpl.write_text("template v2\n", encoding="utf-8")
            target_proj.write_text("local v1\n", encoding="utf-8")
            os.utime(target_proj, (1_000_000, 1_000_000))
            os.utime(target_tpl, (2_000_000, 2_000_000))
            before = target_proj.read_bytes()
            rows = compare(tpl, proj)
            with contextlib.redirect_stdout(io.StringIO()) as captured:
                print_report(rows, tpl, proj, want_diff=True)
            assert target_proj.read_bytes() == before, "--diff 改了本地文件内容（必须只读）"
            assert "本地/src/lib/anim.ts" in captured.getvalue(), "--diff 没打印文本差异"
        return "--diff 只读（本地文件字节不变）且能打印差异"

    def apply_is_safe() -> str:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            tpl = base / "template"
            proj = base / "project"
            for root in (tpl, proj):
                (root / "scripts").mkdir(parents=True, exist_ok=True)
            # a：模板更新 → 默认应覆盖并先备份本地旧内容
            (tpl / "scripts" / "a.py").write_text("template new\n", encoding="utf-8")
            (proj / "scripts" / "a.py").write_text("local old\n", encoding="utf-8")
            os.utime(proj / "scripts" / "a.py", (1_000_000, 1_000_000))
            os.utime(tpl / "scripts" / "a.py", (2_000_000, 2_000_000))
            # b：本地更新 → 默认跳过（不覆盖），但要备份
            (tpl / "scripts" / "b.py").write_text("template old\n", encoding="utf-8")
            (proj / "scripts" / "b.py").write_text("local new\n", encoding="utf-8")
            os.utime(tpl / "scripts" / "b.py", (1_000_000, 1_000_000))
            os.utime(proj / "scripts" / "b.py", (2_000_000, 2_000_000))
            # c：本地独有 → 多余，永不删除
            (proj / "scripts" / "c.py").write_text("local only\n", encoding="utf-8")

            rows = compare(tpl, proj)
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                try:
                    apply_changes(rows, tpl, proj, force=False)
                except SystemExit as exc:
                    assert exc.code == 1, f"跳过了本地较新的文件时应退出 1，实际 {exc.code}"
                else:
                    raise AssertionError("有 OLD 文件被跳过时应非零退出")

            assert (proj / "scripts" / "a.py").read_text(encoding="utf-8") == "template new\n", \
                "模板更新的文件没同步"
            assert (proj / "scripts" / "b.py").read_text(encoding="utf-8") == "local new\n", \
                "本地较新的文件被默认覆盖了"
            assert (proj / "scripts" / "c.py").read_text(encoding="utf-8") == "local only\n", \
                "多余（本地独有）文件被删了"
            backups = sorted((proj / BACKUP_ROOT).rglob("*.py"))
            backup_texts = {path.read_text(encoding="utf-8") for path in backups}
            assert "local old\n" in backup_texts, f"被覆盖前没备份本地旧内容：{backups}"
            assert "local new\n" in backup_texts, f"跳过前没备份本地较新内容：{backups}"

            # 幂等：只剩「模板更新」的文件被同步后，再 apply 应 0 覆盖、不新增备份
            tpl2 = base / "template2"
            proj2 = base / "project2"
            for root in (tpl2, proj2):
                (root / "scripts").mkdir(parents=True, exist_ok=True)
            (tpl2 / "scripts" / "a.py").write_text("v2\n", encoding="utf-8")
            (proj2 / "scripts" / "a.py").write_text("v1\n", encoding="utf-8")
            os.utime(proj2 / "scripts" / "a.py", (1_000_000, 1_000_000))
            os.utime(tpl2 / "scripts" / "a.py", (2_000_000, 2_000_000))
            rows2 = compare(tpl2, proj2)
            with contextlib.redirect_stdout(io.StringIO()):
                copied = apply_changes(rows2, tpl2, proj2, force=False)
            assert copied == 1 and (proj2 / "scripts" / "a.py").read_text(encoding="utf-8") == "v2\n"
            with contextlib.redirect_stdout(io.StringIO()):
                copied_again = apply_changes(compare(tpl2, proj2), tpl2, proj2, force=False)
            assert copied_again == 0, f"没有差异时应 0 覆盖，实际 {copied_again}"
        return "apply：备份 / 默认跳过本地较新 / 不删多余 / 重复执行幂等"

    named_checks = (
        ("同步分类不重叠（通用 vs 项目自有）", classification),
        ("通用文件集合稳定 + 项目自有边界守卫", common_set_stable),
        ("路径 / 模板目录归一化", path_normalization),
        ("compare 六种状态判定", compare_states),
        ("--diff 只读且能打印差异", diff_is_read_only),
        ("--apply 安全语义（备份 / 跳过 / 不删 / 幂等）", apply_is_safe),
    )
    for name, func in named_checks:
        check(name, func)

    print("")
    if failures:
        for failure in failures:
            sys.stderr.write(f"ERROR: selftest — {failure}\n")
        sys.stderr.write(f"ERROR: selftest — {len(failures)} 项失败（共 {total} 项）\n")
        return 1
    print(f"OK: selftest — 同步分类 / 路径归一化 / compare 状态 / --diff 只读 / "
          f"--apply 安全 全部通过（{total} 项）")
    return 0


# ===========================================================================
# main
# ===========================================================================

def main(argv=None) -> int:
    _force_utf8()
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    if "--selftest" in raw_argv:
        return _selftest()
    parser = argparse.ArgumentParser(
        prog="sync_template.py",
        description="用新版 promo-forge 模板更新已有项目（只同步通用文件，不碰项目自有文件）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "同步范围：%s、scripts 下的 .py/.mjs/.js、src/components/*.tsx\n"
            "永不触碰：%s\n"
            "边界：模板里既不同步、也不在 PROJECT_OWNED 的文件，报告末尾会单独列为「未纳入比对」。\n"
            "纯函数自测（不读模板/不写文件）：python scripts/sync_template.py --selftest\n"
            "退出码：0 正常完成（有差异不算失败）；1 出错，或 --apply 时跳过了"
            "「本地比模板新 / 修改时间相同」的文件（没同步完，看清 WARN 再加 --force）。"
            % ("、".join(COMMON_FILES), "、".join(PROJECT_OWNED))
        ),
    )
    parser.add_argument("--template", default=None,
                        help="模板目录，或 skill 根目录（会用它的 template/ 子目录）")
    parser.add_argument("--diff", action="store_true",
                        help=f"打印文本差异摘要（每个文件前 {DIFF_MAX_LINES} 行）")
    parser.add_argument("--apply", action="store_true",
                        help=f"用模板覆盖本地通用文件（先备份到 {BACKUP_ROOT}/<时间戳>/）")
    parser.add_argument("--force", action="store_true",
                        help="配合 --apply：连「本地比模板新 / 修改时间相同」的文件也覆盖"
                             "（默认跳过它们，避免冲掉你的改动）")
    parser.add_argument("--selftest", action="store_true",
                        help="纯函数自测（不定位模板/不写文件）：python scripts/sync_template.py --selftest")
    args = parser.parse_args(raw_argv)

    template_dir, source = locate_template(args.template)
    project_dir = PROJECT_ROOT.resolve()

    if template_dir == project_dir:
        error(
            f"当前目录就是模板本体（{template_dir}），没什么可同步的。"
            "请到你的项目目录（含 src/timing.json 的副本）里运行本脚本。"
        )

    if not (project_dir / "src" / "timing.json").is_file():
        warn(
            f"{project_dir} 下没有 src/timing.json，看起来不像是 promo-forge 项目。"
            "继续比对，但请确认你没有在错误的目录里跑这个脚本"
        )

    log(f"定位到模板：{template_dir}（来源：{source}）")
    log("")

    rows = compare(template_dir, project_dir)
    print_report(rows, template_dir, project_dir, want_diff=args.diff)

    # 「未纳入比对」清单：模板里有、但既不在同步白名单、也不在 PROJECT_OWNED 的文件。
    # 白名单漏一项，模板对它的修复就永远同步不到已有项目，而旧报告只会说「N 一致」。
    uncovered = find_uncovered_template_files(template_dir, collect_common_files(template_dir))
    log("")
    if uncovered:
        warn(f"模板里有 {len(uncovered)} 个文件未纳入比对（既不同步、也不在 PROJECT_OWNED 里）：")
        for relative in uncovered[:20]:
            log(f"       {relative}")
        if len(uncovered) > 20:
            log(f"       …（还有 {len(uncovered) - 20} 个）")
        log("    通用文件请加进 sync_template.py 的 COMMON_FILES / COMMON_GLOBS：")
        log("    项目自有的请加进 PROJECT_OWNED。否则模板对它们的修复永远同步不到已有项目。")
    else:
        log("未纳入比对：0 个模板文件（同步范围 + PROJECT_OWNED 已覆盖模板的 scripts/、src/、web/）")

    if args.apply:
        apply_changes(rows, template_dir, project_dir, force=args.force)
        log("")
        warn(
            "同步的是**公共**文件，你的项目里可能已经按需改过它们。请务必重跑复核：\n"
            "       npm run lint            # 类型检查，确认脚本改版后 TS 侧仍然对得上\n"
            "       python scripts/mix.py   # 混音，确认响度与 ducking 仍达标\n"
            "       python scripts/check_assets.py\n"
            "    有疑问就用上面的备份目录回滚。"
        )
    else:
        log("")
        log("这是只读报告，本地文件一个都没改。要真正覆盖请加 --apply（会先备份；"
            "「本地比模板新 / 修改时间相同」的文件仍会跳过，除非再加 --force）。")

    log("")
    check_index_css(project_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
