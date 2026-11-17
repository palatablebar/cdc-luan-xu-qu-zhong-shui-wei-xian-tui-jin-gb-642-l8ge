"""发布结论：差额桥 + 发布门槛 → 一份页面与导出共用的结论对象。

发布门槛（任一不过即不可发布，结论与差额照样落库供复核）：

1. 锚点齐全：每个输入文件都带声明行数与声明金额（无锚不定发）；
2. 仓内四账守恒：行账 / 键账 / 快照账 / 金额账全部平；
3. 行残差为 0：声明 − 传输缺失 − 实收 = 0（多发少发都不行）；
4. 金额差额可归因：声明 − 实收的差额为 0，或全部由不可读/缺失行承载。
"""
from __future__ import annotations

from decimal import Decimal

from ... import reasons
from . import bridge as bridge_mod

ZERO = Decimal("0.00")

BASELINE = "source_declared"
BASELINE_NOTE = ("对账基准：源端声明总数为外部锚点，仓端折叠结果为实绩；"
                 "差额经差额桥逐项归因，残差为 0 才可发布")


def build(inputs: list[dict], events: list[dict], totals: dict,
          table_order: tuple = ()) -> dict:
    anchor = bridge_mod.input_anchor(inputs)
    classes = bridge_mod.event_classes(events)
    rows = bridge_mod.row_bridge(anchor, classes, totals)
    amounts = bridge_mod.amount_bridge(anchor, classes, totals)
    tables = bridge_mod.per_table(inputs, classes, table_order)

    declared_rows = anchor["declared_rows"]
    declared_amount = anchor["declared_amount"]
    missing = anchor["missing_rows"]
    received = classes["received"]
    received_amount = classes["received_amount"]
    residual_rows = None if declared_rows is None else declared_rows - missing - received
    unattributed = None if declared_amount is None else declared_amount - received_amount
    carriers = list(classes["carriers"])
    for item in anchor["files"]:
        if item["gap"] and item["gap"] > 0:
            carriers.append(f"{item['name']} 缺失 {item['gap']} 行")

    gates = _gates(anchor, totals, residual_rows, unattributed, len(carriers))
    publishable = all(g["ok"] for g in gates)
    code = "R_PUBLISHABLE" if publishable else next(
        g["code"] for g in gates if not g["ok"])
    return {
        "version": 1,
        "baseline": BASELINE,
        "baseline_note": BASELINE_NOTE,
        "status": "publishable" if publishable else "blocked",
        "publishable": publishable,
        "code": code,
        "verdict": _verdict(publishable, code, declared_rows, declared_amount,
                            totals, missing, gates),
        "residual_rows": residual_rows,
        "unattributed_amount": None if unattributed is None else str(unattributed),
        "carriers": carriers,
        "gates": gates,
        "row_bridge": _jsonable(rows),
        "amount_bridge": _jsonable(amounts),
        "per_table": _jsonable(tables),
        "inputs": _jsonable(anchor["files"]),
    }


def _gates(anchor: dict, totals: dict, residual_rows, unattributed,
           carrier_count: int) -> list[dict]:
    missing_meta = anchor["rows_missing_meta"] + anchor["amount_missing_meta"]
    anchor_ok = not missing_meta
    internal_ok = str(totals.get("reconcile_ok") or "0") == "1"
    rows_ok = residual_rows == 0
    amount_ok = (unattributed is not None
                 and (unattributed == ZERO or (unattributed > ZERO and carrier_count > 0)))
    return [
        {"key": "anchor", "code": "R_ANCHOR_MISSING", "label": "源端锚点齐全",
         "ok": anchor_ok,
         "detail": ("每个文件都有声明行数与金额" if anchor_ok
                    else "缺声明：" + "、".join(missing_meta))},
        {"key": "internal", "code": "R_INTERNAL_UNBALANCED", "label": "仓内四账守恒",
         "ok": internal_ok,
         "detail": "行账/键账/快照账/金额账全部平" if internal_ok else "四账存在不平"},
        {"key": "residual_rows", "code": "R_UNEXPLAINED_GAP", "label": "行残差为 0",
         "ok": rows_ok,
         "detail": ("声明 − 传输缺失 = 实收" if rows_ok
                    else f"残差 {residual_rows} 行（声明 − 缺失 − 实收）")},
        {"key": "amount", "code": "R_UNEXPLAINED_AMOUNT", "label": "金额差额可归因",
         "ok": amount_ok,
         "detail": _amount_detail(unattributed, carrier_count, amount_ok)},
    ]


def _amount_detail(unattributed, carrier_count: int, ok: bool) -> str:
    if unattributed is None:
        return "缺声明金额"
    if unattributed == ZERO:
        return "声明金额 = 实收金额"
    if ok:
        return f"差额 {unattributed} 由 {carrier_count} 行不可读/缺失行承载"
    return f"差额 {unattributed} 无可归因行"


def _verdict(publishable: bool, code: str, declared_rows, declared_amount,
             totals: dict, missing: int, gates: list[dict]) -> str:
    snapshot_keys = bridge_mod.integer(totals.get("snapshot_keys"))
    amount_snapshot = bridge_mod.money(totals.get("amount_snapshot"))
    if publishable:
        text = (f"可发布：源端声明 {declared_rows} 行 / {declared_amount} 元 → "
                f"快照 {snapshot_keys} 键 / {amount_snapshot} 元，差额全部归因")
        if missing:
            text += f"；含传输缺失 {missing} 行，已披露"
        return text
    failed = next(g for g in gates if not g["ok"])
    return f"不可发布：{failed['label']}——{failed['detail']}（{reasons.label(code)}）"


def _jsonable(value):
    if isinstance(value, Decimal):
        return f"{value:.2f}"
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_jsonable(v) for v in value]
    return value
