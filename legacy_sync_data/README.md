# legacy_sync_data · 合成数据（旧基线）

这里存放的是**早期合成数据集**的完整产物，已被 **Olist 真实数据**取代为当前基线。

## 为什么保留

1. **对照**：合成数据（20,000 运单）与 Olist 真实数据（112,650 运单）
   是两套独立验证，README 的「双实现交叉验证」章节引用了两者的结果
2. **可复现**：不需要重新跑管道就能核对 README 里引用的每一个数字
3. **基线**：synthetic 模式仍是 `run_all.py` 的默认值，随时可以切回去

## 内容

| 文件 | 说明 |
| --- | --- |
| `internal_records.csv` | 内部系统记录（20,000 运单） |
| `rate_card.csv` | 承运商费率表（216 行：9 分区 × 3 服务等级 × 8 重量档） |
| `carrier_invoice.csv` | 合成承运商账单（20,145 行，注入 5 类差异） |
| `ground_truth.csv` | 差异标注真值（用于算查全率/查准率） |
| `discrepancies.csv` | Python 版核对结果（2,503 条） |
| `discrepancies_sql.csv` | DuckDB SQL 版核对结果 |
| `reconciliation_summary.json` | 汇总指标 |

## 关键数字（README 有引用）

```
差异条数 2,503    多收应追回 $4,927.78    少收待确认 $1,952.74    净影响 $2,969.65
查全率 99.17%     查准率 100.00%
双实现交叉验证：2,503 条 × 13 字段 = 32,539 项，不一致 0 项
```

## 怎么切回合成数据

```bash
python3 run_all.py                        # 默认 --source synthetic
python3 run_all.py --source olist         # 切到 Olist 真实数据
```

> ⚠️ 两者都会覆盖 `data/` 下的同名文件。切换后请重跑 `python3 validate.py` 重新生成 SQL 版产物。
