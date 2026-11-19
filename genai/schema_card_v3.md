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

## ref_debit_interchange_2024 (Federal Reserve Regulation II, U.S. debit, 2024; 20 rows; one row per network or network group)
- network VARCHAR. Individual networks: 'Discover','MasterCard','Visa' (dual-message); 'ACCEL','AFFN','ATH','Culiance','Interlink','Jeanie','Maestro','NYCE','PULSE','SHAZAM','STAR','UnionPay' (single-message). Group rows: 'Dual-message','Single-message','All networks','Visa dual-message','Visa single-message'
- network_type VARCHAR, exactly one of: 'dual' (individual dual-message network), 'single' (individual single-message network), 'dual_total' (row 'Dual-message'), 'single_total' (row 'Single-message'), 'all' (row 'All networks'), 'dual_sub', 'single_sub' (Visa split rows)
- exempt_pct_txn, exempt_pct_value DOUBLE: exempt share of the network's transactions / value, percent 0-100
- exempt_avg_txn_value, exempt_avg_fee DOUBLE (USD), exempt_fee_pct DOUBLE (fee as % of ticket)
- covered_pct_txn, covered_pct_value, covered_avg_txn_value, covered_avg_fee, covered_fee_pct: same, for covered transactions
- all_avg_txn_value, all_avg_fee, all_fee_pct: same, for all transactions
- There are no columns named avg_fee, fee_pct, pct_txn or avg_txn_value without a prefix.

## KPI glossary
- GMV = sum(total_paid). AOV = avg(total_paid).
- Late delivery rate = avg(is_late) over delivered orders (order_status = 'delivered' and delivered_at not null).
- Failed order rate = avg(is_failed) over all orders.
- Approval latency = median(approval_hours).
- Installment order = credit_card order with max_installments > 1.
- Percentages are expressed 0-100 and rounded to 2 decimals unless asked otherwise.
