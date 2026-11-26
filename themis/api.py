"""HTTP API. Three groups of routes:

/v1/traces, /v1/gate/*, /v1/approvals   the data plane: what agents and their runtimes call   (scope: ingest)
/api/*                                   the dashboard and operators                             (scopes: read, operate, admin)
/healthz, /readyz, /metrics              for Kubernetes and Prometheus
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
import zlib
from collections import defaultdict
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

import sqlalchemy as sa
from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sqlalchemy import func, insert, select, update

from themis import __version__, chaos, otlp, slo
from themis import engine as ev
from themis.db import (
    agents,
    api_keys,
    approvals,
    audit,
    audit_log,
    autonomy_events,
    edge_buckets,
    experiments,
    gate_events,
    incidents,
    insert_ignore,
    journeys,
    make_engine,
    quarantine,
    spans,
    tenants,
)
from themis.security import SCOPES, Principal, RateLimiter, digest, key_id_of, new_key, same
from themis.settings import Settings

WEB = Path(__file__).parent / "web"
CSP = "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; frame-ancestors 'none'"
APPROVAL_TTL = 300.0  # real seconds a go / no-go request waits for a human before it expires
# Database trouble is temporary by nature. Answering 503 + Retry-After makes OTLP exporters and the gate client
# try again; a 500 would make an exporter drop the spans. The OpenTelemetry exporters give up 10 seconds after
# the first attempt, so "Retry-After: 2" lets their retries cover an outage of about 8 seconds. For longer
# outages, put an OpenTelemetry Collector with a persistent queue in front (deploy/otel-collector.yaml).
RETRY_AFTER = "2"
DB_UNAVAILABLE = (sa.exc.OperationalError, sa.exc.InterfaceError, sa.exc.TimeoutError, sa.exc.DisconnectionError)
LATENCY_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0)


class AutonomyBody(BaseModel):
    level: Literal["autonomous", "supervised", "suspended"]
    reason: str = Field(min_length=3, max_length=300)


class ExperimentBody(BaseModel):
    fault: str
    target: str = Field(max_length=200)
    intensity: float = 0.5
    duration_s: float = 30
    name: str = Field(default="", max_length=160)


class ApprovalRequest(BaseModel):
    agent: str = Field(max_length=120)
    action: str = Field(max_length=200)
    detail: dict = Field(default_factory=dict)


class Decision(BaseModel):
    approve: bool
    by: str = Field(default="operator", max_length=120)


class GateEvent(BaseModel):
    agent: str = Field(max_length=120)
    action: str = Field(max_length=200)
    decision: Literal["deny", "allow", "denied_by_human"]
    reason: str = Field(default="", max_length=300)


class GateEvents(BaseModel):
    events: list[GateEvent] = Field(max_length=500)


class KeyBody(BaseModel):
    scopes: list[Literal["ingest", "read", "operate", "admin"]] = Field(min_length=1)
    label: str = Field(default="", max_length=120)


class AgentSpec(BaseModel):
    model: str | None = None
    tools: list[str] = []
    handoffs: list[str] = []
    high_risk: list[str] = []
    cost_ceiling: float | None = Field(default=None, gt=0)
    slo: dict[str, float] = {}


class Fleet(BaseModel):
    """The declarative description of a fleet: who may use what, and each agent's targets. Keep it in Git."""

    agents: dict[str, AgentSpec]
    prices: dict[str, tuple[float, float]] = {}  # model -> (USD per 1M input tokens, USD per 1M output tokens)
    fallbacks: dict[str, str] = {}


def create_key(conn: sa.Connection, tenant_id: str, scopes: list[str], label: str, now: float) -> str:
    key_id, key, hashed = new_key()
    row = dict(id=key_id, tenant_id=tenant_id, hash=hashed, scopes=",".join(scopes), label=label, created_at=now)
    conn.execute(insert(api_keys).values(**row))
    return key


def bootstrap(engine: sa.Engine, s: Settings) -> None:
    with engine.begin() as conn:
        insert_ignore(conn, tenants, {"id": "default", "name": "default", "config": {}})
        if s.bootstrap_key and (key_id := key_id_of(s.bootstrap_key)):
            row = dict(id=key_id, tenant_id="default", hash=digest(s.bootstrap_key), scopes="admin", label="bootstrap")
            insert_ignore(conn, api_keys, {**row, "created_at": time.time()})


def create_app(settings: Settings | None = None, engine: sa.Engine | None = None) -> FastAPI:
    s = settings or Settings.from_env()
    engine = engine or make_engine(s.db_url)
    bootstrap(engine, s)
    evaluator = ev.Evaluator(engine, s)
    limiter = RateLimiter(s.rate_limit_per_sec)
    counters: dict[str, int] = defaultdict(int)
    cache: dict[tuple, tuple[float, object]] = {}

    def cached(key: tuple, ttl: float, build):
        hit = cache.get(key)
        if hit and time.time() - hit[0] < ttl:
            return hit[1]
        if len(cache) > 2000:
            cache.clear()
        value = build()
        cache[key] = (time.time(), value)
        return value

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        evaluator.start()
        yield
        await asyncio.to_thread(evaluator.shutdown, s.eval_interval + 5)  # hand the lease over before exiting

    # no built-in docs pages: they load scripts from a CDN, which the CSP forbids, and they are one more public route
    app = FastAPI(title="Themis", version=__version__, lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.engine, app.state.settings, app.state.evaluator = engine, s, evaluator
    if s.cors_origins:
        app.add_middleware(
            CORSMiddleware, allow_origins=list(s.cors_origins), allow_methods=["*"], allow_headers=["Authorization"]
        )

    for db_error in DB_UNAVAILABLE:

        @app.exception_handler(db_error)
        async def database_unavailable(request: Request, exc: Exception):
            body = {"detail": "database unavailable, try again"}
            return JSONResponse(body, status_code=503, headers={"Retry-After": RETRY_AFTER})

    # Themis's own SLIs, under the OpenTelemetry HTTP semantic-convention name (http.server.request.duration).
    # The SLO alerts IntentOps renders into deploy/kubernetes/slo-rules.yaml read exactly this metric.
    # Updated only from the middleware, which runs on the event loop, so no lock is needed.
    http_hist: dict[tuple[str, str, int], list[float]] = defaultdict(lambda: [0] * (len(LATENCY_BUCKETS) + 2))

    def observe(route: str, method: str, status: int, seconds: float) -> None:
        row = http_hist[(route, method, status)]
        for i, edge in enumerate(LATENCY_BUCKETS):
            if seconds <= edge:
                row[i] += 1
        row[-2] += 1
        row[-1] += seconds

    @app.middleware("http")
    async def hardening(request: Request, call_next):
        started = time.perf_counter()
        length = request.headers.get("content-length")
        if length is not None and (not length.isdigit() or int(length) > s.max_body_bytes):
            observe("rejected", request.method, 413, time.perf_counter() - started)
            return Response("request too large", status_code=413)
        # every other body is parsed by the framework, so it must announce its size; /v1/traces counts bytes itself
        if request.method in ("POST", "PUT") and length is None and request.url.path != "/v1/traces":
            observe("rejected", request.method, 411, time.perf_counter() - started)
            return Response("Content-Length required", status_code=411)
        try:
            response = await call_next(request)
        except Exception:
            observe(_route(request), request.method, 500, time.perf_counter() - started)
            raise
        observe(_route(request), request.method, response.status_code, time.perf_counter() - started)
        response.headers.update(
            {
                "X-Content-Type-Options": "nosniff",
                "X-Frame-Options": "DENY",
                "Referrer-Policy": "no-referrer",
                "Content-Security-Policy": CSP,
                "Cache-Control": "no-store",
            }
        )
        return response

    def need(scope: str):
        def dependency(authorization: str = Header("")) -> Principal:
            key = authorization.removeprefix("Bearer ").strip()
            key_id = key_id_of(key)

            def lookup():
                with engine.connect() as conn:
                    return conn.execute(select(api_keys).where(api_keys.c.id == key_id)).mappings().first()

            row = cached(("key", key_id), 5.0, lookup) if key_id else None
            if not row or row["revoked_at"] or not same(row["hash"], digest(key)):
                raise HTTPException(401, "missing or wrong API key")
            scopes = frozenset(row["scopes"].split(","))
            if scope not in scopes and "admin" not in scopes:
                raise HTTPException(403, f"this key does not have the '{scope}' scope")
            if not limiter.allow(key_id, time.time()):
                raise HTTPException(429, "rate limit reached", headers={"Retry-After": "1"})
            return Principal(row["tenant_id"], key_id, scopes)

        return dependency

    as_ingest, as_read, as_operate, as_admin = (Depends(need(scope)) for scope in SCOPES)

    # ------------------------------------------------------------ data plane

    @app.post("/v1/traces")
    async def traces(request: Request, p: Principal = as_ingest):
        size, chunks = 0, []
        async for chunk in request.stream():
            size += len(chunk)
            if size > s.max_body_bytes:
                raise HTTPException(413, "request too large")
            chunks.append(chunk)
        body = b"".join(chunks)
        if request.headers.get("content-encoding") == "gzip":
            unzip = zlib.decompressobj(wbits=31)
            try:
                body = unzip.decompress(body, s.max_body_bytes)
            except zlib.error as e:
                raise HTTPException(400, "Content-Encoding says gzip but the body is not") from e
            if unzip.unconsumed_tail:  # a small body that inflates past the limit is a zip bomb
                raise HTTPException(413, "request too large")
        try:
            rows, rejected = otlp.parse(body, request.headers.get("content-type", ""), time.time(), s.store_content)
        except otlp.BadPayload as e:
            raise HTTPException(400, str(e)) from e
        for row in rows:
            row["tenant_id"] = p.tenant_id
        with engine.begin() as conn:
            written = insert_ignore(conn, spans, rows)
        counters["spans"] += written
        counters["rejected"] += rejected
        # OTLP partial success: the exporter must not retry spans we refused on purpose
        partial = {"partialSuccess": {"rejectedSpans": rejected, "errorMessage": "bad ids or timestamps"}} if rejected else {}
        return Response(content=json.dumps(partial), media_type="application/json")

    @app.post("/v1/metrics")
    @app.post("/v1/logs")
    async def accepted_not_stored(p: Principal = as_ingest):
        """OTLP exporters send all three signals to one endpoint. Themis reads traces; the rest is acknowledged."""
        return {}

    @app.get("/v1/gate/snapshot")
    def gate_snapshot(p: Principal = as_ingest):
        def build():
            now = time.time()
            with engine.connect() as conn:
                cfg = conn.execute(select(tenants.c.config).where(tenants.c.id == p.tenant_id)).scalar() or {}
                rows = conn.execute(
                    select(agents.c.name, agents.c.autonomy, agents.c.config).where(agents.c.tenant_id == p.tenant_id)
                )
                fleet = {
                    name: {"autonomy": level, **{k: (c or {}).get(k, []) for k in ("tools", "handoffs", "high_risk")}}
                    for name, level, c in rows
                }
                q = quarantine.c
                held = conn.execute(select(q.component, q.until).where(q.tenant_id == p.tenant_id, q.active, q.until > now))
                x = experiments.c
                running = conn.execute(
                    select(x.id, x.fault, x.target, x.intensity).where(x.tenant_id == p.tenant_id, x.status == "running")
                )
                return {
                    "at": now,
                    "agents": fleet,
                    "quarantine": {c: until for c, until in held},
                    "fallbacks": cfg.get("fallbacks") or {},
                    "faults": [dict(r) for r in running.mappings()],
                }

        return cached(("snapshot", p.tenant_id), 0.5, build)

    @app.post("/v1/gate/events", status_code=202)
    def gate_report(body: GateEvents, p: Principal = as_ingest):
        now = time.time()
        with engine.begin() as conn:
            if body.events:
                conn.execute(insert(gate_events), [{"tenant_id": p.tenant_id, "at": now, **e.model_dump()} for e in body.events])
        return {"accepted": len(body.events)}

    @app.post("/v1/approvals", status_code=201)
    def approval_request(body: ApprovalRequest, p: Principal = as_ingest):
        row = dict(id=uuid.uuid4().hex, tenant_id=p.tenant_id, status="pending", requested_at=time.time(), **body.model_dump())
        with engine.begin() as conn:
            conn.execute(insert(approvals).values(**row))
        return {"id": row["id"], "status": "pending"}

    @app.get("/v1/approvals/{approval_id}")
    def approval_status(approval_id: str, p: Principal = as_ingest):
        a = approvals.c
        mine = sa.and_(a.id == approval_id, a.tenant_id == p.tenant_id)
        with engine.begin() as conn:
            row = conn.execute(select(a.status, a.requested_at).where(mine)).first()
            if not row:
                raise HTTPException(404, "no such approval")
            status = row[0]
            if status == "pending" and time.time() - row[1] > APPROVAL_TTL:  # nobody answered: the safe answer is no
                conn.execute(
                    update(approvals).where(mine, a.status == "pending").values(status="expired", decided_at=time.time())
                )
                status = "expired"
        return {"id": approval_id, "status": status}

    # ------------------------------------------------------------ dashboard: read

    @app.get("/api/overview")
    def overview(p: Principal = as_read):
        return cached(("overview", p.tenant_id), 1.0, lambda: _overview(engine, s, p.tenant_id, evaluator))

    @app.get("/api/journeys")
    def journey_list(outcome: str = "", agent: str = "", session: str = "", limit: int = 60, p: Principal = as_read):
        j = journeys.c
        where = [j.tenant_id == p.tenant_id]
        if outcome in ("success", "failed", "blocked"):
            where.append(j.outcome == outcome)
        if session:
            where.append(j.session_id == session)
        with engine.connect() as conn:
            rows = conn.execute(
                select(journeys).where(*where).order_by(j.end.desc()).limit(min(max(limit, 1), 500) * (4 if agent else 1))
            )
            out = [_journey(r) for r in rows.mappings() if not agent or f"agent:{agent}" in (r["components"] or ())]
        return {"journeys": out[: min(max(limit, 1), 500)]}

    @app.get("/api/journeys/{trace_id}")
    def journey_detail(trace_id: str, p: Principal = as_read):
        with engine.connect() as conn:
            where = sa.and_(journeys.c.tenant_id == p.tenant_id, journeys.c.trace_id == trace_id)
            row = conn.execute(select(journeys).where(where)).mappings().first()
            if not row:
                raise HTTPException(404, "no such journey (it may not be judged yet)")
            q = select(spans).where(spans.c.tenant_id == p.tenant_id, spans.c.trace_id == trace_id).order_by(spans.c.start)
            ss = [dict(r) for r in conn.execute(q).mappings()]
        t0 = row["start"]
        counted = "model" if any(x["kind"] == "model" for x in ss) else None  # see engine.journey_tokens
        twice = ev.nested_model_calls(ss)  # one call seen by two layers of instrumentation counts once
        uses: dict[str, dict] = {}
        for x in ss:
            if x["agent"] and x["span_id"] not in twice:
                u = uses.setdefault(
                    x["agent"],
                    {"agent": x["agent"], "pod": None, "node": None, "models": [], "tools": [], "cost": 0.0, "tokens": 0},
                )
                u["pod"], u["node"] = u["pod"] or x["pod"], u["node"] or x["node"]
                if x["kind"] in ("model", "tool") and x["target"] and x["target"] not in u[x["kind"] + "s"]:
                    u[x["kind"] + "s"].append(x["target"])
                u["cost"] += x["cost"] or 0.0
                if counted is None or x["kind"] == counted:
                    u["tokens"] += x["input_tokens"] + x["output_tokens"]
        waterfall = [
            {
                "span_id": x["span_id"],
                "parent_id": x["parent_id"],
                "kind": x["kind"],
                "name": x["name"],
                "agent": x["agent"],
                "target": x["target"],
                "pod": x["pod"],
                "offset_ms": round((x["start"] - t0) * 1000, 1),
                "duration_ms": round((x["end"] - x["start"]) * 1000, 1),
                "ok": x["ok"],
                "error_type": x["error_type"],
                "tokens": x["input_tokens"] + x["output_tokens"],
                "cost": x["cost"],
                "attrs": {k: v for k, v in (x["attrs"] or {}).items() if not k.startswith("sim.")},
            }
            for x in ss
        ]
        return {"journey": _journey(row), "uses": list(uses.values()), "spans": waterfall}

    @app.get("/api/sessions")
    def sessions(p: Principal = as_read):
        j = journeys.c
        good = func.sum(sa.case((j.outcome == "success", 1), else_=0))
        q = select(j.session_id, func.max(j.user_id), func.count(), good, func.sum(j.cost), func.min(j.start), func.max(j.end))
        q = (
            q.where(j.tenant_id == p.tenant_id, j.session_id.is_not(None))
            .group_by(j.session_id)
            .order_by(func.max(j.end).desc())
            .limit(60)
        )
        with engine.connect() as conn:
            keys = ("session_id", "user_id", "journeys", "succeeded", "cost", "start", "end")
            return {"sessions": [dict(zip(keys, r, strict=True)) for r in conn.execute(q)]}

    @app.get("/api/agents/{name}")
    def agent_detail(name: str, p: Principal = as_read):
        now = time.time()
        with engine.connect() as conn:
            found = ev.agent_statuses(conn, s, p.tenant_id, now).get(name)
            if not found:
                raise HTTPException(404, "no such agent")
            a, statuses = found
            e, g, j = autonomy_events.c, gate_events.c, journeys.c
            events = conn.execute(
                select(autonomy_events).where(e.tenant_id == p.tenant_id, e.agent == name).order_by(e.at.desc()).limit(30)
            )
            gates = conn.execute(
                select(gate_events).where(g.tenant_id == p.tenant_id, g.agent == name).order_by(g.at.desc()).limit(30)
            )
            blamed = (
                select(journeys).where(j.tenant_id == p.tenant_id, j.culprit == f"agent:{name}").order_by(j.end.desc()).limit(15)
            )
            return {
                "agent": _agent(a, statuses),
                "config": a["config"] or {},
                "events": [dict(r) for r in events.mappings()],
                "gate_events": [dict(r) for r in gates.mappings()],
                "blamed_journeys": [_journey(r) for r in conn.execute(blamed).mappings()],
            }

    @app.get("/api/experiments")
    def experiment_list(p: Principal = as_read):
        x = experiments.c
        with engine.connect() as conn:
            rows = conn.execute(select(experiments).where(x.tenant_id == p.tenant_id).order_by(x.started_at.desc()).limit(50))
            return {"experiments": [dict(r) for r in rows.mappings()], "faults": {k: list(v) for k, v in chaos.FAULTS.items()}}

    @app.get("/api/incidents")
    def incident_list(p: Principal = as_read):
        i = incidents.c
        with engine.connect() as conn:
            rows = conn.execute(select(incidents).where(i.tenant_id == p.tenant_id).order_by(i.opened_at.desc()).limit(80))
            return {"incidents": [dict(r) for r in rows.mappings()]}

    @app.get("/api/approvals")
    def approval_list(status: str = "pending", p: Principal = as_read):
        a = approvals.c
        with engine.connect() as conn:
            q = select(approvals).where(a.tenant_id == p.tenant_id, a.status == status).order_by(a.requested_at.desc()).limit(80)
            return {"approvals": [dict(r) for r in conn.execute(q).mappings()]}

    @app.get("/api/rca")
    def rca_view(p: Principal = as_read):
        now = time.time()
        with engine.connect() as conn:
            recent = ev.recent_journeys(conn, s, p.tenant_id, now)
        bad = [r for r in recent if r["outcome"] != "success"]
        named = defaultdict(lambda: {"journeys": 0, "evidence": 0, "statistics": 0})
        for r in bad:
            row = named[r["culprit"] or "unknown"]
            row["journeys"] += 1
            if r["culprit_source"]:
                row[r["culprit_source"]] += 1
        ranked = sorted(({"component": c, **v} for c, v in named.items()), key=lambda r: -r["journeys"])
        scores = sorted(ev.suspicion_scores(recent).items(), key=lambda kv: -kv[1])[:15]
        return {
            "window_journeys": len(recent),
            "not_successful": len(bad),
            "culprits": ranked,
            "suspicion": [{"component": c, "score": v} for c, v in scores],
        }

    @app.get("/api/audit")
    def audit_list(p: Principal = as_admin):
        a = audit_log.c
        with engine.connect() as conn:
            rows = conn.execute(select(audit_log).where(a.tenant_id == p.tenant_id).order_by(a.at.desc()).limit(200))
            return {"audit": [dict(r) for r in rows.mappings()]}

    # ------------------------------------------------------------ dashboard: operate

    @app.post("/api/agents/{name}/autonomy")
    def autonomy_override(name: str, body: AutonomyBody, p: Principal = as_operate):
        now = time.time()
        with engine.begin() as conn:
            where = sa.and_(agents.c.tenant_id == p.tenant_id, agents.c.name == name)
            old = conn.execute(select(agents.c.autonomy).where(where)).scalar()
            if old is None:
                raise HTTPException(404, "no such agent")
            ev.set_autonomy(conn, p.tenant_id, name, old, body.level, body.reason, f"key:{p.key_id}", now)
            audit(conn, p.tenant_id, f"key:{p.key_id}", "autonomy.set", {"agent": name, "from": old, **body.model_dump()}, now)
        cache.pop(("snapshot", p.tenant_id), None)  # this replica's runtimes see a person's decision on their next sync
        return {"agent": name, "autonomy": body.level}

    @app.post("/api/experiments", status_code=201)
    def experiment_start(body: ExperimentBody, p: Principal = as_operate):
        try:
            chaos.validate(body.fault, body.target, body.intensity, body.duration_s)
        except ValueError as e:
            raise HTTPException(422, str(e)) from e
        now = time.time()
        row = dict(id=uuid.uuid4().hex, tenant_id=p.tenant_id, status="running", started_at=now, **body.model_dump())
        row["name"] = row["name"] or f"{body.fault} on {body.target}"
        with engine.begin() as conn:
            x = experiments.c
            clash = sa.and_(x.tenant_id == p.tenant_id, x.target == body.target, x.status == "running")
            if conn.execute(select(x.id).where(clash)).first():
                raise HTTPException(409, "an experiment is already running on that target")
            conn.execute(insert(experiments).values(**row))
            audit(conn, p.tenant_id, f"key:{p.key_id}", "chaos.start", body.model_dump(), now)
        return {"id": row["id"], "status": "running"}

    @app.post("/api/experiments/{experiment_id}/stop")
    def experiment_stop(experiment_id: str, p: Principal = as_operate):
        x, now = experiments.c, time.time()
        with engine.begin() as conn:
            mine = sa.and_(x.id == experiment_id, x.tenant_id == p.tenant_id, x.status == "running")
            if not conn.execute(update(experiments).where(mine).values(status="analysing", ended_at=now)).rowcount:
                raise HTTPException(404, "no running experiment with that id")
            audit(conn, p.tenant_id, f"key:{p.key_id}", "chaos.stop", {"id": experiment_id}, now)
        return {"id": experiment_id, "status": "analysing"}

    @app.post("/api/approvals/{approval_id}/decision")
    def approval_decide(approval_id: str, body: Decision, p: Principal = as_operate):
        a, now = approvals.c, time.time()
        status = "approved" if body.approve else "denied"
        with engine.begin() as conn:
            # "pending" in the WHERE makes this safe when two operators click at once: only the first one wins
            mine = sa.and_(a.id == approval_id, a.tenant_id == p.tenant_id, a.status == "pending")
            if not conn.execute(update(approvals).where(mine).values(status=status, decided_at=now, decided_by=body.by)).rowcount:
                raise HTTPException(409, "already decided, expired or unknown")
            audit(conn, p.tenant_id, f"key:{p.key_id}", f"approval.{status}", {"id": approval_id, "by": body.by}, now)
        return {"id": approval_id, "status": status}

    # ------------------------------------------------------------ admin

    @app.put("/api/fleet")
    def fleet_apply(body: Fleet, p: Principal = as_admin):
        now = time.time()
        for name, spec in body.agents.items():
            unknown = set(spec.slo) - set(slo.DEFAULT_SLOS)
            if unknown or any(not 0 < t < 1 for t in spec.slo.values()):
                raise HTTPException(
                    422, f"{name}: SLO targets must be one of {list(slo.DEFAULT_SLOS)} with a value between 0 and 1"
                )
        with engine.begin() as conn:
            cfg = {"prices": {m: list(v) for m, v in body.prices.items()}, "fallbacks": body.fallbacks}
            conn.execute(update(tenants).where(tenants.c.id == p.tenant_id).values(config=cfg))
            for name, spec in body.agents.items():
                where = sa.and_(agents.c.tenant_id == p.tenant_id, agents.c.name == name)
                config = spec.model_dump(exclude_none=True)
                if not conn.execute(update(agents).where(where).values(config=config)).rowcount:
                    row = dict(tenant_id=p.tenant_id, name=name, autonomy="autonomous", since=now, config=config)
                    conn.execute(insert(agents).values(**row))
            audit(conn, p.tenant_id, f"key:{p.key_id}", "fleet.apply", {"agents": sorted(body.agents)}, now)
        cache.pop(("snapshot", p.tenant_id), None)
        return {"agents": len(body.agents)}

    @app.post("/api/keys", status_code=201)
    def key_create(body: KeyBody, p: Principal = as_admin):
        now = time.time()
        with engine.begin() as conn:
            key = create_key(conn, p.tenant_id, sorted(set(body.scopes)), body.label, now)
            audit(conn, p.tenant_id, f"key:{p.key_id}", "key.create", {"id": key_id_of(key), "scopes": body.scopes}, now)
        return {"id": key_id_of(key), "key": key, "note": "shown once; store it now"}

    @app.delete("/api/keys/{key_id}")
    def key_revoke(key_id: str, p: Principal = as_admin):
        now = time.time()
        with engine.begin() as conn:
            mine = sa.and_(api_keys.c.id == key_id, api_keys.c.tenant_id == p.tenant_id)
            if not conn.execute(update(api_keys).where(mine).values(revoked_at=now)).rowcount:
                raise HTTPException(404, "no such key")
            audit(conn, p.tenant_id, f"key:{p.key_id}", "key.revoke", {"id": key_id}, now)
        cache.pop(("key", key_id), None)  # shortcut: other replicas keep a revoked key for up to 5s (their cache TTL)
        return {"id": key_id, "revoked": True}

    # ------------------------------------------------------------ ops

    @app.get("/healthz")
    def healthz():
        return {"ok": True}

    @app.get("/readyz")
    def readyz():
        try:
            with engine.connect() as conn:
                conn.execute(select(1))
        except sa.exc.SQLAlchemyError as e:
            raise HTTPException(503, "database unreachable") from e
        return {"ok": True, "leader": evaluator.leader}

    @app.get("/metrics")
    def metrics(authorization: str = Header("")):
        """Prometheus text. Without a key: this process only. With a read key: that tenant's agents and SLOs too."""
        now = time.time()
        lines = [
            "# TYPE themis_ingest_spans_total counter",
            f"themis_ingest_spans_total {counters['spans']}",
            "# TYPE themis_ingest_rejected_spans_total counter",
            f"themis_ingest_rejected_spans_total {counters['rejected']}",
            "# TYPE themis_evaluator_leader gauge",
            f"themis_evaluator_leader {int(evaluator.leader)}",
            "# TYPE themis_evaluator_last_tick_age_seconds gauge",
            f"themis_evaluator_last_tick_age_seconds {now - evaluator.last_tick if evaluator.last_tick else -1:.1f}",
            "# HELP themis_evaluator_lag_seconds how long the oldest finished journey waited to be judged, at the last tick",
            "# TYPE themis_evaluator_lag_seconds gauge",
            f"themis_evaluator_lag_seconds {evaluator.lag:.2f}",
            "# TYPE themis_evaluator_tick_seconds gauge",
            f"themis_evaluator_tick_seconds {evaluator.tick_seconds:.3f}",
            "# TYPE themis_evaluator_interval_seconds gauge",
            f"themis_evaluator_interval_seconds {s.eval_interval}",
            "# TYPE themis_journeys_judged_total counter",
            f"themis_journeys_judged_total {ev.COUNTERS['journeys_judged']}",
            "# TYPE themis_traces_skipped_total counter",
            f"themis_traces_skipped_total {ev.COUNTERS['traces_skipped']}",
            "# TYPE themis_webhook_deliveries_total counter",
            *[
                f'themis_webhook_deliveries_total{{result="{r}"}} {ev.COUNTERS[f"webhook_{r}"]}'
                for r in ("sent", "pending", "failed", "skipped", "deferred")
            ],
            "# HELP http_server_request_duration_seconds Duration of HTTP server requests",
            "# TYPE http_server_request_duration_seconds histogram",
        ]
        for (route, method, status), row in sorted(http_hist.items()):
            tag = f'http_route="{route}",http_request_method="{method}",http_response_status_code="{status}"'
            for edge, n in zip(LATENCY_BUCKETS, row, strict=False):
                lines.append(f'http_server_request_duration_seconds_bucket{{{tag},le="{edge}"}} {n}')
            lines.append(f'http_server_request_duration_seconds_bucket{{{tag},le="+Inf"}} {row[-2]}')
            lines.append(f"http_server_request_duration_seconds_sum{{{tag}}} {row[-1]:.4f}")
            lines.append(f"http_server_request_duration_seconds_count{{{tag}}} {row[-2]}")
        if authorization:
            p = need("read")(authorization)  # a bad key is still a 401, never a silent downgrade
            with engine.connect() as conn:
                found = ev.agent_statuses(conn, s, p.tenant_id, now)
            level = {"autonomous": 2, "supervised": 1, "suspended": 0}
            lines += [
                "# HELP themis_agent_autonomy 2 autonomous, 1 supervised, 0 suspended",
                "# TYPE themis_agent_autonomy gauge",
            ]
            lines += [f'themis_agent_autonomy{{agent="{_label(n)}"}} {level[a["autonomy"]]}' for n, (a, _) in found.items()]
            lines += ["# TYPE themis_slo_burn_rate gauge", "# TYPE themis_slo_budget_left gauge"]
            for n, (_, statuses) in found.items():
                for st in statuses:
                    if st.value is not None:
                        tag = f'agent="{_label(n)}",sli="{st.sli}"'
                        lines.append(f'themis_slo_burn_rate{{{tag},window="1h"}} {st.burns.get(slo.FAST_LONG, 0):.4f}')
                        lines.append(f'themis_slo_burn_rate{{{tag},window="5m"}} {st.burns.get(slo.FAST_SHORT, 0):.4f}')
                        lines.append(f"themis_slo_budget_left{{{tag}}} {st.budget_left:.4f}")
        return Response("\n".join(lines) + "\n", media_type="text/plain; version=0.0.4")

    app.mount("/", StaticFiles(directory=WEB, html=True), name="web")
    return app


def _route(request: Request) -> str:
    """The route template ("/api/agents/{name}"), never the raw path, so the metric's label set stays small.
    Methods come from the route table too, so a client cannot invent label values with odd methods."""
    route = request.scope.get("route")
    path = getattr(route, "path", "")
    return path if path and not isinstance(getattr(route, "app", None), StaticFiles) else "static"


def _label(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")


def _journey(r) -> dict:
    keys = ("trace_id", "name", "session_id", "user_id", "start", "end", "outcome", "cost", "tokens", "span_count")
    out = {k: r[k] for k in keys}
    out.update(duration_ms=round((r["end"] - r["start"]) * 1000, 1), culprit=r["culprit"], culprit_source=r["culprit_source"])
    out.update(culprit_reason=r["culprit_reason"], confidence=r["confidence"])
    return out


def _agent(a: dict, statuses: list[slo.SliStatus]) -> dict:
    cfg = a["config"] or {}
    return {
        "name": a["name"],
        "autonomy": a["autonomy"],
        "reason": a["reason"],
        "since": a["since"],
        "last_seen": a["last_seen"],
        "model": cfg.get("model"),
        "slis": [st.as_dict() for st in statuses if st.value is not None],
    }


def _overview(engine: sa.Engine, s: Settings, tid: str, evaluator: ev.Evaluator) -> dict:
    now = time.time()
    live = max(s.scaled(1800), 15.0)  # "right now" for the map: 30 minutes in production, at least 15 seconds
    with engine.connect() as conn:
        # Vitals: counts and shares over the window, in SQL; percentiles from the latest 2,000 journeys.
        # Both stay cheap however much traffic the window holds.
        j = journeys.c
        window = sa.and_(j.tenant_id == tid, j.end >= now - live)
        of = lambda outcome: func.sum(sa.case((j.outcome == outcome, 1), else_=0))  # noqa: E731
        n, ok_n, failed_n, blocked_n, cost, tokens = conn.execute(
            select(func.count(), of("success"), of("failed"), of("blocked"), func.sum(j.cost), func.sum(j.tokens)).where(window)
        ).one()
        latest = conn.execute(select(j.start, j.end).where(window).order_by(j.end.desc()).limit(2000)).all()
        found = ev.agent_statuses(conn, s, tid, now)
        eb = edge_buckets.c
        first = int((now - live) // s.bucket_seconds)
        q = select(
            eb.agent,
            eb.kind,
            eb.target,
            func.sum(eb.calls),
            func.sum(eb.errors),
            func.sum(eb.seconds),
            func.sum(eb.cost),
            func.max(eb.pod),
        )
        edges_raw = conn.execute(q.where(eb.tenant_id == tid, eb.bucket >= first).group_by(eb.agent, eb.kind, eb.target)).all()
        qz = quarantine.c
        held = (
            conn.execute(select(qz.component, qz.until, qz.reason, qz.strikes).where(qz.tenant_id == tid, qz.active))
            .mappings()
            .all()
        )
        i, a, x, e = incidents.c, approvals.c, experiments.c, autonomy_events.c
        open_incidents = conn.execute(select(func.count()).where(i.tenant_id == tid, i.status == "open")).scalar()
        pending = conn.execute(select(func.count()).where(a.tenant_id == tid, a.status == "pending")).scalar()
        running = conn.execute(
            select(x.id, x.name, x.fault, x.target, x.intensity, x.started_at, x.duration_s).where(
                x.tenant_id == tid, x.status == "running"
            )
        )
        running = [dict(r) for r in running.mappings()]
        moves = conn.execute(
            select(e.at, e.agent, e.old, e.new, e.reason, e.actor).where(e.tenant_id == tid).order_by(e.at.desc()).limit(25)
        )
        events = [
            {
                "at": at,
                "kind": "autonomy",
                "text": f"{ag} moved from {old} to {new}",
                "detail": reason,
                "actor": actor,
                "level": new,
            }
            for at, ag, old, new, reason, actor in moves
        ]
        for comp, since, reason in conn.execute(
            select(qz.component, qz.since, qz.reason).where(qz.tenant_id == tid).order_by(qz.since.desc()).limit(10)
        ):
            events.append(
                {"at": since, "kind": "quarantine", "text": f"{comp} quarantined", "detail": reason, "level": "supervised"}
            )
        for at, name in conn.execute(
            select(x.started_at, x.name).where(x.tenant_id == tid).order_by(x.started_at.desc()).limit(10)
        ):
            events.append({"at": at, "kind": "chaos", "text": f"chaos started: {name}", "detail": "", "level": "chaos"})

    n = n or 0
    durations = sorted((end - start) * 1000 for start, end in latest)
    share = {o: (c or 0) / n if n else None for o, c in (("success", ok_n), ("failed", failed_n), ("blocked", blocked_n))}
    per_agent: dict[str, dict] = defaultdict(lambda: {"tasks": 0, "errors": 0, "avg_ms": None, "cost": 0.0, "pod": None})
    edges = []
    for agent, kind, target, count, errs, seconds, spent, pod in edges_raw:
        avg = (seconds or 0.0) / count if count else 0.0
        row = per_agent[agent]
        row["cost"] += spent or 0.0
        row["pod"] = row["pod"] or pod
        if kind == "agent":
            row.update(tasks=count, errors=int(errs or 0), avg_ms=round(avg * 1000, 1))
        elif target:
            edges.append(
                {
                    "from": agent,
                    "kind": kind,
                    "to": target,
                    "count": count,
                    "errors": int(errs or 0),
                    "avg_ms": round((avg or 0) * 1000, 1),
                }
            )
    return {
        "at": now,
        "window_s": live,
        "window_scale": s.window_scale,
        "leader": evaluator.leader,
        "vitals": {
            "journeys": n,
            "per_min": round(n / (live / 60), 1),
            **share,
            "p50_ms": round(durations[len(durations) // 2], 1) if durations else None,
            "p95_ms": round(durations[min(len(durations) - 1, int(len(durations) * 0.95))], 1) if durations else None,
            "cost_per_min": round((cost or 0.0) / (live / 60), 5),
            "tokens_per_min": round((tokens or 0) / (live / 60)),
        },
        "agents": [{**_agent(a_row, statuses), **per_agent.get(name, {})} for name, (a_row, statuses) in found.items()],
        "edges": edges,
        "quarantine": [dict(r) for r in held],
        "open_incidents": open_incidents,
        "pending_approvals": pending,
        "experiments": running,
        "events": sorted(events, key=lambda ev_: -(ev_["at"] or 0))[:30],
    }
