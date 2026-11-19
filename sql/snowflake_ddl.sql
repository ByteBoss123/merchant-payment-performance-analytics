-- Snowflake port of sql/models.sql. Not executed in this build (no writable Snowflake account
-- was connected); syntax translated by hand from the DuckDB models that were run and tested.
-- Load raw CSVs to RAW_* tables first (e.g. PUT + COPY INTO from an internal stage).

create or replace table DIM_SELLER as
select seller_id, seller_zip_code_prefix as seller_zip_prefix, seller_city, seller_state
from RAW_SELLERS;

create or replace table FACT_PAYMENT as
select order_id, payment_sequential, payment_type, payment_installments, payment_value
from RAW_ORDER_PAYMENTS;

create or replace table FACT_ORDER cluster by (purchase_month) as
with pay_order as (
  select order_id,
         count(*)                               as n_payment_records,
         count(distinct payment_type)           as n_payment_methods,
         sum(payment_value)                     as total_paid,
         max(payment_installments)              as max_installments,
         max_by(payment_type, payment_value)    as primary_payment_type,
         boolor_agg(payment_type = 'voucher')   as used_voucher
  from RAW_ORDER_PAYMENTS group by 1
), item_order as (
  select i.order_id,
         count(*)                               as n_items,
         count(distinct i.seller_id)            as n_sellers,
         sum(i.price)                           as items_price,
         sum(i.freight_value)                   as items_freight,
         sum(i.price) + sum(i.freight_value)    as items_amount,
         max_by(i.seller_id, i.price)           as seller_id,
         max_by(coalesce(t.product_category_name_english, p.product_category_name), i.price) as category
  from RAW_ORDER_ITEMS i
  left join RAW_PRODUCTS p on p.product_id = i.product_id
  left join RAW_CATEGORY_TRANSLATION t on t.product_category_name = p.product_category_name
  group by 1
), review_order as (
  select order_id, avg(review_score) as review_score from RAW_ORDER_REVIEWS group by 1
)
select o.order_id, o.order_status,
       o.order_purchase_timestamp as purchased_at,
       o.order_approved_at        as approved_at,
       date_trunc('month', o.order_purchase_timestamp) as purchase_month,
       datediff('second', o.order_purchase_timestamp, o.order_approved_at) / 3600.0 as approval_hours,
       o.order_delivered_customer_date as delivered_at,
       o.order_estimated_delivery_date as estimated_delivery_at,
       datediff('second', o.order_purchase_timestamp, o.order_delivered_customer_date) / 86400.0 as delivery_days,
       iff(o.order_delivered_customer_date is null, null,
           o.order_delivered_customer_date::date > o.order_estimated_delivery_date::date) as is_late,
       o.order_status in ('canceled', 'unavailable') as is_failed,
       c.customer_state,
       i.seller_id, i.n_sellers, i.n_items, i.category, i.items_price, i.items_freight, i.items_amount,
       p.primary_payment_type, p.n_payment_records, p.n_payment_methods, p.total_paid,
       p.max_installments, p.used_voucher,
       p.total_paid - i.items_amount as recon_diff,
       r.review_score
from RAW_ORDERS o
left join RAW_CUSTOMERS c on c.customer_id = o.customer_id
left join item_order   i on i.order_id = o.order_id
left join pay_order    p on p.order_id = o.order_id
left join review_order r on r.order_id = o.order_id;

-- Read-only role for the GenAI assistant (least privilege)
create role if not exists PAYMENTS_ASSISTANT_RO;
grant usage on warehouse ANALYTICS_WH to role PAYMENTS_ASSISTANT_RO;
grant select on table FACT_ORDER to role PAYMENTS_ASSISTANT_RO;
grant select on table FACT_PAYMENT to role PAYMENTS_ASSISTANT_RO;
grant select on table DIM_SELLER to role PAYMENTS_ASSISTANT_RO;
