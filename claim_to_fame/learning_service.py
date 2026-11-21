"""Bounded public-settlement refresh between rounds; no runner, keys, or model calls.

The runner and optional watcher share a nonblocking process lock. Partial HTTP pages
are resumable private checkpoints; only complete, validated games become training data.
"""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
import tempfile
import threading
import time

import calibrate as C
from . import feedback


def _records(path):
    """Latest successful local record determines which public games are relevant."""
    records = {}
    if not Path(path).exists():
        return records
    for line in Path(path).read_text().splitlines():
        if not line.strip():
            continue
        try:
            rec = C.strict_json(line)
            gid = rec.get("game")
            if type(gid) is int and gid > 0 and not rec.get("dry"):
                records[gid] = rec
        except (ValueError, AttributeError):
            continue
    return {gid: rec for gid, rec in records.items() if rec.get("ok") is True and not rec.get("dry")}


def _safe_until(schedule, now, *, margin=20.0, round_seconds=60.0):
    upcoming = []
    for gid, start in schedule:
        if gid <= 0:
            continue
        if start - margin <= now <= start + round_seconds + 5:
            return now
        if start > now:
            upcoming.append(start - margin)
    return min(upcoming, default=float("inf"))


def refresh_once(team, rounds_path, cache_dir, output_path, report_path=None, *,
                 schedule=None, deadline=None, before_game=None, max_refresh=120.0,
                 margin=20.0, request_timeout=5.0, getter=None, clock=time.time,
                 status_path=None):
    """Fetch only completed locally recorded games and atomically publish nonregressing state.

    ``schedule`` may be a callable returning the latest [(id, start_timestamp)] list;
    its deadline is rechecked before each request, cache scan and artifact write.
    ``getter`` is a test transport with calibrate.get's public-only signature.
    Returns a diagnostic dict; failures preserve the previous learning artifact.
    """
    cache_dir, output_path = Path(cache_dir), Path(output_path)
    status_path = Path(status_path) if status_path else output_path.with_name("learning-status.json")
    started, hard_wall = clock(), time.time() + max_refresh
    hard_end = started + max_refresh
    schedule_fn = schedule if callable(schedule) else lambda: (schedule or [])

    def guard():
        now = clock()
        end = min(hard_end, _safe_until(schedule_fn(), now, margin=margin))
        external = deadline() if callable(deadline) else deadline
        if external is not None:
            end = min(end, external)
        remaining = min(end - now, hard_wall - time.time())
        if remaining <= 0:
            raise C.DeadlineExceeded("refresh deferred near a game window or budget limit")
        # C's helpers use wall time; injected clocks are solely for deterministic tests.
        return time.time() + remaining

    status = {"status": "running", "started_at": started, "fetched_games": [], "pending_games": [],
              "performance_reconciliation": "per-game public matrix"}
    try:
        guard()
        with C.cache_lock(cache_dir / ".learning-refresh.lock", deadline=guard):
            records = _records(rounds_path)
            guard()
            if not records:
                status.update(status="idle", reason="no successful local round records")
                return status
            schedule_now = [(gid, start) for gid, start in schedule_fn() if gid > 0]
            starts = dict(schedule_now)
            if before_game is None:
                not_finished = [gid for gid, start in schedule_now if start + 65 > clock()]
                cutoff = min(not_finished) if not_finished else max(records) + 1
            else:
                cutoff = before_game
            relevant = {gid: rec for gid, rec in records.items() if gid < cutoff
                        and (gid not in starts or starts[gid] + 65 < clock())}
            if not relevant:
                status.update(status="deferred", reason="no local rounds completed before the next game")
                return status
            public = C.completed_games(getter=getter, deadline=guard, timeout=request_timeout)
            ready = {g["id"] for g in public} & set(relevant)
            status["pending_games"] = sorted(set(relevant) - ready)
            if not ready:
                status.update(status="pending", reason="local results are not publicly completed yet")
                return status
            matrix = C.performance_matrix(getter=getter, deadline=guard, timeout=request_timeout)
            for gid in sorted(ready):
                guard()
                item_keys = relevant[gid].get("items", {})
                expected = {int(line) for line in item_keys} if item_keys else None
                try:
                    C.load_game(gid, team, cache_dir=cache_dir, getter=getter, deadline=guard,
                                timeout=request_timeout, expected_lines=expected, matrix=matrix)
                    status["fetched_games"].append(gid)
                except C.SettlementMismatch:
                    status.setdefault("quarantined_games", []).append(gid)
                except C.IncompleteGame:
                    status["pending_games"].append(gid)
            guard()
            previous = feedback.load_state(output_path, cutoff,
                                           now=dt.datetime.fromtimestamp(clock(), dt.timezone.utc),
                                           max_age_hours=float("inf"))  # Bookkeeping only; runner still enforces freshness.
            # Keep only explicitly completed IDs in this build. Legacy future/partial
            # cache files cannot enter by merely sharing a rounds.jsonl identifier.
            selected = [relevant[gid] for gid in sorted(ready) if gid in status["fetched_games"]]
            if not selected:
                status.update(status="pending", reason="no complete public settlement matrices")
                return status
            with tempfile.TemporaryDirectory(prefix="learning-build-", dir=cache_dir) as directory:
                filtered = Path(directory) / "rounds.jsonl"
                filtered.write_text("".join(json.dumps(rec, allow_nan=False) + "\n" for rec in selected))
                filtered.chmod(0o600)
                guard()
                state = feedback.build_state(filtered, cache_dir, team, before_game=cutoff,
                                             now=dt.datetime.fromtimestamp(clock(), dt.timezone.utc),
                                             previous_state=previous, checkpoint=guard,
                                             case_dir=Path(rounds_path).parent / "cases")
            guard()
            old_games = set(previous["games"]) if previous else set()
            new_games = set(state.get("games", []))
            if not new_games or not old_games <= new_games:
                status.update(status="preserved", reason="new state is empty or would lose reconciled games")
                return status
            # Stage both artifacts before publishing; a late deadline never installs
            # a newly built model. Learning JSON is the final authoritative pointer.
            state_text = json.dumps(state, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
            report = feedback.render_report(state) if report_path is not None else None
            guard()
            if report_path is not None:
                C.atomic_text(report_path, report, deadline=guard)
            C.atomic_json(output_path, json.loads(state_text), deadline=guard)
            status.update(status="updated", games=sorted(new_games),
                          validated_calibrations=sum(c.get("status") == "validated" for c in state["calibrations"]))
    except C.RefreshBusy:
        status.update(status="busy", reason="another process is refreshing learning")
    except C.DeadlineExceeded:
        status.update(status="deferred", reason="game window or refresh budget reached; completed pages retained")
    except Exception as exc:
        status.update(status="error", reason=type(exc).__name__)
    finally:
        status["finished_at"] = clock()
        try:
            guard()
            C.atomic_json(status_path, status, deadline=guard)
        except (C.DeadlineExceeded, OSError):
            pass
    return status


class LearningService:
    """Single background worker, explicit cooldown, and live schedule cancellation."""
    def __init__(self, team, rounds_path, cache_dir, output_path, report_path=None, *,
                 cooldown=120.0, max_refresh=120.0, margin=20.0, request_timeout=5.0,
                 getter=None, clock=time.time, status_path=None):
        self.args = (team, rounds_path, cache_dir, output_path, report_path)
        self.options = dict(max_refresh=max_refresh, margin=margin, request_timeout=request_timeout,
                            getter=getter, clock=clock, status_path=status_path)
        self.clock, self.cooldown, self.margin = clock, cooldown, margin
        self._lock = threading.Lock()
        self._schedule = []
        self._thread = None
        self._last_started = -float("inf")
        self._closed = False
        self.status = {"status": "idle"}

    def _current_schedule(self):
        with self._lock:
            if self._closed:
                raise C.DeadlineExceeded("learning service closed")
            return list(self._schedule)

    def tick(self, schedule, now=None):
        """Update the schedule and start a safe refresh without blocking the runner."""
        now = self.clock() if now is None else now
        with self._lock:
            self._schedule = list(schedule)
            if (self._closed or not self._schedule or (self._thread is not None and self._thread.is_alive())
                    or now - self._last_started < self.cooldown
                    or _safe_until(self._schedule, now, margin=self.margin) <= now + self.options["request_timeout"] + C.PAUSE):
                return False
            self._last_started = now
            self._thread = threading.Thread(target=self._work, name="learning-refresh", daemon=True)
            self._thread.start()
            return True

    def _work(self):
        result = refresh_once(*self.args, schedule=self._current_schedule, **self.options)
        with self._lock:
            self.status = result

    def close(self):
        """Cancel future work/requests immediately; never block live shutdown on HTTP."""
        with self._lock:
            self._closed = True

    @property
    def running(self):
        with self._lock:
            return self._thread is not None and self._thread.is_alive()
