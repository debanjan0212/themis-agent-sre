"""The gate: what an agent runtime needs from Themis, in one small client.

The runtime keeps a snapshot of the control plane's decisions and refreshes it every second, so a check costs
nothing and agents keep working if Themis is briefly unreachable (they stay on the last known snapshot).
The one thing that fails closed is a high-risk approval: no answer means no.

    gate = Gate("http://themis:8600", key)
    gate.start()
    if gate.autonomy("deployer") == "suspended": ...           # do not call the agent
    if not gate.allowed("deployer", "tools", "deploy.apply"): ...  # outside its allow-list
    if gate.autonomy("deployer") == "supervised" and gate.high_risk("deployer", "deploy.apply"):
        go = await gate.approve("deployer", "tool:deploy.apply", {"change": "..."})
"""

from __future__ import annotations

import asyncio
import contextlib
import time

import httpx


class Gate:
    def __init__(self, base_url: str, key: str, refresh: float = 1.0):
        self.http = httpx.AsyncClient(base_url=base_url.rstrip("/"), headers={"Authorization": f"Bearer {key}"}, timeout=5.0)
        self.refresh = refresh
        self.snapshot: dict = {"agents": {}, "quarantine": {}, "fallbacks": {}, "faults": []}
        self.outbox: list[dict] = []
        self.task: asyncio.Task | None = None

    # ---- lifecycle

    async def sync(self) -> None:
        """Pull the latest decisions and push any policy events that are waiting."""
        self.snapshot = (await self.http.get("/v1/gate/snapshot")).raise_for_status().json()
        if self.outbox:
            batch, self.outbox = self.outbox[:500], self.outbox[500:]
            try:
                (await self.http.post("/v1/gate/events", json={"events": batch})).raise_for_status()
            except httpx.HTTPError:
                self.outbox = batch + self.outbox  # a lost denial would under-count violations: keep it for next time
                raise

    def start(self) -> None:
        async def loop():
            while True:
                with contextlib.suppress(httpx.HTTPError):  # control plane down: keep the last snapshot
                    await self.sync()
                await asyncio.sleep(self.refresh)

        self.task = asyncio.create_task(loop())

    async def close(self) -> None:
        if self.task:
            self.task.cancel()
        with contextlib.suppress(httpx.HTTPError):
            await self.sync()
        await self.http.aclose()

    # ---- questions a runtime asks before it acts

    def autonomy(self, agent: str) -> str:
        return self.snapshot["agents"].get(agent, {}).get("autonomy", "autonomous")

    def allowed(self, agent: str, kind: str, name: str) -> bool:
        """kind is "tools" or "handoffs". An agent with no allow-list declared is not restricted."""
        allow = self.snapshot["agents"].get(agent, {}).get(kind)
        return not allow or name in allow

    def high_risk(self, agent: str, tool: str) -> bool:
        return tool in self.snapshot["agents"].get(agent, {}).get("high_risk", ())

    def quarantined(self, component: str) -> bool:
        return self.snapshot["quarantine"].get(component, 0) > time.time()

    def fallback(self, model: str) -> str | None:
        return self.snapshot["fallbacks"].get(model)

    def fault(self, fault: str, target: str) -> float:
        """Chaos: the probability an injected fault should hit this target right now (0 when none is running)."""
        return max(
            (f["intensity"] for f in self.snapshot["faults"] if f["fault"] == fault and f["target"] == target), default=0.0
        )

    def report(self, agent: str, action: str, decision: str, reason: str = "") -> None:
        self.outbox.append({"agent": agent, "action": action, "decision": decision, "reason": reason})

    async def approve(self, agent: str, action: str, detail: dict, timeout: float = 30.0, poll: float = 0.2) -> bool:
        """Ask a human for go / no-go and wait. Any error, timeout or expiry is a no."""
        try:
            created = (
                await self.http.post("/v1/approvals", json={"agent": agent, "action": action, "detail": detail})
            ).raise_for_status()
            approval_id, deadline = created.json()["id"], time.time() + timeout
            while time.time() < deadline:
                status = (await self.http.get(f"/v1/approvals/{approval_id}")).raise_for_status().json()["status"]
                if status != "pending":
                    return status == "approved"
                await asyncio.sleep(poll)
        except httpx.HTTPError:
            pass
        return False
