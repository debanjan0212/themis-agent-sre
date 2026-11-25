"""Everything on one machine: `themis demo` to watch it, `themis bench` to measure it.

Both start the real API and evaluator in this process, run the agent world against them over HTTP, and inject
the same four faults. Time is compressed (THEMIS_WINDOW_SCALE) so an hour-long SLO window passes in 30 seconds.
"""

from __future__ import annotations

import asyncio
import json
import os
import platform
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx
import uvicorn
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest

from themis import __version__
from themis.api import create_app, create_key
from themis.db import insert_ignore, tenants
from themis.settings import Settings
from themis.world.sim import World

# fault, target, intensity: one experiment for each way an agent fleet goes wrong
PLAN = (
    ("model_rate_limit", "model:sim-large", 0.8),
    ("tool_wrong_result", "tool:kb.search", 0.6),
    ("context_poison", "agent:coder", 0.6),
    ("rogue_agent", "agent:deployer", 0.3),
)


class Lab:
    def __init__(self, db_url: str, port: int = 8600, scale: float = 120.0, **settings):
        base = dict(db_url=db_url, window_scale=scale, eval_interval=1.0, settle_seconds=1.5)
        self.url = f"http://127.0.0.1:{port}"
        self.s = Settings.from_env(**{**base, "external_url": self.url, **settings})
        self.app = create_app(self.s)
        self.engine = self.app.state.engine
        self.key = self.new_key("default", ["admin"])
        self.server = uvicorn.Server(uvicorn.Config(self.app, host="127.0.0.1", port=port, log_level="warning"))
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def new_key(self, tenant: str, scopes: list[str]) -> str:
        with self.engine.begin() as conn:
            insert_ignore(conn, tenants, {"id": tenant, "name": tenant, "config": {}})
            return create_key(conn, tenant, scopes, "lab", time.time())

    def start(self) -> Lab:
        self.thread.start()
        deadline = time.time() + 15
        while not self.server.started:
            if time.time() > deadline:
                raise RuntimeError("the API did not start; is the port in use?")
            time.sleep(0.05)
        return self

    def stop(self) -> None:
        self.server.should_exit = True
        self.thread.join(5)

    def http(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(base_url=self.url, headers={"Authorization": f"Bearer {self.key}"}, timeout=15)


async def run_plan(http: httpx.AsyncClient, duration: float, log=print) -> list[dict]:
    done = []
    for fault, target, intensity in PLAN:
        body = {"fault": fault, "target": target, "intensity": intensity, "duration_s": duration}
        started = (await http.post("/api/experiments", json=body)).raise_for_status().json()
        log(f"chaos   {fault} on {target} at {intensity:.0%} for {duration:.0f}s")
        while True:
            await asyncio.sleep(1)
            mine = next(x for x in (await http.get("/api/experiments")).json()["experiments"] if x["id"] == started["id"])
            if mine["status"] == "done":
                break
        done.append(mine)
        d = mine["result"]["detection"]
        log(f"        response: {d['response'] or 'none'}" + (f" after {d['seconds']}s" if d["detected"] else ""))
        recovered = await recover(http)
        log("        every agent is back within its budget" if recovered else "        not every agent recovered in time")
    return done


async def recover(http: httpx.AsyncClient, timeout: float = 120.0) -> bool:
    """What an operator does after an incident: put suspended agents back under supervision, then wait for calm."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        o = (await http.get("/api/overview")).json()
        for a in o["agents"]:
            if a["autonomy"] == "suspended":
                body = {"level": "supervised", "reason": "operator reinstated the agent after the fault was removed"}
                await http.post(f"/api/agents/{a['name']}/autonomy", json=body)
        if o["agents"] and all(a["autonomy"] == "autonomous" for a in o["agents"]) and not o["quarantine"]:
            return True
        await asyncio.sleep(1)
    return False


async def demo(port: int, users: int, scale: float, duration: float, chaos: bool, db: str) -> None:
    lab = Lab(f"sqlite:///{db}", port, scale).start()
    world = World(lab.url, lab.key)
    await world.apply_fleet()
    print(f"\nThemis is running. Open the dashboard:\n\n  {lab.url}/#key={lab.key}\n")
    print(f"{users} simulated users are sending work to 10 agents. Press Ctrl+C to stop.\n")
    running = asyncio.create_task(world.run(users, duration))
    try:
        if chaos:
            await asyncio.sleep(25)  # let a healthy baseline build first
            async with lab.http() as http:
                await run_plan(http, 40.0)
        await running
    finally:
        running.cancel()
        await world.stop()
        lab.stop()


# ---------------------------------------------------------------- bench


def _otlp_batch(n: int) -> bytes:
    """n child spans with fresh ids. They have a parent that never arrives, so the evaluator leaves them alone."""
    req = ExportTraceServiceRequest()
    scope = req.resource_spans.add().scope_spans.add()
    now = time.time_ns()
    for _ in range(n):
        span = scope.spans.add(
            trace_id=os.urandom(16), span_id=os.urandom(8), parent_span_id=os.urandom(8), name="chat sim-small"
        )
        span.start_time_unix_nano, span.end_time_unix_nano = now - 50_000_000, now
        for key, value in (
            ("gen_ai.operation.name", "chat"),
            ("gen_ai.agent.name", "bench"),
            ("gen_ai.request.model", "sim-small"),
        ):
            span.attributes.add(key=key).value.string_value = value
        span.attributes.add(key="gen_ai.usage.input_tokens").value.int_value = 900
    return req.SerializeToString()


def bench_ingest(lab: Lab, requests: int = 60, batch: int = 1000, clients: int = 4) -> dict:
    key = lab.new_key("bench", ["ingest"])  # its own tenant, so it cannot touch the world's numbers
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/x-protobuf"}
    bodies = [_otlp_batch(batch) for _ in range(requests)]

    def send(body: bytes) -> float:
        t0 = time.perf_counter()
        httpx.post(f"{lab.url}/v1/traces", content=body, headers=headers, timeout=60).raise_for_status()
        return time.perf_counter() - t0

    t0 = time.perf_counter()
    with ThreadPoolExecutor(clients) as pool:
        latencies = sorted(pool.map(send, bodies))
    elapsed = time.perf_counter() - t0
    return {
        "spans": requests * batch,
        "batch": batch,
        "clients": clients,
        "seconds": round(elapsed, 2),
        "spans_per_second": round(requests * batch / elapsed),
        "p50_ms": round(latencies[len(latencies) // 2] * 1000, 1),
        "p95_ms": round(latencies[int(len(latencies) * 0.95)] * 1000, 1),
    }


async def bench(out: Path, users: int, seed: int, duration: float, port: int, db_url: str) -> dict:
    lab = Lab(db_url, port, 120.0, rate_limit_per_sec=5000).start()
    world = World(lab.url, lab.key, seed=seed)
    await world.apply_fleet()
    total = 30 + len(PLAN) * (duration + 150)  # upper bound; the world is stopped when the plan is done
    running = asyncio.create_task(world.run(users, total))
    t0 = time.time()
    try:
        print("warm-up  30s of healthy traffic")
        await asyncio.sleep(30)
        async with lab.http() as http:
            baseline = (await http.get("/api/overview")).json()["vitals"]
            done = await run_plan(http, duration)
            rca = (await http.get("/api/rca")).json()
            final = (await http.get("/api/overview")).json()
    finally:
        running.cancel()
    await world.stop()
    print("ingest   60,000 spans from 4 clients")
    ingest = await asyncio.to_thread(bench_ingest, lab)
    lab.stop()
    data = {
        "version": __version__,
        "seed": seed,
        "users": users,
        "window_scale": 120,
        "windows_seconds": {"1h": lab.s.scaled(3600), "5m": lab.s.scaled(300)},
        "wall_seconds": round(time.time() - t0),
        "machine": f"{platform.machine()}, {os.cpu_count()} CPUs, Python {platform.python_version()}",
        "database": db_url.split(":")[0],
        "world": dict(world.stats),
        "baseline": baseline,
        "experiments": [{k: x[k] for k in ("fault", "target", "intensity", "duration_s", "result")} for x in done],
        "rca_last_window": rca,
        "final_autonomy": {a["name"]: a["autonomy"] for a in final["agents"]},
        "ingest": ingest,
    }
    out.mkdir(parents=True, exist_ok=True)
    (out / "results.json").write_text(json.dumps(data, indent=2) + "\n")
    (out / "RESULTS.md").write_text(render(data))
    return data


def render(d: dict) -> str:
    """docs/RESULTS.md, written from the measured numbers so the document cannot drift from the run."""
    w = d["world"]
    judged = sum(w.get(k, 0) for k in ("success", "failed", "blocked"))
    lines = [
        "# Results",
        "",
        "Every number on this page was measured by `themis bench` and written by it. Nothing here is typed by hand.",
        "Run it yourself: `make bench`. The same seed gives the same traffic; timings vary a little with the machine.",
        "",
        f"- Themis {d['version']}, {d['database']} database, {d['machine']}",
        f"- {d['users']} simulated users, seed {d['seed']}, {d['wall_seconds']}s wall time, {judged:,} journeys",
        f"- Time is compressed {d['window_scale']}x: the 1h / 5m burn-rate windows are "
        f"{d['windows_seconds']['1h']:g}s / {d['windows_seconds']['5m']:g}s here",
        f"- Healthy baseline: {d['baseline']['success']:.1%} success, p95 {d['baseline']['p95_ms']:.0f} ms, "
        f"{d['baseline']['per_min']:.0f} journeys/min",
        "",
        "The models and tools are simulated. The control plane is not: the spans, the SLO maths, the gate, the",
        "approvals, the quarantine and the root-cause analysis are the same code that runs in production.",
        "",
        "## Four faults, one after another",
        "",
        "| Fault | Target | How Themis responded | Detected in | Failed rate before → after response "
        "| Unsafe actions before → after |",
        "|---|---|---|---|---|---|",
    ]
    for x in d["experiments"]:
        r = x["result"]
        det, c = r["detection"], r["containment"]
        took = f"{det['seconds']}s" if det["detected"] else "not detected"
        b, a = c["before_response"]["failed_rate"], c["after_response"]["failed_rate"]
        rate = f"{b:.1%} → {a:.1%}" if b is not None and a is not None else "n/a"
        lines.append(
            f"| `{x['fault']}` at {x['intensity']:.0%} | `{x['target']}` | {det['response'] or 'none'} | {took} | {rate} | "
            f"{c['unsafe_actions_before']} → {c['unsafe_actions_after']} |"
        )
    lines += [
        "",
        "## Root cause accuracy",
        "",
        "The simulator knows the true cause of every failed journey. The root-cause code never sees that label;",
        "it is only used here to mark the answers.",
        "",
        "| Fault | Journeys the fault broke | Themis named the target | by evidence | by statistics "
        "| named something else | no answer |",
        "|---|---|---|---|---|---|---|",
    ]
    total = correct = 0
    for x in d["experiments"]:
        r = x["result"]["rca"]
        share = f" ({r['named_target'] / r['fault_caused']:.1%})" if r["fault_caused"] else ""
        lines.append(
            f"| `{x['fault']}` | {r['fault_caused']} | {r['named_target']}{share} | {r['by_evidence']} | {r['by_statistics']} | "
            f"{r['named_other']} | {r['unknown']} |"
        )
        total, correct = total + r["all_labelled"], correct + r["all_correct"]
    lines += [
        "",
        "Across every failed or blocked journey in the four experiments (injected faults and ordinary background",
        f"failures together): **{correct} of {total} named correctly ({correct / total:.1%})**." if total else "",
        "",
        "## Blast radius",
        "",
        "| Fault | Journeys during the fault | Failed | Safely blocked | Agents in failed journeys | Users affected |",
        "|---|---|---|---|---|---|",
    ]
    for x in d["experiments"]:
        r = x["result"]
        j, br = r["journeys"], r["blast_radius"]
        lines.append(f"| `{x['fault']}` | {j['total']} | {j['failed']} | {j['blocked']} | {len(br['agents'])} | {br['users']} |")
    i = d["ingest"]
    lines += [
        "",
        "## Ingest",
        "",
        f"{i['spans']:,} spans in OTLP/HTTP protobuf, {i['batch']} per request, {i['clients']} clients: "
        f"**{i['spans_per_second']:,} spans/s**, request p50 {i['p50_ms']} ms, p95 {i['p95_ms']} ms.",
        "One API process, with the load generator in the same process, so this is a floor and not a ceiling.",
        "",
        "## What these numbers do not show",
        "",
        "- Real models fail in messier ways than a simulator. Treat the accuracy as an upper bound until it is",
        "  measured on your own traffic.",
        "- Root cause by evidence works where a boundary check exists (a schema check, a gate decision, a",
        "  post-action verification). Where none exists Themis falls back to statistics, which need volume.",
        "- Detection time is mostly the SLO window, on purpose: it is the same 14.4x / 1h / 5m rule used for",
        "  services. Policy violations and quarantine do not wait for a window.",
        "",
    ]
    return "\n".join(lines)
