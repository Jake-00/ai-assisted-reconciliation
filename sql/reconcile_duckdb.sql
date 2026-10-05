-- ============================================================================
-- 承运商账单核对 · DuckDB 版（5 条校验规则）
-- ============================================================================
-- 特点：
--   · 直接 read_csv_auto 读 CSV，**无需任何加载/建表步骤**
--   · 用 CTE + CASE 优先级实现与 Python 版（src/reconcile.py）**完全一致的规则语义**
--   · 无需第三方依赖 —— 只用 `duckdb` CLI
--
-- 用法：
--   cd ai-assisted-reconciliation
--   duckdb -c ".read sql/reconcile_duckdb.sql"
--   或：  duckdb < sql/reconcile_duckdb.sql
--
-- 产出：
--   data/discrepancies_sql.csv     差异清单（字段与 Python 版一致，可逐条比对）
--
-- 迁移说明：本文件 95% 语法可直接用于 BigQuery / Snowflake，
--           仅需把 read_csv_auto(...) 换成对应的表名。
-- ============================================================================

-- ---------------------------------------------------------------------------
-- 0 · 参数（阈值可按承运商合同调整）
-- ---------------------------------------------------------------------------
CREATE OR REPLACE TEMP TABLE params AS
SELECT
    0.05::DOUBLE AS weight_deviation_pct,   -- 计费重量相对偏差阈值
    0.05::DOUBLE AS weight_min_delta_kg,    -- 计费重量绝对差下限（抗称重噪声）
    0.01::DOUBLE AS amount_tolerance,       -- 各项费用容差
    0.05::DOUBLE AS total_tolerance;        -- 总额与明细加总容差

-- ---------------------------------------------------------------------------
-- 1 · 读取三张源表（DuckDB 直接读 CSV，自动推断类型）
-- ---------------------------------------------------------------------------
CREATE OR REPLACE TEMP TABLE internal AS
SELECT * FROM read_csv_auto('data/internal_records.csv');

CREATE OR REPLACE TEMP TABLE invoice AS
SELECT * FROM read_csv_auto('data/carrier_invoice.csv');

CREATE OR REPLACE TEMP TABLE rate AS
SELECT * FROM read_csv_auto('data/rate_card.csv');

-- ---------------------------------------------------------------------------
-- 2 · 按费率表重算「应付金额」—— 与生成账单时同一套公式
--     非等值 join：按 (分区, 服务等级, 计费重量) 唯一命中一个重量档
--
--     ⚠️ 金额必须用 DECIMAL 计算，**不能用 DOUBLE**。
--        实测：同一份 2 万行数据，DOUBLE 路径与 DECIMAL 路径有 **1,198 行（6%）结果不同** ——
--        因为二进制浮点无法精确表示十进制小数，在 .5 舍入边界上会翻车
--        （例：真实值 30.051945 存成 30.051944999999996，round 后变成 30.0519 而非 30.0520）。
--        这与 src/rating.py 的 Decimal 实现保持一致。
--
--     ⚠️ 舍入口径也与 src/rating.py 对齐：**逐项舍入**（基础费/续重费/燃油费各自取整），
--        而非最后一次性舍入 —— 真实承运商账单就是逐项列示的。
-- ---------------------------------------------------------------------------
CREATE OR REPLACE TEMP TABLE priced AS
WITH raw AS (
    SELECT
        i.shipment_id,
        i.ship_date,
        i.dest_zone,
        i.service_level,
        i.chargeable_weight_kg,
        CAST(r.base_rate          AS DECIMAL(18,4)) AS base_rate,
        CAST(r.per_kg_rate        AS DECIMAL(18,4)) AS per_kg_rate,
        CAST(r.fuel_surcharge_pct AS DECIMAL(18,4)) AS fuel_surcharge_pct,
        CAST(r.remote_surcharge   AS DECIMAL(18,4)) AS remote_surcharge,
        -- ① 续重费先舍入
        ROUND(CAST(r.per_kg_rate AS DECIMAL(18,4))
              * CAST(i.chargeable_weight_kg AS DECIMAL(18,3)), 4) AS weight_amount
    FROM internal AS i
    LEFT JOIN rate AS r
        ON  r.zone = i.dest_zone
        AND r.service_level = i.service_level
        AND i.chargeable_weight_kg >= r.weight_min_kg
        AND (r.weight_max_kg IS NULL OR i.chargeable_weight_kg < r.weight_max_kg)
),
stepped AS (
    -- ② 小计 = 基础费 + 已舍入的续重费，再舍入
    SELECT *, ROUND(base_rate + weight_amount, 4) AS subtotal_before
    FROM raw
),
fueled AS (
    -- ③ 燃油费 = 已舍入小计 × 费率，再舍入
    SELECT *, ROUND(subtotal_before * fuel_surcharge_pct, 4) AS fuel_amount
    FROM stepped
)
SELECT
    shipment_id, ship_date, dest_zone, service_level, chargeable_weight_kg,
    base_rate, per_kg_rate, fuel_surcharge_pct, remote_surcharge,
    weight_amount, subtotal_before, fuel_amount,
    -- ④ 应付合计 = 小计 + 燃油费 + 偏远附加，最后舍入
    ROUND(subtotal_before + fuel_amount + remote_surcharge, 4) AS expected_total
FROM fueled;

-- ---------------------------------------------------------------------------
-- 3 · 账单侧预处理：每个运单的账单行数 + 行内明细之和
-- ---------------------------------------------------------------------------
CREATE OR REPLACE TEMP TABLE invoice_stat AS
SELECT
    shipment_id,
    COUNT(*) AS line_count
FROM invoice
GROUP BY shipment_id;

-- ---------------------------------------------------------------------------
-- R1 · 未入账：内部有发货记录，账单中查无此运单
-- ---------------------------------------------------------------------------
CREATE OR REPLACE TEMP TABLE f_r1 AS
SELECT
    'R1_MISSING_FROM_INVOICE'                       AS rule,
    '未入账：内部有记录、账单无此行'                    AS rule_name,
    p.shipment_id,
    NULL                                            AS invoice_line_id,
    NULL::BIGINT                                    AS billed_zone,
    p.dest_zone                                     AS expected_zone,
    NULL::DOUBLE                                    AS billed_weight_kg,
    p.chargeable_weight_kg                          AS expected_weight_kg,
    0.0                                             AS billed_amount,
    p.expected_total                                AS expected_amount,
    ROUND(0.0 - p.expected_total, 4)                AS diff_amount,
    '内部记录 ' || p.ship_date || ' 发货，账单中无对应行' AS detail
FROM priced AS p
LEFT JOIN invoice_stat AS s ON s.shipment_id = p.shipment_id
WHERE s.shipment_id IS NULL;

-- ---------------------------------------------------------------------------
-- R2 · 重复计费：同一运单出现多次；按 invoice_line_id 排序，除首行外均视为多收
-- ---------------------------------------------------------------------------
CREATE OR REPLACE TEMP TABLE f_r2 AS
SELECT
    'R2_DUPLICATE_BILLING'                          AS rule,
    '重复计费：同一运单多次出现'                       AS rule_name,
    i.shipment_id,
    i.invoice_line_id,
    i.billed_zone,
    p.dest_zone                                     AS expected_zone,
    i.billed_weight_kg,
    p.chargeable_weight_kg                          AS expected_weight_kg,
    i.total_amount                                  AS billed_amount,
    0.0                                             AS expected_amount,
    ROUND(i.total_amount, 4)                        AS diff_amount,
    '该运单在账单中出现 ' || s.line_count || ' 次（账单批次 ' || i.invoice_batch || '），本行为多收' AS detail
FROM invoice AS i
JOIN invoice_stat AS s ON s.shipment_id = i.shipment_id AND s.line_count > 1
LEFT JOIN priced AS p ON p.shipment_id = i.shipment_id
-- 只保留每个运单的首行之外的账单行
QUALIFY ROW_NUMBER() OVER (PARTITION BY i.shipment_id ORDER BY i.invoice_line_id) > 1;

-- ---------------------------------------------------------------------------
-- R3 / R4 / R5 · 逐行核对，按根因优先级只取首个命中
--   优先级：R3 计费重量偏差 → R4 费率不符 → R5 金额加总错误
--   与 Python 版的 `continue` 语义完全等价（CASE 只返回第一个成立的分支）
-- ---------------------------------------------------------------------------
CREATE OR REPLACE TEMP TABLE f_r345 AS
SELECT
    rule,
    CASE rule
        WHEN 'R3_WEIGHT_DEVIATION' THEN '计费重量偏差超阈值'
        WHEN 'R4_RATE_MISMATCH'    THEN '费率不符：分区或费用与费率表不一致'
        WHEN 'R5_AMOUNT_MISMATCH'  THEN '金额加总错误：总额与各项费用之和不符'
    END AS rule_name,
    shipment_id, invoice_line_id,
    billed_zone, expected_zone, billed_weight_kg, expected_weight_kg,
    billed_amount,
    -- 应付口径随规则不同：R5 是「行内明细之和」，R3/R4 是「费率表算出的应付」
    CASE WHEN rule = 'R5_AMOUNT_MISMATCH' THEN components ELSE expected_total END AS expected_amount,
    ROUND(billed_amount
          - CASE WHEN rule = 'R5_AMOUNT_MISMATCH' THEN components ELSE expected_total END, 4) AS diff_amount,
    detail
FROM (
    SELECT
        CASE
            -- R3：相对偏差超阈值 且 绝对差超下限（抗称重噪声）
            WHEN p.chargeable_weight_kg > 0
             AND ABS(i.billed_weight_kg - p.chargeable_weight_kg) / p.chargeable_weight_kg
                 > (SELECT weight_deviation_pct FROM params)
             AND ABS(i.billed_weight_kg - p.chargeable_weight_kg)
                 > (SELECT weight_min_delta_kg FROM params)
                THEN 'R3_WEIGHT_DEVIATION'
            -- R4：分区不一致
            WHEN i.billed_zone <> p.dest_zone
                THEN 'R4_RATE_MISMATCH'
            -- R4：分区一致但某一费用项与费率表不符（逐项检查，与 Python 版顺序一致）
            WHEN p.base_rate > 0
             AND ABS(i.base_amount - p.base_rate) / p.base_rate
                 > (SELECT amount_tolerance FROM params)
                THEN 'R4_RATE_MISMATCH'
            WHEN p.weight_amount > 0
             AND ABS(i.weight_amount - p.weight_amount) / p.weight_amount
                 > (SELECT amount_tolerance FROM params)
                THEN 'R4_RATE_MISMATCH'
            WHEN p.fuel_amount > 0
             AND ABS(i.fuel_surcharge - p.fuel_amount) / p.fuel_amount
                 > (SELECT amount_tolerance FROM params)
                THEN 'R4_RATE_MISMATCH'
            WHEN p.remote_surcharge > 0
             AND ABS(i.remote_surcharge - p.remote_surcharge) / p.remote_surcharge
                 > (SELECT amount_tolerance FROM params)
                THEN 'R4_RATE_MISMATCH'
            -- R5：总额 ≠ 各项费用之和
            WHEN ABS(i.total_amount
                     - (i.base_amount + i.weight_amount + i.fuel_surcharge + i.remote_surcharge))
                 > (SELECT total_tolerance FROM params)
                THEN 'R5_AMOUNT_MISMATCH'
            ELSE NULL
        END AS rule,

        i.shipment_id,
        i.invoice_line_id,
        i.billed_zone,
        p.dest_zone                                     AS expected_zone,
        i.billed_weight_kg,
        p.chargeable_weight_kg                          AS expected_weight_kg,
        i.total_amount                                  AS billed_amount,
        p.expected_total                                AS expected_total,
        (i.base_amount + i.weight_amount + i.fuel_surcharge + i.remote_surcharge) AS components,

        -- detail 文本与 Python 版逐字对齐（含 printf 补零格式），便于交叉验证逐字节比对
        CASE
            WHEN p.chargeable_weight_kg > 0
             AND ABS(i.billed_weight_kg - p.chargeable_weight_kg) / p.chargeable_weight_kg
                 > (SELECT weight_deviation_pct FROM params)
             AND ABS(i.billed_weight_kg - p.chargeable_weight_kg)
                 > (SELECT weight_min_delta_kg FROM params)
                THEN '账单计费重量 ' || printf('%.3f', i.billed_weight_kg)
                     || 'kg vs 应付 ' || printf('%.3f', p.chargeable_weight_kg)
                     -- ⚠️ 偏差百分比要用 DECIMAL 算，且**除法后必须再 CAST 回 DECIMAL**。
                     --    因为 DuckDB 的 `DECIMAL / DECIMAL` 会**返回 DOUBLE**！
                     --    反例：0.351000 / 2.000000 → 0.17549999999999999（DOUBLE）
                     --          × 100 → 17.549999999999997 → ROUND(,1) → 17.5（错）
                     --          正确为十进制 17.55 → 四舍五入 → 17.6
                     --    这个差异是在**真实 Olist 数据**上跑双实现交叉验证时发现的：
                     --    合成数据恰好没落到 .5 边界，所以一直没暴露。
                     || 'kg，偏差 ' || printf('%.1f', ROUND(
                            100 * CAST(ABS(CAST(i.billed_weight_kg AS DECIMAL(18,6))
                                            - CAST(p.chargeable_weight_kg AS DECIMAL(18,6)))
                                     / CAST(p.chargeable_weight_kg AS DECIMAL(18,6))
                                     AS DECIMAL(18,8)), 1)) || '%'
            WHEN i.billed_zone <> p.dest_zone
                THEN '账单分区 ' || i.billed_zone || ' vs 应付分区 ' || p.dest_zone
                     || '，按更高分区计费'
            WHEN p.base_rate > 0
             AND ABS(i.base_amount - p.base_rate) / p.base_rate
                 > (SELECT amount_tolerance FROM params)
                THEN '基础费 与费率表不符：账单 ' || printf('%.4f', i.base_amount)
                     || ' vs 应付 ' || printf('%.4f', p.base_rate)
            WHEN p.weight_amount > 0
             AND ABS(i.weight_amount - p.weight_amount) / p.weight_amount
                 > (SELECT amount_tolerance FROM params)
                THEN '续重费 与费率表不符：账单 ' || printf('%.4f', i.weight_amount)
                     || ' vs 应付 ' || printf('%.4f', p.weight_amount)
            WHEN p.fuel_amount > 0
             AND ABS(i.fuel_surcharge - p.fuel_amount) / p.fuel_amount
                 > (SELECT amount_tolerance FROM params)
                THEN '燃油附加 与费率表不符：账单 ' || printf('%.4f', i.fuel_surcharge)
                     || ' vs 应付 ' || printf('%.4f', p.fuel_amount)
            WHEN p.remote_surcharge > 0
             AND ABS(i.remote_surcharge - p.remote_surcharge) / p.remote_surcharge
                 > (SELECT amount_tolerance FROM params)
                THEN '偏远附加 与费率表不符：账单 ' || printf('%.4f', i.remote_surcharge)
                     || ' vs 应付 ' || printf('%.4f', p.remote_surcharge)
            WHEN ABS(i.total_amount - (i.base_amount + i.weight_amount
                                       + i.fuel_surcharge + i.remote_surcharge))
                 > (SELECT total_tolerance FROM params)
                THEN '总额 ' || printf('%.4f', i.total_amount) || ' ≠ 各项之和 '
                     || printf('%.4f', i.base_amount + i.weight_amount
                              + i.fuel_surcharge + i.remote_surcharge) || '，差 '
                     || printf('%+.4f', i.total_amount - (i.base_amount + i.weight_amount
                              + i.fuel_surcharge + i.remote_surcharge))
            ELSE NULL
        END AS detail
    FROM invoice AS i
    JOIN invoice_stat AS s ON s.shipment_id = i.shipment_id
    JOIN priced AS p       ON p.shipment_id = i.shipment_id
    WHERE s.line_count = 1          -- 重复计费的运单已由 R2 处理，此处排除避免重复计算
) AS t
WHERE rule IS NOT NULL;

-- ---------------------------------------------------------------------------
-- 4 · 合并差异清单，计算严重度与编号，导出 CSV
-- ---------------------------------------------------------------------------
CREATE OR REPLACE TEMP TABLE findings AS
SELECT
    'F' || LPAD(CAST(ROW_NUMBER() OVER (
        ORDER BY rule, ABS(diff_amount) DESC, shipment_id) AS VARCHAR), 6, '0') AS finding_id,
    shipment_id, rule, rule_name,
    CASE
        -- 严重度按「分」分档：1e-5 级别的差异是浮点噪声，不是真实金额差。
        --
        -- ⚠️ 必须**先 CAST 成 DECIMAL 再 ROUND**，不能直接 ROUND(diff_amount, 2)：
        --    DuckDB 的 ROUND(x::DOUBLE, n) 在「二进制值」上舍入，
        --    而 PostgreSQL 的 round(x::numeric, n) 在「十进制」上舍入 —— 两者在 .5 边界不同：
        --        1.005 → DuckDB ROUND(double,2) = 1.0   |  PG round(numeric,2) = 1.01
        --        0.995 → DuckDB ROUND(double,2) = 1.0   |  PG round(numeric,2) = 1.00
        --    CAST 成 DECIMAL 后两者完全一致，也与 Python 的 money.round_cents 一致。
        --    （另外 PostgreSQL 根本不存在 round(double precision, integer)，不 CAST 直接报错）
        --    详见 README「问题 5」。
        WHEN ABS(ROUND(CAST(diff_amount AS DECIMAL(18,4)), 2)) >= 5.0 THEN 'high'
        WHEN ABS(ROUND(CAST(diff_amount AS DECIMAL(18,4)), 2)) >= 1.0 THEN 'medium'
        ELSE 'low'
    END AS severity,
    invoice_line_id, billed_zone, expected_zone,
    billed_weight_kg, expected_weight_kg,
    billed_amount, expected_amount, diff_amount, detail
FROM (
    SELECT rule, rule_name, shipment_id, invoice_line_id, billed_zone, expected_zone,
           billed_weight_kg, expected_weight_kg, billed_amount, expected_amount,
           diff_amount, detail FROM f_r1
    UNION ALL
    SELECT rule, rule_name, shipment_id, invoice_line_id, billed_zone, expected_zone,
           billed_weight_kg, expected_weight_kg, billed_amount, expected_amount,
           diff_amount, detail FROM f_r2
    UNION ALL
    SELECT rule, rule_name, shipment_id, invoice_line_id, billed_zone, expected_zone,
           billed_weight_kg, expected_weight_kg, billed_amount, expected_amount,
           diff_amount, detail FROM f_r345
);

COPY (SELECT * FROM findings) TO 'data/discrepancies_sql.csv' (HEADER, DELIMITER ',');

-- ---------------------------------------------------------------------------
-- 5 · 汇总报告
-- ---------------------------------------------------------------------------
SELECT '── 差异汇总（按规则）─────────────────────' AS report;

SELECT
    rule,
    COUNT(*)                                                  AS finding_count,
    ROUND(SUM(CASE WHEN diff_amount > 0 THEN diff_amount ELSE 0 END), 2) AS overbilled_to_recover,
    ROUND(SUM(diff_amount), 2)                                AS net_impact,
    SUM(CASE WHEN severity = 'high'   THEN 1 ELSE 0 END)      AS high,
    SUM(CASE WHEN severity = 'medium' THEN 1 ELSE 0 END)      AS medium,
    SUM(CASE WHEN severity = 'low'    THEN 1 ELSE 0 END)      AS low
FROM findings
GROUP BY rule
ORDER BY rule;

SELECT '── 金额影响总计 ─────────────────────────' AS report;

SELECT
    COUNT(*)                                                                   AS total_findings,
    ROUND(SUM(CASE WHEN diff_amount > 0 THEN diff_amount ELSE 0 END), 2)       AS overbilled_to_recover,
    ROUND(SUM(CASE WHEN rule = 'R1_MISSING_FROM_INVOICE'
                   THEN -diff_amount ELSE 0 END), 2)                           AS unbilled_to_confirm,
    ROUND(SUM(diff_amount), 2)                                                 AS net_impact
FROM findings;
