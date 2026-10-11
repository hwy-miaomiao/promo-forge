#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""promo-forge · 用新版模板更新已有项目（sync_template.py）。

用途
----
`template/` 是会被复制成项目的工作模板。项目开工之后你会在副本里改场景、改风格，
但**脚本与公共库**是会随模板一起迭代的——本脚本负责把「与项目风格无关的通用文件」
从新版模板同步回已有项目，同时**绝不碰**属于项目自有的文件。

- **会比对/同步**：`scripts/**/*.py|*.mjs|*.js`、`web/App.tsx`、`web/main.tsx`、
  `web/vite.config.ts`、`src/lib/anim.ts`、`src/lib/beats.ts`。
- **永不触碰**（各项目自有）：`src/timing.json`、`src/scenes/**`、`src/Promo.tsx`、
  `src/lib/theme.ts`、`fonts.json`、`web/config.ts`、`web/style.css`、`remotion.config.ts`。

典型调用
--------
    python scripts/sync_template.py                 # 只报告差异（安全，默认）
    python scripts/sync_template.py --diff          # 差异 + 文本 diff 摘要
    python scripts/sync_template.py --apply         # 覆盖本地通用文件（会先备份）
    python scripts/sync_template.py --apply --force # 连「本地比模板新」的文件也覆盖（危险）
    python scripts/sync_template.py --template D:/promo-forge --apply

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
1 出错（找不到模板、写入失败等，一律打印 `ERROR: ...`）。
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

# 需要比对的「通用文件」——它们只依赖契约（timing.json 的字段），与项目风格无关。
COMMON_FILES = (
    "web/App.tsx",
    "web/main.tsx",
    "web/vite.config.ts",
    "src/lib/anim.ts",
    "src/lib/beats.ts",
)

# 需要比对的目录与后缀（scripts/ 下全部脚本）。
COMMON_GLOBS = (
    ("scripts", (".py", ".mjs", ".js")),
)

# 永不触碰的项目自有文件（写在这里是为了让使用者一眼看到边界，也让 AI 不要自作聪明）。
PROJECT_OWNED = (
    "src/timing.json",
    "src/scenes/**",
    "src/Promo.tsx",
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


# ===========================================================================
# 比较
# ===========================================================================

STATUS_NEW = "新版"
STATUS_OLD = "旧版"
STATUS_MISSING = "缺失"
STATUS_SAME = "一致"
STATUS_EXTRA = "多余"

STATUS_ORDER = (STATUS_NEW, STATUS_OLD, STATUS_MISSING, STATUS_EXTRA, STATUS_SAME)


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
                newer_in_template = template_path.stat().st_mtime >= project_path.stat().st_mtime
            except OSError:
                newer_in_template = True
            rows.append(
                (STATUS_NEW if newer_in_template else STATUS_OLD, relative, template_path, project_path)
            )
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
            if want_diff and status in (STATUS_NEW, STATUS_OLD) and template_path and project_path:
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
        if status in (STATUS_NEW, STATUS_OLD, STATUS_MISSING) and template_path
    ]
    if not pending:
        log("本地通用文件与模板一致，没有需要覆盖的文件。")
        return 0

    # [旧版] = 内容不一样、而本地 mtime 比模板晚：多半是你在项目里按需改过它（改脚本、调参数、
    # 改动画库）。默认不能覆盖（模板会把你的改动冲掉），要覆盖必须显式加 --force。
    backlog = []
    if not force:
        backlog = [item for item in pending if item[0] == STATUS_OLD]
        pending = [item for item in pending if item[0] != STATUS_OLD]
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
        warn(
            f"有 {len(backlog)} 个文件**本地比模板新**，已跳过，没有覆盖：\n"
            + "\n".join(f"       {relative}" for _s, relative, _t, _p in backlog[:10])
            + (f"\n       …（还有 {len(backlog) - 10} 个）" if len(backlog) > 10 else "")
            + "\n    这一栏多半是你自己按需改过的（改脚本、调参数、改动画库），"
              "直接用模板覆盖会把你的改动冲掉，所以默认不动它们。\n"
              "    先看差异：python scripts/sync_template.py --diff\n"
              "    确认可以放弃本地改动，再加 --force 覆盖。"
        )
        if backed_up_local:
            log("")
            log(f"（本地当前内容已备份 {len(backed_up_local)} 个到 {_rel_to(backup_dir, project_dir)}/，"
                "真被覆盖了也能从这里回滚）")
        error(
            f"{len(backlog)} 个文件本地比模板新，默认不覆盖。"
            "确认要放弃这些本地改动请加 --force 重跑。"
        )
    return copied


# ===========================================================================
# 额外检查：src/index.css 的 max-width
# ===========================================================================

def check_index_css(project_dir: Path) -> None:
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
# main
# ===========================================================================

def main(argv=None) -> int:
    _force_utf8()
    parser = argparse.ArgumentParser(
        prog="sync_template.py",
        description="用新版 promo-forge 模板更新已有项目（只同步通用文件，不碰项目自有文件）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "同步范围：%s、scripts 下的 .py/.mjs/.js\n"
            "永不触碰：%s\n"
            "退出码：0 正常完成（有差异不算失败）；1 出错。"
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
                        help="配合 --apply：连「本地比模板新」的文件也覆盖（默认跳过它们，避免冲掉你的改动）")
    args = parser.parse_args(argv)

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
            "「本地比模板新」的文件仍会跳过，除非再加 --force）。")

    log("")
    check_index_css(project_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
