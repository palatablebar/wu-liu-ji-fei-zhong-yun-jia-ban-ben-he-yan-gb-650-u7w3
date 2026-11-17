"""M4 入口：计费重页、计费重计算与查询、上游磅重更新重算。"""

from __future__ import annotations

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse

from app import store, web
from app.modules.m4_weights import billable, service

router = APIRouter(tags=["weights"])


@router.get("/weights", response_class=HTMLResponse)
def page_weights(request: Request):
    context = web.shell(request, active="weights")
    run_id = context["run_id"]
    context.update({
        "lines": service.lines(run_id) if run_id else [],
        "summary": service.summary(run_id) if run_id else {},
        "labels": billable.SOURCE_LABEL,
    })
    return web.page("weights.html", context)


@router.get("/api/weights")
def api_weights(run: str = ""):
    run_id = run or store.latest_run_id()
    return {
        "ok": True,
        "run_id": run_id,
        "summary": service.summary(run_id) if run_id else {},
        "lines": service.lines(run_id) if run_id else [],
    }


@router.post("/api/weights")
def api_compute(run: str = ""):
    run_id = run or store.latest_run_id()
    return {"ok": True, "result": service.compute_weights(run_id)}


@router.get("/api/weights/events")
def api_weight_events(run: str = "", waybill_no: str = ""):
    run_id = run or store.latest_run_id()
    return {"ok": True, "run_id": run_id, "events": service.events(run_id, waybill_no)}


@router.post("/api/weights/recompute")
def api_recompute(waybill_no: str = Form(...), raw_weight: str = Form(...),
                  unit: str = Form("lb"), reason: str = Form("上游磅重更新"),
                  run: str = Form("")):
    """上游磅重更新：旧值归档 → 重新换算/择版/进位；已核账单只标记冲突不改金额。"""
    run_id = run or store.latest_run_id()
    result = service.recompute_waybill(run_id, waybill_no, raw_weight, unit, reason)
    return {"ok": True, "result": result}
