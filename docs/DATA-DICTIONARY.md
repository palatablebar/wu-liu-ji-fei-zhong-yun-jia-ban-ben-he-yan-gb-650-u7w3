# 定义契约 freightrate

## 数值

| 列 | 类型 | 量化 | 可空 | 说明 |
|---|---|---|---|---|
| weight_kg | Decimal | 0.001 kg | 否 | 实重，输入单位 g/kg/t 都换算到 kg |
| length_cm / width_cm / height_cm | Decimal | 0.001 cm | 否 | 尺寸，输入单位 cm/m/mm 都换算到 cm |
| volumetric_kg | Decimal | 0.001 kg | 否 | 长×宽×高 ÷ volume_divisor |
| billable_kg | Decimal | 0.5 kg 向上 | 否 | 取实重与体积重的较大值后按台阶进位 |
| first_price / step_price / min_charge | Decimal | 0.01 元 | 否 | 运价表金额 |
| fuel_pct / insurance_pct | Decimal | 小数 | 否 | 8.5% 与 0.085 两种写法都归到小数 |
| declared_value | Decimal | 0.01 元 | 是 | 空值与哨兵都按 0 处理，表示未保价 |
| total_amount | Decimal | 0.01 元 | 否 | 由分项求和得出，不单独舍入 |

金额与用量一律 Decimal，对外 JSON 用字符串。禁止 float 累加。

## 单位

单位表只在 `app/config.py` 定义一份。重量 g=0.001 kg、t=1000 kg；尺寸 m=100 cm、mm=0.1 cm。
带单位后缀的单元格（如 3200g）先剥后缀，单位列非空时以单位列为准。

### 磅制回传与双台阶裁决（TRAP-27）

承运商按磅（lb/lbs/磅）回传实重，合同按公斤台阶报价。两侧口径不能混用，裁决如下：

- **客户应收只认合同公斤台阶（`tier_basis = contract_kg`）**：磅重先按法定因子
  `1 lb = 0.45359237 kg`（`config.LB_TO_KG`，全程 Decimal）换算到 0.001 kg，
  再与体积重取大、按运价版本 `step_weight_kg` 向上进位得到 `billable_kg`。
- **承运商半磅控制重仅对账留痕**：磅值按 0.5 lb 向上取整（`carrier_control_lb`）
  并换算成 `carrier_control_kg`，平行记录但绝不反向决定客户计费台阶。
  沿用磅制台阶会让公斤报价失去合同依据。
- 例：`5 lb × 0.45359237 = 2.268 kg`，合同 0.5 kg 台阶向上到 `2.500 kg`；
  承运商控制重为 `5.000 lb / 2.268 kg`，两者差一个台阶，以合同侧 2.500 kg 计费。
- 每行保留可一起复核的审计链：磅重原值 `raw_weight` + 单位 `weight_unit`、
  换算式 `conversion_expr`（含因子）、控制重、采用的 `version_id` /
  `rate_set_version` / `rate_digest`、版次 `revision` 与重算序号 `recalc_seq`。
- 上游磅重更新：旧值写入 `weight_events`（事件类型 `computed/superseded/updated`）
  后幂等覆盖，不新增重复行；账单 `locked=1`（已核）后重算只改重量侧并打
  `LOCKED_WEIGHT_CONFLICT`，账单金额原样保留。

## 时间

- 存储：UTC，ISO 文本，秒精度，带 Z 后缀
- 输入：接受 Z、+hh:mm、无偏移三种写法；无偏移按 +08:00 解释
- 展示：页面显示 Asia/Shanghai，生成时间与批次时间都带时区名
- 运价版本窗口：`effective_from` 与 `effective_to` 也是 UTC，区间半开 [from, to)

## 空值

- `null` / 空串 / 哨兵 三者语义分开
- 哨兵清单：`-`、`--`、`/`、`N/A`、`NA`、`NULL`、`NONE`、`未知`、`无`、`空`
- 重量与尺寸出现哨兵即进隔离区；声明价值出现哨兵按未保价处理

## 半开区间

运价版本用 `[effective_from, effective_to)`：提货时刻等于 from 命中该版，等于 to 落到下一版。
相邻版本首尾相接时不重不漏。

## 去重键

业务键是运单号 `waybill_no`。同一运单号第二次出现时比较载荷
（承运商、服务、分区、提货时刻、重量、三边尺寸、声明价值）：
载荷相同记 DUPLICATE_WAYBILL，不同记 DUP_CONFLICT，两种都只保留第一条参与计费。

## 空态

- 0 字节文件：拒绝采信，返回 EMPTY_FILE
- 只有表头：采信成功但 rows_in 为 0，页面显示空态
- 全是空行：empty_rows 计数，rows_in 为 0

## 原因码

原因码只在 `app/reasons.py` 定义，分两级：`block` 表示该行不进计费，`info` 表示只做标记。
每条隔离都带原始行号与原因码，可导出 CSV 复核。
