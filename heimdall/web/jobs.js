// Durable job tracking (polling; the backend has no push channel). The service owns retries and recovery:
// this module never resubmits, only reconnects to job ids it was given. Job ids survive a reload in
// localStorage (a per-viewer convenience; the server remains the authority).
import { FAILURE } from "./api.js";

export const ACTIVE_MS = 3000;    // documented cadence: every 2-5 s while preparing
export const BACKOFF_MS = 5000;   // after a failed poll, still inside the documented window
export const DORMANT_MS = 60000;  // deadline passed and the service will re-check later: no fast polling
const STORAGE_KEY = "heimdall.jobs.v1";
const MAX_TRACKED = 30;
export const TERMINAL = new Set(["COMPLETE", "COMPLETED", "NO_COVERAGE", "INVALID_SOURCE_DATA", "CANCELLED", "FAILED", "UNAVAILABLE_AFTER_DEADLINE"]);

/** Reduce a GET /jobs/{id} answer (target result, data status or field status) to a display summary. */
export function summarize(data) {
  const job = data?.job || {};
  const result = data?.investigation_id ? { kind: "version", id: data.investigation_id }
    : data?.run?.run_id || data?.run_id ? { kind: "run", id: data.run?.run_id || data.run_id } : null;
  const readiness = data?.readiness || (result ? "COMPLETE" : null);
  const state = result ? "COMPLETE" : job.state || readiness || data?.status?.execution || "UNKNOWN";
  const recovery = job.auto_recovery || data?.auto_recovery || null;
  const terminal = Boolean(result) || TERMINAL.has(state) || TERMINAL.has(readiness);
  // An archive outage after the deadline is not final while the service still schedules recovery attempts.
  const dormant = (state === "UNAVAILABLE_AFTER_DEADLINE" || readiness === "UNAVAILABLE_AFTER_DEADLINE") && Boolean(recovery?.active || job.next_attempt_at)
    && data?.must_resubmit !== true;
  return {
    state, readiness, terminal: terminal && !dormant, dormant, result, kind: job.kind || data?.kind,
    reason: job.reason || data?.status?.reason || null, message: data?.message || null,
    nextAttempt: job.next_attempt_at || recovery?.next_check_at || null,
    progress: job.progress ?? null, waitingOn: job.progress?.waiting_on || job.waiting_on || null, childJob: job.progress?.child_job || null, mustResubmit: data?.must_resubmit === true,
    recovery, events: Array.isArray(data?.events) ? data.events : [], updatedAt: job.updated_at || null,
  };
}

export class JobTracker {
  constructor({ client, onChange = () => {}, storage = safeStorage(), timers = globalThis, doc = globalThis.document, now = () => Date.now() }) {
    Object.assign(this, { client, onChange, storage, timers, doc, now });
    this.jobs = new Map();
    this.paused = Boolean(doc?.hidden);
    for (const saved of this.load()) this.jobs.set(saved.id, { ...saved, summary: null, error: null, timer: null, inflight: false });
    doc?.addEventListener?.("visibilitychange", () => this.setPaused(Boolean(doc.hidden)));
    globalThis.addEventListener?.("online", () => this.resumeAll());
  }

  load() {
    try {
      const rows = JSON.parse(this.storage?.getItem(STORAGE_KEY) || "[]");
      return Array.isArray(rows) ? rows.filter((r) => r && typeof r.id === "string" && /^job:[a-z_]+:[0-9a-f]{12}$/.test(r.id)).slice(-MAX_TRACKED) : [];
    } catch { return []; }
  }

  save() {
    try {
      const rows = [...this.jobs.values()].map(({ id, label, added, origin }) => ({ id, label, added, origin }));
      this.storage?.setItem(STORAGE_KEY, JSON.stringify(rows.slice(-MAX_TRACKED)));
    } catch { /* storage may be unavailable: tracking continues for this tab */ }
  }

  track(id, { label = "", origin = null } = {}) {
    if (!this.jobs.has(id)) {
      this.jobs.set(id, { id, label, origin, added: new Date(this.now()).toISOString(), summary: null, error: null, timer: null, inflight: false });
      while (this.jobs.size > MAX_TRACKED) this.forget(this.jobs.keys().next().value);
      this.save();
    }
    this.schedule(id, 0);
    this.onChange(id);
    return this.jobs.get(id);
  }

  forget(id) {
    const job = this.jobs.get(id);
    if (!job) return;
    this.timers.clearTimeout(job.timer);
    this.jobs.delete(id);
    this.save();
    this.onChange(id);
  }

  list() { return [...this.jobs.values()].reverse(); }
  active() { return this.list().filter((j) => !j.summary?.terminal); }

  setPaused(paused) {
    this.paused = paused;
    if (paused) for (const job of this.jobs.values()) { this.timers.clearTimeout(job.timer); job.timer = null; }
    else this.resumeAll();
  }

  resumeAll() { for (const id of this.jobs.keys()) if (!this.jobs.get(id).summary?.terminal) this.schedule(id, 0); }

  schedule(id, delay) {
    const job = this.jobs.get(id);
    if (!job || this.paused) return;
    this.timers.clearTimeout(job.timer);
    job.timer = this.timers.setTimeout(() => this.poll(id), delay);
  }

  async poll(id) {
    const job = this.jobs.get(id);
    if (!job || job.inflight || this.paused) return;
    job.inflight = true;
    job.timer = null;
    let next = ACTIVE_MS;
    try {
      const data = await this.client.get(`/jobs/${encodeURIComponent(id).replaceAll("%3A", ":")}`, { fresh: true });
      job.data = data;
      job.summary = summarize(data);
      job.error = null;
      job.checkedAt = new Date(this.now()).toISOString();
      if (job.summary.dormant) {
        const at = Date.parse(job.summary.nextAttempt || "");
        next = Number.isFinite(at) ? Math.max(DORMANT_MS, Math.min(at - this.now(), 6 * 3600e3)) : DORMANT_MS;
      }
    } catch (e) {
      job.error = e;
      // A job unknown to this server (another state directory) is reported, not polled forever.
      if (e.kind === FAILURE.NOT_FOUND) job.summary = { ...(job.summary || {}), state: "NOT_ON_THIS_SERVER", terminal: true };
      next = e.kind === FAILURE.OFFLINE ? null : BACKOFF_MS;
    } finally {
      job.inflight = false;
    }
    if (this.jobs.get(id) !== job) return;
    this.onChange(id);
    if (!job.summary?.terminal && next !== null) this.schedule(id, next);
  }

  async cancel(id) {
    const data = await this.client.post(`/jobs/${encodeURIComponent(id).replaceAll("%3A", ":")}/cancel`, {});
    const job = this.jobs.get(id);
    if (job) { job.data = data; job.summary = summarize(data); this.onChange(id); }
    return data;
  }
}

function safeStorage() {
  try { return globalThis.localStorage; } catch { return null; }
}
