# 运行手册 freightrate

## 启动

```bash
docker compose config --quiet
docker compose up -d --build
curl -s http://127.0.0.1:18209/api/health
```

宿主机端口 18209 映射容器 8000。SQLite 落在命名卷 `freightrate-data` 的 `/app/data/freightrate.db`。

## 启动后自动完成的事

1. 建表
2. 装载 `sample_data/rate_cards.csv`（运价集 v3，38 行）
3. 采信 `sample_data/waybills_dirty.csv`
4. 计算计费重与账单

页面首次打开即可看到 27 条运单、15 票计费、12 行隔离、应收 1521.04 元。

## 手工重跑

```bash
curl -s -X POST http://127.0.0.1:18209/api/waybills/import -F sample=waybills_dirty.csv
curl -s -X POST http://127.0.0.1:18209/api/weights
curl -s -X POST http://127.0.0.1:18209/api/charges
curl -s http://127.0.0.1:18209/api/reconcile
```

同一份输入重复导入命中同一个 run_id，行数与合计不变。

## 上游磅重更新与已核账单保护

磅制运单（如 WB-1022，5 lbs）按合同公斤台阶计费：换算 2.268 kg 后进位到 2.500 kg，
承运商半磅控制重 2.268 kg 仅在证据 CSV 与计费重页留痕对账。

```bash
# 上游更正某票磅重：旧值入事件历史，重新换算/择版/进位，不新增重复行
curl -s -X POST http://127.0.0.1:18209/api/weights/recompute \
  -F waybill_no=WB-1022 -F raw_weight=5.6 -F unit=lb -F reason=承运商更正磅重
# 查看该票重量事件（computed / superseded / updated）
curl -s "http://127.0.0.1:18209/api/weights/events?waybill_no=WB-1022"
# 核定一票账单：锁定后任何重算都不改金额
curl -s -X POST http://127.0.0.1:18209/api/charges/lock -F waybill_no=WB-1022
```

账单锁定后再收到磅重更新：重量侧照常重算并打 `LOCKED_WEIGHT_CONFLICT`，
账单金额保持核定值，需人工裁决（冲红/补差），系统不自动改已核金额。
完整审计链（磅原值、换算式、控制重、运价版本与摘要、版次）见证据 CSV：

```bash
curl -s http://127.0.0.1:18209/api/export/evidence.csv | head -2
```

## 运价文件校验

```bash
curl -s -X POST http://127.0.0.1:18209/api/rates/audit -F sample=rate_cards_overlap.csv
```

返回重叠清单；`/api/rates/import` 在校验不通过时返回 409，不会替换现有运价。

## 重建数据库

```bash
docker compose down -v --remove-orphans
docker compose up -d --build
```

## 测试

```bash
docker compose exec -T app python -m pytest -q
```

## 常见失败

| 现象 | 原因 | 处理 |
|---|---|---|
| 页面 500，提示 RATE_MISSING | 采信时运价表还没装载 | 先打开运价手册页确认 38 行版本窗口 |
| /api/charges 返回 409 NO_WEIGHTS | 还没算计费重 | 先调 /api/weights |
| 导入后 rows_ok 为 0 | 全部行都被隔离 | 到运单页看原因码分布 |
| 运价装载 409 RATE_STALE | 提交的运价集版本低于现有 | 提高 CSV 里的 rate_set_version |
