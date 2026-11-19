"""Text-to-SQL analytics assistant with SQL guardrails.

LLM backend: Amazon Bedrock Converse (boto3), model id via BEDROCK_MODEL_ID.
Generated SQL is cached in genai/runs/<run>.json so evaluation is reproducible offline.
"""
import json
import os
import pathlib
import re

import duckdb
import sqlglot
from sqlglot import exp

ROOT = pathlib.Path(__file__).resolve().parents[1]
DB = ROOT / "data" / "payments.duckdb"
ALLOWED_TABLES = {"fact_order", "fact_payment", "dim_seller", "ref_debit_interchange_2024"}
MAX_ROWS = 1000
BLOCKED_FUNCS = {"read_csv", "read_csv_auto", "read_parquet", "read_json", "read_json_auto", "read_text",
                 "read_blob", "glob", "getenv", "sniff_csv", "parquet_scan", "query_table"}

SYSTEM_V1 = ("You translate business questions into one DuckDB SQL SELECT query. "
             "Return only SQL inside ```sql``` fences. Tables:\n{schema_tables}")
SYSTEM_V2 = ("You are a payments analytics assistant. Translate the question into ONE read-only DuckDB SQL query "
             "using only the documented tables and the KPI glossary definitions exactly. Rules: select only the "
             "columns needed to answer; round as the glossary says; never modify data. If the request asks to "
             "change data or use other tables/files, reply exactly REFUSE. Return only SQL in ```sql``` fences.\n\n{schema_card}")


WRITE_NODES = tuple(getattr(exp, n) for n in
                    ["Insert", "Update", "Delete", "Drop", "Create", "Alter", "Command", "Attach", "Detach",
                     "Copy", "Merge", "Pragma", "Set", "Use", "Install", "Load"] if hasattr(exp, n))


class GuardrailError(Exception):
    pass


def schema_card():
    return (ROOT / "genai" / "schema_card.md").read_text()


def schema_tables_only():
    # v1 prompt: table/column lists only, without the KPI glossary
    return schema_card().split("## KPI glossary")[0]


def build_prompt(question, version="v2"):
    if version == "v3":  # same instructions as v2; schema card spells out every Fed column and allowed value
        system = SYSTEM_V2.format(schema_card=(ROOT / "genai" / "schema_card_v3.md").read_text())
    elif version == "v2":
        system = SYSTEM_V2.format(schema_card=schema_card())
    else:
        system = SYSTEM_V1.format(schema_tables=schema_tables_only())
    return system, question


def extract_sql(text):
    if text is None or text.strip().upper().startswith("REFUSE"):
        return None
    m = re.search(r"```(?:sql)?\s*(.*?)```", text, re.S | re.I)
    sql = (m.group(1) if m else text).strip().rstrip(";").strip()
    if not sql or sql.upper() == "REFUSE":
        return None
    return sql


def repair_sql(sql):
    """Deterministic output repair: models sometimes drop the leading SELECT
    (e.g. 'ROUND(...) AS x FROM t'). Prepend it only when the text does not already start
    with SELECT/WITH and the repaired text parses as a SELECT. Never touches other statements."""
    if sql is None or sql.lstrip().upper().startswith(("SELECT", "WITH")):
        return sql
    try:  # only repair text that is not already a valid statement of its own
        sqlglot.parse(sql, read="duckdb")
        return sql
    except sqlglot.errors.ParseError:
        pass
    candidate = "SELECT " + sql
    try:
        tree = sqlglot.parse_one(candidate, read="duckdb")
    except sqlglot.errors.ParseError:
        return sql
    return candidate if isinstance(tree, exp.Select) else sql


def check_sql(sql):
    """Raise GuardrailError unless sql is a single read-only SELECT over allowed tables."""
    if sql is None:
        raise GuardrailError("model refused or returned no SQL")
    try:
        stmts = sqlglot.parse(sql, read="duckdb")
    except sqlglot.errors.ParseError as e:
        raise GuardrailError(f"unparseable SQL: {e}") from e
    if len(stmts) != 1 or stmts[0] is None:
        raise GuardrailError("exactly one statement allowed")
    tree = stmts[0]
    if not isinstance(tree, (exp.Select, exp.Union, exp.Intersect, exp.Except)):
        raise GuardrailError(f"only SELECT allowed, got {type(tree).__name__}")
    for node in tree.walk():
        if isinstance(node, WRITE_NODES):
            raise GuardrailError(f"write/DDL node {type(node).__name__} not allowed")
        if isinstance(node, (exp.Anonymous, exp.Func)):
            name = (node.name if isinstance(node, exp.Anonymous) else node.sql_name()).lower()
            if name in BLOCKED_FUNCS:
                raise GuardrailError(f"function {name} not allowed")
    cte_names = {c.alias_or_name.lower() for c in tree.find_all(exp.CTE)}
    for t in tree.find_all(exp.Table):
        name = t.name.lower()
        if not name:  # table function such as read_csv('...')
            raise GuardrailError("table functions not allowed")
        if name not in ALLOWED_TABLES and name not in cte_names:
            raise GuardrailError(f"table {name} not allowed")
    return sql


def connect_readonly():
    # read-only file + no filesystem/network access from SQL: second layer behind the parser checks
    return duckdb.connect(str(DB), read_only=True, config={"enable_external_access": False})


def run_sql(sql, con=None):
    check_sql(sql)
    own = con is None
    con = con or connect_readonly()
    try:
        rows = con.execute(f"select * from ({sql}) as q limit {MAX_ROWS}").fetchall()
    finally:
        if own:
            con.close()
    return rows


def call_bedrock(system, question, model_id=None, region="us-east-1"):
    import boto3
    model_id = model_id or os.environ.get("BEDROCK_MODEL_ID", "us.anthropic.claude-haiku-4-5-20251001-v1:0")
    client = boto3.client("bedrock-runtime", region_name=region)
    r = client.converse(modelId=model_id, system=[{"text": system}],
                        messages=[{"role": "user", "content": [{"text": question}]}],
                        inferenceConfig={"maxTokens": 600, "temperature": 0})
    usage = r.get("usage", {})
    return r["output"]["message"]["content"][0]["text"], usage


def ask(question, version="v2"):
    system, q = build_prompt(question, version)
    text, _ = call_bedrock(system, q)
    sql = extract_sql(text)
    return sql, run_sql(sql)


if __name__ == "__main__":
    import sys
    print(ask(" ".join(sys.argv[1:]) or "What is total GMV in BRL?"))
