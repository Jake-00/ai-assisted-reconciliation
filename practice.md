## SQL
```sql
/* R3 · 计费重量偏差 */
with tmp as (
select priced.shipment_id, row_number() over(partition by invoice.shipment_id order by invoice_line_id) as row_num, ABS(invoice.billed_weight_kg - priced.chargeable_weight_kg) as diff_weight, (invoice.billed_weight_kg - priced.chargeable_weight_kg) / priced.chargeable_weight_kg as diff_rate 
from priced left join invoice on priced.shipment_id = invoice.shipment_id
where invoice.shipment_id is not null and priced.chargeable_weight_kg > 0
)
select shipment_id
from tmp
where row_num = 1 and diff_weight > 0.05 and diff_rate > 0.05
```

# R4 · 费率不符

**业务含义**：账单金额和**按费率表算出来的应付金额**对不上。分两种情形：

1. **分区用错**（最常见）：账单用了更高的分区计费
   → 直接比 `invoice.billed_zone` 和 `priced.dest_zone`
2. **费用项不符**：分区对，但某一项（基础费 / 续重费 / 燃油附加 / 偏远附加）不对
   → 逐项和 `priced` 里的对应列比，容差 1%

**输出要求**：至少包含 `shipment_id`
```sql
/* R4 · 费率不符 */
with tmp as (
    select 
        priced.shipment_id, row_number() over(partition by invoice.shipment_id order by invoice_line_id) as row_num, ABS(invoice.billed_weight_kg - priced.chargeable_weight_kg) as diff_weight, (invoice.billed_weight_kg - priced.chargeable_weight_kg) / priced.chargeable_weight_kg as diff_rate,
        invoice.billed_zone, invoice.base_amount, invoice.weight_amount, invoice.fuel_surcharge, invoice.remote_surcharge
    from priced left join invoice on priced.shipment_id = invoice.shipment_id
    where invoice.shipment_id is not null and priced.chargeable_weight_kg > 0
), invoice_not_r3 as (
    select *
    from tmp
    where row_num = 1 and not (diff_weight > 0.05 and diff_rate > 0.05)
)
-- 分区不对
select p.shipment_id
from priced as p inner join invoice_not_r3 as i on p.shipment_id = i.shipment_id
where p.dest_zone != i.billed_zone
UNION
-- 分区对但是加和不对
select p.shipment_id
from priced as p inner join invoice_not_r3 as i on p.shipment_id = i.shipment_id
where p.dest_zone = i.billed_zone and (
-- 基础费
(p.base_rate > 0 and ABS( i.base_amount - p.base_rate ) / p.base_rate > 0.01)
OR
-- 续重费
(p.weight_amount > 0 and ABS( i.weight_amount - p.weight_amount ) / p.weight_amount > 0.01)
OR
-- 燃油附加
(p.fuel_amount > 0 and ABS( i.fuel_surcharge - p.fuel_amount ) / p.fuel_amount > 0.01)
OR
-- 偏远附加
(p.remote_surcharge > 0 and ABS( i.remote_surcharge - p.remote_surcharge ) / p.remote_surcharge > 0.01)
)
```

---

# R5 · 金额加总错误

**业务含义**：账单行的**总额 ≠ 各项费用之和**。各项都对，就是加总错了 —— 账单生成逻辑的缺陷。

**输出要求**：至少包含 `shipment_id`
```sql
with tmp as (
    select 
        priced.shipment_id, row_number() over(partition by invoice.shipment_id order by invoice_line_id) as row_num, ABS(invoice.billed_weight_kg - priced.chargeable_weight_kg) as diff_weight, (invoice.billed_weight_kg - priced.chargeable_weight_kg) / priced.chargeable_weight_kg as diff_rate,
        invoice.billed_zone, invoice.base_amount, invoice.weight_amount, invoice.fuel_surcharge, invoice.remote_surcharge,
        invoice.total_amount
    from priced left join invoice on priced.shipment_id = invoice.shipment_id
    where invoice.shipment_id is not null and priced.chargeable_weight_kg > 0
), invoice_not_r3 as (
    select *
    from tmp
    where row_num = 1 and not (diff_weight > 0.05 and diff_rate > 0.05)
)
select p.shipment_id
from priced as p inner join invoice_not_r3 as i on p.shipment_id = i.shipment_id
where p.dest_zone = i.billed_zone and not (
-- 基础费
(p.base_rate > 0 and ABS( i.base_amount - p.base_rate ) / p.base_rate > 0.01)
OR
-- 续重费
(p.weight_amount > 0 and ABS( i.weight_amount - p.weight_amount ) / p.weight_amount > 0.01)
OR
-- 燃油附加
(p.fuel_amount > 0 and ABS( i.fuel_surcharge - p.fuel_amount ) / p.fuel_amount > 0.01)
OR
-- 偏远附加
(p.remote_surcharge > 0 and ABS( i.remote_surcharge - p.remote_surcharge ) / p.remote_surcharge > 0.01)
)
-- R4 加总不对
and ABS(p.expected_total - i.total_amount ) > 0.05
```