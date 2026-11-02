"""Dependency-free local web application for exploring synthetic MBIST data."""

from __future__ import annotations

import argparse
import csv
import json
import logging
import math
import re
from functools import lru_cache
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
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
ALGORITHMS = (
    ("MATSplus", "det_MATSplus", 5),
    ("MarchCm", "det_MarchCm", 10),
    ("MarchLR", "det_MarchLR", 14),
    ("MarchCm_RD", "det_MarchCm_RD", 15),
    ("MarchSS", "det_MarchSS", 22),
)


class DatasetError(Exception):
    """Raised when an input dataset is missing or malformed."""


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
def get_dataset() -> dict[str, list[dict]]:
    return load_dataset()


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
                    },
                )
            elif path == "/api/summary":
                self._send_json(200, build_summary(get_dataset()))
            elif path == "/api/analysis":
                self._send_json(200, build_analysis(get_dataset()))
            elif path in ("/api/dies", "/api/memories", "/api/failures"):
                table = path.rsplit("/", 1)[-1]
                self._send_json(200, query_table(get_dataset(), table, query))
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
        except DatasetError as exc:
            logging.exception("Dataset unavailable or invalid")
            self._send_error_json(500, str(exc))

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
