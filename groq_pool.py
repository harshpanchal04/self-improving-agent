"""
groq_pool.py — Multi-key Groq client pool with round-robin rotation and 429 failover.

Usage:
    from groq_pool import GroqClientPool
    pool = GroqClientPool()
    response = pool.call(model=..., messages=..., ...)

Key resolution order (from .env):
    GROQ_API_KEY_1 .. GROQ_API_KEY_5  (primary pool, round-robin)
    GROQ_API_KEY                       (fallback if no numbered keys are set)

Rotation policy:
    - Round-robin by default.
    - On 429 from the current key: immediately rotate to the next key and retry.
    - A key that 429'd is put on cooldown for COOLDOWN_CALLS calls (default 3).
      During cooldown it is skipped; the pool tries other keys first.
    - Backoff sleep only fires if ALL keys are currently on cooldown (full-pool 429).
    - Each call is logged at DEBUG level with the key index that served it.
"""

from __future__ import annotations

import logging
import time
from collections import deque

import groq
from groq import Groq

import config

logger = logging.getLogger(__name__)

# Number of subsequent calls to skip a key after it 429s
COOLDOWN_CALLS = 3

# Backoff when every key is on cooldown (seconds)
FULL_POOL_BACKOFF = 5.0


class GroqClientPool:
    """
    Pool of Groq clients, one per API key, with round-robin dispatch and
    immediate key rotation on 429 errors.
    """

    def __init__(self) -> None:
        keys = self._load_keys()
        if not keys:
            raise RuntimeError(
                "No Groq API keys found. Set GROQ_API_KEY (or GROQ_API_KEY_1 .. "
                "GROQ_API_KEY_5) in your .env file."
            )

        self._clients: list[Groq] = [Groq(api_key=k) for k in keys]
        self._n = len(self._clients)

        # Cooldown counter per key: how many more calls to skip this key.
        # 0 = ready; > 0 = cooling down (decremented on each call attempt).
        self._cooldown: list[int] = [0] * self._n

        # Round-robin cursor (deque of indices in rotation order)
        self._rotation: deque[int] = deque(range(self._n))

        logger.debug("GroqClientPool initialised with %d key(s).", self._n)

    # ── Key loading ───────────────────────────────────────────────────────────

    @staticmethod
    def _load_keys() -> list[str]:
        """
        Load API keys from environment/config.

        Tries GROQ_API_KEY_1 .. GROQ_API_KEY_5 first; falls back to
        GROQ_API_KEY (singular) if none of the numbered ones are set.
        """
        import os

        numbered: list[str] = []
        for i in range(1, 6):
            val = os.environ.get(f"GROQ_API_KEY_{i}", "").strip()
            if val:
                numbered.append(val)

        if numbered:
            logger.debug("Loaded %d numbered Groq API key(s).", len(numbered))
            return numbered

        # Fallback: GROQ_API_KEY (already loaded by config.py via dotenv)
        fallback = (config.GROQ_API_KEY or "").strip()
        if fallback:
            logger.debug("Using fallback GROQ_API_KEY (single key).")
            return [fallback]

        return []

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _next_ready_index(self) -> int | None:
        """
        Return the index of the next ready (not cooling down) key, rotating
        the deque so future calls continue round-robin from after this key.
        Returns None if all keys are on cooldown.
        """
        for _ in range(self._n):
            idx = self._rotation[0]
            if self._cooldown[idx] > 0:
                # Decrement and skip — this counts as a "call" for cooldown purposes
                self._cooldown[idx] -= 1
                self._rotation.rotate(-1)
            else:
                # This key is ready; rotate past it for next time
                self._rotation.rotate(-1)
                return idx
        return None  # all keys cooling down

    def _mark_cooldown(self, idx: int) -> None:
        """Put key at index on cooldown for COOLDOWN_CALLS calls."""
        self._cooldown[idx] = COOLDOWN_CALLS
        logger.debug("Key[%d] put on cooldown for %d calls.", idx, COOLDOWN_CALLS)

    # ── Public API ────────────────────────────────────────────────────────────

    def call(self, **kwargs) -> groq.types.chat.ChatCompletion:
        """
        Call chat.completions.create(**kwargs) using the pool.

        Rotation policy:
        1. Pick the next ready key (round-robin, skipping cooled-down keys).
        2. If the call succeeds → return result.
        3. If 429 → mark key on cooldown, rotate to next ready key, retry.
        4. If ALL keys are on cooldown after a full rotation pass → sleep
           FULL_POOL_BACKOFF seconds, then retry from step 1 (up to MAX_RETRIES
           total 429 cycles before raising).
        5. Any non-429 error is raised immediately (no rotation for these).
        """
        attempts = 0
        max_attempts = self._n * config.MAX_RETRIES  # generous upper bound

        while attempts < max_attempts:
            idx = self._next_ready_index()

            if idx is None:
                # All keys are on cooldown — back off before retrying
                logger.debug(
                    "All %d key(s) on cooldown. Sleeping %.1fs before retry.",
                    self._n,
                    FULL_POOL_BACKOFF,
                )
                time.sleep(FULL_POOL_BACKOFF)
                attempts += 1
                continue

            client = self._clients[idx]
            logger.debug("Pool: key[%d] serving call (attempt %d).", idx, attempts + 1)

            try:
                result = client.chat.completions.create(**kwargs)
                logger.debug("Pool: key[%d] succeeded.", idx)
                return result

            except groq.RateLimitError:
                logger.debug(
                    "Pool: key[%d] got 429. Rotating to next key.", idx
                )
                self._mark_cooldown(idx)
                attempts += 1
                # Immediately continue the loop — no sleep here; we rotate first
                continue

            # Non-429 errors (auth, bad request, etc.) raise immediately
            # No except needed — they propagate naturally

        raise groq.RateLimitError(
            f"All {self._n} Groq key(s) exhausted after {max_attempts} attempts.",
            response=None,  # type: ignore[arg-type]
            body=None,
        )

    @property
    def pool_size(self) -> int:
        """Number of API keys in the pool."""
        return self._n


# ── Module-level singleton (imported by agent.py, judge.py, improver.py) ─────
# Instantiated once; all modules share the same pool and round-robin state.
_pool: GroqClientPool | None = None


def get_pool() -> GroqClientPool:
    """Return the module-level singleton pool, creating it on first call."""
    global _pool
    if _pool is None:
        _pool = GroqClientPool()
    return _pool
