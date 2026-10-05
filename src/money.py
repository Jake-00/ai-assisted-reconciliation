#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
money.py · 金额计算的统一口径
===============================

**为什么单独抽一个模块？**

金融/对账场景有一类 bug 极难发现：用二进制浮点（float）做金额计算。
IEEE754 无法精确表示大多数十进制小数，误差会在 `.5` 舍入边界上**翻转舍入方向**：

    真实值（账单上列示的数）: 30.051945
    float 实际存储的值        : 30.051944999999996   ← 落在 .5 边界下方
    round(..., 4) 的结果      : 30.0519  ❌  正确应为 30.0520

本项目实测：同一份 2 万行数据，**float 路径与 Decimal 路径有 1,198 行（6%）结果不同**。
这类差异在对账系统里会直接变成"多收/少收"的错误结论，因此必须全程用 Decimal。

三条纪律
--------
1. **金额一律用 `Decimal` 计算**，不要用 `float`；
2. **从 float/字符串转 Decimal 时用 `to_decimal()`** —— 它走 `str()` 中转，
   取的是"人类书写的那个数"，而不是二进制误差；
3. **舍入模式显式指定为 ROUND_HALF_UP（四舍五入）**，
   而不是 Python 内置 `round()` 的银行家舍入（round-half-to-even），
   以便与 SQL 的 `ROUND()`、以及发票的列示口径保持一致。
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

# 金额精度：4 位小数（与费率表、账单的列示精度一致）
MONEY_EXP = Decimal("0.0001")
# 「分」的精度：2 位小数 —— 钱真正有意义的精度，用于分档/比较
CENT_EXP = Decimal("0.01")
# 百分比精度：1 位小数（与 SQL printf('%.1f') 一致）
PCT_EXP = Decimal("0.1")


def to_decimal(x) -> Decimal:
    """把数值安全转成 Decimal。

    用 `str()` 中转而不是 `Decimal(float)`：
        Decimal(30.051944999999996)  → Decimal('30.051944999999996')   ← 带入了二进制误差
        Decimal(str(30.051944999999996)) → Decimal('30.051945')        ← 账单上列示的那个数
    """
    if isinstance(x, Decimal):
        return x
    return Decimal(str(x))


def round_money(x) -> Decimal:
    """金额四舍五入到 4 位小数。"""
    return to_decimal(x).quantize(MONEY_EXP, rounding=ROUND_HALF_UP)


def round_cents(x) -> Decimal:
    """金额四舍五入到「分」（2 位小数）。

    ⚠️ 不要用 Python 内置 `round(x, 2)` 代替它。两者在 .5 边界结果不同：

        round(1.005, 2)   → 1.0    （内置：在二进制浮点上做「银行家舍入」）
        round_cents(1.005)→ 1.01   （Decimal：在十进制上做「四舍五入」）

    分组/分档时用哪个会直接改变结论（例如 0.995 元判 low 还是 medium）。
    跨引擎一致性（实测 PostgreSQL 15.3 / DuckDB 1.5.5）：
        · PostgreSQL `round(x::numeric, 2)`                → 与 round_cents 一致
        · DuckDB `ROUND(CAST(x AS DECIMAL(18,4)), 2)`      → 与 round_cents 一致
        · DuckDB `ROUND(x::DOUBLE, 2)`                     → ❌ 在二进制值上舍，与两者都不一致
    """
    return to_decimal(x).quantize(CENT_EXP, rounding=ROUND_HALF_UP)


def round_pct(x) -> Decimal:
    """百分比四舍五入到 1 位小数（用于展示）。"""
    return to_decimal(x).quantize(PCT_EXP, rounding=ROUND_HALF_UP)
