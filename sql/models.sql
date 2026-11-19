-- Payments analytics models (DuckDB dialect; Snowflake port in sql/snowflake_ddl.sql)

create table dim_seller as
select seller_id,
       seller_zip_code_prefix as seller_zip_prefix,
       seller_city,
       seller_state
from raw_sellers;

create table fact_payment as
select order_id,
       payment_sequential,
       payment_type,
       payment_installments,
       payment_value
from raw_order_payments;

-- order-level payment summary
create temp table pay_order as
select order_id,
       count(*)                                         as n_payment_records,
       count(distinct payment_type)                     as n_payment_methods,
       sum(payment_value)                               as total_paid,
       max(payment_installments)                        as max_installments,
       arg_max(payment_type, payment_value)             as primary_payment_type,
       bool_or(payment_type = 'voucher')                as used_voucher
from raw_order_payments
group by 1;

-- order-level item summary; primary seller = seller of the highest-priced line
create temp table item_order as
select i.order_id,
       count(*)                                         as n_items,
       count(distinct i.seller_id)                      as n_sellers,
       sum(i.price)                                     as items_price,
       sum(i.freight_value)                             as items_freight,
       sum(i.price) + sum(i.freight_value)              as items_amount,
       arg_max(i.seller_id, i.price)                    as seller_id,
       arg_max(coalesce(t.product_category_name_english, p.product_category_name), i.price) as category
from raw_order_items i
left join raw_products p using (product_id)
left join raw_category_translation t using (product_category_name)
group by 1;

create temp table review_order as
select order_id, avg(review_score) as review_score
from raw_order_reviews
group by 1;

create table fact_order as
select o.order_id,
       o.order_status,
       o.order_purchase_timestamp                       as purchased_at,
       o.order_approved_at                              as approved_at,
       date_trunc('month', o.order_purchase_timestamp)  as purchase_month,
       epoch(o.order_approved_at - o.order_purchase_timestamp) / 3600.0 as approval_hours,
       o.order_delivered_customer_date                  as delivered_at,
       o.order_estimated_delivery_date                  as estimated_delivery_at,
       epoch(o.order_delivered_customer_date - o.order_purchase_timestamp) / 86400.0 as delivery_days,
       case when o.order_delivered_customer_date is null then null
            else (o.order_delivered_customer_date::date > o.order_estimated_delivery_date::date) end as is_late,
       (o.order_status in ('canceled', 'unavailable'))   as is_failed,
       c.customer_state,
       i.seller_id, i.n_sellers, i.n_items, i.category,
       i.items_price, i.items_freight, i.items_amount,
       p.primary_payment_type, p.n_payment_records, p.n_payment_methods,
       p.total_paid, p.max_installments, p.used_voucher,
       p.total_paid - i.items_amount                    as recon_diff,
       r.review_score
from raw_orders o
left join raw_customers c using (customer_id)
left join item_order i using (order_id)
left join pay_order p using (order_id)
left join review_order r using (order_id);
