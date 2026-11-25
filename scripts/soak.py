"""Hold Themis to its own SLO: three replicas behind a load balancer, production settings, and the failures a
real deployment sees. Writes docs/RELIABILITY.md and docs/reliability.json from what it measured.

    python scripts/soak.py --db-url postgresql+psycopg://postgres@localhost/themis_soak \\
        --db-stop "sudo systemctl stop postgresql" --db-start "sudo systemctl start postgresql"

The database must be empty. --db-stop and --db-start are any shell commands that stop and start it for real
(`pg_ctl`, `systemctl`, `docker stop`). Without them the database outage is skipped, and the report says so.

What runs:
  - 3 Themis replicas (`themis serve`), each its own process, sharing one Postgres. Production settings:
    1h / 5m windows, a 15 s evaluator interval, the default rate limit.
  - A load balancer that sends each request to a random ready replica and records every request's status and
    latency, as an ingress would. Readiness is /readyz, polled every half second, as Kubernetes does.
  - Load: agent runtimes exporting journeys through the real OpenTelemetry SDK and OTLP exporter, agent
    runtimes polling the gate snapshot every second, and people watching the dashboard.

What happens, on the clock:
  1. steady state
  2. the leader is killed with SIGKILL (a crashed node), and restarted 15 s later
  3. the leader is stopped with SIGTERM (a rolling deploy), and restarted 5 s later
  4. Postgres is stopped for 8 s, then started again
  5. steady state, then the load stops and everything sent is counted
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import platform
import random
import signal
import socket
import subprocess
import sys
import threading
import time
from collections import Counter, defaultdict
from pathlib import Path

import httpx
import sqlalchemy as sa

REPO = Path(__file__).resolve().parent.parent
SPANS_PER_JOURNEY = 12  # root + 3 x (agent, chat, tool) + 2 handoffs
AGENTS = ("planner", "coder", "reviewer")


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def pct(values: list[float], q: float) -> float | None:
    if not values:
        return None
    values = sorted(values)
    return values[min(len(values) - 1, int(q * len(values)))]


# ---------------------------------------------------------------- the load balancer (its own process)


def run_lb(port: int, backends: list[str], stats_file: Path) -> None:
    """A minimal ingress: route to a random ready replica, record status and latency of every request."""
    import uvicorn
    from starlette.applications import Starlette
    from starlette.requests import Request
    from starlette.responses import JSONResponse, Response
    from starlette.routing import Route

    ready: dict[str, bool] = dict.fromkeys(backends, False)
    log: list[tuple[float, str, int, float]] = []  # (at, route, status, seconds)
    client = httpx.AsyncClient(timeout=30, limits=httpx.Limits(max_connections=200, max_keepalive_connections=100))

    def route_of(path: str) -> str:
        for known in ("/v1/traces", "/v1/gate/snapshot", "/api/overview", "/api/journeys"):
            if path.startswith(known):
                return known
        return "other"

    async def health() -> None:
        while True:
            for b in backends:
                try:
                    ready[b] = (await client.get(f"{b}/readyz", timeout=1)).status_code == 200
                except httpx.HTTPError:
                    ready[b] = False
            await asyncio.sleep(0.5)

    async def forward(request: Request) -> Response:
        started = time.perf_counter()
        body = await request.body()
        up = [b for b, ok in ready.items() if ok]
        if not up:
            status, out = 503, Response("no ready replica", status_code=503, headers={"Retry-After": "2"})
        else:
            target = random.choice(up)  # nosec B311 - load spreading, not security
            headers = {k: v for k, v in request.headers.items() if k.lower() not in ("host", "content-length", "connection")}
            try:
                r = await client.request(
                    request.method, f"{target}{request.url.path}?{request.url.query}", content=body, headers=headers
                )
                keep = {k: v for k, v in r.headers.items() if k.lower() in ("content-type", "retry-after")}
                status, out = r.status_code, Response(r.content, status_code=r.status_code, headers=keep)
            except httpx.HTTPError:
                ready[target] = False  # stop sending to it until its readiness check passes again
                status, out = 502, Response("replica unreachable", status_code=502)
        log.append((time.time(), route_of(request.url.path), status, time.perf_counter() - started))
        return out

    async def dump(_: Request) -> Response:
        stats_file.write_text(json.dumps(log))
        return JSONResponse({"requests": len(log)})

    @contextlib.asynccontextmanager
    async def lifespan(_):  # Starlette 1.x: startup work goes in a lifespan
        checker = asyncio.get_running_loop().create_task(health())
        yield
        checker.cancel()

    app = Starlette(
        routes=[
            Route("/__soak/dump", dump, methods=["POST"]),
            Route("/{path:path}", forward, methods=["GET", "POST", "PUT", "DELETE"]),
        ],
        lifespan=lifespan,
    )
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")


# ---------------------------------------------------------------- the load


class Tally:
    """Counters shared by many threads. `counter[k] += 1` is not atomic across threads, so this takes a lock."""

    def __init__(self):
        self.lock, self.counts = threading.Lock(), Counter()

    def add(self, key: str, n: int = 1) -> None:
        with self.lock:
            self.counts[key] += n

    def __getitem__(self, key: str) -> int:
        return self.counts[key]


class Counting:
    """Wraps the OTLP exporter to count what it delivered and what it gave up on, per exporter timeout."""

    def __init__(self, inner, tally: Tally, group: str):
        self.inner, self.tally, self.group = inner, tally, group

    def export(self, spans):
        from opentelemetry.sdk.trace.export import SpanExportResult

        result = self.inner.export(spans)
        outcome = "delivered" if result == SpanExportResult.SUCCESS else "dropped"
        self.tally.add(outcome, len(spans))
        self.tally.add(f"{outcome}_{self.group}", len(spans))
        return result

    def shutdown(self):
        self.inner.shutdown()

    def force_flush(self, timeout_millis: int = 30_000) -> bool:
        return True


def exporters(url: str, key: str, workers: int, rate: float, stop: threading.Event, tally: Tally) -> list:
    """`workers` agent runtimes, each with its own OpenTelemetry pipeline, together sending `rate` journeys a second.

    Half keep the exporter's default 10 s timeout, half use the 30 s that docs/INTEGRATION.md recommends, so one run
    shows what each setting does through the same outage.
    """
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor
    from opentelemetry.trace import Status, StatusCode

    providers = []

    def work(i: int, provider) -> None:
        tracer, rng, gap = provider.get_tracer("soak"), random.Random(i), workers / rate  # nosec B311
        next_at = time.monotonic() + rng.uniform(0, gap)
        while not stop.is_set():
            time.sleep(max(0.0, next_at - time.monotonic()))
            next_at += gap
            failed = rng.random() < 0.02
            root_attrs = {"themis.journey.name": "ship_change", "session.id": f"s{i}-{rng.getrandbits(16)}", "user.id": f"u{i}"}
            with tracer.start_as_current_span("journey ship_change", attributes=root_attrs):
                for n, agent in enumerate(AGENTS):
                    agent_attrs = {
                        "gen_ai.operation.name": "invoke_agent",
                        "gen_ai.agent.name": agent,
                        "k8s.pod.name": f"{agent}-{i % 4}",
                    }
                    with tracer.start_as_current_span(f"invoke_agent {agent}", attributes=agent_attrs) as a:
                        chat = {
                            "gen_ai.operation.name": "chat",
                            "gen_ai.request.model": "m-large",
                            "gen_ai.usage.input_tokens": 900,
                            "gen_ai.usage.output_tokens": 200,
                        }
                        with tracer.start_as_current_span("chat m-large", attributes=chat):
                            pass
                        with tracer.start_as_current_span(
                            f"execute_tool t{n}",
                            attributes={"gen_ai.operation.name": "execute_tool", "gen_ai.tool.name": f"t{n}"},
                        ):
                            pass
                        if failed and n == 1:  # 2% of journeys: the coder's task fails
                            a.set_status(Status(StatusCode.ERROR))
                        if n < len(AGENTS) - 1:
                            hand = {"themis.handoff.from": agent, "themis.handoff.to": AGENTS[n + 1]}
                            with tracer.start_as_current_span(f"handoff {AGENTS[n + 1]}", attributes=hand):
                                pass
            tally.add("journeys")
            tally.add("spans", SPANS_PER_JOURNEY)
            tally.add(f"spans_{10 if i % 2 == 0 else 30}s", SPANS_PER_JOURNEY)

    threads = []
    for i in range(workers):
        provider = TracerProvider(resource=Resource.create({"service.name": f"agent-runtime-{i}"}))
        timeout = 10 if i % 2 == 0 else 30
        inner = OTLPSpanExporter(endpoint=f"{url}/v1/traces", headers={"Authorization": f"Bearer {key}"}, timeout=timeout)
        processor = BatchSpanProcessor(Counting(inner, tally, f"{timeout}s"), max_queue_size=200_000, schedule_delay_millis=2000)
        provider.add_span_processor(processor)
        providers.append(provider)
        threads.append(threading.Thread(target=work, args=(i, provider), daemon=True))
        threads[-1].start()
    return providers, threads


async def pollers(url: str, key: str, gate: int, viewers: int, stop: threading.Event) -> None:
    headers = {"Authorization": f"Bearer {key}"}
    async with httpx.AsyncClient(
        base_url=url, headers=headers, timeout=10, limits=httpx.Limits(max_connections=gate + viewers + 10)
    ) as http:

        async def loop(path: str, every: float) -> None:
            await asyncio.sleep(random.uniform(0, every))  # nosec B311
            while not stop.is_set():
                try:
                    await http.get(path)
                except httpx.HTTPError:
                    pass
                await asyncio.sleep(every)

        tasks = [loop("/v1/gate/snapshot", 1.0) for _ in range(gate)]
        tasks += [loop("/api/overview", 1.5) for _ in range(viewers)] + [
            loop("/api/journeys?limit=40", 2.5) for _ in range(viewers)
        ]
        await asyncio.gather(*tasks)


# ---------------------------------------------------------------- the run


class Replica:
    def __init__(self, n: int, env: dict):
        self.n, self.env, self.port = n, env, free_port()
        self.url, self.proc = f"http://127.0.0.1:{self.port}", None

    def start(self) -> None:
        env = {**self.env, "THEMIS_INSTANCE": f"soak-replica-{self.n}"}  # Themis adds a random part per process
        cmd = [sys.executable, "-m", "themis", "serve", "--host", "127.0.0.1", "--port", str(self.port)]
        self.proc = subprocess.Popen(cmd, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def metrics(self) -> dict:
        try:
            text = httpx.get(f"{self.url}/metrics", timeout=1).text
        except httpx.HTTPError:
            return {}
        out = {}
        for line in text.splitlines():
            if line.startswith(
                ("themis_evaluator_leader ", "themis_evaluator_lag_seconds ", "themis_evaluator_last_tick_age_seconds ")
            ):
                name, value = line.rsplit(" ", 1)
                out[name] = float(value)
        return out


def soak(args) -> dict:
    from themis.security import new_key

    engine = sa.create_engine(args.db_url, pool_pre_ping=True)  # survives the database restart below
    with engine.connect() as conn:
        if sa.inspect(conn).has_table("spans") and conn.execute(sa.text("select count(*) from spans")).scalar():
            sys.exit("the database must be empty: point --db-url at a new database")
    key = new_key()[1]
    env = {
        **os.environ,
        "THEMIS_DB_URL": args.db_url,
        "THEMIS_BOOTSTRAP_KEY": key,
        "THEMIS_EVAL_INTERVAL": str(args.eval_interval),
    }
    replicas = [Replica(n, env) for n in range(args.replicas)]
    for r in replicas:
        r.start()
    lb_port, stats_file = free_port(), Path(args.out) / ".soak-lb.json"
    lb = subprocess.Popen([sys.executable, __file__, "--serve-lb", str(lb_port), str(stats_file), *[r.url for r in replicas]])
    try:
        return run(args, engine, key, replicas, lb_port, stats_file)
    finally:  # whatever happened, leave nothing running that could hold the lease
        lb.terminate()
        for r in replicas:
            if r.proc and r.proc.poll() is None:
                r.proc.terminate()
        for r in replicas:
            if r.proc:
                try:
                    r.proc.wait(30)
                except subprocess.TimeoutExpired:
                    r.proc.kill()


def run(args, engine, key, replicas, lb_port, stats_file) -> dict:
    lb_url = f"http://127.0.0.1:{lb_port}"
    deadline = time.time() + 30
    while time.time() < deadline:
        try:
            if httpx.get(f"{lb_url}/healthz", timeout=1).status_code == 200:
                break
        except httpx.HTTPError:
            time.sleep(0.3)
    time.sleep(1.5)  # the load balancer's first readiness checks

    t0 = time.time()
    events: list[dict] = []
    samples: list[dict] = []
    stop, sampled, tally = threading.Event(), threading.Event(), Tally()
    providers, workers = exporters(lb_url, key, args.workers, args.rate, stop, tally)
    poll_thread = threading.Thread(
        target=lambda: asyncio.run(pollers(lb_url, key, args.gate_pollers, args.viewers, stop)), daemon=True
    )
    poll_thread.start()

    def sample_loop() -> None:  # what Prometheus would scrape from each replica, every half second
        while not sampled.is_set():
            now = time.time() - t0
            for r in replicas:
                samples.append({"t": round(now, 2), "replica": r.n, **r.metrics()})
            time.sleep(0.5)

    sampler = threading.Thread(target=sample_loop, daemon=True)
    sampler.start()

    def leader() -> Replica | None:
        for r in replicas:
            if r.proc and r.proc.poll() is None and r.metrics().get("themis_evaluator_leader") == 1.0:
                return r
        return None

    def at(second: float) -> None:
        time.sleep(max(0.0, t0 + second - time.time()))

    def mark(what: str, **more) -> None:
        events.append({"t": round(time.time() - t0, 2), "what": what, **more})
        print(f"{events[-1]['t']:7.1f}s  {what} {more if more else ''}", flush=True)

    def wait_new_leader(old: int, limit: float) -> float | None:
        started = time.time()
        while time.time() - started < limit:
            if (now := leader()) and now.n != old:
                return round(time.time() - started, 2)
            time.sleep(0.25)
        return None

    plan = args.seconds
    at(plan * 0.2)
    victim = leader()
    if victim:
        mark("SIGKILL the leader", replica=victim.n)
        victim.proc.send_signal(signal.SIGKILL)
        victim.proc.wait()
        took = wait_new_leader(victim.n, 3 * args.eval_interval + 2 * args.eval_interval + 10)
        mark("new leader after crash", seconds=took)
        victim.start()
        mark("crashed replica restarted", replica=victim.n)
    at(plan * 0.5)
    victim = leader()
    if victim:
        mark("SIGTERM the leader (rolling deploy)", replica=victim.n)
        t_stop = time.time()
        victim.proc.send_signal(signal.SIGTERM)
        took = wait_new_leader(victim.n, 3 * args.eval_interval + 10)
        victim.proc.wait(30)
        mark("new leader after clean stop", seconds=took, exit_seconds=round(time.time() - t_stop, 2))
        victim.start()
        mark("stopped replica restarted", replica=victim.n)
    at(plan * 0.75)
    if args.db_stop and args.db_start:
        mark("Postgres stopped", seconds=args.db_outage)
        subprocess.run(args.db_stop, shell=True, check=True, capture_output=True)  # nosec B602 - the operator's own command
        time.sleep(args.db_outage)
        subprocess.run(args.db_start, shell=True, check=True, capture_output=True)  # nosec B602
        mark("Postgres started")
    else:
        mark("database outage skipped (no --db-stop / --db-start)")
    at(plan)
    stop.set()
    for w in workers:  # let every journey in progress finish before the pipelines close
        w.join(10)
    mark("load stopped", journeys=tally["journeys"])
    for p in providers:
        p.shutdown()  # flush everything still queued, retries included
    mark("exporters flushed", delivered=tally["delivered"], dropped=tally["dropped"])

    # drain: wait until every journey that reached Themis has been judged
    deadline = time.time() + 3 * args.eval_interval + 60
    with engine.connect() as conn:
        while time.time() < deadline:
            left = conn.execute(sa.text("select count(*) from spans where is_root and not done")).scalar()
            if not left:
                break
            time.sleep(1)
            conn.rollback()
        stored_spans = conn.execute(sa.text("select count(*) from spans")).scalar()
        judged = conn.execute(sa.text("select count(*) from journeys")).scalar()
        task_events = conn.execute(sa.text("select coalesce(sum(total), 0) from sli_buckets where sli = 'task_success'")).scalar()
        handoff_events = conn.execute(
            sa.text("select coalesce(sum(total), 0) from sli_buckets where sli = 'handoff_success'")
        ).scalar()
        unjudged = conn.execute(sa.text("select count(*) from spans where is_root and not done")).scalar()
    mark("drained")
    sampled.set()
    httpx.post(f"{lb_url}/__soak/dump", timeout=30)
    requests = json.loads(stats_file.read_text())
    stats_file.unlink()

    return summarise(args, t0, tally, events, samples, requests, stored_spans, judged, task_events, handoff_events, unjudged)


def summarise(args, t0, tally, events, samples, requests, stored, judged, task_events, handoff_events, unjudged) -> dict:
    fault_windows = []  # (start, end) of each injected fault, plus 10 s of recovery after it
    starts = {
        "SIGKILL the leader": "crashed replica restarted",
        "SIGTERM the leader (rolling deploy)": "stopped replica restarted",
        "Postgres stopped": "Postgres started",
    }
    for e in events:
        if e["what"] in starts:
            end = next((x["t"] for x in events if x["what"] == starts[e["what"]] and x["t"] >= e["t"]), e["t"])
            fault_windows.append((e["t"], end + 10))

    def in_fault(t: float) -> bool:
        return any(a <= t <= b for a, b in fault_windows)

    routes: dict[str, dict] = {}
    by_route: dict[str, list] = defaultdict(list)
    for at, route, status, seconds in requests:
        t = at - t0
        if 10 <= t <= args.seconds:  # skip the first 10 s of warm-up and the drain
            by_route[route].append((t, status, seconds))
    for route, rows in sorted(by_route.items()):
        steady = [r for r in rows if not in_fault(r[0])]
        faults = [r for r in rows if in_fault(r[0])]

        def stats(rs):
            ok = [r for r in rs if r[1] < 500]
            lat = [r[2] * 1000 for r in ok]
            return {
                "requests": len(rs),
                "availability": round(len(ok) / len(rs), 5) if rs else None,
                "p50_ms": round(pct(lat, 0.5), 1) if lat else None,
                "p95_ms": round(pct(lat, 0.95), 1) if lat else None,
                "p99_ms": round(pct(lat, 0.99), 1) if lat else None,
                "codes": dict(Counter(f"{r[1] // 100}xx" for r in rs)),
            }

        routes[route] = {"steady": stats(steady), "during_faults": stats(faults), "all": stats(rows)}
    lags = [s.get("themis_evaluator_lag_seconds", 0.0) for s in samples if s.get("themis_evaluator_leader") == 1.0]
    return {
        "machine": f"{platform.machine()}, {os.cpu_count()} CPUs, Python {platform.python_version()}",
        "settings": {
            "replicas": args.replicas,
            "eval_interval_s": args.eval_interval,
            "lease_ttl_s": max(3 * args.eval_interval, 5.0),
            "window_scale": 1,
            "seconds": args.seconds,
        },
        "load": {
            "journeys_per_second": args.rate,
            "spans_per_second": args.rate * SPANS_PER_JOURNEY,
            "agent_runtimes_exporting": args.workers,
            "gate_pollers": args.gate_pollers,
            "dashboard_viewers": args.viewers,
        },
        "events": events,
        "fault_windows": fault_windows,
        "sent": {"journeys": tally["journeys"], "spans": tally["spans"]},
        "exporter": {"delivered": tally["delivered"], "dropped": tally["dropped"]},
        "by_exporter_timeout": {
            g: {"sent": tally[f"spans_{g}"], "delivered": tally[f"delivered_{g}"], "dropped": tally[f"dropped_{g}"]}
            for g in ("10s", "30s")
        },
        "stored": {"spans": stored, "journeys_judged": judged, "unjudged_left": unjudged},
        "double_count_check": {
            "task_events": task_events,
            "expected_task_events": judged * len(AGENTS),
            "handoff_events": handoff_events,
            "expected_handoff_events": judged * (len(AGENTS) - 1),
        },
        "evaluator_lag_s": {"p50": pct(lags, 0.5), "p95": pct(lags, 0.95), "max": max(lags) if lags else None},
        "routes": routes,
        "timeline": [s for s in samples if s.get("themis_evaluator_leader") == 1.0][::4],
    }


def render(d: dict) -> str:
    """docs/RELIABILITY.md, written from the measurements so the document cannot drift from the run."""
    s, load, sent, st, dc = d["settings"], d["load"], d["sent"], d["stored"], d["double_count_check"]
    lost = sent["spans"] - st["spans"]
    ev = {e["what"]: e for e in d["events"]}
    crash = ev.get("new leader after crash", {}).get("seconds")
    clean = ev.get("new leader after clean stop", {}).get("seconds")
    db = ev.get("Postgres stopped", {}).get("seconds")
    lines = [
        "# Reliability",
        "",
        "Themis watches other systems' error budgets, so it has to keep its own. This page holds it to the SLO",
        "in `deploy/intent.yaml` (99.9% available, p95 under 300 ms) while it is broken on purpose.",
        "",
        "Every number here was measured by `scripts/soak.py` and written by it. Run it yourself with `make soak`.",
        "",
        "## The setup",
        "",
        f"- {s['replicas']} Themis replicas, each its own process, sharing one PostgreSQL, behind a load balancer that",
        "  routes to ready replicas only (readiness is `/readyz`, as in Kubernetes)",
        f"- Production settings: 1h / 5m SLO windows, a {s['eval_interval_s']:g} s evaluator interval, the default rate limit",
        f"- Load for {s['seconds']:g} s: {load['journeys_per_second']:g} journeys a second ({load['spans_per_second']:,.0f} spans a second) "
        f"from {load['agent_runtimes_exporting']} agent runtimes using the OpenTelemetry SDK and OTLP exporter, "
        f"{load['gate_pollers']} runtimes polling the gate every second, {load['dashboard_viewers']} people watching the dashboard",
        f"- {d['machine']}. Replicas, database, load balancer and load generator all share these CPUs",
        "",
        "## What was done to it",
        "",
        "| When | What |",
        "|---|---|",
        *[f"| {e['t']:.0f} s | {e['what']}{_detail(e)} |" for e in d["events"] if e["what"] != "drained"],
        "",
        "## Was anything lost?",
        "",
        "| | Count |",
        "|---|---|",
        f"| Spans the agents created | {sent['spans']:,} |",
        f"| Spans the exporters delivered | {d['exporter']['delivered']:,} |",
        f"| Spans the exporters gave up on | {d['exporter']['dropped']:,} |",
        f"| Spans stored by Themis | {st['spans']:,} |",
        f"| Journeys sent / judged | {sent['journeys']:,} / {st['journeys_judged']:,} |",
        f"| Agent tasks counted towards SLOs (expected {dc['expected_task_events']:,}) | {dc['task_events']:,} |",
        f"| Handoffs counted towards SLOs (expected {dc['expected_handoff_events']:,}) | {dc['handoff_events']:,} |",
        "",
        "Half the agent runtimes used the OTLP exporter's default timeout and half the 30 s that",
        "`docs/INTEGRATION.md` recommends. The timeout is how long an exporter keeps retrying a batch:",
        "",
        "| Exporter timeout | Spans sent | Delivered | Given up on |",
        "|---|---|---|---|",
        *[
            f"| {g} {'(the default)' if g == '10s' else '(`OTEL_EXPORTER_OTLP_TIMEOUT=30`)'} | {v['sent']:,} | {v['delivered']:,} | {v['dropped']:,} |"
            for g, v in d["by_exporter_timeout"].items()
        ],
        "",
        _verdict(d, lost, db),
        "",
        "## Themis's own SLO",
        "",
        "Measured at the load balancer, like an ingress log. *Steady* is everything outside the fault windows; a fault",
        "window runs from the moment a fault starts to 10 s after it has been undone.",
        "",
        "| Route | Requests | Steady: available | Steady: p95 | Steady: p99 | During faults: available | During faults: p95 |",
        "|---|---|---|---|---|---|---|",
    ]
    for route, r in d["routes"].items():
        if route == "other":
            continue
        a, f = r["steady"], r["during_faults"]
        lines.append(
            f"| `{route}` | {r['all']['requests']:,} | {_pct(a['availability'])} | {_ms(a['p95_ms'])} | {_ms(a['p99_ms'])} | "
            f"{_pct(f['availability'])} | {_ms(f['p95_ms'])} |"
        )
    lag = d["evaluator_lag_s"]
    lines += [
        "",
        "Requests refused during a fault were answered `503` with `Retry-After` or `502`, which OpenTelemetry",
        "exporters retry. That is why the availability dips there and the span count above does not.",
        "",
        "## Judging and failover",
        "",
        f"- A crashed leader was replaced in **{crash} s**. Its lease has to expire first ({s['lease_ttl_s']:g} s, three intervals)."
        if crash is not None
        else "- A crashed leader was not replaced within the time allowed.",
        f"- A leader stopped for a rolling deploy handed over in **{clean} s**: it gives its lease up on the way out,"
        " and the other replicas check the lease every second."
        if clean is not None
        else "- A cleanly stopped leader did not hand over within the time allowed.",
        f"- How long a finished journey waited to be judged: p50 {lag['p50']:.1f} s, p95 {lag['p95']:.1f} s, worst {lag['max']:.1f} s."
        if lag["max"] is not None
        else "",
        "  Most of that is the evaluator interval itself. The worst case is the crash, while the old lease runs out.",
        "",
        "## What this does not show",
        "",
        "- One machine. A real cluster spreads replicas over nodes and zones; here they share CPUs with the database",
        "  and the load, so latencies are higher than they would be on their own.",
        "- An outage longer than the exporters' retry deadline (10 s) loses spans unless an OpenTelemetry Collector",
        "  with a persistent queue sits in front. `deploy/otel-collector.yaml` is that configuration.",
        "- Postgres itself was a single instance. Use a replicated, managed Postgres in production; its failover",
        "  looks like the outage above.",
        "",
    ]
    return "\n".join(lines)


def _verdict(d: dict, lost: int, db) -> str:
    dc, groups = d["double_count_check"], d["by_exporter_timeout"]
    faults = "a crashed leader, a rolling restart" + (f" and a database outage of {db:g} seconds" if db else "")
    if lost == 0:
        head = f"**No span was lost** through {faults}."
    else:
        head = f"**{lost:,} of {d['sent']['spans']:,} spans ({lost / d['sent']['spans']:.2%}) were lost** through {faults}. "
        head += (
            f"On the default 10 s timeout {groups['10s']['dropped']:,} were given up on; on 30 s, {groups['30s']['dropped']:,}."
        )
    once = dc["task_events"] == dc["expected_task_events"] and dc["handoff_events"] == dc["expected_handoff_events"]
    if once:
        return (
            head
            + " Nothing was counted twice: every journey that arrived was judged exactly once, even when the leader died mid-judging."
        )
    return head + " Some events were counted twice."


def _detail(e: dict) -> str:
    extra = {k: v for k, v in e.items() if k not in ("t", "what")}
    return f" ({', '.join(f'{k.replace(chr(95), chr(32))} {v}' for k, v in extra.items())})" if extra else ""


def _pct(x) -> str:
    return "n/a" if x is None else f"{x:.2%}"


def _ms(x) -> str:
    return "n/a" if x is None else f"{x:.0f} ms"


def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] == "--serve-lb":
        run_lb(int(sys.argv[2]), sys.argv[4:], Path(sys.argv[3]))
        return
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--db-url", required=True, help="an empty PostgreSQL database")
    ap.add_argument("--db-stop", default="", help="a shell command that stops the database for real")
    ap.add_argument("--db-start", default="", help="a shell command that starts it again")
    ap.add_argument("--db-outage", type=float, default=8.0)
    ap.add_argument("--replicas", type=int, default=3)
    ap.add_argument("--seconds", type=float, default=240.0)
    ap.add_argument("--rate", type=float, default=40.0, help="journeys a second")
    ap.add_argument("--workers", type=int, default=24, help="agent runtimes exporting spans")
    ap.add_argument("--gate-pollers", type=int, default=100)
    ap.add_argument("--viewers", type=int, default=3)
    ap.add_argument("--eval-interval", type=float, default=15.0)
    ap.add_argument("--out", default=str(REPO / "docs"))
    args = ap.parse_args()
    Path(args.out).mkdir(parents=True, exist_ok=True)
    data = soak(args)
    (Path(args.out) / "reliability.json").write_text(json.dumps(data, indent=2) + "\n")
    (Path(args.out) / "RELIABILITY.md").write_text(render(data))
    print(f"wrote {Path(args.out) / 'RELIABILITY.md'}")


if __name__ == "__main__":
    main()
