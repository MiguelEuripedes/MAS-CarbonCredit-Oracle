"""
orchestrator.py — Master coordinator for Carbon MAS v2.

Pipeline:
  Input (bytes + metadata)
       │
  [Sanitize + SHA-256]         ← before any agent sees data
       │
  [SensorAgent]                ← CO2 physics + LLM quality assessment
       │  moderator check
  [ValidatorAgent]             ← statistical decision + LLM explanation
       │  moderator check
  [GovernanceAgent]            ← deterministic credit formula + LLM rationale
       │  moderator check
  [BlockchainAgent]            ← on-chain writes (3-role signing) + LLM audit
       │
  [session_store.save_session] ← persist for future Z-score validation
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from typing import Optional

from tools.csv_tools import load_csv_bytes
from tools.sanitize import sanitize_vehicle_id
from agents import SensorAgent, ValidatorAgent, GovernanceAgent, BlockchainAgent
from session_store import save_session
import config


class CarbonMASOrchestrator:
    """
    Orchestrates the full 4-agent Carbon MAS v2 pipeline.

    Usage:
        orch   = CarbonMASOrchestrator()
        report = orch.run(
            csv_bytes=...,
            vehicle_id="VIN-001",
            recipient_address="0x...",
            engine_cc=2000,
        )
    """

    def __init__(self):
        self._sensor     = SensorAgent()
        self._validator  = ValidatorAgent()
        self._governance = GovernanceAgent()
        self._blockchain = BlockchainAgent()

    # ── Main entry point ──────────────────────────────────────────────────────

    def run(
        self,
        csv_bytes: bytes,
        vehicle_id: str,
        recipient_address: str,
        engine_cc: Optional[float] = None,
        dry_run: bool = False,
    ) -> dict:
        """
        Execute the full pipeline for one driving session.

        Args:
            csv_bytes:         Raw CSV file bytes (received from API or file).
            vehicle_id:        Vehicle identifier (will be sanitized here).
            recipient_address: Ethereum address for CCT tokens.
            engine_cc:         Engine displacement (default from .env).
            dry_run:           If True, skip blockchain writes (phases 1-3 only).

        Returns:
            Full pipeline report with all agent outputs and final audit.
        """
        engine_cc = engine_cc or config.DEFAULT_ENGINE_CC
        started   = datetime.now(timezone.utc).isoformat()
        phases: dict = {}
        
        timings: dict = {}

        # ── Sanitize inputs (before anything touches the data) ────────────────
        try:
            vehicle_id = sanitize_vehicle_id(vehicle_id)
        except ValueError as exc:
            return self._abort(phases, f"Invalid vehicle_id: {exc}", started)

        # ── Load CSV + compute SHA-256 (single source of truth) ───────────────
        try:
            df, fuel_type, sha256_bytes = load_csv_bytes(csv_bytes)
            sha256_hex = sha256_bytes.hex()
        except Exception as exc:
            return self._abort(phases, f"CSV load failed: {exc}", started)

        # ── Phase 1: Sensor Agent ─────────────────────────────────────────────
        print("\n[1/4] SensorAgent  — CO2 physics + data quality …")
        _t0 = time.perf_counter()
        sensor_out = self._sensor.run(
            df=df,
            vehicle_id=vehicle_id,
            engine_cc=engine_cc,
            sha256_hex=sha256_hex,
        )
        timings["sensor"] = round(time.perf_counter() - _t0, 3)
        phases["sensor"] = sensor_out
        self._print_phase("SensorAgent", sensor_out)

        # Moderator check 1
        if sensor_out.get("status") == "error":
            return self._abort(phases, sensor_out.get("message", "SensorAgent failed"), started)
        if not isinstance(sensor_out.get("total_co2_mg"), (int, float)):
            return self._abort(phases, "SensorAgent returned invalid CO2 value", started)

        # ── Phase 2: Validator Agent ──────────────────────────────────────────
        print("\n[2/4] ValidatorAgent — statistical anomaly detection …")
        _t0 = time.perf_counter()
        validator_out = self._validator.run(sensor_report=sensor_out)
        timings["validator"] = round(time.perf_counter() - _t0, 3)
        phases["validator"] = validator_out
        self._print_phase("ValidatorAgent", validator_out)

        # Moderator check 2
        if "approved" not in validator_out:
            validator_out["approved"]        = False
            validator_out["confidence"]      = 0
            validator_out["_moderator_fix"]  = "Missing 'approved' — defaulted False"

        # ── Phase 3: Governance Agent ─────────────────────────────────────────
        print("\n[3/4] GovernanceAgent — deterministic credit decision …")
        _t0 = time.perf_counter()
        governance_out = self._governance.run(
            sensor_report=sensor_out,
            validator_report=validator_out,
            recipient_address=recipient_address,
        )
        timings["governance"] = round(time.perf_counter() - _t0, 3)
        phases["governance"] = governance_out
        self._print_phase("GovernanceAgent", governance_out)

        # Moderator check 3
        if not isinstance(governance_out.get("credits_wei"), int):
            governance_out["credits_wei"]   = 0
            governance_out["issue_credits"] = False
            governance_out["_moderator_fix"] = "Invalid credits_wei — zeroed"

        # ── Dry-run exit point ────────────────────────────────────────────────
        if dry_run:
            print("\n  [DRY RUN] Skipping blockchain writes.")
            # Still persist session so Z-score validation accumulates history
            save_session(
                vehicle_id=vehicle_id,
                co2_mg=float(sensor_out["total_co2_mg"]),
                validated=bool(validator_out.get("approved", False)),
                record_id=None,
            )
            return self._build_report(
                phases, "dry_run", started, vehicle_id, engine_cc, recipient_address,
                sha256_hex, timings
            )

        # ── Phase 4: Blockchain Agent ─────────────────────────────────────────
        print("\n[4/4] BlockchainAgent — writing to Hyperledger Besu …")
        _t0 = time.perf_counter()
        blockchain_out = self._blockchain.run(
            sensor_report=sensor_out,
            validator_report=validator_out,
            governance_report=governance_out,
        )
        timings["blockchain"] = round(time.perf_counter() - _t0, 3)
        phases["blockchain"] = blockchain_out
        self._print_phase("BlockchainAgent", blockchain_out)

        # ── Persist session for future Z-score validation ─────────────────────
        record_id = (
            blockchain_out.get("on_chain_data", {}).get("emission_record_id")
        )
        save_session(
            vehicle_id=vehicle_id,
            co2_mg=float(sensor_out["total_co2_mg"]),
            validated=bool(validator_out.get("approved", False)),
            record_id=record_id,
        )

        status = blockchain_out.get("pipeline_status", "success")
        return self._build_report(
            phases, status, started, vehicle_id, engine_cc, recipient_address,
            sha256_hex, timings
        )

    # ── Helpers ───────────────────────────────────────────────────────────────

    @staticmethod
    def _build_report(phases, status, started, vehicle_id, engine_cc, recipient,
                      sha256_hex, timings=None):
        sensor     = phases.get("sensor", {})
        validator  = phases.get("validator", {})
        governance = phases.get("governance", {})
        blockchain = phases.get("blockchain", {})
        on_chain   = blockchain.get("on_chain_data", {})
        timings    = timings or {}

        return {
            "pipeline_status": status,
            "pipeline_start":  started,
            "pipeline_end":    datetime.now(timezone.utc).isoformat(),
            "vehicle_id":      vehicle_id,
            "engine_cc":       engine_cc,
            "recipient":       recipient,
            "csv_sha256":      sha256_hex,
            "phases":          phases,
            "phase_latencies_s": {
                "sensor":     timings.get("sensor"),
                "validator":  timings.get("validator"),
                "governance": timings.get("governance"),
                "blockchain": timings.get("blockchain"),
                "total":      round(sum(v for v in timings.values() if v), 3),
            },
            "summary": {
                "total_co2_mg":        sensor.get("total_co2_mg"),
                "fuel_type":           sensor.get("fuel_type"),
                "distance_km":         sensor.get("distance_km"),
                "data_quality":        sensor.get("data_quality"),
                "maf_estimated":       sensor.get("maf_estimated"),
                "validated":           validator.get("approved"),
                "confidence":          validator.get("confidence"),
                "anomaly":             validator.get("anomaly_type"),
                "validation_method":   validator.get("method"),
                "credits_cct":         governance.get("credits_cct", 0.0),
                "co2_saved_mg":        governance.get("saved_co2_mg", 0.0),
                "governance_decision": governance.get("governance_decision"),
                "emission_record_id":  on_chain.get("emission_record_id"),
                "token_id":            on_chain.get("token_id"),
                "emission_tx":         on_chain.get("emission_tx"),
                "governance_tx":       on_chain.get("governance_tx"),
                "certificate_tx":      on_chain.get("certificate_tx"),
                "data_hash":           sha256_hex,
            },
        }

    @staticmethod
    def _print_phase(name: str, data: dict) -> None:
        keys = {"total_co2_mg","data_quality","approved","confidence","anomaly_type",
                "governance_decision","credits_cct","pipeline_status","assessment",
                "reasoning","rationale","audit_summary"}
        brief = {k: v for k, v in data.items() if k in keys}
        print(f"    ✓ {name}: {json.dumps(brief, ensure_ascii=False)[:220]}")

    @staticmethod
    def _abort(phases: dict, reason: str, started: str) -> dict:
        print(f"\n    ✗ Pipeline aborted: {reason}")
        return {
            "pipeline_status": "aborted",
            "pipeline_start":  started,
            "abort_reason":    reason,
            "phases":          phases,
        }
