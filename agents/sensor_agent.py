"""
agents/sensor_agent.py — Phase 1: CO2 calculation + data quality assessment.
"""
from __future__ import annotations

import json

import pandas as pd
from langchain_core.prompts import ChatPromptTemplate

from agents.base import get_llm, extract_json, build_pipeline_metadata
from tools.co2_tools import calculate_co2_physics, check_physical_plausibility
from tools.csv_tools import validate_dataframe
from session_store import get_vehicle_history

_SYSTEM = """\
You are the Sensor Agent in a Carbon Footprint Multi-Agent System.

Your ONLY role is to interpret data quality. You do NOT calculate CO2.
The CO2 value has already been computed deterministically by a physics engine.
Your job: assess whether the sensor data quality is reliable enough to proceed.

Always respond with ONLY a valid JSON object — no prose, no markdown.

Schema:
{{
  "agent":        "SensorAgent",
  "data_quality": "<good|fair|poor>",
  "quality_notes":"<brief note on completeness or sensor issues>",
  "assessment":   "<1-2 sentence plain-English summary for the audit record>"
}}
"""

_HUMAN = """\
Vehicle: {vehicle_id}  |  Engine: {engine_cc}cc  |  Rows: {rows}

CSV Validation:
{csv_validation}

Computed CO2 (deterministic physics — do not question this number):
  total_co2_g  : {total_co2_g} g
  distance_km  : {distance_km} km
  fuel_type    : {fuel_type}
  maf_estimated: {maf_estimated}

Vehicle history (last {history_count} validated sessions, mg):
{history}

Assess data quality and produce your JSON now.
"""

_PROMPT = ChatPromptTemplate.from_messages([("system", _SYSTEM), ("human", _HUMAN)])


class SensorAgent:
    def __init__(self):
        self._llm   = get_llm(temperature=0.0)
        self._chain = _PROMPT | self._llm

    def run(
        self,
        df: pd.DataFrame,
        vehicle_id: str,
        engine_cc: float,
        sha256_hex: str,
    ) -> dict:
        """
        Args:
            df:          Pre-loaded OBD-II DataFrame (from orchestrator).
            vehicle_id:  Sanitized vehicle identifier.
            engine_cc:   Engine displacement in cm³.
            sha256_hex:  Hex SHA-256 of the raw CSV (computed before load).
        """
        # ── Deterministic physics (no LLM) ────────────────────────────────────
        total_co2, fuel_type, distance_km = calculate_co2_physics(df, engine_cc)
        maf_estimated = (
            "mass_air_flow" not in df.columns
            or df["mass_air_flow"].isna().all()
            or (df["mass_air_flow"] == 0).all()
        )

        # ── Physical plausibility (deterministic, history-independent) ────────
        plausibility = check_physical_plausibility(df)

        # ── CSV validation ────────────────────────────────────────────────────
        validation = validate_dataframe(df)

        # ── Vehicle history (for validator context) ───────────────────────────
        history = get_vehicle_history(vehicle_id, n=10)

        # ── LLM data quality assessment ───────────────────────────────────────
        response = self._chain.invoke({
            "vehicle_id":    vehicle_id,
            "engine_cc":     engine_cc,
            "rows":          len(df),
            "csv_validation": json.dumps(validation, indent=2),
            "total_co2_g":   round(total_co2 / 1000, 2),
            "distance_km":   distance_km,
            "fuel_type":     fuel_type,
            "maf_estimated": maf_estimated,
            "history_count": len(history),
            "history":       str(history) if history else "No previous sessions found.",
        })

        try:
            llm_result = extract_json(response.content)
        except Exception:
            llm_result = {
                "data_quality": "fair",
                "quality_notes": "LLM parse error — using physics values directly.",
                "assessment":   f"Session CO2: {total_co2:.0f} mg ({fuel_type}, {len(df)} rows).",
            }

        # Pipeline metadata fingerprint (stored on-chain)
        pipeline_metadata = build_pipeline_metadata({"sensor": _SYSTEM})

        return {
            "agent":             "SensorAgent",
            "vehicle_id":        vehicle_id,
            "total_co2_mg":      total_co2,
            "fuel_type":         fuel_type,
            "distance_km":       distance_km,
            "rows_processed":    len(df),
            "maf_estimated":     maf_estimated,
            "physical_plausible": plausibility["plausible"],
            "physical_anomaly":   plausibility["anomaly_type"],
            "physical_anomaly_details": plausibility["details"],
            "sha256_hex":        sha256_hex,
            "vehicle_history":   history,
            "data_quality":      llm_result.get("data_quality", "fair"),
            "quality_notes":     llm_result.get("quality_notes", ""),
            "assessment":        llm_result.get("assessment", ""),
            "pipeline_metadata": pipeline_metadata,
        }
