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

    def submit(self, kind: str, payload: dict, *, deadline_s: float | None = None, revive_complete: bool = False, revive_terminal: bool = True,
               internal: bool = False) -> tuple[dict, bool]:
        """Idempotent: the same (kind, payload) returns the existing active or completed job. Returns (job, created_or_revived).

        A job that ended in ``UNAVAILABLE_AFTER_DEADLINE`` or ``CANCELLED`` is revived (generation + 1, fresh deadline) by a *new* submission,
        with the old history kept. ``UNAVAILABLE_AFTER_DEADLINE`` jobs are also revived by the recorded recovery policy (``recover_due``).
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
            elif not revive_terminal and JobState(row["state"]) in (JobState.UNAVAILABLE_AFTER_DEADLINE, JobState.CANCELLED, JobState.FAILED):
                created = False  # status-only callers (e.g. map coverage polling) report the terminal state; they never revive it
            elif internal and JobState(row["state"]) is JobState.UNAVAILABLE_AFTER_DEADLINE:
                # a dependent job (a parent's worker step) needs this child again: short attempt, recovery window unchanged
                db.conn.execute("UPDATE jobs SET state=?, generation=generation+1, updated_at=?, deadline_at=?, attempts=0, next_attempt_at=NULL, lease_until=NULL, reason=? WHERE job_id=?",
                                (JobState.QUEUED, now_iso(now), now_iso(now + timedelta(seconds=self.cfg.recovery_attempt_s)), "revived for a dependent job", jid))
                db.conn.execute("INSERT INTO job_events(job_id,at,from_state,to_state,reason) VALUES(?,?,?,?,?)",
                                (jid, now_iso(now), row["state"], JobState.QUEUED, "revived for a dependent job (recovery window unchanged)"))
                created = True
            elif JobState(row["state"]) in (JobState.UNAVAILABLE_AFTER_DEADLINE, JobState.CANCELLED, JobState.FAILED) or (revive_complete and JobState(row["state"]) is JobState.COMPLETE):
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
        """Filtered in SQL (indexed on state), so the worker loop does not decode the whole job history on every wake."""
        where, args = [], []
        if states is not None:
            if not states:
                return []
            where.append(f"state IN ({','.join('?' * len(states))})")
            args += [str(x) for x in sorted(states)]
        if kind is not None:
            where.append("kind=?")
            args.append(kind)
        sql = "SELECT * FROM jobs" + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY created_at, job_id"
        return [self._dict(r) for r in self.db.query(sql, tuple(args))]

    def count_open(self) -> int:
        return int(self.db.one(f"SELECT COUNT(*) FROM jobs WHERE state NOT IN ({','.join('?' * len(TERMINAL))})", tuple(str(x) for x in TERMINAL))[0])

    def due(self, now: datetime | None = None) -> list[dict]:
        """Jobs ready to run: QUEUED; PREPARING_DATA whose retry time has come; running states whose lease expired (crash recovery)."""
        now = now or self.clock()
        out = []
        for j in self.list({JobState.QUEUED, JobState.PREPARING_DATA} | set(ACTIVE_RUNNING)):
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

    # -- service-owned recovery after a deadline -------------------------------------------------------------
    _REQUEST_REASONS = ("submitted", "resubmitted by a new request")

    def recovery_info(self, job: dict) -> dict | None:
        """For an ``UNAVAILABLE_AFTER_DEADLINE`` job: whether the service will re-check it on its own, when, and until when.

        The window starts at the first deadline reached since the last *user* (re)submission, so automatic recovery attempts that also
        hit their deadline do not extend it."""
        if job["state"] is not JobState.UNAVAILABLE_AFTER_DEADLINE:
            return None
        events = self.events(job["job_id"])
        last_req = max((i for i, e in enumerate(events) if (e["reason"] or "").startswith(self._REQUEST_REASONS)), default=-1)
        first = next((parse_ts(e["at"]) for e in events[last_req + 1:] if e["to_state"] == JobState.UNAVAILABLE_AFTER_DEADLINE), None)
        first = first or parse_ts(job["updated_at"])
        until = first + timedelta(seconds=self.cfg.recovery_window_s)
        nxt = parse_ts(job["updated_at"]) + timedelta(seconds=self.cfg.recovery_interval_s)
        active = self.cfg.recovery_window_s > 0 and nxt < until
        return {"active": active, "next_check_at": now_iso(nxt) if active else None, "until": now_iso(until),
                "policy": f"re-check every {self.cfg.recovery_interval_s:.0f} s for {self.cfg.recovery_window_s:.0f} s after the first deadline; "
                          f"each recovery attempt has a {self.cfg.recovery_attempt_s:.0f} s deadline"}

    def recover_due(self, now: datetime | None = None) -> list[str]:
        """Revive ``UNAVAILABLE_AFTER_DEADLINE`` jobs whose low-frequency recovery check is due (generation + 1, short deadline, event logged)."""
        now = now or self.clock()
        revived = []
        for j in self.list({JobState.UNAVAILABLE_AFTER_DEADLINE}):
            info = self.recovery_info(j)
            if not info or not info["active"] or parse_ts(info["next_check_at"]) > now:
                continue
            with self.db.transaction() as db:
                cur = db.conn.execute("SELECT state FROM jobs WHERE job_id=?", (j["job_id"],)).fetchone()
                if cur is None or cur["state"] != JobState.UNAVAILABLE_AFTER_DEADLINE:
                    continue  # changed concurrently
                db.conn.execute("UPDATE jobs SET state=?, generation=generation+1, updated_at=?, deadline_at=?, attempts=0, next_attempt_at=NULL, lease_until=NULL, reason=? WHERE job_id=?",
                                (JobState.QUEUED, now_iso(now), now_iso(now + timedelta(seconds=self.cfg.recovery_attempt_s)), "automatic recovery check", j["job_id"]))
                db.conn.execute("INSERT INTO job_events(job_id,at,from_state,to_state,reason,detail) VALUES(?,?,?,?,?,?)",
                                (j["job_id"], now_iso(now), JobState.UNAVAILABLE_AFTER_DEADLINE, JobState.QUEUED,
                                 "automatic recovery check (service-owned policy; no user request)", canonical({"recovery_until": info["until"]})))
            revived.append(j["job_id"])
        return revived

    MAX_SOFTWARE_ERRORS = 3

    def fail_or_retry(self, job: dict, exc: BaseException, *, progress: dict | None = None) -> dict:
        """A software/request error in a job step: request errors fail at once; unexpected errors are retried a bounded number of times and
        then end in FAILED -- never left to run into the archive deadline and be reported as an outage."""
        from ..contracts import ErrorCode, HeimdallError
        err = {"code": "UNEXPECTED", "message": f"{type(exc).__name__}: {exc}"}
        if isinstance(exc, HeimdallError) and exc.code in (ErrorCode.INPUT_INVALID, ErrorCode.CONFIG_INVALID):
            return self.transition(job["job_id"], JobState.FAILED, reason="request or configuration error", error=err | {"code": str(exc.code)})
        n = 1 + sum(1 for e in self.events(job["job_id"]) if (e["reason"] or "").startswith("unexpected error"))
        if n >= self.MAX_SOFTWARE_ERRORS:
            return self.transition(job["job_id"], JobState.FAILED, reason=f"software error repeated {n} times", error=err)
        return self.schedule_retry(job, error=err, reason=f"unexpected error ({n}/{self.MAX_SOFTWARE_ERRORS}); retrying automatically",
                                   progress=progress or {"waiting_on": "retry"})

    def must_resubmit(self, job: dict | None) -> bool:
        """True only when the service will take no further action on its own and a new request is the only way to try again."""
        if job is None:
            return False
        if job["state"] in (JobState.CANCELLED, JobState.FAILED):
            return True
        if job["state"] is JobState.UNAVAILABLE_AFTER_DEADLINE:
            return not (self.recovery_info(job) or {}).get("active", False)
        return False

    def cancel(self, job_id: str, *, reason: str = "cancelled by request") -> dict:
        j = self.get(job_id)
        if j is None:
            return j  # type: ignore[return-value]
        if j["state"] is JobState.UNAVAILABLE_AFTER_DEADLINE:  # cancelling also ends the automatic recovery policy
            out = self.transition(job_id, JobState.CANCELLED, reason=f"{reason} (automatic recovery stopped)")
        elif j["state"] in TERMINAL:
            return j
        else:
            out = self.transition(job_id, JobState.CANCELLED, reason=reason)
        self._cancel_orphaned_child(j)
        return out

    def _cancel_orphaned_child(self, parent: dict) -> None:
        """Propagate a cancellation to the child job the parent was waiting on, unless another open job is also waiting on it (shared work)."""
        child = (parent.get("progress") or {}).get("child_job")
        if not child:
            return
        c = self.get(child)
        if c is None or c["state"] in TERMINAL and c["state"] is not JobState.UNAVAILABLE_AFTER_DEADLINE:
            return
        others = [j for j in self.list(set(JobState) - set(TERMINAL)) if j["job_id"] != parent["job_id"] and (j["progress"] or {}).get("child_job") == child]
        if not others:
            self.cancel(child, reason=f"cancelled with its parent {parent['job_id']} (no other request was waiting on it)")

    def expired(self, job: dict) -> bool:
        d = parse_ts(job["deadline_at"])
        return d is not None and self.clock() >= d and job["state"] not in TERMINAL
