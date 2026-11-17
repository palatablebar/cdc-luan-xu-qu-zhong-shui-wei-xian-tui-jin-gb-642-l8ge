"""M8 入口：发布结论查询、正式日表导出、对账发布页。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request, Response

from ... import errors
from ...web import Ctx, get_ctx, render
from ..m7_export import json_out
from . import service

router = APIRouter(tags=["M8 对账发布"])

JSON_MEDIA = "application/json; charset=utf-8"


@router.get("/api/recon")
def api_recon(batch_id: str | None = None, ctx: Ctx = Depends(get_ctx)) -> dict:
    batch = ctx.current_batch(batch_id)
    if batch is None:
        return {"batch_id": None, "publish": service.blank_conclusion(),
                "published": None}
    return {
        "batch_id": batch["batch_id"],
        "publish": service.conclusion_for(ctx.store, batch["batch_id"]),
        "published": service.published_pointer(ctx.store),
    }


@router.get("/api/export/daily.json")
def export_daily(ctx: Ctx = Depends(get_ctx)) -> Response:
    """正式日表：当前发布版的结论与证据；新批次不可发布时仍发上一版。"""
    if ctx.store.latest_batch() is None:
        raise errors.NotFound("还没有批次，先采信一次", code="E_NO_BATCH")
    document, blocked_newer = service.daily_document(ctx.store)
    if document is None:
        latest = ctx.store.latest_batch()
        publish = service.conclusion_for(ctx.store, latest["batch_id"])
        raise errors.Conflict(
            f"没有可发布的批次：{publish['verdict']}",
            code=publish["code"] if publish["code"].startswith("R_") else "R_UNEXPLAINED_GAP",
            detail={"batch_id": latest["batch_id"], "verdict": publish["verdict"],
                    "gates": publish["gates"]})
    text = json_out.dumps(document)
    suffix = "" if blocked_newer is None else "-superseded"
    filename = f"{document['batch_id']}-daily{suffix}.json"
    return Response(content=text.encode("utf-8"), media_type=JSON_MEDIA,
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'})


@router.get("/recon")
def page_recon(request: Request, ctx: Ctx = Depends(get_ctx)):
    batch = ctx.current_batch()
    publish = (service.conclusion_for(ctx.store, batch["batch_id"]) if batch
               else service.blank_conclusion())
    return render(request, "recon.html", batch=batch, page="recon",
                  publish=publish,
                  published=service.published_pointer(ctx.store),
                  totals=ctx.store.load_totals(batch["batch_id"]) if batch else {},
                  page_title="对账发布")
