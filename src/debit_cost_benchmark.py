"""U.S. debit acceptance-cost benchmark from Federal Reserve Regulation II data (2024 interchange, 2023 fraud).

1. Validates the transcribed table (fee% == fee / ticket within rounding; exempt+covered shares sum to 100).
2. Ranks networks by interchange cost per $100 for covered and exempt transactions.
3. Computes the per-transaction routing gap (dual- vs single-message) and a fraud-adjusted merchant cost,
   using only shares the Fed publishes (single-message merchant share is not published -> upper bound).
Network/acquirer fees are not included (not in this table).
"""
import json
import pathlib

import pandas as pd

ROOT = pathlib.Path(__file__).resolve().parents[1]
FED = ROOT / "data" / "fed"
OUT = ROOT / "results"


def load():
    return pd.read_csv(FED / "regii_avg_interchange_2024.csv")


def validate(df):
    issues = []
    for seg in ["exempt", "covered", "all"]:
        sub = df.dropna(subset=[f"{seg}_avg_fee", f"{seg}_avg_txn_value"])
        implied = sub[f"{seg}_avg_fee"] / sub[f"{seg}_avg_txn_value"] * 100
        # fees are rounded to the cent, so allow the error that $0.005 rounding implies, plus 0.01pp display rounding
        tol = 0.005 / sub[f"{seg}_avg_txn_value"] * 100 + 0.011
        bad = sub[(implied - sub[f"{seg}_fee_pct"]).abs() > tol]
        issues += [f"{seg}:{n}" for n in bad["network"]]
    share = (df["exempt_pct_txn"] + df["covered_pct_txn"] - 100).abs()
    issues += [f"txn_share:{n}" for n in df.loc[share > 0.15, "network"]]
    return {"rows": int(len(df)), "fee_pct_consistency_failures": issues, "pass": not issues}


def benchmark(df):
    fraud = json.loads((FED / "fraud_2023.json").read_text())
    nets = df[df["network_type"].isin(["dual", "single"])].copy()
    rank = nets[["network", "network_type", "covered_avg_fee", "covered_fee_pct", "exempt_avg_fee", "exempt_fee_pct",
                 "exempt_pct_txn"]].copy()
    rank["exempt_minus_covered_fee"] = (rank["exempt_avg_fee"] - rank["covered_avg_fee"]).round(2)
    rank = rank.sort_values("exempt_fee_pct")
    tot = df.set_index("network")
    dual, single = tot.loc["Dual-message"], tot.loc["Single-message"]

    # fraud-adjusted merchant cost per $100 (interchange + merchant-borne fraud)
    dual_merchant_fraud_bps = fraud["dual_message_fraud_bps"] * fraud["dual_message_merchant_share_pct"] / 100
    single_merchant_fraud_bps_max = fraud["single_message_fraud_bps"] * (100 - fraud["single_message_issuer_share_pct"]) / 100
    routing = {
        "exempt_dual_fee": float(dual["exempt_avg_fee"]), "exempt_single_fee": float(single["exempt_avg_fee"]),
        "exempt_gap_per_txn": round(float(dual["exempt_avg_fee"] - single["exempt_avg_fee"]), 2),
        "exempt_gap_pct_of_dual": round(float((dual["exempt_avg_fee"] - single["exempt_avg_fee"]) / dual["exempt_avg_fee"] * 100), 1),
        "exempt_gap_per_100_usd": round(float(dual["exempt_fee_pct"] - single["exempt_fee_pct"]), 2),
        "covered_dual_fee": float(dual["covered_avg_fee"]), "covered_single_fee": float(single["covered_avg_fee"]),
        "covered_gap_per_txn": round(float(dual["covered_avg_fee"] - single["covered_avg_fee"]), 2),
        "dual_merchant_fraud_bps": round(dual_merchant_fraud_bps, 2),
        "single_merchant_fraud_bps_upper_bound": round(single_merchant_fraud_bps_max, 2),
        "dual_all_in_pct": round(float(dual["all_fee_pct"]) + dual_merchant_fraud_bps / 100, 3),
        "single_all_in_pct_upper_bound": round(float(single["all_fee_pct"]) + single_merchant_fraud_bps_max / 100, 3),
        "notes": [
            "Interchange is what issuers receive; merchants pay it through acquirer pricing. Network and acquirer markups excluded.",
            "Single-message merchant fraud share is not published; bound uses (100% - issuer share 45.3%), so cardholder share is attributed to merchants (conservative).",
            "Fraud figures are 2023 covered-issuer data; interchange is 2024. Mixed years, stated explicitly.",
            "Single-message routing is mostly card-present PIN today; card-not-present PINless availability varies by network and issuer.",
        ],
    }
    routing["all_in_gap_pct_points_at_least"] = round(routing["dual_all_in_pct"] - routing["single_all_in_pct_upper_bound"], 3)
    return rank, routing


def main():
    OUT.mkdir(exist_ok=True)
    df = load()
    v = validate(df)
    rank, routing = benchmark(df)
    rank.to_csv(OUT / "debit_network_benchmark.csv", index=False)
    res = {"validation": v, "routing": routing}
    (OUT / "debit_cost_benchmark.json").write_text(json.dumps(res, indent=2))
    print(json.dumps(res, indent=2))
    print(rank.to_string(index=False))
    return res


if __name__ == "__main__":
    main()
