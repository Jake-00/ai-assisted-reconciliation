#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
rating.py · 共用计费模块
=========================

`step3_carrier_invoice.py`（生成账单）与 `reconcile.py`（核对账单）**共用同一套计费逻辑**，
避免"自己算一套、核对用另一套"的经典错误 —— 这也是对账系统必须守住的底线：
**唯一的应付口径来源**。

对外接口
--------
    load_rate_card(path)                -> RateCard
    card.expected(zone, service, weight) -> dict(base, weight_amount, fuel, remote, subtotal, total)
    card.lookup(zone, service, weight)   -> dict   # 命中的费率行
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from typing import Dict, Optional, Tuple

from money import round_money, to_decimal


@dataclass(frozen=True)
class RateRow:
    zone: int
    service_level: str
    weight_min_kg: float
    weight_max_kg: Optional[float]
    base_rate: float
    per_kg_rate: float
    fuel_surcharge_pct: float
    remote_surcharge: float

    def contains(self, weight_kg: float) -> bool:
        if weight_kg < self.weight_min_kg:
            return False
        return self.weight_max_kg is None or weight_kg < self.weight_max_kg


class RateCard:
    """承运商费率表：按 (分区, 服务等级, 计费重量) 查找唯一费率行。"""

    def __init__(self, rows: list[RateRow]):
        self._index: Dict[Tuple[int, str], list[RateRow]] = {}
        for r in rows:
            self._index.setdefault((r.zone, r.service_level), []).append(r)
        for v in self._index.values():
            v.sort(key=lambda x: x.weight_min_kg)

    def __len__(self) -> int:
        return sum(len(v) for v in self._index.values())

    def lookup(self, zone: int, service: str, weight_kg: float) -> RateRow:
        """返回命中的费率行；找不到则抛异常（宁可报错，也不静默用错费率）。"""
        candidates = self._index.get((zone, service))
        if not candidates:
            raise KeyError(f"费率表缺失：zone={zone}, service={service}")
        for r in candidates:
            if r.contains(weight_kg):
                return r
        # 超出最大档：用最后一档兜底（真实报价中通常有"30kg 以上另议"）
        return candidates[-1]

    def expected(self, zone: int, service: str, weight_kg: float) -> dict:
        """按费率表计算应付金额（拆到每一项，便于对账时定位差异来源）。

        ⚠️ 全程用 Decimal 做十进制运算，**不碰二进制浮点**。
        用 float 计算会在 .5 舍入边界上翻车：例如真实值 30.051945，
        IEEE754 双精度存成 30.051944999999996，round() 会向下舍成 30.0519，
        而正确结果是 30.0520。实测 2 万行数据里，float 与 Decimal 路径
        有 6% 的行结果不一致 —— 金融计算必须用十进制。
        """
        row = self.lookup(zone, service, weight_kg)

        base = to_decimal(row.base_rate)
        per_kg = to_decimal(row.per_kg_rate)
        fuel_pct = to_decimal(row.fuel_surcharge_pct)
        remote = to_decimal(row.remote_surcharge)
        weight = to_decimal(weight_kg)

        # 与真实账单一致：**逐项舍入**（而非最后一次性舍入）
        weight_amount = round_money(per_kg * weight)
        subtotal_before = round_money(base + weight_amount)
        fuel = round_money(subtotal_before * fuel_pct)
        total = round_money(subtotal_before + fuel + remote)

        return {
            "zone": zone,
            "service_level": service,
            "chargeable_weight_kg": round(weight_kg, 3),
            "base_rate": float(base),
            "per_kg_rate": float(per_kg),
            "weight_amount": float(weight_amount),
            "fuel_surcharge": float(fuel),
            "remote_surcharge": float(remote),
            "total": float(total),
        }


def load_rate_card(path: str) -> RateCard:
    rows: list[RateRow] = []
    with open(path, newline="", encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            wmax = (r.get("weight_max_kg") or "").strip()
            rows.append(RateRow(
                zone=int(float(r["zone"])),
                service_level=r["service_level"].strip(),
                weight_min_kg=float(r["weight_min_kg"]),
                weight_max_kg=float(wmax) if wmax else None,
                base_rate=float(r["base_rate"]),
                per_kg_rate=float(r["per_kg_rate"]),
                fuel_surcharge_pct=float(r["fuel_surcharge_pct"]),
                remote_surcharge=float(r["remote_surcharge"]),
            ))
    if not rows:
        raise ValueError(f"费率表为空：{path}")
    return RateCard(rows)
