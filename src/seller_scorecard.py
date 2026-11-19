"""Seller (merchant) payment-performance scorecard with peer benchmarks and rule-based recommendations.

Peer group = seller's dominant product category. Sellers need >= MIN_ORDERS delivered orders.
Writes results/seller_scorecard.csv and results/scorecard_summary.json.
"""
import json
import pathlib

import duckdb
import pandas as pd

ROOT = pathlib.Path(__file__).resolve().parents[1]
DB = ROOT / "data" / "payments.duckdb"
OUT = ROOT / "results"
MIN_ORDERS = 30
MIN_PEERS = 5
MIN_FLAG_EVENTS = 5  # a flag needs at least this many late / failed orders behind it

SQL = """
with o as (
  select *, (max_installments >= 4) as long_installment
  from fact_order where seller_id is not null and primary_payment_type is not null
), main_cat as (
  select seller_id, arg_max(category, cnt) as peer_category from (
    select seller_id, category, count(*) cnt from o group by 1, 2) group by 1
)
select o.seller_id, m.peer_category, s.seller_state,
       count(*)                                                        as orders,
       count(*) filter (where order_status = 'delivered')              as delivered_orders,
       round(sum(total_paid::decimal(18, 2)), 2)::double                as gmv_brl,
       round(avg(total_paid::decimal(18, 2)), 2)::double                as aov_brl,
       round(avg((primary_payment_type = 'boleto')::int) * 100, 1)     as boleto_share_pct,
       round(avg(long_installment::int) filter (where primary_payment_type = 'credit_card') * 100, 1) as card_4plus_installment_pct,
       round(median(approval_hours), 2)                                as median_approval_hours,
       round(median(approval_hours) filter (where primary_payment_type = 'boleto'), 1) as boleto_median_approval_hours,
       count(*) filter (where is_failed)                               as failed_orders,
       count(*) filter (where is_late)                                 as late_orders,
       round(avg(is_failed::int) * 100, 2)                             as failed_pct,
       round(avg(is_late::int) * 100, 2)                               as late_pct,
       round(avg(is_late::int) filter (where primary_payment_type = 'boleto') * 100, 2) as boleto_late_pct,
       count(*) filter (where primary_payment_type = 'boleto' and is_late
                        and delivered_at::date <= (estimated_delivery_at + interval 1 day)::date) as boleto_late_within_1day,
       round(avg(review_score), 3)                                     as avg_review
from o join main_cat m using (seller_id) left join dim_seller s using (seller_id)
group by 1, 2, 3
having count(*) filter (where order_status = 'delivered') >= {min_orders}
"""


def build():
    c = duckdb.connect(str(DB), read_only=True)
    df = c.execute(SQL.format(min_orders=MIN_ORDERS)).df().sort_values("seller_id").reset_index(drop=True)
    peers = df.groupby("peer_category")
    df["peer_n"] = peers["seller_id"].transform("count")
    for col in ["late_pct", "failed_pct", "card_4plus_installment_pct", "aov_brl", "avg_review"]:
        df[f"peer_median_{col}"] = peers[col].transform("median")
    df["peer_p75_late_pct"] = peers["late_pct"].transform(lambda s: s.quantile(0.75))
    df["peer_p75_failed_pct"] = peers["failed_pct"].transform(lambda s: s.quantile(0.75))
    df["peer_p25_installment_pct"] = peers["card_4plus_installment_pct"].transform(lambda s: s.quantile(0.25))
    df = df[df["peer_n"] >= MIN_PEERS].copy()

    recs = []
    for _, r in df.iterrows():
        out = []
        if r.late_pct > r.peer_p75_late_pct and r.late_orders >= MIN_FLAG_EVENTS:
            out.append(f"Late delivery {r.late_pct:.1f}% vs category median {r.peer_median_late_pct:.1f}%: review handling/carrier SLAs.")
        if r.boleto_late_within_1day >= 3:
            out.append(f"{int(r.boleto_late_within_1day)} boleto orders missed the promise by <=1 day: add boleto clearing time "
                       f"(median {r.boleto_median_approval_hours:.0f}h boleto approval for this seller) to the delivery promise.")
        if r.failed_pct > r.peer_p75_failed_pct and r.failed_orders >= MIN_FLAG_EVENTS:
            out.append(f"Canceled/unavailable {r.failed_pct:.1f}% vs category median {r.peer_median_failed_pct:.1f}%: check stock sync before payment capture.")
        if pd.notna(r.card_4plus_installment_pct) and r.card_4plus_installment_pct < r.peer_p25_installment_pct \
                and r.aov_brl >= r.peer_median_aov_brl:
            out.append(f"Only {r.card_4plus_installment_pct:.0f}% of card orders use 4+ installments vs category median "
                       f"{r.peer_median_card_4plus_installment_pct:.0f}% despite above-median ticket: test longer installment offers.")
        recs.append(" | ".join(out))
    df["recommendations"] = recs
    df["n_flags"] = df["recommendations"].str.count(r"\|") + (df["recommendations"] != "").astype(int)
    df = df.sort_values(["n_flags", "gmv_brl", "seller_id"], ascending=[False, False, True])
    OUT.mkdir(exist_ok=True)
    df.to_csv(OUT / "seller_scorecard.csv", index=False)
    summary = {
        "sellers_scored": int(len(df)),
        "peer_categories": int(df["peer_category"].nunique()),
        "min_delivered_orders": MIN_ORDERS,
        "sellers_with_any_flag": int((df["n_flags"] > 0).sum()),
        "flag_counts": {
            "late_above_peer_p75": int(df["recommendations"].str.contains("Late delivery").sum()),
            "boleto_promise_gap": int(df["recommendations"].str.contains("boleto orders missed").sum()),
            "failed_above_peer_p75": int(df["recommendations"].str.contains("Canceled/unavailable").sum()),
            "installment_gap": int(df["recommendations"].str.contains("installments vs category").sum()),
        },
        "gmv_share_of_flagged_sellers_pct": round(float(df.loc[df["n_flags"] > 0, "gmv_brl"].sum() / df["gmv_brl"].sum() * 100), 1),
    }
    (OUT / "scorecard_summary.json").write_text(json.dumps(summary, indent=2))
    return df, summary


if __name__ == "__main__":
    df, s = build()
    print(json.dumps(s, indent=2))
    print(df[["seller_id", "peer_category", "orders", "late_pct", "n_flags", "recommendations"]].head(5).to_string())
