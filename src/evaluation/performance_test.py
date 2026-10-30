"""物流售后系统黑盒性能测试与资源采集。

运行（需 Python 包 httpx、psutil）：
    python -m src.evaluation.performance_test --mode core --repeat 20 --concurrency 2
    python -m src.evaluation.performance_test --mode load --requests 40 --concurrency 10
    python -m src.evaluation.performance_test --mode soak --duration 1800 --rate 0.1

指标口径：
- e2e_s：客户端发出请求到响应体结束的端到端耗时。
- ttfb_s：收到响应头的耗时；first_token_s：首个 SSE token 的耗时。
- HTTP 成功：无传输异常且状态码为 2xx。
- 业务成功：HTTP 成功，且响应结构、业务状态、转人工标志符合用例预期，未出现降级文案。
- P50/P95/P99 使用最近秩法；样本较少时 P99 等于最大值，仅作探索性参考。
- CPU/RSS/线程/句柄为被测进程及子进程；磁盘和网卡吞吐另采集整机口径，不能解释为应用独占。
"""

import argparse
import asyncio
import importlib.metadata
import json
import math
import platform
import statistics
import time
import uuid
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import httpx
import psutil


CHAT_CASES = {
    "faq": {
        "input": "客服工作时间是什么",
        "expected_status": "ai_solved",
        "expected_source": "faq",
        "expected_human": False,
    },
    "logistics": {
        "input": "订单 DD20260901001 现在到哪里了？",
        "expected_status": "ai_solved",
        "expected_human": False,
    },
    "refund_high": {
        "input": "订单 DD20260910003 商品不合适，我要退款 680 元",
        "expected_status": "pending_human",
        "expected_human": True,
    },
    "pending_info": {
        "input": "我要退款",
        "expected_status": "pending_info",
        "expected_human": False,
    },
    "multiturn_refund": {
        "input": "订单号是 DD20260910004，我要退款",
        "expected_status": "ai_solved",
        "expected_human": False,
    },
}
LOAD_MIX = ["faq", "logistics", "refund_high", "pending_info"]
SOAK_MIX = ["faq", "logistics", "faq", "refund_high", "pending_info"]
DEGRADED_TEXTS = ("服务异常，请稍后重试", "抱歉，生成回复失败，请稍后重试")


def now_iso():
    return datetime.now().astimezone().isoformat()


def percentile(values, quantile):
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(0, math.ceil(len(ordered) * quantile) - 1)]


def latency_stats(values):
    if not values:
        return {"n": 0}
    return {
        "n": len(values),
        "mean_s": round(statistics.fmean(values), 4),
        "p50_s": round(percentile(values, 0.50), 4),
        "p95_s": round(percentile(values, 0.95), 4),
        "p99_s": round(percentile(values, 0.99), 4),
        "min_s": round(min(values), 4),
        "max_s": round(max(values), 4),
    }


def summarize_requests(rows, elapsed_s):
    def rates(items):
        n = len(items)
        http_ok = sum(bool(r["http_success"]) for r in items)
        business_ok = sum(bool(r["business_success"]) for r in items)
        return {
            "requests": n,
            "http_success_rate": round(http_ok / n, 6) if n else None,
            "business_success_rate": round(business_ok / n, 6) if n else None,
            "http_error_rate": round((n - http_ok) / n, 6) if n else None,
            "business_error_rate": round((n - business_ok) / n, 6) if n else None,
            "throughput_rps": round(n / elapsed_s, 4) if elapsed_s > 0 else None,
            "e2e": latency_stats([r["e2e_s"] for r in items]),
            "ttfb": latency_stats([r["ttfb_s"] for r in items if r.get("ttfb_s") is not None]),
            "first_token": latency_stats([r["first_token_s"] for r in items if r.get("first_token_s") is not None]),
            "server_elapsed": latency_stats([r["server_elapsed_s"] for r in items if r.get("server_elapsed_s") is not None]),
        }

    by_scenario = defaultdict(list)
    for row in rows:
        by_scenario[row["scenario"]].append(row)
    stage_values = defaultdict(list)
    final_llm_ttft = []
    final_stream = []
    for row in rows:
        for node, duration in (row.get("stage_durations") or {}).items():
            stage_values[node].append(duration)
        if row.get("final_llm_ttft_s") is not None:
            final_llm_ttft.append(row["final_llm_ttft_s"])
        if row.get("final_stream_and_finalize_s") is not None:
            final_stream.append(row["final_stream_and_finalize_s"])
    return {
        "overall": rates(rows),
        "by_scenario": {name: rates(items) for name, items in sorted(by_scenario.items())},
        "stage_latency": {name: latency_stats(items) for name, items in sorted(stage_values.items())},
        "final_llm_ttft": latency_stats(final_llm_ttft),
        "final_stream_and_finalize": latency_stats(final_stream),
    }


class ResourceMonitor:
    def __init__(self, pid, interval=1.0):
        self.target = psutil.Process(pid)
        self.interval = interval
        self.samples = []
        self.processes = {}
        self.previous_process_io = None
        self.previous_disk = None
        self.previous_net = None
        self.stop_event = asyncio.Event()

    def _target_processes(self):
        found = [self.target] + self.target.children(recursive=True)
        current = {}
        for proc in found:
            if proc.pid not in self.processes:
                proc.cpu_percent(None)
            current[proc.pid] = proc
        self.processes = current
        return list(current.values())

    def sample(self):
        timestamp = time.time()
        processes = self._target_processes()
        cpu = 0.0
        rss = 0
        threads = 0
        handles = 0
        read_bytes = 0
        write_bytes = 0
        alive = 0
        for proc in processes:
            try:
                with proc.oneshot():
                    cpu += proc.cpu_percent(None)
                    rss += proc.memory_info().rss
                    threads += proc.num_threads()
                    if hasattr(proc, "num_handles"):
                        handles += proc.num_handles()
                    io = proc.io_counters()
                    read_bytes += io.read_bytes
                    write_bytes += io.write_bytes
                alive += 1
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue

        disk = psutil.disk_io_counters()
        net = psutil.net_io_counters()
        process_read_bps = process_write_bps = None
        disk_read_bps = disk_write_bps = None
        net_sent_bps = net_recv_bps = None
        if self.previous_process_io:
            dt, prev_read, prev_write = self.previous_process_io
            process_read_bps = max(0, read_bytes - prev_read) / max(timestamp - dt, 1e-6)
            process_write_bps = max(0, write_bytes - prev_write) / max(timestamp - dt, 1e-6)
        if self.previous_disk and disk:
            dt, prev_read, prev_write = self.previous_disk
            disk_read_bps = max(0, disk.read_bytes - prev_read) / max(timestamp - dt, 1e-6)
            disk_write_bps = max(0, disk.write_bytes - prev_write) / max(timestamp - dt, 1e-6)
        if self.previous_net:
            dt, prev_sent, prev_recv = self.previous_net
            net_sent_bps = max(0, net.bytes_sent - prev_sent) / max(timestamp - dt, 1e-6)
            net_recv_bps = max(0, net.bytes_recv - prev_recv) / max(timestamp - dt, 1e-6)
        self.previous_process_io = (timestamp, read_bytes, write_bytes)
        if disk:
            self.previous_disk = (timestamp, disk.read_bytes, disk.write_bytes)
        self.previous_net = (timestamp, net.bytes_sent, net.bytes_recv)

        virtual = psutil.virtual_memory()
        self.samples.append({
            "timestamp": timestamp,
            "time": now_iso(),
            "alive_processes": alive,
            "process_cpu_percent": round(cpu, 3),
            "system_cpu_percent": psutil.cpu_percent(None),
            "process_rss_bytes": rss,
            "system_memory_percent": virtual.percent,
            "threads": threads,
            "handles": handles,
            "process_read_bps": process_read_bps,
            "process_write_bps": process_write_bps,
            "system_disk_read_bps": disk_read_bps,
            "system_disk_write_bps": disk_write_bps,
            "system_net_sent_bps": net_sent_bps,
            "system_net_recv_bps": net_recv_bps,
        })

    async def run(self):
        psutil.cpu_percent(None)
        while not self.stop_event.is_set():
            self.sample()
            try:
                await asyncio.wait_for(self.stop_event.wait(), timeout=self.interval)
            except asyncio.TimeoutError:
                pass

    async def stop(self):
        self.stop_event.set()
        await asyncio.sleep(0)
        self.sample()

    def summary(self):
        if not self.samples:
            return {}

        def values(key):
            return [s[key] for s in self.samples if s.get(key) is not None]

        def basic(key):
            data = values(key)
            if not data:
                return {}
            return {
                "mean": round(statistics.fmean(data), 4),
                "p95": round(percentile(data, 0.95), 4),
                "max": round(max(data), 4),
            }

        rss = values("process_rss_bytes")
        times = [s["timestamp"] for s in self.samples if s.get("process_rss_bytes") is not None]
        slope_kib_min = None
        if len(rss) >= 2 and times[-1] > times[0]:
            x_mean = statistics.fmean(times)
            y_mean = statistics.fmean(rss)
            denominator = sum((x - x_mean) ** 2 for x in times)
            if denominator:
                slope = sum((x - x_mean) * (y - y_mean) for x, y in zip(times, rss)) / denominator
                slope_kib_min = round(slope * 60 / 1024, 4)
        return {
            "samples": len(self.samples),
            "process_cpu_percent": basic("process_cpu_percent"),
            "system_cpu_percent": basic("system_cpu_percent"),
            "process_rss_bytes": {
                "start": rss[0] if rss else None,
                "end": rss[-1] if rss else None,
                "min": min(rss) if rss else None,
                "max": max(rss) if rss else None,
                "mean": round(statistics.fmean(rss)) if rss else None,
                "linear_slope_kib_per_min": slope_kib_min,
            },
            "system_memory_percent": basic("system_memory_percent"),
            "threads": basic("threads"),
            "handles": basic("handles"),
            "process_read_bps": basic("process_read_bps"),
            "process_write_bps": basic("process_write_bps"),
            "system_disk_read_bps": basic("system_disk_read_bps"),
            "system_disk_write_bps": basic("system_disk_write_bps"),
            "system_net_sent_bps": basic("system_net_sent_bps"),
            "system_net_recv_bps": basic("system_net_recv_bps"),
        }


class Benchmark:
    def __init__(self, args, run_id):
        self.args = args
        self.run_id = run_id
        self.base_url = args.base_url.rstrip("/")
        limits = httpx.Limits(
            max_connections=max(20, args.concurrency * 2),
            max_keepalive_connections=max(20, args.concurrency * 2),
        )
        timeout = httpx.Timeout(args.timeout, connect=min(15, args.timeout))
        self.client = httpx.AsyncClient(limits=limits, timeout=timeout)

    async def close(self):
        await self.client.aclose()

    def base_row(self, scenario, index, started, started_iso):
        return {
            "run_id": self.run_id,
            "scenario": scenario,
            "request_index": index,
            "started_at": started_iso,
            "e2e_s": round(time.perf_counter() - started, 4),
            "ttfb_s": None,
            "first_token_s": None,
            "server_elapsed_s": None,
            "http_status": None,
            "http_success": False,
            "business_success": False,
            "done_status": None,
            "source": None,
            "ticket_id": None,
            "need_human": None,
            "tools": [],
            "stage_events": [],
            "stage_durations": {},
            "final_llm_ttft_s": None,
            "final_stream_and_finalize_s": None,
            "answer_chars": 0,
            "error": None,
        }

    async def chat(self, scenario, index, session_id=None, user_input=None):
        case = CHAT_CASES[scenario]
        started = time.perf_counter()
        started_iso = now_iso()
        row = self.base_row(scenario, index, started, started_iso)
        events = []
        answer_parts = []
        payload = {
            "session_id": session_id or f"perf-{self.run_id}-{scenario}-{index}-{uuid.uuid4().hex[:8]}",
            "user_input": user_input or case["input"],
        }
        try:
            async with self.client.stream("POST", f"{self.base_url}/api/chat", json=payload) as response:
                row["http_status"] = response.status_code
                row["ttfb_s"] = round(time.perf_counter() - started, 4)
                row["http_success"] = 200 <= response.status_code < 300
                async for line in response.aiter_lines():
                    if not line.startswith("data: "):
                        continue
                    try:
                        event = json.loads(line[6:])
                    except json.JSONDecodeError:
                        continue
                    events.append(event)
                    if event.get("type") == "stage":
                        row["stage_events"].append({
                            "node": event.get("node"),
                            "offset_s": round(time.perf_counter() - started, 4),
                            "detail": event.get("detail", ""),
                        })
                    if event.get("type") == "token":
                        if row["first_token_s"] is None:
                            row["first_token_s"] = round(time.perf_counter() - started, 4)
                        answer_parts.append(event.get("content") or "")
        except Exception as exc:
            row["error"] = f"{type(exc).__name__}: {exc}"

        row["e2e_s"] = round(time.perf_counter() - started, 4)
        done = next((e for e in reversed(events) if e.get("type") == "done"), {})
        answer = "".join(answer_parts)
        row.update({
            "server_elapsed_s": done.get("elapsed"),
            "done_status": done.get("status"),
            "source": done.get("source"),
            "ticket_id": done.get("ticket_id"),
            "need_human": done.get("need_human"),
            "tools": [
                e.get("detail", "") for e in events
                if e.get("type") == "stage" and e.get("node") == "tool_agent"
            ],
            "answer_chars": len(answer),
        })
        previous_offset = 0.0
        for stage in row["stage_events"]:
            row["stage_durations"][stage["node"]] = round(max(0.0, stage["offset_s"] - previous_offset), 4)
            previous_offset = stage["offset_s"]
        if row["stage_events"]:
            row["stage_durations"]["post_graph_or_faq_finalize"] = round(
                max(0.0, row["e2e_s"] - previous_offset), 4
            )
        prepare_offset = next(
            (s["offset_s"] for s in row["stage_events"] if s["node"] == "prepare_reply"), None
        )
        if prepare_offset is not None and row["first_token_s"] is not None:
            row["final_llm_ttft_s"] = round(max(0.0, row["first_token_s"] - prepare_offset), 4)
            row["final_stream_and_finalize_s"] = round(max(0.0, row["e2e_s"] - row["first_token_s"]), 4)
        degraded = any(text in answer for text in DEGRADED_TEXTS)
        expected_ok = (
            bool(done)
            and done.get("status") == case["expected_status"]
            and bool(done.get("need_human")) == case["expected_human"]
            and done.get("status") != "error"
            and not degraded
        )
        if case.get("expected_source") is not None:
            expected_ok = expected_ok and done.get("source") == case["expected_source"]
        row["business_success"] = bool(row["http_success"] and expected_ok)
        if row["http_success"] and not row["business_success"] and not row["error"]:
            row["error"] = "业务响应未通过预期校验或出现降级文案"
        return row

    async def api(self, scenario, index, method, path, expected_status=None, payload=None):
        started = time.perf_counter()
        row = self.base_row(scenario, index, started, now_iso())
        data = None
        try:
            async with self.client.stream(method, f"{self.base_url}{path}", json=payload) as response:
                row["http_status"] = response.status_code
                row["ttfb_s"] = round(time.perf_counter() - started, 4)
                row["http_success"] = 200 <= response.status_code < 300
                data = json.loads((await response.aread()).decode("utf-8"))
            if scenario == "health":
                valid = data.get("status") == "ok" and data.get("nodes") == 7
            else:
                ticket = data.get("ticket") or {}
                valid = data.get("code") == 200 and bool(ticket.get("ticket_id"))
                if expected_status:
                    valid = valid and ticket.get("status") == expected_status
                row["ticket_id"] = ticket.get("ticket_id")
                row["done_status"] = ticket.get("status")
            row["business_success"] = bool(row["http_success"] and valid)
        except Exception as exc:
            row["error"] = f"{type(exc).__name__}: {exc}"
        row["e2e_s"] = round(time.perf_counter() - started, 4)
        if row["http_success"] and not row["business_success"] and not row["error"]:
            row["error"] = "接口响应未通过预期校验"
        return row

    async def core_cycle(self, cycle, semaphore):
        async with semaphore:
            rows = []
            session_prefix = f"perf-{self.run_id}-core-{cycle}-{uuid.uuid4().hex[:8]}"
            rows.append(await self.chat("faq", cycle))
            rows.append(await self.chat("logistics", cycle))
            high_refund = await self.chat("refund_high", cycle)
            rows.append(high_refund)

            multi_session = f"{session_prefix}-multi"
            rows.append(await self.chat("pending_info", cycle, session_id=multi_session))
            rows.append(await self.chat("multiturn_refund", cycle, session_id=multi_session))
            rows.append(await self.api("health", cycle, "GET", "/api/health"))

            ticket_id = high_refund.get("ticket_id")
            if ticket_id:
                rows.append(await self.api("ticket_detail", cycle, "GET", f"/api/tickets/{ticket_id}"))
                rows.append(await self.api(
                    "ticket_review", cycle, "POST", f"/api/tickets/{ticket_id}/review",
                    expected_status="human_approved",
                    payload={"approved": True, "comment": "性能测试自动复核"},
                ))
            else:
                for scenario in ("ticket_detail", "ticket_review"):
                    started = time.perf_counter()
                    row = self.base_row(scenario, cycle, started, now_iso())
                    row["error"] = "上游退款请求未返回工单号，链路未执行"
                    rows.append(row)
            return rows

    async def run_core(self):
        semaphore = asyncio.Semaphore(self.args.concurrency)
        nested = await asyncio.gather(*(self.core_cycle(i, semaphore) for i in range(self.args.repeat)))
        return [row for rows in nested for row in rows]

    async def run_load(self):
        semaphore = asyncio.Semaphore(self.args.concurrency)

        async def one(index):
            async with semaphore:
                return await self.chat(LOAD_MIX[index % len(LOAD_MIX)], index)

        return await asyncio.gather(*(one(i) for i in range(self.args.requests)))

    async def run_soak(self):
        semaphore = asyncio.Semaphore(self.args.concurrency)
        total = int(self.args.duration * self.args.rate)
        if total <= 0:
            raise ValueError("duration * rate 必须至少为 1")

        async def one(index):
            async with semaphore:
                return await self.chat(SOAK_MIX[index % len(SOAK_MIX)], index)

        started = time.perf_counter()
        tasks = []
        for index in range(total):
            target = started + index / self.args.rate
            delay = target - time.perf_counter()
            if delay > 0:
                await asyncio.sleep(delay)
            tasks.append(asyncio.create_task(one(index)))
        return await asyncio.gather(*tasks)


def find_listener_pid(port):
    for conn in psutil.net_connections(kind="tcp"):
        if conn.status == "LISTEN" and conn.laddr and conn.laddr.port == port and conn.pid:
            return conn.pid
    raise RuntimeError(f"未找到监听端口 {port} 的进程，请通过 --pid 显式指定")


def environment_metadata(pid):
    packages = {}
    for name in ("httpx", "psutil"):
        try:
            packages[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            packages[name] = None
    proc = psutil.Process(pid)
    return {
        "python": platform.python_version(),
        "platform": platform.platform(),
        "machine": platform.machine(),
        "cpu_logical": psutil.cpu_count(),
        "cpu_physical": psutil.cpu_count(logical=False),
        "memory_total_bytes": psutil.virtual_memory().total,
        "benchmark_packages": packages,
        "server_process": {
            "pid": pid,
            "ppid": proc.ppid(),
            "cmdline": proc.cmdline(),
            "cwd": proc.cwd(),
            "create_time": datetime.fromtimestamp(proc.create_time(), timezone.utc).astimezone().isoformat(),
        },
    }


async def async_main(args):
    run_id = f"{args.mode}-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
    pid = args.pid or find_listener_pid(args.port)
    benchmark = Benchmark(args, run_id)
    monitor = ResourceMonitor(pid, args.sample_interval)
    monitor_task = asyncio.create_task(monitor.run())
    started_at = now_iso()
    started = time.perf_counter()
    rows = []
    run_error = None
    try:
        if args.mode == "core":
            rows = await benchmark.run_core()
        elif args.mode == "load":
            rows = await benchmark.run_load()
        else:
            rows = await benchmark.run_soak()
    except Exception as exc:
        run_error = f"{type(exc).__name__}: {exc}"
    finally:
        elapsed = time.perf_counter() - started
        await benchmark.close()
        await monitor.stop()
        monitor_task.cancel()
        try:
            await monitor_task
        except asyncio.CancelledError:
            pass

    config = vars(args).copy()
    result = {
        "run_id": run_id,
        "started_at": started_at,
        "ended_at": now_iso(),
        "elapsed_s": round(elapsed, 4),
        "config": config,
        "environment": environment_metadata(pid),
        "run_error": run_error,
        "summary": summarize_requests(rows, elapsed),
        "resource_summary": monitor.summary(),
        "requests": rows,
        "resource_samples": monitor.samples,
    }
    output = Path(args.output) if args.output else Path("evaluation_results") / f"{run_id}.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({
        "run_id": run_id,
        "output": str(output.resolve()),
        "elapsed_s": result["elapsed_s"],
        "run_error": run_error,
        "summary": result["summary"],
        "resource_summary": result["resource_summary"],
    }, ensure_ascii=False, indent=2))
    if run_error:
        raise RuntimeError(run_error)


def main():
    parser = argparse.ArgumentParser(description="物流售后系统黑盒性能测试")
    parser.add_argument("--mode", choices=("core", "load", "soak"), required=True)
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--pid", type=int, default=0)
    parser.add_argument("--repeat", type=int, default=20, help="core 模式业务链路重复次数")
    parser.add_argument("--requests", type=int, default=40, help="load 模式总请求数")
    parser.add_argument("--concurrency", type=int, default=2)
    parser.add_argument("--duration", type=float, default=1800, help="soak 模式投放时长（秒）")
    parser.add_argument("--rate", type=float, default=0.1, help="soak 模式固定投放速率（请求/秒）")
    parser.add_argument("--timeout", type=float, default=180)
    parser.add_argument("--sample-interval", type=float, default=1.0)
    parser.add_argument("--output", default="")
    args = parser.parse_args()
    if args.concurrency <= 0 or args.timeout <= 0 or args.sample_interval <= 0:
        parser.error("concurrency、timeout、sample-interval 必须大于 0")
    if args.mode == "core" and args.repeat <= 0:
        parser.error("repeat 必须大于 0")
    if args.mode == "load" and args.requests <= 0:
        parser.error("requests 必须大于 0")
    if args.mode == "soak" and (args.duration <= 0 or args.rate <= 0):
        parser.error("duration 和 rate 必须大于 0")
    asyncio.run(async_main(args))


if __name__ == "__main__":
    main()
