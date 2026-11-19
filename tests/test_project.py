import json
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "genai")]

import assistant as A  # noqa: E402
import debit_cost_benchmark as D  # noqa: E402
import evaluate as E  # noqa: E402


def test_warehouse_dq_pass():
    dq = json.loads((ROOT / "results" / "dq_checks.json").read_text())
    assert dq["pass"]
    assert dq["fact_order_rows"] == 99441 and dq["fact_payment_rows"] == 103886


def test_raw_data_checksums_match():
    import hashlib
    for line in (ROOT / "data" / "raw" / "SHA256SUMS").read_text().splitlines():
        digest, name = line.split()
        assert hashlib.sha256((ROOT / "data" / "raw" / name).read_bytes()).hexdigest() == digest


@pytest.mark.parametrize("sql", [
    "delete from fact_order",
    "update fact_order set review_score = 5",
    "drop table dim_seller",
    "select * from read_csv('/etc/passwd')",
    "select * from raw_customers",
    "attach '/tmp/x.db' as x",
    "select 1; select 2",
    "copy fact_order to '/tmp/out.csv'",
])
def test_guardrail_blocks(sql):
    with pytest.raises(A.GuardrailError):
        A.check_sql(sql)


def test_guardrail_allows_cte_and_join():
    sql = ("with t as (select seller_id, sum(total_paid) g from fact_order group by 1) "
           "select s.seller_state, sum(t.g) from t join dim_seller s using(seller_id) group by 1")
    assert A.check_sql(sql) == sql
    assert len(A.run_sql(sql)) > 10


def test_engine_blocks_file_access_even_if_parser_missed():
    con = A.connect_readonly()
    with pytest.raises(Exception):
        con.execute("select * from read_csv('/etc/hostname')").fetchall()
    con.close()


def test_oracle_scores_perfect():
    items = {g["id"]: {"sql": g["sql"] or "delete from fact_order"} for g in E.load_golden()}
    summary, _ = E.score_items(items)
    assert summary["execution_accuracy_pct"] == 100.0 and summary["adversarial_blocked_pct"] == 100.0


def test_scorer_rejects_wrong_answers():
    # negative control: plausible-but-wrong SQL must not score
    wrong = {
        "q05": "select round(avg(approval_hours),2) from fact_order where primary_payment_type='boleto'",  # mean not median
        "q17": "select seller_id, round(sum(total_paid),2) g from fact_order group by 1 order by g asc limit 5",
        "q29": "select covered_pct_txn from ref_debit_interchange_2024 where network='All networks'",
        "a01": "select count(*) from fact_order",  # model answered instead of refusing -> executed
    }
    _, results = E.score_items({k: {"sql": v} for k, v in wrong.items()})
    by = {r["id"]: r for r in results}
    assert not by["q05"]["correct"] and not by["q17"]["correct"] and not by["q29"]["correct"]
    assert not by["a01"]["correct"]


def test_fed_table_internally_consistent():
    assert D.validate(D.load())["pass"]


def test_fed_routing_numbers():
    _, routing = D.benchmark(D.load())
    assert routing["exempt_gap_per_txn"] == 0.35
    assert routing["dual_all_in_pct"] > routing["single_all_in_pct_upper_bound"]


def test_analysis_outputs():
    k = json.loads((ROOT / "results" / "kpis.json").read_text())
    imp = k["approval_delay_impact"]
    assert imp["odds_ratio_95ci"][0] > 1  # association is significant
    assert imp["review_points_lost_when_late"] < 0
    assert set(k["late_risk_model"]) >= {"auc_gain", "auc_gain_95ci"}


def test_repair_only_prepends_select_to_expressions():
    assert A.repair_sql("ROUND(SUM(total_paid), 2) AS g FROM fact_order").startswith("SELECT ")
    assert A.repair_sql("SELECT 1") == "SELECT 1"
    assert A.repair_sql("DELETE FROM fact_order") == "DELETE FROM fact_order"  # not turned into a SELECT
    assert A.repair_sql(None) is None


def test_repair_cannot_unblock_writes():
    with pytest.raises(A.GuardrailError):
        A.check_sql(A.repair_sql("DROP TABLE dim_seller"))
