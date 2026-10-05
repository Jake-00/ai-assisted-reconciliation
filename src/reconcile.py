#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Step 4 · 账单核对（对账主逻辑，5 条校验规则）
==============================================

这是本项目的核心交付物：把「承运商账单」逐条核对回「内部系统记录 + 费率表」，
识别差异、量化金额影响、按根因归类。

5 条校验规则
------------
| 规则 | 名称 | 判定逻辑 | 典型根因 |
| --- | --- | --- | --- |
| **R1** | 未入账 | 内部有发货记录，账单中查无此运单 | 承运商漏开票 / 数据丢失 |
| **R2** | 重复计费 | 同一运单在账单中出现 > 1 次 | 系统重复推送 / 人工重复录入 |
| **R3** | 计费重量偏差 | 账单重量与应付重量偏差 > 5%（且绝对差 > 0.05kg） | 承运商按体积重或错误称重计费 |
| **R4** | 费率不符 | 分区不一致，或各项费用与费率表算出的不符（容差 1%） | 用错分区 / 用错重量档费率 |
| **R5** | 金额加总错误 | 行总额 ≠ 各项费用之和（容差 $0.05） | 账单生成逻辑缺陷 |

设计要点
--------
1. **同一应付口径**：应付金额由 `rating.py` 计算，与生成账单时用的是**同一套逻辑**。
2. **按根因归类，不重复报**：一条账单行按 R3 → R4 → R5 的优先级命中**首个**规则，
   避免"一个重量虚高同时报成重量问题 + 费率问题"，导致差异被重复计算。
3. **金额影响可量化**：每条发现都给出 `diff_amount`（正数 = 承运商多收）。
4. **可选评估**：若存在 `ground_truth.csv`，自动计算查全率 / 查准率。

产出
----
    data/discrepancies.csv              差异清单（可直接交财务跟进）
    data/reconciliation_summary.json    汇总指标
"""

from __future__ import annotations

import argparse
import csv
import json
import os
from collections import defaultdict

from money import round_cents, round_pct, to_decimal
from rating import load_rate_card

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_DIR = os.path.join(BASE_DIR, "data")

# 规则阈值（可按合同调整）
WEIGHT_DEVIATION_PCT = 0.05   # 计费重量相对偏差阈值 5%
WEIGHT_MIN_DELTA_KG = 0.05    # 计费重量绝对差下限：避免小件被称重噪声触发（见 README「精度/召回的权衡」）
AMOUNT_TOLERANCE = 0.01       # 各项费用容差 1%
TOTAL_TOLERANCE = 0.05        # 总额与明细加总容差 $0.05

RULE_NAMES = {
    "R1_MISSING_FROM_INVOICE": "未入账：内部有记录、账单无此行",
    "R2_DUPLICATE_BILLING": "重复计费：同一运单多次出现",
    "R3_WEIGHT_DEVIATION": "计费重量偏差超阈值",
    "R4_RATE_MISMATCH": "费率不符：分区或费用与费率表不一致",
    "R5_AMOUNT_MISMATCH": "金额加总错误：总额与各项费用之和不符",
}

FIELDS = [
    "finding_id", "shipment_id", "rule", "rule_name", "severity",
    "invoice_line_id", "billed_zone", "expected_zone",
    "billed_weight_kg", "expected_weight_kg",
    "billed_amount", "expected_amount", "diff_amount", "detail",
]


def _severity(impact: float) -> str:
    """按金额分档（high >= 5 元，medium >= 1 元，否则 low）。

    **先按「分」取整再分档** —— 金额的精度是分，而程序算出来的差值带着 IEEE754 噪声：

        账单总额 26.5647
        分项之和 4.15 + 16.1671 + 3.0476 + 2.2 = 25.564700000000002   ← 误差在「求和」时产生
        差值 = 0.9999999999999964                                     ← 差一点不到 1

        直接比：0.9999999999999964 >= 1.0  → False → low     ❌ 但账面明明差 1.00 元
        先取整：round_cents(差值) = 1.00   → True  → medium  ✅

    ⚠️ 必须用 `money.round_cents`（Decimal + ROUND_HALF_UP），**不能用内置 `round(x, 2)`**：
    后者在二进制浮点上做银行家舍入，在 .5 边界会给出不同结果（round(0.995,2)=0.99
    vs round_cents(0.995)=1.00），足以翻转档位。实测 PostgreSQL / DuckDB 的十进制路径
    都与 round_cents 一致。详见 README「问题 5」。
    """
    a = abs(round_cents(impact))
    if a >= 5.0:
        return "high"
    if a >= 1.0:
        return "medium"
    return "low"


def reconcile(internal_path: str, invoice_path: str, rate_card_path: str,
              weight_deviation_pct: float = WEIGHT_DEVIATION_PCT,
              weight_min_delta_kg: float = WEIGHT_MIN_DELTA_KG,
              amount_tolerance: float = AMOUNT_TOLERANCE,
              total_tolerance: float = TOTAL_TOLERANCE):
    rate_card = load_rate_card(rate_card_path)

    with open(internal_path, newline="", encoding="utf-8") as fh:
        internal = {r["shipment_id"]: r for r in csv.DictReader(fh)}
    with open(invoice_path, newline="", encoding="utf-8") as fh:
        invoice_lines = list(csv.DictReader(fh))

    # 按运单聚合账单行
    by_shipment: dict[str, list[dict]] = defaultdict(list)
    for line in invoice_lines:
        by_shipment[line["shipment_id"]].append(line)

    findings: list[dict] = []
    fid = 0

    def add(sid, rule, severity, line_id, billed_zone, exp_zone, bw, ew, ba, ea, detail):
        nonlocal fid
        fid += 1
        findings.append({
            "finding_id": f"F{fid:06d}",
            "shipment_id": sid,
            "rule": rule,
            "rule_name": RULE_NAMES[rule],
            "severity": severity,
            "invoice_line_id": line_id,
            "billed_zone": billed_zone,
            "expected_zone": exp_zone,
            "billed_weight_kg": "" if bw is None else round(bw, 3),
            "expected_weight_kg": "" if ew is None else round(ew, 3),
            "billed_amount": "" if ba is None else round(ba, 4),
            "expected_amount": "" if ea is None else round(ea, 4),
            "diff_amount": round((ba or 0) - (ea or 0), 4),
            "detail": detail,
        })

    # ---------------- R1 未入账（运单级） ----------------
    for sid, rec in internal.items():
        if sid not in by_shipment:
            exp = rate_card.expected(int(rec["dest_zone"]), rec["service_level"],
                                     float(rec["chargeable_weight_kg"]))["total"]
            add(sid, "R1_MISSING_FROM_INVOICE", _severity(exp), "", "", rec["dest_zone"],
                None, float(rec["chargeable_weight_kg"]), 0.0, exp,
                f"内部记录 {rec['ship_date']} 发货，账单中无对应行")

    # ---------------- 逐运单核对 ----------------
    for sid, lines in by_shipment.items():
        rec = internal.get(sid)
        if rec is None:
            # 账单里有、内部没有 —— 现实中也要查（可能是内部数据缺失）
            for line in lines:
                add(sid, "R4_RATE_MISMATCH", "high", line["invoice_line_id"],
                    line["billed_zone"], "", float(line["billed_weight_kg"]), None,
                    float(line["total_amount"]), None,
                    "账单中的运单号在内部系统不存在，需核实是否为内部数据缺失")
            continue

        zone = int(rec["dest_zone"])
        service = rec["service_level"]
        weight = float(rec["chargeable_weight_kg"])
        exp = rate_card.expected(zone, service, weight)

        # ---------------- R2 重复计费 ----------------
        if len(lines) > 1:
            # 保留最早一行视为正常，其余视为多收，逐条报出
            ordered = sorted(lines, key=lambda x: x["invoice_line_id"])
            for extra in ordered[1:]:
                amt = float(extra["total_amount"])
                add(sid, "R2_DUPLICATE_BILLING", _severity(amt), extra["invoice_line_id"],
                    extra["billed_zone"], zone, float(extra["billed_weight_kg"]), weight,
                    amt, 0.0,
                    f"该运单在账单中出现 {len(lines)} 次（账单批次 {extra['invoice_batch']}），本行为多收")
            continue  # 重复计费的运单不再叠加其他规则，避免重复计算

        line = lines[0]
        billed_zone = int(line["billed_zone"])
        billed_weight = float(line["billed_weight_kg"])
        billed_total = float(line["total_amount"])
        components = (float(line["base_amount"]) + float(line["weight_amount"])
                      + float(line["fuel_surcharge"]) + float(line["remote_surcharge"]))

        # ---------------- R3 计费重量偏差 ----------------
        delta = abs(billed_weight - weight)
        dev = delta / weight if weight else 0.0
        if dev > weight_deviation_pct and delta > weight_min_delta_kg:
            # 偏差百分比的显示口径：
            #   ① 必须用 Decimal（float 在 .5 边界会翻车，如 0.114/0.608 = 18.75% → 18.7%）
            #   ② 必须**先把两个操作数转成十进制再相减**，而不是先做 float 减法再转
            #      —— 否则 Python 会带着完整浮点精度（Decimal('0.30699999999999994')），
            #         而 SQL 的 CAST(... AS DECIMAL(18,6)) 会舍到 6 位，两边对不上。
            #      这个坑是在**真实 Olist 数据**上跑双实现交叉验证时才暴露的。
            d_billed, d_weight = to_decimal(billed_weight), to_decimal(weight)
            dev_pct = round_pct((d_billed - d_weight).copy_abs() / d_weight * 100)
            add(sid, "R3_WEIGHT_DEVIATION", _severity(billed_total - exp["total"]),
                line["invoice_line_id"], billed_zone, zone, billed_weight, weight,
                billed_total, exp["total"],
                f"账单计费重量 {billed_weight:.3f}kg vs 应付 {weight:.3f}kg，偏差 {dev_pct}%")
            continue

        # ---------------- R4 费率不符 ----------------
        if billed_zone != zone:
            add(sid, "R4_RATE_MISMATCH", _severity(billed_total - exp["total"]),
                line["invoice_line_id"], billed_zone, zone, billed_weight, weight,
                billed_total, exp["total"],
                f"账单分区 {billed_zone} vs 应付分区 {zone}，按更高分区计费")
            continue

        for label, billed_v, exp_v in (
            ("基础费", float(line["base_amount"]), exp["base_rate"]),
            ("续重费", float(line["weight_amount"]), exp["weight_amount"]),
            ("燃油附加", float(line["fuel_surcharge"]), exp["fuel_surcharge"]),
            ("偏远附加", float(line["remote_surcharge"]), exp["remote_surcharge"]),
        ):
            if exp_v > 0 and abs(billed_v - exp_v) / exp_v > amount_tolerance:
                add(sid, "R4_RATE_MISMATCH", _severity(billed_total - exp["total"]),
                    line["invoice_line_id"], billed_zone, zone, billed_weight, weight,
                    billed_total, exp["total"],
                    f"{label} 与费率表不符：账单 {billed_v:.4f} vs 应付 {exp_v:.4f}")
                break
        else:
            # ---------------- R5 金额加总错误 ----------------
            if abs(billed_total - components) > total_tolerance:
                add(sid, "R5_AMOUNT_MISMATCH", _severity(billed_total - components),
                    line["invoice_line_id"], billed_zone, zone, billed_weight, weight,
                    billed_total, components,
                    f"总额 {billed_total:.4f} ≠ 各项之和 {components:.4f}，差 {billed_total - components:+.4f}")

    return findings, internal, by_shipment


def summarize(findings: list[dict]) -> dict:
    by_rule = defaultdict(lambda: {"count": 0, "impact": 0.0})
    for f in findings:
        by_rule[f["rule"]]["count"] += 1
        by_rule[f["rule"]]["impact"] += f["diff_amount"]

    # 金额影响拆成三类，便于财务直接行动
    overbilled = sum(f["diff_amount"] for f in findings if f["diff_amount"] > 0)          # 应追回
    underbilled = sum(-f["diff_amount"] for f in findings if f["diff_amount"] < 0)        # 待确认（账单偏少）
    missing = sum(-f["diff_amount"] for f in findings if f["rule"] == "R1_MISSING_FROM_INVOICE")

    return {
        "total_findings": len(findings),
        "amount_impact": {
            "overbilled_to_recover": round(overbilled, 2),      # 承运商多收 → 应追回
            "unbilled_to_confirm": round(missing, 2),           # 未入账 → 需与承运商确认
            "other_underbilled": round(underbilled - missing, 2),
            "net": round(overbilled - underbilled, 2),
        },
        "by_rule": {
            k: {"name": RULE_NAMES[k], "count": v["count"], "impact": round(v["impact"], 2)}
            for k, v in sorted(by_rule.items())
        },
        "by_severity": {
            s: sum(1 for f in findings if f["severity"] == s)
            for s in ("high", "medium", "low")
        },
    }


def evaluate(findings: list[dict], truth_path: str) -> dict:
    """与 ground truth 对比，计算查全率 / 查准率（仅在合成数据下可用）。"""
    with open(truth_path, newline="", encoding="utf-8") as fh:
        truth = {r["shipment_id"]: r for r in csv.DictReader(fh)}

    truth_bad = {sid for sid, t in truth.items() if t["is_discrepancy"] == "1"}
    flagged = {f["shipment_id"] for f in findings}

    tp = len(truth_bad & flagged)
    fn = len(truth_bad - flagged)
    fp = len(flagged - truth_bad)

    # 按类型核对
    flagged_rules = defaultdict(set)
    for f in findings:
        flagged_rules[f["shipment_id"]].add(f["rule"])

    type_map = {
        "R1_MISSING_FROM_INVOICE": "missing_from_invoice",
        "R2_DUPLICATE_BILLING": "duplicate_billing",
        "R3_WEIGHT_DEVIATION": "weight_inflated",
        "R4_RATE_MISMATCH": "wrong_rate",
        "R5_AMOUNT_MISMATCH": "amount_mismatch",
    }
    type_hit = defaultdict(int)
    type_total = defaultdict(int)
    for t in truth.values():
        if t["is_discrepancy"] != "1":
            continue
        kind = t["discrepancy_type"]
        type_total[kind] += 1
        if type_map.get(next(iter(flagged_rules.get(t["shipment_id"], {""})), ""), "") == kind:
            type_hit[kind] += 1

    return {
        "ground_truth_discrepancies": len(truth_bad),
        "flagged": len(flagged),
        "true_positive": tp,
        "false_negative": fn,
        "false_positive": fp,
        "recall": round(tp / len(truth_bad), 4) if truth_bad else None,
        "precision": round(tp / len(flagged), 4) if flagged else None,
        "by_type_recall": {
            k: {"hit": type_hit[k], "total": type_total[k],
                "recall": round(type_hit[k] / type_total[k], 4) if type_total[k] else None}
            for k in sorted(type_total)
        },
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="Step 4 · 账单核对")
    ap.add_argument("--internal", default=os.path.join(OUT_DIR, "internal_records.csv"))
    ap.add_argument("--invoice", default=os.path.join(OUT_DIR, "carrier_invoice.csv"))
    ap.add_argument("--rate-card", default=os.path.join(OUT_DIR, "rate_card.csv"))
    ap.add_argument("--out", default=os.path.join(OUT_DIR, "discrepancies.csv"))
    ap.add_argument("--summary", default=os.path.join(OUT_DIR, "reconciliation_summary.json"))
    ap.add_argument("--truth", default=os.path.join(OUT_DIR, "ground_truth.csv"))
    ap.add_argument("--weight-pct", type=float, default=WEIGHT_DEVIATION_PCT,
                    help="计费重量相对偏差阈值（默认 0.05）")
    ap.add_argument("--weight-min-delta", type=float, default=WEIGHT_MIN_DELTA_KG,
                    help="计费重量绝对差下限 kg（默认 0.05；设 0 可提高召回但会引入噪声）")
    ap.add_argument("--amount-tolerance", type=float, default=AMOUNT_TOLERANCE)
    ap.add_argument("--total-tolerance", type=float, default=TOTAL_TOLERANCE)
    args = ap.parse_args()

    findings, internal, by_shipment = reconcile(
        args.internal, args.invoice, args.rate_card,
        weight_deviation_pct=args.weight_pct,
        weight_min_delta_kg=args.weight_min_delta,
        amount_tolerance=args.amount_tolerance,
        total_tolerance=args.total_tolerance,
    )
    findings.sort(key=lambda f: (f["rule"], -abs(f["diff_amount"])))

    with open(args.out, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(findings)

    summary = summarize(findings)
    summary["scope"] = {
        "internal_shipments": len(internal),
        "invoice_lines": sum(len(v) for v in by_shipment.values()),
        "thresholds": {
            "weight_deviation_pct": args.weight_pct,
            "weight_min_delta_kg": args.weight_min_delta,
            "amount_tolerance": args.amount_tolerance,
            "total_tolerance": args.total_tolerance,
        },
    }
    if os.path.exists(args.truth):
        summary["evaluation"] = evaluate(findings, args.truth)

    with open(args.summary, "w", encoding="utf-8") as fh:
        json.dump(summary, fh, ensure_ascii=False, indent=2)

    # ---- 控制台报告 ----
    ai = summary["amount_impact"]
    print("=" * 74)
    print("对账结果汇总")
    print("=" * 74)
    print(f"核对范围：内部运单 {len(internal):,} 条 / 账单 {summary['scope']['invoice_lines']:,} 行")
    print(f"发现差异：{summary['total_findings']:,} 条")
    print(f"  💰 承运商多收（应追回）：{ai['overbilled_to_recover']:>12,.2f} USD")
    print(f"  ❓ 未入账（需确认）    ：{ai['unbilled_to_confirm']:>12,.2f} USD")
    print(f"  📊 净影响              ：{ai['net']:>12,.2f} USD")
    print(f"严重度分布：high={summary['by_severity']['high']}  "
          f"medium={summary['by_severity']['medium']}  low={summary['by_severity']['low']}")
    print("-" * 74)
    print(f"{'规则':<34}{'条数':>8}{'金额影响':>16}")
    print("-" * 74)
    for rule, v in summary["by_rule"].items():
        print(f"{rule:<34}{v['count']:>8,}{v['impact']:>16,.2f}")
    print("-" * 74)

    if "evaluation" in summary:
        ev = summary["evaluation"]
        print("规则有效性评估（对比 ground truth）")
        print(f"  真实异常 {ev['ground_truth_discrepancies']:,} 个  |  "
              f"命中 {ev['true_positive']:,}  |  漏报 {ev['false_negative']:,}  |  误报 {ev['false_positive']:,}")
        print(f"  查全率 Recall = {ev['recall']:.2%}   查准率 Precision = {ev['precision']:.2%}")
        print("  根因归类命中率（同一运单是否被正确的规则命中）：")
        for k, v in ev["by_type_recall"].items():
            print(f"    {k:<22} {v['hit']:>5}/{v['total']:<5}  {v['recall']:.1%}")

    print("-" * 74)
    print(f"差异清单 → {args.out}")
    print(f"汇总指标 → {args.summary}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
