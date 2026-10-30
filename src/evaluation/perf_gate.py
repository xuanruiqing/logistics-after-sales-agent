"""性能门禁：读取 performance_test 输出与 /api/metrics，按阈值判定是否可发布。

用法：
    # 只校验本次压测结果
    python -m src.evaluation.perf_gate --input evaluation_results/core-opt.json --metrics-url http://127.0.0.1:8000/api/metrics

    # 与基线对比，同时校验不得回退
    python -m src.evaluation.perf_gate --input evaluation_results/core-opt.json --baseline evaluation_results/core-c2-n160.json

退出码：
- 0：全部门禁通过
- 1：存在未通过或回退项

门禁口径：
- 成功率：HTTP 成功率必须 100%，业务成功率 >= 0.95
- 延迟：P50 / P95 / P99 不超过阈值；与基线对比时 P95 不得劣化超过 20%
- 降级：业务失败中不得出现降级文案或 error 状态
- 长稳：剔除前 20% 预热样本后，RSS 线性增长 <= 256 KiB/min，且总增长 <= 5%
- 指标：/api/metrics 可访问且检索请求数 > 0（仅在提供 --metrics-url 时校验）
"""

import argparse
import json
import sys
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

DEFAULT_LIMITS = {
    "http_success_rate": 1.0,
    "business_success_rate": 0.95,
    "p50_s": 8.0,
    "p95_s": 15.0,
    "p99_s": 25.0,
    "rss_slope_kib_per_min": 256.0,
}
REGRESSION_TOLERANCE = 1.2
RSS_WARMUP_RATIO = 0.2


def _load(path: Path) -> Dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _fetch_metrics(url: str, timeout: float) -> Dict[str, Any]:
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _rss_slope(result: Dict[str, Any]) -> Optional[float]:
    """剔除预热阶段后计算 RSS 线性斜率（KiB/min），避免缓存填充被误判为泄漏。"""
    samples = [
        s for s in result.get("resource_samples") or []
        if s.get("process_rss_bytes") is not None
    ]
    if len(samples) < 3:
        return None
    start = samples[0]["timestamp"]
    end = samples[-1]["timestamp"]
    cut = start + (end - start) * RSS_WARMUP_RATIO
    points = [(s["timestamp"] - start, s["process_rss_bytes"]) for s in samples if s["timestamp"] >= cut]
    if len(points) < 3:
        return None
    x_mean = sum(x for x, _ in points) / len(points)
    y_mean = sum(y for _, y in points) / len(points)
    denominator = sum((x - x_mean) ** 2 for x, _ in points)
    if not denominator:
        return None
    slope = sum((x - x_mean) * (y - y_mean) for x, y in points) / denominator
    return round(slope * 60 / 1024, 2)


def _checks(result: Dict[str, Any], limits: Dict[str, float], baseline: Optional[Dict[str, Any]]) -> List[Tuple[str, bool, str]]:
    summary = result.get("summary", {}).get("overall", {})
    e2e = summary.get("e2e", {})
    resource = result.get("resource_summary", {})
    rss = resource.get("process_rss_bytes", {})
    rows = result.get("requests", [])

    checks: List[Tuple[str, bool, str]] = []

    http_rate = summary.get("http_success_rate")
    checks.append((
        "HTTP 成功率",
        http_rate is not None and http_rate >= limits["http_success_rate"],
        f"{http_rate} (阈值 >= {limits['http_success_rate']})",
    ))

    biz_rate = summary.get("business_success_rate")
    checks.append((
        "业务成功率",
        biz_rate is not None and biz_rate >= limits["business_success_rate"],
        f"{biz_rate} (阈值 >= {limits['business_success_rate']})",
    ))

    for key in ("p50_s", "p95_s", "p99_s"):
        value = e2e.get(key)
        checks.append((
            f"端到端 {key.upper()}",
            value is not None and value <= limits[key],
            f"{value}s (阈值 <= {limits[key]}s)",
        ))

    degraded_rows = [
        r for r in rows
        if r.get("done_status") == "error" or "降级文案" in str(r.get("error") or "")
    ]
    checks.append(("降级/错误响应", not degraded_rows, f"{len(degraded_rows)} 条"))

    slope = _rss_slope(result)
    if slope is not None and result.get("config", {}).get("mode") == "soak":
        checks.append((
            "RSS 预热后线性增长",
            slope <= limits["rss_slope_kib_per_min"],
            f"{slope} KiB/min (阈值 <= {limits['rss_slope_kib_per_min']})",
        ))
        start = rss.get("start")
        end = rss.get("end")
        if start and end:
            growth = (end - start) / start
            checks.append((
                "RSS 总增长",
                growth <= 0.05,
                f"{growth:.2%} (阈值 <= 5%)",
            ))

    if baseline:
        base_e2e = baseline.get("summary", {}).get("overall", {}).get("e2e", {})
        base_p95 = base_e2e.get("p95_s")
        if base_p95 and e2e.get("p95_s"):
            allowed = round(base_p95 * REGRESSION_TOLERANCE, 4)
            checks.append((
                "P95 相对基线无回退",
                e2e["p95_s"] <= allowed,
                f"本次 {e2e['p95_s']}s / 基线 {base_p95}s / 允许 <= {allowed}s",
            ))
        base_biz = baseline.get("summary", {}).get("overall", {}).get("business_success_rate")
        if base_biz is not None and biz_rate is not None:
            checks.append((
                "业务成功率相对基线无回退",
                biz_rate >= base_biz,
                f"本次 {biz_rate} / 基线 {base_biz}",
            ))
    return checks


def _metrics_checks(metrics: Dict[str, Any]) -> List[Tuple[str, bool, str]]:
    retrieval = metrics.get("retrieval", {}) or {}
    requests = retrieval.get("requests", 0)
    checks = [
        ("/api/metrics 可访问", metrics.get("status") == "ok", f"status={metrics.get('status')}"),
        ("检索指标已采集", requests > 0, f"requests={requests}"),
    ]
    degraded = retrieval.get("degraded_requests", 0)
    checks.append((
        "缓存/降级可观测",
        retrieval.get("cache_hit", 0) + degraded > 0,
        f"cache_hit={retrieval.get('cache_hit', 0)}, degraded={degraded}",
    ))
    return checks


def _print(checks: List[Tuple[str, bool, str]]) -> bool:
    print("\n| 门禁项 | 结果 | 观测值 |")
    print("| --- | --- | --- |")
    for name, ok, detail in checks:
        print(f"| {name} | {'PASS' if ok else 'FAIL'} | {detail} |")
    return all(ok for _, ok, _ in checks)


def main():
    parser = argparse.ArgumentParser(description="性能门禁")
    parser.add_argument("--input", required=True, help="performance_test 输出的 JSON")
    parser.add_argument("--baseline", default="", help="基线 JSON，用于回退校验")
    parser.add_argument("--metrics-url", default="", help="运行中服务的 /api/metrics 地址")
    parser.add_argument("--timeout", type=float, default=10)
    parser.add_argument("--p50", type=float, default=DEFAULT_LIMITS["p50_s"])
    parser.add_argument("--p95", type=float, default=DEFAULT_LIMITS["p95_s"])
    parser.add_argument("--p99", type=float, default=DEFAULT_LIMITS["p99_s"])
    parser.add_argument("--business-success", type=float, default=DEFAULT_LIMITS["business_success_rate"])
    parser.add_argument("--rss-slope", type=float, default=DEFAULT_LIMITS["rss_slope_kib_per_min"])
    args = parser.parse_args()

    limits = {
        "http_success_rate": DEFAULT_LIMITS["http_success_rate"],
        "business_success_rate": args.business_success,
        "p50_s": args.p50,
        "p95_s": args.p95,
        "p99_s": args.p99,
        "rss_slope_kib_per_min": args.rss_slope,
    }

    result = _load(Path(args.input))
    baseline = _load(Path(args.baseline)) if args.baseline else None
    checks = _checks(result, limits, baseline)

    if args.metrics_url:
        try:
            checks.extend(_metrics_checks(_fetch_metrics(args.metrics_url, args.timeout)))
        except Exception as exc:
            checks.append(("/api/metrics 可访问", False, f"{type(exc).__name__}: {exc}"))

    passed = _print(checks)
    print(f"\n门禁结论：{'PASS' if passed else 'FAIL'}")
    sys.exit(0 if passed else 1)


if __name__ == "__main__":
    main()
