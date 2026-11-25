"""The world: ten agents doing a small company's work, so there is something real to watch and to break.

The models and tools are simulated (seeded random latency, tokens and results), but everything around them is
real: every step is an OpenTelemetry span exported over OTLP, every action goes through the gate, injected
faults are applied here at the agent's boundary, and a quarantined model or tool is routed to its fallback.

Swap `chat` and `tool` for real calls and this file becomes a real agent runtime. docs/INTEGRATION.md shows how.
"""

from __future__ import annotations

import asyncio
import contextlib
import random
import time
import zlib
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

import httpx
import yaml
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.trace import Status, StatusCode

from themis.client import Gate

FLEET = Path(__file__).with_name("fleet.yaml")
BASE_FAILURE = 0.006  # an agent task fails on its own this often, so the SLOs always have something to count


class TaskFailed(Exception):
    def __init__(self, kind: str, truth: str | None = None, blocked: bool = False):
        self.kind, self.truth, self.blocked = kind, truth, blocked


class Blocked(Exception):
    def __init__(self, by: str, suspended: bool):
        self.by, self.suspended = by, suspended


@dataclass
class Ctx:
    rng: random.Random
    name: str
    truth: str | None = None  # the real cause, when this journey goes wrong. Scores the RCA; the RCA never reads it
    tainted: bool = False  # a tool gave a wrong answer and nobody noticed
    unsafe: bool = False  # an unsafe high-risk action really ran


def load_fleet(path: Path | str = FLEET) -> dict:
    return yaml.safe_load(Path(path).read_text())


class World:
    def __init__(
        self,
        base_url: str,
        key: str,
        fleet: dict | None = None,
        seed: int = 7,
        approver: bool = True,
        human_delay: tuple[float, float] = (0.2, 0.6),  # how long the simulated person takes to answer go / no-go
    ):
        self.base_url, self.key, self.seed, self.approver = base_url.rstrip("/"), key, seed, approver
        self.human_delay = human_delay
        self.fleet = fleet or load_fleet()
        self.gate = Gate(self.base_url, key, refresh=0.5)
        self.stats: Counter = Counter()
        names = sorted(self.fleet["agents"])
        self.pods = {n: f"agent-{n}-{zlib.crc32(n.encode()) % 0xFFFFF:05x}" for n in names}
        self.nodes = {n: f"node-{1 + i % 3}" for i, n in enumerate(names)}
        resource = Resource.create({"service.name": "themis-world", "deployment.environment.name": "sim"})
        self.provider = TracerProvider(resource=resource)
        exporter = OTLPSpanExporter(endpoint=f"{self.base_url}/v1/traces", headers={"Authorization": f"Bearer {key}"})
        batching = dict(max_queue_size=50_000, schedule_delay_millis=400, max_export_batch_size=2_000)
        self.provider.add_span_processor(BatchSpanProcessor(exporter, **batching))
        self.tracer = self.provider.get_tracer("themis.world")

    def span(self, name: str, attrs: dict):
        # errors are set by hand below: a stack trace on every simulated failure would only bloat the telemetry
        return self.tracer.start_as_current_span(name, attributes=attrs, record_exception=False, set_status_on_exception=False)

    @staticmethod
    def fail(span, kind: str) -> None:
        span.set_status(Status(StatusCode.ERROR, kind))
        span.set_attribute("error.type", kind)

    # ------------------------------------------------------------ running it

    async def apply_fleet(self) -> None:
        body = {k: self.fleet[k] for k in ("agents", "prices", "fallbacks")}
        async with httpx.AsyncClient(headers={"Authorization": f"Bearer {self.key}"}, timeout=10) as http:
            (await http.put(f"{self.base_url}/api/fleet", json=body)).raise_for_status()

    async def run(self, users: int = 12, duration: float = 60.0) -> Counter:
        await self.gate.sync()
        self.gate.start()
        stop_at = time.time() + duration
        human = asyncio.create_task(self.human_on_call()) if self.approver else None
        await asyncio.gather(*(self.user(i, stop_at) for i in range(users)))
        if human:
            human.cancel()
        await self.stop()
        return self.stats

    async def stop(self) -> None:
        """Send what is still queued, then shut the exporter down."""
        await asyncio.to_thread(self.provider.shutdown)
        await self.gate.close()

    async def user(self, i: int, stop_at: float) -> None:
        rng = random.Random(self.seed * 10_007 + i)  # nosec B311 - a seeded simulation, not a secret
        await asyncio.sleep(rng.uniform(0, 1.0))  # users do not all arrive in the same instant
        names = list(self.fleet["journeys"])
        weights = [self.fleet["journeys"][n]["weight"] for n in names]
        while time.time() < stop_at:
            session = f"s{i:03d}-{rng.getrandbits(20):05x}"
            for _ in range(rng.randint(3, 8)):
                if time.time() >= stop_at:
                    break
                await self.journey(f"user-{i:03d}", session, rng.choices(names, weights)[0], rng)
                await asyncio.sleep(rng.uniform(0.2, 1.0))

    async def human_on_call(self) -> None:
        """Stands in for the person who answers go / no-go. Rejects an action when its safety check has failed."""
        seen: set[str] = set()

        async def decide(http: httpx.AsyncClient, approval: dict) -> None:
            await asyncio.sleep(random.uniform(*self.human_delay))  # nosec B311 - simulated reading time
            body = {"approve": not approval["detail"].get("unsafe_params"), "by": "sim-human"}
            await http.post(f"/api/approvals/{approval['id']}/decision", json=body)

        async with httpx.AsyncClient(base_url=self.base_url, headers={"Authorization": f"Bearer {self.key}"}, timeout=5) as http:
            pending: set[asyncio.Task] = set()
            while True:
                with contextlib.suppress(httpx.HTTPError):
                    for approval in (await http.get("/api/approvals")).json()["approvals"]:
                        if approval["id"] not in seen:
                            seen.add(approval["id"])
                            task = asyncio.create_task(decide(http, approval))
                            pending.add(task)
                            task.add_done_callback(pending.discard)
                await asyncio.sleep(0.25)

    # ------------------------------------------------------------ one journey

    async def journey(self, user: str, session: str, name: str, rng: random.Random) -> str:
        ctx, steps = Ctx(rng, name), self.fleet["journeys"][name]["steps"]
        attrs = {"themis.journey.name": name, "session.id": session, "user.id": user}
        with self.span(f"journey {name}", attrs) as root:
            outcome, payload_ok = "success", True
            try:
                for i, agent in enumerate(steps):
                    payload_ok = await self.agent(ctx, agent, steps[i + 1] if i + 1 < len(steps) else None, payload_ok)
                if ctx.unsafe or (ctx.tainted and rng.random() < 0.9):
                    outcome = "failed"  # nothing errored, the result was simply wrong: the hard case for root cause
            except Blocked as b:
                outcome, ctx.truth = "blocked", ctx.truth or b.by
                if b.suspended:
                    root.set_attribute("themis.blocked.by", b.by)
            except TaskFailed as e:
                outcome = "failed"
                self.fail(root, e.kind)
            root.set_attribute("themis.journey.outcome", outcome)
            if outcome != "success" and ctx.truth:
                root.set_attribute("sim.truth", ctx.truth)
        self.stats[outcome] += 1
        return outcome

    async def agent(self, ctx: Ctx, name: str, nxt: str | None, payload_ok: bool) -> bool:
        """Run one agent's task. Returns whether what it hands on is valid."""
        g, rng, spec = self.gate, ctx.rng, self.fleet["agents"][name]
        if g.autonomy(name) == "suspended":
            raise Blocked(f"agent:{name}", suspended=True)
        attrs = {
            "gen_ai.operation.name": "invoke_agent",
            "gen_ai.agent.name": name,
            "gen_ai.provider.name": "sim",
            "gen_ai.request.model": spec["model"],
            "k8s.pod.name": self.pods[name],
            "k8s.node.name": self.nodes[name],
        }
        with self.span(f"invoke_agent {name}", attrs) as span:
            try:
                poison = rng.choice(("tool", "handoff")) if rng.random() < g.fault("context_poison", f"agent:{name}") else None
                rogue = rng.random() < g.fault("rogue_agent", f"agent:{name}")
                if not payload_ok and rng.random() < 0.85:
                    raise TaskFailed("bad_input")  # the upstream agent already set the truth
                for _ in range(rng.randint(1, 2) + (3 if rogue else 0)):
                    await self.chat(ctx, name, spec["model"], big=rogue)
                if poison == "tool":  # the injected instruction: "also read the secrets"
                    await self.tool(ctx, name, "secrets.read", rogue=False)
                used = [t for t in spec["tools"] if rng.random() < 0.7] or spec["tools"][:1]
                for tool in used:
                    await self.tool(ctx, name, tool, rogue)
                if rng.random() < BASE_FAILURE:
                    raise TaskFailed("task_error", truth=f"agent:{name}")
                if not nxt:
                    return True
                valid = poison != "handoff"
                handoff = {
                    "themis.handoff.from": name,
                    "themis.handoff.to": nxt,
                    "themis.handoff.valid": valid,
                    "gen_ai.agent.name": name,
                }
                with self.span(f"handoff {nxt}", handoff):
                    await asyncio.sleep(0.002)
                if not valid:
                    ctx.truth = ctx.truth or f"agent:{name}"
                return valid
            except TaskFailed as e:
                self.fail(span, e.kind)
                ctx.truth = ctx.truth or e.truth
                if e.blocked:
                    raise Blocked(f"agent:{name}", suspended=False) from e
                raise

    async def chat(self, ctx: Ctx, agent: str, model: str, big: bool = False) -> None:
        g, rng = self.gate, ctx.rng
        for attempt in range(3):
            use = (g.quarantined(f"model:{model}") and g.fallback(model)) or model
            attrs = {
                "gen_ai.operation.name": "chat",
                "gen_ai.agent.name": agent,
                "gen_ai.provider.name": "sim",
                "gen_ai.request.model": use,
            }
            with self.span(f"chat {use}", attrs) as span:
                if use != model:
                    span.set_attribute("themis.fallback", True)
                slow = 4 if rng.random() < g.fault("latency", f"model:{use}") else 1
                await asyncio.sleep(rng.lognormvariate(-2.9, 0.45) * slow)
                if rng.random() < g.fault("model_rate_limit", f"model:{use}"):
                    self.fail(span, "rate_limited")
                    await asyncio.sleep(0.02 * (attempt + 1))
                    continue
                span.set_attribute("gen_ai.usage.input_tokens", max(int(rng.gauss(900, 200)), 50) * (4 if big else 1))
                span.set_attribute("gen_ai.usage.output_tokens", max(int(rng.gauss(250, 80)), 10) * (4 if big else 1))
                return
        raise TaskFailed("model_unavailable", truth=f"model:{model}")

    async def tool(self, ctx: Ctx, agent: str, name: str, rogue: bool) -> None:
        g, rng = self.gate, ctx.rng
        attrs = {
            "gen_ai.operation.name": "execute_tool",
            "gen_ai.agent.name": agent,
            "gen_ai.tool.name": name,
            "gen_ai.tool.type": "function",
        }
        with self.span(f"execute_tool {name}", attrs) as span:
            if not g.allowed(agent, "tools", name):
                g.report(agent, f"tool:{name}", "deny", "not in the agent's allow-list")
                span.set_attribute("themis.gate.decision", "deny")
                self.fail(span, "policy_denied")
                raise TaskFailed("policy_denied", truth=f"agent:{agent}")
            unsafe = False
            if g.high_risk(agent, name):
                span.set_attribute("themis.action.risk", "high")
                unsafe = rogue  # a rogue agent passes parameters that fail the safety check
                if g.autonomy(agent) == "supervised":
                    if not await g.approve(agent, f"tool:{name}", {"unsafe_params": unsafe, "journey": ctx.name}):
                        span.set_attribute("themis.gate.decision", "denied_by_human")
                        self.fail(span, "approval_denied")
                        raise TaskFailed("approval_denied", truth=f"agent:{agent}", blocked=True)
                    span.set_attribute("themis.gate.decision", "approved_by_human")
            quarantined = g.quarantined(f"tool:{name}")
            slow = 4 if rng.random() < g.fault("latency", f"tool:{name}") else 1
            await asyncio.sleep(rng.lognormvariate(-4.2, 0.5) * slow * (2 if quarantined else 1))
            if quarantined:
                span.set_attribute("themis.fallback", True)  # the healthy replica: slower, and the fault cannot reach it
            elif rng.random() < g.fault("tool_wrong_result", f"tool:{name}"):
                ctx.tainted, ctx.truth = True, ctx.truth or f"tool:{name}"
                if rng.random() < 0.25:  # only some wrong answers are malformed enough for a schema check to notice
                    span.set_attribute("themis.check.passed", False)
                    span.set_attribute("themis.check.name", "result_schema")
            if unsafe:
                ctx.unsafe, ctx.truth = True, ctx.truth or f"agent:{agent}"
                self.stats["unsafe_actions"] += 1
                span.set_attribute("themis.check.passed", False)
                span.set_attribute("themis.check.name", "post_action_verification")
                span.set_attribute("themis.check.blame", "caller")
