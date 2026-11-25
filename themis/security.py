"""API keys, scopes, rate limiting and outbound-URL checks."""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import secrets
import socket
import threading
from dataclasses import dataclass
from urllib.parse import urlsplit

# ingest: send telemetry, read the gate snapshot, ask for approvals.  read: the dashboard.
# operate: approve, demote, reinstate, run chaos.  admin: keys and the fleet manifest.
SCOPES = ("ingest", "read", "operate", "admin")


@dataclass(frozen=True)
class Principal:
    tenant_id: str
    key_id: str
    scopes: frozenset[str]


def new_key(prefix: str = "thm") -> tuple[str, str, str]:
    """(key id, full key, sha256 of the full key). The full key is shown once and never stored."""
    key_id = secrets.token_hex(6)
    key = f"{prefix}_{key_id}_{secrets.token_urlsafe(32)}"
    return key_id, key, digest(key)


def digest(key: str) -> str:
    # A plain hash is enough here: keys are 256 random bits, so there is nothing to brute-force.
    return hashlib.sha256(key.encode()).hexdigest()


def key_id_of(key: str) -> str | None:
    parts = key.split("_")
    return parts[1] if len(parts) >= 3 and len(parts[1]) == 12 else None


def same(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode(), b.encode())


def sign(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


class RateLimiter:
    """Token bucket per key. shortcut: counted per replica, move to Redis if one shared limit is needed."""

    def __init__(self, per_second: float):
        self.rate, self.capacity = per_second, per_second * 2
        self.state: dict[str, tuple[float, float]] = {}
        self.lock = threading.Lock()

    def allow(self, key: str, now: float, cost: float = 1.0) -> bool:
        with self.lock:
            if len(self.state) > 10_000:  # never let unknown keys grow this without bound
                self.state.clear()
            tokens, last = self.state.get(key, (self.capacity, now))
            tokens = min(self.capacity, tokens + (now - last) * self.rate)
            ok = tokens >= cost
            self.state[key] = (tokens - cost if ok else tokens, now)
            return ok


def check_webhook_url(url: str, allow_private: bool) -> None:
    """Refuse URLs that would let a webhook setting reach internal services (SSRF). Raises ValueError."""
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError("webhook URL must be http(s) with a host")
    if allow_private:
        return
    try:
        infos = socket.getaddrinfo(parts.hostname, parts.port or (443 if parts.scheme == "https" else 80))
    except OSError as e:
        raise ValueError(f"webhook host does not resolve: {parts.hostname}") from e
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if not ip.is_global:
            raise ValueError(
                f"webhook host {parts.hostname} resolves to a non-public address; set THEMIS_WEBHOOK_ALLOW_PRIVATE=1"
            )
