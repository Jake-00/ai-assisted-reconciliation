#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Step 2 · 构建承运商费率表（Rate Card）
========================================

费率表是「对账的标尺」：没有它，就无法判断承运商收得对不对。

结构（贴近真实跨境承运商报价）
------------------------------
    分区(zone) × 服务等级(service_level) × 重量档(weight bracket)
        → 基础费 base_rate ＋ 续重单价 per_kg_rate
        ＋ 燃油附加费 fuel_surcharge_pct（按小计百分比）
        ＋ 偏远附加费 remote_surcharge（仅远区，固定额）

计费公式
--------
    小计 subtotal = base_rate + per_kg_rate × 计费重量
    应付 expected = subtotal × (1 + fuel_surcharge_pct) + remote_surcharge

两种模式
--------
1. `--source derived`（默认）：按上表公式生成一套完整费率表，规则透明、可解释。
2. `--source olist`：从 Olist 真实 `freight_value` 反推各 (zone, 重量档) 的
   中位运费作为费率，用于「用真实数据校准费率」的演示。

产出
----
    data/rate_card.csv
"""

from __future__ import annotations

import argparse
import csv
import os
import statistics

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_DIR = os.path.join(BASE_DIR, "data")

# 重量档（下界含、上界不含；最后一档上界 None 表示无上限）
WEIGHT_BRACKETS = [
    (0.0, 0.5), (0.5, 1.0), (1.0, 2.0), (2.0, 5.0),
    (5.0, 10.0), (10.0, 20.0), (20.0, 30.0), (30.0, None),
]

# 分区基础费与续重单价（zone → (base, per_kg)）；越远越贵
ZONE_PRICING = {
    1: (2.10, 1.10), 2: (2.30, 1.25), 3: (2.55, 1.45), 4: (2.80, 1.70),
    5: (3.20, 2.05), 6: (3.45, 2.35), 7: (3.80, 2.75), 8: (4.15, 3.10),
    9: (4.50, 3.50),
}
SERVICE_MULTIPLIER = {"economy": 0.85, "standard": 1.00, "express": 1.65}
FUEL_SURCHARGE = {"economy": 0.13, "standard": 0.15, "express": 0.18}
REMOTE_SURCHARGE = {7: 1.50, 8: 2.20, 9: 2.80}  # 仅远区加收


def build_derived() -> list[dict]:
    rows = []
    for zone, (base, per_kg) in ZONE_PRICING.items():
        for service, mult in SERVICE_MULTIPLIER.items():
            for lo, hi in WEIGHT_BRACKETS:
                # 续重单价按档位递减（重量越大单价越低，真实报价的常见形态）
                bracket_discount = {0: 1.00, 1: 1.00, 2: 0.95, 3: 0.88,
                                    4: 0.80, 5: 0.72, 6: 0.66, 7: 0.60}[WEIGHT_BRACKETS.index((lo, hi))]
                rows.append({
                    "zone": zone,
                    "service_level": service,
                    "weight_min_kg": lo,
                    "weight_max_kg": "" if hi is None else hi,
                    "base_rate": round(base * mult, 4),
                    "per_kg_rate": round(per_kg * mult * bracket_discount, 4),
                    "fuel_surcharge_pct": FUEL_SURCHARGE[service],
                    "remote_surcharge": round(REMOTE_SURCHARGE.get(zone, 0.0) * mult, 4),
                    "currency": "USD",
                })
    return rows


def build_from_olist(internal_path: str, min_samples: int = 30) -> list[dict]:
    """用 Olist 真实 freight_value 反推费率（按 zone × 重量档 取中位运费）。"""
    with open(internal_path, newline="", encoding="utf-8") as fh:
        records = list(csv.DictReader(fh))
    # Olist 原始运费存在 order_items 里，这里需要额外文件；若不存在则退回 derived
    items_path = os.path.join(OUT_DIR, "raw", "olist_order_items_dataset.csv")
    if not os.path.exists(items_path):
        print("⚠️  未找到 olist_order_items_dataset.csv，退回 derived 模式")
        return build_derived()

    freight = {}
    with open(items_path, newline="", encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            freight[r["order_id"]] = float(r["freight_value"] or 0)

    buckets: dict[tuple, list[float]] = {}
    for rec in records:
        f = freight.get(rec["order_id"])
        if f is None:
            continue
        w = float(rec["chargeable_weight_kg"])
        for lo, hi in WEIGHT_BRACKETS:
            if w >= lo and (hi is None or w < hi):
                buckets.setdefault((rec["dest_zone"], lo, hi), []).append(f)
                break

    rows = []
    for (zone, lo, hi), values in sorted(buckets.items()):
        if len(values) < min_samples:
            continue
        rows.append({
            "zone": zone, "service_level": "standard",
            "weight_min_kg": lo, "weight_max_kg": "" if hi is None else hi,
            "base_rate": round(statistics.median(values), 4),
            "per_kg_rate": 0.0,
            "fuel_surcharge_pct": 0.0,
            "remote_surcharge": 0.0,
            "currency": "BRL",
            "sample_size": len(values),
        })
    return rows


def main() -> int:
    ap = argparse.ArgumentParser(description="Step 2 · 构建承运商费率表")
    ap.add_argument("--source", choices=["derived", "olist"], default="derived")
    ap.add_argument("--internal", default=os.path.join(OUT_DIR, "internal_records.csv"))
    ap.add_argument("--out", default=os.path.join(OUT_DIR, "rate_card.csv"))
    args = ap.parse_args()

    rows = build_derived() if args.source == "derived" else build_from_olist(args.internal)
    if not rows:
        raise SystemExit("费率表为空，请检查输入数据")

    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    fields = list(rows[0].keys())
    with open(args.out, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)

    print(f"✅ 费率表：{len(rows)} 行 → {args.out}")
    print(f"   维度：{len(ZONE_PRICING)} 个分区 × {len(SERVICE_MULTIPLIER)} 个服务等级 × {len(WEIGHT_BRACKETS)} 个重量档")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
