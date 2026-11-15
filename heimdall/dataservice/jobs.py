"""Durable, idempotent jobs with an explicit state machine, persisted history, leases and backoff scheduling."""

from __future__ import annotations

import json
import random
from datetime import UTC, datetime, timedelta

from .db import ServiceDB
from .inventory import now_iso
from .schemas import ACTIVE_RUNNING, TERMINAL, TRANSITIONS, JobState, ServiceConfig, canonical, digest


class IllegalTransition(Exception):
    pass


def parse_ts(s: str | None) -> datetime | None:
    return datetime.fromisoformat(s) if s else None


def backoff_delay(attempt: int, cfg: ServiceConfig, rng: random.Random) -> float:
    """Exponential backoff with jitter: base * 2^(attempt-1), capped, plus up to ``jitter`` x that value."""
    base = min(cfg.backoff_cap_s, cfg.backoff_base_s * 2 ** max(0, attempt - 1))
    return base * (1.0 + cfg.backoff_jitter * rng.random())


class JobQueue:
    def __init__(self, db: ServiceDB, cfg: ServiceConfig, clock=lambda: datetime.now(UTC), rng: random.Random | None = None) -> None:
        self.db, self.cfg, self.clock = db, cfg, clock
        self.rng = rng or random.Random()

    # -- submission -------------------------------------------------------------------------------
    @staticmethod
    def job_id(kind: str, payload: dict) -> str:
        return f"job:{kind.lower()}:{digest([kind, payload], 12)}"

    def submit(self, kind: str, payload: dict, *, deadline_s: float | None = None, revive_complete: bool = False) -> tuple[dict, bool]:
        """Idempotent: the same (kind, payload) returns the existing active or completed job. Returns (job, created_or_revived).

        A job that ended in ``UNAVAILABLE_AFTER_DEADLINE`` or ``CANCELLED`` is revived (generation + 1, fresh deadline) by a *new* submission,
        with the old history kept; the service never revives it silently on its own.
        """
        jid = self.job_id(kind, payload)
        now = self.clock()
        with self.db.transaction() as db:
            row = db.conn.execute("SELECT * FROM jobs WHERE job_id=?", (jid,)).fetchone()
            if row is None:
                db.conn.execute("INSERT INTO jobs(job_id,kind,payload,state,generation,created_at,updated_at,deadline_at,attempts,progress) VALUES(?,?,?,?,?,?,?,?,0,?)",
                                (jid, kind, canonical(payload), JobState.QUEUED, 1, now_iso(now), now_iso(now),
                                 now_iso(now + timedelta(seconds=deadline_s or self.cfg.deadline_s)), "{}"))
                db.conn.execute("INSERT INTO job_events(job_id,at,from_state,to_state,reason) VALUES(?,?,?,?,?)", (jid, now_iso(now), None, JobState.QUEUED, "submitted"))
                created = True
            elif JobState(row["state"]) in (JobState.UNAVAILABLE_AFTER_DEADLINE, JobState.CANCELLED) or (revive_complete and JobState(row["state"]) is JobState.COMPLETE):
                db.conn.execute("UPDATE jobs SET state=?, generation=generation+1, updated_at=?, deadline_at=?, attempts=0, next_attempt_at=NULL, lease_until=NULL, reason=? WHERE job_id=?",
                                (JobState.QUEUED, now_iso(now), now_iso(now + timedelta(seconds=deadline_s or self.cfg.deadline_s)), "resubmitted", jid))
                db.conn.execute("INSERT INTO job_events(job_id,at,from_state,to_state,reason) VALUES(?,?,?,?,?)", (jid, now_iso(now), row["state"], JobState.QUEUED, "resubmitted by a new request (data no longer present locally)" if row["state"] == "COMPLETE" else "resubmitted by a new request"))
                created = True
            else:
                created = False
        return self.get(jid), created

    # -- reads ------------------------------------------------------------------------------------
    def get(self, job_id: str) -> dict | None:
        r = self.db.one("SELECT * FROM jobs WHERE job_id=?", (job_id,))
        return self._dict(r) if r else None

    @staticmethod
    def _dict(r) -> dict:
        d = dict(r)
        for k in ("payload", "last_error", "result", "progress"):
            d[k] = json.loads(d[k]) if d.get(k) else (None if k in ("last_error", "result") else {})
        d["state"] = JobState(d["state"])
        return d

    def events(self, job_id: str) -> list[dict]:
        return [dict(r) | {"detail": json.loads(r["detail"]) if r["detail"] else None} for r in
                self.db.query("SELECT at,from_state,to_state,reason,detail FROM job_events WHERE job_id=? ORDER BY seq", (job_id,))]

    def list(self, states: set[JobState] | None = None, kind: str | None = None) -> list[dict]:
        rows = self.db.query("SELECT * FROM jobs ORDER BY created_at, job_id")
        out = [self._dict(r) for r in rows]
        return [j for j in out if (states is None or j["state"] in states) and (kind is None or j["kind"] == kind)]

    def due(self, now: datetime | None = None) -> list[dict]:
        """Jobs ready to run: QUEUED; PREPARING_DATA whose retry time has come; running states whose lease expired (crash recovery)."""
        now = now or self.clock()
        out = []
        for j in self.list():
            s = j["state"]
            if s is JobState.QUEUED:
                out.append(j)
            elif s is JobState.PREPARING_DATA and (parse_ts(j["next_attempt_at"]) or now) <= now:
                out.append(j)
            elif s in ACTIVE_RUNNING and (parse_ts(j["lease_until"]) or now) <= now:
                out.append(j)
        return out

    # -- transitions ----------------------------------------------------------------------------------
    def transition(self, job_id: str, to: JobState, *, reason: str = "", detail: dict | None = None, error: dict | None = None,
                   next_attempt_at: datetime | None = None, result: dict | None = None, progress: dict | None = None, lease: bool = False) -> dict:
        now = self.clock()
        with self.db.transaction() as db:
            row = db.conn.execute("SELECT state FROM jobs WHERE job_id=?", (job_id,)).fetchone()
            if row is None:
                raise KeyError(job_id)
            cur = JobState(row["state"])
            if cur != to and to not in TRANSITIONS[cur]:
                raise IllegalTransition(f"{cur} -> {to} is not allowed")
            sets, args = ["state=?", "updated_at=?", "reason=?", "lease_until=?"], [to, now_iso(now), reason or None,
                                                                                    now_iso(now + timedelta(seconds=self.cfg.lease_s)) if lease else None]
            sets.append("next_attempt_at=?")
            args.append(now_iso(next_attempt_at) if next_attempt_at else None)
            if error is not None:
                sets.append("last_error=?")
                args.append(canonical(error))
            if result is not None:
                sets.append("result=?")
                args.append(canonical(result))
            if progress is not None:
                sets.append("progress=?")
                args.append(canonical(progress))
            db.conn.execute(f"UPDATE jobs SET {', '.join(sets)} WHERE job_id=?", (*args, job_id))
            if cur != to or reason:
                db.conn.execute("INSERT INTO job_events(job_id,at,from_state,to_state,reason,detail) VALUES(?,?,?,?,?,?)",
                                (job_id, now_iso(now), cur, to, reason or None, canonical(detail) if detail else None))
        return self.get(job_id)  # type: ignore[return-value]

    def record_attempt(self, job_id: str) -> int:
        self.db.execute("UPDATE jobs SET attempts=attempts+1 WHERE job_id=?", (job_id,))
        return self.get(job_id)["attempts"]  # type: ignore[index]

    def schedule_retry(self, job: dict, *, error: dict, reason: str, min_delay_s: float = 0.0, progress: dict | None = None) -> dict:
        """PREPARING_DATA with the next attempt at now + backoff (>= ``min_delay_s``, e.g. an open circuit breaker's remaining cooldown).
        If the deadline would pass before that attempt, the job ends ``UNAVAILABLE_AFTER_DEADLINE`` -- a precise terminal state, never silence."""
        now = self.clock()
        attempts = job["attempts"]
        delay = max(backoff_delay(attempts, self.cfg, self.rng), min_delay_s)
        nxt = now + timedelta(seconds=delay)
        deadline = parse_ts(job["deadline_at"])
        if deadline is not None and now >= deadline:
            return self.transition(job["job_id"], JobState.UNAVAILABLE_AFTER_DEADLINE, reason="deadline reached while the archive was unavailable",
                                   error=error, progress=progress, detail={"attempts": attempts})
        return self.transition(job["job_id"], JobState.PREPARING_DATA, reason=reason, error=error, next_attempt_at=nxt, progress=progress,
                               detail={"attempts": attempts, "retry_in_s": round(delay, 1)})

    def cancel(self, job_id: str) -> dict:
        j = self.get(job_id)
        if j is None or j["state"] in TERMINAL:
            return j  # type: ignore[return-value]
        return self.transition(job_id, JobState.CANCELLED, reason="cancelled by request")

    def expired(self, job: dict) -> bool:
        d = parse_ts(job["deadline_at"])
        return d is not None and self.clock() >= d and job["state"] not in TERMINAL
