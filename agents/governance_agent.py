"""
agents/governance_agent.py — Phase 3: Deterministic credit decision + LLM rationale.
"""
from __future__ import annotations

import json

from langchain_core.prompts import ChatPromptTemplate

from agents.base import get_llm, extract_json
import config

# ── Deterministic credit rule engine ─────────────────────────────────────────

def _credit_decision(
    co2_mg: float,
    approved: bool,
    baseline_mg: float,
    co2_per_credit_gram: float,
) -> tuple[bool, float, int, float]:
    """
    Pure deterministic function — the sole source of credit decisions.

    Args:
        co2_mg:               Actual session CO2 in milligrams.
        approved:             Whether the ValidatorAgent approved the record.
        baseline_mg:          Configured baseline CO2 per session (mg).
        co2_per_credit_gram:  Grams of CO2 saved = 1 CCT token.

    Returns:
        (issue_credits, credits_cct, credits_wei, saved_co2_mg)

    This function has no side effects and is trivially unit-testable.
    """
    if not approved:
        return False, 0.0, 0, 0.0

    saved_mg = baseline_mg - co2_mg
    if saved_mg <= 0:
        return False, 0.0, 0, round(saved_mg, 2)

    # Convert: saved mg → saved grams → CCT tokens → wei (18 decimals)
    saved_grams  = saved_mg / 1000.0
    credits_cct  = saved_grams / co2_per_credit_gram
    credits_wei  = int(credits_cct * 1e18)

    return True, round(credits_cct, 8), credits_wei, round(saved_mg, 2)


# ── LLM prompt (rationale writing only) ──────────────────────────────────────

_SYSTEM = """\
You are the Governance Agent in a Carbon Footprint Multi-Agent System.

IMPORTANT: The credit issuance DECISION has already been made by a deterministic
rule engine. You cannot change it. Your ONLY job is to write the governance
rationale — a 2-3 sentence explanation that will be stored permanently on the
blockchain so that auditors and regulators can understand this decision.

Write clearly and precisely. Mention the actual CO2 value, the baseline, and
the credits issued (or why they were not issued). Do not use vague language.

Always respond with ONLY a valid JSON object — no prose, no markdown.

Schema:
{{
  "agent":     "GovernanceAgent",
  "rationale": "<2-3 sentence on-chain governance explanation>"
}}
"""

_HUMAN = """\
Deterministic decision (FINAL — do not change):
  issue_credits   : {issue_credits}
  credits_cct     : {credits_cct}
  saved_co2_g     : {saved_co2_g} g
  actual_co2_g    : {actual_co2_g} g
  baseline_g      : {baseline_g} g  ({baseline_g_per_km} g/km × {distance_km} km — Programa MOVER reference)
  validator_status: {validator_status}
  anomaly_type    : {anomaly_type}
  recipient       : {recipient}
  policy          : 1 CCT = {co2_per_credit_g} g CO2 saved vs {baseline_g} g baseline

Write the on-chain governance rationale now.
"""

_PROMPT = ChatPromptTemplate.from_messages([("system", _SYSTEM), ("human", _HUMAN)])


class GovernanceAgent:
    """
    Governance Agent — Phase 3.
    Decision: pure Python rule engine (deterministic, reproducible).
    Output:   LLM-written on-chain rationale text.
    """

    def __init__(self):
        self._llm   = get_llm(temperature=0.1)   # slight variance for natural rationale text
        self._chain = _PROMPT | self._llm

    def run(
        self,
        sensor_report: dict,
        validator_report: dict,
        recipient_address: str,
    ) -> dict:
        co2_mg      = float(sensor_report.get("total_co2_mg", 0))
        distance_km = float(sensor_report.get("distance_km", 0.0))
        approved    = bool(validator_report.get("approved", False))

        # ── Dynamic baseline: 175 g/km × distance (Programa MOVER reference) ─
        if distance_km > 0:
            baseline_mg = distance_km * config.BASELINE_CO2_G_PER_KM * 1000
        else:
            baseline_mg = config.BASELINE_CO2_MG  # fallback for sessions without speed data

        # ── Deterministic decision (never touches LLM) ────────────────────────
        issue, credits_cct, credits_wei, saved_mg = _credit_decision(
            co2_mg=co2_mg,
            approved=approved,
            baseline_mg=baseline_mg,
            co2_per_credit_gram=config.CO2_PER_CREDIT_GRAM,
        )

        # ── LLM writes the rationale ──────────────────────────────────────────
        response = self._chain.invoke({
            "issue_credits":    issue,
            "credits_cct":      credits_cct,
            "saved_co2_g":      round(saved_mg / 1000, 2),
            "actual_co2_g":     round(co2_mg / 1000, 2),
            "baseline_g":       round(baseline_mg / 1000, 2),
            "baseline_g_per_km": config.BASELINE_CO2_G_PER_KM,
            "distance_km":      round(distance_km, 2),
            "validator_status": "approved" if approved else "rejected",
            "anomaly_type":     validator_report.get("anomaly_type", "none"),
            "recipient":        recipient_address,
            "co2_per_credit_g": config.CO2_PER_CREDIT_GRAM,
        })

        try:
            llm_result = extract_json(response.content)
            rationale  = llm_result.get("rationale", "")
        except Exception:
            if issue:
                rationale = (
                    f"Vehicle emitted {co2_mg:.0f} mg CO2, saving {saved_mg:.0f} mg "
                    f"vs the {config.BASELINE_CO2_MG:.0f} mg baseline. "
                    f"Issuing {credits_cct:.6f} CCT tokens per DAO policy v2.0."
                )
            else:
                reason = "validator rejection" if not approved else "no CO2 saving vs baseline"
                rationale = (
                    f"Credit issuance denied due to {reason}. "
                    f"Actual CO2: {co2_mg:.0f} mg, baseline: {config.BASELINE_CO2_MG:.0f} mg."
                )

        return {
            "agent":               "GovernanceAgent",
            "vehicle_id":          sensor_report.get("vehicle_id", "unknown"),
            "issue_credits":       issue,
            "credits_cct":         credits_cct,
            "credits_wei":         credits_wei,
            "saved_co2_mg":        saved_mg,
            "recipient":           recipient_address,
            "rationale":           rationale,
            "governance_decision": "approved" if issue else "denied",
            "policy_reference":    "Carbon MAS DAO Policy v2.0",
        }
