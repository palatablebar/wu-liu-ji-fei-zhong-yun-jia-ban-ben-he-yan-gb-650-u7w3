"""M4 编排：按运单逐行择版、算体积重、取大、进位，落库计费重行。

计费重裁决（磅/公斤双台阶）：
- 客户应收只认合同公斤台阶：磅重先按法定因子换算 kg，再与体积重取大、
  按运价版本 step_weight_kg 向上进位；
- 承运商半磅控制重并行留痕（carrier_control_lb/kg），仅供对账，不进计费；
- 每行保留磅重原值、换算式、采用的运价版本与运价摘要，可一起复核；
- 上游磅重更新走 recompute_waybill：旧值进 weight_events 历史，账单锁定不被改金额。
"""

from __future__ import annotations

from decimal import Decimal

from app import config, manifest, store
from app.errors import DomainError
from app.modules.m2_rates import service as rates
from app.modules.m2_rates import version as rateversion
from app.modules.m4_weights import billable, imperial, rounding, volumetric

CONTRACT_KG = "contract_kg"


def _rate_set_version_of(card: dict) -> int:
    try:
        return int(card.get("rate_set_version") or 0)
    except (TypeError, ValueError):
        return 0


def _audit_fields(row: dict, card: dict | None) -> dict:
    """组装磅重原值、换算过程与承运商控制重等审计字段。"""
    unit = (row.get("weight_unit") or "kg").strip().lower()
    raw_weight = str(row.get("raw_weight") or "").strip()
    actual = Decimal(row["weight_kg"])
    factor = config.LB_TO_KG if imperial.is_pound(unit) else Decimal("1")
    expr = imperial.conversion_expression(raw_weight or str(actual), unit, factor, actual)

    audit = {
        "weight_unit": unit,
        "raw_weight": raw_weight,
        "conversion_factor": str(factor),
        "conversion_expr": expr,
        "carrier_control_lb": "",
        "carrier_control_kg": "",
        "tier_basis": CONTRACT_KG,
    }
    if imperial.is_pound(unit) and raw_weight:
        try:
            raw_lb = Decimal(raw_weight)
        except Exception:  # noqa: BLE001 - 原值异常时控制重留空，不阻断主流程
            raw_lb = None
        if raw_lb is not None:
            control_lb = imperial.carrier_control_weight_lb(raw_lb)
            control_kg = imperial.carrier_control_weight_kg(control_lb)
            audit["carrier_control_lb"] = str(control_lb)
            audit["carrier_control_kg"] = str(control_kg)
    if card is not None:
        audit["rate_set_version"] = _rate_set_version_of(card)
        audit["rate_digest"] = rates.rate_digest()
    return audit


def _compute_one(row: dict, index_map: dict, digest: str) -> dict:
    pickup = rateversion.parse_utc(row["pickup_utc"])
    card, codes = rates.select_card(index_map, row["carrier"], row["service"], row["zone"], pickup)
    audit = _audit_fields(row, card)
    if card is None:
        return {
            "waybill_no": row["waybill_no"],
            "version_id": "",
            "actual_kg": row["weight_kg"],
            "volumetric_kg": "0.000",
            "source": billable.UNBILLED,
            "billable_kg": "0.000",
            "steps": 0,
            "codes": ",".join(codes),
            **{k: v for k, v in audit.items() if k != "rate_digest"},
            "rate_digest": digest,
            "rate_set_version": audit.get("rate_set_version", 0),
            "revision": 1,
            "recalc_seq": 0,
        }

    actual = Decimal(row["weight_kg"])
    volume = volumetric.volumetric_kg(
        Decimal(row["length_cm"]),
        Decimal(row["width_cm"]),
        Decimal(row["height_cm"]),
        int(card["volume_divisor"]),
    )
    raw, source = billable.pick(actual, volume)
    step = Decimal(card["step_weight_kg"])
    billable_kg = rounding.ceil_to_step(raw, step)
    steps = rounding.steps_above(billable_kg, Decimal(card["first_weight_kg"]), step)

    row_codes = list(billable.source_codes(source))
    if imperial.is_pound(audit["weight_unit"]):
        row_codes.append("LB_TO_KG_CONVERTED")
        if audit["carrier_control_lb"]:
            row_codes.append("CARRIER_CONTROL_RECORDED")
    row_codes.append("TIER_CONTRACT_KG")

    return {
        "waybill_no": row["waybill_no"],
        "version_id": card["version_id"],
        "actual_kg": str(actual),
        "volumetric_kg": str(volume),
        "source": source,
        "billable_kg": str(billable_kg),
        "steps": steps,
        "codes": ",".join(sorted(set(row_codes))),
        **audit,
        "revision": 1,
        "recalc_seq": 0,
    }


def _emit_event(run_id: str, line: dict, event_type: str, reason: str, seq: int) -> None:
    store.insert_weight_event(run_id, {
        "waybill_no": line["waybill_no"],
        "seq": seq,
        "event_type": event_type,
        "raw_weight": line.get("raw_weight", ""),
        "weight_unit": line.get("weight_unit", ""),
        "actual_kg": line.get("actual_kg", ""),
        "billable_kg": line.get("billable_kg", ""),
        "carrier_control_lb": line.get("carrier_control_lb", ""),
        "carrier_control_kg": line.get("carrier_control_kg", ""),
        "version_id": line.get("version_id", ""),
        "rate_set_version": int(line.get("rate_set_version") or 0),
        "rate_digest": line.get("rate_digest", ""),
        "reason": reason,
        "created_at": manifest.utc_now_iso(),
    })


def compute_weights(run_id: str) -> dict:
    run = store.get_run(run_id)
    if not run:
        raise DomainError("RUN_NOT_FOUND", f"运行不存在：{run_id}", 404)
    rows = store.waybills(run_id)
    if not rows:
        raise DomainError("NO_ROWS", "该运行没有可计费的运单", 409)

    index_map = rates.index()
    digest = rates.rate_digest()
    existing = {row["waybill_no"]: row for row in store.weight_lines(run_id)}
    lines = [_compute_one(row, index_map, digest) for row in rows]

    # 全量重跑幂等：已存在的运单保留原 revision/recalc_seq，只有首次出现的行记 computed 事件
    fresh_events = 0
    for line in lines:
        prev = existing.get(line["waybill_no"])
        if prev is not None:
            line["revision"] = int(prev.get("revision") or 1)
            line["recalc_seq"] = int(prev.get("recalc_seq") or 0)
        else:
            fresh_events += 1
    store.replace_weight_lines(run_id, lines)
    now = manifest.utc_now_iso()
    for line in lines:
        if line["waybill_no"] not in existing:
            _emit_event(run_id, line, "computed", "首次计费重计算",
                        store.next_weight_seq(run_id, line["waybill_no"]))
    store.add_stage(run_id, "weights", now)
    return {
        "run_id": run_id,
        "billed": sum(1 for line in lines if line["source"] != billable.UNBILLED),
        "unbilled": sum(1 for line in lines if line["source"] == billable.UNBILLED),
        "volumetric_hits": sum(1 for line in lines if line["source"] == billable.VOLUMETRIC),
        "pound_sources": sum(1 for line in lines if imperial.is_pound(line.get("weight_unit", ""))),
        "steps_total": sum(int(line["steps"]) for line in lines),
        "fresh_events": fresh_events,
        "rate_digest": digest,
    }


def recompute_waybill(run_id: str, waybill_no: str, raw_weight: str, unit: str,
                      reason: str = "上游磅重更新") -> dict:
    """上游磅重更新后的单行重算。

    - 旧重量与旧计费重先落 weight_events 历史，再覆盖运单与计费重行（不新增重复行）；
    - 已核（locked）账单金额不动：本函数只改重量侧，并返回冲突标记交人工裁决；
    - 运价版本按当前提货时刻重新择版，版本号与运价摘要随结果留痕。
    """
    run = store.get_run(run_id)
    if not run:
        raise DomainError("RUN_NOT_FOUND", f"运行不存在：{run_id}", 404)
    waybill = next((w for w in store.waybills(run_id) if w["waybill_no"] == waybill_no), None)
    if waybill is None:
        raise DomainError("WAYBILL_NOT_FOUND", f"运单不存在：{waybill_no}", 404)

    previous = store.weight_line(run_id, waybill_no) or {}
    locked = waybill_no in store.locked_waybills(run_id)

    kg, weight_codes, weight_reason = _convert_update(raw_weight, unit)
    if weight_reason:
        raise DomainError(weight_reason, f"更新重量无法采信：{raw_weight} {unit}", 422)

    waybill["weight_kg"] = str(kg)
    waybill["raw_weight"] = str(_normalized_number(raw_weight))
    waybill["weight_unit"] = unit.strip().lower()
    codes = set(c for c in (waybill.get("codes") or "").split(",") if c)
    codes.update(weight_codes)
    codes.add("WEIGHT_UPDATED")
    waybill["codes"] = ",".join(sorted(codes))
    store.upsert_waybill(run_id, waybill)

    index_map = rates.index()
    line = _compute_one(waybill, index_map, rates.rate_digest())
    line["revision"] = int(previous.get("revision") or 1) + 1
    line["recalc_seq"] = int(previous.get("recalc_seq") or 0) + 1
    line_codes = set(c for c in line["codes"].split(",") if c)
    line_codes.add("WEIGHT_RERUN")

    conflict = False
    if locked:
        # 已核账单：重量侧照常重算并留痕，金额侧不动，冲突交人工裁决
        conflict = True
        line_codes.add("LOCKED_WEIGHT_CONFLICT")
    line["codes"] = ",".join(sorted(line_codes))
    store.upsert_weight_line(run_id, line)

    seq = store.next_weight_seq(run_id, waybill_no)
    if previous:
        store.insert_weight_event(run_id, {
            "waybill_no": waybill_no, "seq": seq, "event_type": "superseded",
            "reason": f"{reason}：旧值归档", "raw_weight": previous.get("raw_weight", ""),
            "weight_unit": previous.get("weight_unit", ""),
            "actual_kg": previous.get("actual_kg", ""),
            "billable_kg": previous.get("billable_kg", ""),
            "carrier_control_lb": previous.get("carrier_control_lb", ""),
            "carrier_control_kg": previous.get("carrier_control_kg", ""),
            "version_id": previous.get("version_id", ""),
            "rate_set_version": int(previous.get("rate_set_version") or 0),
            "rate_digest": previous.get("rate_digest", ""),
            "created_at": manifest.utc_now_iso(),
        })
        seq = store.next_weight_seq(run_id, waybill_no)
    _emit_event(run_id, line, "updated", reason, seq)

    return {
        "run_id": run_id,
        "waybill_no": waybill_no,
        "revision": line["revision"],
        "previous_actual_kg": previous.get("actual_kg", ""),
        "previous_billable_kg": previous.get("billable_kg", ""),
        "actual_kg": line["actual_kg"],
        "billable_kg": line["billable_kg"],
        "carrier_control_lb": line["carrier_control_lb"],
        "carrier_control_kg": line["carrier_control_kg"],
        "conversion_expr": line["conversion_expr"],
        "version_id": line["version_id"],
        "rate_set_version": line["rate_set_version"],
        "rate_digest": line["rate_digest"],
        "charge_locked": locked,
        "locked_conflict": conflict,
    }


def _normalized_number(text: str) -> Decimal:
    from app.modules.m1_waybill import normalize
    number, _suffix, _codes = normalize.scan_number(str(text))
    return number if number is not None else Decimal(str(text))


def _convert_update(raw_weight: str, unit: str):
    from app.modules.m1_waybill import normalize
    return normalize.to_kg(str(raw_weight), unit)


def lines(run_id: str) -> list[dict]:
    return store.weight_lines(run_id)


def events(run_id: str, waybill_no: str = "") -> list[dict]:
    return store.weight_events(run_id, waybill_no)


def summary(run_id: str) -> dict:
    items = lines(run_id)
    return {
        "count": len(items),
        "billed": sum(1 for i in items if i["source"] != billable.UNBILLED),
        "unbilled": sum(1 for i in items if i["source"] == billable.UNBILLED),
        "volumetric_hits": sum(1 for i in items if i["source"] == billable.VOLUMETRIC),
        "actual_hits": sum(1 for i in items if i["source"] in (billable.ACTUAL, billable.TIE)),
        "pound_sources": sum(1 for i in items if imperial.is_pound(i.get("weight_unit", ""))),
        "billable_total": str(sum((Decimal(i["billable_kg"]) for i in items), Decimal("0.000"))),
    }
