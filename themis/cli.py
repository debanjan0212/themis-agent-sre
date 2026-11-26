"""themis: the command line. `themis demo` is the place to start."""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import tempfile
import time
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="themis", description="SRE for agent fleets")
    sub = ap.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("demo", help="run Themis and a ten-agent world on this machine, then open the dashboard")
    d.add_argument("--port", type=int, default=8600)
    d.add_argument("--users", type=int, default=12)
    d.add_argument("--scale", type=float, default=120.0, help="time compression: 120 turns a 1h SLO window into 30s")
    d.add_argument("--duration", type=float, default=3600.0, help="seconds to run")
    d.add_argument("--chaos", action="store_true", help="inject the four faults one after another")

    b = sub.add_parser("bench", help="run the four chaos experiments under load and write docs/RESULTS.md")
    b.add_argument("--out", type=Path, default=Path("docs"))
    b.add_argument("--users", type=int, default=16)
    b.add_argument("--seed", type=int, default=7)
    b.add_argument("--duration", type=float, default=40.0, help="seconds per experiment")
    b.add_argument("--port", type=int, default=8611)
    b.add_argument("--db-url", default="", help="default: a temporary SQLite file")

    r = sub.add_parser("bench-real", help="real faults on a real cluster, worked by an on-call agent under Themis")
    r.add_argument("--faults", default="bad-deploy", help="comma separated, from: bad-deploy")
    r.add_argument("--runs", type=int, default=1, help="runs of each fault")
    r.add_argument("--brain", choices=("claude", "scripted"), default="claude", help="scripted: a fixed playbook, no model")
    r.add_argument("--model", default="claude-haiku-5-5")
    r.add_argument("--max-calls", type=int, default=30, help="hard cap on model calls per run")
    r.add_argument("--max-total-calls", type=int, default=200, help="stop starting runs once this many calls are spent")
    r.add_argument("--agent-python", required=True, help="the python of a virtualenv with agent-harness-sdk and Argus")
    r.add_argument("--gitops", type=Path, required=True, help="the GitOps repository the shop is deployed from")
    r.add_argument("--prometheus", default="http://127.0.0.1:9090")
    r.add_argument("--port", type=int, default=8621)
    r.add_argument("--out", type=Path, default=Path("bench-real-out"))
    r.add_argument(
        "--agent-script", type=Path, default=Path(__file__).resolve().parent.parent / "scripts" / "bench_real_agent.py"
    )

    s = sub.add_parser("serve", help="run the API and evaluator (configured by THEMIS_* environment variables)")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8600)

    k = sub.add_parser("key", help="create an API key. It is printed once and only its hash is stored")
    k.add_argument("--scopes", default="admin", help="comma separated: ingest, read, operate, admin")
    k.add_argument("--tenant", default="default")
    k.add_argument("--label", default="cli")

    w = sub.add_parser("world", help="run the ten-agent world against a Themis that is already running")
    w.add_argument("--url", default=os.getenv("THEMIS_URL", "http://127.0.0.1:8600"))
    w.add_argument("--users", type=int, default=12)
    w.add_argument("--duration", type=float, default=600.0)
    w.add_argument("--seed", type=int, default=7)

    c = sub.add_parser("chaos", help="start one chaos experiment")
    c.add_argument("fault")
    c.add_argument("target", help="for example model:sim-large, tool:kb.search or agent:coder")
    c.add_argument("--url", default=os.getenv("THEMIS_URL", "http://127.0.0.1:8600"))
    c.add_argument("--intensity", type=float, default=0.5)
    c.add_argument("--duration", type=float, default=40.0)

    args = ap.parse_args(argv)
    try:
        return run(args)
    except KeyboardInterrupt:
        return 130


def _key() -> str:
    key = os.getenv("THEMIS_KEY", "")
    if not key:
        sys.exit("set THEMIS_KEY to an API key first (create one with: themis key)")
    return key


def run(args) -> int:
    if args.cmd == "demo":
        from themis import lab

        with tempfile.TemporaryDirectory() as tmp:
            asyncio.run(lab.demo(args.port, args.users, args.scale, args.duration, args.chaos, f"{tmp}/themis-demo.db"))
    elif args.cmd == "bench":
        from themis import lab

        with tempfile.TemporaryDirectory() as tmp:
            db_url = args.db_url or f"sqlite:///{tmp}/themis-bench.db"
            asyncio.run(lab.bench(args.out, args.users, args.seed, args.duration, args.port, db_url))
        print(f"\nwrote {args.out / 'RESULTS.md'} and {args.out / 'results.json'}")
    elif args.cmd == "bench-real":
        from themis import benchreal

        unknown = [f for f in args.faults.split(",") if f not in benchreal.FAULTS]
        if unknown:
            sys.exit(f"unknown fault {unknown[0]!r}; choose from {', '.join(benchreal.FAULTS)}")
        cfg = dict(vars(args), faults=args.faults.split(","), out=str(args.out), gitops=str(args.gitops))
        result = benchreal.bench(cfg)
        print(f"\nwrote {args.out / 'RESULTS-real.md'} and {args.out / 'results-real.json'}")
        return 0 if result["trials"] and all(t["verdict"] != "agent failed" for t in result["trials"]) else 1
    elif args.cmd == "serve":
        import uvicorn

        from themis.api import create_app

        uvicorn.run(create_app(), host=args.host, port=args.port, log_level="info")
    elif args.cmd == "key":
        from themis.api import bootstrap, create_key
        from themis.db import insert_ignore, make_engine, tenants
        from themis.security import SCOPES
        from themis.settings import Settings

        scopes = [x.strip() for x in args.scopes.split(",") if x.strip()]
        if not scopes or set(scopes) - set(SCOPES):
            sys.exit(f"scopes must be from: {', '.join(SCOPES)}")
        s = Settings.from_env()
        engine = make_engine(s.db_url)
        bootstrap(engine, s)
        with engine.begin() as conn:
            insert_ignore(conn, tenants, {"id": args.tenant, "name": args.tenant, "config": {}})
            print(create_key(conn, args.tenant, scopes, args.label, time.time()))
    elif args.cmd == "world":
        from themis.world.sim import World

        async def go():
            world = World(args.url, _key(), seed=args.seed)
            await world.apply_fleet()
            print(dict(await world.run(args.users, args.duration)))

        asyncio.run(go())
    elif args.cmd == "chaos":
        import httpx

        body = {"fault": args.fault, "target": args.target, "intensity": args.intensity, "duration_s": args.duration}
        r = httpx.post(f"{args.url}/api/experiments", json=body, headers={"Authorization": f"Bearer {_key()}"}, timeout=10)
        print(r.status_code, r.text)
        return 0 if r.is_success else 1
    return 0
