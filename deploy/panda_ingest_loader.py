#!/usr/bin/env python3
"""Load job-history batches from the spool into PostgreSQL (the only code with DB access).

deploy/panda_ingest.py runs as the launcher's user and writes sanitized batch files to the
spool. This loader runs as a SEPARATE OS user that owns the database credential, so neither
the launcher nor its claude legs can ever read the DSN. It:

  * reads each batch (gzip JSONL: one header line, then {"table": ..., "row": {...}} lines)
    and checks the header, the row checksum, the table name and every column against the
    live schema,
  * inserts the batch in one transaction, oldest batch first, with ON CONFLICT DO NOTHING
    (row ids are deterministic, so a replay adds nothing); the jobs and artifacts
    projections are upserted, approvals go through record_phase1_approval(),
  * sets aside only the rows PostgreSQL rejects (the batch is reloaded row by row) and keeps
    a copy of that batch in spool/failed/ with a .rejected.json list of what was refused,
  * leaves every batch in spool/pending/ when the database itself is the problem (a missing
    migration or grant, read-only, full, unreachable): nothing is lost, the next run retries,
  * moves a file that is not a valid batch to spool/failed/ at once, and loaded batches to
    spool/done/ (pruned after --keep-done-days).

SETUP (once per box; see deploy/README.md "Job history in PostgreSQL"):
  sudo install -d -m 0755 /usr/local/lib/panda-ingest
  sudo install -m 0644 deploy/panda_ingest_loader.py /usr/local/lib/panda-ingest/
  sudo -u panda-ingest python3 -m venv ~panda-ingest/venv
  sudo -u panda-ingest ~panda-ingest/venv/bin/pip install "psycopg[binary]"
  # DSN, one line, mode 0600, owned by panda-ingest: ~panda-ingest/.config/panda-ingest/dsn
  sudo -u panda-ingest ~panda-ingest/venv/bin/python \
      /usr/local/lib/panda-ingest/panda_ingest_loader.py --install

Needs psycopg (3) or psycopg2. Python 3.9+.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import time
import zlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Set, Tuple

BATCH_FORMAT = "panda-ingest-batch"
BATCH_VERSIONS = {1}
SCHEMA = "openmontage"
CRON_MARKER = "panda_ingest_loader.py"
MAX_FAILURES = 3
MAX_BATCH_BYTES = 512 * 1024 * 1024                 # decompressed
MAX_LINE_BYTES = 64 * 1024 * 1024

DEFAULT_SPOOL = Path("/var/spool/panda-ingest")
DEFAULT_DSN_FILE = Path("~/.config/panda-ingest/dsn").expanduser()
DEFAULT_STATE_DIR = Path("~/.local/state/panda-ingest").expanduser()

# Insert order satisfies every foreign key in V001/V002.
TABLE_ORDER: Tuple[str, ...] = (
    "jobs",
    "ingestion_sources",
    "ingestion_records",
    "job_state_observations",
    "checkpoints",
    "decisions",
    "agent_runs",
    "artifacts",
    "cost_observations",
    "timing_events",
    "llm_inferences",
    "llm_blocks",
    "trace_spans",
    "span_links",
    "runtime_events",
    "approval_events",
)

# The tables deploy/panda_ingest.py writes, and their primary keys. Anything else in a batch
# (transcript_archives, a system table) is refused.
PRIMARY_KEYS: Dict[str, str] = {
    "jobs": "job_id",
    "ingestion_sources": "ingestion_source_id",
    "ingestion_records": "ingestion_record_id",
    "job_state_observations": "job_state_observation_id",
    "checkpoints": "checkpoint_id",
    "decisions": "decision_row_id",
    "agent_runs": "agent_run_id",
    "artifacts": "artifact_id",
    "cost_observations": "cost_observation_id",
    "timing_events": "timing_event_id",
    "llm_inferences": "llm_inference_id",
    "llm_blocks": "llm_block_id",
    "trace_spans": "span_id",
    "span_links": "span_link_id",
    "runtime_events": "runtime_event_id",
    "approval_events": "approval_event_id",
}

# Projection columns the writer role may update (V001 grants plus V002). Every other table
# is append-only: a row that is already there (by any unique key) is left as it is.
UPSERT_COLUMNS: Dict[str, Tuple[str, ...]] = {
    "jobs": (
        "current_status", "current_stage", "current_gate", "current_question", "state_version",
        "code_branch", "code_commit", "runner_version", "processing_started_at",
        "processing_finished_at", "completed_at", "failed_at", "terminal_error_redacted",
        "trace_id", "options_redacted", "inputs_redacted",
    ),
    "artifacts": (
        "selected", "artifact_status", "rejected_reason_redacted", "expires_at",
        "metadata_redacted",
        # Learned after the file was first seen (the manifest lists a clip later): filled
        # once, never overwritten.
        "stage", "scene_id", "source_tool", "provider", "provider_job_id", "model", "mime_type",
        "duration_seconds", "width_pixels", "height_pixels", "fps", "producing_span_id",
    ),
}
FILL_ONCE: Dict[str, Set[str]] = {
    "artifacts": {"stage", "scene_id", "source_tool", "provider", "provider_job_id", "model",
                  "mime_type", "duration_seconds", "width_pixels", "height_pixels", "fps",
                  "producing_span_id"},
}
# A newer jobs row only replaces an older one (state.json can be read out of order).
UPSERT_GUARDS: Dict[str, str] = {
    "jobs": f"{SCHEMA}.jobs.state_version <= EXCLUDED.state_version",
}

APPROVAL_ARGS: Tuple[Tuple[str, str], ...] = (
    ("job_id", "text"), ("checkpoint_id", "uuid"), ("ingestion_record_id", "uuid"),
    ("event_key", "text"), ("gate", "text"), ("decision", "text"),
    ("answer_redacted", "text"), ("provenance", "text"), ("actor_reference", "text"),
    ("received_at", "timestamptz"), ("metadata_redacted", "jsonb"),
)
# The exporter infers approvals from files; it never claims a launcher receipt.
EXPORTER_PROVENANCE = "legacy_inferred"
# state_version is microseconds since the epoch; a value from the future would freeze the
# jobs row (the guard keeps the highest version).
MAX_VERSION_AHEAD_US = 86_400 * 1_000_000

_IDENT = re.compile(r"^[a-z_][a-z0-9_]*$")
_BATCH_NAME = re.compile(r"^[A-Za-z0-9_.\-]+\.jsonl\.gz$")
_JOB_ID = re.compile(r"^job_[A-Za-z0-9_\-]+$")

# SQLSTATE classes that describe the database or the connection, not a row: the batch
# waits in pending/ and the run stops (a later run retries everything in order).
ENVIRONMENT_STATES = {"08", "25", "28", "3D", "3F", "40", "53", "57", "58", "F0", "HV", "XX"}
ENVIRONMENT_CODES = {"42501", "42P01", "42703", "42883", "42704", "55P03", "55006"}
# Any other class-42 error comes from SQL this loader generated: stop rather than drop rows.
DATA_CODES_IN_42 = {"42804", "42P18"}


class BatchError(Exception):
    """A file that is not a valid batch (bad gzip, header, checksum, JSON or table)."""


class EnvironmentProblem(Exception):
    """The database cannot take writes right now (or lacks a migration): retry later."""


class RowRejected(Exception):
    """PostgreSQL refused one row's values."""

    def __init__(self, index: int, reason: str) -> None:
        super().__init__(reason)
        self.index = index
        self.reason = reason


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def log(message: str) -> None:
    print(f"{utc_now().isoformat(timespec='seconds')} {message}", flush=True)


# ---------------------------------------------------------------------------
# Batch files
# ---------------------------------------------------------------------------

def _decompress(path: Path) -> bytes:
    try:
        with gzip.open(path, "rb") as handle:
            chunks: List[bytes] = []
            total = 0
            while True:
                chunk = handle.read(1 << 20)
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_BATCH_BYTES:
                    raise BatchError(f"batch larger than {MAX_BATCH_BYTES} bytes uncompressed")
                chunks.append(chunk)
    except BatchError:
        raise
    except (OSError, EOFError, zlib.error, ValueError) as exc:
        raise BatchError(f"unreadable batch: {type(exc).__name__}") from exc
    return b"".join(chunks)


def read_batch(path: Path) -> Tuple[Dict[str, Any], List[Tuple[str, Dict[str, Any]]]]:
    """Header and rows of one batch; raises BatchError on any inconsistency."""
    raw = _decompress(path)
    lines = raw.split(b"\n")
    if lines and lines[-1] == b"":
        lines.pop()
    if not lines:
        raise BatchError("empty batch")
    try:
        header = json.loads(lines[0])
    except (ValueError, RecursionError) as exc:
        raise BatchError(f"bad header: {type(exc).__name__}") from exc
    if not isinstance(header, dict) or header.get("format") != BATCH_FORMAT:
        raise BatchError("not a panda-ingest batch")
    if header.get("version") not in BATCH_VERSIONS:
        raise BatchError(f"unsupported batch version {header.get('version')!r}")
    body = lines[1:]
    digest = hashlib.sha256(b"\n".join(body)).hexdigest()
    if header.get("rows_sha256") != digest:
        raise BatchError("row checksum mismatch (truncated or altered batch)")
    if header.get("row_count") != len(body):
        raise BatchError("row count mismatch")
    rows: List[Tuple[str, Dict[str, Any]]] = []
    for number, line in enumerate(body, start=2):
        if len(line) > MAX_LINE_BYTES:
            raise BatchError(f"line {number}: longer than {MAX_LINE_BYTES} bytes")
        try:
            item = json.loads(line)
        except (ValueError, RecursionError) as exc:
            raise BatchError(f"line {number}: bad JSON ({type(exc).__name__})") from exc
        if not isinstance(item, dict):
            raise BatchError(f"line {number}: not a JSON object")
        table, row = item.get("table"), item.get("row")
        if table not in PRIMARY_KEYS or not isinstance(row, dict):
            raise BatchError(f"line {number}: unknown table {str(table)[:40]!r}")
        rows.append((table, row))
    return header, rows


def list_batches(spool: Path) -> List[Path]:
    """Pending batches, oldest first (names start with a UTC timestamp)."""
    pending = spool / "pending"
    if not pending.is_dir():
        return []
    found = [p for p in pending.rglob("*.jsonl.gz") if _BATCH_NAME.match(p.name)]
    return sorted(found, key=lambda p: (p.name, str(p)))


def check_rows(header: Dict[str, Any], path: Path, rows: List[Tuple[str, Dict[str, Any]]]
               ) -> List[Tuple[int, str]]:
    """Rows a genuine exporter batch never contains: (index, reason) to set aside.

    The spool is written by the launcher's user, so a claude leg could drop a batch there.
    A batch names one job (its folder and header); every row must belong to that job, an
    approval can only be an inference from files, and a state version cannot be from the
    future. This bounds what a forged batch can claim; it cannot tell a forged row for the
    named job from a real one (see deploy/README.md)."""
    job_id = header.get("job_id")
    if not isinstance(job_id, str) or not _JOB_ID.match(job_id) or path.parent.name != job_id:
        raise BatchError("header job_id does not match the batch's folder")
    sources = {row.get("ingestion_source_id"): row.get("job_id")
               for table, row in rows if table == "ingestion_sources"}
    limit = int(time.time() * 1_000_000) + MAX_VERSION_AHEAD_US
    problems: List[Tuple[int, str]] = []
    for index, (table, row) in enumerate(rows):
        if "job_id" in row and row.get("job_id") != job_id:
            problems.append((index, "job_id does not match the batch"))
        elif table == "ingestion_sources" and row.get("job_id") != job_id:
            problems.append((index, "source of another job"))
        elif table == "ingestion_records" and row.get("ingestion_source_id") in sources \
                and sources[row.get("ingestion_source_id")] != job_id:
            problems.append((index, "record of another job's source"))
        elif table == "approval_events" and row.get("provenance") != EXPORTER_PROVENANCE:
            problems.append((index, "approval provenance the exporter never writes"))
        elif table == "jobs" and isinstance(row.get("state_version"), int) \
                and row["state_version"] > limit:
            problems.append((index, "state_version in the future"))
    return problems


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

def connect(dsn: str) -> Any:
    try:
        import psycopg  # type: ignore

        conn = psycopg.connect(dsn, autocommit=False, application_name="panda_ingest_loader")
        return conn
    except ImportError:
        pass
    try:
        import psycopg2  # type: ignore
    except ImportError as exc:
        raise SystemExit("panda_ingest_loader needs psycopg (3) or psycopg2: "
                         "pip install 'psycopg[binary]'") from exc
    conn = psycopg2.connect(dsn, application_name="panda_ingest_loader")
    conn.autocommit = False
    return conn


def table_columns(conn: Any) -> Dict[str, Dict[str, str]]:
    """{table: {column: sql type}} for the tables this loader writes."""
    sql = (
        "SELECT table_name, column_name, data_type, udt_name "
        "FROM information_schema.columns WHERE table_schema = %s"
    )
    with conn.cursor() as cur:
        cur.execute(sql, (SCHEMA,))
        result: Dict[str, Dict[str, str]] = {}
        for table, column, data_type, udt in cur.fetchall():
            if table not in PRIMARY_KEYS:
                continue
            result.setdefault(table, {})[column] = _cast_type(data_type, udt)
    conn.rollback()
    return result


def _cast_type(data_type: str, udt: str) -> str:
    if data_type == "ARRAY":
        return udt.lstrip("_") + "[]"
    if data_type == "USER-DEFINED":
        return udt
    mapping = {
        "timestamp with time zone": "timestamptz",
        "timestamp without time zone": "timestamp",
        "character varying": "varchar",
        "double precision": "float8",
    }
    return mapping.get(data_type, data_type)


class RowValueError(Exception):
    """A row value that cannot be sent as it stands (a bad column name or shape)."""


def _param(value: Any, sql_type: str) -> Any:
    if value is None:
        return None
    if sql_type in ("jsonb", "json"):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    if isinstance(value, (dict, list)):
        raise RowValueError(f"structured value for non-JSON column of type {sql_type}")
    if isinstance(value, bool) or sql_type == "boolean":
        return value
    return value if isinstance(value, (int, float)) else str(value)


def _check_row(table: str, row: Dict[str, Any], columns: Dict[str, str]) -> List[str]:
    if PRIMARY_KEYS[table] not in row:
        raise RowValueError(f"{table}: row without primary key {PRIMARY_KEYS[table]}")
    names = sorted(row)
    for name in names:
        if not _IDENT.match(name):
            raise RowValueError(f"{table}: invalid column name {name[:40]!r}")
        if name not in columns:
            raise EnvironmentProblem(f"{table}.{name} does not exist (apply the latest migration)")
    return names


def insert_row(cur: Any, table: str, row: Dict[str, Any], columns: Dict[str, str]) -> str:
    """Write one row: 'inserted', 'updated' (projection columns changed), 'unchanged' or
    'recorded' (an approval)."""
    if table == "approval_events":
        args = [_param(row.get(name), sql_type) for name, sql_type in APPROVAL_ARGS]
        placeholders = ", ".join(f"%s::{sql_type}" for _name, sql_type in APPROVAL_ARGS)
        cur.execute(f"SELECT {SCHEMA}.record_phase1_approval({placeholders})", args)
        return "recorded"
    names = _check_row(table, row, columns)
    values = [_param(row[name], columns[name]) for name in names]
    column_sql = ", ".join(names)
    value_sql = ", ".join(f"%s::{columns[name]}" for name in names)
    pk = PRIMARY_KEYS[table]
    updatable = [n for n in names if n in UPSERT_COLUMNS.get(table, ())]
    if updatable:
        fill_once = FILL_ONCE.get(table, set())
        exprs = [f"coalesce({SCHEMA}.{table}.{n}, EXCLUDED.{n})" if n in fill_once
                 else f"EXCLUDED.{n}" for n in updatable]
        assignments = ", ".join(f"{n} = {e}" for n, e in zip(updatable, exprs))
        current = ", ".join(f"{SCHEMA}.{table}.{n}" for n in updatable)
        conditions = [f"({current}) IS DISTINCT FROM ({', '.join(exprs)})"]  # skip no-ops
        if UPSERT_GUARDS.get(table):
            conditions.append(UPSERT_GUARDS[table])
        cur.execute(
            f"INSERT INTO {SCHEMA}.{table} ({column_sql}) VALUES ({value_sql}) "
            f"ON CONFLICT ({pk}) DO UPDATE SET {assignments} WHERE {' AND '.join(conditions)} "
            "RETURNING (xmax = 0)", values)
        result = cur.fetchone()
        if result is None:
            return "unchanged"
        if result[0] and table == "artifacts":
            _supersede_older_versions(cur, row)
        return "inserted" if result[0] else "updated"
    # Append-only: a row already present under ANY unique key (its id, or a domain key such
    # as agent_runs (job_id, run_key) rebuilt after Claude Code deleted a transcript) is
    # skipped. The first version wins.
    cur.execute(f"INSERT INTO {SCHEMA}.{table} ({column_sql}) VALUES ({value_sql}) "
                "ON CONFLICT DO NOTHING", values)
    return "inserted" if cur.rowcount > 0 else "unchanged"


def _supersede_older_versions(cur: Any, row: Dict[str, Any]) -> None:
    """A new content version of a file retires the older versions of the same path (the
    exporter only sees the file as it is now, so it can never re-send them)."""
    if row.get("artifact_status") != "available" or not row.get("created_at"):
        return
    cur.execute(
        f"UPDATE {SCHEMA}.artifacts SET selected = false, artifact_status = 'superseded' "
        "WHERE job_id = %s AND logical_id = %s AND artifact_id <> %s::uuid "
        "AND artifact_status = 'available' AND created_at < %s::timestamptz",
        (row.get("job_id"), row.get("logical_id"), row.get("artifact_id"), row.get("created_at")))


def _sqlstate(exc: BaseException) -> Optional[str]:
    diag = getattr(exc, "diag", None)
    state = getattr(diag, "sqlstate", None) if diag is not None else None
    return state or getattr(exc, "sqlstate", None) or getattr(exc, "pgcode", None)


def _db_error(exc: BaseException) -> str:
    """SQLSTATE and the primary message only: PostgreSQL's DETAIL ('Failing row contains
    ...') echoes row values, which must not end up in a log file."""
    diag = getattr(exc, "diag", None)
    primary = getattr(diag, "message_primary", None) if diag is not None else None
    state = _sqlstate(exc)
    if primary:
        return f"{type(exc).__name__} {state or ''}: {primary}"[:500]
    return f"{type(exc).__name__}: {str(exc).splitlines()[0] if str(exc) else ''}"[:500]


def is_environment_error(exc: BaseException) -> bool:
    """True when the database (not the row) is the problem: connection, privileges, a
    missing table or column, read-only, disk full, timeouts, shutdown."""
    if isinstance(exc, EnvironmentProblem):
        return True
    if isinstance(exc, RowValueError):
        return False
    state = _sqlstate(exc)
    if state:
        if state in ENVIRONMENT_CODES or state[:2] in ENVIRONMENT_STATES:
            return True
        if state[:2] == "42":
            return state not in DATA_CODES_IN_42
        return False
    name = type(exc).__name__
    # No SQLSTATE: a dropped connection or a client-side failure.
    return name in ("OperationalError", "InterfaceError", "ConnectionTimeout",
                    "ConnectionException", "InternalError", "DatabaseError")


def _apply(cur: Any, rows: List[Tuple[str, Dict[str, Any]]],
           columns: Dict[str, Dict[str, str]], counts: Dict[str, int]) -> None:
    for index, (table, row) in enumerate(rows):
        if table != "approval_events" and table not in columns:
            raise EnvironmentProblem(f"table {SCHEMA}.{table} is missing (apply the migrations)")
        try:
            outcome = insert_row(cur, table, row, columns.get(table, {}))
        except Exception as exc:  # noqa: BLE001 - classified below
            if is_environment_error(exc):
                raise EnvironmentProblem(_db_error(exc)) from exc
            raise RowRejected(index, _db_error(exc)) from exc
        counts[outcome] = counts.get(outcome, 0) + 1


def load_batch(conn: Any, path: Path, columns: Dict[str, Dict[str, str]]
               ) -> Tuple[Dict[str, Any], Dict[str, int], List[Dict[str, Any]]]:
    """Load one batch: (header, counts by outcome, rejected rows). Raises BatchError for a
    file that is not a valid batch and EnvironmentProblem when the database is the problem
    (nothing is written then). When PostgreSQL refuses a row, the batch is reloaded row by
    row and only the refused rows are set aside."""
    header, rows = read_batch(path)
    rejected: List[Dict[str, Any]] = []
    blocked = dict(check_rows(header, path, rows))
    ordered: List[Tuple[str, Dict[str, Any]]] = []
    for index, (table, row) in enumerate(rows):
        if index in blocked:
            rejected.append({"table": table, "id": str(row.get(PRIMARY_KEYS[table]))[:80],
                             "reason": blocked[index]})
        else:
            ordered.append((table, row))
    order = {table: position for position, table in enumerate(TABLE_ORDER)}
    ordered.sort(key=lambda item: order[item[0]])
    counts: Dict[str, int] = {}
    try:
        with conn.cursor() as cur:
            cur.execute("SET LOCAL statement_timeout = '120s'")
            _apply(cur, ordered, columns, counts)
        conn.commit()
        return header, counts, rejected
    except RowRejected:
        conn.rollback()
    except Exception:
        conn.rollback()
        raise
    # One row was refused: reload row by row, each under a savepoint.
    counts = {}
    try:
        with conn.cursor() as cur:
            cur.execute("SET LOCAL statement_timeout = '120s'")
            for table, row in ordered:
                cur.execute("SAVEPOINT panda_row")
                try:
                    _apply(cur, [(table, row)], columns, counts)
                except RowRejected as exc:
                    cur.execute("ROLLBACK TO SAVEPOINT panda_row")
                    rejected.append({"table": table,
                                     "id": str(row.get(PRIMARY_KEYS[table]))[:80],
                                     "reason": exc.reason})
                    continue
                cur.execute("RELEASE SAVEPOINT panda_row")
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return header, counts, rejected


# ---------------------------------------------------------------------------
# State and file handling
# ---------------------------------------------------------------------------

def read_state(path: Path) -> Dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def write_state(path: Path, data: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)


def _bucket_dir(spool: Path, bucket: str, job_dir: str) -> Path:
    """done/<job> or failed/<job>. Readable (not writable) by the launcher's user, so its
    exporter --status can count them; the batches in them are its own files."""
    target = spool / bucket / job_dir
    for folder in (spool / bucket, target):
        if not folder.is_dir():
            folder.mkdir(exist_ok=True)
            if os.name == "posix":
                try:
                    os.chmod(folder, 0o2755)
                except OSError:
                    pass
    return target


def _move(path: Path, spool: Path, bucket: str) -> Path:
    relative = path.relative_to(spool / "pending")
    target_dir = _bucket_dir(spool, bucket, relative.parts[0] if len(relative.parts) > 1
                             else "_")
    target = target_dir / path.name
    os.replace(path, target)
    return target


def _note(target: Path, suffix: str, payload: Dict[str, Any]) -> None:
    note = target.with_name(target.name + suffix)
    note.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def prune_done(spool: Path, keep_days: int) -> int:
    done = spool / "done"
    if keep_days <= 0 or not done.is_dir():
        return 0
    cutoff = time.time() - keep_days * 86400
    removed = 0
    for path in done.rglob("*.jsonl.gz"):
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
                removed += 1
        except OSError:
            continue
    return removed


def read_dsn(path: Path) -> str:
    try:
        mode = path.stat().st_mode
    except OSError as exc:
        raise SystemExit(f"DSN file {path} is missing ({type(exc).__name__})") from exc
    if os.name == "posix" and mode & 0o077:
        raise SystemExit(f"DSN file {path} must not be readable by others (chmod 600)")
    dsn = path.read_text(encoding="utf-8").strip()
    if not dsn:
        raise SystemExit(f"DSN file {path} is empty")
    return dsn


def run(spool: Path, dsn_file: Path, state_dir: Path, max_batches: int, keep_days: int,
        connect_fn: Callable[[str], Any] = connect) -> int:
    batches = list_batches(spool)
    state_path = state_dir / "loader_state.json"
    state = read_state(state_path)
    failures: Dict[str, int] = state.get("failures") or {}
    if not batches:
        removed = prune_done(spool, keep_days)
        state.update(last_run=utc_now().isoformat(), last_pending=0)
        write_state(state_path, state)
        log(f"nothing to load (pruned {removed})")
        return 0
    dsn = read_dsn(dsn_file)
    try:
        conn = connect_fn(dsn)
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001 - never print the message: it can echo the DSN
        log(f"cannot connect to PostgreSQL: {type(exc).__name__} {_sqlstate(exc) or ''}; "
            f"{len(batches)} batch(es) stay in pending/")
        return 2
    loaded = failed = quarantined = 0
    stopped: Optional[str] = None
    try:
        try:
            columns = table_columns(conn)
        except Exception as exc:  # noqa: BLE001
            stopped = _db_error(exc)
            columns = {}
        todo = batches[:max_batches] if max_batches > 0 else batches
        for path in todo if stopped is None else []:
            key = str(path.relative_to(spool))
            try:
                header, counts, rejected = load_batch(conn, path, columns)
            except BatchError as exc:
                # Not a valid batch: retrying cannot help. Kept for inspection.
                quarantined += 1
                failures.pop(key, None)
                log(f"NOT A VALID BATCH {path.name}: {exc}; moved to failed/")
                try:
                    _note(_move(path, spool, "failed"), ".reason.json",
                          {"reason": str(exc), "at": utc_now().isoformat()})
                except OSError as move_exc:
                    log(f"could not move {path.name}: {type(move_exc).__name__}")
                continue
            except EnvironmentProblem as exc:
                stopped = str(exc)
                break
            except Exception as exc:  # noqa: BLE001 - one odd file must not stop the others
                failed += 1
                failures[key] = failures.get(key, 0) + 1
                log(f"FAILED {path.name} (attempt {failures[key]}): {type(exc).__name__}")
                if failures[key] >= MAX_FAILURES:
                    failures.pop(key, None)
                    try:
                        _note(_move(path, spool, "failed"), ".reason.json",
                              {"reason": type(exc).__name__, "at": utc_now().isoformat()})
                        log(f"moved {path.name} to failed/ after {MAX_FAILURES} attempts")
                    except OSError as move_exc:
                        log(f"could not move {path.name}: {type(move_exc).__name__}")
                continue
            failures.pop(key, None)
            loaded += 1
            log(f"loaded {path.name} job={header.get('job_id')} "
                f"inserted={counts.get('inserted', 0)} updated={counts.get('updated', 0)} "
                f"unchanged={counts.get('unchanged', 0)} approvals={counts.get('recorded', 0)} "
                f"rejected={len(rejected)}")
            try:
                if rejected:
                    # Loaded except these rows; the batch is kept with the list of refusals.
                    target = _move(path, spool, "failed")
                    _note(target, ".rejected.json", {"rejected": rejected,
                                                     "at": utc_now().isoformat()})
                    for item in rejected[:5]:
                        log(f"  rejected {item['table']} {item['id']}: {item['reason']}")
                else:
                    _move(path, spool, "done")
            except OSError as exc:
                log(f"loaded {path.name} but could not move it: {type(exc).__name__}")
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass
    if stopped:
        log(f"STOPPED, batches stay in pending/: {stopped}")
    removed = prune_done(spool, keep_days)
    pending = len(list_batches(spool))
    state.update(failures=failures, last_run=utc_now().isoformat(),
                 last_loaded=loaded, last_failed=failed, last_quarantined=quarantined,
                 last_pending=pending, last_stopped=stopped,
                 loaded_total=int(state.get("loaded_total") or 0) + loaded)
    write_state(state_path, state)
    log(f"done: loaded={loaded} failed={failed} invalid={quarantined} pending={pending} "
        f"pruned={removed}")
    return 1 if (failed or quarantined or stopped) else 0


# ---------------------------------------------------------------------------
# Cron
# ---------------------------------------------------------------------------

def _crontab_read() -> str:
    proc = subprocess.run(["crontab", "-l"], capture_output=True, timeout=15, check=False)
    text = proc.stdout.decode("utf-8", "surrogateescape")
    if proc.returncode == 0:
        return text
    if "no crontab" in (proc.stderr.decode("utf-8", "replace") + text).lower():
        return ""
    raise SystemExit("could not read the current crontab, so it was not changed")


def _root_owned_chain(path: Path) -> Optional[str]:
    """None when the file and every folder above it are owned by root and writable by no
    one else; otherwise the first path that is not. Cron runs this file with the DSN, so
    the launcher's user must not be able to change it."""
    for item in [path] + list(path.parents):
        try:
            info = item.stat()
        except OSError:
            return str(item)
        if info.st_uid != 0:
            return str(item)
        if info.st_mode & (stat.S_IWGRP | stat.S_IWOTH) and not (
                item.is_dir() and info.st_mode & stat.S_ISVTX and item != path.parent):
            return str(item)                           # a root-owned sticky folder above is fine
    return None


def install(interval_min: int, spool: Path, dsn_file: Path) -> int:
    if os.name != "posix" or os.geteuid() == 0:
        raise SystemExit("run --install as the ingest user (not root), on the box")
    if not 1 <= interval_min <= 59:
        raise SystemExit("--every must be 1-59 minutes")
    script = Path(__file__).resolve()
    unsafe = _root_owned_chain(script)
    if unsafe:
        raise SystemExit(f"{unsafe} is not root-owned and protected: install the loader into "
                         "/usr/local/lib/panda-ingest (deploy/README.md) and run --install "
                         "from there")
    python = Path(sys.executable)
    state_dir = DEFAULT_STATE_DIR
    state_dir.mkdir(parents=True, exist_ok=True)
    flock = shutil.which("flock") or ""
    lock = f"{flock} -n {state_dir / 'loader.lock'} " if flock else ""
    extra = ""
    if spool != DEFAULT_SPOOL:
        extra += f" --spool {spool}"
    if dsn_file != DEFAULT_DSN_FILE:
        extra += f" --dsn-file {dsn_file}"
    log_path = Path.home() / "panda-ingest-loader.log"
    # umask 077: the log and state files stay private to the ingest user.
    line = (f"*/{interval_min} * * * * umask 077; {lock}{python} {script}{extra} "
            f">> {log_path} 2>&1")
    if " " in str(script) or " " in str(python):
        raise SystemExit("paths with spaces are not supported in the cron line")
    current = _crontab_read()
    backup = state_dir / f"crontab.before.{utc_now().strftime('%Y%m%dT%H%M%SZ')}"
    backup.write_text(current, encoding="utf-8", errors="surrogateescape")
    kept = [ln for ln in current.splitlines() if CRON_MARKER not in ln]
    new = "\n".join(kept + [line]) + "\n"
    proc = subprocess.run(["crontab", "-"], input=new.encode("utf-8", "surrogateescape"),
                          capture_output=True, timeout=15, check=False)
    if proc.returncode != 0:
        raise SystemExit("crontab rejected the update: "
                         + proc.stderr.decode("utf-8", "replace")[-300:])
    print(json.dumps({"installed": line, "crontab_backup": str(backup)}, indent=2))
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--spool", type=Path, default=DEFAULT_SPOOL)
    parser.add_argument("--dsn-file", type=Path, default=DEFAULT_DSN_FILE)
    parser.add_argument("--state-dir", type=Path, default=DEFAULT_STATE_DIR)
    parser.add_argument("--max-batches", type=int, default=0, help="0 = all pending")
    parser.add_argument("--keep-done-days", type=int, default=30)
    parser.add_argument("--check", metavar="BATCH", type=Path,
                        help="validate one batch file without a database and print a summary")
    parser.add_argument("--install", action="store_true", help="add the cron line")
    parser.add_argument("--every", type=int, default=5, help="cron interval in minutes")
    args = parser.parse_args(argv)
    if args.check:
        try:
            header, rows = read_batch(args.check)
            problems = check_rows(header, args.check, rows)
        except BatchError as exc:
            print(json.dumps({"ok": False, "error": str(exc)}))
            return 1
        tables: Dict[str, int] = {}
        for table, _row in rows:
            tables[table] = tables.get(table, 0) + 1
        print(json.dumps({"ok": not problems, "job_id": header.get("job_id"), "rows": len(rows),
                          "tables": tables, "rows_refused": len(problems)}, sort_keys=True))
        return 0 if not problems else 1
    if args.install:
        return install(args.every, args.spool, args.dsn_file)
    return run(args.spool, args.dsn_file, args.state_dir, args.max_batches,
               args.keep_done_days)


if __name__ == "__main__":
    sys.exit(main())
