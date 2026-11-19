# Payments analytics warehouse: schema card (DuckDB SQL)

## fact_order (one row per order; 99,441 rows; Olist marketplace, Brazil, 2016-09 to 2018-10, BRL)
- order_id VARCHAR (PK)
- order_status VARCHAR: delivered, shipped, canceled, unavailable, invoiced, processing, created, approved
- purchased_at TIMESTAMP, approved_at TIMESTAMP (payment approved), purchase_month TIMESTAMP (first of month)
- approval_hours DOUBLE: hours from purchase to payment approval
- delivered_at TIMESTAMP, estimated_delivery_at TIMESTAMP, delivery_days DOUBLE (purchase to delivery)
- is_late BOOLEAN: delivered after the promised (estimated) date; NULL if not delivered
- is_failed BOOLEAN: order_status in ('canceled','unavailable')
- customer_state VARCHAR (2-letter Brazilian state)
- seller_id VARCHAR (seller of the highest-priced item), n_sellers, n_items INTEGER, category VARCHAR (English)
- items_price, items_freight, items_amount DOUBLE (price + freight)
- primary_payment_type VARCHAR: credit_card, boleto, voucher, debit_card, not_defined (method carrying the largest amount)
- n_payment_records, n_payment_methods INTEGER, total_paid DOUBLE (BRL), max_installments INTEGER, used_voucher BOOLEAN
- recon_diff DOUBLE: total_paid - items_amount
- review_score DOUBLE (1-5)

## fact_payment (one row per payment record; 103,886 rows)
- order_id, payment_sequential INTEGER, payment_type VARCHAR, payment_installments INTEGER, payment_value DOUBLE (BRL)

## dim_seller (3,095 rows)
- seller_id (PK), seller_zip_prefix, seller_city, seller_state

## ref_debit_interchange_2024 (Federal Reserve Regulation II, U.S. debit, 2024; 20 rows)
- network VARCHAR (e.g. 'Visa','MasterCard','STAR','All networks','Dual-message','Single-message')
- network_type VARCHAR: dual, single, dual_sub, single_sub, dual_total, single_total, all
- exempt_/covered_/all_ prefixes with: pct_txn, pct_value, avg_txn_value (USD), avg_fee (USD per txn), fee_pct (fee as % of ticket)

## KPI glossary
- GMV = sum(total_paid). AOV = avg(total_paid).
- Late delivery rate = avg(is_late) over delivered orders (order_status = 'delivered' and delivered_at not null).
- Failed order rate = avg(is_failed) over all orders.
- Approval latency = median(approval_hours).
- Installment order = credit_card order with max_installments > 1.
- Percentages are expressed 0-100 and rounded to 2 decimals unless asked otherwise.
