"""磅/公斤双台阶裁决：合同公斤侧计费、承运商半磅控制重留痕、更新重算与账单锁定。

裁决要点：
- 客户应收只认合同公斤台阶，磅重按 1 lb = 0.45359237 kg 精确换算后向上进位；
- 承运商半磅控制重仅对账，不反向决定台阶；
- 上游磅重更新：旧值入事件历史、重量行幂等覆盖不重复、已核账单金额不动。
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app import store
from app.modules.m4_weights import imperial, service as weights
from app.modules.m5_charges import service as charges


# --- 纯函数：换算与控制重 ---------------------------------------------------

def test_lb_to_kg_uses_legal_factor_exactly():
    assert imperial.lb_to_kg(Decimal("5")) == Decimal("2.268")
    assert imperial.lb_to_kg(Decimal("11")) == Decimal("4.990")
    # 11 lb = 4.98951607 kg，量化到 0.001 为 4.990，绝不使用 float 近似
    assert imperial.is_pound("lb") and imperial.is_pound("LBS") and imperial.is_pound("磅")
    assert not imperial.is_pound("kg")


def test_carrier_control_rounds_half_pound_ceiling():
    # 承运商口径：5.1 lb 向上到 5.5 lb；该值只留痕，不参与合同计费
    assert imperial.carrier_control_weight_lb(Decimal("5.1")) == Decimal("5.500")
    assert imperial.carrier_control_weight_lb(Decimal("5.0")) == Decimal("5.000")
    assert imperial.carrier_control_weight_kg(Decimal("5.5")) == Decimal("2.495")


def test_conversion_expression_is_auditable():
    expr = imperial.conversion_expression("5", "lbs", Decimal("0.45359237"), Decimal("2.268"))
    assert expr == "5 lb × 0.45359237 = 2.268 kg"
    assert imperial.conversion_expression("12", "kg", Decimal("1"), Decimal("12")) == "12 kg = 12 kg"


def test_contract_kg_tier_sits_one_step_above_carrier_control():
    """复现题面：5 lb 一票，合同 2.5kg vs 控制重 2.268kg，恰差一个 0.5kg 台阶。"""
    control_kg = imperial.carrier_control_weight_kg(
        imperial.carrier_control_weight_lb(Decimal("5"))
    )
    contract_billable = Decimal("2.500")
    assert imperial.tier_gap_kg(contract_billable, control_kg) == Decimal("0.232")
    assert contract_billable > control_kg


# --- 集成：磅制票走完整流水线，审计字段齐全 ---------------------------------

def test_pound_waybill_billed_on_contract_kg_tier(pipeline):
    run_id = pipeline["run_id"]
    line = next(r for r in weights.lines(run_id) if r["waybill_no"] == "WB-1022")
    # 原值与单位
    assert line["raw_weight"] == "5"
    assert line["weight_unit"] == "lbs"
    # 换算过程可复核
    assert line["actual_kg"] == "2.268"
    assert line["conversion_factor"] == "0.45359237"
    assert line["conversion_expr"] == "5 lb × 0.45359237 = 2.268 kg"
    # 承运商半磅控制重并行留痕，但计费重按合同公斤台阶
    assert line["carrier_control_lb"] == "5.000"
    assert line["carrier_control_kg"] == "2.268"
    assert line["billable_kg"] == "2.500"
    assert line["tier_basis"] == "contract_kg"
    # 采用的运价版本可一起复核
    assert line["version_id"] == "SF-standard-Z1-v2"
    assert line["rate_set_version"] == 3
    assert len(line["rate_digest"]) == 64
    codes = set(line["codes"].split(","))
    assert {"LB_TO_KG_CONVERTED", "CARRIER_CONTROL_RECORDED", "TIER_CONTRACT_KG"} <= codes


def test_pound_source_counted_in_summary(pipeline):
    summary = weights.summary(pipeline["run_id"])
    assert summary["pound_sources"] == 1


def test_evidence_csv_carries_full_audit_chain(pipeline):
    from app.modules.m7_export import csv_out

    row = next(r for r in csv_out.rows(pipeline["run_id"]) if r["waybill_no"] == "WB-1022")
    assert row["raw_weight"] == "5"
    assert row["weight_unit"] == "lbs"
    assert row["conversion_expr"] == "5 lb × 0.45359237 = 2.268 kg"
    assert row["carrier_control_lb"] == "5.000"
    assert row["tier_basis"] == "contract_kg"
    assert row["version_id"] == "SF-standard-Z1-v2"
    assert row["rate_digest"]
    assert "LB_TO_KG_CONVERTED" in row["reason_codes"]


# --- 上游磅重更新：重算、历史、幂等、锁定 -----------------------------------

def test_recompute_archives_old_weight_and_recalculates(pipeline):
    run_id = pipeline["run_id"]
    before = store.weight_line(run_id, "WB-1022")
    base_rev = int(before["revision"])
    result = weights.recompute_waybill(run_id, "WB-1022", "5.6", "lb", "承运商更正磅重")

    # 5.6 lb × 0.45359237 = 2.540 kg → 合同 0.5kg 台阶向上 3.0kg
    assert result["actual_kg"] == "2.540"
    assert result["billable_kg"] == "3.000"
    assert result["previous_actual_kg"] == before["actual_kg"] == "2.268"
    assert result["previous_billable_kg"] == "2.500"
    assert result["revision"] == base_rev + 1
    assert result["charge_locked"] is False

    after = store.weight_line(run_id, "WB-1022")
    assert after["revision"] == base_rev + 1
    assert "WEIGHT_RERUN" in after["codes"]

    events = weights.events(run_id, "WB-1022")
    kinds = [e["event_type"] for e in events]
    assert kinds[0] == "computed"
    assert "superseded" in kinds and "updated" in kinds
    superseded = next(e for e in events if e["event_type"] == "superseded")
    assert superseded["actual_kg"] == "2.268"  # 旧重量原值保留
    assert superseded["billable_kg"] == "2.500"


def test_recompute_is_idempotent_and_never_duplicates_lines(pipeline):
    run_id = pipeline["run_id"]
    weights.recompute_waybill(run_id, "WB-1022", "6.0", "lb")
    weight_count = len(store.weight_lines(run_id))
    waybill_count = len(store.waybills(run_id))
    weights.recompute_waybill(run_id, "WB-1022", "6.0", "lb")  # 同值再报一次
    weights.recompute_waybill(run_id, "WB-1022", "6.0", "lb")
    assert len(store.weight_lines(run_id)) == weight_count
    assert len(store.waybills(run_id)) == waybill_count
    line = store.weight_line(run_id, "WB-1022")
    assert line["billable_kg"] == "3.000"  # 6lb=2.722kg → 3.0kg


def test_locked_charge_amount_untouched_after_weight_update(pipeline):
    run_id = pipeline["run_id"]
    lock = charges.lock_charge(run_id, "WB-1022")
    assert lock["locked"] is True
    locked_before = next(c for c in store.charge_lines(run_id) if c["waybill_no"] == "WB-1022")
    locked_total = locked_before["total_amount"]
    locked_at = locked_before["locked_at"]

    # 上游把磅重从 5 改成 9 lb，账单金额必须原样保留
    result = weights.recompute_waybill(run_id, "WB-1022", "9.0", "lb")
    assert result["charge_locked"] is True
    assert result["locked_conflict"] is True
    assert result["billable_kg"] == "4.500"  # 9lb=4.082kg → 4.5kg，重量侧已重算

    charges.compute_charges(run_id)  # 重跑账单
    locked_after = next(c for c in store.charge_lines(run_id) if c["waybill_no"] == "WB-1022")
    assert locked_after["total_amount"] == locked_total == "22.57"
    assert locked_after["locked"] == 1
    assert locked_after["locked_at"] == locked_at
    line = store.weight_line(run_id, "WB-1022")
    assert "LOCKED_WEIGHT_CONFLICT" in line["codes"]


def test_charge_rerun_does_not_duplicate_or_mutate_other_lines(pipeline):
    run_id = pipeline["run_id"]
    charges.lock_charge(run_id, "WB-1022")
    before = {c["waybill_no"]: c["total_amount"] for c in store.charge_lines(run_id)}
    report = charges.compute_charges(run_id)
    after = {c["waybill_no"]: c["total_amount"] for c in store.charge_lines(run_id)}
    assert report["skipped_locked"] == 1
    assert after == before  # 所有账单金额保持，行数不增
    assert len(after) == len(before)


def test_full_rerun_keeps_revision_and_does_not_duplicate_events(pipeline):
    run_id = pipeline["run_id"]
    weights.recompute_waybill(run_id, "WB-1022", "5.6", "lb")
    rev_before = store.weight_line(run_id, "WB-1022")["revision"]
    events_before = len(weights.events(run_id, "WB-1022"))

    # 全量重跑（同一份输入）：revision 保留，不重复记 computed 事件
    weights.compute_weights(run_id)
    rev_after = store.weight_line(run_id, "WB-1022")["revision"]
    events_after = len(weights.events(run_id, "WB-1022"))
    assert rev_after == rev_before
    assert events_after == events_before


def test_recompute_rejects_unknown_unit(pipeline):
    from app.errors import DomainError

    with pytest.raises(DomainError) as exc:
        weights.recompute_waybill(pipeline["run_id"], "WB-1022", "5", "stone")
    assert exc.value.code == "UNIT_UNKNOWN"


def test_recompute_unknown_waybill_raises(pipeline):
    from app.errors import DomainError

    with pytest.raises(DomainError) as exc:
        weights.recompute_waybill(pipeline["run_id"], "WB-NOPE", "5", "lb")
    assert exc.value.code == "WAYBILL_NOT_FOUND"
