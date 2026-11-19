"""Payment-performance KPIs, approval-delay impact, late-delivery risk model, delivery-promise simulation.

Outputs results/kpis.json and results/*.csv (Tableau-ready extracts).
"""
import json
import pathlib

import duckdb
import numpy as np
import pandas as pd
import statsmodels.formula.api as smf
from sklearn.metrics import roc_auc_score
from xgboost import XGBClassifier

ROOT = pathlib.Path(__file__).resolve().parents[1]
DB = ROOT / "data" / "payments.duckdb"
OUT = ROOT / "results"
SEED = 7


def con():
    return duckdb.connect(str(DB), read_only=True)


# ---------------------------------------------------------------- KPIs
KPI_SQL = """
select primary_payment_type                                   as payment_type,
       count(*)                                               as orders,
       round(sum(total_paid), 2)                              as gmv_brl,
       round(sum(total_paid) / sum(sum(total_paid)) over () * 100, 2) as gmv_share_pct,
       round(avg(total_paid), 2)                              as avg_order_value_brl,
       round(median(approval_hours), 2)                       as median_approval_hours,
       round(quantile_cont(approval_hours, 0.9), 2)           as p90_approval_hours,
       round(avg(is_failed::int) * 100, 2)                    as failed_order_pct,
       round(avg(is_late::int) * 100, 2)                      as late_delivery_pct,
       round(avg(review_score), 3)                            as avg_review
from fact_order
where primary_payment_type is not null and primary_payment_type <> 'not_defined'
group by 1 order by orders desc
"""

INSTALLMENT_SQL = """
select case when max_installments <= 1 then '1'
            when max_installments <= 3 then '2-3'
            when max_installments <= 6 then '4-6'
            else '7+' end                                     as installment_band,
       count(*)                                               as orders,
       round(avg(total_paid), 2)                              as avg_order_value_brl,
       round(sum(total_paid) / sum(sum(total_paid)) over () * 100, 2) as gmv_share_pct,
       round(avg((recon_diff > 1)::int) * 100, 2)             as paid_above_items_pct
from fact_order
where primary_payment_type = 'credit_card' and total_paid is not null
group by 1 order by 1
"""

APPROVAL_BAND_SQL = """
select case when approval_hours < 1 then '1: <1h'
            when approval_hours < 24 then '2: 1-24h'
            when approval_hours < 48 then '3: 24-48h'
            else '4: 48h+' end                                as approval_band,
       count(*)                                               as delivered_orders,
       round(avg(is_late::int) * 100, 2)                      as late_delivery_pct,
       round(median(delivery_days), 1)                        as median_delivery_days,
       round(avg(review_score), 3)                            as avg_review
from fact_order
where order_status = 'delivered' and delivered_at is not null and approval_hours is not null
group by 1 order by 1
"""


def delivered_frame(c):
    df = c.execute("""
        select order_id, purchased_at, purchase_month, approval_hours, delivery_days, is_late, review_score,
               primary_payment_type, max_installments, n_items, n_sellers, items_price, items_freight,
               customer_state, category, seller_id,
               epoch(estimated_delivery_at - purchased_at) / 86400.0 as promised_days,
               delivered_at, estimated_delivery_at
        from fact_order
        where order_status = 'delivered' and delivered_at is not null and approval_hours is not null
          and primary_payment_type in ('credit_card','boleto','debit_card','voucher')
    """).df()
    df["is_late"] = df["is_late"].astype(int)
    df["approval_days"] = df["approval_hours"] / 24
    top_cat = df["category"].value_counts().index[:30]
    df["cat30"] = np.where(df["category"].isin(top_cat), df["category"], "other")
    df["month"] = df["purchase_month"].dt.strftime("%Y-%m")
    # keep months with enough volume for stable fixed effects (drops sparse 2016 / late-2018 tail)
    vc = df["month"].value_counts()
    df = df[df["month"].isin(vc[vc >= 500].index)].copy()
    df["freight_ratio"] = df["items_freight"] / df["items_price"].clip(lower=1)
    df["log_price"] = np.log1p(df["items_price"])
    return df


def approval_impact(df):
    """Logit: late ~ approval_days + controls (customer state, month, category, basket)."""
    f = ("is_late ~ approval_days + log_price + freight_ratio + n_items + promised_days"
         " + C(customer_state) + C(month) + C(cat30)")
    m = smf.logit(f, data=df).fit(disp=0, maxiter=100, method="newton")
    assert m.mle_retvals["converged"], "logit did not converge"
    b, se = m.params["approval_days"], m.bse["approval_days"]
    # average marginal effect of one extra approval day on late probability: mean(p(1-p)) * beta
    p = m.predict(df)
    w = float((p * (1 - p)).mean())
    ame = {"dy/dx": w * b, "lo": w * (b - 1.96 * se), "hi": w * (b + 1.96 * se)}
    # review ~ late (+ same controls) : what a late delivery costs in review points
    r = smf.ols("review_score ~ is_late + log_price + freight_ratio + n_items + C(customer_state) + C(month) + C(cat30)",
                data=df.dropna(subset=["review_score"])).fit(cov_type="HC1")
    return {
        "n_delivered_orders": int(len(df)),
        "odds_ratio_per_approval_day": round(float(np.exp(b)), 4),
        "odds_ratio_95ci": [round(float(np.exp(b - 1.96 * se)), 4), round(float(np.exp(b + 1.96 * se)), 4)],
        "p_value": float(m.pvalues["approval_days"]),
        "ame_late_pp_per_approval_day": round(float(ame["dy/dx"]) * 100, 3),
        "ame_95ci_pp": [round(float(ame["lo"]) * 100, 3), round(float(ame["hi"]) * 100, 3)],
        "review_points_lost_when_late": round(float(r.params["is_late"]), 3),
        "review_late_95ci": [round(float(x), 3) for x in r.conf_int().loc["is_late"]],
        "controls": "customer state, purchase month, top-30 category, log item price, freight ratio, items, promised days",
        "design_note": "Observational; approval delay is mostly driven by payment method (boleto), so this is an association conditional on controls, not a randomized effect.",
    }


def late_risk_model(df):
    """Does payment info improve late-delivery risk scoring? Time-ordered split, XGBoost, bootstrap CI on AUC gain."""
    df = df.sort_values("purchased_at").reset_index(drop=True)
    cut = int(len(df) * 0.8)
    base_cols = ["log_price", "freight_ratio", "n_items", "n_sellers", "promised_days"]
    cats = ["customer_state", "cat30"]
    X = pd.get_dummies(df[base_cols + cats], columns=cats, dtype=float)
    pay = pd.get_dummies(df[["primary_payment_type"]], dtype=float)
    pay["max_installments"] = df["max_installments"].fillna(1)
    pay["approval_hours"] = df["approval_hours"]
    Xp = pd.concat([X, pay], axis=1)
    y = df["is_late"].values
    res = {}
    preds = {}
    for name, M in [("without_payment_features", X), ("with_payment_features", Xp)]:
        clf = XGBClassifier(n_estimators=300, max_depth=4, learning_rate=0.05, subsample=0.8,
                            colsample_bytree=0.8, random_state=SEED, n_jobs=4, eval_metric="auc")
        clf.fit(M.iloc[:cut], y[:cut])
        p = clf.predict_proba(M.iloc[cut:])[:, 1]
        preds[name] = p
        res[name + "_auc"] = round(float(roc_auc_score(y[cut:], p)), 4)
    yt = y[cut:]
    rng = np.random.default_rng(SEED)
    gains = []
    for _ in range(500):
        i = rng.integers(0, len(yt), len(yt))
        if yt[i].min() == yt[i].max():
            continue
        gains.append(roc_auc_score(yt[i], preds["with_payment_features"][i]) - roc_auc_score(yt[i], preds["without_payment_features"][i]))
    res["auc_gain"] = round(res["with_payment_features_auc"] - res["without_payment_features_auc"], 4)
    res["auc_gain_95ci"] = [round(float(np.percentile(gains, 2.5)), 4), round(float(np.percentile(gains, 97.5)), 4)]
    res["train_orders"], res["test_orders"] = cut, len(df) - cut
    res["test_late_rate_pct"] = round(float(yt.mean() * 100), 2)
    res["split"] = "time-ordered 80/20 by purchase timestamp"
    return res


def promise_simulation(df):
    """Delivery promise ignores payment method. Simulate padding the promise for boleto orders only."""
    b = df[df["primary_payment_type"] == "boleto"]
    nb = df[df["primary_payment_type"] != "boleto"]
    out = {
        "boleto_median_promised_days": round(float(b["promised_days"].median()), 2),
        "non_boleto_median_promised_days": round(float(nb["promised_days"].median()), 2),
        "boleto_median_approval_hours": round(float(b["approval_hours"].median()), 2),
        "boleto_delivered_orders": int(len(b)),
        "boleto_late_orders_now": int(b["is_late"].sum()),
        "scenarios": [],
    }
    deliv = b["delivered_at"].dt.normalize()
    for pad in (1, 2):
        new_est = (b["estimated_delivery_at"] + pd.Timedelta(days=pad)).dt.normalize()
        still_late = int((deliv > new_est).sum())
        out["scenarios"].append({
            "pad_days": pad,
            "boleto_late_orders": still_late,
            "late_flags_avoided": int(b["is_late"].sum() - still_late),
            "boleto_late_pct": round(still_late / len(b) * 100, 2),
        })
    out["boleto_late_pct_now"] = round(float(b["is_late"].mean() * 100), 2)
    out["non_boleto_late_pct"] = round(float(nb["is_late"].mean() * 100), 2)
    out["caveat"] = ("Relabels lateness against a longer promise; it does not model whether a longer promise "
                     "lowers conversion, so it sizes the expectation gap, not a forecast of review gains.")
    return out


def main():
    OUT.mkdir(exist_ok=True)
    c = con()
    kpi = c.execute(KPI_SQL).df()
    inst = c.execute(INSTALLMENT_SQL).df()
    band = c.execute(APPROVAL_BAND_SQL).df()
    kpi.to_csv(OUT / "kpi_by_payment_type.csv", index=False)
    inst.to_csv(OUT / "installment_bands.csv", index=False)
    band.to_csv(OUT / "approval_bands.csv", index=False)
    df = delivered_frame(c)
    result = {
        "kpi_by_payment_type": kpi.to_dict("records"),
        "installment_bands": inst.to_dict("records"),
        "approval_bands": band.to_dict("records"),
        "approval_delay_impact": approval_impact(df),
        "late_risk_model": late_risk_model(df),
        "promise_simulation": promise_simulation(df),
    }
    (OUT / "kpis.json").write_text(json.dumps(result, indent=2, default=str))
    print(json.dumps({k: result[k] for k in ["approval_delay_impact", "late_risk_model", "promise_simulation"]}, indent=2))
    print(kpi.to_string(index=False)); print(inst.to_string(index=False)); print(band.to_string(index=False))


if __name__ == "__main__":
    main()
