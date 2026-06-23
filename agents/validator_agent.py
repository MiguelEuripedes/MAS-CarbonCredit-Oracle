"""
agents/validator_agent.py — Phase 2: Statistical anomaly detection.s
"""
from __future__ import annotations

import json
import math
from typing import Optional

import numpy as np
from langchain_core.prompts import ChatPromptTemplate

from agents.base import get_llm, extract_json
import config

# ── Statistical rule engine (deterministic) ───────────────────────────────────

ALERT_LOW:  float = 10_000.0       # 10 g minimum — below this is likely a sensor error
ALERT_HIGH: float = 50_000_000.0   # 50 kg maximum — physically impossible to exceed


def _statistical_decision(
    co2_mg: float,
    history: list[float],
    physical_anomaly: str = "none",
) -> tuple[bool, str, int]:
    """
    Pure deterministic function. Returns (approved, anomaly_type, confidence_0_100).

    A physical-plausibility anomaly (from the SensorAgent) is a hard reject that
    short-circuits the statistical test: an impossible speed, engine-off motion,
    a hacked temperature sensor, or robotic data invalidates the session no matter
    what its CO2 total or history looks like. Confidence 0 — there is nothing
    statistical to weigh, the data is physically inconsistent.

    Otherwise uses Z-score when history is sufficient, range check otherwise.
    This function has no side effects and is trivially unit-testable.
    """
    if physical_anomaly and physical_anomaly != "none":
        return False, physical_anomaly, 0

    if len(history) >= config.STAT_VALIDATION_MIN_HISTORY:
        mean = float(np.mean(history))
        std  = float(np.std(history))

        if std < 1.0:
            # Degenerate case: all historical values identical
            # Fall back to range check
            approved = ALERT_LOW < co2_mg < ALERT_HIGH
            anomaly  = "none" if approved else ("too_low" if co2_mg <= ALERT_LOW else "too_high")
            return approved, anomaly, 80

        z = abs(co2_mg - mean) / std
        approved = z <= config.STAT_VALIDATION_Z_THRESHOLD

        if not approved:
            anomaly = "statistical_outlier"
        elif co2_mg <= ALERT_LOW:
            approved = False
            anomaly  = "too_low"
        elif co2_mg >= ALERT_HIGH:
            approved = False
            anomaly  = "too_high"
        else:
            anomaly = "none"

        # Confidence: 100 at z=0, decaying toward 0 at z=threshold*2
        confidence = int(max(0, min(100, 100 * (1 - z / (config.STAT_VALIDATION_Z_THRESHOLD * 2)))))
        return approved, anomaly, confidence

    else:
        # Insufficient history: range check only
        if co2_mg <= ALERT_LOW:
            return False, "too_low", 60
        if co2_mg >= ALERT_HIGH:
            return False, "too_high", 60
        # Pass with moderate confidence (no historical baseline yet)
        return True, "none", 65


# ── LLM prompt (explanation only) ────────────────────────────────────────────

_SYSTEM = """\
You are the Validator Agent in a Carbon Footprint Multi-Agent System.

IMPORTANT: The validation DECISION has already been made by a deterministic
statistical engine. You cannot change it. Your ONLY job is to write a clear,
honest, 2-3 sentence explanation of WHY the statistical engine made this
decision. This explanation will be stored on the blockchain as the audit rationale.

Do not second-guess the decision. Do not suggest a different outcome.
Write as if you are explaining a technical result to a regulator.

Always respond with ONLY a valid JSON object — no prose, no markdown.

Schema:
{{
  "agent":      "ValidatorAgent",
  "reasoning":  "<2-3 sentences explaining the statistical result>",
  "recommendation": "<approve|reject|flag_for_review>"
}}
"""

_HUMAN = """\
Statistical validation result (FINAL — do not change):
  approved        : {approved}
  anomaly_type    : {anomaly_type}
  confidence      : {confidence}/100
  co2_mg          : {co2_mg}
  history_count   : {history_count}
  history_mean_mg : {history_mean}
  history_std_mg  : {history_std}
  z_score         : {z_score}
  method          : {method}

Write the plain-English audit explanation for the on-chain record.
"""

_PROMPT = ChatPromptTemplate.from_messages([("system", _SYSTEM), ("human", _HUMAN)])


class ValidatorAgent:
    """
    Validator Agent — Phase 2.
    Decision: deterministic statistical engine.
    Output:   LLM-written audit explanation.
    """

    def __init__(self):
        self._llm   = get_llm(temperature=0.0)
        self._chain = _PROMPT | self._llm

    def run(self, sensor_report: dict) -> dict:
        co2_mg  = float(sensor_report.get("total_co2_mg", 0))
        history = sensor_report.get("vehicle_history", [])
        physical_anomaly = sensor_report.get("physical_anomaly", "none")

        # ── Deterministic decision ────────────────────────────────────────────
        approved, anomaly_type, confidence = _statistical_decision(
            co2_mg, history, physical_anomaly
        )

        # Compute stats for LLM context
        if history:
            h_mean = float(np.mean(history))
            h_std  = float(np.std(history))
            z_score = round(abs(co2_mg - h_mean) / h_std, 3) if h_std > 0 else 0.0
        else:
            h_mean = h_std = z_score = 0.0

        if physical_anomaly and physical_anomaly != "none":
            method = "physical-plausibility (hard reject: {}) — {}".format(
                physical_anomaly, sensor_report.get("physical_anomaly_details", "")
            )
        elif len(history) >= config.STAT_VALIDATION_MIN_HISTORY:
            method = "z-score (n={})".format(len(history))
        else:
            method = "range-check (insufficient history, n={})".format(len(history))

        # ── LLM writes the explanation ────────────────────────────────────────
        response = self._chain.invoke({
            "approved":      approved,
            "anomaly_type":  anomaly_type,
            "confidence":    confidence,
            "co2_mg":        co2_mg,
            "history_count": len(history),
            "history_mean":  round(h_mean, 1),
            "history_std":   round(h_std, 1),
            "z_score":       z_score,
            "method":        method,
        })

        try:
            llm_result = extract_json(response.content)
            reasoning  = llm_result.get("reasoning", "")
        except Exception:
            reasoning = (
                f"Statistical validation using {method}. "
                f"CO2 value {co2_mg:.0f} mg {'approved' if approved else 'rejected'} "
                f"(anomaly: {anomaly_type}, confidence: {confidence}/100)."
            )

        return {
            "agent":          "ValidatorAgent",
            "vehicle_id":     sensor_report.get("vehicle_id", "unknown"),
            "approved":       approved,
            "anomaly_detected": not approved,
            "anomaly_type":   anomaly_type,
            "confidence":     confidence,
            "method":         method,
            "reasoning":      reasoning,
            "recommendation": "approve" if approved else (
                "flag_for_review" if confidence >= 50 else "reject"
            ),
        }
