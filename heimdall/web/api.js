// Transport for the existing /api/v1 contract (heimdall.http/1.0). Every failure becomes an ApiFailure with a
// distinct kind so the interface can explain it precisely; nothing here interprets science.

export const FAILURE = Object.freeze({
  OFFLINE: "offline",            // the browser reports no network
  UNREACHABLE: "unreachable",    // the HEIMDALL server did not answer (stopped, wrong origin)
  TIMEOUT: "timeout",
  ABORTED: "aborted",            // superseded by a newer request; never shown
  UNAUTHORIZED: "unauthorized",
  NOT_FOUND: "not_found",
  INVALID: "invalid_input",
  TOO_LARGE: "payload_too_large",
  QUEUE_FULL: "queue_full",
  OUTAGE: "upstream_outage",     // 503: an archive or catalogue is unavailable (not a scientific result)
  INTERNAL: "internal",
  MALFORMED: "malformed",        // the response was not the documented JSON
  HTTP: "http",
});

export class ApiFailure extends Error {
  constructor(kind, message, extra = {}) {
    super(message);
    this.name = "ApiFailure";
    this.kind = kind;
    Object.assign(this, extra);
  }
}

const BY_CODE = {
  INPUT_INVALID: FAILURE.INVALID, UNAUTHORIZED: FAILURE.UNAUTHORIZED, NOT_FOUND: FAILURE.NOT_FOUND,
  PAYLOAD_TOO_LARGE: FAILURE.TOO_LARGE, QUEUE_FULL: FAILURE.QUEUE_FULL, INTERNAL: FAILURE.INTERNAL,
  RECORD_UNREADABLE: FAILURE.INTERNAL,
};
const BY_STATUS = { 400: FAILURE.INVALID, 401: FAILURE.UNAUTHORIZED, 404: FAILURE.NOT_FOUND, 413: FAILURE.TOO_LARGE, 429: FAILURE.QUEUE_FULL, 503: FAILURE.OUTAGE };

/** Map an HTTP status and the documented error envelope to a failure kind. */
export function classify(status, payload) {
  const error = payload && typeof payload === "object" ? payload.error : null;
  if (status === 503 || error?.category === "UPSTREAM_OUTAGE") return FAILURE.OUTAGE;
  if (error?.code && BY_CODE[error.code]) return BY_CODE[error.code];
  if (BY_STATUS[status]) return BY_STATUS[status];
  return status >= 500 ? FAILURE.INTERNAL : FAILURE.HTTP;
}

const ADVICE = {
  [FAILURE.OFFLINE]: ["You are offline", "Saved records already open stay readable. Reconnect to load anything new."],
  [FAILURE.UNREACHABLE]: ["HEIMDALL server not reachable", "Start it with `python3 -m heimdall serve` and reload. Records already shown stay visible."],
  [FAILURE.TIMEOUT]: ["The server took too long", "Nothing was changed. Try again; durable jobs continue on the server either way."],
  [FAILURE.UNAUTHORIZED]: ["Access token required", "Enter the server's API token. It is kept only in this tab's memory."],
  [FAILURE.NOT_FOUND]: ["Record not found on this server", "Links only reopen on a server that holds the same saved data."],
  [FAILURE.INVALID]: ["The request was rejected as invalid", "Check the highlighted values; your entries were kept."],
  [FAILURE.TOO_LARGE]: ["Request too large", "Reduce the request size and try again."],
  [FAILURE.QUEUE_FULL]: ["The job queue is full", "Existing identical requests still join their job. Try a new request later."],
  [FAILURE.OUTAGE]: ["An upstream archive or catalogue is unavailable", "This is an availability state, not a scientific result. Saved results remain usable."],
  [FAILURE.INTERNAL]: ["The server reported an internal error", "Details are in the server log. Saved results are unaffected."],
  [FAILURE.MALFORMED]: ["Unexpected response format", "The server answered with data this workbench cannot read. Check that the server version matches."],
  [FAILURE.HTTP]: ["Request failed", "Try again."],
  [FAILURE.ABORTED]: ["Superseded", ""],
};

export function describe(error) {
  const kind = error?.kind || FAILURE.HTTP;
  const [title, action] = ADVICE[kind] || ADVICE[FAILURE.HTTP];
  return { kind, title, action, message: error?.message || title, code: error?.code, status: error?.status };
}

export const isAbort = (error) => error?.kind === FAILURE.ABORTED || error?.name === "AbortError";

function query(params) {
  if (!params) return "";
  const entries = Object.entries(params).filter(([, v]) => v !== undefined && v !== null && v !== "");
  return entries.length ? `?${new URLSearchParams(entries)}` : "";
}

// Resolve when `promise` does, or reject as ABORTED when `signal` fires; the shared request keeps running.
function abortable(promise, signal) {
  if (!signal) return promise;
  if (signal.aborted) return Promise.reject(new ApiFailure(FAILURE.ABORTED, "superseded"));
  return new Promise((resolve, reject) => {
    const stop = () => reject(new ApiFailure(FAILURE.ABORTED, "superseded"));
    signal.addEventListener("abort", stop, { once: true });
    promise.then((v) => { signal.removeEventListener("abort", stop); resolve(v); },
      (e) => { signal.removeEventListener("abort", stop); reject(e); });
  });
}

// Immutable resources: a saved version, run, manifest or report never changes once written.
export const IMMUTABLE = /^\/(investigations\/tinv:[^/]+(\/(claims|patterns|report))?|runs\/R\d{8}T\d{6}-[0-9a-f]{8}(\/skymap)?|manifests\/man:[0-9a-f]+)$/;

export class ApiClient {
  constructor({ base = "/api/v1", fetchImpl = (...a) => fetch(...a), timeoutMs = 15000, cacheSize = 48, online = () => navigator.onLine } = {}) {
    Object.assign(this, { base, fetchImpl, timeoutMs, cacheSize, online });
    this.token = "";
    this.inflight = new Map();
    this.cache = new Map();
    this.stats = { requests: 0, deduplicated: 0, cached: 0 };
  }

  url(path, params) { return `${this.base}${path}${query(params)}`; }

  async send(method, path, { params, body, timeoutMs = this.timeoutMs } = {}) {
    const headers = { Accept: "application/json" };
    if (this.token) headers.Authorization = `Bearer ${this.token}`;
    if (body !== undefined) headers["Content-Type"] = "application/json";
    this.stats.requests++;
    let response;
    try {
      response = await this.fetchImpl(this.url(path, params), {
        method, headers, body: body === undefined ? undefined : JSON.stringify(body), signal: AbortSignal.timeout(timeoutMs),
      });
    } catch (e) {
      if (e?.name === "TimeoutError") throw new ApiFailure(FAILURE.TIMEOUT, `No answer within ${Math.round(timeoutMs / 1000)} s`);
      if (this.online() === false) throw new ApiFailure(FAILURE.OFFLINE, "The browser is offline");
      throw new ApiFailure(FAILURE.UNREACHABLE, "The HEIMDALL server could not be reached");
    }
    let payload;
    try {
      payload = await response.json();
    } catch {
      throw new ApiFailure(response.ok ? FAILURE.MALFORMED : classify(response.status, null),
        response.ok ? "The response was not valid JSON" : `Request failed (${response.status})`, { status: response.status });
    }
    if (!response.ok) {
      const kind = classify(response.status, payload);
      const err = payload?.error || {};
      throw new ApiFailure(kind, err.message || `Request failed (${response.status})`, { status: response.status, code: err.code, detail: err.detail });
    }
    if (!payload || typeof payload !== "object" || Array.isArray(payload)) throw new ApiFailure(FAILURE.MALFORMED, "The response was not a JSON object");
    return payload;
  }

  /** GET with in-flight de-duplication and a bounded cache for immutable records. */
  get(path, { params, signal, fresh = false } = {}) {
    const url = this.url(path, params);
    const immutable = !params && IMMUTABLE.test(path);
    if (immutable && !fresh && this.cache.has(url)) {
      const value = this.cache.get(url);
      this.cache.delete(url);
      this.cache.set(url, value);
      this.stats.cached++;
      return abortable(Promise.resolve(value), signal);
    }
    let shared = this.inflight.get(url);
    if (shared) this.stats.deduplicated++;
    else {
      shared = this.send("GET", path, { params }).then((value) => {
        if (immutable) {
          this.cache.set(url, value);
          while (this.cache.size > this.cacheSize) this.cache.delete(this.cache.keys().next().value);
        }
        return value;
      }).finally(() => this.inflight.delete(url));
      this.inflight.set(url, shared);
    }
    return abortable(shared, signal);
  }

  /** POST: never de-duplicated or retried here; the backend owns idempotency (identical requests join their job). */
  post(path, body = {}, { params } = {}) { return this.send("POST", path, { params, body }); }
}
