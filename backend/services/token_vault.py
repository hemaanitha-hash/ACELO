"""
In-memory, per-run holding of the delegated tokens a user supplied when they
STARTED a run, so the backend — not a browser tab — can follow that run to the
end (status polling, OneLake result read, approval tracking import).

Guarantees:
  * Memory only. Never written to the database, a file or a log line.
  * Scoped to one run id; discarded when the run reaches a terminal state.
  * Expiry-aware: a token past its own `exp` claim is never used. When it
    expires the worker stops and the run keeps its last observed state; the
    frontend then advances it with fresh tokens (poll-on-read). Nothing is
    ever marked failed because a token expired.
  * Lost on backend restart by design — the persisted run and poll-on-read
    recover it; tokens are not something to persist.
"""

from __future__ import annotations

import base64
import json
import threading
import time
from dataclasses import dataclass

# Treat a token as expired a little early so a poll never starts with one about to lapse.
_SKEW_SECONDS = 60
# If a token's expiry cannot be read, assume a conservative lifetime.
_DEFAULT_LIFETIME_SECONDS = 45 * 60


@dataclass
class RunTokens:
    fabric: str | None = None
    onelake: str | None = None
    fabric_expires_at: float = 0.0
    onelake_expires_at: float = 0.0


_lock = threading.Lock()
_vault: dict[str, RunTokens] = {}


def _expiry(token: str | None) -> float:
    """The token's `exp` claim (JWT payload, not verified — only used to stop early)."""
    if not token:
        return 0.0
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        exp = json.loads(base64.urlsafe_b64decode(payload)).get("exp")
        if isinstance(exp, (int, float)):
            return float(exp)
    except Exception:  # noqa: BLE001 - opaque token: fall back to a default lifetime
        pass
    return time.time() + _DEFAULT_LIFETIME_SECONDS


def store(run_id: str, fabric: str | None, onelake: str | None = None) -> None:
    if not fabric and not onelake:
        return
    with _lock:
        current = _vault.get(run_id, RunTokens())
        if fabric:
            current.fabric, current.fabric_expires_at = fabric, _expiry(fabric)
        if onelake:
            current.onelake, current.onelake_expires_at = onelake, _expiry(onelake)
        _vault[run_id] = current


def fabric_token(run_id: str) -> str | None:
    with _lock:
        tokens = _vault.get(run_id)
        if tokens and tokens.fabric and tokens.fabric_expires_at - _SKEW_SECONDS > time.time():
            return tokens.fabric
    return None


def onelake_token(run_id: str) -> str | None:
    with _lock:
        tokens = _vault.get(run_id)
        if tokens and tokens.onelake and tokens.onelake_expires_at - _SKEW_SECONDS > time.time():
            return tokens.onelake
    return None


def has_usable_token(run_id: str) -> bool:
    return fabric_token(run_id) is not None


def discard(run_id: str) -> None:
    with _lock:
        _vault.pop(run_id, None)


def clear() -> None:
    """Tests only."""
    with _lock:
        _vault.clear()
