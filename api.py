"""
api.py v2
──────────
FastAPI production server for Carbon MAS v2.

Endpoints:
  POST /process              Upload OBD-II CSV → returns job_id immediately
  POST /process-envelope     Lab JSON envelope (CSV + SHA-256 + user address)
  GET  /status/{job_id}      Poll pipeline job status
  GET  /result/{job_id}      Full pipeline report once done
  GET  /record/{record_id}   Query emission record from blockchain
  POST /verify               Verify a token's integrity (CSV vs on-chain dataHash)
  GET  /portfolio/{address}  All CCT holdings + issuance history + EUR value
  GET  /price/cct-eur        Live CCT price in EUR (CoinGecko, with fallback)
  GET  /jobs                 List recent jobs
  GET  /health               System health check

Run:
    uvicorn api:app --host 0.0.0.0 --port 8000 --workers 1
"""

from __future__ import annotations

import base64
import json
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Optional

import httpx
from fastapi import FastAPI, File, Form, UploadFile, HTTPException, BackgroundTasks, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from slowapi import Limiter
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded

import config
import session_store as store
from tools.sanitize import sanitize_vehicle_id, verify_lab_envelope


# ── Lifespan (replaces deprecated on_event) ───────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    store.init_db()
    yield


# ── App setup ─────────────────────────────────────────────────────────────────

limiter = Limiter(key_func=get_remote_address)

app = FastAPI(
    title="Carbon MAS API v2",
    description="Agentic Oracle for Vehicular Carbon Microcredit Tokenization",
    version="2.0.0",
    lifespan=lifespan,
)
app.state.limiter = limiter

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(RateLimitExceeded)
async def rate_limit_handler(request: Request, exc: RateLimitExceeded):
    return JSONResponse(
        status_code=429,
        content={"detail": "Rate limit exceeded. Try again later."},
    )


# ── Rate limiting helpers ─────────────────────────────────────────────────────

def _check_vehicle_rate(vehicle_id: str) -> None:
    """Raise HTTP 429 if the vehicle exceeds configured submission limits."""
    count = store.count_vehicle_jobs_last_hour(vehicle_id)
    if count >= config.RATE_LIMIT_PER_VEHICLE_PER_HOUR:
        raise HTTPException(
            status_code=429,
            detail=(
                f"Vehicle '{vehicle_id}' has submitted {count} sessions in the last hour. "
                f"Maximum is {config.RATE_LIMIT_PER_VEHICLE_PER_HOUR}."
            ),
        )
    last_ts = store.get_last_session_time(vehicle_id)
    if last_ts:
        last_dt = datetime.fromisoformat(last_ts).astimezone(timezone.utc)
        elapsed = (datetime.now(timezone.utc) - last_dt).total_seconds()
        if elapsed < config.MIN_SESSION_GAP_SECONDS:
            wait = int(config.MIN_SESSION_GAP_SECONDS - elapsed)
            raise HTTPException(
                status_code=429,
                detail=(
                    f"Sessions for '{vehicle_id}' must be at least "
                    f"{config.MIN_SESSION_GAP_SECONDS}s apart. Wait {wait}s."
                ),
            )


def _validate_address(address: str) -> None:
    if not address.startswith("0x") or len(address) != 42:
        raise HTTPException(status_code=422, detail=f"Invalid Ethereum address: {address}")


def _make_job(vehicle_id: str, recipient: str) -> str:
    job_id = str(uuid.uuid4())
    store.create_job(job_id, vehicle_id, recipient)
    return job_id


# ── Background task ───────────────────────────────────────────────────────────

async def _run_pipeline(
    job_id: str,
    csv_bytes: bytes,
    vehicle_id: str,
    recipient: str,
    engine_cc: float,
    dry_run: bool = False,
) -> None:
    store.update_job_status(job_id, "running")
    try:
        from orchestrator import CarbonMASOrchestrator
        import asyncio
        orch   = CarbonMASOrchestrator()
        loop   = asyncio.get_event_loop()
        result = await loop.run_in_executor(
            None,
            lambda: orch.run(
                csv_bytes=csv_bytes,
                vehicle_id=vehicle_id,
                recipient_address=recipient,
                engine_cc=engine_cc,
                dry_run=dry_run,
            ),
        )
        store.update_job_status(job_id, "done", result=result)
    except Exception as exc:
        store.update_job_status(job_id, "failed", error=str(exc))


# ── Endpoints ─────────────────────────────────────────────────────────────────

@app.post("/process", summary="Upload OBD-II CSV and start pipeline")
@limiter.limit("20/minute")
async def process_csv(
    request: Request,
    background_tasks: BackgroundTasks,
    file:       UploadFile = File(..., description="OBD-II CSV file"),
    vehicle_id: str        = Form(..., description="Vehicle identifier"),
    recipient:  str        = Form(..., description="Ethereum address for CCT credits"),
    engine_cc:  float      = Form(default=None, description="Engine displacement cm³"),
    dry_run:    bool       = Form(default=False, description="Skip blockchain writes"),
):
    """
    Upload a CSV and start the 4-agent pipeline asynchronously.
    Returns job_id immediately. Poll GET /status/{job_id} for progress.
    """
    if not file.filename or not file.filename.endswith(".csv"):
        raise HTTPException(status_code=400, detail="Only .csv files are accepted")
    _validate_address(recipient)
    try:
        vehicle_id = sanitize_vehicle_id(vehicle_id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    _check_vehicle_rate(vehicle_id)
    csv_bytes = await file.read()
    job_id    = _make_job(vehicle_id, recipient)

    background_tasks.add_task(
        _run_pipeline, job_id, csv_bytes, vehicle_id, recipient,
        engine_cc or config.DEFAULT_ENGINE_CC, dry_run,
    )
    return {"job_id": job_id, "status": "pending", "poll_url": f"/status/{job_id}"}


@app.post("/process-envelope", summary="Lab JSON envelope with embedded CSV")
@limiter.limit("20/minute")
async def process_envelope(
    request: Request,
    background_tasks: BackgroundTasks,
):
    """
    Accepts a lab JSON body (not multipart) containing:

        {
            "csv_b64":      "<base64-encoded CSV>",
            "sha256":       "<hex SHA-256 of the decoded CSV bytes>",
            "user_address": "0x...",
            "vehicle_id":   "VIN-001",
            "engine_cc":    2000,
            "dry_run":      false
        }

    The server verifies the SHA-256 matches the CSV before starting the pipeline.
    The user_address from the envelope becomes the CCT credit recipient.
    """
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=422, detail="Request body must be valid JSON")

    vehicle_id = body.get("vehicle_id", "")
    engine_cc  = float(body.get("engine_cc", config.DEFAULT_ENGINE_CC))
    dry_run    = bool(body.get("dry_run", False))

    try:
        vehicle_id = sanitize_vehicle_id(vehicle_id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    csv_b64 = body.get("csv_b64", "")
    if not csv_b64:
        raise HTTPException(status_code=422, detail="Missing 'csv_b64' field")

    try:
        csv_bytes = base64.b64decode(csv_b64)
    except Exception:
        raise HTTPException(status_code=422, detail="Invalid base64 in 'csv_b64'")

    try:
        user_address, _ = verify_lab_envelope(body, csv_bytes)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    _check_vehicle_rate(vehicle_id)
    job_id = _make_job(vehicle_id, user_address)

    background_tasks.add_task(
        _run_pipeline, job_id, csv_bytes, vehicle_id, user_address, engine_cc, dry_run,
    )
    return {
        "job_id":       job_id,
        "status":       "pending",
        "user_address": user_address,
        "integrity":    "sha256_verified",
        "poll_url":     f"/status/{job_id}",
    }


@app.get("/status/{job_id}", summary="Poll pipeline job status")
async def get_status(job_id: str):
    """
    Status values: pending | running | done | failed | dry_run
    Once 'done', a 'summary' field is included with key metrics.
    """
    job = store.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")
    response = {k: job[k] for k in
                ["job_id", "status", "vehicle_id", "created_at", "started_at", "ended_at"]}
    if job["status"] == "done" and job.get("result"):
        response["summary"] = job["result"].get("summary", {})
    if job["status"] == "failed":
        response["error"] = job.get("error")
    return response


@app.get("/result/{job_id}", summary="Get full pipeline report")
async def get_result(job_id: str):
    """Returns the complete pipeline report including all agent outputs and tx hashes."""
    job = store.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")
    if job["status"] != "done":
        raise HTTPException(status_code=202, detail=f"Job is '{job['status']}'")
    return job["result"]


@app.get("/record/{record_id}", summary="Query emission record from blockchain")
async def get_emission_record(record_id: int):
    """Fetch an emission record directly from EmissionsRegistry on Besu."""
    from tools.blockchain_tools import get_emission_record as _fetch
    result = json.loads(_fetch.invoke({"record_id": record_id}))
    if result.get("status") == "error":
        raise HTTPException(status_code=500, detail=result.get("message"))
    return result


@app.post("/verify", summary="Verify the integrity & provenance of a credit token")
@limiter.limit("60/minute")
async def verify_token(
    request: Request,
    file:       UploadFile = File(..., description="Original OBD-II CSV to verify"),
    token_id:   Optional[int] = Form(default=None, description="CarbonCertificate token ID"),
    record_id:  Optional[int] = Form(default=None, description="EmissionsRegistry record ID"),
):
    """
    Auditability endpoint for an organization. Given the original CSV and a token
    (or emission record) id, this:
      1. recomputes the SHA-256 of the submitted CSV bytes;
      2. reads the on-chain EmissionRecord (its immutable dataHash);
      3. compares the two -> proves the token is anchored to that exact data;
      4. returns the full provenance chain (certificate + record).

    `integrity_verified == true` means the CSV is byte-identical to what was
    tokenized — nothing was altered.
    """
    import hashlib
    from tools.blockchain_tools import (
        get_certificate as _cert,
        get_emission_record as _rec,
    )

    if token_id is None and record_id is None:
        raise HTTPException(status_code=422, detail="Provide token_id or record_id")
    if not file.filename or not file.filename.endswith(".csv"):
        raise HTTPException(status_code=400, detail="Only .csv files are accepted")

    csv_bytes = await file.read()
    recomputed = hashlib.sha256(csv_bytes).hexdigest()

    certificate = None
    if token_id is not None:
        certificate = json.loads(_cert.invoke({"token_id": token_id}))
        if certificate.get("status") == "error":
            raise HTTPException(status_code=404, detail=certificate.get("message"))
        record_id = certificate["emission_record_id"]

    record = json.loads(_rec.invoke({"record_id": record_id}))
    if record.get("status") == "error":
        raise HTTPException(status_code=404, detail=record.get("message"))

    on_chain_hash = record["data_hash"].replace("0x", "")
    verified = recomputed == on_chain_hash

    return {
        "integrity_verified": verified,
        "token_id":           token_id,
        "emission_record_id": record_id,
        "recomputed_sha256":  recomputed,
        "on_chain_data_hash": on_chain_hash,
        "vehicle_id":         record.get("vehicle_id"),
        "governance_status":  record.get("governance_status"),
        "agent_confidence":   record.get("agent_confidence"),
        "agent_decision":     record.get("agent_decision"),
        "pipeline_metadata":  record.get("pipeline_metadata"),
        "certificate":        certificate,
        "record":             record,
    }


@app.get("/portfolio/{address}", summary="CCT portfolio for a wallet")
async def get_portfolio(address: str):
    """
    Returns the full carbon credit portfolio for a wallet address:
      - Current CCT balance (tokens + wei)
      - Live EUR valuation (CoinGecko with fallback)
      - Complete issuance history with linked emission record IDs
    """
    _validate_address(address)

    from tools.blockchain_tools import get_credit_balance, get_issuance_history

    balance_raw = json.loads(get_credit_balance.invoke({"address": address}))
    history_raw = json.loads(get_issuance_history.invoke({"address": address, "limit": 100}))

    if balance_raw.get("status") == "error":
        raise HTTPException(status_code=500, detail=balance_raw.get("message"))

    nft_count    = int(balance_raw.get("nft_count", 0))
    token_ids    = balance_raw.get("token_ids", [])
    certificates = history_raw.get("certificates", [])
    total_credits = float(history_raw.get("total_credits_cct", 0.0))
    total_co2_saved = int(history_raw.get("total_co2_saved_mg", 0))
    eur_rate = config.CCT_EUR_RATE
    brl_rate = config.EUR_BRL_RATE
    eur_value = round(total_credits * eur_rate, 4)

    return {
        "address":            address,
        "certificate_count":  nft_count,
        "token_ids":          token_ids,
        "total_credits_cct":  total_credits,
        "total_co2_saved_mg": total_co2_saved,
        "total_co2_saved_g":  round(total_co2_saved / 1000, 2),
        "eur_value":          eur_value,
        "brl_value":          round(eur_value * brl_rate, 4),
        "eur_rate":           eur_rate,
        "eur_brl_rate":       brl_rate,
        "rate_source":        "config (.env fixed snapshot)",
        "certificates":       certificates,
    }


@app.get("/price/cct-eur", summary="CCT price in EUR and BRL")
async def get_cct_price():
    """
    Fixed CCT price from .env (manual snapshot of the European carbon exchange).
    1 CCT = 1 kg CO2 saved. BRL value derived via the configured EUR→BRL rate.
    """
    return {
        "cct_eur_rate":  config.CCT_EUR_RATE,
        "cct_brl_rate":  round(config.CCT_EUR_RATE * config.EUR_BRL_RATE, 8),
        "eur_brl_rate":  config.EUR_BRL_RATE,
        "source":        "config (.env fixed snapshot)",
        "note":          "1 CCT ≈ 1 kg CO2 saved. EUR price snapshotted from the European carbon exchange; BRL via fixed EUR/BRL rate.",
    }


@app.get("/jobs", summary="List recent pipeline jobs")
async def list_jobs(limit: int = 20):
    return store.list_jobs(limit=limit)


@app.get("/health", summary="System health check")
async def health_check():
    health: dict = {"status": "ok", "components": {}}

    # Besu
    try:
        from tools.blockchain_tools import check_besu_connection
        besu = json.loads(check_besu_connection.invoke({}))
        health["components"]["besu"] = {
            "connected":    besu.get("connected"),
            "chain_id":     besu.get("chain_id"),
            "latest_block": besu.get("latest_block"),
        }
        if not besu.get("connected"):
            health["status"] = "degraded"
    except Exception as exc:
        health["components"]["besu"] = {"connected": False, "error": str(exc)}
        health["status"] = "degraded"

    # Contracts
    deployed = bool(config.EMISSIONS_REGISTRY_ADDRESS and config.CARBON_CREDIT_ADDRESS)
    health["components"]["contracts"] = {
        "deployed":           deployed,
        "emissions_registry": config.EMISSIONS_REGISTRY_ADDRESS or "NOT SET",
        "carbon_credit":      config.CARBON_CREDIT_ADDRESS or "NOT SET",
    }
    if not deployed:
        health["status"] = "degraded"

    # Ollama
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            r = await client.get(f"{config.OLLAMA_BASE_URL}/api/tags")
        models   = [m["name"] for m in r.json().get("models", [])]
        model_ok = any(config.OLLAMA_MODEL in m for m in models)
        health["components"]["ollama"] = {
            "reachable":       True,
            "configured_model": config.OLLAMA_MODEL,
            "model_present":   model_ok,
            "available_models": models,
        }
        if not model_ok:
            health["status"] = "degraded"
    except Exception as exc:
        health["components"]["ollama"] = {"reachable": False, "error": str(exc)}
        health["status"] = "degraded"

    # Job queue
    jobs = store.list_jobs(limit=500)
    health["components"]["job_queue"] = {
        "backend": "SQLite (persistent)",
        "total":   len(jobs),
        "running": sum(1 for j in jobs if j["status"] == "running"),
        "pending": sum(1 for j in jobs if j["status"] == "pending"),
        "failed":  sum(1 for j in jobs if j["status"] == "failed"),
    }

    return health


