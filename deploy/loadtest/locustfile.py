"""
agent-platform B-M6 load test — agent session end-to-end.

Simulates the real user journey observed in apps/web/src/hooks/useChat.ts:
  1. POST /api/bff/v1/auth/login          — obtain session cookie
  2. POST /api/bff/v1/sessions            — create a new session
  3. POST /api/bff/v1/sessions/{id}/chat  — send a user message + consume SSE
     stream until the 'done' event (this is the run end-to-end).
  4. (optional) GET /api/bff/v1/sessions/{id}/messages — verify transcript

Metrics collected
-----------------
  TTFT (time-to-first-token)      — time from POST /chat until the first
                                    'token' SSE event arrives.
  Run completion time             — time from POST /chat until the 'done'
                                    SSE event (or EOF).
  Per-phase HTTP latencies         — Locust native request table.
  Error rate                      — Locust native failure counter.

SLO thresholds (configurable via env)
--------------------------------------
  LOAD_SLO_P95_TTFT_MS    default 3000  ms  (P95 TTFT < 3 s)
  LOAD_SLO_P95_TOTAL_MS   default 8000  ms  (P95 run total < 8 s, per B-M6 SLO)
  LOAD_SLO_ERROR_RATE_PCT default 1     %

Usage — quick smoke (local, 1 user / 30 s)
-------------------------------------------
  pip install locust
  locust -f deploy/loadtest/locustfile.py \\
    --headless -u 1 -r 1 -t 30s \\
    --host http://localhost:8005 \\
    --html deploy/loadtest/report_smoke.html

Usage — staging SLO run (100 users / 24 h)
-------------------------------------------
  locust -f deploy/loadtest/locustfile.py \\
    --headless -u 100 -r 5 -t 86400s \\
    --host https://staging.your-domain.example \\
    --html deploy/loadtest/report_staging.html \\
    --csv deploy/loadtest/results/staging_$(date +%Y%m%d_%H%M%S)

Environment variables
----------------------
  LOAD_EMAIL            test user email   (default: loadtest@example.com)
  LOAD_PASSWORD         test user password (default: loadtest_pass_changeme)
  LOAD_PROMPT           agent prompt to send (default: lightweight deterministic)
  LOAD_SLO_P95_TTFT_MS  P95 TTFT threshold in ms (default: 3000)
  LOAD_SLO_P95_TOTAL_MS P95 run-total threshold in ms (default: 8000)
  LOAD_SLO_ERROR_RATE_PCT error rate threshold % (default: 1.0)

Multi-account pool
-------------------
Set LOAD_ACCOUNTS to a JSON array for 100-concurrent scenarios where you want
distinct credentials per virtual-user group.  Example:
  LOAD_ACCOUNTS='[["user1@example.com","pw1"],["user2@example.com","pw2"]]'
When set, each Locust user picks an account round-robin from the pool.
When not set, all users share the single LOAD_EMAIL / LOAD_PASSWORD account
(the server issues per-session cookies so this is safe for moderate concurrency).
"""

from __future__ import annotations

import json
import logging
import os
import statistics
import threading
import time
from typing import Iterator

from locust import HttpUser, between, events, task
from locust.runners import MasterRunner, WorkerRunner

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────────────────────────
# Configuration
# ──────────────────────────────────────────────────────────────────────────────

_DEFAULT_EMAIL = "loadtest@example.com"
_DEFAULT_PASSWORD = "loadtest_pass_changeme"
_DEFAULT_PROMPT = (
    "Reply with exactly one sentence: 'Load test OK.' Do not add anything else."
)

LOAD_EMAIL: str = os.environ.get("LOAD_EMAIL", _DEFAULT_EMAIL)
LOAD_PASSWORD: str = os.environ.get("LOAD_PASSWORD", _DEFAULT_PASSWORD)
LOAD_PROMPT: str = os.environ.get("LOAD_PROMPT", _DEFAULT_PROMPT)

SLO_P95_TTFT_MS: float = float(os.environ.get("LOAD_SLO_P95_TTFT_MS", "3000"))
SLO_P95_TOTAL_MS: float = float(os.environ.get("LOAD_SLO_P95_TOTAL_MS", "8000"))
SLO_ERROR_RATE_PCT: float = float(os.environ.get("LOAD_SLO_ERROR_RATE_PCT", "1.0"))

# Optional multi-account pool  [ ["email","pw"], ... ]
_ACCOUNTS_RAW: str = os.environ.get("LOAD_ACCOUNTS", "")
_ACCOUNT_POOL: list[tuple[str, str]] = []
if _ACCOUNTS_RAW:
    try:
        parsed = json.loads(_ACCOUNTS_RAW)
        _ACCOUNT_POOL = [(row[0], row[1]) for row in parsed]
    except Exception:
        logger.warning("LOAD_ACCOUNTS is not valid JSON — using single account")

_account_counter = 0
_account_lock = threading.Lock()


def _next_account() -> tuple[str, str]:
    """Round-robin account from pool, or fall back to singleton."""
    if not _ACCOUNT_POOL:
        return (LOAD_EMAIL, LOAD_PASSWORD)
    global _account_counter
    with _account_lock:
        idx = _account_counter % len(_ACCOUNT_POOL)
        _account_counter += 1
    return _ACCOUNT_POOL[idx]


# ──────────────────────────────────────────────────────────────────────────────
# Custom metric collectors
# ──────────────────────────────────────────────────────────────────────────────

_ttft_samples: list[float] = []       # ms
_run_total_samples: list[float] = []  # ms
_samples_lock = threading.Lock()


def _record_ttft(ms: float) -> None:
    with _samples_lock:
        _ttft_samples.append(ms)


def _record_run_total(ms: float) -> None:
    with _samples_lock:
        _run_total_samples.append(ms)


def _percentile(data: list[float], pct: float) -> float:
    if not data:
        return float("nan")
    sorted_data = sorted(data)
    idx = int(len(sorted_data) * pct / 100)
    idx = min(idx, len(sorted_data) - 1)
    return sorted_data[idx]


# ──────────────────────────────────────────────────────────────────────────────
# SSE stream parser
# ──────────────────────────────────────────────────────────────────────────────

def _parse_sse_lines(lines: list[str]) -> Iterator[tuple[str, str]]:
    """Parse SSE text/event-stream lines into (event_type, data_json) tuples."""
    event_type = ""
    for line in lines:
        if line.startswith("event:"):
            event_type = line[len("event:"):].strip()
        elif line.startswith("data:") and event_type:
            data = line[len("data:"):].strip()
            yield event_type, data
            event_type = ""


# ──────────────────────────────────────────────────────────────────────────────
# Locust User
# ──────────────────────────────────────────────────────────────────────────────

class AgentSessionUser(HttpUser):
    """Simulates one concurrent agent session user."""

    # Wait between consecutive tasks: 1–3 s (think time).
    # For pure throughput testing set to between(0, 0).
    wait_time = between(1, 3)

    def on_start(self) -> None:
        """Login once per virtual user and persist the session cookie."""
        email, password = _next_account()
        with self.client.post(
            "/api/bff/v1/auth/login",
            json={"email": email, "password": password},
            catch_response=True,
            name="auth/login",
        ) as resp:
            if resp.status_code >= 400:
                resp.failure(f"login failed: HTTP {resp.status_code} — {resp.text[:200]}")
                # Re-raise so Locust marks this user as failed to start.
                raise RuntimeError(f"Login failed for {email}")
            resp.success()
        # The BFF sets an HttpOnly cookie; requests.Session tracks it automatically.

    @task
    def agent_session_run(self) -> None:
        """Full journey: create session → send message → consume SSE → assert."""
        # Step 1: create session
        with self.client.post(
            "/api/bff/v1/sessions",
            json={},
            catch_response=True,
            name="sessions/create",
        ) as resp:
            if resp.status_code not in (200, 201):
                resp.failure(f"create_session HTTP {resp.status_code}")
                return
            session = resp.json()
            session_id = session.get("id")
            if not session_id:
                resp.failure("create_session: no id in response")
                return
            resp.success()

        # Step 2: POST /chat and consume the SSE stream.
        # We use stream=True to read line-by-line without buffering the whole body.
        chat_url = f"/api/bff/v1/sessions/{session_id}/chat"
        run_start_ts = time.monotonic()
        first_token_ts: float | None = None
        done_received = False
        error_detail: str | None = None
        bytes_received = 0

        try:
            with self.client.post(
                chat_url,
                json={"message": LOAD_PROMPT, "attachment_ids": []},
                stream=True,
                catch_response=True,
                name="chat/send+stream",
                timeout=120,
            ) as resp:
                if resp.status_code >= 400:
                    resp.failure(f"chat POST HTTP {resp.status_code}: {resp.text[:200]}")
                    return

                buffer = ""
                for chunk in resp.iter_content(chunk_size=None, decode_unicode=True):
                    if chunk:
                        bytes_received += len(chunk)
                        buffer += chunk
                        lines = buffer.split("\n")
                        buffer = lines.pop()  # keep incomplete last line

                        for event_type, data_raw in _parse_sse_lines(lines):
                            now = time.monotonic()

                            if event_type == "token" and first_token_ts is None:
                                first_token_ts = now
                                ttft_ms = (first_token_ts - run_start_ts) * 1000
                                _record_ttft(ttft_ms)
                                # Fire a Locust request event so TTFT appears
                                # in the built-in stats table.
                                events.request.fire(
                                    request_type="SSE",
                                    name="chat/ttft",
                                    response_time=ttft_ms,
                                    response_length=0,
                                    exception=None,
                                    context={},
                                )

                            elif event_type == "done":
                                done_received = True
                                total_ms = (now - run_start_ts) * 1000
                                _record_run_total(total_ms)
                                events.request.fire(
                                    request_type="SSE",
                                    name="chat/run_total",
                                    response_time=total_ms,
                                    response_length=bytes_received,
                                    exception=None,
                                    context={},
                                )
                                break  # stop reading after done

                            elif event_type == "error":
                                try:
                                    payload = json.loads(data_raw)
                                    error_detail = payload.get("detail", data_raw)
                                except json.JSONDecodeError:
                                    error_detail = data_raw

                # If SSE ended without 'done' (e.g. timeout, server drop)
                if not done_received and not error_detail:
                    total_ms = (time.monotonic() - run_start_ts) * 1000
                    _record_run_total(total_ms)

                if error_detail:
                    resp.failure(f"agent error event: {error_detail}")
                elif not done_received:
                    resp.failure("SSE stream ended without 'done' event")
                else:
                    resp.success()

        except Exception as exc:
            # Locust catches uncaught exceptions from tasks but we log for
            # debugging and record a failure.
            logger.warning("agent_session_run exception: %s", exc)
            events.request.fire(
                request_type="SSE",
                name="chat/run_total",
                response_time=(time.monotonic() - run_start_ts) * 1000,
                response_length=0,
                exception=exc,
                context={},
            )


# ──────────────────────────────────────────────────────────────────────────────
# SLO report on test end
# ──────────────────────────────────────────────────────────────────────────────

@events.quitting.add_listener
def _slo_report(environment, **kwargs):
    """Print SLO pass/fail report when Locust exits."""
    # Skip on workers in distributed mode — master collects global stats.
    if isinstance(environment.runner, WorkerRunner):
        return

    with _samples_lock:
        ttft = list(_ttft_samples)
        totals = list(_run_total_samples)

    p95_ttft = _percentile(ttft, 95)
    p95_total = _percentile(totals, 95)

    stats = environment.stats
    total_reqs = stats.total.num_requests
    total_fails = stats.total.num_failures
    error_rate = (total_fails / total_reqs * 100) if total_reqs > 0 else 0.0

    ttft_ok = p95_ttft <= SLO_P95_TTFT_MS
    total_ok = p95_total <= SLO_P95_TOTAL_MS
    error_ok = error_rate <= SLO_ERROR_RATE_PCT
    all_ok = ttft_ok and total_ok and error_ok

    sep = "=" * 60
    print(f"\n{sep}")
    print("  B-M6 SLO REPORT")
    print(sep)
    print(f"  Samples collected : TTFT={len(ttft)}, run_total={len(totals)}")
    print(f"  P95 TTFT          : {p95_ttft:>8.1f} ms  (threshold {SLO_P95_TTFT_MS:.0f} ms)  {'PASS' if ttft_ok else 'FAIL'}")
    print(f"  P95 run total     : {p95_total:>8.1f} ms  (threshold {SLO_P95_TOTAL_MS:.0f} ms)  {'PASS' if total_ok else 'FAIL'}")
    print(f"  Error rate        : {error_rate:>8.2f} %   (threshold {SLO_ERROR_RATE_PCT:.1f} %)        {'PASS' if error_ok else 'FAIL'}")
    print(sep)
    print(f"  Overall SLO       : {'PASS' if all_ok else 'FAIL'}")
    print(sep + "\n")

    if not all_ok:
        # Signal non-zero exit code so CI fails.
        environment.process_exit_code = 1
