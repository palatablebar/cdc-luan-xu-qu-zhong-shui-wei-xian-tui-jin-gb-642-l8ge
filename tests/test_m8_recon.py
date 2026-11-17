"""M8 对账发布：锚点、差额桥、发布门槛、幂等重放与失败保留上一版。

裁决（docs/DATA-DICTIONARY.md 第 10 节）：对账基准是源端声明总数（外部锚点），
仓端折叠结果为实绩；差额桥逐项归因，残差为 0 才可发布。
"""
from __future__ import annotations

import json
from dataclasses import replace
from decimal import Decimal

import pytest

from app.modules.m6_batches import service
from app.modules.m8_recon import service as m8
from fixtures import expected as X


def _write(directory, name: str, text: str) -> None:
    (directory / name).write_text(text, encoding="utf-8")


def _clean_event(pk: str, amount: str, event_time: str, table: str = "orders") -> str:
    return ('{"table":"%s","pk":"%s","version":1,"op":"upsert",'
            '"event_time":"%s","schema":"v2","payload":{"status":"新建","amount":"%s"}}'
            % (table, pk, event_time, amount))


# ---------- 发布结论（样例批次） ----------

def test_sample_batch_is_publishable(run):
    publish = run["publish"]
    assert publish["status"] == "publishable"
    assert publish["publishable"] is True
    assert publish["code"] == "R_PUBLISHABLE"
    assert publish["baseline"] == "source_declared"
    assert publish["residual_rows"] == 0
    assert publish["unattributed_amount"] == X.CARRIED_AMOUNT
    assert all(g["ok"] for g in publish["gates"])
    assert "43 行" in publish["verdict"] and "6330.01" in publish["verdict"]


def test_row_bridge_telescopes_to_snapshot(run):
    steps = {s["key"]: s["value"] for s in run["publish"]["row_bridge"]}
    assert steps["declared"] == X.DECLARED_ROWS
    assert steps["declared"] + steps["missing"] == steps["received"] == X.RAW_LINES
    assert (steps["received"] + steps["duplicates"] + steps["conflicts"]
            + steps["quarantine"]) == steps["applied"] == X.APPLIED
    assert steps["applied"] + steps["folding"] == steps["keys"] == X.KEYS_TOTAL
    assert steps["keys"] + steps["pending"] == steps["snapshot"] == X.SNAPSHOT_KEYS
    assert steps["missing"] == -X.MISSING_ROWS


def test_amount_bridge_telescopes_to_folded(run):
    steps = {s["key"]: Decimal(str(s["value"])) for s in run["publish"]["amount_bridge"]}
    assert steps["declared"] == Decimal(X.DECLARED_AMOUNT)
    assert steps["declared"] + steps["carried"] == steps["received"]
    assert steps["received"] == Decimal(X.RECEIVED_AMOUNT)
    assert (steps["received"] + steps["duplicates"] + steps["conflicts"]
            + steps["quarantine"]) == steps["applied"]
    assert steps["applied"] == Decimal(X.APPLIED_EVENTS_AMOUNT)
    assert steps["applied"] + steps["folding"] == steps["folded"]
    assert steps["folded"] == Decimal(X.AMOUNT_APPLIED)
    assert steps["folding"] == -Decimal(X.FOLDING_AMOUNT)


def test_declared_meta_stored_on_inputs(settings, store, run):
    inputs = {f["name"]: f for f in store.load_inputs(run["batch"]["batch_id"])}
    main = inputs["events_main.jsonl"]
    assert main["declared_rows"] == X.MAIN_DECLARED_ROWS
    assert main["declared_amount"] == "3523.00"
    tables = json.loads(main["declared_tables"])
    assert tables["orders"] == {"rows": 22, "amount": "3400.00"}
    per_table = {r["table"]: r for r in run["publish"]["per_table"]}
    assert per_table["orders"]["declared_rows"] == X.ORDERS_DECLARED_ROWS
    assert per_table["orders"]["gap_rows"] == X.ORDERS_DECLARED_ROWS - 25
    assert per_table["（未归表）"]["received_rows"] == 3


def test_carriers_are_unreadable_rows(run):
    carriers = run["publish"]["carriers"]
    for ref in X.CARRIER_REFS:
        assert ref in carriers
    assert any("缺失 1 行" in ref for ref in carriers)


# ---------- 幂等重放：重跑同一批不重复计数 ----------

def test_replay_returns_identical_conclusion(settings, store):
    first = service.run(settings=settings, store=store, source="all")
    batch_id = first["batch"]["batch_id"]
    rows_before = store.count_rows(batch_id)
    replay = service.run(settings=settings, store=store, source="all")
    assert replay["idempotent_replay"] is True
    assert replay["publish"] == first["publish"]
    assert replay["totals"] == first["totals"]
    assert store.count_rows(batch_id) == rows_before
    assert len(store.list_batches()) == 1
    stored = m8.conclusion_for(store, batch_id)
    assert stored == first["publish"]


def test_conclusion_recomputed_for_legacy_batch(settings, store, run):
    """老批次没有落库结论：读路径用库存数据按同一函数重算，结果一致。"""
    batch_id = run["batch"]["batch_id"]
    with store.connect() as conn:
        conn.execute("DELETE FROM totals WHERE batch_id=? AND key IN "
                     "('publish_json','publish_ok')", (batch_id,))
    recomputed = m8.conclusion_for(store, batch_id)
    assert recomputed == run["publish"]


# ---------- 发布门槛：无法解释的差额不能发布 ----------

def test_over_delivery_is_blocked(settings, store, tmp_samples):
    """声明 1 行实收 2 行：多出来的行无法归因，禁止发布。"""
    _write(tmp_samples, "over.jsonl",
           "# cdc-export v2 rows=1 amount=1.00 tables=orders:1:1.00 charset=utf-8\n"
           + _clean_event("9001", "1.00", "2026-11-16T08:00:00Z") + "\n"
           + _clean_event("9002", "2.00", "2026-11-16T08:01:00Z") + "\n")
    run = service.run(settings=replace(settings, sample_dir=str(tmp_samples)),
                      store=store, source="over.jsonl")
    publish = run["publish"]
    assert publish["publishable"] is False
    assert publish["code"] == "R_UNEXPLAINED_GAP"
    assert publish["residual_rows"] == -1
    gates = {g["key"]: g for g in publish["gates"]}
    assert gates["residual_rows"]["ok"] is False
    assert store.latest_publishable() is None


def test_missing_anchor_is_blocked(settings, store, tmp_samples):
    """没有声明头的文件：仓端自证再平也不作数（无锚不定发）。"""
    _write(tmp_samples, "plain.jsonl",
           _clean_event("9001", "1.00", "2026-11-16T08:00:00Z") + "\n")
    run = service.run(settings=replace(settings, sample_dir=str(tmp_samples)),
                      store=store, source="plain.jsonl")
    publish = run["publish"]
    assert publish["publishable"] is False
    assert publish["code"] == "R_ANCHOR_MISSING"
    assert publish["residual_rows"] is None
    assert "plain.jsonl" in publish["gates"][0]["detail"]


def test_unexplained_amount_is_blocked(settings, store, tmp_samples):
    """行数对得上但声明金额多出来，且没有不可读行可承载。"""
    _write(tmp_samples, "amount.jsonl",
           "# cdc-export v2 rows=1 amount=99.00 tables=orders:1:99.00 charset=utf-8\n"
           + _clean_event("9001", "1.00", "2026-11-16T08:00:00Z") + "\n")
    run = service.run(settings=replace(settings, sample_dir=str(tmp_samples)),
                      store=store, source="amount.jsonl")
    publish = run["publish"]
    assert publish["publishable"] is False
    assert publish["code"] == "R_UNEXPLAINED_AMOUNT"
    assert publish["unattributed_amount"] == "98.00"
    assert publish["carriers"] == []


def test_truncation_gap_is_publishable_with_disclosure(settings, store, tmp_samples):
    """声明 2 行实收 1 行（截断）：缺失有证据，结论可发布但必须披露。"""
    _write(tmp_samples, "cut.jsonl",
           "# cdc-export v2 rows=2 amount=3.00 tables=orders:2:3.00 charset=utf-8\n"
           + _clean_event("9001", "1.00", "2026-11-16T08:00:00Z") + "\n")
    run = service.run(settings=replace(settings, sample_dir=str(tmp_samples)),
                      store=store, source="cut.jsonl")
    publish = run["publish"]
    assert publish["publishable"] is True
    assert publish["residual_rows"] == 0
    assert "传输缺失 1 行" in publish["verdict"]
    assert any("缺失 1 行" in ref for ref in publish["carriers"])


# ---------- 失败保留上一版结果和证据 ----------

def test_blocked_batch_keeps_previous_publish(settings, store, tmp_samples):
    good = service.run(settings=settings, store=store, source="all")
    good_id = good["batch"]["batch_id"]
    _write(tmp_samples, "over.jsonl",
           "# cdc-export v2 rows=1 amount=1.00 charset=utf-8\n"
           + _clean_event("9001", "1.00", "2026-11-16T10:00:00Z") + "\n"
           + _clean_event("9002", "2.00", "2026-11-16T10:01:00Z") + "\n")
    blocked = service.run(settings=replace(settings, sample_dir=str(tmp_samples)),
                          store=store, source="over.jsonl")
    blocked_id = blocked["batch"]["batch_id"]
    assert blocked["publish"]["publishable"] is False

    # 发布指针仍是上一版，上一版的证据（清单/结论）原样保留
    assert store.latest_publishable()["batch_id"] == good_id
    manifest = store.load_manifest(good_id)
    assert manifest["publish"]["code"] == "R_PUBLISHABLE"
    document, blocked_newer = m8.daily_document(store)
    assert document["batch_id"] == good_id
    assert blocked_newer["batch_id"] == blocked_id


def test_failed_run_keeps_previous_results(settings, store):
    good = service.run(settings=settings, store=store, source="all")
    good_id = good["batch"]["batch_id"]
    rows_before = store.count_rows(good_id)
    with pytest.raises(Exception):
        service.run(settings=settings, store=store, source="no-such-file.jsonl")
    assert store.count_rows(good_id) == rows_before
    assert store.latest_publishable()["batch_id"] == good_id
    assert m8.conclusion_for(store, good_id)["code"] == "R_PUBLISHABLE"


# ---------- 页面与导出共用同一份结论 ----------

def test_recon_page_api_and_exports_share_conclusion(client):
    api = client.get("/api/recon").json()
    assert api["publish"]["code"] == "R_PUBLISHABLE"
    assert api["published"]["batch_id"] == api["batch_id"]

    page = client.get("/recon")
    assert page.status_code == 200
    assert "发布结论" in page.text and "差额桥" in page.text
    assert "可发布" in page.text

    daily = client.get("/api/export/daily.json")
    assert daily.status_code == 200
    document = json.loads(daily.content.decode("utf-8"))
    assert document["kind"] == "cdcwater.daily"
    assert document["conclusion"] == api["publish"]
    assert document["evidence"]["inputs"]

    manifest = json.loads(client.get("/api/export/manifest.json").content.decode("utf-8"))
    assert manifest["publish"] == api["publish"]


def test_daily_endpoint_409_when_nothing_publishable(settings, tmp_samples):
    """唯一的批次不可发布时，正式日表不出货（409），不拿仓端自证充数。"""
    from fastapi.testclient import TestClient

    from app.main import create_app

    _write(tmp_samples, "over.jsonl",
           "# cdc-export v2 rows=1 amount=1.00 charset=utf-8\n"
           + _clean_event("9001", "1.00", "2026-11-16T08:00:00Z") + "\n"
           + _clean_event("9002", "2.00", "2026-11-16T08:01:00Z") + "\n")
    app = create_app(settings, seed=False)
    with TestClient(app) as client:
        store = app.state.store
        service.run(settings=replace(settings, sample_dir=str(tmp_samples)),
                    store=store, source="over.jsonl")
        response = client.get("/api/export/daily.json")
        assert response.status_code == 409
        assert response.json()["error"] == "R_UNEXPLAINED_GAP"
