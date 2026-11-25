"""Shared fixtures.

Every database test runs on SQLite, and again on Postgres when THEMIS_TEST_PG_URL is set (CI sets it).
Keys are generated at run time: no secret-looking string is ever written in this repository.
"""

from __future__ import annotations

import os
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest
import sqlalchemy as sa
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest

from themis import db
from themis.lab import Lab
from themis.settings import Settings

PG_URL = os.getenv("THEMIS_TEST_PG_URL", "")
T0 = 1_000_000.0  # a fixed "now" for engine tests, which never touch the clock


@pytest.fixture(params=["sqlite", "postgres"] if PG_URL else ["sqlite"])
def db_url(request, tmp_path):
    if request.param == "sqlite":
        yield f"sqlite:///{tmp_path}/themis.db"
        return
    yield PG_URL
    engine = sa.create_engine(PG_URL)  # leave the shared database empty for the next test
    with engine.begin() as conn:
        for table in reversed(db.md.sorted_tables):
            conn.execute(table.delete())
    engine.dispose()


@pytest.fixture
def settings(db_url):
    # production windows (scale 1), no settle delay, and an evaluator that only runs when a test calls tick()
    return Settings.from_env(db_url=db_url, window_scale=1.0, settle_seconds=0.0, eval_interval=3600.0, rate_limit_per_sec=10_000)


@pytest.fixture
def engine(settings):
    engine = db.make_engine(settings.db_url)
    with engine.begin() as conn:
        db.insert_ignore(conn, db.tenants, {"id": "default", "name": "default", "config": {}})
    yield engine
    engine.dispose()


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def wait_for(check, seconds: float, what: str = "condition"):
    """Poll until check() is truthy and return its value. Fails with a clear message instead of hanging."""
    deadline = time.time() + seconds
    while time.time() < deadline:
        if value := check():
            return value
        time.sleep(0.1)
    raise AssertionError(f"timed out after {seconds}s waiting for {what}")


class DatabaseOutage:
    """Makes every statement on one engine fail the way a lost database does, while it is switched on.

    It hooks the driver call itself, so everything above it (pool, retries, error handling, HTTP answers)
    runs exactly as it would in a real outage.
    """

    def __init__(self, engine: sa.Engine):
        self.engine, self.on = engine, False
        sa.event.listen(engine, "before_cursor_execute", self._fail)

    def _fail(self, conn, cursor, statement, params, context, executemany):
        if self.on:
            raise sa.exc.OperationalError(statement[:60], None, ConnectionError("simulated: the database is unreachable"))

    def __enter__(self):
        self.on = True
        return self

    def __exit__(self, *_):
        self.on = False


class Receiver:
    """A stand-in for Aegis War Room or Argus: records what Themis posts."""

    def __init__(self, status=200, delay=0.0):
        self.got, outer = [], self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers["Content-Length"]))
                time.sleep(delay)  # a receiver that is slow to answer
                outer.got.append((dict(self.headers), body))
                self.send_response(status)
                self.end_headers()

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}/api/alerts"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()


@pytest.fixture
def receiver():
    r = Receiver()
    yield r
    r.server.shutdown()


class Api:
    """The real server on a free port, a key for each scope, and a way to mint keys for a second tenant."""

    def __init__(self, settings: Settings):
        port = free_port()
        keep = ("settle_seconds", "eval_interval", "rate_limit_per_sec", "max_body_bytes", "webhooks", "webhook_allow_private")
        self.lab = Lab(settings.db_url, port, settings.window_scale, **{k: getattr(settings, k) for k in keep}).start()
        self.engine, self.settings = self.lab.engine, self.lab.s
        self.http = httpx.Client(base_url=self.lab.url, timeout=10)
        self.keys = {scope: self.lab.new_key("default", [scope]) for scope in ("ingest", "read", "operate", "admin")}

    def auth(self, scope_or_key: str) -> dict:
        return {"Authorization": f"Bearer {self.keys.get(scope_or_key, scope_or_key)}"}

    def ingest(self, body: bytes, key: str = "ingest", **headers):
        headers = {"Content-Type": "application/x-protobuf", **self.auth(key), **headers}
        return self.http.post("/v1/traces", content=body, headers=headers)

    def close(self) -> None:
        self.http.close()
        self.lab.stop()
        self.engine.dispose()


@pytest.fixture
def api(settings):
    api = Api(settings)
    yield api
    api.close()


def journey(agent: str = "coder", error: bool = False) -> tuple[str, bytes]:
    """(trace id, OTLP body) for one small real journey that ended just now."""
    now, trace, root, a = time.time(), os.urandom(16), os.urandom(8), os.urandom(8)
    agent_attrs = {"gen_ai.operation.name": "invoke_agent", "gen_ai.agent.name": agent}
    return trace.hex(), otlp(
        [
            dict(
                name="journey ask",
                trace=trace,
                id=root,
                start=now - 1,
                end=now,
                attrs={"themis.journey.name": "ask", "session.id": "s1"},
            ),
            dict(name="invoke", trace=trace, id=a, parent=root, start=now - 0.9, end=now - 0.1, error=error, attrs=agent_attrs),
        ]
    )


def otlp(spans: list[dict], resource: dict | None = None) -> bytes:
    """An OTLP/HTTP protobuf export. Each span: name, trace, id, parent, start, end, error, attrs."""
    req = ExportTraceServiceRequest()
    rs = req.resource_spans.add()
    for k, v in (resource or {"service.name": "test"}).items():
        rs.resource.attributes.add(key=k).value.string_value = v
    scope = rs.scope_spans.add()
    for sp in spans:
        out = scope.spans.add(trace_id=sp["trace"], span_id=sp["id"], parent_span_id=sp.get("parent", b""), name=sp["name"])
        out.start_time_unix_nano, out.end_time_unix_nano = int(sp["start"] * 1e9), int(sp["end"] * 1e9)
        if sp.get("error"):
            out.status.code = 2
        for k, v in (sp.get("attrs") or {}).items():
            value = out.attributes.add(key=k).value
            if isinstance(v, bool):
                value.bool_value = v
            elif isinstance(v, int):
                value.int_value = v
            elif isinstance(v, float):
                value.double_value = v
            else:
                value.string_value = v
    return req.SerializeToString()


class Trace:
    """One journey's span rows, written straight to the database the way ingest would store them."""

    def __init__(self, at: float, name: str = "ask_question", tenant: str = "default", outcome: str | None = None, **attrs):
        self.at, self.tenant, self.trace_id, self.rows = at, tenant, os.urandom(16).hex(), []
        root_attrs = {"themis.journey.name": name, "session.id": "s1", "user.id": "u1", **attrs}
        if outcome:
            root_attrs["themis.journey.outcome"] = outcome
        self.root = self.add(None, "other", f"journey {name}", attrs=root_attrs, start=at - 1.0)

    def add(self, parent, kind, name, agent=None, target=None, ok=True, error=None, attrs=None, tokens=(0, 0), start=None) -> str:
        span_id = os.urandom(8).hex()
        start = start if start is not None else self.at - 0.9 + 0.01 * len(self.rows)  # later spans start later
        self.rows.append(
            dict(
                tenant_id=self.tenant, trace_id=self.trace_id, span_id=span_id, parent_id=parent, is_root=parent is None, done=False,
                kind=kind, name=name, agent=agent, target=target, pod=f"pod-{agent}" if agent else None, node="node-1",
                start=start, end=self.at if parent is None else start + 0.005, ok=ok, error_type=error,
                input_tokens=tokens[0], output_tokens=tokens[1], cost=None, attrs=attrs or {},
            )
        )  # fmt: skip
        return span_id

    def agent(self, name: str, ok: bool = True, error: str | None = None) -> str:
        return self.add(self.root, "agent", f"invoke_agent {name}", agent=name, ok=ok, error=error)

    def model(self, parent: str, agent: str | None, model: str, ok: bool = True, tokens=(1000, 200)) -> str:
        return self.add(
            parent,
            "model",
            f"chat {model}",
            agent=agent,
            target=model,
            ok=ok,
            error=None if ok else "rate_limited",
            tokens=tokens,
        )

    def tool(self, parent: str, agent: str | None, tool: str, ok: bool = True, error: str | None = None, **attrs) -> str:
        return self.add(parent, "tool", f"execute_tool {tool}", agent=agent, target=tool, ok=ok, error=error, attrs=attrs)

    def handoff(self, parent: str, frm: str, to: str, valid: bool = True) -> str:
        return self.add(parent, "handoff", f"handoff {to}", agent=frm, target=to, attrs={"themis.handoff.valid": valid})

    def save(self, conn: sa.Connection) -> Trace:
        conn.execute(sa.insert(db.spans), self.rows)
        return self


def simple(conn, at: float, agent: str = "coder", ok: bool = True, tool: str | None = None, **tool_attrs) -> Trace:
    """The smallest useful journey: one agent, one model call, optionally one tool call."""
    t = Trace(at, outcome=None if ok else "failed")
    a = t.agent(agent, ok=ok, error=None if ok else "task_error")
    t.model(a, agent, "sim-large")
    if tool:
        t.tool(a, agent, tool, **tool_attrs)
    return t.save(conn)
