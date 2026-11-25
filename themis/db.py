"""Tables and the few portable helpers the rest of the code needs. SQLite for one box, Postgres for HA.

Everything is scoped by tenant_id. Timestamps are float epoch seconds.
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy import Boolean, Column, Float, Index, Integer, String, Table
from sqlalchemy.dialects import postgresql, sqlite

md = sa.MetaData()
JSON = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")
BigId = sa.BigInteger().with_variant(Integer, "sqlite")  # SQLite only auto-increments INTEGER primary keys

tenants = Table("tenants", md, Column("id", String(40), primary_key=True), Column("name", String(120)), Column("config", JSON))

api_keys = Table(
    "api_keys",
    md,
    Column("id", String(16), primary_key=True),  # public part of the key, used for lookup
    Column("tenant_id", String(40), nullable=False),
    Column("hash", String(64), nullable=False),  # sha256 of the whole key; the key itself is never stored
    Column("scopes", String(120), nullable=False),
    Column("label", String(120)),
    Column("created_at", Float),
    Column("revoked_at", Float),
)

spans = Table(
    "spans",
    md,
    Column("id", BigId, primary_key=True, autoincrement=True),
    Column("tenant_id", String(40), nullable=False),
    Column("trace_id", String(32), nullable=False),
    Column("span_id", String(16), nullable=False),
    Column("parent_id", String(16)),
    Column("is_root", Boolean, nullable=False),
    Column("done", Boolean, nullable=False, default=False),  # root only: the journey has been judged
    Column("kind", String(12), nullable=False),  # agent | model | tool | handoff | other
    Column("name", String(200)),
    Column("agent", String(120)),
    Column("target", String(160)),  # model name, tool name, or the agent handed to
    Column("pod", String(160)),
    Column("node", String(160)),
    Column("start", Float, nullable=False),
    Column("end", Float, nullable=False),
    Column("ok", Boolean, nullable=False),
    Column("error_type", String(80)),
    Column("input_tokens", Integer, default=0),
    Column("output_tokens", Integer, default=0),
    Column("cost", Float),
    Column("attrs", JSON),
    sa.UniqueConstraint("tenant_id", "trace_id", "span_id", name="uq_span"),
    Index("ix_spans_trace", "tenant_id", "trace_id"),
    Index("ix_spans_roots", "tenant_id", "is_root", "done", "end"),
    Index("ix_spans_end", "tenant_id", "end"),
)

journeys = Table(
    "journeys",
    md,
    Column("tenant_id", String(40), primary_key=True),
    Column("trace_id", String(32), primary_key=True),
    Column("name", String(200)),
    Column("session_id", String(120)),
    Column("user_id", String(120)),
    Column("start", Float),
    Column("end", Float),
    Column("outcome", String(12)),  # success | failed | blocked
    Column("cost", Float),
    Column("tokens", Integer),
    Column("span_count", Integer),
    Column("components", JSON),  # every agent:/tool:/model:/handoff: the journey touched
    Column("culprit", String(200)),
    Column("culprit_source", String(12)),  # evidence | statistics
    Column("culprit_reason", String(300)),
    Column("confidence", Float),
    Column("truth", String(200)),  # simulator ground truth, used only to score the RCA
    Index("ix_journeys_end", "tenant_id", "end"),
    Index("ix_journeys_session", "tenant_id", "session_id"),
)

agents = Table(
    "agents",
    md,
    Column("tenant_id", String(40), primary_key=True),
    Column("name", String(120), primary_key=True),
    Column("autonomy", String(12), nullable=False, default="autonomous"),
    Column("reason", String(300)),
    Column("since", Float),
    Column("config", JSON),  # slo targets, policy (tools, handoffs, high_risk), cost_ceiling, model
    Column("last_seen", Float),
)

# subject is an agent name; sli is one of slo.DEFAULT_SLOS. One row per time bucket.
sli_buckets = Table(
    "sli_buckets",
    md,
    Column("tenant_id", String(40), primary_key=True),
    Column("subject", String(200), primary_key=True),
    Column("sli", String(24), primary_key=True),
    Column("bucket", sa.BigInteger, primary_key=True),
    Column("good", Integer, nullable=False),
    Column("total", Integer, nullable=False),
)

quarantine = Table(
    "quarantine",
    md,
    Column("tenant_id", String(40), primary_key=True),
    Column("component", String(200), primary_key=True),
    Column("active", Boolean, nullable=False),
    Column("since", Float),
    Column("until", Float),
    Column("released_at", Float),
    Column("strikes", Integer, default=0),
    Column("reason", String(300)),
)

autonomy_events = Table(
    "autonomy_events",
    md,
    Column("id", BigId, primary_key=True, autoincrement=True),
    Column("tenant_id", String(40), nullable=False),
    Column("agent", String(120)),
    Column("old", String(12)),
    Column("new", String(12)),
    Column("reason", String(300)),
    Column("actor", String(120)),
    Column("at", Float),
    Index("ix_autonomy_events", "tenant_id", "at"),
)

gate_events = Table(
    "gate_events",
    md,
    Column("id", BigId, primary_key=True, autoincrement=True),
    Column("tenant_id", String(40), nullable=False),
    Column("agent", String(120)),
    Column("action", String(200)),
    Column("decision", String(24)),
    Column("reason", String(300)),
    Column("at", Float),
    Index("ix_gate_events", "tenant_id", "agent", "at"),
)

approvals = Table(
    "approvals",
    md,
    Column("id", String(32), primary_key=True),
    Column("tenant_id", String(40), nullable=False),
    Column("agent", String(120)),
    Column("action", String(200)),
    Column("detail", JSON),
    Column("status", String(12)),  # pending | approved | denied | expired
    Column("requested_at", Float),
    Column("decided_at", Float),
    Column("decided_by", String(120)),
    Index("ix_approvals", "tenant_id", "status", "requested_at"),
)

experiments = Table(
    "experiments",
    md,
    Column("id", String(32), primary_key=True),
    Column("tenant_id", String(40), nullable=False),
    Column("name", String(160)),
    Column("fault", String(40)),
    Column("target", String(200)),
    Column("intensity", Float),
    Column("duration_s", Float),
    Column("status", String(12)),  # running | analysing | done
    Column("started_at", Float),
    Column("ended_at", Float),
    Column("result", JSON),
    Index("ix_experiments", "tenant_id", "started_at"),
)

incidents = Table(
    "incidents",
    md,
    Column("id", String(32), primary_key=True),
    Column("tenant_id", String(40), nullable=False),
    Column("kind", String(40)),
    Column("subject", String(200)),
    Column("severity", String(12)),  # info | warning | critical
    Column("title", String(300)),
    Column("detail", JSON),
    Column("status", String(12)),  # open | resolved
    Column("opened_at", Float),
    Column("resolved_at", Float),
    Column("notify_state", String(12)),  # pending | sent | failed | skipped; set back to pending on resolve
    Column("notify_attempts", Integer, default=0),
    Index("ix_incidents", "tenant_id", "status", "opened_at"),
)

audit_log = Table(
    "audit_log",
    md,
    Column("id", BigId, primary_key=True, autoincrement=True),
    Column("tenant_id", String(40)),
    Column("at", Float),
    Column("actor", String(120)),
    Column("action", String(80)),
    Column("detail", JSON),
)

leases = Table(
    "leases", md, Column("name", String(40), primary_key=True), Column("holder", String(120)), Column("expires", Float)
)


def make_engine(url: str) -> sa.Engine:
    if url.startswith("sqlite"):
        engine = sa.create_engine(url, connect_args={"check_same_thread": False, "timeout": 15})

        @sa.event.listens_for(engine, "connect")
        def _pragmas(dbapi, _):  # WAL lets the API read while the evaluator writes
            cur = dbapi.cursor()
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA synchronous=NORMAL")
            cur.close()

    else:
        engine = sa.create_engine(url, pool_size=10, max_overflow=10, pool_pre_ping=True)
    md.create_all(engine)
    return engine


def insert_ignore(conn: sa.Connection, table: Table, rows: list[dict] | dict) -> int:
    """INSERT that skips rows which already exist. Returns how many rows were written."""
    if not rows:
        return 0
    dialect = postgresql if conn.dialect.name == "postgresql" else sqlite
    return conn.execute(dialect.insert(table).on_conflict_do_nothing(), rows).rowcount


def take_lease(conn: sa.Connection, name: str, holder: str, ttl: float, now: float) -> bool:
    """One replica at a time holds a lease. It is free again when the holder stops renewing it."""
    free = sa.and_(leases.c.name == name, sa.or_(leases.c.holder == holder, leases.c.expires < now))
    if conn.execute(sa.update(leases).where(free).values(holder=holder, expires=now + ttl)).rowcount:
        return True
    return insert_ignore(conn, leases, {"name": name, "holder": holder, "expires": now + ttl}) == 1


def audit(conn: sa.Connection, tenant_id: str, actor: str, action: str, detail: dict, now: float) -> None:
    conn.execute(sa.insert(audit_log).values(tenant_id=tenant_id, at=now, actor=actor, action=action, detail=detail))
