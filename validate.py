#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
validate.py · 双实现交叉验证
==============================

本项目用**两套完全独立的实现**做同一件事：

    A. `src/reconcile.py`      —— Python 过程式实现（逐条遍历、按优先级 classify）
    B. `sql/reconcile_duckdb.sql` —— DuckDB 集合式 SQL 实现（CASE 优先级 + 窗口函数）

对账系统最怕的不是"算错"，而是"**不知道自己算错了**"。
两套独立实现跑同一份数据、结果必须逐条一致 —— 这是最经济的一致性验证手段：
    · 同一处逻辑 bug 很难在两套不同范式的实现里同时出现；
    · 一旦不一致，说明至少有一边的规则语义理解错了，必须人工定位。

用法：
    python3 validate.py              # 跑 SQL 版并与 Python 版比对
    python3 validate.py --skip-sql   # 只比对已有的两个产物文件

退出码：0 = 完全一致；1 = 存在差异（可用于 CI）
"""

from __future__ import annotations

import argparse
import csv
import os
import shutil
import subprocess
import sys
from collections import Counter

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(BASE_DIR, "data")
PY_CSV = os.path.join(DATA, "discrepancies.csv")
SQL_CSV = os.path.join(DATA, "discrepancies_sql.csv")
SQL_FILE = os.path.join(BASE_DIR, "sql", "reconcile_duckdb.sql")

KEY = ("shipment_id", "rule")


def load(path: str) -> list[dict]:
    with open(path, newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def run_sql() -> None:
    """用 duckdb CLI 执行 SQL 版（无需任何语言依赖）。"""
    if not shutil.which("duckdb"):
        raise SystemExit(
            "未找到 duckdb CLI。\n"
            "  安装：brew install duckdb   或   https://duckdb.org/docs/installation\n"
            "  或改用：python3 validate.py --skip-sql（只比对已有产物）"
        )
    print(f"▶ 执行 DuckDB SQL：{os.path.relpath(SQL_FILE, BASE_DIR)}")
    with open(SQL_FILE, encoding="utf-8") as fh:
        result = subprocess.run(["duckdb"], stdin=fh, cwd=BASE_DIR,
                                capture_output=True, text=True)
    if result.returncode != 0:
        print(result.stdout[-2000:])
        print(result.stderr[-2000:], file=sys.stderr)
        raise SystemExit(f"SQL 执行失败（退出码 {result.returncode}）")
    print("  ✅ SQL 执行完成")


def main() -> int:
    ap = argparse.ArgumentParser(description="双实现交叉验证")
    ap.add_argument("--skip-sql", action="store_true", help="不重跑 SQL，只比对已有产物")
    args = ap.parse_args()

    if not os.path.exists(PY_CSV):
        raise SystemExit(f"缺少 Python 版产物：{PY_CSV}\n请先执行 python3 run_all.py")

    if not args.skip_sql:
        run_sql()
    if not os.path.exists(SQL_CSV):
        raise SystemExit(f"缺少 SQL 版产物：{SQL_CSV}")

    py, sq = load(PY_CSV), load(SQL_CSV)
    # 便于按 (shipment_id, rule) 取行做逐字段比对
    py_map = {tuple(r[k] for k in KEY): r for r in py}
    sq_map = {tuple(r[k] for k in KEY): r for r in sq}

    print("\n" + "=" * 72)
    print("Python 版 vs DuckDB SQL 版 —— 逐条比对")
    print("=" * 72)
    print(f"发现数：Python {len(py):,}   SQL {len(sq):,}")

    ok = True

    # ① 按规则计数
    c_py, c_sq = Counter(r["rule"] for r in py), Counter(r["rule"] for r in sq)
    print(f"\n{'规则':<26}{'Python':>10}{'SQL':>10}{'':>6}")
    for k in sorted(set(c_py) | set(c_sq)):
        same = c_py[k] == c_sq[k]
        ok &= same
        print(f"{k:<26}{c_py[k]:>10,}{c_sq[k]:>10,}{'  ✅' if same else '  ❌'}")

    # ② 集合比对（key = shipment_id + rule）
    s_py = {tuple(r[k] for k in KEY) for r in py}
    s_sq = {tuple(r[k] for k in KEY) for r in sq}
    only_py, only_sq = s_py - s_sq, s_sq - s_py
    print(f"\n集合比对（key = {' + '.join(KEY)}）：")
    print(f"  一致条目：{len(s_py & s_sq):,}   仅 Python：{len(only_py):,}   仅 SQL：{len(only_sq):,}")
    if only_py:
        ok = False
        print(f"    Python 独有示例：{sorted(only_py)[:3]}")
    if only_sq:
        ok = False
        print(f"    SQL 独有示例   ：{sorted(only_sq)[:3]}")

    # ③ 全字段逐字节比对（含 detail 文本）
    cols = [c for c in py[0].keys() if c != "finding_id"]
    num_cols = {"billed_amount", "expected_amount", "diff_amount",
                "billed_weight_kg", "expected_weight_kg"}
    field_diffs: list[tuple] = []
    for k in s_py & s_sq:
        a, b = py_map[k], sq_map[k]
        for c in cols:
            va, vb = a.get(c, ""), b.get(c, "")
            if c in num_cols:
                try:
                    if abs(float(va or 0) - float(vb or 0)) > 1e-9:
                        field_diffs.append((k, c, va, vb))
                except ValueError:
                    if va != vb:
                        field_diffs.append((k, c, va, vb))
            elif va != vb:
                field_diffs.append((k, c, va, vb))

    print(f"\n全字段比对：{len(s_py & s_sq):,} 条 × {len(cols)} 字段 "
          f"= {len(s_py & s_sq) * len(cols):,} 项")
    print(f"  不一致字段：{len(field_diffs)} 项")
    for k, c, va, vb in field_diffs[:5]:
        print(f"    {k} .{c}\n      Python: {va}\n      SQL   : {vb}")
    if field_diffs:
        ok = False

    # ④ 金额比对
    t_py = sum(float(r["diff_amount"]) for r in py)
    t_sq = sum(float(r["diff_amount"]) for r in sq)
    print(f"\n净影响合计：Python {t_py:,.2f}   SQL {t_sq:,.2f}   差 {abs(t_py - t_sq):.2f}")

    # ⑤ 严重度分布
    sev_py, sev_sq = Counter(r["severity"] for r in py), Counter(r["severity"] for r in sq)
    print(f"\n严重度分布：")
    for s in ("high", "medium", "low"):
        same = sev_py[s] == sev_sq[s]
        ok &= same
        print(f"  {s:<8} Python {sev_py[s]:>6,}   SQL {sev_sq[s]:>6,}   {'✅' if same else '❌'}")

    print("\n" + "=" * 72)
    if ok:
        print("✅ 两套实现输出逐字段完全一致 —— 规则语义、舍入口径、金额计算三者均已对齐")
    else:
        print("❌ 两套实现结果不一致 —— 需人工定位规则语义差异")
    print("=" * 72)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
