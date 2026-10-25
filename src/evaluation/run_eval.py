"""离线评测脚本：对运行中的服务回放 20 条测试集，输出指标与 Markdown 结果表。

用法：
    # 先启动服务（uvicorn main:app --port 8000）
    python -m src.evaluation.run_eval
    python -m src.evaluation.run_eval --base-url http://127.0.0.1:8000 --json-out result.json

指标口径：
- 意图准确率：router 节点输出的 request_type 与标注一致（FAQ 短路样本不参与）
- 信息抽取准确率：info_extract 判定的缺失字段集合与标注一致
- 状态准确率：done 事件的 status（ai_solved / pending_human / pending_info）与标注一致
- 人工转接准确率：done 事件的 need_human 与标注一致
- 工具调用率：应调用工具的样本中实际发生工具调用的比例
- 平均耗时 / P95：done 事件中的 elapsed（秒）
"""

import argparse
import json
import statistics
import time
import urllib.request
from pathlib import Path
from typing import Dict, List, Optional, Tuple

DEFAULT_DATASET = Path(__file__).with_name("dataset.json")


def chat(base_url: str, user_input: str, timeout: float) -> Tuple[List[Dict], float]:
    """调用 /api/chat，解析 SSE 事件，返回事件列表与端到端耗时。"""
    body = json.dumps(
        {"session_id": f"eval_{int(time.time() * 1000)}", "user_input": user_input},
        ensure_ascii=False,
    ).encode("utf-8")
    req = urllib.request.Request(
        f"{base_url.rstrip('/')}/api/chat",
        data=body,
        headers={"Content-Type": "application/json"},
    )
    started = time.time()
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8")
    events = []
    for line in raw.splitlines():
        if line.startswith("data: "):
            try:
                events.append(json.loads(line[6:]))
            except json.JSONDecodeError:
                continue
    return events, round(time.time() - started, 2)


def _stages(events: List[Dict]) -> Dict[str, str]:
    return {e["node"]: e.get("detail", "") for e in events if e.get("type") == "stage"}


def _done(events: List[Dict]) -> Dict:
    for e in reversed(events):
        if e.get("type") == "done":
            return e
    return {}


def _parse_missing(detail: str) -> Optional[List[str]]:
    if not detail:
        return None
    if detail == "信息完整":
        return []
    if detail.startswith("缺失："):
        return [x for x in detail[len("缺失："):].split("、") if x]
    return None


def _answer(events: List[Dict]) -> str:
    return "".join(e.get("content", "") for e in events if e.get("type") == "token")


def run_case(base_url: str, case: Dict, timeout: float) -> Dict:
    events, wall = chat(base_url, case["input"], timeout)
    stages = _stages(events)
    done = _done(events)

    intent = stages.get("router")
    missing = _parse_missing(stages.get("info_extract", ""))
    tool_detail = stages.get("tool_agent", "")
    tools_called = bool(tool_detail) and tool_detail != "本轮无需工具"
    status = done.get("status")
    need_human = bool(done.get("need_human"))

    checks = {
        "intent": (intent == case["expect_type"]) if case["expect_type"] else None,
        "info": (missing == case["expect_missing"]) if not case["expect_faq"] else None,
        "status": status == case["expect_status"],
        "human": need_human == case["expect_human"],
        "faq": (done.get("source") == "faq") == case["expect_faq"],
    }
    if case["expect_faq"] or case["expect_status"] == "pending_info":
        checks["tool"] = None
    else:
        checks["tool"] = tools_called == case["expect_tools"]

    return {
        "id": case["id"],
        "input": case["input"],
        "expect_type": case["expect_type"],
        "actual_type": intent,
        "expect_missing": case["expect_missing"],
        "actual_missing": missing,
        "expect_status": case["expect_status"],
        "actual_status": status,
        "expect_human": case["expect_human"],
        "actual_human": need_human,
        "tools_called": tools_called,
        "expect_tools": case["expect_tools"],
        "expect_faq": case["expect_faq"],
        "faq": done.get("source") == "faq",
        "citations": done.get("citations") or [],
        "elapsed": done.get("elapsed", wall),
        "checks": checks,
        "answer": _answer(events)[:120],
    }


def _rate(values: List[bool]) -> Optional[float]:
    return (sum(values) / len(values) * 100) if values else None


def _fmt(value: Optional[float]) -> str:
    return "-" if value is None else f"{value:.1f}%"


def summarize(rows: List[Dict]) -> Dict:
    latencies = [r["elapsed"] for r in rows if r["elapsed"]]
    non_faq = [r for r in rows if not r["faq"]]
    rag_rows = [r for r in rows if not r["faq"] and r["checks"]["status"]]
    cited = [r for r in rag_rows if r["citations"]]
    return {
        "total": len(rows),
        "intent_acc": _rate([r["checks"]["intent"] for r in rows if r["checks"]["intent"] is not None]),
        "info_acc": _rate([r["checks"]["info"] for r in rows if r["checks"]["info"] is not None]),
        "status_acc": _rate([r["checks"]["status"] for r in rows]),
        "human_acc": _rate([r["checks"]["human"] for r in rows]),
        "tool_rate": _rate([r["tools_called"] for r in rows if r["expect_tools"]]),
        "tool_false_positive": sum(
            1 for r in rows if not r["expect_tools"] and r["tools_called"] and not r["faq"]
        ),
        "citation_rate": _rate([bool(r["citations"]) for r in rag_rows]),
        "avg_elapsed": round(statistics.mean(latencies), 2) if latencies else 0,
        "p95_elapsed": round(
            sorted(latencies)[max(0, int(len(latencies) * 0.95) - 1)], 2
        ) if latencies else 0,
        "avg_elapsed_non_faq": round(statistics.mean([r["elapsed"] for r in non_faq]), 2) if non_faq else 0,
        "faq_hit": f"{sum(1 for r in rows if r['faq'])}/{sum(1 for r in rows if r['expect_faq'])}",
    }


def print_report(rows: List[Dict], summary: Dict):
    print("\n| ID | 输入 | 期望类型 | 实际类型 | 缺失字段 | 状态 | 人工 | 工具 | 耗时(s) |")
    print("| --- | --- | --- | --- | --- | --- | --- | --- | --- |")
    for r in rows:
        missing = "、".join(r["actual_missing"] or []) or "-"
        ok = "PASS" if all(v is not False for v in r["checks"].values()) else "FAIL"
        print(
            f"| {r['id']} {ok} | {r['input']} | {r['expect_type'] or 'faq'} | "
            f"{r['actual_type'] or '-'} | {missing} | {r['actual_status']} | "
            f"{'是' if r['actual_human'] else '否'} | "
            f"{'是' if r['tools_called'] else '否'} | {r['elapsed']} |"
        )

    print("\n### 汇总指标\n")
    print("| 指标 | 结果 |")
    print("| --- | --- |")
    print(f"| 样本数 | {summary['total']} |")
    print(f"| 意图准确率 | {_fmt(summary['intent_acc'])} |")
    print(f"| 信息抽取准确率 | {_fmt(summary['info_acc'])} |")
    print(f"| 工单状态准确率 | {_fmt(summary['status_acc'])} |")
    print(f"| 人工转接准确率 | {_fmt(summary['human_acc'])} |")
    print(f"| 工具调用率（应调用样本） | {_fmt(summary['tool_rate'])} |")
    print(f"| 工具误调用次数 | {summary['tool_false_positive']} |")
    print(f"| RAG 引用覆盖率 | {_fmt(summary['citation_rate'])} |")
    print(f"| FAQ 命中 | {summary['faq_hit']} |")
    print(f"| 平均耗时 | {summary['avg_elapsed']}s |")
    print(f"| P95 耗时 | {summary['p95_elapsed']}s |")
    print(f"| 非 FAQ 平均耗时 | {summary['avg_elapsed_non_faq']}s |")

    failed = [r for r in rows if any(v is False for v in r["checks"].values())]
    if failed:
        print("\n### 未通过样本\n")
        for r in failed:
            bad = [k for k, v in r["checks"].items() if v is False]
            print(f"- {r['id']} {r['input']} → 未通过：{', '.join(bad)}")
            print(f"  - 回答：{r['answer']}")


def main():
    parser = argparse.ArgumentParser(description="物流售后 Agent 离线评测")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--dataset", default=str(DEFAULT_DATASET))
    parser.add_argument("--timeout", type=float, default=120)
    parser.add_argument("--json-out", default="")
    args = parser.parse_args()

    cases = json.loads(Path(args.dataset).read_text(encoding="utf-8"))
    rows = []
    for case in cases:
        print(f"running {case['id']}: {case['input']}")
        rows.append(run_case(args.base_url, case, args.timeout))

    summary = summarize(rows)
    print_report(rows, summary)

    if args.json_out:
        Path(args.json_out).write_text(
            json.dumps({"summary": summary, "rows": rows}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(f"\n结果已写入 {args.json_out}")


if __name__ == "__main__":
    main()
