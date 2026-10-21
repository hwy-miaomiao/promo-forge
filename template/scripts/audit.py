#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""promo-forge 决策与成本审计（AGENTS.md 阶段 9 / 第 7 节闸门之一）。

「默认零付费」不能只是一句口号：谁在什么时候、为了什么拍了板、花了多少钱、
用了哪个付费服务，必须能在一份 ``decisions.json`` 里逐项对账。本脚本负责：

1. 维护项目根的 ``decisions.json``（谁拍板 / 何时 / 花了多少 / 用没用付费服务）；
2. 校验结构完整、金额不超预算（默认预算 **0 元**，可用 ``PROMO_BUDGET_CNY`` 或
   ``decisions.json`` 的 ``budget_limit_cny`` 或 ``--budget`` 调高）；
3. 阶段 2 的需求确认必须落成一条 ``user_requirement``（缺它 = ERROR，防止跳过
   问询直接开工）；启用付费服务（ElevenLabs / 付费曲库 / 付费素材 / 付费 API）
   必须逐项记账并写服务名（花了钱却没写服务名 = ERROR），累计金额超过预算即
   ERROR（`check_assets.py` / `check_video.py` 会把它当交付闸门）。

``decisions.json`` 位置：项目根目录（与 ``src/timing.json`` 同级）。

    {
      "schema_version": 1,
      "budget_limit_cny": 0,
      "decisions": [
        {
          "id": "D001",
          "stage": 2,
          "kind": "user_requirement",
          "question": "目标产品与受众确认",
          "decision": "用户确认：面向独立开发者的小账本，45 秒竖屏",
          "decided_by": "user",
          "decided_at": "2026-10-21T23:00:00+08:00",
          "cost_cny": 0,
          "paid_service": null,
          "note": ""
        }
      ]
    }

``kind`` 四类：``user_requirement``（需求确认）/ ``user_audio_review``（试听声音）/
``user_fact_check``（只有用户能核实的事实）三类**必须由用户拍板**，``decided_by``
必须是 ``user``；``technical_self``（技术自决：脚本/参数/素材处理）由执行者记。
其中 ``user_requirement`` **必须存在**（阶段 2 的唯一强制卡点）；另外两类是
条件校验：项目涉及配音/配乐试听或需要核实事实时才记录，记录了就强制由用户拍板。

用法：
    python scripts/audit.py init [--budget 0] [--force]
    python scripts/audit.py append --kind technical_self --decision "……"
    python scripts/audit.py append --kind user_audio_review --decision "用户选定音色 B"
    python scripts/audit.py validate [decisions.json] [--budget 0] [--format text|json]
    python scripts/audit.py --selftest        # 纯函数自测：不读真实项目、不联网

退出码：0 通过（0 ERROR）；1 有 ERROR，或文件不存在 / 参数错误。
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import math
import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# 导入公共库 scripts/_common.py
# ---------------------------------------------------------------------------
sys.path.insert(0, str(Path(__file__).resolve().parent))
try:  # pragma: no cover - 只在 _common.py 缺失时走到
    from _common import PROJECT_ROOT, ensure_dir, log  # type: ignore
except ImportError as exc:  # pragma: no cover
    sys.stderr.write(
        "ERROR: 无法导入 scripts/_common.py（%s）。请确认 scripts 目录完整，"
        "并在项目根目录下运行本脚本。\n" % (exc,)
    )
    raise SystemExit(1)


# ===========================================================================
# 常量与 schema
# ===========================================================================

DECISIONS_REL = "decisions.json"
ENV_BUDGET = "PROMO_BUDGET_CNY"
SCHEMA_VERSION = 1
DEFAULT_BUDGET_CNY = 0.0
STAGE_MIN = 0
STAGE_MAX = 11

#: kind → 中文说明。「三类人拍板的事」必须由用户拍板（decided_by = user）。
KIND_LABELS = {
    "user_requirement": "需求确认（用户拍板：产品/受众/时长/平台/风格）",
    "user_audio_review": "试听声音（用户拍板：配音音色 / 配乐候选）",
    "user_fact_check": "事实核实（只有用户能核实的事实：数据、授权、品牌说法）",
    "technical_self": "技术自决（执行者：脚本/参数/素材处理，不涉及用户偏好）",
}
USER_KINDS = ("user_requirement", "user_audio_review", "user_fact_check")
ALL_KINDS = tuple(KIND_LABELS)

_ISO_RE = re.compile(r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}")

#: 金额比较容差（浮点累加后 0.1+0.2 这类误差不该误报超预算）。
MONEY_EPS = 1e-9


# ===========================================================================
# 基础工具（全部纯函数，方便 --selftest 覆盖）
# ===========================================================================

def _res(level: str, item: str, message: str) -> Dict[str, str]:
    """构造一条检查结果。"""
    return {"level": level, "item": item, "message": message}


def _is_number(value: Any) -> bool:
    """只认真正的 JSON 数字：bool / 字符串 / NaN / Infinity 都不算。"""
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
    )


def parse_money(value: Any) -> Tuple[Optional[float], Optional[str]]:
    """把金额解析成非负有限浮点；返回 (金额, 错误说明)。

    ``None`` / 空串按 0 处理（旧文件里没有 cost_cny 视为免费）。
    """
    if value is None or (isinstance(value, str) and not value.strip()):
        return 0.0, None
    if isinstance(value, bool):
        return None, "布尔值不是金额"
    if isinstance(value, str):
        text = value.strip().replace(",", "").replace("，", "")
        try:
            parsed = float(text)
        except ValueError:
            return None, "不是数字"
    elif isinstance(value, (int, float)):
        parsed = float(value)
    else:
        return None, "必须是数字（单位：人民币元）"
    if not math.isfinite(parsed):
        return None, "必须是有限数字（NaN / Infinity 不算）"
    if parsed < 0:
        return None, "不能为负数"
    return parsed, None


def parse_iso8601(text: Any) -> Tuple[Optional[_dt.datetime], Optional[str]]:
    """解析 ISO 8601 时间；带 ``Z`` 的 UTC 时间也接受。返回 (datetime, 错误说明)。"""
    if not isinstance(text, str) or not text.strip():
        return None, "不是非空字符串"
    raw = text.strip()
    if not _ISO_RE.match(raw):
        return None, "必须写成 YYYY-MM-DDTHH:MM 形式（建议带 +08:00 时区）"
    normalized = raw[:-1] + "+00:00" if raw.endswith("Z") else raw
    try:
        return _dt.datetime.fromisoformat(normalized), None
    except ValueError:
        return None, "不是合法的 ISO 8601 时间"


def now_iso() -> str:
    """当前本地时间的 ISO 8601 字符串（带时区）。"""
    return _dt.datetime.now().astimezone().isoformat(timespec="seconds")


def decisions_path(root: Optional[Path] = None) -> Path:
    """decisions.json 的路径（默认项目根）。"""
    base = Path(root) if root is not None else PROJECT_ROOT
    return base / DECISIONS_REL


def load_decisions_file(path: Path) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """读 decisions.json；返回 (data, 错误说明)。文件不存在也会返回错误说明。"""
    target = Path(path)
    if not target.is_file():
        return None, "找不到 %s" % (target,)
    try:
        raw = target.read_bytes()
        text = raw.decode("utf-8-sig")
        data = json.loads(text)
    except (OSError, UnicodeDecodeError) as exc:
        return None, "读不了 %s：%s" % (target, exc)
    except json.JSONDecodeError as exc:
        return None, "%s 不是合法 JSON：%s" % (target, exc)
    return data, None


def resolve_budget(
    data: Any, env: Optional[Dict[str, str]] = None
) -> Tuple[float, str, Optional[str]]:
    """解析预算上限。返回 (金额, 来源说明, 错误说明)。

    优先级：环境变量 ``PROMO_BUDGET_CNY`` > ``decisions.json`` 的
    ``budget_limit_cny`` > 默认 0 元（零付费路线）。
    """
    env_map = os.environ if env is None else env
    try:
        raw_env = env_map.get(ENV_BUDGET)  # type: ignore[union-attr]
    except AttributeError:
        raw_env = None
    if raw_env is not None and str(raw_env).strip() != "":
        value, err = parse_money(raw_env)
        if err is not None:
            return DEFAULT_BUDGET_CNY, "环境变量 %s" % ENV_BUDGET, (
                "环境变量 %s=%r 不是合法金额（%s）" % (ENV_BUDGET, raw_env, err))
        return value or 0.0, "环境变量 %s" % ENV_BUDGET, None
    raw = data.get("budget_limit_cny") if isinstance(data, dict) else None
    if raw is None:
        return DEFAULT_BUDGET_CNY, "默认值（零付费路线）", None
    value, err = parse_money(raw)
    if err is not None:
        return DEFAULT_BUDGET_CNY, "decisions.json 的 budget_limit_cny", (
            "decisions.json 的 budget_limit_cny 不是合法金额（%s）" % (err,))
    return value or 0.0, "decisions.json 的 budget_limit_cny", None


def total_cost_cny(entries: Any) -> float:
    """累计已记账金额（只统计合法金额；非法金额由 validate 单独报 ERROR）。"""
    total = 0.0
    if not isinstance(entries, list):
        return total
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        value, err = parse_money(entry.get("cost_cny"))
        if err is None and value is not None:
            total += value
    return total


def audit_summary(data: Any, budget_cny: Optional[float] = None) -> Dict[str, Any]:
    """给 check_assets / check_video / MCP 用的一行摘要。

    ``budget_cny`` 非空时用它当预算（来源标成 ``--budget 参数``），保证
    ``--budget`` 覆盖后 summary 里的预算与校验判定用的是同一个数。
    """
    entries = data.get("decisions") if isinstance(data, dict) else None
    rows = entries if isinstance(entries, list) else []
    if budget_cny is not None:
        parsed, parse_err = parse_money(budget_cny)
        if parse_err is not None:
            budget, source, budget_err = DEFAULT_BUDGET_CNY, "--budget 参数", (
                "--budget 不是合法金额（%s）" % (parse_err,))
        else:
            budget, source, budget_err = parsed or 0.0, "--budget 参数", None
    else:
        budget, source, budget_err = resolve_budget(data)
    return {
        "count": len(rows),
        "total_cost_cny": round(total_cost_cny(rows), 2),
        "budget_cny": round(budget, 2),
        "budget_source": source,
        "budget_error": budget_err,
        "paid_decisions": sum(
            1 for entry in rows
            if isinstance(entry, dict) and isinstance(entry.get("paid_service"), str)
            and entry.get("paid_service", "").strip()
        ),
    }


# ===========================================================================
# 校验（核心，纯函数）
# ===========================================================================

def validate_decisions(
    data: Any,
    budget_cny: Optional[float] = None,
) -> List[Dict[str, str]]:
    """校验 decisions.json 的内容，返回 ERROR / WARN 列表（没有 OK 行）。

    规则要点：
    - 顶层是对象、``decisions`` 是非空数组、``schema_version`` = 1；
    - 每条：id 唯一、stage 0~11、kind 合法、``decision`` 非空、``decided_at`` 是
      ISO 8601、``cost_cny`` 是 ≥0 的有限数、``paid_service`` 是字符串或 null；
    - 三类人拍板的事（user_*）必须 ``decided_by = user``；
    - 累计金额超预算 = ERROR；启用付费服务却记 0 元 = WARN（免费额度要写清）。
    """
    results: List[Dict[str, str]] = []

    if not isinstance(data, dict):
        return [_res("ERROR", "决策审计", "decisions.json 顶层必须是 JSON 对象")]

    # --- 预算 ---
    if budget_cny is not None:
        budget, budget_err = parse_money(budget_cny)
        budget_src = "--budget 参数"
    else:
        budget, budget_src, budget_err = resolve_budget(data)
    if budget_err is not None:
        results.append(_res("ERROR", "预算", budget_err))
        budget = DEFAULT_BUDGET_CNY

    # --- schema_version ---
    schema_version = data.get("schema_version")
    if schema_version is None:
        results.append(_res("WARN", "schema_version", "缺 schema_version，按 1 兼容处理"))
    elif (
        isinstance(schema_version, bool)
        or not isinstance(schema_version, int)
        or schema_version != SCHEMA_VERSION
    ):
        results.append(_res(
            "ERROR", "schema_version",
            "schema_version=%r 不认识（本脚本只支持 %d）" % (schema_version, SCHEMA_VERSION)))

    decisions = data.get("decisions")
    if not isinstance(decisions, list):
        results.append(_res("ERROR", "decisions", "decisions 必须是数组"))
        return results
    if not decisions:
        results.append(_res(
            "ERROR", "decisions",
            "decisions 为空：谁拍板 / 花了多少钱还没有任何记录。"
            "先跑 python scripts/audit.py init，再用 append 逐项记账"))
        return results

    seen_ids = set()
    has_user_requirement = False
    total = 0.0
    paid_count = 0

    for index, entry in enumerate(decisions, start=1):
        label = "第 %d 条决策" % (index,)
        if not isinstance(entry, dict):
            results.append(_res("ERROR", label, "必须是 JSON 对象"))
            continue

        # id（唯一）
        entry_id = entry.get("id")
        if not isinstance(entry_id, str) or not entry_id.strip():
            results.append(_res("ERROR", label, "id 必须是非空字符串"))
        elif entry_id.strip() in seen_ids:
            results.append(_res("ERROR", label, "id=%r 重复（每条决策 id 必须唯一）" % entry_id.strip()))
        else:
            seen_ids.add(entry_id.strip())

        # stage
        stage = entry.get("stage")
        if isinstance(stage, bool) or not isinstance(stage, int) or not (
                STAGE_MIN <= stage <= STAGE_MAX):
            results.append(_res(
                "ERROR", label,
                "stage=%r 必须是 %d~%d 的整数（对应 AGENTS.md 阶段号）"
                % (stage, STAGE_MIN, STAGE_MAX)))

        # kind
        kind = entry.get("kind")
        if kind not in ALL_KINDS:
            results.append(_res(
                "ERROR", label,
                "kind=%r 不认识（允许：%s）" % (kind, " / ".join(ALL_KINDS))))
        elif kind == "user_requirement":
            has_user_requirement = True

        # decided_by
        decided_by = entry.get("decided_by")
        if not isinstance(decided_by, str) or not decided_by.strip():
            results.append(_res("ERROR", label, "decided_by 必须非空（谁拍板）"))
        elif kind in USER_KINDS and decided_by.strip().lower() not in ("user", "用户"):
            results.append(_res(
                "ERROR", label,
                "kind=%r 是必须用户拍板的三类事之一，decided_by 必须是 user（实际 %r）"
                % (kind, decided_by.strip())))

        # decision 内容
        decision = entry.get("decision")
        if not isinstance(decision, str) or not decision.strip():
            results.append(_res("ERROR", label, "decision 必须是非空字符串（拍板结论）"))

        # decided_at（何时）
        decided_at = entry.get("decided_at")
        if decided_at is None or (isinstance(decided_at, str) and not decided_at.strip()):
            results.append(_res("ERROR", label, "缺 decided_at（何时拍板）"))
        elif not isinstance(decided_at, str):
            results.append(_res("ERROR", label, "decided_at 必须是 ISO 8601 字符串"))
        else:
            parsed, parse_err = parse_iso8601(decided_at)
            if parse_err is not None:
                results.append(_res("ERROR", label, "decided_at 不合法：%s" % (parse_err,)))
            elif parsed is not None and parsed.tzinfo is None:
                results.append(_res(
                    "WARN", label,
                    "decided_at 没带时区（建议 +08:00 或 Z，跨时区对账才不歧义）"))

        # cost_cny（花了多少）
        cost: Optional[float] = 0.0
        if "cost_cny" in entry and entry.get("cost_cny") is not None:
            cost, cost_err = parse_money(entry.get("cost_cny"))
            if cost_err is not None:
                results.append(_res("ERROR", label, "cost_cny 不是合法金额（%s）" % (cost_err,)))
                cost = None
        if cost is not None:
            total += cost

        # paid_service（用没用付费服务）
        paid_service = entry.get("paid_service")
        if paid_service is not None and not isinstance(paid_service, str):
            results.append(_res("ERROR", label, "paid_service 必须是字符串（服务名）或 null"))
        elif isinstance(paid_service, str) and paid_service.strip():
            paid_count += 1
            if cost is not None and cost <= 0:
                results.append(_res(
                    "WARN", label,
                    "paid_service=%r 却记 cost_cny=0（免费额度？请在 note 写清计价依据）"
                    % (paid_service.strip(),)))
        elif cost is not None and cost > 0:
            # 花了钱却没写用在哪：无法对账，零付费路线的「悄悄花钱」必须拦。
            results.append(_res(
                "ERROR", label,
                "cost_cny=%.2f 但 paid_service 为空：付了钱必须写清是哪个付费服务"
                "（免费额度请记 cost_cny=0 并写服务名 + note）" % (cost,)))

        # note
        note = entry.get("note")
        if note is not None and not isinstance(note, str):
            results.append(_res("ERROR", label, "note 必须是字符串"))

    # --- 累计金额 vs 预算 ---
    if budget_err is None and budget is not None:
        if total > budget + MONEY_EPS:
            results.append(_res(
                "ERROR", "预算",
                "累计 ¥%.2f 超过预算上限 ¥%.2f（预算来源：%s）" % (total, budget, budget_src)))
    if paid_count and total == 0.0 and budget_err is None and budget == 0.0:
        # 逐条已 WARN 过；这里再给一条汇总，说明「零付费路线被破坏」的性质。
        results.append(_res(
            "WARN", "预算",
            "记录了 %d 条付费服务但累计 0 元：零付费路线下必须确认用的是免费额度" % (paid_count,)))

    if not has_user_requirement:
        # 需求确认是 AGENTS.md 阶段 2 的唯一强制卡点：没有它就不该开工/交付。
        results.append(_res(
            "ERROR", "三类人拍板",
            "还没有 user_requirement 记录：阶段 2 的需求确认必须由用户拍板并落账"
            "（audit.py append --kind user_requirement --decision \"…\"）"))

    return results


# ===========================================================================
# 文件维护（init / append）
# ===========================================================================

def _write_json(path: Path, data: Any) -> None:
    """写 UTF-8 JSON（LF 行尾、中文不转义、带结尾换行）。"""
    ensure_dir(Path(path).parent)
    text = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    with open(str(path), "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)


def init_decisions(
    root: Optional[Path] = None,
    budget: float = DEFAULT_BUDGET_CNY,
    force: bool = False,
) -> Tuple[Optional[Path], Optional[str]]:
    """初始化 decisions.json，返回 (路径, 错误说明)。

    初始文件里只放一条 ``technical_self``（零付费路线基线）。``init`` 之后
    ``validate`` 仍会因为缺 ``user_requirement`` 报 ERROR —— 这是有意的：阶段 2
    的需求确认必须由用户拍板，``init`` 只是把账本建好，不能替用户确认需求。
    """
    target = decisions_path(root)
    if target.exists() and not force:
        return None, "%s 已存在（要重建请加 --force）" % (target,)
    budget_value, budget_err = parse_money(budget)
    if budget_err is not None:
        return None, "--budget 不是合法金额（%s）" % (budget_err,)
    budget_value = budget_value or 0.0
    if budget_value > 0:
        baseline = (
            "启用带预算路线：预算上限 ¥%.2f；付费服务必须逐项记账，"
            "超预算即交付闸门 ERROR" % (budget_value,))
    else:
        baseline = (
            "沿用模板默认零付费路线（预算上限 ¥0）：只允许免费工具"
            "（edge-tts / 免费可商用曲库 / Remotion / 本地 ffmpeg）")
    data = {
        "schema_version": SCHEMA_VERSION,
        "budget_limit_cny": round(budget_value, 2),
        "created_at": now_iso(),
        "decisions": [
            {
                "id": "D001",
                "stage": 0,
                "kind": "technical_self",
                "question": "项目初始化与费用基线",
                "decision": baseline,
                "decided_by": "agent",
                "decided_at": now_iso(),
                "cost_cny": 0,
                "paid_service": None,
                "note": "由 python scripts/audit.py init 生成",
            }
        ],
    }
    try:
        _write_json(target, data)
    except OSError as exc:
        return None, "写不了 %s：%s" % (target, exc)
    return target, None


def append_decision(
    root: Optional[Path] = None,
    kind: str = "technical_self",
    decision: str = "",
    question: str = "",
    stage: Optional[int] = None,
    decided_by: Optional[str] = None,
    cost: float = 0.0,
    paid_service: Optional[str] = None,
    note: str = "",
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """追加一条决策记录，返回 (记录, 错误说明)。

    三类人拍板的事默认 ``decided_by = user``；技术自决默认 ``agent``；
    stage 默认：用户类决策 = 2，其余 = 9。文件不存在时先自动 init。
    """
    if kind not in ALL_KINDS:
        return None, "kind=%r 不认识（允许：%s）" % (kind, " / ".join(ALL_KINDS))
    if not isinstance(decision, str) or not decision.strip():
        return None, "--decision 不能为空"
    if kind in USER_KINDS:
        default_stage = 2
        default_by = "user"
    else:
        default_stage = 9
        default_by = "agent"
    stage_value = default_stage if stage is None else stage
    if isinstance(stage_value, bool) or not isinstance(stage_value, int) or not (
            STAGE_MIN <= stage_value <= STAGE_MAX):
        return None, "stage 必须是 %d~%d 的整数" % (STAGE_MIN, STAGE_MAX)
    by = default_by if decided_by is None else decided_by
    if not isinstance(by, str) or not by.strip():
        return None, "--by 不能为空"
    cost_value, cost_err = parse_money(cost)
    if cost_err is not None:
        return None, "--cost 不是合法金额（%s）" % (cost_err,)
    if paid_service is not None and not isinstance(paid_service, str):
        return None, "--paid-service 必须是字符串"
    target = decisions_path(root)
    if not target.exists():
        _path, init_err = init_decisions(root=root, budget=DEFAULT_BUDGET_CNY)
        if init_err is not None:
            return None, init_err
    data, load_err = load_decisions_file(target)
    if load_err is not None:
        return None, load_err
    assert data is not None
    entries = data.get("decisions")
    if not isinstance(entries, list):
        return None, "decisions 不是数组，先手工修好 %s" % (target,)
    entry = {
        "id": "D%03d" % (len(entries) + 1,),
        "stage": stage_value,
        "kind": kind,
        "question": question or "",
        "decision": decision.strip(),
        "decided_by": by.strip(),
        "decided_at": now_iso(),
        "cost_cny": round(cost_value or 0.0, 2),
        "paid_service": paid_service.strip() if isinstance(paid_service, str) and paid_service.strip() else None,
        "note": note or "",
    }
    entries.append(entry)
    try:
        _write_json(target, data)
    except OSError as exc:
        return None, "写不了 %s：%s" % (target, exc)
    return entry, None


# ===========================================================================
# 输出
# ===========================================================================

def _emit(level: str, item: str, message: str) -> None:
    """统一输出格式：LEVEL: <检查项> — <说明>。"""
    print("%s: %s — %s" % (level, item, message), flush=True)


def _print_results(results: List[Dict[str, str]], summary: Dict[str, Any], path: Path) -> None:
    _emit("OK" if not any(r["level"] == "ERROR" for r in results) else "ERROR",
          "决策审计", "文件 %s" % (path,))
    for row in results:
        _emit(row["level"], row["item"], row["message"])
    errors = sum(1 for r in results if r["level"] == "ERROR")
    warnings = sum(1 for r in results if r["level"] == "WARN")
    print("", flush=True)
    print(
        "决策审计：%d ERROR / %d WARN（%d 条决策 / 累计 ¥%.2f / 预算 ¥%.2f，来源：%s）"
        % (errors, warnings, summary["count"], summary["total_cost_cny"],
           summary["budget_cny"], summary["budget_source"]),
        flush=True,
    )


def _json_payload(results: List[Dict[str, str]], summary: Dict[str, Any],
                  path: Path, extra: Optional[Dict[str, Any]] = None) -> str:
    errors = sum(1 for r in results if r["level"] == "ERROR")
    warnings = sum(1 for r in results if r["level"] == "WARN")
    payload: Dict[str, Any] = {
        "ok": errors == 0,
        "errors": errors,
        "warnings": warnings,
        "path": str(path),
        "summary": summary,
        "results": results,
    }
    if extra:
        payload.update(extra)
    return json.dumps(payload, ensure_ascii=False)


# ===========================================================================
# 自测（纯函数；不读真实项目、不联网、不花钱）
# ===========================================================================

def _selftest() -> int:
    """纯函数自测；全部通过返回 0，任一失败打印 ERROR 并返回 1。"""
    failures = []

    def check(name: str, got, want) -> None:
        if got != want:
            failures.append("%s: 期望 %r，实际 %r" % (name, want, got))

    # --- 金额解析 ---
    for value, want in (
        (0, 0.0),
        (1.5, 1.5),
        ("2.5", 2.5),
        (" 3 ", 3.0),
        ("1,000", 1000.0),
        (None, 0.0),
        ("", 0.0),
    ):
        parsed, err = parse_money(value)
        check("parse_money(%r)" % (value,), (parsed, err), (want, None))
    for bad in (-1, -0.01, "-3", "abc", True, False, float("nan"), float("inf"), {}, []):
        parsed, err = parse_money(bad)
        check("parse_money(%r) 必须报错" % (bad,), parsed is None and err is not None, True)

    # --- 时间解析 ---
    ok_times = (
        "2026-10-21T23:00:00+08:00",
        "2026-10-21 23:00",
        "2026-10-21T23:00:00Z",
        "2026-10-21T23:00",
    )
    for text in ok_times:
        parsed, err = parse_iso8601(text)
        check("parse_iso8601(%r)" % (text,), parsed is not None and err is None, True)
    for bad in ("", "not-a-date", "2026-13-40T99:00", 20261021, None):
        parsed, err = parse_iso8601(bad)
        check("parse_iso8601(%r) 必须报错" % (bad,), parsed is None and err is not None, True)
    check("parse_iso8601 空格形式无时区", parse_iso8601("2026-10-21 23:00")[0].tzinfo, None)

    # --- 预算优先级 ---
    budget, source, err = resolve_budget({"budget_limit_cny": 5}, env={})
    check("预算取 decisions.json", (budget, err), (5.0, None))
    check("预算来源说明", "budget_limit_cny" in source, True)
    budget, source, err = resolve_budget(
        {"budget_limit_cny": 5}, env={ENV_BUDGET: "12.5"})
    check("环境变量覆盖文件预算", (budget, err), (12.5, None))
    budget, source, err = resolve_budget({}, env={})
    check("默认预算 0 元", (budget, err), (0.0, None))
    budget, source, err = resolve_budget({"budget_limit_cny": "abc"}, env={})
    check("文件预算非法 → 报错", (budget, err is not None), (0.0, True))
    budget, source, err = resolve_budget({}, env={ENV_BUDGET: "nope"})
    check("环境变量预算非法 → 报错", (budget, err is not None), (0.0, True))
    summary = audit_summary({"budget_limit_cny": 0, "decisions": []}, budget_cny=20.0)
    check("summary 预算跟随 --budget 覆盖", summary["budget_cny"], 20.0)
    check("summary 预算来源标记 --budget", summary["budget_source"], "--budget 参数")

    # --- 合法夹具 ---
    def entry(**over):
        base = {
            "id": "D001",
            "stage": 2,
            "kind": "user_requirement",
            "question": "需求确认",
            "decision": "用户确认：45 秒竖屏，免费路线",
            "decided_by": "user",
            "decided_at": "2026-10-21T23:00:00+08:00",
            "cost_cny": 0,
            "paid_service": None,
            "note": "",
        }
        base.update(over)
        return base

    def valid_data():
        return {
            "schema_version": 1,
            "budget_limit_cny": 0,
            "decisions": [
                entry(),
                entry(id="D002", stage=9, kind="technical_self", decided_by="agent",
                      question="脚本参数", decision="用 edge-tts 默认音色",
                      decided_at="2026-10-21T23:05:00+08:00"),
            ],
        }

    def levels(results):
        return (
            sum(1 for r in results if r["level"] == "ERROR"),
            sum(1 for r in results if r["level"] == "WARN"),
        )

    check("合法 decisions → 0 ERROR / 0 WARN", levels(validate_decisions(valid_data())), (0, 0))

    # --- 超预算 ---
    over = valid_data()
    over["budget_limit_cny"] = 0
    over["decisions"][1]["cost_cny"] = 10
    over["decisions"][1]["paid_service"] = "ElevenLabs"
    check("超预算 → ERROR", levels(validate_decisions(over))[0] > 0, True)
    check("超预算错误信息含金额",
          any("超过预算" in r["message"] for r in validate_decisions(over)), True)
    within = valid_data()
    within["budget_limit_cny"] = 10
    within["decisions"][1].update({"cost_cny": 10, "paid_service": "ElevenLabs"})
    check("金额等于预算 → 0 ERROR", levels(validate_decisions(within))[0], 0)
    env_over = valid_data()
    env_over["decisions"][1].update({"cost_cny": 3, "paid_service": "ElevenLabs"})
    # 覆盖参数直接生效（check_video --budget-cny 走这条）
    check("--budget 覆盖：2 元预算花 3 元 → ERROR",
          levels(validate_decisions(env_over, budget_cny=2.0))[0] > 0, True)
    check("--budget 覆盖：3 元预算花 3 元 → 0 ERROR",
          levels(validate_decisions(env_over, budget_cny=3.0))[0], 0)

    # --- 付费服务记账 ---
    paid_free = valid_data()
    paid_free["decisions"][1].update({"cost_cny": 0, "paid_service": "ElevenLabs"})
    check("付费服务记 0 元 → WARN 不拦", levels(validate_decisions(paid_free)), (0, 2))
    unbooked = valid_data()
    unbooked["decisions"][1].update({"cost_cny": 5, "paid_service": None})
    check("花了钱但 paid_service 为空 → ERROR",
          levels(validate_decisions(unbooked))[0] > 0, True)

    # --- 各类结构错误 ---
    check("顶层非对象 → ERROR", levels(validate_decisions([]))[0], 1)
    bad_kind = valid_data()
    bad_kind["decisions"][0]["kind"] = "nope"
    check("未知 kind → ERROR", any("kind" in r["message"] for r in validate_decisions(bad_kind)), True)
    user_by_agent = valid_data()
    user_by_agent["decisions"][0]["decided_by"] = "agent"
    check("三类人拍板由 agent 记 → ERROR",
          any("必须是 user" in r["message"] for r in validate_decisions(user_by_agent)), True)
    dup = valid_data()
    dup["decisions"].append(entry(id="D001"))
    check("id 重复 → ERROR",
          any("重复" in r["message"] for r in validate_decisions(dup)), True)
    bad_stage = valid_data()
    bad_stage["decisions"][0]["stage"] = 12
    check("stage 越界 → ERROR",
          any("stage" in r["message"] for r in validate_decisions(bad_stage)), True)
    empty = {"schema_version": 1, "decisions": []}
    check("decisions 空 → ERROR", levels(validate_decisions(empty))[0], 1)
    bad_schema = valid_data()
    bad_schema["schema_version"] = 2
    check("schema_version 错 → ERROR", levels(validate_decisions(bad_schema))[0], 1)
    no_at = valid_data()
    del no_at["decisions"][0]["decided_at"]
    check("缺 decided_at → ERROR",
          any("decided_at" in r["message"] for r in validate_decisions(no_at)), True)
    naive = valid_data()
    naive["decisions"][0]["decided_at"] = "2026-10-21T23:00:00"
    check("decided_at 无时区 → WARN",
          levels(validate_decisions(naive))[1] > 0, True)
    no_user = {"schema_version": 1, "decisions": [
        entry(id="D002", kind="technical_self", decided_by="agent")]}
    check("缺 user_requirement → ERROR",
          any(r["level"] == "ERROR" and "user_requirement" in r["message"]
              for r in validate_decisions(no_user)), True)
    only_tech = {"schema_version": 1, "decisions": [
        entry(id="D002", kind="technical_self", decided_by="agent")]}
    check("只有技术自决 → ERROR（需求确认未落账）",
          levels(validate_decisions(only_tech))[0] > 0, True)

    # --- 累计金额 ---
    check("total_cost_cny 累加", total_cost_cny(valid_data()["decisions"]), 0.0)
    check("total_cost_cny 忽略非法金额",
          total_cost_cny([{"cost_cny": 1.5}, {"cost_cny": "x"}, {"cost_cny": 2}]), 3.5)

    # --- init / append 写盘（临时目录，不碰真实项目）---
    import tempfile

    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        path, init_err = init_decisions(root=root, budget=0)
        check("init 成功", (str(path) if path else None, init_err),
              (str(root / DECISIONS_REL), None))
        data, load_err = load_decisions_file(root / DECISIONS_REL)
        check("init 后文件可读", (isinstance(data, dict), load_err), (True, None))
        check("init 后缺 user_requirement → ERROR",
              levels(validate_decisions(data))[0] > 0, True)
        check("init 基线无其他 WARN", levels(validate_decisions(data))[1], 0)
        entry_added, append_err = append_decision(
            root=root, kind="user_requirement", decision="用户确认：45 秒竖屏")
        check("append 成功", (entry_added is not None, append_err), (True, None))
        data, _ = load_decisions_file(root / DECISIONS_REL)
        check("append 后 0 ERROR / 0 WARN", levels(validate_decisions(data)), (0, 0))
        check("append 自动编号", entry_added["id"], "D002")
        check("用户类决策默认 decided_by=user", entry_added["decided_by"], "user")
        check("init 不覆盖已有文件",
              init_decisions(root=root)[1] is not None, True)
        paid_entry, paid_err = append_decision(
            root=root, kind="technical_self", decision="试用付费音色",
            cost=5, paid_service="ElevenLabs")
        check("append 付费服务", (paid_entry is not None, paid_err), (True, None))
        data, _ = load_decisions_file(root / DECISIONS_REL)
        check("付费但预算 0 → ERROR", levels(validate_decisions(data))[0] > 0, True)
        bad_append, bad_append_err = append_decision(
            root=root, kind="nope", decision="x")
        check("append 未知 kind → 报错", bad_append is None and bad_append_err is not None, True)

    if failures:
        for message in failures:
            sys.stderr.write("ERROR: selftest — %s\n" % (message,))
        sys.stderr.write("ERROR: selftest — %d 项失败\n" % (len(failures),))
        return 1
    print("OK: selftest — 金额/时间解析 / 预算优先级与 --budget 摘要 / 需求确认必填 / "
          "三类人拍板 / 付费服务必记账 / 超预算拦截 / init+append 全部通过")
    return 0


# ===========================================================================
# main
# ===========================================================================

def _force_utf8() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):
                pass


def _resolve_root(raw: Optional[str]) -> Path:
    return Path(raw) if raw else PROJECT_ROOT


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="audit.py",
        description="promo-forge 决策与成本审计（AGENTS.md 阶段 9 / 第 7 节）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "输出格式：LEVEL: <检查项> — <说明>；最后一行统计。\n"
            "退出码：0 通过（0 ERROR）；1 有 ERROR 或文件不存在。\n"
            "纯函数自测：python scripts/audit.py --selftest"
        ),
    )
    parser.add_argument("--selftest", action="store_true", help="只跑纯函数自测")
    sub = parser.add_subparsers(dest="cmd", metavar="子命令")

    init_p = sub.add_parser("init", help="初始化 decisions.json（默认零付费基线）")
    init_p.add_argument("--root", default=None, help="项目根目录，默认当前项目（PROMO_ROOT / scripts 上级）")
    init_p.add_argument("--budget", type=float, default=DEFAULT_BUDGET_CNY,
                        help="预算上限（元），默认 0（零付费路线）")
    init_p.add_argument("--force", action="store_true", help="已存在时覆盖重建")

    append_p = sub.add_parser("append", help="追加一条决策记录")
    append_p.add_argument("--root", default=None, help="项目根目录，默认当前项目")
    append_p.add_argument("--kind", required=True, choices=ALL_KINDS,
                          help="决策类型：三类人拍板用 user_*，技术自决用 technical_self")
    append_p.add_argument("--decision", required=True, help="拍板结论（写清楚决定了什么）")
    append_p.add_argument("--question", default="", help="要解决什么问题")
    append_p.add_argument("--stage", type=int, default=None,
                          help="对应的 AGENTS.md 阶段号（用户类默认 2，其余默认 9）")
    append_p.add_argument("--by", default=None, help="谁拍板（用户类默认 user，其余默认 agent）")
    append_p.add_argument("--cost", type=float, default=0.0, help="花了多少钱（人民币元），默认 0")
    append_p.add_argument("--paid-service", default=None,
                          help="用了哪个付费服务（免费额度也写服务名并在 note 说明）")
    append_p.add_argument("--note", default="", help="备注（计价依据 / 免费额度说明）")

    validate_p = sub.add_parser("validate", help="校验 decisions.json 与预算")
    validate_p.add_argument("path", nargs="?", default=None, help="decisions.json 路径（默认项目根）")
    validate_p.add_argument("--root", default=None, help="项目根目录，默认当前项目")
    validate_p.add_argument("--budget", type=float, default=None,
                            help="临时覆盖预算上限（默认 PROMO_BUDGET_CNY / decisions.json / 0 元）")
    validate_p.add_argument("--format", choices=("text", "json"), default="text",
                            help="输出格式：text（人看）或 json（程序 / MCP 用）")
    return parser


def main(argv=None) -> int:
    _force_utf8()
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    if "--selftest" in raw_argv:
        return _selftest()
    parser = build_parser()
    args = parser.parse_args(raw_argv)
    if not getattr(args, "cmd", None):
        parser.print_help()
        return 1

    if args.cmd == "init":
        root = _resolve_root(args.root)
        if not root.is_dir():
            sys.stderr.write("ERROR: 项目根目录不存在：%s\n" % (root,))
            return 1
        path, err = init_decisions(root=root, budget=args.budget, force=args.force)
        if err is not None:
            sys.stderr.write("ERROR: %s\n" % (err,))
            return 1
        print("已初始化决策审计文件：%s" % (path,), flush=True)
        print("下一步：用户拍板的三类事请用 append --kind user_* 逐项记账。", flush=True)
        return 0

    if args.cmd == "append":
        root = _resolve_root(args.root)
        entry, err = append_decision(
            root=root, kind=args.kind, decision=args.decision,
            question=args.question, stage=args.stage, decided_by=args.by,
            cost=args.cost, paid_service=args.paid_service, note=args.note)
        if err is not None:
            sys.stderr.write("ERROR: %s\n" % (err,))
            return 1
        assert entry is not None
        print("已追加决策 %s（%s，%s）：%s" % (
            entry["id"], entry["kind"], entry["decided_by"], entry["decision"]), flush=True)
        return 0

    # validate
    root = _resolve_root(args.root)
    path = Path(args.path) if args.path else decisions_path(root)
    data, load_err = load_decisions_file(path)
    if load_err is not None:
        results = [_res("ERROR", "决策审计", "%s；先跑 python scripts/audit.py init"
                        % (load_err,))]
        summary = {"count": 0, "total_cost_cny": 0.0, "budget_cny": DEFAULT_BUDGET_CNY,
                   "budget_source": "未知", "budget_error": None, "paid_decisions": 0}
        if args.format == "json":
            print(_json_payload(results, summary, path), flush=True)
        else:
            _print_results(results, summary, path)
        return 1
    assert data is not None
    budget_value = args.budget
    if budget_value is not None:
        parsed, budget_err = parse_money(budget_value)
        if budget_err is not None:
            sys.stderr.write("ERROR: --budget 不是合法金额（%s）\n" % (budget_err,))
            return 1
        budget_value = parsed
    results = validate_decisions(data, budget_cny=budget_value)
    summary = audit_summary(data, budget_cny=budget_value)
    if args.format == "json":
        print(_json_payload(results, summary, path), flush=True)
    else:
        _print_results(results, summary, path)
    return 0 if not any(r["level"] == "ERROR" for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
