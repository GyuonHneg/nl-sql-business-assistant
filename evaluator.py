"""离线用例集评估：SQL 是确定性输出，用客观指标而非主观打分。

用例文件为 JSONL，每行:
    {"question": "...", "expected_sql": "...", "expect_tool": true}

指标:
    tool_call_rate   工具调用成功率（是否触发 exc_sql）
    exact_match      SQL 归一化后完全一致的比例
    exec_success     生成 SQL 能成功执行的比例
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from assistant import assert_readonly, build_bot, get_engine

_CASES = Path(__file__).with_name("cases.jsonl")


def normalize(sql: str) -> str:
    """归一化：去分号、压空白、统一小写、去掉别名 AS。"""
    s = sql.strip().rstrip(";").lower()
    s = re.sub(r"\s+", " ", s)
    s = re.sub(r"\bas\b", "", s)
    return s.strip()


def load_cases(path: Path = _CASES) -> list[dict]:
    if not path.exists():
        raise FileNotFoundError(f"用例文件不存在: {path}")
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def evaluate(cases: list[dict] | None = None) -> dict:
    cases = cases or load_cases()
    bot = build_bot()
    engine = get_engine()

    tool_calls = exact = exec_ok = 0
    failures: list[dict] = []

    for case in cases:
        question = case["question"]
        messages = [{"role": "user", "content": question}]

        sql = None
        for chunk in bot.run(messages):
            for msg in chunk:
                for item in msg.get("function_call", []) or []:
                    pass
                fc = msg.get("function_call")
                if fc and fc.get("name") == "exc_sql":
                    try:
                        sql = json.loads(fc["arguments"])["sql_input"]
                    except (json.JSONDecodeError, KeyError):
                        sql = None

        if sql is None:
            # 兜底：从文本里抽取代码块中的 SQL
            for msg in chunk:
                m = re.search(r"```sql\s*(.+?)```", msg.get("content", ""), re.S)
                if m:
                    sql = m.group(1)
                    break

        if sql:
            tool_calls += 1
        else:
            failures.append({"question": question, "reason": "未生成 SQL"})
            continue

        if normalize(sql) == normalize(case.get("expected_sql", "")):
            exact += 1

        try:
            assert_readonly(sql)
            with engine.connect() as conn:
                conn.execute(__import__("sqlalchemy").text(sql + " LIMIT 1"))
            exec_ok += 1
        except Exception as exc:  # noqa: BLE001
            failures.append({"question": question, "sql": sql, "reason": str(exc)})

    total = len(cases)
    return {
        "total": total,
        "tool_call_rate": round(tool_calls / total, 3) if total else 0,
        "exact_match_rate": round(exact / total, 3) if total else 0,
        "exec_success_rate": round(exec_ok / total, 3) if total else 0,
        "failures": failures,
    }


if __name__ == "__main__":
    result = evaluate()
    print(json.dumps({k: v for k, v in result.items() if k != "failures"}, ensure_ascii=False, indent=2))
    for f in result["failures"]:
        print(f"  ✗ {f['question']} -> {f.get('reason')}")
