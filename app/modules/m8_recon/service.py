"""对账发布编排：一次运行算一份结论落库，页面 / API / 导出读同一份。

- 写路径：M6 运行时调用 build_conclusion，结论随 totals 与 manifest 一起落库；
- 读路径：conclusion_for 先读落库 JSON，老批次没有就用库存数据按同一函数重算；
- 发布指针：latest_publishable 是「当前发布版」，失败或不可发布的新批次顶不掉它。
"""
from __future__ import annotations

import json

from ... import config
from . import bridge, conclusion


def build_conclusion(*, inputs: list[dict], events: list[dict], totals: dict) -> dict:
    """run 时与重算共用同一入口：普通字典进，可 JSON 化的结论出。"""
    norm_inputs = [bridge.normalize_input(item) for item in inputs]
    norm_events = [bridge.normalize_event(row) for row in events]
    return conclusion.build(norm_inputs, norm_events, totals,
                            table_order=tuple(config.TABLES))


def conclusion_for(store, batch_id: str) -> dict:
    totals = store.load_totals(batch_id)
    cached = totals.get("publish_json")
    if cached:
        return json.loads(cached)
    return build_conclusion(inputs=store.load_inputs(batch_id),
                            events=store.load_events(batch_id), totals=totals)


def publish_ok(totals: dict) -> bool:
    return str(totals.get("publish_ok") or "0") == "1"


def published_pointer(store) -> dict | None:
    """当前发布版：最新一个结论为可发布的完成批次。"""
    batch = store.latest_publishable()
    if batch is None:
        return None
    return {"batch_id": batch["batch_id"], "run_id": batch["run_id"],
            "watermark_utc": batch.get("watermark_utc"),
            "finished_at": batch.get("finished_at")}


def blank_conclusion() -> dict:
    """还没有批次时的空态结论（页面不 500）。"""
    return {"version": 1, "baseline": conclusion.BASELINE,
            "baseline_note": conclusion.BASELINE_NOTE,
            "status": "blocked", "publishable": False, "code": "R_ANCHOR_MISSING",
            "verdict": "还没有批次：先采信一次再谈发布",
            "residual_rows": None, "unattributed_amount": None, "carriers": [],
            "gates": [], "row_bridge": [], "amount_bridge": [],
            "per_table": [], "inputs": []}


def daily_document(store) -> tuple[dict | None, dict | None]:
    """正式日表：当前发布版的结论 + 证据指针。

    返回 (document, blocked_newer)：没有可发布批次时 document 为 None；
    最新完成批次不可发布时 blocked_newer 给出它的结论摘要（日表仍是上一版）。
    """
    published = store.latest_publishable()
    if published is None:
        return None, None
    publish = conclusion_for(store, published["batch_id"])
    latest = store.latest_batch()
    blocked_newer = None
    if latest and latest["batch_id"] != published["batch_id"]:
        newer = conclusion_for(store, latest["batch_id"])
        blocked_newer = {"batch_id": latest["batch_id"], "code": newer["code"],
                         "verdict": newer["verdict"]}
    inputs = store.load_inputs(published["batch_id"])
    totals = store.load_totals(published["batch_id"])
    document = {
        "kind": "cdcwater.daily",
        "baseline": conclusion.BASELINE,
        "batch_id": published["batch_id"],
        "run_id": published["run_id"],
        "watermark_utc": published.get("watermark_utc"),
        "finished_at": published.get("finished_at"),
        "conclusion": publish,
        "snapshot": {
            "keys": int(totals.get("snapshot_keys", 0) or 0),
            "active_keys": int(totals.get("active_keys", 0) or 0),
            "tombstone_keys": int(totals.get("tombstone_keys", 0) or 0),
            "amount": totals.get("amount_snapshot", "0.00"),
        },
        "evidence": {
            "inputs": [{"name": f["name"], "sha256": f["sha256"],
                        "declared_rows": f.get("declared_rows"),
                        "declared_amount": f.get("declared_amount")}
                       for f in inputs],
            "manifest": f"/api/export/manifest.json",
            "input_digest": published.get("input_digest"),
        },
        "superseded": blocked_newer,
    }
    return document, blocked_newer
