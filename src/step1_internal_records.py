#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Step 1 · 构建「内部系统记录」发货真相表
=========================================

这是对账项目的数据底座：内部系统记录的应该是**真实发生的发货事实**
（实际重量、体积、目的地、服务等级），后续承运商账单以它为基准去核对。

两种数据来源
------------
1. `--source synthetic`（默认）
   按真实物流分布生成合成发货记录，**零依赖、立刻可跑**。
   分布参考真实跨境小包特征：多数 0.2–3kg，少量重货到 30kg；
   国家/地区按跨境直发的主要目的地加权。

2. `--source olist`
   读取 Kaggle 公开数据集 **Brazilian E-Commerce Public Dataset by Olist**
   （CC BY-NC-SA 4.0，仅限非商业用途）。
   把 `olist_order_items / products / customers / orders` 四张表 join 成内部记录：
   - 重量、体积来自 `olist_products_dataset`
   - 目的地来自 `olist_customers_dataset`
   - 下单/发货时间来自 `olist_orders_dataset`
   详见 README「数据来源与许可」。

产出
----
    data/internal_records.csv
"""

from __future__ import annotations

import argparse
import csv
import os
import random
from datetime import date, timedelta

# --------------------------------------------------------------------------
# 配置
# --------------------------------------------------------------------------
OUT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
RAW_DIR = os.path.join(OUT_DIR, "raw")
DIVISOR = 6000  # 体积重除数：长×宽×高(cm) / 6000 = kg（航空/跨境小包常用）

# 跨境直发目的地加权（国家 → (权重, 分区)）
DEST_COUNTRIES = [
    ("US", 30, 5), ("GB", 10, 6), ("DE", 9, 6), ("FR", 6, 6), ("CA", 7, 5),
    ("AU", 7, 8), ("JP", 6, 3), ("KR", 4, 3), ("SG", 4, 4), ("MY", 3, 4),
    ("TH", 3, 4), ("VN", 2, 4), ("PH", 2, 4), ("ID", 2, 4), ("IN", 2, 7),
    ("BR", 2, 9), ("MX", 2, 5), ("NL", 2, 6), ("ES", 2, 6), ("IT", 2, 6),
]
SERVICE_LEVELS = [("economy", 5), ("standard", 75), ("express", 20)]

# Olist 州 → 分区映射（巴西境内，按地理远近粗分）
OLIST_STATE_ZONE = {
    "SP": 1, "RJ": 2, "MG": 2, "ES": 2, "PR": 2, "SC": 2, "RS": 3,
    "GO": 3, "DF": 3, "MT": 4, "MS": 4, "BA": 5, "SE": 5, "PE": 5,
    "AL": 5, "PB": 5, "RN": 5, "CE": 6, "PI": 6, "MA": 6, "PA": 7,
    "AP": 7, "AM": 8, "RR": 8, "AC": 8, "RO": 7, "TO": 6,
}


def _weighted_choice(rng: random.Random, items, weight_idx=1):
    total = sum(it[weight_idx] for it in items)
    r = rng.uniform(0, total)
    acc = 0.0
    for it in items:
        acc += it[weight_idx]
        if r <= acc:
            return it
    return items[-1]


def _gen_weight_kg(rng: random.Random) -> float:
    """按跨境小包的真实分布生成实重（kg）。"""
    r = rng.random()
    if r < 0.55:      # 小件
        return round(rng.uniform(0.05, 1.0), 3)
    if r < 0.85:      # 中件
        return round(rng.uniform(1.0, 3.0), 3)
    if r < 0.96:      # 大件
        return round(rng.uniform(3.0, 10.0), 3)
    return round(rng.uniform(10.0, 30.0), 3)  # 重货


def _gen_dims_cm(rng: random.Random, weight_kg: float):
    """尺寸与重量弱相关，保证体积重有时会超过实重（这正是对账要识别的场景）。"""
    base = max(8.0, (weight_kg ** (1 / 3)) * 11.0)
    length = round(base * rng.uniform(0.9, 1.6), 1)
    width = round(base * rng.uniform(0.7, 1.2), 1)
    height = round(base * rng.uniform(0.4, 1.0), 1)
    return length, width, height


def build_synthetic(n: int, seed: int, start: date, days: int) -> list[dict]:
    rng = random.Random(seed)
    rows = []
    for i in range(1, n + 1):
        country, _, zone = _weighted_choice(rng, DEST_COUNTRIES)
        service = _weighted_choice(rng, SERVICE_LEVELS)[0]
        weight = _gen_weight_kg(rng)
        length, width, height = _gen_dims_cm(rng, weight)
        vol_weight = round(length * width * height / DIVISOR, 3)
        rows.append({
            "shipment_id": f"SHP{2026000000 + i}",
            "order_id": f"ORD{rng.randint(100000, 999999)}{i:05d}",
            "seller_id": f"SEL{rng.randint(1, 400):04d}",
            "ship_date": (start + timedelta(days=rng.randrange(days))).isoformat(),
            "dest_country": country,
            "dest_state": "-",
            "dest_zip_prefix": f"{rng.randint(10000, 99999)}",
            "dest_zone": zone,
            "service_level": service,
            "actual_weight_g": int(round(weight * 1000)),
            "length_cm": length,
            "width_cm": width,
            "height_cm": height,
            "volumetric_weight_kg": vol_weight,
            "chargeable_weight_kg": round(max(weight, vol_weight), 3),
            "declared_value": round(rng.uniform(5, 400), 2),
            "currency": "USD",
        })
    return rows


def build_from_olist(start: date) -> list[dict]:
    """从 Olist 原始 CSV 构建内部记录（需先把 Kaggle 数据放到 data/raw/）。"""
    need = {
        "order_items": os.path.join(RAW_DIR, "olist_order_items_dataset.csv"),
        "products": os.path.join(RAW_DIR, "olist_products_dataset.csv"),
        "customers": os.path.join(RAW_DIR, "olist_customers_dataset.csv"),
        "orders": os.path.join(RAW_DIR, "olist_orders_dataset.csv"),
    }
    missing = [k for k, p in need.items() if not os.path.exists(p)]
    if missing:
        raise SystemExit(
            "缺少 Olist 原始文件：" + ", ".join(missing) + "\n"
            f"请从 Kaggle 下载 'Brazilian E-Commerce Public Dataset by Olist' 后解压到 {RAW_DIR}/\n"
            "或改用默认的 --source synthetic。"
        )

    def load(path, key):
        out = {}
        with open(path, newline="", encoding="utf-8") as fh:
            for r in csv.DictReader(fh):
                out[r[key]] = r
        return out

    products = load(need["products"], "product_id")
    customers = load(need["customers"], "customer_id")
    orders = load(need["orders"], "order_id")

    rows = []
    with open(need["order_items"], newline="", encoding="utf-8") as fh:
        for idx, it in enumerate(csv.DictReader(fh), start=1):
            prod = products.get(it["product_id"], {})
            order = orders.get(it["order_id"], {})
            cust = customers.get(order.get("customer_id", ""), {})

            def fnum(d, k, default=0.0):
                try:
                    v = d.get(k) or ""
                    return float(v) if v not in ("", "nan") else default
                except ValueError:
                    return default

            w_g = fnum(prod, "product_weight_g", 500.0) or 500.0
            L = fnum(prod, "product_length_cm", 20.0) or 20.0
            W = fnum(prod, "product_width_cm", 15.0) or 15.0
            H = fnum(prod, "product_height_cm", 10.0) or 10.0
            weight_kg = round(w_g / 1000.0, 3)
            vol_kg = round(L * W * H / DIVISOR, 3)
            state = (cust.get("customer_state") or "SP").upper()

            rows.append({
                # 主键：一个订单行 = 一个运单。
                # 用「完整 order_id + 分隔符 + order_item_id」，避免截断碰撞与拼接歧义。
                # 早期实现写作 f"OL{order_id[-8:]}{order_item_id:>2}"：order_id 只取后 8 位十六进制，
                # 10 万订单规模下按生日问题估算期望约 1 次碰撞；且两位数 order_item_id 会错位 —— 已修正。
                "shipment_id": f"OL-{it['order_id']}-{it['order_item_id']}",
                "order_id": it["order_id"],
                "seller_id": it["seller_id"],
                # 发货日期 = 交承运商日期；缺失时兜底到「下单日期」而不是 --start-date。
                #
                # ⚠️ 这里曾经写成 `or start.isoformat()`，而 start 来自 --start-date（默认 2026-06-01，
                #    本是给合成模式造日期用的）。结果 Olist 里 1,783 个缺失交运日期的订单
                #    （几乎全是 canceled / unavailable / processing 等**未真正发出**的订单）
                #    被塞进了 2026 年的假日期 —— 而 Olist 真实数据只到 2018-10-17。
                #    order_purchase_timestamp 缺失率为 0，是可靠的兜底来源。
                "ship_date": (order.get("order_delivered_carrier_date") or "")[:10]
                             or (order.get("order_purchase_timestamp") or "")[:10]
                             or start.isoformat(),
                "dest_country": "BR",
                "dest_state": state,
                "dest_zip_prefix": (cust.get("customer_zip_code_prefix") or "")[:5],
                "dest_zone": OLIST_STATE_ZONE.get(state, 5),
                "service_level": "standard",
                "actual_weight_g": int(w_g),
                "length_cm": L, "width_cm": W, "height_cm": H,
                "volumetric_weight_kg": vol_kg,
                "chargeable_weight_kg": round(max(weight_kg, vol_kg), 3),
                "declared_value": round(fnum(it, "price", 0.0), 2),
                "currency": "BRL",
            })
    return rows


def main() -> int:
    ap = argparse.ArgumentParser(description="Step 1 · 构建内部系统记录")
    ap.add_argument("--source", choices=["synthetic", "olist"], default="synthetic")
    ap.add_argument("--rows", type=int, default=20000, help="合成模式的行数")
    ap.add_argument("--seed", type=int, default=42, help="随机种子（保证可复现）")
    ap.add_argument("--start-date", default="2026-06-01")
    ap.add_argument("--days", type=int, default=90, help="发货日期跨度")
    ap.add_argument("--out", default=os.path.join(OUT_DIR, "internal_records.csv"))
    args = ap.parse_args()

    start = date.fromisoformat(args.start_date)
    rows = (build_synthetic(args.rows, args.seed, start, args.days)
            if args.source == "synthetic" else build_from_olist(start))

    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)

    print(f"✅ 内部系统记录：{len(rows):,} 行 → {args.out}")
    print(f"   来源={args.source}  日期={rows[0]['ship_date']} ~ {max(r['ship_date'] for r in rows)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
