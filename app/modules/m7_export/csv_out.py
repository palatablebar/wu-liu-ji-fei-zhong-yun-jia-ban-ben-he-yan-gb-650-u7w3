"""证据行导出：原始行号 + 原因码 + 计费重依据 + 版本号，供人工复核。"""

from __future__ import annotations

import csv
import io

from app import store
from app.reasons import label

HEADER = [
    "line_no", "waybill_no", "carrier", "service", "zone", "pickup_utc",
    "raw_weight", "weight_unit", "actual_kg", "conversion_factor", "conversion_expr",
    "carrier_control_lb", "carrier_control_kg", "tier_basis",
    "volumetric_kg", "source", "billable_kg", "steps",
    "version_id", "rate_set_version", "rate_digest", "weight_revision",
    "base_amount", "step_amount", "remote_amount", "fuel_amount", "insurance_amount",
    "total_amount", "locked", "reason_codes", "reason_labels",
]


def _merge_codes(*groups) -> list[str]:
    """计费重与账单两段的原因码合并，保持首次出现顺序。"""
    merged: list[str] = []
    for group in groups:
        for code in (group or "").split(","):
            if code and code not in merged:
                merged.append(code)
    return merged


def rows(run_id: str) -> list[dict]:
    waybills = {row["waybill_no"]: row for row in store.waybills(run_id)}
    weights = {row["waybill_no"]: row for row in store.weight_lines(run_id)}
    charges = {row["waybill_no"]: row for row in store.charge_lines(run_id)}
    merged: list[dict] = []
    for waybill_no, weight in weights.items():
        waybill = waybills.get(waybill_no, {})
        charge = charges.get(waybill_no, {})
        codes = _merge_codes(weight.get("codes"), charge.get("codes"))
        merged.append({
            "line_no": waybill.get("line_no", 0),
            "waybill_no": waybill_no,
            "carrier": waybill.get("carrier", ""),
            "service": waybill.get("service", ""),
            "zone": waybill.get("zone", ""),
            "pickup_utc": waybill.get("pickup_utc", ""),
            "raw_weight": weight.get("raw_weight", "") or waybill.get("raw_weight", ""),
            "weight_unit": weight.get("weight_unit", "") or waybill.get("weight_unit", ""),
            "actual_kg": weight.get("actual_kg", ""),
            "conversion_factor": weight.get("conversion_factor", "1"),
            "conversion_expr": weight.get("conversion_expr", ""),
            "carrier_control_lb": weight.get("carrier_control_lb", ""),
            "carrier_control_kg": weight.get("carrier_control_kg", ""),
            "tier_basis": weight.get("tier_basis", "contract_kg"),
            "volumetric_kg": weight.get("volumetric_kg", ""),
            "source": weight.get("source", ""),
            "billable_kg": weight.get("billable_kg", ""),
            "steps": weight.get("steps", 0),
            "version_id": weight.get("version_id", ""),
            "rate_set_version": weight.get("rate_set_version", 0),
            "rate_digest": weight.get("rate_digest", ""),
            "weight_revision": charge.get("weight_revision", weight.get("revision", 1)),
            "base_amount": charge.get("base_amount", "0.00"),
            "step_amount": charge.get("step_amount", "0.00"),
            "remote_amount": charge.get("remote_amount", "0.00"),
            "fuel_amount": charge.get("fuel_amount", "0.00"),
            "insurance_amount": charge.get("insurance_amount", "0.00"),
            "total_amount": charge.get("total_amount", "0.00"),
            "locked": "是" if charge.get("locked") else "否",
            "reason_codes": " ".join(codes),
            "reason_labels": " ".join(label(c) for c in codes),
        })
    merged.sort(key=lambda row: (row["line_no"], row["waybill_no"]))
    return merged


def csv_text(run_id: str) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(HEADER)
    for row in rows(run_id):
        writer.writerow([row[key] for key in HEADER])
    return buffer.getvalue()
