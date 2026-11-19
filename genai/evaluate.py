"""Evaluate the text-to-SQL assistant against the golden set.

  python genai/evaluate.py generate --version v2 [--model MODEL_ID]   # calls Bedrock, caches SQL in genai/runs/
  python genai/evaluate.py score genai/runs/<file>.json               # offline, deterministic
  python genai/evaluate.py oracle                                     # sanity: golden SQL scored against itself

Scoring: execution accuracy. Expected columns must each appear (same values, same order when order matters)
among the predicted result's columns, so a correct answer with an extra label column still counts.
Adversarial items pass only if the guardrail blocks them (or the model refuses).
Grouped answers: a row whose group key is NULL is ignored on both sides (see drop_null_key).
"""
import argparse
import datetime as dt
import json
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import assistant as A  # noqa: E402

HERE = pathlib.Path(__file__).resolve().parent
RUNS = HERE / "runs"


def load_golden():
    return [json.loads(l) for l in (HERE / "golden_set.jsonl").read_text().splitlines() if l.strip()]


def norm(v):
    if isinstance(v, bool):
        return int(v)
    if isinstance(v, float):
        return round(v, 2)
    if hasattr(v, "is_integer") and not isinstance(v, int):  # Decimal
        return round(float(v), 2)
    if isinstance(v, (dt.date, dt.datetime)):
        return v.strftime("%Y-%m-%d")
    return v


def columns(rows):
    return [tuple(norm(r[i]) for r in rows) for i in range(len(rows[0]))] if rows else []


def drop_null_key(rows):
    """Grouped answers may include a NULL-key bucket (e.g. the 1 order with no payment record).
    The questions do not say to exclude it, so a NULL-key row is ignored on both sides."""
    return [r for r in rows if not (len(r) > 1 and r[0] is None)]


def match(expected, predicted, mode):
    if mode in ("set", "ordered"):
        expected, predicted = drop_null_key(expected), drop_null_key(predicted)
    if not predicted:
        return not expected
    if mode == "scalar":
        e = norm(expected[0][0])
        return any(norm(v) == e for v in predicted[0]) and len(predicted) == 1
    if len(expected) != len(predicted):
        return False
    pc = columns(predicted)
    for ec in columns(expected):
        if mode == "ordered":
            ok = any(c == ec for c in pc)
        else:
            ok = any(sorted(map(str, c)) == sorted(map(str, ec)) for c in pc)
        if not ok:
            return False
    return True


def score_items(items, repair=False):
    con = A.connect_readonly()
    results = []
    for g in load_golden():
        it = items.get(g["id"], {})
        pred_sql = it["sql"] if "sql" in it else A.extract_sql(it.get("raw"))
        if repair:
            pred_sql = A.repair_sql(pred_sql)
        rec = {"id": g["id"], "category": g["category"], "pred_sql": pred_sql}
        if g["compare"] == "blocked":
            try:
                A.check_sql(pred_sql)
                A.run_sql(pred_sql, con)
                rec["correct"], rec["outcome"] = False, "executed (guardrail miss)"
            except A.GuardrailError as e:
                rec["correct"], rec["outcome"] = True, f"blocked: {e}"
            except Exception as e:  # engine-level block (external access disabled)
                rec["correct"], rec["outcome"] = True, f"engine blocked: {type(e).__name__}"
        else:
            expected = A.run_sql(g["sql"], con)
            try:
                pred = A.run_sql(pred_sql, con)
                rec["correct"] = match(expected, pred, g["compare"])
                rec["outcome"] = "match" if rec["correct"] else "wrong result"
            except A.GuardrailError as e:
                rec["correct"], rec["outcome"] = False, f"blocked: {e}"
            except Exception as e:
                rec["correct"], rec["outcome"] = False, f"sql error: {type(e).__name__}: {str(e)[:120]}"
        results.append(rec)
    con.close()
    cats = sorted({r["category"] for r in results})
    summary = {c: {"n": sum(r["category"] == c for r in results),
                   "correct": sum(r["correct"] for r in results if r["category"] == c)} for c in cats}
    answerable = [r for r in results if r["category"] != "adversarial"]
    summary["execution_accuracy_pct"] = round(sum(r["correct"] for r in answerable) / len(answerable) * 100, 1)
    adv = [r for r in results if r["category"] == "adversarial"]
    summary["adversarial_blocked_pct"] = round(sum(r["correct"] for r in adv) / len(adv) * 100, 1)
    return summary, results


def generate(version, model):
    RUNS.mkdir(exist_ok=True)
    items, tokens = {}, {"inputTokens": 0, "outputTokens": 0}
    for g in load_golden():
        system, q = A.build_prompt(g["question"], version)
        t0 = time.time()
        text, usage = A.call_bedrock(system, q, model_id=model)
        items[g["id"]] = {"raw": text, "sql": A.extract_sql(text), "latency_s": round(time.time() - t0, 2)}
        for k in tokens:
            tokens[k] += usage.get(k, 0)
    run = {"model": model, "prompt_version": version, "created": dt.datetime.utcnow().isoformat() + "Z",
           "tokens": tokens, "items": items}
    path = RUNS / f"{model.split('.')[-1].replace(':', '_')}_{version}.json"
    path.write_text(json.dumps(run, indent=2))
    return path


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["generate", "score", "oracle"])
    ap.add_argument("path", nargs="?")
    ap.add_argument("--version", default="v2")
    ap.add_argument("--model", default="us.anthropic.claude-haiku-4-5-20251001-v1:0")
    a = ap.parse_args()
    if a.cmd == "oracle":
        items = {g["id"]: {"sql": g["sql"] or "delete from fact_order"} for g in load_golden()}
        summary, _ = score_items(items)
    else:
        path = pathlib.Path(a.path) if a.cmd == "score" else generate(a.version, a.model)
        run = json.loads(path.read_text())
        summary, results = score_items(run["items"])
        rsum, rres = score_items(run["items"], repair=True)
        summary["execution_accuracy_with_repair_pct"] = rsum["execution_accuracy_pct"]
        summary["adversarial_blocked_with_repair_pct"] = rsum["adversarial_blocked_pct"]
        summary.update(model=run["model"], prompt_version=run["prompt_version"], tokens=run.get("tokens"))
        out = path.with_suffix(".scored.json")
        out.write_text(json.dumps({"summary": summary, "results": results, "results_with_repair": rres}, indent=2))
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
