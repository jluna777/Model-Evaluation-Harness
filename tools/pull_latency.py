"""Pull per-call latencies from Langfuse into a committed JSON export.

One-shot evidence tool (T19): fetches the ``candidate`` / ``judge:<field>``
observation spans that ``harness.tracing`` emits (one span per API call,
spec §8), computes per-call latency from the span timestamps, and writes a
self-contained export with both the raw per-observation rows and the
summary statistics the README quotes -- so the summary is recomputable
from the committed file alone, without Langfuse access.

Trace binding disclosure: the CLI does not currently thread ``run_id``
into ``TraceContext.for_run`` (the deterministic ``_new_trace_id(run_id)``
seam exists but is unused), so traces cannot be looked up by run identity.
This tool instead selects observations by a wall-clock window and groups
them by ``traceId``; within an ``eval compare`` window the two runs execute
sequentially, so trace groups are ordered by their earliest span and
labeled positionally (first -> candidate a, second -> candidate b). The
export records this binding method verbatim. Threading run identity into
trace ids is the booked post-v1 fix that retires the heuristic.

Usage (keys read from the environment / .env, never from argv):

    uv run python tools/pull_latency.py --from 2026-07-19T21:00:00Z \
        --to 2026-07-19T22:00:00Z --out results/published/latency-export.json
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
from dotenv import load_dotenv

_DEFAULT_HOST = "https://cloud.langfuse.com"
_PAGE_LIMIT = 100
_SPAN_PREFIXES = ("candidate", "judge:")


def _resolve_host() -> str:
    """Mirror harness.tracing._resolve_langfuse_host's env precedence
    (LANGFUSE_BASE_URL, then the deprecated LANGFUSE_HOST alias) without
    importing harness -- this tool must stay standalone."""

    for name in ("LANGFUSE_BASE_URL", "LANGFUSE_HOST"):
        value = os.environ.get(name)
        if value:
            return value.rstrip("/")
    return _DEFAULT_HOST


def _parse_ts(raw: str) -> datetime:
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    return datetime.fromisoformat(raw).astimezone(UTC)


def _fetch_observations(
    client: httpx.Client, host: str, from_ts: datetime, to_ts: datetime
) -> list[dict[str, Any]]:
    """Paginate GET /api/public/observations for the window; keep only the
    harness's per-call spans (candidate / judge:<field>)."""

    rows: list[dict[str, Any]] = []
    page = 1
    while True:
        resp = client.get(
            f"{host}/api/public/observations",
            params={
                "page": page,
                "limit": _PAGE_LIMIT,
                "fromStartTime": from_ts.isoformat(),
                "toStartTime": to_ts.isoformat(),
            },
        )
        resp.raise_for_status()
        body = resp.json()
        data = body.get("data", [])
        rows.extend(
            row
            for row in data
            if isinstance(row.get("name"), str) and row["name"].startswith(_SPAN_PREFIXES)
        )
        meta = body.get("meta", {})
        if "totalPages" not in meta and len(data) == _PAGE_LIMIT:
            print(
                "warning: response meta lacks totalPages on a full page; "
                "stopping pagination -- the export may be incomplete",
                file=sys.stderr,
            )
        total_pages = int(meta.get("totalPages", page))
        if page >= total_pages or not data:
            return rows
        page += 1


def _latency_ms(row: dict[str, Any]) -> float | None:
    start, end = row.get("startTime"), row.get("endTime")
    if not start or not end:
        return None
    return (_parse_ts(end) - _parse_ts(start)).total_seconds() * 1000.0


def _percentile(values: list[float], q: float) -> float:
    """Nearest-rank percentile -- deliberately simple and stdlib-only so the
    committed export's summary is trivially recomputable by hand."""

    ordered = sorted(values)
    rank = max(1, round(q / 100.0 * len(ordered)))
    return ordered[rank - 1]


def _summarize(values: list[float]) -> dict[str, float | int]:
    return {
        "n": len(values),
        "mean_ms": round(statistics.fmean(values), 1),
        "p50_ms": round(_percentile(values, 50), 1),
        "p95_ms": round(_percentile(values, 95), 1),
        "min_ms": round(min(values), 1),
        "max_ms": round(max(values), 1),
    }


def _group_traces(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Group observations by traceId; order groups by earliest span start
    (the positional a-then-b binding documented in the module docstring)."""

    by_trace: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        by_trace.setdefault(str(row.get("traceId")), []).append(row)

    groups: list[dict[str, Any]] = []
    for trace_id, trace_rows in by_trace.items():
        starts = sorted(r["startTime"] for r in trace_rows if r.get("startTime"))
        candidate_lat = [
            ms
            for r in trace_rows
            if r["name"] == "candidate" and (ms := _latency_ms(r)) is not None
        ]
        judge_lat = [
            ms
            for r in trace_rows
            if r["name"].startswith("judge:") and (ms := _latency_ms(r)) is not None
        ]
        groups.append(
            {
                "trace_id": trace_id,
                "first_span_at": starts[0] if starts else None,
                "last_span_at": starts[-1] if starts else None,
                "n_candidate_calls": len(candidate_lat),
                "n_judge_calls": len(judge_lat),
                "candidate_latency": _summarize(candidate_lat) if candidate_lat else None,
                "judge_latency": _summarize(judge_lat) if judge_lat else None,
            }
        )
    groups.sort(key=lambda g: (g["first_span_at"] is None, g["first_span_at"]))
    return groups


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--from", dest="from_ts", required=True, help="window start (ISO 8601)")
    parser.add_argument("--to", dest="to_ts", required=True, help="window end (ISO 8601)")
    parser.add_argument(
        "--out",
        default="results/published/latency-export.json",
        help="output path for the committed export",
    )
    args = parser.parse_args(argv)

    load_dotenv()
    public_key = os.environ.get("LANGFUSE_PUBLIC_KEY")
    secret_key = os.environ.get("LANGFUSE_SECRET_KEY")
    if not public_key or not secret_key:
        print(
            "LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY are required "
            "(environment or .env).",
            file=sys.stderr,
        )
        return 2

    host = _resolve_host()
    from_ts, to_ts = _parse_ts(args.from_ts), _parse_ts(args.to_ts)

    with httpx.Client(auth=(public_key, secret_key), timeout=60.0) as client:
        rows = _fetch_observations(client, host, from_ts, to_ts)

    observations = sorted(
        (
            {
                "trace_id": str(row.get("traceId")),
                "name": row["name"],
                "item_id": (row.get("metadata") or {}).get("item_id"),
                "replicate": (row.get("metadata") or {}).get("replicate"),
                "start_time": row.get("startTime"),
                "latency_ms": round(ms, 1) if (ms := _latency_ms(row)) is not None else None,
            }
            for row in rows
        ),
        key=lambda r: (r["start_time"] or "", r["trace_id"], r["name"]),
    )

    export = {
        "pulled_at": datetime.now(UTC).isoformat(),
        "host": host,
        "window": {"from": from_ts.isoformat(), "to": to_ts.isoformat()},
        "binding_method": (
            "time-window + span-shape: traces are grouped by traceId and ordered "
            "by earliest span; run identity is NOT stamped on spans in v1 "
            "(run_id threading into trace ids is the booked post-v1 fix)"
        ),
        "trace_groups": _group_traces(rows),
        "observations": observations,
    }

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(export, indent=2) + "\n", encoding="utf-8")
    print(
        f"Wrote {len(observations)} observations across "
        f"{len(export['trace_groups'])} trace(s) to {out_path}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
