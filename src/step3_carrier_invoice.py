#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Step 3 · 合成承运商账单（注入真实世界的 5 类差异）
====================================================

现实背景
--------
真实的承运商账单是**商业机密，公开数据集里不存在**。
所以本项目的做法是：以「内部系统记录 + 费率表」为**应付真相**，
**反向合成**一张承运商账单，并按真实世界常见的差异类型注入错误。

这样做有三个好处：
    ① 底表可以是真实数据（Olist），不是玩具数据；
    ② **我们知道 ground truth**，因此能算出查全率 / 查准率；
    ③ 现实里拿到的账单本来就没有标注 —— 异常检测本就要自己定义规则、自己判断。

5 类差异（互斥，每单最多注入一种，便于评估）
--------------------------------------------
| 类型 | 注入方式 | 对账时的表现 |
| --- | --- | --- |
| `weight_inflated`   | 计费重量 = 应付重量 × (1.05~1.20) | 重量偏差超阈值，金额随之偏高 |
| `wrong_rate`        | 用「上一级分区」的费率计费 | 分区或金额与费率表不符 |
| `duplicate_billing` | 同一运单多出一行 | 运单号重复 |
| `missing_from_invoice` | 内部有、账单无 | 运单号在账单中缺失 |
| `amount_mismatch`   | 各项正确但总额 ≠ 各项之和 | 行内金额加总对不上 |

产出
----
    data/carrier_invoice.csv   承运商账单（含差异，无标注）
    data/ground_truth.csv      标注真值（仅评估用，现实中不存在）
"""

from __future__ import annotations

import argparse
import csv
import os
import random
from datetime import date, timedelta

from rating import load_rate_card

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_DIR = os.path.join(BASE_DIR, "data")

DEFAULT_RATES = {
    "weight_inflated": 0.050,
    "wrong_rate": 0.030,
    "duplicate_billing": 0.015,
    "missing_from_invoice": 0.010,
    "amount_mismatch": 0.020,
}
WEIGHT_INFLATE_RANGE = (1.05, 1.20)

INVOICE_FIELDS = [
    "invoice_line_id", "invoice_batch", "shipment_id", "invoice_date",
    "billed_zone", "service_level", "billed_weight_kg",
    "base_amount", "weight_amount", "fuel_surcharge", "remote_surcharge",
    "total_amount", "currency",
]
TRUTH_FIELDS = [
    "shipment_id", "is_discrepancy", "discrepancy_type",
    "expected_total", "billed_total", "impact_amount", "note",
]


def _amounts_for(rate_card, zone: int, service: str, weight: float) -> dict:
    """按费率表计算各项金额。"""
    exp = rate_card.expected(zone, service, weight)
    return {
        "base_amount": exp["base_rate"],
        "weight_amount": exp["weight_amount"],
        "fuel_surcharge": exp["fuel_surcharge"],
        "remote_surcharge": exp["remote_surcharge"],
        "total_amount": exp["total"],
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="Step 3 · 合成承运商账单")
    ap.add_argument("--internal", default=os.path.join(OUT_DIR, "internal_records.csv"))
    ap.add_argument("--rate-card", default=os.path.join(OUT_DIR, "rate_card.csv"))
    ap.add_argument("--out-invoice", default=os.path.join(OUT_DIR, "carrier_invoice.csv"))
    ap.add_argument("--out-truth", default=os.path.join(OUT_DIR, "ground_truth.csv"))
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--rate-weight", type=float, default=DEFAULT_RATES["weight_inflated"])
    ap.add_argument("--rate-wrong-rate", type=float, default=DEFAULT_RATES["wrong_rate"])
    ap.add_argument("--rate-duplicate", type=float, default=DEFAULT_RATES["duplicate_billing"])
    ap.add_argument("--rate-missing", type=float, default=DEFAULT_RATES["missing_from_invoice"])
    ap.add_argument("--rate-amount", type=float, default=DEFAULT_RATES["amount_mismatch"])
    args = ap.parse_args()

    probs = {
        "weight_inflated": args.rate_weight,
        "wrong_rate": args.rate_wrong_rate,
        "duplicate_billing": args.rate_duplicate,
        "missing_from_invoice": args.rate_missing,
        "amount_mismatch": args.rate_amount,
    }

    rate_card = load_rate_card(args.rate_card)
    with open(args.internal, newline="", encoding="utf-8") as fh:
        records = list(csv.DictReader(fh))

    rng = random.Random(args.seed)
    invoice: list[dict] = []
    truth: list[dict] = []
    line_no = 0
    batch = f"INV-{(date.today()):%Y%m}"

    for rec in records:
        sid = rec["shipment_id"]
        zone = int(rec["dest_zone"])
        service = rec["service_level"]
        weight = float(rec["chargeable_weight_kg"])
        ship_date = date.fromisoformat(rec["ship_date"])
        inv_date = (ship_date + timedelta(days=rng.randint(5, 12))).isoformat()

        expected = _amounts_for(rate_card, zone, service, weight)
        exp_total = expected["total_amount"]

        # 抽取注入类型（互斥）
        r = rng.random()
        acc = 0.0
        kind = "none"
        for k in ("weight_inflated", "wrong_rate", "duplicate_billing",
                  "missing_from_invoice", "amount_mismatch"):
            acc += probs[k]
            if r < acc:
                kind = k
                break

        # ---- 未入账：账单里干脆没有这一行 ----
        if kind == "missing_from_invoice":
            truth.append({
                "shipment_id": sid, "is_discrepancy": 1, "discrepancy_type": kind,
                "expected_total": round(exp_total, 2), "billed_total": "",
                "impact_amount": round(exp_total, 2),
                "note": "内部系统有发货记录，承运商账单中无对应行",
            })
            continue

        billed_zone, billed_weight = zone, weight
        if kind == "weight_inflated":
            billed_weight = round(weight * rng.uniform(*WEIGHT_INFLATE_RANGE), 3)
        elif kind == "wrong_rate":
            billed_zone = min(zone + 1, 9)   # 按更贵的分区计费

        amt = _amounts_for(rate_card, billed_zone, service, billed_weight)

        # ---- 金额加总错误：各项正确，但总额被改掉 ----
        if kind == "amount_mismatch":
            components = round(amt["base_amount"] + amt["weight_amount"]
                               + amt["fuel_surcharge"] + amt["remote_surcharge"], 4)
            amt["total_amount"] = round(components * rng.uniform(1.01, 1.05), 4)

        line_no += 1
        invoice.append({
            "invoice_line_id": f"L{line_no:07d}",
            "invoice_batch": batch,
            "shipment_id": sid,
            "invoice_date": inv_date,
            "billed_zone": billed_zone,
            "service_level": service,
            "billed_weight_kg": round(billed_weight, 3),
            **{k: round(v, 4) for k, v in amt.items()},
            "currency": rec["currency"],
        })
        billed_total = amt["total_amount"]

        # ---- 重复计费：再补一行（同一运单第二次出现）----
        if kind == "duplicate_billing":
            line_no += 1
            invoice.append({
                "invoice_line_id": f"L{line_no:07d}",
                "invoice_batch": batch + "-RE",
                "shipment_id": sid,
                "invoice_date": (ship_date + timedelta(days=rng.randint(20, 40))).isoformat(),
                "billed_zone": zone,
                "service_level": service,
                "billed_weight_kg": round(weight, 3),
                **{k: round(v, 4) for k, v in expected.items()},
                "currency": rec["currency"],
            })
            billed_total = round(billed_total + exp_total, 4)

        impact = round(billed_total - exp_total, 4)
        truth.append({
            "shipment_id": sid,
            "is_discrepancy": 0 if kind == "none" else 1,
            "discrepancy_type": kind,
            "expected_total": round(exp_total, 2),
            "billed_total": round(billed_total, 2),
            "impact_amount": round(impact, 2),
            "note": {
                "none": "正常",
                "weight_inflated": f"计费重量由 {weight:.3f}kg 虚增至 {billed_weight:.3f}kg",
                "wrong_rate": f"分区由 {zone} 错记为 {billed_zone}",
                "duplicate_billing": "同一运单重复计费一次",
                "amount_mismatch": "各项金额正确但总额与明细加总不符",
            }[kind],
        })

    os.makedirs(OUT_DIR, exist_ok=True)
    with open(args.out_invoice, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=INVOICE_FIELDS)
        w.writeheader()
        w.writerows(invoice)
    with open(args.out_truth, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=TRUTH_FIELDS)
        w.writeheader()
        w.writerows(truth)

    n_bad = sum(t["is_discrepancy"] for t in truth)
    total_impact = sum(t["impact_amount"] for t in truth if isinstance(t["impact_amount"], (int, float)))
    print(f"✅ 承运商账单：{len(invoice):,} 行 / {len(truth):,} 个运单 → {args.out_invoice}")
    print(f"✅ 标注真值  ：{n_bad:,} 个异常运单（{n_bad / len(truth):.2%}）→ {args.out_truth}")
    print(f"💰 差异金额影响合计：{total_impact:,.2f} USD")
    print("   注入比例：" + "  ".join(f"{k}={v:.1%}" for k, v in probs.items()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
