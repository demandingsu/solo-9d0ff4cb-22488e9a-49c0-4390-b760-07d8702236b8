"""逐小时净电量计算。

规则（与需求逐条对应）：
- 每小时净电量 = 总表相邻整点差值 - 直接下级表差值之和；孙表及更深不再扣；
- 参与表（总表 + 全部直接下级）缺任一整点 -> 该小时只给待核原因；
- 任一参与表读数倒退（差值为负）-> 该小时只给待核原因；
- 净值为负 -> 该小时只给待核原因。

`compute_day_details` 保留"数据是否可用"和"净值本身"两个维度，供改接核查
同时观察负数与缺/倒退；`compute_daily_net` 再把它映射为对外查询的既有语义。
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone

from .parsing import TS_FORMAT

EPS = 1e-9  # 浮点容差：|x| <= EPS 视为 0


def hour_str(day: date, hour: int) -> str:
    dt = datetime.combine(day, time(hour % 24), tzinfo=timezone.utc)
    if hour == 24:
        dt += timedelta(days=1)
    return dt.strftime(TS_FORMAT)


def compute_day_details(
    meter_id: str,
    day: date,
    children: list[str],
    readings: dict[tuple[str, str], float],
) -> list[dict]:
    """计算 24 个小时的明细，不丢弃负净值。

    返回每小时：
      hour_start, data_ok,
      master_delta_kwh, children_delta_kwh, net_kwh（data_ok 时才有值）,
      missing, backwards（数据不可用的原因，均为字符串列表）。

    readings: {(meter_id, ts_utc): kwh}，由调用方在同一事务快照内取出。
    """
    participants = [meter_id, *children]
    hours: list[dict] = []

    for h in range(24):
        t0, t1 = hour_str(day, h), hour_str(day, h + 1)
        detail = {
            "hour_start": t0,
            "data_ok": False,
            "master_delta_kwh": None,
            "children_delta_kwh": None,
            "net_kwh": None,
            "missing": [],
            "backwards": [],
        }

        # 1) 参与表缺任一整点
        missing = [
            f"表 {m} 缺 {t}" for m in participants for t in (t0, t1) if (m, t) not in readings
        ]
        if missing:
            detail["missing"] = missing
            hours.append(detail)
            continue

        # 2) 读数倒退
        deltas = {m: readings[(m, t1)] - readings[(m, t0)] for m in participants}
        backwards = [
            f"表 {m} 读数倒退: {t0}={readings[(m, t0)]} -> {t1}={readings[(m, t1)]}"
            for m in participants
            if deltas[m] < -EPS
        ]
        if backwards:
            detail["backwards"] = backwards
            hours.append(detail)
            continue

        master_delta = deltas[meter_id]
        children_delta = sum(deltas[c] for c in children)
        detail["data_ok"] = True
        detail["master_delta_kwh"] = master_delta
        detail["children_delta_kwh"] = children_delta
        detail["net_kwh"] = master_delta - children_delta
        hours.append(detail)

    return hours


def describe_issue(detail: dict) -> str | None:
    """把明细里的缺/倒退原因拼成人类可读文本；数据可用时返回 None。"""
    if detail["data_ok"]:
        return None
    if detail["missing"]:
        return "缺整点读数: " + "; ".join(detail["missing"])
    return "; ".join(detail["backwards"])


def compute_daily_net(
    meter_id: str,
    day: date,
    children: list[str],
    readings: dict[tuple[str, str], float],
) -> list[dict]:
    """计算 24 个小时的对外净电量结果（负净值只给待核原因）。"""
    hours: list[dict] = []

    for detail in compute_day_details(meter_id, day, children, readings):
        entry = {
            "hour_start": detail["hour_start"],
            "status": "ok",
            "net_kwh": None,
            "master_delta_kwh": None,
            "children_delta_kwh": None,
            "review_reason": None,
        }

        if not detail["data_ok"]:
            entry["status"] = "pending_review"
            entry["review_reason"] = describe_issue(detail)
            hours.append(entry)
            continue

        master_delta = detail["master_delta_kwh"]
        children_delta = detail["children_delta_kwh"]
        net = detail["net_kwh"]
        entry["master_delta_kwh"] = master_delta
        entry["children_delta_kwh"] = children_delta

        # 净值为负
        if net < -EPS:
            entry["status"] = "pending_review"
            entry["review_reason"] = (
                f"净电量为负: 总表差值 {master_delta} - 直接下级差值和 "
                f"{children_delta} = {net}"
            )
        else:
            entry["net_kwh"] = 0.0 if abs(net) <= EPS else net
        hours.append(entry)

    return hours
