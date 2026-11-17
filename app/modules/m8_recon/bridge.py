"""差额桥：源端声明 → 仓端实绩，每一步差额都归因到具名原因。

裁决（docs/DATA-DICTIONARY.md 第 10 节）：对账基准是**源端声明总数**（外部锚点），
仓端按键折叠的结果是实绩；两者之间的差额必须被下面的桥逐项解释，残差为 0 才可发布。

行桥：声明 − 传输缺失 = 实收 − 重复 − 冲突 − 隔离 = 已应用 − 版本折叠 = 键 − 待定 = 快照键
金额桥：声明 − 不可读/缺失行承载 = 实收 − 重复 − 冲突 − 隔离 = 已应用事件金额
        − 版本折叠/作废 = 折叠金额（= 快照 + 待定 + 变化）

本模块只有纯函数：输入是 inputs / events / totals 的普通字典，输出是可 JSON 化的桥。
"""
from __future__ import annotations

import json
from decimal import Decimal

# 内容不可读的隔离行：金额无法解析，其源端金额只能由声明承载
UNREADABLE_CODES = {"E_BAD_JSON", "E_TAIL_TRUNCATED", "E_COLUMN_COUNT", "E_TYPE_MISMATCH"}

ZERO = Decimal("0.00")


def money(value) -> Decimal:
    if value is None or value == "":
        return ZERO
    return Decimal(str(value))


def integer(value) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def normalize_input(item: dict) -> dict:
    """run 时与读库时两种形状的输入行统一成一份。"""
    tables = item.get("declared_tables") or {}
    if isinstance(tables, str):
        tables = json.loads(tables or "{}")
    declared_amount = item.get("declared_amount")
    return {
        "name": item.get("name") or "",
        "lines": integer(item.get("lines")),
        "code": item.get("code"),
        "declared_rows": item.get("declared_rows"),
        "declared_amount": None if declared_amount in (None, "") else money(declared_amount),
        "declared_tables": tables,
    }


def normalize_event(row: dict) -> dict:
    return {
        "row_ref": row.get("row_ref") or "",
        "table": row.get("table") or row.get("table_name") or "",
        "row_class": row.get("row_class") or "",
        "reason_code": row.get("reason_code"),
        "amount": money(row.get("amount")) if row.get("amount") is not None else None,
    }


def input_anchor(inputs: list[dict]) -> dict:
    """源端锚点：声明行数/金额合计、传输缺失与锚点缺口。"""
    declared_rows = 0
    declared_amount = ZERO
    rows_missing_meta: list[str] = []
    amount_missing_meta: list[str] = []
    missing_rows = 0
    files: list[dict] = []
    for item in inputs:
        declared = item["declared_rows"]
        received = item["lines"]
        gap = None if declared is None else declared - received
        if declared is None:
            rows_missing_meta.append(item["name"])
        else:
            declared_rows += declared
            missing_rows += max(gap, 0)
        if item["declared_amount"] is None:
            amount_missing_meta.append(item["name"])
        else:
            declared_amount += item["declared_amount"]
        files.append({**item, "gap": gap,
                      "truncated": bool(gap) and "E_TAIL_TRUNCATED" in (item.get("code") or "")})
    return {
        "declared_rows": None if rows_missing_meta else declared_rows,
        "declared_amount": None if amount_missing_meta else declared_amount,
        "missing_rows": missing_rows,
        "rows_missing_meta": rows_missing_meta,
        "amount_missing_meta": amount_missing_meta,
        "files": files,
    }


def event_classes(events: list[dict]) -> dict:
    """按分类聚合实收行：计数、金额、不可读行（金额载体）。"""
    out = {
        "received": len(events),
        "received_amount": ZERO,
        "counts": {"applied": 0, "duplicate": 0, "conflict": 0, "quarantine": 0},
        "amounts": {"applied": ZERO, "duplicate": ZERO, "conflict": ZERO, "quarantine": ZERO},
        "carriers": [],
        "by_table": {},
    }
    for row in events:
        klass = row["row_class"] if row["row_class"] in out["counts"] else "applied"
        out["counts"][klass] += 1
        if row["amount"] is not None:
            out["received_amount"] += row["amount"]
            out["amounts"][klass] += row["amount"]
        elif klass == "quarantine" and row["reason_code"] in UNREADABLE_CODES:
            out["carriers"].append(row["row_ref"])
        bucket = out["by_table"].setdefault(
            row["table"], {"rows": 0, "amount": ZERO})
        bucket["rows"] += 1
        if row["amount"] is not None:
            bucket["amount"] += row["amount"]
    return out


def row_bridge(anchor: dict, classes: dict, totals: dict) -> list[dict]:
    declared = anchor["declared_rows"]
    missing = anchor["missing_rows"]
    received = classes["received"]
    counts = classes["counts"]
    keys_total = integer(totals.get("keys_total"))
    snapshot_keys = integer(totals.get("snapshot_keys"))
    pending_only = integer(totals.get("pending_only_keys"))
    applied = counts["applied"]
    steps = [
        {"key": "declared", "kind": "total", "label": "源端声明行",
         "value": declared, "note": "各文件头 rows= 合计" if declared is not None else "缺声明"},
        {"key": "missing", "kind": "step", "label": "传输缺失", "value": -missing,
         "note": "声明多于实收，文件级 E_TAIL_TRUNCATED"},
        {"key": "received", "kind": "total", "label": "实收数据行", "value": received,
         "note": "含隔离与冲突行"},
        {"key": "duplicates", "kind": "step", "label": "重复投递",
         "value": -counts["duplicate"], "note": "同签名保留首条"},
        {"key": "conflicts", "kind": "step", "label": "冲突不应用",
         "value": -counts["conflict"], "note": "版本冲突 / 墓碑复活"},
        {"key": "quarantine", "kind": "step", "label": "隔离（作废）",
         "value": -counts["quarantine"], "note": "契约不通过"},
        {"key": "applied", "kind": "total", "label": "已应用事件", "value": applied,
         "note": "进入折叠"},
        {"key": "folding", "kind": "step", "label": "版本折叠",
         "value": -(applied - keys_total), "note": "同键多版本折叠为一键"},
        {"key": "keys", "kind": "total", "label": "键", "value": keys_total, "note": ""},
        {"key": "pending", "kind": "step", "label": "待定窗口", "value": -pending_only,
         "note": "as_of 之后，本次不入快照"},
        {"key": "snapshot", "kind": "total", "label": "快照键", "value": snapshot_keys,
         "note": f"活跃 {integer(totals.get('active_keys'))} + 墓碑 "
                 f"{integer(totals.get('tombstone_keys'))}"},
    ]
    return steps


def amount_bridge(anchor: dict, classes: dict, totals: dict) -> list[dict]:
    declared = anchor["declared_amount"]
    received = classes["received_amount"]
    carried = None if declared is None else declared - received
    amounts = classes["amounts"]
    applied_amount = amounts["applied"]
    folded = money(totals.get("amount_applied"))
    carriers = len(classes["carriers"]) + anchor["missing_rows"]
    steps = [
        {"key": "declared", "kind": "total", "label": "源端声明金额",
         "value": declared, "note": "各文件头 amount= 合计" if declared is not None else "缺声明"},
        {"key": "carried", "kind": "step", "label": "不可读/缺失行承载",
         "value": None if carried is None else -carried,
         "note": f"{carriers} 行金额不可解析或未到达，按声明差额承载"},
        {"key": "received", "kind": "total", "label": "实收金额", "value": received,
         "note": "全部数据行可解析金额合计"},
        {"key": "duplicates", "kind": "step", "label": "重复投递",
         "value": -amounts["duplicate"], "note": ""},
        {"key": "conflicts", "kind": "step", "label": "冲突不应用",
         "value": -amounts["conflict"], "note": ""},
        {"key": "quarantine", "kind": "step", "label": "隔离（作废）",
         "value": -amounts["quarantine"], "note": "可解析部分"},
        {"key": "applied", "kind": "total", "label": "已应用事件金额", "value": applied_amount,
         "note": "含被覆盖的旧版本"},
        {"key": "folding", "kind": "step", "label": "版本折叠 / 作废",
         "value": -(applied_amount - folded), "note": "旧版本被覆盖、墓碑作废"},
        {"key": "folded", "kind": "total", "label": "折叠金额", "value": folded,
         "note": f"= 快照 {money(totals.get('amount_snapshot'))} + 待定 "
                 f"{money(totals.get('amount_pending'))} + 变化 "
                 f"{money(totals.get('amount_drift'))}"},
    ]
    return steps


def per_table(inputs: list[dict], classes: dict, table_order: tuple = ()) -> list[dict]:
    """分表对账：声明（文件头 tables=）vs 实收（按 table 归组），差额用于定位。"""
    declared: dict = {}
    for item in inputs:
        for table, meta in (item["declared_tables"] or {}).items():
            bucket = declared.setdefault(table, {"rows": 0, "amount": ZERO,
                                                 "rows_known": True, "amount_known": True})
            if meta.get("rows") is None:
                bucket["rows_known"] = False
            else:
                bucket["rows"] += integer(meta.get("rows"))
            if meta.get("amount") in (None, ""):
                bucket["amount_known"] = False
            else:
                bucket["amount"] += money(meta.get("amount"))
    received = classes["by_table"]
    names = set(declared) | set(received)
    ordered = [t for t in table_order if t in names]
    ordered += sorted(t for t in names if t and t not in ordered)
    if "" in names:
        ordered.append("")
    rows = []
    for name in ordered:
        decl = declared.get(name)
        recv = received.get(name, {"rows": 0, "amount": ZERO})
        decl_rows = None if decl is None or not decl["rows_known"] else decl["rows"]
        decl_amount = None if decl is None or not decl["amount_known"] else decl["amount"]
        rows.append({
            "table": name or "（未归表）",
            "declared_rows": decl_rows,
            "received_rows": recv["rows"],
            "gap_rows": None if decl_rows is None else decl_rows - recv["rows"],
            "declared_amount": decl_amount,
            "received_amount": recv["amount"],
            "gap_amount": None if decl_amount is None else decl_amount - recv["amount"],
        })
    return rows
