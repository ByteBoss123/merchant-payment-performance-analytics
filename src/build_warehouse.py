"""Build the payments analytics warehouse (DuckDB) from the real Olist marketplace data.

Grain:
  fact_payment       one row per payment record (order_id, payment_sequential)
  fact_order         one row per order, with payment, fulfillment and review outcomes
  dim_seller         one row per seller
Also runs data-quality checks and writes results/dq_checks.json.
"""
import json
import pathlib
import sys

import duckdb

ROOT = pathlib.Path(__file__).resolve().parents[1]
RAW = ROOT / "data" / "raw"
DB = ROOT / "data" / "payments.duckdb"


def build(db_path=DB):
    if db_path.exists():
        db_path.unlink()
    con = duckdb.connect(str(db_path))
    tables = ["orders", "order_payments", "order_items", "sellers", "order_reviews", "products", "customers"]
    for t in tables:
        con.execute(f"create table raw_{t} as select * from read_csv_auto('{RAW}/olist_{t}_dataset.csv', header=true)")
    con.execute(f"create table raw_category_translation as select * from read_csv_auto('{RAW}/product_category_name_translation.csv', header=true)")
    con.execute((ROOT / "sql" / "models.sql").read_text())
    fed = ROOT / "data" / "fed" / "regii_avg_interchange_2024.csv"
    con.execute(f"create table ref_debit_interchange_2024 as select * from read_csv_auto('{fed}')")
    dq = run_dq(con)
    (ROOT / "results").mkdir(exist_ok=True)
    (ROOT / "results" / "dq_checks.json").write_text(json.dumps(dq, indent=2, default=str))
    con.close()
    return dq


def run_dq(con):
    q = lambda s: con.execute(s).fetchone()[0]
    checks = {
        "fact_payment_rows": q("select count(*) from fact_payment"),
        "fact_order_rows": q("select count(*) from fact_order"),
        "dim_seller_rows": q("select count(*) from dim_seller"),
        "dup_payment_keys": q("select count(*) - count(distinct (order_id, payment_sequential)) from fact_payment"),
        "dup_order_keys": q("select count(*) - count(distinct order_id) from fact_order"),
        "orders_without_payment": q("select count(*) from fact_order where total_paid is null"),
        "orphan_payments": q("select count(*) from fact_payment p left join fact_order o using(order_id) where o.order_id is null"),
        "orphan_order_sellers": q("select count(*) from fact_order o left join dim_seller s using(seller_id) where o.seller_id is not null and s.seller_id is null"),
        "negative_payment_values": q("select count(*) from fact_payment where payment_value < 0"),
        "zero_value_payments": q("select count(*) from fact_payment where payment_value = 0"),
        "approved_before_purchase": q("select count(*) from fact_order where approval_hours < 0"),
        # Reconciliation: what the customer paid vs. item price + freight on the order.
        "orders_reconcilable": q("select count(*) from fact_order where total_paid is not null and items_amount is not null"),
        "orders_recon_mismatch_gt_1brl": q("select count(*) from fact_order where abs(recon_diff) > 1"),
        "recon_mismatch_total_brl": q("select round(sum(abs(recon_diff)) filter (where abs(recon_diff) > 1), 2) from fact_order"),
    }
    checks["pass"] = all([
        checks["dup_payment_keys"] == 0, checks["dup_order_keys"] == 0,
        checks["orphan_payments"] == 0, checks["orphan_order_sellers"] == 0,
        checks["negative_payment_values"] == 0,
    ])
    return checks


if __name__ == "__main__":
    out = build()
    print(json.dumps(out, indent=2, default=str))
    sys.exit(0 if out["pass"] else 1)
