"""Build site/index.html from site/page.html and the measured results.

    python site/build.py

Every number on the product page comes from docs/results.json (written by `themis bench`) and
docs/reliability.json (written by `scripts/soak.py`), so the page cannot claim more than was measured.
The imagery in site/assets was recorded from a live run of the dashboard by scripts/site_assets.py.
"""

from __future__ import annotations

import html
import json
import re
import tomllib
from pathlib import Path

SITE = Path(__file__).resolve().parent
REPO = SITE.parent


def load() -> dict:
    bench = json.loads((REPO / "docs" / "results.json").read_text())
    soak = json.loads((REPO / "docs" / "reliability.json").read_text())
    journey = json.loads((SITE / "assets" / "journey.json").read_text())
    return {"bench": bench, "soak": soak, "journey": journey}


def values(d: dict) -> dict:
    b, s, j = d["bench"], d["soak"], d["journey"]
    by_fault = {x["fault"]: x["result"] for x in b["experiments"]}
    right = sum(x["result"]["rca"]["all_correct"] for x in b["experiments"])
    total = sum(x["result"]["rca"]["all_labelled"] for x in b["experiments"])
    events = {e["what"]: e for e in s["events"]}
    db_s = events.get("Postgres stopped", {}).get("seconds")
    groups = s.get("by_exporter_timeout", {})
    recommended, default = groups.get("30s"), groups.get("10s")
    steady = {route: r["steady"] for route, r in s["routes"].items()}
    dc = s["double_count_check"]
    once = dc["task_events"] == dc["expected_task_events"] and dc["handoff_events"] == dc["expected_handoff_events"]
    deps = tomllib.loads((REPO / "pyproject.toml").read_text())["project"]["dependencies"]
    version = re.search(r'__version__ = "([^"]+)"', (REPO / "themis" / "__init__.py").read_text()).group(1)
    faults = "a crashed leader, a rolling redeploy" + (f" and a database outage of {db_s:g} s" if db_s else "")

    # the journey shown close up
    kv = j["kv"]
    took = kv[1]  # "186 ms across 15 steps"
    spent = kv[2]  # "$0.0035 on 3,062 tokens"
    cause = kv[3] if len(kv) > 3 else ""
    culprit, _, reason = cause.partition(". ")
    culprit = culprit.split(" (")[0].replace("agent:", "the ").replace("tool:", "the tool ").replace("model:", "the model ")
    tokens = spent.split(" on ")[1] if " on " in spent else spent

    def sec(fault: str) -> str:
        det = by_fault.get(fault, {}).get("detection", {})
        return f"{det['seconds']:.1f} s after the fault began" if det.get("detected") else "not detected in the run"

    rows = [
        ("root cause named correctly", f"{right:,} of {total:,} failed or stopped journeys ({right / total:.1%})", 1),
        ("rogue agent put under supervision", sec("rogue_agent"), 1),
        ("poisoned agent suspended", sec("context_poison"), 1),
        ("rate-limited model quarantined", sec("model_rate_limit"), 1),
        ("silently wrong tool quarantined", sec("tool_wrong_result"), 1),
        ("span ingest, one process", f"{b['ingest']['spans_per_second']:,} spans a second", 1),
        ("gate snapshot p95 under load", f"{steady['/v1/gate/snapshot']['p95_ms']:.0f} ms", 2),
        ("span ingest p95 under load", f"{steady['/v1/traces']['p95_ms']:.0f} ms", 2),
        ("dashboard overview p95 under load", f"{steady['/api/overview']['p95_ms']:.0f} ms", 2),
        ("availability outside the injected faults", f"{min(r['availability'] for r in steady.values()):.2%}", 2),
        (
            f"spans lost through {faults}",
            (
                f"{recommended['dropped']:,} with a 30 s exporter timeout, {default['dropped']:,} with the 10 s default"
                if recommended and default
                else f"{s['exporter']['dropped']:,} of {s['sent']['spans']:,}"
            ),
            2,
        ),
        ("journeys judged twice", "none" if once else "some", 2),
        ("new evaluator after the leader crashes", f"{events['new leader after crash']['seconds']:.1f} s", 2),
        ("handover on a rolling redeploy", f"{events['new leader after clean stop']['seconds']:.1f} s", 2),
        ("telemetry in", "OTLP over HTTP, OpenTelemetry GenAI conventions", 0),
        ("incidents out", "Alertmanager webhooks, signed with HMAC-SHA256", 0),
        ("storage", "SQLite on one machine, PostgreSQL for several replicas", 0),
        ("runtime dependencies", f"{len(deps)} Python packages", 0),
        ("licence", "MIT", 0),
    ]
    spec_rows = "\n            ".join(
        f'<tr><th scope="row">{html.escape(label)}</th><td>{html.escape(value)}{f"<sup>{note}</sup>" if note else ""}</td></tr>'
        for label, value, note in rows
    )
    st, ld = s["settings"], s["load"]
    journeys = sum(b["world"].get(k, 0) for k in ("success", "failed", "blocked"))
    note_bench = (
        f"themis bench: {b['users']} simulated users, seed {b['seed']}, {journeys:,} journeys across {len(b['final_autonomy'])} agents, {len(b['experiments'])} faults one after"
        f" another, SLO windows compressed {b['window_scale']} times, {b['database'].split('+')[0]}, {b['machine']}."
        " The models and tools are simulated; the control plane is the released code."
    )
    note_soak = (
        f"scripts/soak.py: {st['replicas']} replicas behind a load balancer on production settings ({st['eval_interval_s']:g} s evaluator"
        f" interval), {ld['journeys_per_second']:g} journeys a second ({ld['spans_per_second']:,.0f} spans) with {ld['gate_pollers']} gate"
        f" pollers and {ld['dashboard_viewers']} dashboard viewers for {st['seconds']:g} s, on PostgreSQL. The leader was killed with SIGKILL,"
        f" another stopped with SIGTERM{f', and PostgreSQL stopped for {db_s:g} s' if db_s else ''}. {s['machine']}, shared by"
        " every process. Latency measured at the load balancer."
    )
    lost = recommended["dropped"] if recommended else s["exporter"]["dropped"]
    return {
        "version": version,
        "rogue_s": f"{by_fault['rogue_agent']['detection']['seconds']:.1f}",
        "rca_right": f"{right:,}",
        "rca_total": f"{total:,}",
        "rca_pct": f"{right / total:.1%}",
        "lost_value": f"{lost:,} spans lost" if lost else "0 spans lost",
        "lost_label": f"through {faults}" + (", with a 30 s exporter timeout" if recommended else ""),
        "macro_caption": (
            f"One failed {j['title']} journey, recorded during a live run: {took}. Themis named {culprit} as the cause"
            + (f": {reason[0].lower() + reason[1:]}." if reason else ".")
        ),
        "used_tokens": tokens,
        "used_caption": f"{spent[0].upper() + spent[1:]} for the whole journey, split by agent, with the pod and node each one ran on.",
        "spec_rows": spec_rows,
        "agents": str(len(b["final_autonomy"])),
        "note_bench": html.escape(note_bench),
        "note_soak": html.escape(note_soak),
    }


def orrery(flat: bool) -> str:
    svg = (SITE / "assets" / "orrery.svg").read_text()
    if flat:  # the dissection lights the rim, which the dashboard draws no line for
        svg = svg.replace("</svg>", '<circle class="rimline" r="285"></circle></svg>', 1)
        for name in ("glow", "bloom", "orb"):  # the page holds the map twice; ids must stay unique
            svg = svg.replace(f'id="{name}"', f'id="{name}-2"').replace(f"url(#{name})", f"url(#{name}-2)")
    return svg


def render() -> str:
    v = values(load())
    page = (SITE / "page.html").read_text()
    escaped = {k: (val if k == "spec_rows" or k.startswith("note_") else html.escape(val)) for k, val in v.items()}
    escaped["orrery"], escaped["orrery_flat"] = orrery(False), orrery(True)
    out = re.sub(r"\{\{(\w+)\}\}", lambda m: escaped[m.group(1)], page)
    left = re.findall(r"\{\{\w+\}\}", out)
    if left:
        raise SystemExit(f"unfilled placeholders: {left}")
    return out


def build() -> Path:
    (SITE / "index.html").write_text(render())
    return SITE / "index.html"


if __name__ == "__main__":
    print(f"wrote {build()}")
