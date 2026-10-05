#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
run_all.py · 一键跑通对账全流程
================================

按顺序执行：
    Step 1  构建内部系统记录
    Step 2  构建承运商费率表
    Step 3  合成承运商账单（注入 5 类差异）
    Step 4  账单核对 + 效果评估

用法：
    python3 run_all.py                        # 默认 2 万条运单
    python3 run_all.py --rows 50000           # 放大规模
    python3 run_all.py --source olist         # 用 Olist 真实数据做底表
    python3 run_all.py --weight-min-delta 0   # 跑阈值对照实验
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(BASE_DIR, "src")


def run(title: str, args: list[str]) -> None:
    print("\n" + "=" * 74)
    print(f"▶ {title}")
    print("=" * 74)
    t0 = time.time()
    result = subprocess.run([sys.executable, *args], cwd=SRC)
    if result.returncode != 0:
        print(f"\n❌ 执行失败：{' '.join(args)}", file=sys.stderr)
        raise SystemExit(result.returncode)
    print(f"   ⏱  耗时 {time.time() - t0:.1f}s")


def main() -> int:
    ap = argparse.ArgumentParser(description="一键跑通对账全流程")
    ap.add_argument("--rows", type=int, default=20000)
    ap.add_argument("--source", choices=["synthetic", "olist"], default="synthetic")
    ap.add_argument("--seed", type=int, default=42)
    # 透传给 Step 4 的阈值参数
    ap.add_argument("--weight-pct", type=float, default=0.05)
    ap.add_argument("--weight-min-delta", type=float, default=0.05)
    args = ap.parse_args()

    run("Step 1 · 构建内部系统记录",
        ["step1_internal_records.py", "--source", args.source,
         "--rows", str(args.rows), "--seed", str(args.seed)])
    run("Step 2 · 构建承运商费率表", ["step2_rate_card.py"])
    run("Step 3 · 合成承运商账单", ["step3_carrier_invoice.py"])
    run("Step 4 · 账单核对与效果评估",
        ["reconcile.py", "--weight-pct", str(args.weight_pct),
         "--weight-min-delta", str(args.weight_min_delta)])

    print("\n" + "=" * 74)
    print("✅ 全流程完成")
    print("=" * 74)
    data = os.path.join(BASE_DIR, "data")
    for name in ("internal_records.csv", "rate_card.csv", "carrier_invoice.csv",
                 "ground_truth.csv", "discrepancies.csv", "reconciliation_summary.json"):
        p = os.path.join(data, name)
        if os.path.exists(p):
            size = os.path.getsize(p) / 1024
            print(f"   {name:<32}{size:>10,.1f} KB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
