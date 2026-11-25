"""All configuration comes from environment variables. docs/CONFIGURATION.md lists every one."""

from __future__ import annotations

import os
import socket
import uuid
from dataclasses import dataclass


def _list(name: str) -> tuple[str, ...]:
    return tuple(x.strip() for x in os.getenv(name, "").split(",") if x.strip())


@dataclass(frozen=True)
class Settings:
    db_url: str = "sqlite:///themis.db"
    # Divides every SLO window. 1 = production (1h/5m burn windows). 120 turns one hour into 30 seconds for demos.
    window_scale: float = 1.0
    eval_interval: float = 15.0  # seconds between evaluator ticks
    settle_seconds: float = 2.0  # wait this long after a journey's root span ends before judging it
    retention_hours: float = 72.0
    max_body_bytes: int = 4_000_000
    rate_limit_per_sec: float = 300.0  # per API key, per replica
    store_content: bool = False  # prompts and tool payloads are dropped at ingest unless this is on
    webhooks: tuple[str, ...] = ()  # Alertmanager-format receivers: Aegis, Argus, Incident Commander, anything
    webhook_token: str = ""
    webhook_secret: str = ""
    webhook_allow_private: bool = False  # SSRF guard: private and loopback targets are refused unless this is on
    bootstrap_key: str = ""  # optional admin key supplied by a Kubernetes Secret
    cors_origins: tuple[str, ...] = ()
    external_url: str = "http://localhost:8600"
    instance: str = ""

    @classmethod
    def from_env(cls, **override) -> Settings:
        e = os.getenv
        values = dict(
            db_url=e("THEMIS_DB_URL", cls.db_url),
            window_scale=max(float(e("THEMIS_WINDOW_SCALE", "1")), 0.001),
            eval_interval=float(e("THEMIS_EVAL_INTERVAL", "15")),
            settle_seconds=float(e("THEMIS_SETTLE_SECONDS", "2")),
            retention_hours=float(e("THEMIS_RETENTION_HOURS", "72")),
            max_body_bytes=int(e("THEMIS_MAX_BODY_BYTES", "4000000")),
            rate_limit_per_sec=float(e("THEMIS_RATE_LIMIT", "300")),
            store_content=e("THEMIS_STORE_CONTENT", "0") == "1",
            webhooks=_list("THEMIS_WEBHOOKS"),
            webhook_token=e("THEMIS_WEBHOOK_TOKEN", ""),
            webhook_secret=e("THEMIS_WEBHOOK_SECRET", ""),
            webhook_allow_private=e("THEMIS_WEBHOOK_ALLOW_PRIVATE", "0") == "1",
            bootstrap_key=e("THEMIS_BOOTSTRAP_KEY", ""),
            cors_origins=_list("THEMIS_CORS_ORIGINS"),
            external_url=e("THEMIS_EXTERNAL_URL", cls.external_url).rstrip("/"),
            instance=e("THEMIS_INSTANCE", "") or f"{socket.gethostname()}-{uuid.uuid4().hex[:6]}",
        )
        values.update(override)
        return cls(**values)

    def scaled(self, seconds: float) -> float:
        """A window written in production seconds, as real seconds on this deployment.

        In production (scale 1) a window is exactly what it says. Under time compression a window could shrink
        below the time it takes to judge a journey (settle_seconds) and run the evaluator (eval_interval), and
        would then be empty most of the time, so compressed windows never go below a few of those cycles.
        """
        if self.window_scale == 1:
            return seconds
        return max(seconds / self.window_scale, 3 * (self.settle_seconds + self.eval_interval))

    @property
    def bucket_seconds(self) -> float:
        return 30.0 / self.window_scale
