"""The HTTP surface, over real HTTP: who may call what, tenant isolation, and input limits."""

import dataclasses
import gzip
import os
import re
import time

import pytest
import sqlalchemy as sa

from tests.conftest import Api, DatabaseOutage, journey, otlp
from themis import api as api_module
from themis import db

PUBLIC = {"/healthz", "/readyz", "/metrics"}  # the only routes that answer without a key (plus the static dashboard files)
FLEET = {
    "agents": {"coder": {"model": "m1", "tools": ["repo.read"], "handoffs": ["reviewer"], "slo": {"task_success": 0.9}}},
    "prices": {"m1": [1.0, 2.0]},
}


def judge(api: Api):
    from themis.engine import tick

    tick(api.engine, api.settings, time.time() + 1)


# ---------------------------------------------------------------- authentication and scopes


def test_every_route_refuses_a_request_without_a_key(api):
    """Walks the real route table, so a route added later without auth fails this test."""
    checked = 0
    for route in api.lab.app.routes:
        methods = getattr(route, "methods", None)
        if not methods or route.path in PUBLIC:
            continue
        path = re.sub(r"{[^}]+}", "x", route.path)
        for method in methods - {"HEAD", "OPTIONS"}:
            r = api.http.request(method, path, json={} if method in ("POST", "PUT") else None)
            assert r.status_code == 401, f"{method} {route.path} answered {r.status_code} without a key"
            checked += 1
    assert checked >= 20


def test_metrics_without_a_key_show_the_process_and_nothing_about_any_tenant(api):
    api.ingest(journey(agent="secret-project-agent")[1])
    judge(api)
    anonymous = api.http.get("/metrics")
    assert anonymous.status_code == 200 and "themis_evaluator_leader" in anonymous.text
    assert "secret-project-agent" not in anonymous.text and "themis_agent_autonomy" not in anonymous.text
    assert "secret-project-agent" in api.http.get("/metrics", headers=api.auth("read")).text
    assert api.http.get("/metrics", headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert api.http.get("/metrics", headers=api.auth("ingest")).status_code == 403


def test_a_wrong_or_malformed_key_is_refused(api):
    for bad in ("", "Bearer nonsense", "Bearer thm_000000000000_wrong", api.auth("read")["Authorization"][:-3] + "abc"):
        assert api.http.get("/api/overview", headers={"Authorization": bad}).status_code == 401


@pytest.mark.parametrize(
    "scope, method, path, allowed",
    [
        ("ingest", "GET", "/v1/gate/snapshot", True),
        ("ingest", "GET", "/api/overview", False),  # an agent's key cannot read the dashboard
        ("read", "GET", "/api/overview", True),
        ("read", "GET", "/v1/gate/snapshot", False),  # a dashboard key cannot see the fault plan
        ("read", "POST", "/api/experiments", False),
        ("read", "POST", "/api/agents/coder/autonomy", False),
        ("operate", "PUT", "/api/fleet", False),  # operators run the fleet, they do not redefine it
        ("operate", "POST", "/api/keys", False),
        ("operate", "GET", "/api/audit", False),
        ("admin", "GET", "/api/audit", True),  # admin includes every other scope
        ("admin", "GET", "/v1/gate/snapshot", True),
    ],
)
def test_scopes(api, scope, method, path, allowed):
    r = api.http.request(method, path, headers=api.auth(scope), json={} if method != "GET" else None)
    assert (r.status_code != 403) == allowed, r.text


def test_keys_are_stored_as_hashes_and_can_be_revoked(api):
    made = api.http.post("/api/keys", headers=api.auth("admin"), json={"scopes": ["read"], "label": "ci"}).json()
    with api.engine.connect() as conn:
        stored = [tuple(r) for r in conn.execute(sa.select(db.api_keys))]
    assert not any(made["key"] in str(row) for row in stored)  # only the hash is in the database
    assert api.http.get("/api/overview", headers=api.auth(made["key"])).status_code == 200
    assert api.http.delete(f"/api/keys/{made['id']}", headers=api.auth("admin")).status_code == 200
    assert api.http.get("/api/overview", headers=api.auth(made["key"])).status_code == 401
    audit = api.http.get("/api/audit", headers=api.auth("admin")).json()["audit"]
    assert {a["action"] for a in audit} >= {"key.create", "key.revoke"} and made["key"] not in str(audit)


def test_rate_limit_answers_429_with_retry_after(settings):
    api = Api(dataclasses.replace(settings, rate_limit_per_sec=3))
    try:
        codes = [api.http.get("/api/incidents", headers=api.auth("read")) for _ in range(12)]
        limited = [r for r in codes if r.status_code == 429]
        assert limited and limited[0].headers["retry-after"] == "1"
    finally:
        api.close()


# ---------------------------------------------------------------- tenant isolation


def test_one_tenant_cannot_see_or_touch_another(api):
    trace_id, body = journey()
    assert api.ingest(body).status_code == 200
    api.http.put("/api/fleet", headers=api.auth("admin"), json=FLEET).raise_for_status()
    approval = api.http.post(
        "/v1/approvals", headers=api.auth("ingest"), json={"agent": "coder", "action": "tool:deploy.apply"}
    ).json()
    api.http.post(
        "/api/experiments", headers=api.auth("operate"), json={"fault": "rogue_agent", "target": "agent:coder"}
    ).raise_for_status()
    judge(api)
    assert api.http.get(f"/api/journeys/{trace_id}", headers=api.auth("read")).status_code == 200  # the owner sees it

    other = api.auth(api.lab.new_key("other", ["admin"]))
    assert api.http.get("/api/journeys", headers=other).json()["journeys"] == []
    assert api.http.get(f"/api/journeys/{trace_id}", headers=other).status_code == 404
    assert api.http.get("/api/agents/coder", headers=other).status_code == 404
    assert api.http.get("/api/overview", headers=other).json()["agents"] == []
    assert api.http.get("/api/experiments", headers=other).json()["experiments"] == []
    assert api.http.get("/api/approvals", headers=other).json()["approvals"] == []
    snapshot = api.http.get("/v1/gate/snapshot", headers=other).json()
    assert snapshot["agents"] == {} and snapshot["faults"] == []
    assert api.http.get(f"/v1/approvals/{approval['id']}", headers=other).status_code == 404
    assert api.http.post(f"/api/approvals/{approval['id']}/decision", headers=other, json={"approve": True}).status_code == 409
    assert (
        api.http.post("/api/agents/coder/autonomy", headers=other, json={"level": "suspended", "reason": "not mine"}).status_code
        == 404
    )
    assert api.http.get(f"/v1/approvals/{approval['id']}", headers=api.auth("ingest")).json()["status"] == "pending"


# ---------------------------------------------------------------- ingest limits


def test_ingest_stores_once_and_reports_what_it_refused(api):
    trace_id, body = journey()
    assert api.ingest(body).json() == {}
    assert api.ingest(body).status_code == 200  # an exporter retry must not double-count
    with api.engine.connect() as conn:
        assert conn.execute(sa.select(sa.func.count()).select_from(db.spans)).scalar() == 2
    stale = otlp([dict(name="old", trace=os.urandom(16), id=os.urandom(8), start=1.0, end=2.0)])
    assert api.ingest(stale).json()["partialSuccess"]["rejectedSpans"] == 1
    assert api.ingest(b"\xff\xfe not otlp").status_code == 400


def test_bodies_over_the_limit_are_refused_before_they_are_read(settings):
    api = Api(dataclasses.replace(settings, max_body_bytes=20_000))
    try:
        big = b"x" * 30_000
        assert api.ingest(big).status_code == 413  # declared size
        assert (
            api.http.post("/v1/traces", content=iter([big[:15_000], big[15_000:]]), headers=api.auth("ingest")).status_code == 413
        )  # chunked
        bomb = gzip.compress(b"\0" * 5_000_000)  # 5 MB that travels as about 5 kB
        assert len(bomb) < 20_000 and api.ingest(bomb, **{"Content-Encoding": "gzip"}).status_code == 413
        _, small = journey()
        assert api.ingest(gzip.compress(small), **{"Content-Encoding": "gzip"}).status_code == 200
        assert api.ingest(small, **{"Content-Encoding": "gzip"}).status_code == 400  # says gzip, is not
        # other routes are parsed by the framework, so a body with no declared length is not accepted at all
        chunked = api.http.post(
            "/api/experiments", content=iter([b"{}"]), headers={**api.auth("operate"), "Content-Type": "application/json"}
        )
        assert chunked.status_code == 411
    finally:
        api.close()


def test_security_headers_on_api_and_dashboard(api):
    for path in ("/", "/app.js", "/healthz"):
        h = api.http.get(path).headers
        assert (
            h["x-content-type-options"] == "nosniff" and h["x-frame-options"] == "DENY" and h["referrer-policy"] == "no-referrer"
        )
        assert "script-src 'self'" in h["content-security-policy"] and "unsafe-eval" not in h["content-security-policy"]


# ---------------------------------------------------------------- operator actions


def test_fleet_and_chaos_inputs_are_validated(api):
    bad_slo = {"agents": {"coder": {"slo": {"made_up": 0.9}}}}
    assert api.http.put("/api/fleet", headers=api.auth("admin"), json=bad_slo).status_code == 422
    assert (
        api.http.put(
            "/api/fleet", headers=api.auth("admin"), json={"agents": {"coder": {"slo": {"task_success": 1.5}}}}
        ).status_code
        == 422
    )
    start = lambda **body: api.http.post("/api/experiments", headers=api.auth("operate"), json=body)  # noqa: E731
    assert start(fault="meteor", target="agent:coder").status_code == 422
    assert start(fault="rogue_agent", target="model:m1").status_code == 422  # a model cannot go rogue
    assert start(fault="rogue_agent", target="agent:coder", intensity=3).status_code == 422
    assert start(fault="rogue_agent", target="agent:coder").status_code == 201
    assert start(fault="context_poison", target="agent:coder").status_code == 409  # one experiment per target


def test_fleet_manifest_reaches_the_gate_snapshot(api):
    api.http.put("/api/fleet", headers=api.auth("admin"), json=FLEET).raise_for_status()
    snap = api.http.get("/v1/gate/snapshot", headers=api.auth("ingest")).json()
    assert snap["agents"]["coder"] == {
        "autonomy": "autonomous",
        "tools": ["repo.read"],
        "handoffs": ["reviewer"],
        "high_risk": [],
    }


def test_a_persons_autonomy_change_is_in_the_very_next_snapshot(api):
    """The snapshot is cached for half a second; a person's decision must not wait for that on this replica."""
    api.http.put("/api/fleet", headers=api.auth("admin"), json=FLEET).raise_for_status()
    snapshot = lambda: api.http.get("/v1/gate/snapshot", headers=api.auth("ingest")).json()["agents"]["coder"]  # noqa: E731
    assert snapshot()["autonomy"] == "autonomous"  # now cached
    body = {"level": "suspended", "reason": "pulled for review"}
    api.http.post("/api/agents/coder/autonomy", headers=api.auth("operate"), json=body).raise_for_status()
    assert snapshot()["autonomy"] == "suspended"


def test_approval_is_decided_once_and_expires_to_no(api, monkeypatch):
    def ask():
        body = {"agent": "deployer", "action": "tool:deploy.apply"}
        return api.http.post("/v1/approvals", headers=api.auth("ingest"), json=body).json()["id"]

    def decide(approval_id, go):
        return api.http.post(f"/api/approvals/{approval_id}/decision", headers=api.auth("operate"), json={"approve": go})

    first = ask()
    assert decide(first, True).json()["status"] == "approved"
    assert decide(first, False).status_code == 409  # the second click loses; the first answer stands
    assert api.http.get(f"/v1/approvals/{first}", headers=api.auth("ingest")).json()["status"] == "approved"
    second = ask()
    monkeypatch.setattr(api_module, "APPROVAL_TTL", -1.0)  # nobody answered in time
    assert api.http.get(f"/v1/approvals/{second}", headers=api.auth("ingest")).json()["status"] == "expired"
    assert decide(second, True).status_code == 409  # too late to say yes


def test_a_human_override_is_applied_logged_and_announced(api):
    api.http.put("/api/fleet", headers=api.auth("admin"), json=FLEET).raise_for_status()
    r = api.http.post(
        "/api/agents/coder/autonomy", headers=api.auth("operate"), json={"level": "suspended", "reason": "odd commits"}
    )
    assert r.json() == {"agent": "coder", "autonomy": "suspended"}
    detail = api.http.get("/api/agents/coder", headers=api.auth("read")).json()
    assert detail["agent"]["autonomy"] == "suspended" and detail["events"][0]["actor"].startswith("key:")
    incident = api.http.get("/api/incidents", headers=api.auth("read")).json()["incidents"][0]
    assert incident["severity"] == "critical" and incident["subject"] == "agent:coder"
    assert api.http.get("/api/audit", headers=api.auth("admin")).json()["audit"][0]["action"] == "autonomy.set"


def test_metrics_escape_hostile_agent_names(api):
    _, body = journey(agent='x"} 1\nthemis_agent_autonomy{agent="admin')
    api.ingest(body)
    judge(api)
    text = api.http.get("/metrics", headers=api.auth("read")).text
    lines = [line for line in text.splitlines() if line.startswith("themis_agent_autonomy")]
    assert len(lines) == 1 and '\\"' in lines[0]  # one sample, not the two the name tried to forge


# ---------------------------------------------------------------- when the database is down


def test_a_database_outage_answers_retry_later_everywhere_and_recovers_by_itself(api):
    """503 + Retry-After is what makes OTLP exporters and the gate client try again. A 500 makes an exporter drop spans."""
    outage = DatabaseOutage(api.engine)
    _, body = journey()
    with outage:
        for r in (
            api.ingest(body),
            api.http.get("/api/overview", headers=api.auth("read")),
            api.http.get("/v1/approvals/x", headers=api.auth("ingest")),
            api.http.get("/readyz"),  # Kubernetes takes the pod out of the Service; nothing is restarted
        ):
            assert r.status_code == 503, r.text
        assert api.ingest(body).headers["retry-after"] == "2"
        assert api.http.get("/healthz").status_code == 200  # the process is fine, so the liveness probe passes
    assert api.ingest(body).status_code == 200  # no restart needed
    assert api.http.get("/readyz").status_code == 200


def test_metrics_report_latency_per_route_template_not_per_path(api):
    """Named the way OpenTelemetry names HTTP server metrics, so the SLO alerts IntentOps renders can read them."""
    api.http.get("/api/agents/coder", headers=api.auth("read"))
    api.http.get("/api/agents/somebody-else", headers=api.auth("read"))
    api.http.get("/app.js")
    text = api.http.get("/metrics").text
    tag = 'http_route="/api/agents/{name}",http_request_method="GET",http_response_status_code="404"'
    assert f"http_server_request_duration_seconds_count{{{tag}}} 2" in text
    assert f'http_server_request_duration_seconds_bucket{{{tag},le="+Inf"}} 2' in text
    assert 'http_route="/api/agents/coder"' not in text and 'http_route="static"' in text
    assert "themis_evaluator_lag_seconds" in text and 'themis_webhook_deliveries_total{result="sent"}' in text


def test_every_metric_the_committed_alert_rules_read_is_one_themis_emits(api):
    """deploy/kubernetes/slo-rules.yaml (rendered by IntentOps) and deploy/alerts.yaml must read real metrics.

    Before this test existed, the SLO rules read a metric Themis did not emit, so they could never fire.
    """
    import re
    from pathlib import Path

    import yaml

    api.http.get("/api/overview", headers=api.auth("read"))
    api.http.get("/api/overview")  # a 401, so there is a 4xx series too
    exposed = {line.split("{")[0].split(" ")[0] for line in api.http.get("/metrics").text.splitlines() if line and line[0] != "#"}
    deploy = Path(__file__).parent.parent / "deploy"
    rules = []
    for path in (deploy / "kubernetes" / "slo-rules.yaml", deploy / "alerts.yaml"):
        for group in yaml.safe_load(path.read_text())["spec"]["groups"]:
            rules += group["rules"]
    recorded = {r["record"] for r in rules if "record" in r}
    read = set()
    for r in rules:
        for name in re.findall(r"([a-z_:][a-z0-9_:]*)\s*(?:\{|\[)", r["expr"]):
            read.add(re.sub(r"_(bucket|count|sum)$", "", name) if name.startswith("http_server") else name)
    unknown = {
        m for m in read - recorded if m not in {re.sub(r"_(bucket|count|sum)$", "", e) for e in exposed} and m not in exposed
    }
    assert not unknown, f"alert rules read metrics Themis never emits: {sorted(unknown)}"
    assert "http_server_request_duration_seconds" in read  # the IntentOps SLO rules really are covered
    labels = re.search(r"http_server_request_duration_seconds_count\{([^}]*)\}", api.http.get("/metrics").text).group(1)
    assert "http_response_status_code=" in labels  # the label the error-ratio rules filter on
