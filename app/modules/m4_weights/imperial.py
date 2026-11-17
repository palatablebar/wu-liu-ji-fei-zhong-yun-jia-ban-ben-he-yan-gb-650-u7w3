"""磅制回传的换算与承运商控制重（TRAP：磅/公斤双台阶）。

裁决口径（对客户应收一侧）：
- 承运商按磅回传实重，合同按公斤台阶计费；
- 磅重先按法定因子 1 lb = 0.45359237 kg 精确换算到公斤，
  再与体积重取大、按合同 step_weight_kg 向上进位得到计费重；
- 承运商按半磅（0.5 lb）取整后的控制重只做对账留痕，
  绝不反向决定客户计费台阶（沿用磅制台阶会让公斤报价失去合同依据）。

全程 Decimal，禁止 float；换算式以文本保留，供逐票复核。
"""

from __future__ import annotations

from decimal import Decimal, ROUND_CEILING, ROUND_HALF_UP

from app import config
from app.modules.m4_weights.rounding import ceil_to_step

LB_UNITS = frozenset({"lb", "lbs", "磅"})
Q3 = config.WEIGHT_QUANT
# 控制重磅值展示精度：半磅台阶，保留 3 位小数足够表达 0.5 lb
LB_QUANT = Decimal("0.001")


def is_pound(unit: str) -> bool:
    return (unit or "").strip().lower() in LB_UNITS


def lb_to_kg(pounds: Decimal, factor: Decimal = config.LB_TO_KG) -> Decimal:
    """磅→公斤，量化到 0.001 kg（与重量列刻度一致）。"""
    return (Decimal(pounds) * Decimal(factor)).quantize(Q3, rounding=ROUND_HALF_UP)


def conversion_expression(raw_weight: str, unit: str, factor: Decimal, kg: Decimal) -> str:
    """留下可人工复核的换算式，例如 11.0 lb × 0.45359237 = 4.990 kg。"""
    if not is_pound(unit):
        return f"{raw_weight} {unit} = {kg} kg"
    return f"{raw_weight} lb × {factor} = {kg} kg"


def carrier_control_weight_lb(raw_lb: Decimal,
                              step_lb: Decimal = config.CARRIER_STEP_LB) -> Decimal:
    """承运商口径：磅值按半磅向上取整（其内部控制重，仅对账用）。"""
    return ceil_to_step(Decimal(raw_lb), Decimal(step_lb)).quantize(
        LB_QUANT, rounding=ROUND_CEILING
    )


def carrier_control_weight_kg(control_lb: Decimal,
                              factor: Decimal = config.LB_TO_KG) -> Decimal:
    """控制重换算成公斤，仅用于与合同计费重做差异展示，不参与计费。"""
    return (Decimal(control_lb) * Decimal(factor)).quantize(Q3, rounding=ROUND_HALF_UP)


def tier_gap_kg(contract_billable_kg: Decimal, control_kg: Decimal) -> Decimal:
    """合同计费重与承运商控制重（公斤口径）的台阶差，正值表示合同侧更高。"""
    return (Decimal(contract_billable_kg) - Decimal(control_kg)).quantize(Q3)
