"""
run_system_tests.py — Batch system test over driving-session CSVs.

Runs the full Carbon MAS v2 pipeline on every CSV under a data directory and
stores one JSON report per session under a timestamped run folder.

Two dataset layouts are auto-detected per file:

  1. Real data:        data/<vehicle>/<trip>.csv
       vehicle_id = "<vehicle>-<VIN>"        e.g. "creta-9BHPC81EBTP234663"
       Files directly under the data dir use their filename stem as <vehicle>.

  2. Synthetic tests:  data_synthetic/csv_testes_<vehicle>_viagem_<n>/<NN_scenario>.csv
       vehicle_id = "<vehicle>-<VIN>-rodada-<NN>"
       Keeps each scenario isolated so fraud/anomaly cases don't pollute one
       another's Z-score history. The scenario label and trip number are stored
       in the manifest for analysis.

When the VIN column is missing/null (pandas reads "null" as NaN), "unknownvin"
is used as the suffix.

History isolation:
    If the data dir name contains "synthetic", the SQLite history defaults to
    a separate DB (carbon_mas_synthetic.db) so the real-vehicle Z-score history
    stays clean. Override with --db.

Usage:
    python run_system_tests.py                              # real data → blockchain
    python run_system_tests.py --data-dir data_synthetic    # synthetic → separate DB
    python run_system_tests.py --dry-run                    # skip blockchain
"""
from __future__ import annotations

import argparse
import io
import json
import re
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

import config

# Folder pattern for synthetic scenario sets: csv_testes_<vehicle>_viagem_<n>
_SYNTHETIC_DIR_RE = re.compile(r"^csv_testes_(?P<vehicle>.+)_viagem_(?P<viagem>\d+)$")
_LEADING_NUM_RE = re.compile(r"^(\d+)")


def _slug(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", text).strip("-")


def _extract_vin(df: pd.DataFrame) -> str:
    """First non-null VIN in the CSV, or 'unknownvin'. pandas reads 'null' as NaN."""
    if "VIN" in df.columns:
        vins = df["VIN"].dropna().astype(str).str.strip()
        vins = vins[vins != ""]
        if not vins.empty:
            return vins.iloc[0]
    return "unknownvin"


def _classify(csv_path: Path, data_dir: Path, df: pd.DataFrame):
    """
    Return (vehicle_id, meta) for a CSV, auto-detecting real vs synthetic layout.
    `meta` carries dataset/vehicle/viagem/scenario fields for the manifest.
    """
    from tools.sanitize import sanitize_vehicle_id

    vin = _extract_vin(df)
    parent = csv_path.parent
    m = _SYNTHETIC_DIR_RE.match(parent.name)

    if m:
        vehicle = m.group("vehicle")
        viagem = m.group("viagem")
        num_match = _LEADING_NUM_RE.match(csv_path.stem)
        rodada = num_match.group(1) if num_match else csv_path.stem
        # Include the trip number (viagem) so two trips of the same vehicle/rodada
        # don't collapse to the same vehicle_id — otherwise the second trip reuses
        # the first's id and trips the API per-vehicle MIN_SESSION_GAP_SECONDS (429).
        vehicle_id = sanitize_vehicle_id(f"{vehicle}-{vin}-v{viagem}-rodada-{rodada}"[:50])
        meta = {
            "dataset":  "synthetic",
            "vehicle":  vehicle,
            "viagem":   viagem,
            "scenario": csv_path.stem,
            "rodada":   rodada,
        }
    else:
        vehicle = csv_path.stem if parent == data_dir else parent.name
        vehicle_id = sanitize_vehicle_id(f"{vehicle}-{vin}"[:50])
        meta = {"dataset": "real", "vehicle": vehicle, "scenario": csv_path.stem}

    return vehicle_id, meta


def _run_via_api(
    *,
    api_url: str,
    csv_bytes: bytes,
    csv_name: str,
    vehicle_id: str,
    recipient: str,
    engine_cc: float,
    dry_run: bool,
    poll_interval: float,
    poll_timeout: float,
) -> dict:
    """
    Submit one CSV to a running API server and return the full pipeline report.

    Mirrors the dict shape returned by CarbonMASOrchestrator.run(), so the
    manifest/report-writing code below is identical for local and API modes.

    Flow: POST /process (multipart) → poll GET /status/{job_id} until the job
    resolves → GET /result/{job_id} for the full report.
    """
    import httpx

    base = api_url.rstrip("/")
    with httpx.Client(timeout=30.0) as client:
        resp = client.post(
            f"{base}/process",
            files={"file": (csv_name, csv_bytes, "text/csv")},
            data={
                "vehicle_id": vehicle_id,
                "recipient":  recipient,
                "engine_cc":  str(engine_cc),
                "dry_run":    "true" if dry_run else "false",
            },
        )
        resp.raise_for_status()
        job_id = resp.json().get("job_id")
        if not job_id:
            raise RuntimeError(f"API did not return a job_id: {resp.text}")

        deadline = time.time() + poll_timeout
        while True:
            if time.time() > deadline:
                raise TimeoutError(f"job {job_id} did not finish within {poll_timeout}s")
            status_resp = client.get(f"{base}/status/{job_id}")
            status_resp.raise_for_status()
            status_data = status_resp.json()
            state = status_data.get("status")
            if state in ("done", "dry_run"):
                break
            if state == "failed":
                raise RuntimeError(f"job {job_id} failed: {status_data.get('error')}")
            time.sleep(poll_interval)

        result_resp = client.get(f"{base}/result/{job_id}")
        result_resp.raise_for_status()
        return result_resp.json()


def main() -> int:
    parser = argparse.ArgumentParser(description="Batch system test over driving-session CSVs.")
    parser.add_argument("--data-dir", default="data", help="Root folder to scan for CSVs (default: data)")
    parser.add_argument("--results-dir", default="results", help="Where to write JSON reports (default: results)")
    parser.add_argument("--recipient", default=config.EMISSION_ADDRESS,
                        help="Ethereum address that receives minted NFTs (default: EMISSION_ADDRESS)")
    parser.add_argument("--engine-cc", type=float, default=config.DEFAULT_ENGINE_CC,
                        help=f"Engine displacement (default: {config.DEFAULT_ENGINE_CC})")
    parser.add_argument("--db", default=None,
                        help="SQLite history DB path (default: carbon_mas_synthetic.db for "
                             "synthetic dirs, else the project DB)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Skip blockchain writes (phases 1-3 only)")
    parser.add_argument("--api", action="store_true",
                        help="Submit each CSV to a running API server instead of calling the "
                             "orchestrator in-process. The pipeline then runs in the server "
                             "process using the SERVER's history DB (--db is ignored).")
    parser.add_argument("--api-url", default="http://localhost:8000",
                        help="Base URL of the API server (default: http://localhost:8000)")
    parser.add_argument("--poll-interval", type=float, default=2.0,
                        help="Seconds between status polls in --api mode (default: 2.0)")
    parser.add_argument("--poll-timeout", type=float, default=600.0,
                        help="Max seconds to wait for one job in --api mode (default: 600)")
    args = parser.parse_args()

    data_dir = Path(args.data_dir).resolve()
    if not data_dir.is_dir():
        print(f"ERROR: data dir not found: {data_dir}")
        return 1

    # In --api mode the server validates the recipient address (0x + 42 chars)
    # regardless of dry_run, so a valid address is always required there.
    if args.api and not args.recipient:
        print("ERROR: --api requires --recipient (the server validates the address "
              "even for dry runs). Set EMISSION_ADDRESS in .env or pass --recipient.")
        return 1
    if not args.api and not args.dry_run and not args.recipient:
        print("ERROR: no --recipient and EMISSION_ADDRESS is empty. "
              "Set it in .env or pass --recipient (or use --dry-run).")
        return 1

    orch = None
    if args.api:
        print(f"API mode: submitting to {args.api_url}")
        print("NOTE: the pipeline runs in the SERVER process using the server's history DB. "
              "--db is ignored; ensure synthetic-history isolation is configured server-side.\n")
    else:
        # ── History isolation: separate DB for synthetic data ─────────────────
        is_synthetic_dir = "synthetic" in data_dir.name.lower()
        if args.db:
            config.DB_PATH = Path(args.db).resolve()
        elif is_synthetic_dir:
            config.DB_PATH = config.BASE_DIR / "carbon_mas_synthetic.db"
        # else: keep the project default (config.DB_PATH already set)

        # Import AFTER DB_PATH is finalized; _conn() reads config.DB_PATH lazily.
        from session_store import init_db
        from orchestrator import CarbonMASOrchestrator
        init_db()
        orch = CarbonMASOrchestrator()

    csv_paths = sorted(data_dir.rglob("*.csv"))
    if not csv_paths:
        print(f"No CSV files found under {data_dir}")
        return 1

    run_id = datetime.now(timezone.utc).strftime("run_%Y%m%dT%H%M%SZ")
    out_dir = Path(args.results_dir).resolve() / run_id
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"Found {len(csv_paths)} CSV(s). Mode: {'DRY-RUN' if args.dry_run else 'BLOCKCHAIN'}"
          f" | {'API' if args.api else 'LOCAL'}")
    if not args.api:
        print(f"History DB: {config.DB_PATH}")
    print(f"Results   : {out_dir}\n")

    manifest: list[dict] = []

    for i, csv_path in enumerate(csv_paths, 1):
        rel = csv_path.relative_to(data_dir)
        try:
            csv_bytes = csv_path.read_bytes()
            df = pd.read_csv(io.BytesIO(csv_bytes))
            vehicle_id, meta = _classify(csv_path, data_dir, df)
        except Exception as exc:
            print(f"[{i}/{len(csv_paths)}] {rel}  ✗ pre-processing failed: {exc}")
            manifest.append({"csv": str(rel), "status": "error",
                             "stage": "preprocess", "error": str(exc)})
            continue

        print(f"[{i}/{len(csv_paths)}] {rel}  →  vehicle_id={vehicle_id}")
        t0 = time.time()
        try:
            if args.api:
                report = _run_via_api(
                    api_url=args.api_url,
                    csv_bytes=csv_bytes,
                    csv_name=csv_path.name,
                    vehicle_id=vehicle_id,
                    recipient=args.recipient,
                    engine_cc=args.engine_cc,
                    dry_run=args.dry_run,
                    poll_interval=args.poll_interval,
                    poll_timeout=args.poll_timeout,
                )
            else:
                report = orch.run(
                    csv_bytes=csv_bytes,
                    vehicle_id=vehicle_id,
                    recipient_address=args.recipient,
                    engine_cc=args.engine_cc,
                    dry_run=args.dry_run,
                )
            elapsed = round(time.time() - t0, 2)

            out_name = f"{_slug(vehicle_id)}__{_slug(str(rel.with_suffix('')))}.json"
            (out_dir / out_name).write_text(
                json.dumps(report, indent=2, ensure_ascii=False, default=str),
                encoding="utf-8",
            )

            summary = report.get("summary", {})
            lat = report.get("phase_latencies_s", {})
            manifest.append({
                "csv":                 str(rel),
                "vehicle_id":          vehicle_id,
                **meta,
                "report_file":         out_name,
                "status":              "ok",
                "elapsed_s":           elapsed,
                "lat_sensor_s":        lat.get("sensor"),
                "lat_validator_s":     lat.get("validator"),
                "lat_governance_s":    lat.get("governance"),
                "lat_blockchain_s":    lat.get("blockchain"),
                "pipeline_status":     report.get("pipeline_status"),
                "total_co2_g":         round((summary.get("total_co2_mg") or 0) / 1000, 2),
                "co2_saved_g":         round((summary.get("co2_saved_mg") or 0) / 1000, 2),
                "distance_km":         summary.get("distance_km"),
                "confidence":          summary.get("confidence"),
                "anomaly":             summary.get("anomaly"),
                "governance_decision": summary.get("governance_decision"),
                "credits_cct":         summary.get("credits_cct"),
                "token_id":            summary.get("token_id"),
                "emission_record_id":  summary.get("emission_record_id"),
            })
            print(f"        done in {elapsed}s  | decision={summary.get('governance_decision')} "
                  f"| confidence={summary.get('confidence')} | NFT#={summary.get('token_id')}")
        except Exception as exc:
            elapsed = round(time.time() - t0, 2)
            print(f"        ✗ pipeline error after {elapsed}s: {exc}")
            traceback.print_exc()
            manifest.append({"csv": str(rel), "vehicle_id": vehicle_id, **meta,
                             "status": "error", "stage": "pipeline",
                             "elapsed_s": elapsed, "error": str(exc)})

    manifest_path = out_dir / "_manifest.json"
    manifest_path.write_text(
        json.dumps({
            "run_id":     run_id,
            "data_dir":   str(data_dir),
            "mode":       "api" if args.api else "local",
            "api_url":    args.api_url if args.api else None,
            "db_path":    None if args.api else str(config.DB_PATH),
            "dry_run":    args.dry_run,
            "recipient":  args.recipient,
            "engine_cc":  args.engine_cc,
            "total":      len(manifest),
            "ok":         sum(1 for m in manifest if m["status"] == "ok"),
            "errors":     sum(1 for m in manifest if m["status"] == "error"),
            "sessions":   manifest,
        }, indent=2, ensure_ascii=False, default=str),
        encoding="utf-8",
    )

    ok = sum(1 for m in manifest if m["status"] == "ok")
    err = len(manifest) - ok
    print(f"\nDone. {ok} ok, {err} error(s).")
    print(f"Manifest → {manifest_path}")
    return 0 if err == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
