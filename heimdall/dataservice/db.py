"""SQLite persistence for the data service (stdlib only, no external database; WAL; one file).

Migration path: every access goes through the ``Inventory``, ``ProductStore`` and ``JobQueue`` classes, which use plain SQL over the
tables below. Replacing the file with PostgreSQL/PostGIS (footprints as geography columns, the ``s_ra/s_dec`` + polygon text becoming a
spatial index) and the content-addressed ``objects/`` directory with an object store (S3/GCS keyed by SHA-256) changes only these classes.
``PRAGMA user_version`` carries the schema version.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path

from .schemas import SCHEMA_VERSION

DDL = """
CREATE TABLE IF NOT EXISTS observations(
  obs_id TEXT NOT NULL, release TEXT NOT NULL, collection TEXT NOT NULL, detector INTEGER NOT NULL,
  footprint TEXT, s_ra REAL, s_dec REAL, t_min REAL, t_max REAL, wl_min_um REAL, wl_max_um REAL,
  route TEXT, access_reference TEXT NOT NULL, discovered_at TEXT NOT NULL, last_seen_at TEXT NOT NULL,
  archive_checksum TEXT, product_version TEXT, source_version TEXT, origin TEXT NOT NULL, legacy INTEGER NOT NULL DEFAULT 0,
  synthetic INTEGER NOT NULL DEFAULT 0, superseded_by TEXT, extra TEXT,
  PRIMARY KEY(obs_id, release));
CREATE INDEX IF NOT EXISTS obs_time ON observations(t_min);
CREATE INDEX IF NOT EXISTS obs_pos ON observations(s_ra, s_dec);
CREATE TABLE IF NOT EXISTS syncs(
  sync_id INTEGER PRIMARY KEY AUTOINCREMENT, ra REAL NOT NULL, dec REAL NOT NULL, radius REAL NOT NULL,
  detectors TEXT, mjd_start REAL, mjd_end REAL, collections TEXT NOT NULL, scope TEXT NOT NULL, completed_at TEXT NOT NULL,
  route TEXT, from_cache INTEGER NOT NULL DEFAULT 0, n_obs INTEGER NOT NULL, newest_t_max REAL, members TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS products(
  sha256 TEXT PRIMARY KEY, path TEXT NOT NULL, size INTEGER NOT NULL, created_at TEXT NOT NULL, validated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS product_keys(
  key TEXT PRIMARY KEY, sha256 TEXT NOT NULL REFERENCES products(sha256), kind TEXT NOT NULL, obs_id TEXT, release TEXT,
  access_reference TEXT, origin TEXT NOT NULL, route TEXT, archive_checksum TEXT, retrieved_at TEXT NOT NULL, meta TEXT);
CREATE TABLE IF NOT EXISTS pins(sha256 TEXT NOT NULL, manifest_id TEXT NOT NULL, PRIMARY KEY(sha256, manifest_id));
CREATE TABLE IF NOT EXISTS manifests(manifest_id TEXT PRIMARY KEY, created_at TEXT NOT NULL, body TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS jobs(
  job_id TEXT PRIMARY KEY, kind TEXT NOT NULL, payload TEXT NOT NULL, state TEXT NOT NULL, generation INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL, deadline_at TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
  next_attempt_at TEXT, lease_until TEXT, last_error TEXT, result TEXT, progress TEXT, reason TEXT);
CREATE INDEX IF NOT EXISTS jobs_state ON jobs(state);
CREATE TABLE IF NOT EXISTS job_events(
  seq INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT NOT NULL, at TEXT NOT NULL, from_state TEXT, to_state TEXT NOT NULL, reason TEXT, detail TEXT);
CREATE INDEX IF NOT EXISTS job_events_job ON job_events(job_id, seq);
CREATE TABLE IF NOT EXISTS route_events(
  seq INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL, route TEXT NOT NULL, ok INTEGER NOT NULL, error_class TEXT, latency_s REAL);
CREATE TABLE IF NOT EXISTS investigation_results(
  key TEXT PRIMARY KEY, investigation_id TEXT NOT NULL, saved_to TEXT NOT NULL, manifest_id TEXT, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS counters(name TEXT PRIMARY KEY, value REAL NOT NULL DEFAULT 0);
"""


class ServiceDB:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        # Several processes (API server, worker, CLI) share one file: wait for locks instead of failing, and retry first-time
        # initialisation (journal-mode switch, DDL) a bounded number of times when another process is initialising concurrently.
        self.conn = sqlite3.connect(str(self.path), check_same_thread=False, isolation_level=None, timeout=30.0)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA busy_timeout=30000")
        for attempt in range(50):
            try:
                self._init_schema()
                break
            except sqlite3.OperationalError as exc:
                if "locked" not in str(exc) or attempt == 49:
                    raise
                time.sleep(0.1 + 0.02 * attempt)

    def _init_schema(self) -> None:
        if self.conn.execute("PRAGMA journal_mode").fetchone()[0].lower() != "wal":
            self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        have = self.conn.execute("PRAGMA user_version").fetchone()[0]
        if have > SCHEMA_VERSION:
            raise RuntimeError(f"service database schema {have} is newer than this software ({SCHEMA_VERSION})")
        self.conn.executescript(DDL)
        if have != SCHEMA_VERSION:
            self.conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")

    def execute(self, sql: str, args: tuple = ()):
        with self.lock:
            return self.conn.execute(sql, args)

    def query(self, sql: str, args: tuple = ()) -> list[sqlite3.Row]:
        with self.lock:
            return self.conn.execute(sql, args).fetchall()

    def one(self, sql: str, args: tuple = ()):
        rows = self.query(sql, args)
        return rows[0] if rows else None

    def transaction(self):
        return _Tx(self)

    def bump(self, name: str, by: float = 1.0) -> None:
        self.execute("INSERT INTO counters(name,value) VALUES(?,?) ON CONFLICT(name) DO UPDATE SET value=value+excluded.value", (name, by))

    def counter(self, name: str) -> float:
        r = self.one("SELECT value FROM counters WHERE name=?", (name,))
        return float(r["value"]) if r else 0.0

    def close(self) -> None:
        try:
            self.conn.close()
        except Exception:  # noqa: BLE001 - closing twice or at interpreter exit must be harmless
            pass

    def __del__(self) -> None:
        self.close()


class _Tx:
    def __init__(self, db: ServiceDB) -> None:
        self.db = db

    def __enter__(self):
        self.db.lock.acquire()
        self.db.conn.execute("BEGIN IMMEDIATE")
        return self.db

    def __exit__(self, exc_type, *_):
        try:
            self.db.conn.execute("ROLLBACK" if exc_type else "COMMIT")
        finally:
            self.db.lock.release()
