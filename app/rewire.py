"""表改接核查：用现有读数模拟"某分表从原直属总表改挂到目标根总表"。

纯函数，不触碰数据库；调用方负责在事务快照内取好两边的直属表与读数。

核查通过（可确认）的四个条件：
1. 原总表、目标总表各自全天 25 个整点每个小时都齐全（参与表 = 总表 + 直属；
   即改接前后两边的参与表都不缺整点，改接不会引入新的缺口）；
2. 两边参与表全天读数均未倒退；
3. 原总表至少一小时的改接前净值为负；
4. 改接后两边（原总表、目标总表）24 小时净值为负的小时数均为 0。

任一条不满足 -> 不可确认（`confirmable=false`），逐条给出原因。

改接后逐小时：
- 原总表：净值 + 该分表在该小时的差值（不再扣它，故与改接前恒为"回升"）；
- 目标总表：净值 - 该分表在该小时的差值（新增扣减）。
两边若出现负净值只陈述事实，不在此处否决（条件 4 已表达）。
"""
from __future__ import annotations

from .core import EPS, compute_day_details, hour_str


def _issues(details: list[dict]) -> tuple[list[str], list[str]]:
    missing = [m for d in details for m in d["missing"]]
    backwards = [b for d in details for b in d["backwards"]]
    return missing, backwards


def _negative_hours(details: list[dict]) -> list[str]:
    return [d["hour_start"] for d in details if d["data_ok"] and d["net_kwh"] < -EPS]


def simulate_rewire(
    child: str,
    from_parent: str,
    to_parent: str,
    from_children: list[str],
    to_children: list[str],
    readings: dict[tuple[str, str], float],
    day,
) -> dict:
    """返回核查结论；不抛业务异常（参数级错误由调用方先行校验）。"""
    # 改接前：分表挂在原总表下；改接后：原总表少一只直属，目标总表多一只直属
    before_from = compute_day_details(from_parent, day, from_children, readings)
    before_to = compute_day_details(to_parent, day, to_children, readings)

    from_after_children = [c for c in from_children if c != child]
    to_after_children = [*to_children, child]
    after_from = compute_day_details(from_parent, day, from_after_children, readings)
    after_to = compute_day_details(to_parent, day, to_after_children, readings)

    from_ok = all(d["data_ok"] for d in before_from)
    to_ok = all(d["data_ok"] for d in before_to)
    after_from_ok = all(d["data_ok"] for d in after_from)
    after_to_ok = all(d["data_ok"] for d in after_to)

    before_from_neg = _negative_hours(before_from)
    before_to_neg = _negative_hours(before_to)
    after_from_neg = _negative_hours(after_from)
    after_to_neg = _negative_hours(after_to)

    from_missing, from_backwards = _issues(before_from)
    to_missing, to_backwards = _issues(before_to)

    reasons: list[str] = []
    if not from_ok:
        reasons.append("原总表参与表全天整点不齐全或读数倒退")
    if not to_ok:
        reasons.append("目标总表参与表全天整点不齐全或读数倒退")
    if not after_from_ok or not after_to_ok:
        reasons.append("改接后存在参与表缺整点或读数倒退，无法给出完整模拟")
    if not before_from_neg:
        reasons.append("原总表全天没有净值为负的小时，没有挂错总表的迹象")
    if after_from_neg or after_to_neg:
        reasons.append(
            "改接后仍有负净值: "
            + "; ".join(
                [f"原总表 {t}" for t in after_from_neg]
                + [f"目标总表 {t}" for t in after_to_neg]
            )
        )

    confirmable = not reasons

    detail_rows = []
    for idx, (bf, bt, af, at) in enumerate(zip(before_from, before_to, after_from, after_to)):
        t0, t1 = hour_str(day, idx), hour_str(day, idx + 1)
        child_delta = None
        if (child, t0) in readings and (child, t1) in readings:
            child_delta = readings[(child, t1)] - readings[(child, t0)]
        detail_rows.append({
            "hour_start": t0,
            "child_delta_kwh": child_delta,
            "from_net_before_kwh": bf["net_kwh"],
            "from_net_after_kwh": af["net_kwh"],
            "to_net_before_kwh": bt["net_kwh"],
            "to_net_after_kwh": at["net_kwh"],
        })

    return {
        "child_meter": child,
        "from_parent": from_parent,
        "to_parent": to_parent,
        "date": day.isoformat(),
        # 核查时两边的直属表集合（确认时据此比对是否过期）
        "from_children": sorted(from_children),
        "to_children": sorted(to_children),
        "from_children_after": sorted(from_after_children),
        "to_children_after": sorted(to_after_children),
        "before_from_hours_ok": from_ok,
        "before_to_hours_ok": to_ok,
        "after_from_hours_ok": after_from_ok,
        "after_to_hours_ok": after_to_ok,
        "before_from_negative_hours": before_from_neg,
        "before_to_negative_hours": before_to_neg,
        "after_from_negative_hours": after_from_neg,
        "after_to_negative_hours": after_to_neg,
        "missing": from_missing + to_missing,
        "backwards": from_backwards + to_backwards,
        "reasons": reasons,
        "confirmable": confirmable,
        "hours": detail_rows,
    }
