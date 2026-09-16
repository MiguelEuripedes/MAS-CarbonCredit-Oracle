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


# ── Deterministic facts, template and faithfulness guard ─────────────────────

ANOMALY_DESCRIPTIONS = {
    "impossible_speed":    "a physically impossible vehicle speed",
    "engine_off_motion":   "vehicle motion recorded while the engine rotation was zero",
    "temperature_hack":    "an intake-air temperature outside the physically plausible range",
    "robotic_data":        "near-constant engine rotation typical of machine-generated telemetry",
    "statistical_outlier": "a CO2 total that deviates from the vehicle's own history beyond the z-score threshold",
    "too_low":             "a CO2 total below the plausible minimum",
    "too_high":            "a CO2 total above the plausible maximum",
}


def _fmt_g(value: float) -> str:
    """Grams with two decimals, or four when the value is below 0.01 g."""
    return f"{value:.2f}" if abs(value) >= 0.01 else f"{value:.4f}"


def _describe_anomaly(anomaly_type: str) -> str:
    if not anomaly_type or anomaly_type == "none":
        return "a validation failure without a specific anomaly code"
    return ANOMALY_DESCRIPTIONS.get(anomaly_type, f"the anomaly '{anomaly_type}'")


def _describe_baseline(baseline_g: float, distance_km: float) -> str:
    if distance_km > 0:
        return (f"{_fmt_g(baseline_g)} g ({config.BASELINE_CO2_G_PER_KM} g/km x "
                f"{distance_km:.2f} km, Programa MOVER reference)")
    return f"{_fmt_g(baseline_g)} g (fixed fallback baseline, no speed data in the session)"


def _decision_facts(issue, approved, anomaly_type, actual_g, baseline_g, saved_g,
                    credits_cct, distance_km, recipient) -> str:
    """Deterministic statement of the outcome. The LLM may only rephrase these facts."""
    if not approved:
        # No baseline and no emissions figure: the comparison was never evaluated.
        return (
            "Outcome: no carbon credits were issued.\n"
            "Reason: the session was rejected at the validation stage, which detected "
            f"{_describe_anomaly(anomaly_type)}.\n"
            "Rejected sessions are not eligible for credits, so no emissions comparison was made."
        )
    if not issue:
        return (
            "Outcome: no carbon credits were issued.\n"
            f"Reason: the session passed validation, but its actual emissions of {_fmt_g(actual_g)} g "
            f"were not below the baseline of {_describe_baseline(baseline_g, distance_km)}."
        )
    return (
        "Outcome: carbon credits were issued.\n"
        f"Reason: the session passed validation and its actual emissions of {_fmt_g(actual_g)} g "
        f"were below the baseline of {_describe_baseline(baseline_g, distance_km)}.\n"
        f"Avoided emissions: {_fmt_g(saved_g)} g, equivalent to {credits_cct:.6f} CCT "
        f"(1 CCT = {config.CO2_PER_CREDIT_GRAM} g of CO2 avoided), credited to {recipient}."
    )


def _template_rationale(issue, approved, anomaly_type, actual_g, baseline_g, saved_g,
                        credits_cct, distance_km, recipient) -> str:
    """Deterministic rationale used whenever the LLM text is missing or unfaithful."""
    if not approved:
        return (
            "No carbon credits were issued because the session was rejected at validation, "
            f"which detected {_describe_anomaly(anomaly_type)}. "
            "Rejected sessions are not eligible for credits."
        )
    if not issue:
        return (
            f"No carbon credits were issued because the actual emissions of {_fmt_g(actual_g)} g "
            f"were not below the baseline of {_describe_baseline(baseline_g, distance_km)}."
        )
    return (
        f"The session emitted {_fmt_g(actual_g)} g of CO2, below the baseline of "
        f"{_describe_baseline(baseline_g, distance_km)}, avoiding {_fmt_g(saved_g)} g. "
        f"{credits_cct:.6f} CCT were credited to {recipient}."
    )


def _rationale_is_consistent(text: str, issue: bool, approved: bool, saved_g: float) -> bool:
    """
    Deterministic guard against unfaithful rationales.
      - Rejected at validation: the text must not invoke a baseline that was never evaluated.
      - Credits issued: the text must state the avoided-emission value.
    """
    if not text or not text.strip():
        return False
    if not approved:
        return "baseline" not in text.lower()
    if issue:
        variants = {_fmt_g(saved_g), f"{saved_g:.1f}", str(round(saved_g, 2))}
        return any(v in text for v in variants)
    return True


# ── LLM prompt (rationale writing only) ──────────────────────────────────────

_SYSTEM = """\
You are the Governance Agent in a Carbon Footprint Multi-Agent System.

The credit issuance DECISION has already been made by a deterministic rule engine,
and the facts you receive state it exactly. Your ONLY job is to rewrite those facts
as a clear 2-3 sentence governance rationale that will be stored permanently on the
blockchain so that auditors and regulators can understand this decision.

Rules:
- Use only the facts provided. Do not add causes, comparisons or numbers that are not in them.
- Copy every number exactly as it appears in the facts.
- Do not mention any baseline unless the facts mention it.

Always respond with ONLY a valid JSON object — no prose, no markdown.

Schema:
{{
  "agent":     "GovernanceAgent",
  "rationale": "<2-3 sentence on-chain governance explanation>"
}}
"""

_HUMAN = """\
Decision facts (FINAL — do not change them and do not add to them):
{facts}

Write the on-chain governance rationale now.
"""

_PROMPT = ChatPromptTemplate.from_messages([("system", _SYSTEM), ("human", _HUMAN)])


class GovernanceAgent:
    """
    Governance Agent — Phase 3.
    Decision: pure Python rule engine (deterministic, reproducible).
    Output:   LLM-written on-chain rationale text, guarded against contradictions.
    """

    def __init__(self):
        # Temperature 0, as in the other agents: faithfulness comes from the deterministic
        # facts and the guard, and fixed sampling keeps the wording reproducible.
        self._llm   = get_llm(temperature=0.0)
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

        case = {
            "issue":        issue,
            "approved":     approved,
            "anomaly_type": validator_report.get("anomaly_type", "none"),
            "actual_g":     co2_mg / 1000,
            "baseline_g":   baseline_mg / 1000,
            "saved_g":      saved_mg / 1000,
            "credits_cct":  credits_cct,
            "distance_km":  distance_km,
            "recipient":    recipient_address,
        }

        # ── LLM rephrases the deterministic facts ─────────────────────────────
        response = self._chain.invoke({"facts": _decision_facts(**case)})

        try:
            candidate = extract_json(response.content).get("rationale", "")
        except Exception:
            candidate = ""

        if _rationale_is_consistent(candidate, issue, approved, case["saved_g"]):
            rationale, rationale_source = candidate, "llm"
        else:
            rationale, rationale_source = _template_rationale(**case), "template"

        return {
            "agent":               "GovernanceAgent",
            "vehicle_id":          sensor_report.get("vehicle_id", "unknown"),
            "issue_credits":       issue,
            "credits_cct":         credits_cct,
            "credits_wei":         credits_wei,
            "saved_co2_mg":        saved_mg,
            "recipient":           recipient_address,
            "rationale":           rationale,
            "rationale_source":    rationale_source,
            "governance_decision": "approved" if issue else "denied",
            "policy_reference":    "Carbon MAS DAO Policy v2.0",
        }
