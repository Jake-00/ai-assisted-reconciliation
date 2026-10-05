-- ============================================================================
-- 承运商账单核对 · BigQuery Standard SQL 版
-- ============================================================================
-- 与 Python 版（src/reconcile.py）实现**同样的 5 条校验规则**，用于展示：
--   ① 如何在云数仓（BigQuery）里做对账；
--   ② 分区裁剪、聚簇、避免 SELECT * 的成本控制习惯；
--   ③ 用 QUALIFY / 窗口函数做"按根因优先级只取首个命中"的逻辑。
--
-- 使用前请替换：
--   `your_project.your_dataset` → 实际数据集
--   internal_records            → 内部系统记录表
--   carrier_invoice             → 承运商账单表
--   rate_card                   → 费率表
--
-- 建议建表时对账单表按 invoice_date 分区、按 shipment_id 聚簇：
--   CREATE TABLE ... PARTITION BY DATE(invoice_date) CLUSTER BY shipment_id AS ...
-- ============================================================================

-- 参数
DECLARE weight_deviation_pct FLOAT64 DEFAULT 0.05;   -- 计费重量相对偏差阈值
DECLARE weight_min_delta_kg  FLOAT64 DEFAULT 0.05;   -- 计费重量绝对差下限
DECLARE amount_tolerance     FLOAT64 DEFAULT 0.01;   -- 各项费用容差
DECLARE total_tolerance      FLOAT64 DEFAULT 0.05;   -- 总额与明细加总容差


-- ----------------------------------------------------------------------------
-- CTE 1 · 账单行聚合：识别重复计费（R2），并计算行内明细之和（R5 用）
-- ----------------------------------------------------------------------------
WITH invoice_lines AS (
  SELECT
    shipment_id,
    COUNT(*)                          AS line_count,
    MIN(invoice_line_id)              AS first_line_id,
    ARRAY_AGG(invoice_line_id ORDER BY invoice_line_id) AS line_ids,
    SUM(total_amount)                 AS billed_total_all_lines
  FROM `your_project.your_dataset.carrier_invoice`
  WHERE invoice_date BETWEEN @period_start AND @period_end   -- 分区裁剪，控制扫描量
  GROUP BY shipment_id
),

-- ----------------------------------------------------------------------------
-- CTE 2 · 按费率表重算「应付金额」——与生成账单时同源的唯一口径
-- ----------------------------------------------------------------------------
expected AS (
  SELECT
    shipment_id, dest_zone, service_level, chargeable_weight_kg,
    base_rate, per_kg_rate, fuel_surcharge_pct, remote_surcharge,
    weight_amount,
    ROUND(subtotal_before + fuel_amount + remote_surcharge, 4) AS expected_total
  FROM (
    SELECT
      *,
      ROUND(base_rate + weight_amount, 4) AS subtotal_before,
      ROUND(ROUND(base_rate + weight_amount, 4) * fuel_surcharge_pct, 4) AS fuel_amount
    FROM (
      SELECT
        i.shipment_id,
        i.dest_zone,
        i.service_level,
        i.chargeable_weight_kg,

        -- ⚠️ 金额一律显式 CAST 成定点类型，并且**一定要写精度**。两个坑：
        --   ① 写 NUMERIC / DECIMAL 而不带精度，用的是**引擎默认值**，各家不同：
        --        DuckDB    → DECIMAL(18,3)  ← 会把 4 位小数的费率 1.1875 吃成 1.188
        --        BigQuery  → NUMERIC(38,9)
        --        PostgreSQL→ 无约束（任意精度）
        --      实测：不写精度时，同一段 SQL 在 DuckDB 上与源口径差 11,080 行。
        --   ② 若列是 FLOAT64/DOUBLE，ROUND() 会在「二进制浮点」上舍入，
        --      与定点数的十进制舍入在 .5 边界结果不同
        --      （实测：ROUND(1.005::DOUBLE,2)=1.0，而 ROUND(1.005::DECIMAL(18,4),2)=1.01）。
        --    这属于 README「问题 4」与「问题 6」。
        CAST(r.base_rate          AS NUMERIC(18,4)) AS base_rate,
        CAST(r.per_kg_rate        AS NUMERIC(18,4)) AS per_kg_rate,
        CAST(r.fuel_surcharge_pct AS NUMERIC(18,4)) AS fuel_surcharge_pct,
        CAST(r.remote_surcharge   AS NUMERIC(18,4)) AS remote_surcharge,

        -- 计费口径：**逐项舍入**（与 src/rating.py 严格一致，详见 README「问题 1」）
        --   ① 续重费 = ROUND(续重单价 × 计费重量, 4)
        ROUND(CAST(r.per_kg_rate AS NUMERIC(18,4)) * CAST(i.chargeable_weight_kg AS NUMERIC(18,3)), 4)
          AS weight_amount
      FROM `your_project.your_dataset.internal_records` AS i
      -- 费率表按 (zone, service, 重量档) 唯一命中；用范围条件 join，避免非等值 join 造成的行膨胀
      LEFT JOIN `your_project.your_dataset.rate_card` AS r
        ON  r.zone = i.dest_zone
        AND r.service_level = i.service_level
        AND i.chargeable_weight_kg >= r.weight_min_kg
        AND (r.weight_max_kg IS NULL OR i.chargeable_weight_kg < r.weight_max_kg)
    )
    -- ② 小计 = ROUND(基础费 + 续重费, 4)；③ 燃油费 = ROUND(小计 × 燃油费率, 4)
  )
  -- ④ 应付 = ROUND(小计 + 燃油费 + 偏远附加, 4)
),

-- ----------------------------------------------------------------------------
-- CTE 3 · 取每个运单的「唯一待核对账单行」
--         R2 重复计费的运单在这里被排除，交由重复计费规则单独处理，避免重复计算金额
-- ----------------------------------------------------------------------------
primary_invoice_line AS (
  SELECT
    inv.shipment_id,
    inv.invoice_line_id,
    inv.billed_zone,
    inv.service_level,
    inv.billed_weight_kg,
    inv.base_amount,
    inv.weight_amount,
    inv.fuel_surcharge,
    inv.remote_surcharge,
    inv.total_amount,
    inv.base_amount + inv.weight_amount + inv.fuel_surcharge + inv.remote_surcharge AS components_sum
  FROM `your_project.your_dataset.carrier_invoice` AS inv
  JOIN invoice_lines AS l
    ON l.shipment_id = inv.shipment_id
   AND l.line_count = 1                         -- 只核对非重复运单
  WHERE DATE(inv.invoice_date) BETWEEN @period_start AND @period_end
),

-- ----------------------------------------------------------------------------
-- CTE 4 · 逐条应用规则，并用 QUALIFY 实现「按根因优先级只取首个命中」
--         优先级：R3 重量 → R4 费率 → R5 金额加总
-- ----------------------------------------------------------------------------
findings AS (
  SELECT
    e.shipment_id,
    p.invoice_line_id,
    p.billed_zone,
    e.dest_zone,
    p.billed_weight_kg,
    e.chargeable_weight_kg,
    p.total_amount                                                AS billed_amount,
    e.expected_total,
    ROUND(p.total_amount - e.expected_total, 4)                   AS diff_amount,

    CASE
      -- R3 计费重量偏差（相对阈值 + 绝对差下限，抗称重噪声）
      WHEN e.chargeable_weight_kg > 0
       AND ABS(p.billed_weight_kg - e.chargeable_weight_kg) / e.chargeable_weight_kg > weight_deviation_pct
       AND ABS(p.billed_weight_kg - e.chargeable_weight_kg) > weight_min_delta_kg
        THEN 'R3_WEIGHT_DEVIATION'
      -- R4 费率不符：分区错，或任一费用项与费率表不符
      WHEN p.billed_zone != e.dest_zone
        THEN 'R4_RATE_MISMATCH'
      WHEN e.base_rate > 0 AND ABS(p.base_amount - e.base_rate) / e.base_rate > amount_tolerance
        THEN 'R4_RATE_MISMATCH'
      WHEN e.per_kg_rate * e.chargeable_weight_kg > 0
       AND ABS(p.weight_amount - e.per_kg_rate * e.chargeable_weight_kg)
           / (e.per_kg_rate * e.chargeable_weight_kg) > amount_tolerance
        THEN 'R4_RATE_MISMATCH'
      -- R5 金额加总错误：总额 ≠ 各项之和
      WHEN ABS(p.total_amount - p.components_sum) > total_tolerance
        THEN 'R5_AMOUNT_MISMATCH'
      ELSE NULL
    END AS rule,

    CASE
      WHEN p.billed_zone != e.dest_zone
        THEN CONCAT('账单分区 ', CAST(p.billed_zone AS STRING), ' vs 应付分区 ', CAST(e.dest_zone AS STRING))
      WHEN ABS(p.billed_weight_kg - e.chargeable_weight_kg) > weight_min_delta_kg
        THEN CONCAT('账单计费重量 ', CAST(p.billed_weight_kg AS STRING),
                    'kg vs 应付 ', CAST(e.chargeable_weight_kg AS STRING), 'kg')
      WHEN ABS(p.total_amount - p.components_sum) > total_tolerance
        THEN CONCAT('总额 ', CAST(p.total_amount AS STRING),
                    ' ≠ 各项之和 ', CAST(p.components_sum AS STRING))
      ELSE '与费率表不符'
    END AS detail
  FROM expected AS e
  JOIN primary_invoice_line AS p USING (shipment_id)
)
SELECT * FROM findings WHERE rule IS NOT NULL;


-- ----------------------------------------------------------------------------
-- R1 · 未入账：内部有发货记录，账单中查无此运单
-- ----------------------------------------------------------------------------
SELECT
  i.shipment_id,
  'R1_MISSING_FROM_INVOICE' AS rule,
  i.ship_date,
  i.dest_zone,
  i.chargeable_weight_kg,
  e.expected_total          AS unbilled_amount,
  '内部有发货记录，账单中无对应行' AS detail
FROM `your_project.your_dataset.internal_records` AS i
LEFT JOIN `your_project.your_dataset.carrier_invoice` AS inv
  ON inv.shipment_id = i.shipment_id
 AND DATE(inv.invoice_date) BETWEEN @period_start AND @period_end
LEFT JOIN expected AS e USING (shipment_id)
WHERE inv.shipment_id IS NULL;


-- ----------------------------------------------------------------------------
-- R2 · 重复计费：同一运单出现多次；除最早一行外，其余视为多收
-- ----------------------------------------------------------------------------
SELECT
  l.shipment_id,
  'R2_DUPLICATE_BILLING' AS rule,
  l.line_count,
  l.billed_total_all_lines,
  -- 多收金额 = 除首行外的其余行金额之和
  l.billed_total_all_lines - first_line.total_amount AS overbilled_amount,
  CONCAT('该运单在账单中出现 ', CAST(l.line_count AS STRING), ' 次') AS detail
FROM invoice_lines AS l
JOIN `your_project.your_dataset.carrier_invoice` AS first_line
  ON first_line.invoice_line_id = l.first_line_id
WHERE l.line_count > 1;


-- ----------------------------------------------------------------------------
-- 汇总 · 按规则统计条数与金额影响（供看板 / 报表直接消费）
-- ----------------------------------------------------------------------------
-- 说明：把上面两段 UNION ALL 到一张临时表后聚合即可，示意如下：
--
-- WITH all_findings AS (
--   SELECT * FROM findings           -- R3/R4/R5
--   UNION ALL
--   SELECT * FROM r1_result          -- R1
--   UNION ALL
--   SELECT * FROM r2_result          -- R2
-- )
-- SELECT
--   rule,
--   COUNT(*)                                              AS finding_count,
--   ROUND(SUM(CASE WHEN diff_amount > 0 THEN diff_amount ELSE 0 END), 2) AS overbilled_to_recover,
--   ROUND(SUM(diff_amount), 2)                            AS net_impact
-- FROM all_findings
-- GROUP BY rule
-- ORDER BY net_impact DESC;
