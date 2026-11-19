#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
python3 src/build_warehouse.py
python3 src/analysis.py
python3 src/seller_scorecard.py
python3 src/debit_cost_benchmark.py
python3 genai/golden_set.py
python3 genai/evaluate.py oracle
for f in genai/runs/nova-*_v?.json; do python genai/evaluate.py score "$f"; done
python3 -m pytest -q tests
