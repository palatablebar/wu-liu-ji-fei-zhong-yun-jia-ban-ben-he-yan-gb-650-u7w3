"""M5 入口：分区附加页、账单计算与查询、账单核定锁定。"""

from __future__ import annotations

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse

from app import store, web
from app.modules.m5_charges import service

router = APIRouter(tags=["charges"])


@router.get("/charges", response_class=HTMLResponse)
def page_charges(request: Request):
    context = web.shell(request, active="charges")
    run_id = context["run_id"]
    context.update({
        "lines": service.lines(run_id),
        "amounts": service.amounts(run_id) if run_id else {},
    })
    return web.page("charges.html", context)


@router.get("/api/charges")
def api_charges(run: str = ""):
    run_id = run or store.latest_run_id()
    return {
        "ok": True,
        "run_id": run_id,
        "amounts": service.amounts(run_id) if run_id else {},
        "lines": service.lines(run_id),
    }


@router.post("/api/charges")
def api_compute(run: str = ""):
    run_id = run or store.latest_run_id()
    return {"ok": True, "result": service.compute_charges(run_id)}


@router.post("/api/charges/lock")
def api_lock(waybill_no: str = Form(...), run: str = Form("")):
    """核定账单：锁定后重算保留原金额，磅重更新只登记冲突。"""
    run_id = run or store.latest_run_id()
    return {"ok": True, "result": service.lock_charge(run_id, waybill_no)}
