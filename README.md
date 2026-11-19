# Merchant Payment Performance Analytics & GenAI SQL Assistant

Payment-performance analytics for marketplace merchants, a U.S. debit acceptance-cost benchmark, and a
guard-railed text-to-SQL assistant with a golden-set evaluation. All inputs are real public data.

## Data
| Source | What | Size |
|---|---|---|
| Olist Brazilian E-Commerce Public Dataset (CC BY-NC-SA 4.0), via GitHub LFS mirror; SHA-256 in `data/raw/SHA256SUMS` | orders, payments, items, sellers, reviews | 99,441 orders, 103,886 payment records, 3,095 sellers (Sep 2016 to Oct 2018, BRL) |
| Federal Reserve Board, Regulation II *Average Debit Card Interchange Fee by Payment Card Network* (2024) | interchange by network, covered vs exempt | 20 network rows |
| Federal Reserve Board, *2023 Interchange Fee Revenue, Covered Issuer Costs, and ... Fraud Losses* (Dec 2025) | fraud bps and loss shares | published highlights |

**Scope limit (important):** Olist only contains orders that went through checkout; 99.9% have an approval
timestamp, so this data **cannot measure authorization/decline rates**. The project measures what the data
does support: approval latency, payment-to-order reconciliation, installment economics, and the downstream
effect of approval delay on delivery and reviews.

## Pipeline
```
python src/build_warehouse.py      # DuckDB star schema + 14 data-quality checks
python src/analysis.py             # KPIs, approval-delay impact model, late-risk model, promise simulation
python src/seller_scorecard.py     # merchant scorecard vs category peers + recommendations
python src/debit_cost_benchmark.py # Fed Reg II routing/cost benchmark (table validated first)
python genai/golden_set.py && python genai/evaluate.py oracle
python -m pytest -q tests          # 19 tests
```
`sql/models.sql` (DuckDB, executed) and `sql/snowflake_ddl.sql` (Snowflake port with a read-only role; syntax-parsed, **not executed** against Snowflake).

## Results (all reproduced from the scripts above)

**Data quality:** 0 duplicate keys, 0 orphan payments/sellers. Reconciliation: 249 of 98,665 orders (0.25%)
have payments differing from item price + freight by more than 1 BRL; 229 of them are card overpayments
(median +10.6%) on orders averaging 6.7 installments, consistent with installment financing charges.

**Payment KPIs**
| Payment type | Orders | GMV share | AOV (BRL) | Median approval | Late delivery | Failed orders |
|---|---|---|---|---|---|---|
| Credit card | 74,975 | 78.35% | 167.29 | 0.27 h | 6.69% | 1.15% |
| Boleto | 19,784 | 17.92% | 145.03 | 29.07 h | 7.32% | 1.24% |
| Voucher | 3,151 | 2.37% | 120.29 | 0.28 h | 6.05% | 3.62% |
| Debit card | 1,527 | 1.36% | 142.72 | 0.85 h | 5.33% | 0.85% |

Card orders with 7+ installments carry 32.68% of card GMV at 337.11 BRL AOV (vs 100.91 BRL for single-payment orders).

**Approval delay → delivery → reviews** (logit with state, month, category and basket controls; 96,189 delivered orders):
each extra day of payment approval is associated with **+0.80 pp** late-delivery probability
(95% CI 0.64 to 0.95; odds ratio 1.147, p = 2.4e-24). A late delivery is associated with **-1.96** review points
(95% CI -2.00 to -1.92). Observational: approval delay is mostly driven by payment method.

**Delivery promise ignores payment method:** boleto orders get the same promise (23.3 vs 23.2 days median)
despite a 29-hour median approval. Re-scoring boleto lateness against a promise padded by 1 or 2 days removes
168 or 290 of 1,404 late flags (12.0% or 20.7%). This sizes the expectation gap; it does not model conversion effects.

**Honest null:** adding payment features (type, installments, approval hours) to an XGBoost late-delivery risk
model on a time-ordered 80/20 split did **not** improve ranking (AUC 0.620 to 0.618; gain -0.002, 95% CI
-0.009 to +0.007). Approval delay shifts average lateness but carries no extra ranking signal beyond basket,
geography and promise length.

**Merchant scorecard:** 567 sellers (30+ delivered orders) benchmarked within 22 category peer groups; 135 flagged
(29.9% of scored GMV): 115 late-delivery above peer p75, 5 cancellation above peer p75, 9 boleto promise gaps,
14 installment-offer gaps. Each flag needs at least 5 underlying events (3 for the boleto rule). Output:
`results/seller_scorecard.csv` with a plain-language recommendation per seller.

**U.S. debit cost benchmark (Fed Reg II):** transcribed table passes an internal-consistency check (fee% equals
fee / ticket within rounding, all 20 rows). For exempt-issuer debit, single-message networks average
$0.26/txn vs $0.61 on dual-message ($0.35, 57.4% lower; 0.74 per $100). For covered debit the gap reverses
slightly (-$0.02). Adding merchant-borne fraud (2023): dual-message all-in 0.905% of value vs at most 0.607% for
single-message (upper bound, because the Fed does not publish the merchant share for single-message). Network
and acquirer fees are excluded.

## GenAI text-to-SQL assistant (`genai/`)
- Guardrails, two layers: (1) sqlglot AST check: one statement, SELECT only, allow-listed tables, no file/table
  functions, no ATTACH/COPY/PRAGMA; (2) DuckDB opened read-only with `enable_external_access=false`.
- Golden set: 30 business questions with trusted SQL (16 KPI, 8 seller, 6 Fed benchmark) + 6 adversarial requests.
  Scoring is execution accuracy on result sets. A negative-control test confirms plausible-but-wrong SQL fails.
- Prompts: v1 = table/column list only; v2 = schema card + KPI glossary + refusal rule; v3 = v2 instructions with
  every Fed column and allowed `network_type` value spelled out (documentation change only, no per-question hints).
- Optional deterministic repair: prepend `SELECT` only when the output is not a valid statement and the result
  parses as a SELECT (tested so it cannot turn a DELETE/DROP into something runnable).
- LLM: Amazon Nova Pro / Nova Lite via Bedrock Converse (us-east-1, temperature 0), run in the author's AWS account.
  Raw outputs stored in S3 and copied to `genai/runs/*.json`; every response carries a SHA-256 prefix checked on load.
  Prompt hashes in each run file match `build_prompt()` locally.

| Run | Execution accuracy | With SELECT repair | KPI /16 | Seller /8 | Fed /6 | Attacks blocked by guardrail | Model refused on its own |
|---|---|---|---|---|---|---|---|
| Nova Pro, v1 | 43.3% | 43.3% | 8 | 5 | 0 | 6/6 | 0/6 |
| Nova Pro, v2 | 73.3% | 76.7% | 14 | 7 | 1 | 6/6 | 6/6 |
| Nova Pro, v3 | 66.7% | 76.7% | 13 | 5 | 2 | 6/6 | 6/6 |
| Nova Lite, v2 | 73.3% | 73.3% | 15 | 7 | 0 | 6/6 | 4/6 |

What this shows:
- Adding the KPI glossary (v1 to v2) lifted accuracy 43.3% to 73.3%. 7 of the 17 v1 misses computed GMV/AOV from
  item price + freight instead of `total_paid`: a metric-definition problem, not SQL syntax.
- In v1 the model wrote the DELETE/UPDATE/DROP/ATTACH statements it was asked for; the guardrail blocked all of them.
  The prompt-level refusal is useful but not sufficient on its own (Nova Lite still emitted 2 of 6).
- The Fed reference table stays weak (best 2/6). Models invent columns or aggregate individual networks where the Fed
  publishes a volume-weighted group row. Spelling out the schema (v3) helped only marginally and the run had more
  dropped-`SELECT` outputs, so v3 is not better than v2 on raw accuracy. Run-to-run variance at temperature 0 is
  visible (a rerun of v1 differed from a first, uncached attempt on a few items).
- Scorer audit before final numbers: fixed one golden answer that rounded to 3 decimals against the 2-decimal glossary,
  and stopped penalizing an extra NULL-key group (1 order has no payment record). Both changes apply to all runs
  equally and are documented in `genai/evaluate.py` and `genai/golden_set.py`.

Total Bedrock usage for the four scored runs: 116,136 input / 8,148 output tokens.

## Tableau
Tableau-ready extracts in `results/*.csv` (KPIs by payment type, installment bands, approval bands, seller scorecard,
debit network benchmark). A Tableau workbook has not been built yet.
