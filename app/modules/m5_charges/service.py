"""M5 编排：按运单组合分区价与附加费，落库账单行并回写运行合计。"""

from __future__ import annotations

from decimal import Decimal

from app import manifest, store
from app.errors import DomainError
from app.modules.m2_rates import service as rates
from app.modules.m2_rates import version as rateversion
from app.modules.m4_weights import billable
from app.modules.m5_charges import addon, money, zone
from app.reasons import sort_codes


def compute_charges(run_id: str) -> dict:
    run = store.get_run(run_id)
    if not run:
        raise DomainError("RUN_NOT_FOUND", f"运行不存在：{run_id}", 404)
    weights = store.weight_lines(run_id)
    if not weights:
        raise DomainError("NO_WEIGHTS", "该运行还没有计费重", 409)
    waybills = {row["waybill_no"]: row for row in store.waybills(run_id)}
    index_map = rates.index()
    locked = store.locked_waybills(run_id)

    lines: list[dict] = []
    skipped_locked = 0
    for weight in weights:
        if weight["source"] == billable.UNBILLED:
            continue
        waybill = waybills.get(weight["waybill_no"])
        if waybill is None:
            continue
        pickup = rateversion.parse_utc(waybill["pickup_utc"])
        card, codes = rates.select_card(
            index_map, waybill["carrier"], waybill["service"], waybill["zone"], pickup
        )
        if card is None:
            continue
        revision = int(weight.get("revision") or 1)
        if weight["waybill_no"] in locked:
            # 已核账单：重算不得改金额。账单行由 store 原样保留，这里只计跳过数。
            skipped_locked += 1
            continue
        steps = int(weight["steps"])
        first = zone.first_amount(card)
        step_part = zone.step_amount(steps, card)
        topup = zone.min_topup(first + step_part, card)
        base = money.q2(first + topup)
        remote = addon.remote_amount(card)
        fuel = addon.fuel_amount(base, step_part, card)
        declared = Decimal(waybill["declared_value"])
        insurance = addon.insurance_amount(declared, card)
        amount = money.total([base, step_part, remote, fuel, insurance])

        row_codes = list(codes)
        if topup > 0:
            row_codes.append("MIN_CHARGE_APPLIED")
        if remote > 0:
            row_codes.append("ZONE_REMOTE")
        if weight["source"] == billable.VOLUMETRIC:
            row_codes.append("VOLUMETRIC_APPLIED")

        lines.append({
            "waybill_no": weight["waybill_no"],
            "version_id": weight["version_id"],
            "base_amount": str(base),
            "step_amount": str(step_part),
            "remote_amount": str(remote),
            "fuel_amount": str(fuel),
            "insurance_amount": str(insurance),
            "total_amount": str(amount),
            "codes": ",".join(sort_codes(row_codes)),
            "locked": 0,
            "locked_at": "",
            "weight_revision": revision,
        })

    # replace_charge_lines 内部会把已锁定行原样写回，金额与核定时一致
    store.replace_charge_lines(run_id, lines)
    grand = money.total([Decimal(row["total_amount"]) for row in store.charge_lines(run_id)])
    now = manifest.utc_now_iso()
    store.upsert_run(run_id, {"amount_total": str(grand), "updated_at": now})
    store.add_stage(run_id, "charges", now)
    return {
        "run_id": run_id,
        "billed": len(store.charge_lines(run_id)),
        "recalculated": len(lines),
        "skipped_locked": skipped_locked,
        "amount_total": str(grand),
        "rate_digest": rates.rate_digest(),
    }


def lock_charge(run_id: str, waybill_no: str) -> dict:
    """核定一票账单：加锁后任何重算都不得改动其金额。"""
    if not store.weight_line(run_id, waybill_no):
        raise DomainError("WAYBILL_NOT_FOUND", f"运单不存在：{waybill_no}", 404)
    changed = store.lock_charge_line(run_id, waybill_no, manifest.utc_now_iso())
    return {"run_id": run_id, "waybill_no": waybill_no, "locked": True, "changed": changed}


def lines(run_id: str) -> list[dict]:
    return store.charge_lines(run_id) if run_id else []


def amounts(run_id: str) -> dict:
    items = lines(run_id)
    parts = {
        "base": money.total([Decimal(i["base_amount"]) for i in items]),
        "step": money.total([Decimal(i["step_amount"]) for i in items]),
        "remote": money.total([Decimal(i["remote_amount"]) for i in items]),
        "fuel": money.total([Decimal(i["fuel_amount"]) for i in items]),
        "insurance": money.total([Decimal(i["insurance_amount"]) for i in items]),
    }
    grand = money.total([Decimal(i["total_amount"]) for i in items])
    return {"count": len(items), "parts": parts, "total": grand,
            "conserved": money.conserved(grand, parts.values())}
