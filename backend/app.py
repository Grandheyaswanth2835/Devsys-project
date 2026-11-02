"""Dependency-free local web application for exploring synthetic MBIST data."""

from __future__ import annotations

import argparse
import csv
import hmac
import io
import json
import logging
import math
import os
import re
import time
import uuid
from functools import lru_cache
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Lock
from urllib.parse import parse_qs, unquote, urlparse


ROOT = Path(__file__).resolve().parent.parent
WEB_ROOT = ROOT / "frontend"
DATA_FILES = {
    "dies": "die_results.csv",
    "memories": "memories.csv",
    "failures": "fail_events.csv",
}

REQUIRED_COLUMNS = {
    "dies": {
        "die_id", "lot_id", "wafer_id", "radial_pos", "ro_speed_mhz",
        "leakage_na", "iddq_ua", "bitcell_vmin_mv", "scan_pass",
        "march_failing_mems", "early_mats_fails", "repaired_rows",
        "repaired_cols", "retention_fail", "dynamic_fault_fail",
        "vmin_corner_only_fail", "unrepairable", "neighbor_fail_cnt",
        "final_bin", "t_march_ms", "t_retention_ms", "t_repair_ms",
        "t_setup_readout_ms", "t_total_baseline_ms",
    },
    "memories": {
        "mem_id", "mem_type", "bitcell", "words", "width_bits",
        "total_bits", "ports", "test_clock_mhz", "test_power_mw",
        "controller_id", "has_repair", "march_ss_time_us",
    },
    "failures": {
        "die_id", "mem_id", "mem_type", "bitcell", "fault_type",
        "det_MATSplus", "det_MarchCm", "det_MarchLR", "det_MarchCm_RD",
        "det_MarchSS",
    },
}

BOOLEAN_COLUMNS = {
    "scan_pass", "retention_fail", "dynamic_fault_fail",
    "vmin_corner_only_fail", "unrepairable", "has_repair",
    "det_MATSplus", "det_MarchCm", "det_MarchLR", "det_MarchCm_RD",
    "det_MarchSS",
}
INTEGER_COLUMNS = {
    "words", "width_bits", "total_bits", "ports", "test_clock_mhz",
    "march_failing_mems", "early_mats_fails", "repaired_rows",
    "repaired_cols", "neighbor_fail_cnt",
}
FLOAT_COLUMNS = {
    "radial_pos", "ro_speed_mhz", "leakage_na", "iddq_ua",
    "bitcell_vmin_mv", "t_march_ms", "t_retention_ms", "t_repair_ms",
    "t_setup_readout_ms", "t_total_baseline_ms", "test_power_mw",
    "march_ss_time_us",
}
NEW_DIE_INTEGER_FIELDS = {
    "die_x", "die_y", "march_failing_mems", "early_mats_fails",
    "repaired_rows", "repaired_cols", "neighbor_fail_cnt",
}
NEW_DIE_FLOAT_FIELDS = {
    "radial_pos", "ro_speed_mhz", "leakage_na", "iddq_ua",
    "bitcell_vmin_mv", "t_march_ms", "t_retention_ms", "t_repair_ms",
    "t_setup_readout_ms", "t_total_baseline_ms",
}
NEW_DIE_BOOLEAN_FIELDS = {
    "scan_pass", "retention_fail", "dynamic_fault_fail",
    "vmin_corner_only_fail", "unrepairable",
}
NEW_DIE_TEXT_FIELDS = {"lot_id", "wafer_id", "final_bin"}
NEW_DIE_FIELDS = (
    NEW_DIE_TEXT_FIELDS
    | NEW_DIE_INTEGER_FIELDS
    | NEW_DIE_FLOAT_FIELDS
    | NEW_DIE_BOOLEAN_FIELDS
)
FINAL_BINS = {
    "PASS", "PASS_REPAIRED", "FAIL_SCAN", "FAIL_UNREPAIRABLE",
    "FAIL_RETENTION", "FAIL_DYNAMIC", "FAIL_VMIN_CORNER",
}
ALGORITHMS = (
    ("MATSplus", "det_MATSplus", 5),
    ("MarchCm", "det_MarchCm", 10),
    ("MarchLR", "det_MarchLR", 14),
    ("MarchCm_RD", "det_MarchCm_RD", 15),
    ("MarchSS", "det_MarchSS", 22),
)


class DatasetError(Exception):
    """Raised when an input dataset is missing or malformed."""


class StorageError(DatasetError):
    """Raised when persistent die storage is unavailable or misconfigured."""


def _convert_row(row: dict[str, str | None], table: str, line_number: int) -> dict:
    if None in row:
        raise DatasetError(f"{DATA_FILES[table]} row {line_number} has extra values.")
    converted = {}
    for column, value in row.items():
        if value is None:
            raise DatasetError(f"{DATA_FILES[table]} row {line_number} is incomplete.")
        if column in BOOLEAN_COLUMNS:
            lowered = value.strip().lower()
            if lowered not in ("true", "false"):
                raise DatasetError(
                    f"{DATA_FILES[table]} row {line_number}: "
                    f"{column} must be true or false."
                )
            converted[column] = lowered == "true"
        elif column in INTEGER_COLUMNS:
            try:
                converted[column] = int(value)
            except ValueError as exc:
                raise DatasetError(
                    f"{DATA_FILES[table]} row {line_number}: "
                    f"{column} must be an integer."
                ) from exc
        elif column in FLOAT_COLUMNS:
            try:
                number = float(value)
            except ValueError as exc:
                raise DatasetError(
                    f"{DATA_FILES[table]} row {line_number}: "
                    f"{column} must be numeric."
                ) from exc
            if not math.isfinite(number):
                raise DatasetError(
                    f"{DATA_FILES[table]} row {line_number}: "
                    f"{column} must be finite."
                )
            converted[column] = number
        else:
            converted[column] = value
    return converted


def _read_csv(data_dir: Path, table: str) -> list[dict]:
    path = data_dir / DATA_FILES[table]
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as source:
            reader = csv.DictReader(source)
            columns = set(reader.fieldnames or ())
            missing = REQUIRED_COLUMNS[table] - columns
            if missing:
                raise DatasetError(
                    f"{path.name} is missing required columns: "
                    f"{', '.join(sorted(missing))}."
                )
            return [
                _convert_row(row, table, line_number)
                for line_number, row in enumerate(reader, start=2)
            ]
    except OSError as exc:
        raise DatasetError(f"Unable to read {path}: {exc.strerror or exc}.") from exc


def load_dataset(data_dir: Path = ROOT / "data") -> dict[str, list[dict]]:
    data = {table: _read_csv(data_dir, table) for table in DATA_FILES}
    die_ids = {row["die_id"] for row in data["dies"]}
    mem_ids = {row["mem_id"] for row in data["memories"]}
    if len(die_ids) != len(data["dies"]):
        raise DatasetError("die_results.csv contains duplicate die_id values.")
    if len(mem_ids) != len(data["memories"]):
        raise DatasetError("memories.csv contains duplicate mem_id values.")
    for event in data["failures"]:
        if event["die_id"] not in die_ids or event["mem_id"] not in mem_ids:
            raise DatasetError(
                "fail_events.csv contains a die_id or mem_id that is not present "
                "in the related dataset."
            )
    return data


@lru_cache(maxsize=1)
def _get_base_dataset() -> dict[str, list[dict]]:
    return load_dataset()


def _database_url() -> str | None:
    return os.environ.get("DATABASE_URL") or None


def _read_added_dies() -> list[dict]:
    database_url = _database_url()
    if not database_url:
        return []
    try:
        import psycopg
    except ImportError as exc:
        raise StorageError(
            "PostgreSQL support is unavailable. Install the project requirements."
        ) from exc
    try:
        with psycopg.connect(database_url) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT record FROM mbist_added_dies ORDER BY created_at, die_id"
                )
                return [row[0] for row in cursor.fetchall()]
    except psycopg.errors.UndefinedTable:
        return []
    except psycopg.Error as exc:
        logging.exception("Unable to read added die records from PostgreSQL")
        raise StorageError(
            "Unable to read added die records. Check DATABASE_URL and database access."
        ) from exc


def _read_die_overrides() -> dict[str, dict]:
    database_url = _database_url()
    if not database_url:
        return {}
    try:
        import psycopg
    except ImportError as exc:
        raise StorageError(
            "PostgreSQL support is unavailable. Install the project requirements."
        ) from exc
    try:
        with psycopg.connect(database_url) as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT die_id, record FROM mbist_die_overrides")
                return {die_id: record for die_id, record in cursor.fetchall()}
    except psycopg.errors.UndefinedTable:
        return {}
    except psycopg.Error as exc:
        logging.exception("Unable to read die record overrides from PostgreSQL")
        raise StorageError(
            "Unable to read die record updates. Check DATABASE_URL and database access."
        ) from exc


def get_dataset() -> dict[str, list[dict]]:
    data = {
        name: [dict(row) for row in rows]
        for name, rows in _get_base_dataset().items()
    }
    added_dies = _read_added_dies()
    overrides = _read_die_overrides()
    all_dies = data["dies"] + added_dies
    for die in all_dies:
        if die["die_id"] in overrides:
            die.update(overrides[die["die_id"]])
    data["dies"].extend(added_dies)
    return data


def initialize_die_storage() -> None:
    database_url = _database_url()
    if not database_url:
        return
    try:
        import psycopg
    except ImportError as exc:
        raise StorageError(
            "PostgreSQL support is unavailable. Install the project requirements."
        ) from exc
    try:
        with psycopg.connect(database_url) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    CREATE TABLE IF NOT EXISTS mbist_added_dies (
                        die_id TEXT PRIMARY KEY,
                        record JSONB NOT NULL,
                        created_at TIMESTAMPTZ NOT NULL DEFAULT now()
                    )
                    """
                )
                cursor.execute(
                    """
                    CREATE TABLE IF NOT EXISTS mbist_die_overrides (
                        die_id TEXT PRIMARY KEY,
                        record JSONB NOT NULL,
                        updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
                    )
                    """
                )
    except psycopg.Error as exc:
        logging.exception("Unable to initialize PostgreSQL die storage")
        raise StorageError(
            "Unable to initialize die storage. Check DATABASE_URL and database permissions."
        ) from exc


def validate_new_die(payload: object) -> dict:
    if not isinstance(payload, dict):
        raise ValueError("The die record must be a JSON object.")
    if set(payload) != NEW_DIE_FIELDS:
        missing = NEW_DIE_FIELDS - set(payload)
        extra = set(payload) - NEW_DIE_FIELDS
        details = []
        if missing:
            details.append(f"missing fields: {', '.join(sorted(missing))}")
        if extra:
            details.append(f"unexpected fields: {', '.join(sorted(extra))}")
        raise ValueError("Invalid die record (" + "; ".join(details) + ").")

    record = {}
    for field in sorted(NEW_DIE_TEXT_FIELDS):
        value = payload[field]
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{field} is required.")
        value = value.strip()
        if len(value) > 80:
            raise ValueError(f"{field} must be 80 characters or fewer.")
        if field == "final_bin" and value not in FINAL_BINS:
            raise ValueError("final_bin is not a supported bin.")
        record[field] = value

    for field in sorted(NEW_DIE_BOOLEAN_FIELDS):
        value = payload[field]
        if not isinstance(value, bool):
            raise ValueError(f"{field} must be true or false.")
        record[field] = value

    for field in sorted(NEW_DIE_INTEGER_FIELDS):
        value = payload[field]
        if isinstance(value, bool):
            raise ValueError(f"{field} must be an integer.")
        try:
            number = int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{field} must be an integer.") from exc
        if str(number) != str(value).strip() or number < 0 and field not in {"die_x", "die_y"}:
            raise ValueError(f"{field} must be a valid integer.")
        record[field] = number

    for field in sorted(NEW_DIE_FLOAT_FIELDS):
        value = payload[field]
        if isinstance(value, bool):
            raise ValueError(f"{field} must be numeric.")
        try:
            number = float(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{field} must be numeric.") from exc
        if not math.isfinite(number):
            raise ValueError(f"{field} must be finite.")
        if number < 0:
            raise ValueError(f"{field} cannot be negative.")
        record[field] = number

    if not 0 <= record["radial_pos"] <= 1:
        raise ValueError("radial_pos must be between 0 and 1.")
    if record["t_total_baseline_ms"] != round(
        record["t_march_ms"] + record["t_retention_ms"]
        + record["t_repair_ms"] + record["t_setup_readout_ms"], 1
    ):
        raise ValueError("t_total_baseline_ms must equal the sum of the time components.")
    return record


def validate_added_die(payload: object) -> dict:
    record = validate_new_die(payload)
    record["die_id"] = f"EXT-{uuid.uuid4().hex[:12].upper()}"
    return record


def validate_die_update(payload: object, die_id: str) -> dict:
    record = validate_new_die(payload)
    if not die_id or len(die_id) > 80:
        raise ValueError("die_id is invalid.")
    record["die_id"] = die_id
    return record


def _save_added_die(record: dict) -> None:
    database_url = _database_url()
    if not database_url:
        raise StorageError(
            "Die submissions are not enabled yet. Configure DATABASE_URL in Render."
        )
    try:
        import psycopg
        from psycopg.types.json import Jsonb
    except ImportError as exc:
        raise StorageError(
            "PostgreSQL support is unavailable. Install the project requirements."
        ) from exc
    try:
        with psycopg.connect(database_url) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    CREATE TABLE IF NOT EXISTS mbist_added_dies (
                        die_id TEXT PRIMARY KEY,
                        record JSONB NOT NULL,
                        created_at TIMESTAMPTZ NOT NULL DEFAULT now()
                    )
                    """
                )
                cursor.execute(
                    "INSERT INTO mbist_added_dies (die_id, record) VALUES (%s, %s)",
                    (record["die_id"], Jsonb(record)),
                )
    except psycopg.Error as exc:
        logging.exception("Unable to save added die record")
        raise StorageError(
            "Unable to save die record. Check DATABASE_URL and database permissions."
        ) from exc


def _save_die_override(record: dict) -> None:
    database_url = _database_url()
    if not database_url:
        raise StorageError(
            "Die updates are not enabled yet. Configure DATABASE_URL in Render."
        )
    try:
        import psycopg
        from psycopg.types.json import Jsonb
    except ImportError as exc:
        raise StorageError(
            "PostgreSQL support is unavailable. Install the project requirements."
        ) from exc
    try:
        with psycopg.connect(database_url) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO mbist_die_overrides (die_id, record)
                    VALUES (%s, %s)
                    ON CONFLICT (die_id) DO UPDATE
                    SET record = EXCLUDED.record, updated_at = now()
                    """,
                    (record["die_id"], Jsonb(record)),
                )
    except psycopg.Error as exc:
        logging.exception("Unable to save die record update")
        raise StorageError(
            "Unable to save die record update. Check DATABASE_URL and database permissions."
        ) from exc


def validate_request_origin(headers) -> bool:
    origin = headers.get("Origin")
    host = headers.get("Host")
    if not origin or not host:
        return False
    protocol = headers.get("X-Forwarded-Proto", "http").split(",", 1)[0].strip()
    if protocol not in {"http", "https"}:
        return False
    return hmac.compare_digest(origin.rstrip("/"), f"{protocol}://{host}".rstrip("/"))


_write_attempts: dict[str, list[float]] = {}
_write_attempts_lock = Lock()


def allow_public_write(client_ip: str, now: float | None = None) -> bool:
    current_time = now if now is not None else time.monotonic()
    with _write_attempts_lock:
        recent = [
            attempt for attempt in _write_attempts.get(client_ip, [])
            if current_time - attempt < 60
        ]
        if len(recent) >= 30:
            _write_attempts[client_ip] = recent
            return False
        recent.append(current_time)
        _write_attempts[client_ip] = recent
        if len(_write_attempts) > 10_000:
            _write_attempts.clear()
        return True


def _mean(rows: list[dict], column: str) -> float:
    return sum(row[column] for row in rows) / len(rows) if rows else 0.0


def build_summary(data: dict[str, list[dict]]) -> dict:
    dies = data["dies"]
    memories = data["memories"]
    failures = data["failures"]
    bins: dict[str, int] = {}
    lots: dict[str, list[dict]] = {}
    fault_types: dict[str, int] = {}
    memory_types: dict[str, int] = {}
    for die in dies:
        bins[die["final_bin"]] = bins.get(die["final_bin"], 0) + 1
        lots.setdefault(die["lot_id"], []).append(die)
    for event in failures:
        fault_types[event["fault_type"]] = fault_types.get(event["fault_type"], 0) + 1
    for memory in memories:
        memory_types[memory["mem_type"]] = memory_types.get(memory["mem_type"], 0) + 1

    return {
        "totals": {
            "dies": len(dies),
            "memories": len(memories),
            "fail_events": len(failures),
            "lots": len(lots),
            "controllers": len({row["controller_id"] for row in memories}),
            "total_bits": sum(row["total_bits"] for row in memories),
            "baseline_ms": round(_mean(dies, "t_total_baseline_ms"), 1),
            "test_power_mw": round(sum(row["test_power_mw"] for row in memories), 1),
            "scan_pass_pct": round(
                100 * sum(row["scan_pass"] for row in dies) / len(dies), 1
            ) if dies else 0.0,
        },
        "bins": [
            {"name": name, "count": count}
            for name, count in sorted(bins.items(), key=lambda item: (-item[1], item[0]))
        ],
        "lots": [
            {
                "name": name,
                "dies": len(rows),
                "fails": sum(row["final_bin"].startswith("FAIL") for row in rows),
                "mean_ms": round(_mean(rows, "t_total_baseline_ms"), 1),
            }
            for name, rows in sorted(lots.items())
        ],
        "fault_types": [
            {"name": name, "count": count}
            for name, count in sorted(
                fault_types.items(), key=lambda item: (-item[1], item[0])
            )
        ],
        "memory_types": [
            {"name": name, "count": count}
            for name, count in sorted(
                memory_types.items(), key=lambda item: (-item[1], item[0])
            )
        ],
    }


def _upper_escape_bound(misses: int, total: int) -> float:
    # Agresti-Coull-style upper bound used by validate_mbist_methods.py.
    adjusted_total = total + 4
    adjusted_misses = misses + 2
    return (
        adjusted_misses / adjusted_total
        + 1.645
        * math.sqrt(
            adjusted_misses * (adjusted_total - adjusted_misses)
            / adjusted_total**3
        )
    )


def _schedule(memories: list[dict], sort_by_time: bool) -> dict:
    ordered = sorted(
        memories,
        key=lambda row: (
            -row["march_ss_time_us"] if sort_by_time else row["mem_id"],
            row["mem_id"],
        ),
    )
    power_cap = 0.08 * sum(row["test_power_mw"] for row in memories)
    sessions: list[dict] = []
    for memory in ordered:
        for session in sessions:
            if (
                session["power_mw"] + memory["test_power_mw"] <= power_cap
                and memory["controller_id"] not in session["controllers"]
            ):
                session["power_mw"] += memory["test_power_mw"]
                session["controllers"].add(memory["controller_id"])
                session["time_us"] = max(
                    session["time_us"], memory["march_ss_time_us"]
                )
                break
        else:
            sessions.append(
                {
                    "power_mw": memory["test_power_mw"],
                    "controllers": {memory["controller_id"]},
                    "time_us": memory["march_ss_time_us"],
                }
            )
    total_ms = sum(session["time_us"] for session in sessions) / 1000
    return {"sessions": len(sessions), "total_ms": round(total_ms, 2)}


def read_adaptive_reference(
    report_path: Path = ROOT / "validation_report.txt",
) -> dict:
    try:
        report = report_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise DatasetError(
            f"Unable to read adaptive-flow reference report {report_path}: "
            f"{exc.strerror or exc}."
        ) from exc

    prevalence_match = re.search(
        r"target prevalence \(test\):\s*([\d.]+)%\s+"
        r"ROC AUC on held-out lots:\s*([\d.]+)",
        report,
    )
    best_match = re.search(
        r"largest low tier within 50 DPPM budget:\s*([\d.]+)% of die,\s*"
        r"([\d.]+)% MBIST time saved",
        report,
    )
    sweep_rows = []
    in_sweep = False
    for line in report.splitlines():
        if "tier sweep" in line:
            in_sweep = True
            continue
        if in_sweep and "largest low tier" in line:
            break
        if in_sweep:
            cells = re.match(
                r"^\s*([\d.]+)\s+([\d.]+)\s+([\d.]+)\s+([\d.]+)\s+([\d.]+)\s*$",
                line,
            )
            if cells:
                low_tier_pct, mean_ms, saving_pct, escapes, added_dppm = (
                    float(value) for value in cells.groups()
                )
                sweep_rows.append(
                    {
                        "low_tier_pct": low_tier_pct,
                        "mean_ms": mean_ms,
                        "saving_pct": saving_pct,
                        "expected_escapes": escapes,
                        "added_dppm": added_dppm,
                    }
                )
    if not prevalence_match or not best_match or not sweep_rows:
        raise DatasetError(
            f"{report_path.name} does not contain a complete adaptive-flow "
            "reference result. Run validate_mbist_methods.py to regenerate it."
        )

    best_low_tier = float(best_match.group(1))
    best_row = next(
        (row for row in sweep_rows if row["low_tier_pct"] == best_low_tier),
        None,
    )
    if best_row is None:
        raise DatasetError(
            f"{report_path.name} is inconsistent: its selected low tier is "
            "missing from the tier sweep."
        )
    return {
        "target_prevalence_pct": float(prevalence_match.group(1)),
        "held_out_auc": float(prevalence_match.group(2)),
        "dppm_budget": 50,
        "best_low_tier_pct": best_low_tier,
        "best_saving_pct": float(best_match.group(2)),
        "best_added_dppm": best_row["added_dppm"],
        "sweep": sweep_rows,
        "source": report_path.name,
    }


def build_analysis(
    data: dict[str, list[dict]],
    report_path: Path = ROOT / "validation_report.txt",
) -> dict:
    failures = data["failures"]
    by_bitcell: dict[str, list[dict]] = {}
    for event in failures:
        by_bitcell.setdefault(event["bitcell"], []).append(event)

    algorithms = []
    for bitcell, events in sorted(by_bitcell.items()):
        selected = None
        coverage = []
        for name, column, operations in ALGORITHMS:
            misses = sum(not event[column] for event in events)
            upper_bound = _upper_escape_bound(misses, len(events))
            entry = {
                "name": name,
                "operations_per_address": operations,
                "coverage_pct": round(100 * (1 - misses / len(events)), 2),
                "misses": misses,
                "escape_rate_pct": round(100 * misses / len(events), 3),
                "upper_bound_pct": round(100 * upper_bound, 3),
            }
            coverage.append(entry)
            if selected is None and upper_bound <= 0.005:
                selected = entry
        algorithms.append(
            {
                "bitcell": bitcell,
                "events": len(events),
                "recommended": selected["name"] if selected else None,
                "coverage": coverage,
            }
        )

    memory_rows = data["memories"]
    naive = _schedule(memory_rows, sort_by_time=False)
    sorted_schedule = _schedule(memory_rows, sort_by_time=True)
    improvement = (
        100 * (1 - sorted_schedule["total_ms"] / naive["total_ms"])
        if naive["total_ms"]
        else 0
    )
    return {
        "adaptive_reference": read_adaptive_reference(report_path),
        "algorithm_pruning": algorithms,
        "scheduling": {
            "power_cap_mw": round(
                0.08 * sum(row["test_power_mw"] for row in memory_rows), 1
            ),
            "inventory_order": naive,
            "longest_first": sorted_schedule,
            "improvement_pct": round(improvement, 1),
        },
    }


TABLE_FIELDS = {
    "dies": (
        "die_id", "lot_id", "wafer_id", "die_x", "die_y", "radial_pos",
        "ro_speed_mhz", "leakage_na", "bitcell_vmin_mv", "scan_pass",
        "march_failing_mems", "final_bin", "t_total_baseline_ms",
    ),
    "memories": (
        "mem_id", "mem_type", "bitcell", "words", "width_bits", "total_bits",
        "ports", "test_clock_mhz", "test_power_mw", "controller_id",
        "has_repair", "march_ss_time_us",
    ),
    "failures": (
        "die_id", "mem_id", "mem_type", "bitcell", "fault_type",
        "cheapest_detecting_algo", "detect_background", "detect_corner",
        "det_MATSplus", "det_MarchCm", "det_MarchLR", "det_MarchCm_RD",
        "det_MarchSS",
    ),
}


def query_table(
    data: dict[str, list[dict]], table: str, query: dict[str, list[str]]
) -> dict:
    if table not in TABLE_FIELDS:
        raise ValueError("Unknown dataset table.")
    try:
        limit = int(query.get("limit", ["25"])[0])
        offset = int(query.get("offset", ["0"])[0])
    except ValueError as exc:
        raise ValueError("limit and offset must be integers.") from exc
    if not 1 <= limit <= 200 or offset < 0:
        raise ValueError("limit must be 1-200 and offset must be 0 or greater.")

    filters = {
        "dies": (("lot_id", "lot"), ("final_bin", "bin")),
        "memories": (("mem_type", "mem_type"), ("bitcell", "bitcell")),
        "failures": (("fault_type", "fault_type"), ("bitcell", "bitcell")),
    }[table]
    rows = data[table]
    for column, parameter in filters:
        value = query.get(parameter, [""])[0].strip()
        if value:
            rows = [row for row in rows if row[column] == value]

    search = query.get("search", [""])[0].strip().lower()
    if search:
        fields = {
            "dies": ("die_id", "wafer_id"),
            "memories": ("mem_id", "controller_id"),
            "failures": ("die_id", "mem_id"),
        }[table]
        rows = [
            row for row in rows
            if any(search in str(row[column]).lower() for column in fields)
        ]
    return {
        "items": [
            {field: row.get(field) for field in TABLE_FIELDS[table]}
            for row in rows[offset:offset + limit]
        ],
        "total": len(rows),
        "limit": limit,
        "offset": offset,
        "fields": TABLE_FIELDS[table],
    }


class MbistRequestHandler(BaseHTTPRequestHandler):
    server_version = "MBISTDashboard/1.0"

    def _headers(self, content_type: str, content_length: int) -> None:
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(content_length))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data:; connect-src 'self'; object-src 'none'; "
            "base-uri 'none'; frame-ancestors 'none'",
        )
        self.send_header("Cache-Control", "no-store")

    def _send_bytes(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self._headers(content_type, len(body))
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, allow_nan=False).encode("utf-8")
        self._send_bytes(status, body, "application/json; charset=utf-8")

    def _send_error_json(self, status: int, message: str) -> None:
        self._send_json(status, {"error": message})

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        query = parse_qs(parsed.query, keep_blank_values=True)
        try:
            if path == "/":
                self._send_static("index.html")
            elif path in ("/frontend/app.js", "/frontend/styles.css"):
                self._send_static(path.removeprefix("/frontend/"))
            elif path == "/api/health":
                data = get_dataset()
                self._send_json(
                    200,
                    {
                        "status": "ok",
                        "rows": {name: len(rows) for name, rows in data.items()},
                        "public_die_edits_enabled": bool(_database_url()),
                    },
                )
            elif path == "/api/die-editing/status":
                self._send_json(
                    200,
                    {
                        "configured": bool(_database_url()),
                        "storage_configured": bool(_database_url()),
                        "public_access": True,
                    },
                )
            elif path == "/api/summary":
                self._send_json(200, build_summary(get_dataset()))
            elif path == "/api/analysis":
                self._send_json(200, build_analysis(get_dataset()))
            elif path in ("/api/dies", "/api/memories", "/api/failures"):
                table = path.rsplit("/", 1)[-1]
                self._send_json(200, query_table(get_dataset(), table, query))
            elif path.startswith("/api/dies/"):
                die_id = path.removeprefix("/api/dies/")
                die = next(
                    (row for row in get_dataset()["dies"] if row["die_id"] == die_id),
                    None,
                )
                if die is None:
                    self._send_error_json(404, "Die record not found.")
                else:
                    self._send_json(200, {"item": die})
            elif path.startswith("/api/download/"):
                table = path.rsplit("/", 1)[-1]
                if table not in DATA_FILES:
                    self._send_error_json(404, "Unknown dataset export.")
                else:
                    self._send_download(table)
            else:
                self._send_error_json(404, "Route not found.")
        except ValueError as exc:
            self._send_error_json(400, str(exc))
        except StorageError as exc:
            logging.error("Die storage unavailable: %s", exc)
            self._send_error_json(503, str(exc))
        except DatasetError as exc:
            logging.exception("Dataset unavailable or invalid")
            self._send_error_json(500, str(exc))

    def _read_json_body(self) -> dict:
        content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip()
        if content_type != "application/json":
            raise ValueError("Content-Type must be application/json.")
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise ValueError("Content-Length must be a valid integer.") from exc
        if not 0 < length <= 16_384:
            raise ValueError("Request body must be between 1 and 16384 bytes.")
        try:
            payload = json.loads(self.rfile.read(length))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ValueError("Request body must contain valid JSON.") from exc
        if not isinstance(payload, dict):
            raise ValueError("Request body must be a JSON object.")
        return payload

    def _request_origin_is_valid(self) -> bool:
        return validate_request_origin(self.headers)

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        try:
            if not self._request_origin_is_valid():
                self._send_error_json(403, "Request origin is not allowed.")
                return
            if path == "/api/dies":
                client_ip = self.headers.get(
                    "X-Forwarded-For", self.client_address[0]
                ).split(",", 1)[0].strip()
                if not allow_public_write(client_ip):
                    self._send_error_json(
                        429, "Too many changes from this connection. Please wait a minute and try again."
                    )
                    return
                if not _database_url():
                    self._send_error_json(
                        503, "Die submissions are not enabled yet. Configure DATABASE_URL in Render."
                    )
                    return
                record = validate_added_die(self._read_json_body())
                _save_added_die(record)
                self._send_json(201, {"item": record, "saved": True})
            else:
                self._send_error_json(404, "Route not found.")
        except ValueError as exc:
            self._send_error_json(400, str(exc))
        except StorageError as exc:
            logging.error("Die storage unavailable: %s", exc)
            self._send_error_json(503, str(exc))

    def do_PUT(self) -> None:
        path = urlparse(self.path).path
        prefix = "/api/dies/"
        try:
            if not self._request_origin_is_valid():
                self._send_error_json(403, "Request origin is not allowed.")
                return
            if not path.startswith(prefix):
                self._send_error_json(404, "Route not found.")
                return
            client_ip = self.headers.get(
                "X-Forwarded-For", self.client_address[0]
            ).split(",", 1)[0].strip()
            if not allow_public_write(client_ip):
                self._send_error_json(
                    429, "Too many changes from this connection. Please wait a minute and try again."
                )
                return
            if not _database_url():
                self._send_error_json(
                    503, "Die updates are not enabled yet. Configure DATABASE_URL in Render."
                )
                return
            die_id = unquote(path[len(prefix):])
            if not die_id or "/" in die_id:
                self._send_error_json(400, "die_id is invalid.")
                return
            current = next(
                (row for row in get_dataset()["dies"] if row["die_id"] == die_id),
                None,
            )
            if current is None:
                self._send_error_json(404, "Die record not found.")
                return
            record = validate_die_update(self._read_json_body(), die_id)
            _save_die_override(record)
            self._send_json(200, {"item": record, "saved": True})
        except ValueError as exc:
            self._send_error_json(400, str(exc))
        except StorageError as exc:
            logging.error("Die storage unavailable: %s", exc)
            self._send_error_json(503, str(exc))

    def do_DELETE(self) -> None:
        self._send_error_json(404, "Route not found.")

    def _send_static(self, filename: str) -> None:
        if filename not in ("index.html", "app.js", "styles.css"):
            self._send_error_json(404, "File not found.")
            return
        path = WEB_ROOT / filename
        try:
            body = path.read_bytes()
        except OSError as exc:
            logging.exception("Unable to read static asset %s", path)
            self._send_error_json(500, f"Unable to read web asset: {filename}.")
            return
        content_type = {
            ".html": "text/html; charset=utf-8",
            ".js": "text/javascript; charset=utf-8",
            ".css": "text/css; charset=utf-8",
        }[path.suffix]
        self._send_bytes(200, body, content_type)

    def _send_download(self, table: str) -> None:
        path = ROOT / "data" / DATA_FILES[table]
        try:
            body = path.read_bytes()
        except OSError as exc:
            logging.exception("Unable to read dataset export %s", path)
            self._send_error_json(500, f"Unable to read {DATA_FILES[table]}.")
            return
        if table == "dies":
            added_dies = _read_added_dies()
            overrides = _read_die_overrides()
            for record in added_dies:
                if record["die_id"] in overrides:
                    record.update(overrides[record["die_id"]])
            if overrides:
                with path.open("r", encoding="utf-8-sig", newline="") as source:
                    reader = csv.DictReader(source)
                    original = list(reader)
                    fieldnames = reader.fieldnames or []
                extra = io.StringIO(newline="")
                writer = csv.DictWriter(extra, fieldnames=fieldnames, lineterminator="\n")
                for row in original:
                    override = overrides.get(row["die_id"])
                    if override:
                        row.update({key: str(value) for key, value in override.items()})
                    writer.writerow(row)
                body = (",".join(fieldnames) + "\n").encode("utf-8") + extra.getvalue().encode("utf-8")
            if added_dies:
                with path.open("r", encoding="utf-8-sig", newline="") as source:
                    fieldnames = next(csv.reader(source))
                extra = io.StringIO(newline="")
                writer = csv.DictWriter(
                    extra, fieldnames=fieldnames, lineterminator="\n"
                )
                writer.writerows(added_dies)
                body += (b"" if body.endswith((b"\n", b"\r")) else b"\n")
                body += extra.getvalue().encode("utf-8")
        self.send_response(200)
        self._headers("text/csv; charset=utf-8", len(body))
        self.send_header(
            "Content-Disposition", f'attachment; filename="{DATA_FILES[table]}"'
        )
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format_string: str, *args) -> None:
        logging.info("%s - %s", self.address_string(), format_string % args)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the local MBIST data dashboard.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("--port must be between 1 and 65535")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    initialize_die_storage()
    data = get_dataset()
    logging.info(
        "Loaded %d dies, %d memories, and %d fail events",
        len(data["dies"]), len(data["memories"]), len(data["failures"]),
    )
    server = ThreadingHTTPServer((args.host, args.port), MbistRequestHandler)
    logging.info("Dashboard available at http://%s:%d", args.host, args.port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logging.info("Shutting down dashboard.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
