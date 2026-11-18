"""表改接核查与确认：判定条件、建议过期、与上传交错的场景。"""
from __future__ import annotations

import threading

import pytest
from fastapi.testclient import TestClient

from app.main import create_app

DAY = "2026-01-15"


@pytest.fixture()
def client(tmp_path):
    app = create_app(str(tmp_path / "test.db"))
    with TestClient(app) as c:
        yield c


def hierarchy_csv(edges: list[tuple[str, str]]) -> str:
    lines = ["parent_meter,child_meter"] + [f"{p},{c}" for p, c in edges]
    return "\n".join(lines) + "\n"


def readings_csv(rows: list[tuple[str, str, float]]) -> str:
    lines = ["meter_id,ts_utc,kwh"] + [f"{m},{t},{v}" for m, t, v in rows]
    return "\n".join(lines) + "\n"


def full_day_rows(meter: str, hourly_delta: float, base: float = 1000.0,
                  skip_hours: set[int] | None = None) -> list[tuple[str, str, float]]:
    rows = []
    for h in range(25):
        if skip_hours and h in skip_hours:
            continue
        ts = f"2026-01-{15 + h // 24:02d}T{h % 24:02d}:00:00Z"
        rows.append((meter, ts, base + hourly_delta * h))
    return rows


def upload_hierarchy(client: TestClient, edges: list[tuple[str, str]]):
    return client.post(
        "/api/hierarchy", files={"file": ("h.csv", hierarchy_csv(edges), "text/csv")}
    )


def upload_readings(client: TestClient, rows: list[tuple[str, str, float]]):
    return client.post(
        "/api/readings", files={"file": ("r.csv", readings_csv(rows), "text/csv")}
    )


def check(client: TestClient, child: str, src: str | None, dst: str, day: str = DAY):
    body = {"child_meter": child, "to_parent": dst, "date": day}
    if src is not None:
        body["from_parent"] = src
    return client.post("/api/rewire/check", json=body)


def confirm(client: TestClient, suggestion_id: str):
    return client.post(f"/api/rewire/{suggestion_id}/confirm")


def hierarchy(client: TestClient):
    return client.get("/api/hierarchy").json()


def daily(client: TestClient, meter: str, day: str = DAY):
    resp = client.get(f"/api/meters/{meter}/daily-net", params={"date": day})
    assert resp.status_code == 200, resp.text
    return resp.json()


# 基线场景：M1 总表 40，却带着 C1(30)+C2(20) -> 每小时净 -10（挂错了）；
# M2 总表 100，带 T1(10) -> 90。把 C1 改挂到 M2 后：M1 = 40-20 = 20，M2 = 100-10-30 = 60。
def _setup_negative_master(client: TestClient, *, m1_delta: float = 40.0) -> None:
    assert upload_hierarchy(client, [("M1", "C1"), ("M1", "C2"), ("M2", "T1")]).status_code == 200
    rows = (
        full_day_rows("M1", m1_delta) + full_day_rows("C1", 30.0)
        + full_day_rows("C2", 20.0) + full_day_rows("M2", 100.0)
        + full_day_rows("T1", 10.0)
    )
    assert upload_readings(client, rows).status_code == 200


# ---------- 1. 核查通过并确认 ----------

def test_check_simulates_both_sides_hourly(client):
    _setup_negative_master(client)
    resp = check(client, "C1", "M1", "M2")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "confirmable"
    assert body["suggestion_id"]

    assert body["from_children"] == ["C1", "C2"]
    assert body["to_children"] == ["T1"]
    assert body["from_children_after"] == ["C2"]
    assert body["to_children_after"] == ["C1", "T1"]

    assert body["before_from_hours_ok"] and body["before_to_hours_ok"]
    assert body["after_from_hours_ok"] and body["after_to_hours_ok"]
    assert len(body["before_from_negative_hours"]) == 24
    assert body["after_from_negative_hours"] == []
    assert body["after_to_negative_hours"] == []

    hour = body["hours"][0]
    assert hour["child_delta_kwh"] == pytest.approx(30.0)
    assert hour["from_net_before_kwh"] == pytest.approx(-10.0)
    assert hour["from_net_after_kwh"] == pytest.approx(20.0)    # 40 - 20
    assert hour["to_net_before_kwh"] == pytest.approx(90.0)     # 100 - 10
    assert hour["to_net_after_kwh"] == pytest.approx(60.0)      # 100 - 10 - 30

    # 核查本身不改层级
    assert ("M1", "C1") in [(e["parent_meter"], e["child_meter"]) for e in hierarchy(client)]


def test_confirm_changes_only_that_edge_and_recomputes_history(client):
    _setup_negative_master(client)
    sid = check(client, "C1", "M1", "M2").json()["suggestion_id"]

    resp = confirm(client, sid)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "confirmed"

    assert [(e["parent_meter"], e["child_meter"]) for e in body["hierarchy"]] == [
        ("M1", "C2"), ("M2", "C1"), ("M2", "T1")
    ]
    # 其他边原样保留；历史日期按新层级重算
    m1 = daily(client, "M1")
    assert m1["child_meters"] == ["C2"]
    assert m1["hours"][0]["net_kwh"] == pytest.approx(20.0)
    assert m1["hours"][0]["status"] == "ok"
    m2 = daily(client, "M2")
    assert m2["child_meters"] == ["C1", "T1"]
    assert m2["hours"][0]["net_kwh"] == pytest.approx(60.0)

    # 建议状态与审计
    rec = client.get("/api/rewire/suggestions").json()[0]
    assert rec["status"] == "confirmed" and rec["confirmed_at"]
    assert any("C1 由 M1 改挂到 M2" in (b["reason"] or "")
               for b in client.get("/api/batches").json())

    # 已确认的建议不能再确认一次
    again = confirm(client, sid)
    assert again.status_code == 409
    assert [(e["parent_meter"], e["child_meter"]) for e in hierarchy(client)] == [
        ("M1", "C2"), ("M2", "C1"), ("M2", "T1")
    ]


def test_from_parent_optional_and_inferred_from_current_hierarchy(client):
    _setup_negative_master(client)
    body = check(client, "C1", None, "M2").json()
    assert body["status"] == "confirmable"
    assert body["from_parent"] == "M1"
    # 省略原总表与显式给出当前直属总表得到同一建议
    explicit = check(client, "C1", "M1", "M2").json()
    assert explicit["suggestion_id"] == body["suggestion_id"]


# ---------- 2. 不生成建议的情形 ----------

def test_missing_point_on_either_side_blocks_suggestion(client):
    assert upload_hierarchy(client, [("M1", "C1"), ("M2", "T1")]).status_code == 200
    rows = (
        full_day_rows("M1", 10.0) + full_day_rows("C1", 30.0)
        + full_day_rows("M2", 100.0) + full_day_rows("T1", 10.0, skip_hours={7})
    )
    assert upload_readings(client, rows).status_code == 200

    body = check(client, "C1", "M1", "M2").json()
    assert body["status"] == "not_confirmable"
    assert body["suggestion_id"] is None
    assert any("目标总表参与表全天整点不齐全" in r for r in body["reasons"])
    assert any("T1" in m and "07:00" in m for m in body["missing"])
    assert len(client.get("/api/rewire/suggestions").json()) == 0


def test_backwards_reading_blocks_suggestion(client):
    assert upload_hierarchy(client, [("M1", "C1"), ("M2", "T1")]).status_code == 200
    rows = (
        full_day_rows("M1", 10.0) + full_day_rows("C1", 30.0)
        + full_day_rows("M2", 100.0) + full_day_rows("T1", 10.0)
    )
    rows = [(m, t, v - 500.0 if (m == "M1" and t == "2026-01-15T05:00:00Z") else v)
            for m, t, v in rows]
    assert upload_readings(client, rows).status_code == 200
    body = check(client, "C1", "M1", "M2").json()
    assert body["status"] == "not_confirmable"
    assert any("读数倒退" in b for b in body["backwards"])
    assert any("原总表参与表" in r for r in body["reasons"])


def test_master_without_negative_hour_blocks_suggestion(client):
    # 原总表净值全天为正（C1 挂了但总量够）：没有挂错迹象
    _setup_negative_master(client, m1_delta=100.0)
    body = check(client, "C1", "M1", "M2").json()
    assert body["status"] == "not_confirmable"
    assert body["before_from_negative_hours"] == []
    assert any("没有净值为负" in r for r in body["reasons"])


def test_rewire_creating_negative_on_target_blocks_suggestion(client):
    # M2 总表只有 20，改挂 C1(30) 后目标总表为负 -> 不能建议
    assert upload_hierarchy(client, [("M1", "C1"), ("M2", "T1")]).status_code == 200
    rows = (
        full_day_rows("M1", 10.0) + full_day_rows("C1", 30.0)
        + full_day_rows("M2", 20.0) + full_day_rows("T1", 5.0)
    )
    assert upload_readings(client, rows).status_code == 200
    body = check(client, "C1", "M1", "M2").json()
    assert body["status"] == "not_confirmable"
    assert len(body["before_from_negative_hours"]) == 24
    assert len(body["after_to_negative_hours"]) == 24
    assert any("改接后仍有负净值" in r for r in body["reasons"])


def test_check_rejects_unknown_edge_wrong_parent_and_cycle(client):
    assert upload_hierarchy(client, [("M1", "C1"), ("C1", "G1"), ("M2", "T1")]).status_code == 200
    assert upload_readings(client, full_day_rows("M1", 10.0)).status_code == 200

    assert check(client, "NOPE", "M1", "M2").status_code == 422
    assert check(client, "C1", "M2", "M1").status_code == 409   # 原总表不符
    assert check(client, "C1", "M1", "C1").status_code == 422   # 目标是自身
    assert check(client, "C1", "M1", "G1").status_code == 422   # 目标是自己的后代
    assert check(client, "C1", "M1", "M1").status_code == 422   # 原地不动
    # 失败核查没有改变层级
    assert [(e["parent_meter"], e["child_meter"]) for e in hierarchy(client)] == [
        ("C1", "G1"), ("M1", "C1"), ("M2", "T1")
    ]


# ---------- 3. 建议过期：不得按旧依据改接 ----------

def test_suggestion_expires_when_participating_readings_change(client):
    _setup_negative_master(client)
    sid = check(client, "C1", "M1", "M2").json()["suggestion_id"]

    # 核查后补传一条参与表读数（即使数值不改变核查结论，也算依据变化）
    assert upload_readings(
        client, [("M2", "2026-01-15T03:00:00Z", 1000.0 + 100.0 * 3 + 1.0)]
    ).status_code == 200

    resp = confirm(client, sid)
    assert resp.status_code == 409
    body = resp.json()
    assert body["status"] == "expired"
    assert "参与表读数" in body["message"]
    # 未改接：层级保持原样
    assert [(e["parent_meter"], e["child_meter"]) for e in hierarchy(client)] == [
        ("M1", "C1"), ("M1", "C2"), ("M2", "T1")
    ]
    assert client.get("/api/rewire/suggestions").json()[0]["status"] == "expired"
    # 过期留痕
    assert any(b["status"] == "expired" for b in client.get("/api/batches").json())

    # 过期建议不能重试成功
    assert confirm(client, sid).status_code == 409
    assert ("M1", "C1") in [(e["parent_meter"], e["child_meter"]) for e in hierarchy(client)]


def test_suggestion_expires_when_direct_children_change(client):
    _setup_negative_master(client)
    sid = check(client, "C1", "M1", "M2").json()["suggestion_id"]

    # 整版层级替换：目标总表下多了一只直属表（与核查时的集合不同）
    assert upload_hierarchy(
        client, [("M1", "C1"), ("M1", "C2"), ("M2", "T1"), ("M2", "T2")]
    ).status_code == 200

    resp = confirm(client, sid)
    assert resp.status_code == 409
    assert resp.json()["status"] == "expired"
    assert "直属表集合" in resp.json()["message"]
    # 未改接：C1 仍在 M1 下
    assert ("M1", "C1") in [(e["parent_meter"], e["child_meter"]) for e in hierarchy(client)]


def test_suggestion_expires_when_edge_already_changed(client):
    _setup_negative_master(client)
    sid = check(client, "C1", "M1", "M2").json()["suggestion_id"]

    # 整版替换把 C1 直接挂到别处
    assert upload_hierarchy(
        client, [("M1", "C2"), ("M3", "C1"), ("M2", "T1")]
    ).status_code == 200

    resp = confirm(client, sid)
    assert resp.status_code == 409
    assert resp.json()["status"] == "expired"
    # 不覆盖新状态：C1 仍在 M3 下
    assert ("M3", "C1") in [(e["parent_meter"], e["child_meter"]) for e in hierarchy(client)]
    assert ("M2", "C1") not in [(e["parent_meter"], e["child_meter"]) for e in hierarchy(client)]


def test_unaffected_other_uploads_do_not_expire_suggestion(client):
    _setup_negative_master(client)
    sid = check(client, "C1", "M1", "M2").json()["suggestion_id"]

    # 与两边参与表无关的读数（别的日期、别的表）不影响建议
    assert upload_readings(
        client, [("M1", "2026-01-20T00:00:00Z", 1.0), ("OTHER", "2026-01-15T00:00:00Z", 2.0)]
    ).status_code == 200
    resp = confirm(client, sid)
    assert resp.status_code == 200
    assert resp.json()["status"] == "confirmed"


def test_confirm_unknown_suggestion_404(client):
    assert confirm(client, "does-not-exist").status_code == 404


def test_repeated_check_with_same_basis_reuses_pending_suggestion(client):
    _setup_negative_master(client)
    first = check(client, "C1", "M1", "M2").json()
    second = check(client, "C1", "M1", "M2").json()
    assert first["suggestion_id"] == second["suggestion_id"]
    assert len(client.get("/api/rewire/suggestions").json()) == 1

    # 依据变化后重新核查会得到新建议
    assert upload_readings(
        client, [("M2", "2026-01-15T02:00:00Z", 1000.0 + 100.0 * 2 + 5.0)]
    ).status_code == 200
    third = check(client, "C1", "M1", "M2").json()
    assert third["suggestion_id"] != first["suggestion_id"]


def test_confirm_twice_concurrently_only_one_wins(client):
    _setup_negative_master(client)
    sid = check(client, "C1", "M1", "M2").json()["suggestion_id"]

    results: list[int] = []
    barrier = threading.Barrier(2)

    def worker():
        barrier.wait()
        results.append(confirm(client, sid).status_code)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert sorted(results) == [200, 409], results
    assert [(e["parent_meter"], e["child_meter"]) for e in hierarchy(client)] == [
        ("M1", "C2"), ("M2", "C1"), ("M2", "T1")
    ]


# ---------- 4. 交错场景 ----------

def test_q_interleaved_query_and_confirm_never_sees_half_rewire(client):
    """确认改接期间反复查询：每次查询只能是"旧层级全量"或"新层级全量"。"""
    _setup_negative_master(client)
    sid = check(client, "C1", "M1", "M2").json()["suggestion_id"]

    errors: list[str] = []
    stop = threading.Event()

    def reader():
        while not stop.is_set():
            try:
                m1 = daily(client, "M1")
                m2 = daily(client, "M2")
            except Exception as exc:  # noqa: BLE001
                errors.append(f"query exception: {exc}")
                return
            # m1 与 m2 各自必须内部一致（同一快照）
            m1_children = m1["child_meters"]
            if m1_children not in (["C1", "C2"], ["C2"]):
                errors.append(f"半份层级(原总表): {m1_children}")
                return
            if m1_children == ["C1", "C2"]:
                # 旧层级：净 -10，按既有规则整天待核
                if any(h["status"] != "pending_review" or h["net_kwh"] is not None
                       for h in m1["hours"]):
                    errors.append("旧层级下原总表结果不是全待核")
                    return
                if {h["master_delta_kwh"] for h in m1["hours"]} != {40.0}:
                    errors.append("旧层级下原总表总表差值不单一")
                    return
            else:
                if {h["status"] for h in m1["hours"]} != {"ok"}:
                    errors.append("新层级下原总表仍有待核小时")
                    return
                if {h["net_kwh"] for h in m1["hours"]} != {20.0}:
                    errors.append(f"新层级下原总表净值混杂: {m1['hours'][0]}")
                    return
            if {h["master_delta_kwh"] for h in m1["hours"]} != {40.0}:
                errors.append("原总表总表差值跨了快照")
                return
            # 若 M2 已看到 C1，则 M1 必须已不再扣 C1（改接是单条边原子提交，
            # 但两次独立查询之间可能跨提交，所以这里只校验单次查询内部一致）
            m2_children = m2["child_meters"]
            if m2_children not in (["T1"], ["C1", "T1"]):
                errors.append(f"半份层级(目标总表): {m2_children}")
                return
            expected_m2 = 90.0 if m2_children == ["T1"] else 60.0
            nets2 = {h["net_kwh"] for h in m2["hours"]}
            if nets2 != {expected_m2}:
                errors.append(f"目标总表净值混杂: {m2_children} -> {nets2}")
                return
            if {h["master_delta_kwh"] for h in m2["hours"]} != {100.0}:
                errors.append("目标总表总表差值跨了快照")
                return

    t = threading.Thread(target=reader)
    t.start()
    try:
        resp = confirm(client, sid)
        assert resp.status_code == 200, resp.text
    finally:
        stop.set()
        t.join(timeout=30)
    assert not errors, errors[:3]


def test_q_rewire_and_readings_upload_interleaved(client):
    """核查/确认与读数补传交错：确认要么按原依据成功，要么因依据变化过期。"""
    _setup_negative_master(client)
    sid = check(client, "C1", "M1", "M2").json()["suggestion_id"]

    errors: list[str] = []

    def writer():
        # 不断更正 M2 的读数（参与表之一），每次都改变核查依据
        for n in range(20):
            rows = [("M2", f"2026-01-15T{n % 24:02d}:00:00Z",
                     5000.0 + n * 100.0 + (n % 24) * 100.0)]
            resp = upload_readings(client, rows)
            if resp.status_code != 200:
                errors.append(f"upload failed: {resp.status_code} {resp.text}")
                return

    t = threading.Thread(target=writer)
    t.start()
    resp = confirm(client, sid)
    t.join(timeout=30)

    assert not errors, errors[:3]
    assert resp.status_code in (200, 409)
    edges = [(e["parent_meter"], e["child_meter"]) for e in hierarchy(client)]
    if resp.status_code == 200:
        assert resp.json()["status"] == "confirmed"
        assert ("M2", "C1") in edges
    else:
        assert resp.json()["status"] == "expired"
        assert ("M1", "C1") in edges        # 旧层级保留
        assert ("M2", "C1") not in edges
    # 无论哪种结果，审计里都有记录
    assert any(b["kind"] == "rewire" for b in client.get("/api/batches").json())


def test_q_failed_confirm_keeps_old_hierarchy(client):
    """确认路径内部出错（依据不一致）时，旧层级完整保留、无部分写入。"""
    _setup_negative_master(client)
    sid = check(client, "C1", "M1", "M2").json()["suggestion_id"]
    before = hierarchy(client)

    # 让依据变化：改 C2 的读数（原总表另一只直属表，参与表）
    assert upload_readings(
        client, [("C2", "2026-01-15T10:00:00Z", 9999.0)]
    ).status_code == 200
    assert confirm(client, sid).json()["status"] == "expired"
    assert hierarchy(client) == before


def test_q_queries_never_see_half_hierarchy_replacement(client):
    """整版层级替换与日查询交错：查询只能看到整版旧层级或整版新层级。"""
    _setup_negative_master(client)
    errors: list[str] = []
    stop = threading.Event()

    v1 = [("M1", "C1"), ("M1", "C2"), ("M2", "T1")]
    v2 = [("M1", "C2"), ("M2", "T1"), ("M2", "C1")]

    def writer():
        edges = v1
        while not stop.is_set():
            resp = upload_hierarchy(client, edges)
            if resp.status_code != 200:
                errors.append(f"hierarchy upload failed: {resp.status_code} {resp.text}")
                return
            edges = v2 if edges == v1 else v1

    def reader():
        while not stop.is_set():
            try:
                m1 = daily(client, "M1")
                m2 = daily(client, "M2")
            except Exception as exc:  # noqa: BLE001
                errors.append(f"query exception: {exc}")
                return
            if m1["child_meters"] not in (["C1", "C2"], ["C2"]):
                errors.append(f"半份层级: {m1['child_meters']}")
                return
            if m2["child_meters"] not in (["T1"], ["C1", "T1"]):
                errors.append(f"半份层级: {m2['child_meters']}")
                return
            # 同一查询内部，层级集合与逐小时结果必须自洽
            if m1["child_meters"] == ["C1", "C2"]:
                if any(h["status"] != "pending_review" for h in m1["hours"]):
                    errors.append("旧层级下结果不自洽")
                    return
            elif {h["net_kwh"] for h in m1["hours"]} != {20.0}:
                errors.append("新层级下原总表净值不对")
                return
            if m2["child_meters"] == ["T1"]:
                if {h["net_kwh"] for h in m2["hours"]} != {90.0}:
                    errors.append("旧层级下目标总表净值不对")
                    return

    w = threading.Thread(target=writer)
    r = threading.Thread(target=reader)
    w.start(); r.start()
    import time as _time
    _time.sleep(0.6)
    stop.set()
    r.join(timeout=30); w.join(timeout=30)
    assert not errors, errors[:3]
