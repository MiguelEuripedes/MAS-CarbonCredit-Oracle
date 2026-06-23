"""
agents/blockchain_agent.py — Phase 4: On-chain execution + audit summary.

Transaction signing:
  logEmission + validateEmission          → Account 2
  updateGovernanceStatus + mintCertificate → Account 3
"""
from __future__ import annotations

import json

from langchain_core.prompts import ChatPromptTemplate

from agents.base import get_llm, extract_json
from tools.blockchain_tools import (
    check_besu_connection,
    log_emission_to_blockchain,
    validate_emission_on_blockchain,
    update_governance_status_on_blockchain,
    issue_carbon_credits,
)

_SYSTEM = """\
You are the Blockchain Agent in a Carbon Footprint Multi-Agent System.

All transactions have already been executed. Your ONLY job is to write a
concise, accurate, 3-4 sentence audit summary of what happened in this
pipeline session. This summary will be shown to users and auditors.

Mention: CO2 emitted, validation outcome, and whether a carbon certificate
NFT was issued (include the token ID if so). Be factual and precise.

Always respond with ONLY a valid JSON object — no prose, no markdown.

Schema:
{{
  "agent":           "BlockchainAgent",
  "audit_summary":   "<3-4 sentence factual audit trail>",
  "pipeline_status": "<success|partial|failed>"
}}
"""

_HUMAN = """\
Pipeline results:
{pipeline_summary}

On-chain transactions:
{tx_results}

Write the audit summary now.
"""

_PROMPT = ChatPromptTemplate.from_messages([("system", _SYSTEM), ("human", _HUMAN)])


class BlockchainAgent:
    def __init__(self):
        self._llm   = get_llm(temperature=0.0)
        self._chain = _PROMPT | self._llm

    def run(
        self,
        sensor_report: dict,
        validator_report: dict,
        governance_report: dict,
    ) -> dict:
        tx_results: dict = {
            "emission_log":      None,
            "validation":        None,
            "governance_status": None,
            "certificate_mint":  None,
        }

        # ── Step 0: verify Besu ───────────────────────────────────────────────
        conn = json.loads(check_besu_connection.invoke({}))
        if not conn.get("connected", False):
            return {
                "agent":           "BlockchainAgent",
                "pipeline_status": "failed",
                "error":           f"Besu unreachable: {conn.get('error', 'unknown')}",
                "vehicle_id":      sensor_report.get("vehicle_id", "unknown"),
            }

        approved       = validator_report.get("approved", False)
        confidence     = int(validator_report.get("confidence", 65))
        sha256_hex     = sensor_report.get("sha256_hex", "0" * 64)
        pipeline_meta  = sensor_report.get("pipeline_metadata", "")

        # Justificativa composta dos 3 agentes, gravada on-chain (string, sem limite
        # no contrato). Limites generosos por agente para preservar a explicação.
        agent_decision = (
            f"Sensor: {sensor_report.get('assessment', '')[:400]} | "
            f"Validator: {validator_report.get('reasoning', '')[:400]} | "
            f"Governance: {governance_report.get('rationale', '')[:400]}"
        )

        # ── Step 1: ALWAYS log emission (Account 2) ───────────────────────────
        log_raw    = log_emission_to_blockchain.invoke({
            "vehicle_id":        sensor_report["vehicle_id"],
            "co2_milligrams":    int(sensor_report["total_co2_mg"]),
            "fuel_type":         sensor_report["fuel_type"],
            "data_hash_hex":     sha256_hex,
            "agent_confidence":  confidence,
            "agent_decision":    agent_decision,
            "pipeline_metadata": pipeline_meta,
        })
        log_result = json.loads(log_raw)
        tx_results["emission_log"] = log_result
        record_id = log_result.get("record_id", -1)

        # ── Step 2: validate emission (Account 2) ─────────────────────────────
        if record_id >= 0:
            val_raw = validate_emission_on_blockchain.invoke({
                "record_id": record_id,
                "approved":  approved,
            })
            tx_results["validation"] = json.loads(val_raw)

        # ── Step 3: update governance status (Account 3) ─────────────────────
        gov_approved = governance_report.get("issue_credits", False)
        if record_id >= 0:
            gov_raw = update_governance_status_on_blockchain.invoke({
                "record_id": record_id,
                "approved":  gov_approved,
            })
            tx_results["governance_status"] = json.loads(gov_raw)

        # ── Step 4: mint ERC-721 certificate (Account 3, only if approved) ────
        token_id  = None
        saved_mg  = int(governance_report.get("saved_co2_mg", 0))
        credits_wei = int(governance_report.get("credits_wei", 0))
        recipient   = governance_report.get("recipient", "")

        if gov_approved and saved_mg > 0 and recipient and record_id >= 0:
            cert_raw = issue_carbon_credits.invoke({
                "recipient_address":      recipient,
                "vehicle_id":             sensor_report["vehicle_id"],
                "emission_record_id":     record_id,
                "co2_saved_mg":           saved_mg,
                "credits_equivalent_wei": credits_wei,
                "reason":                 governance_report.get("rationale", "")[:1000],
            })
            cert_result = json.loads(cert_raw)
            tx_results["certificate_mint"] = cert_result
            token_id = cert_result.get("token_id")

        # ── Step 5: LLM writes audit summary ─────────────────────────────────
        pipeline_summary = {
            "vehicle_id":    sensor_report.get("vehicle_id"),
            "co2_mg":        sensor_report.get("total_co2_mg"),
            "fuel_type":     sensor_report.get("fuel_type"),
            "data_quality":  sensor_report.get("data_quality"),
            "validated":     approved,
            "confidence":    confidence,
            "anomaly":       validator_report.get("anomaly_type"),
            "cert_issued":   gov_approved,
            "token_id":      token_id,
            "co2_saved_mg":  saved_mg,
            "credits_cct":   governance_report.get("credits_cct", 0.0),
            "record_id":     record_id,
        }

        response = self._chain.invoke({
            "pipeline_summary": json.dumps(pipeline_summary, indent=2),
            "tx_results":       json.dumps(tx_results, indent=2),
        })

        try:
            llm_result      = extract_json(response.content)
            audit_summary   = llm_result.get("audit_summary", "")
            pipeline_status = llm_result.get("pipeline_status", "success")
        except Exception:
            co2_mg = sensor_report.get("total_co2_mg", 0)
            cct    = governance_report.get("credits_cct", 0.0)
            tid    = f"NFT #{token_id}" if token_id is not None else "no certificate"
            audit_summary = (
                f"Vehicle {sensor_report.get('vehicle_id')} emitted {co2_mg:.0f} mg CO2 "
                f"({sensor_report.get('fuel_type')}). "
                f"Validator {'approved' if approved else 'rejected'} record #{record_id} "
                f"(confidence {confidence}/100). "
                f"Governance {'issued ' + tid + ' (' + str(round(cct, 6)) + ' CCT equivalent).' if gov_approved else 'denied certificate issuance.'}"
            )
            pipeline_status = "success" if record_id >= 0 else "partial"

        return {
            "agent":           "BlockchainAgent",
            "vehicle_id":      sensor_report.get("vehicle_id", "unknown"),
            "pipeline_status": pipeline_status,
            "audit_summary":   audit_summary,
            "on_chain_data": {
                "emission_record_id":  record_id,
                "emission_tx":         (tx_results["emission_log"] or {}).get("tx_hash"),
                "validation_tx":       (tx_results["validation"] or {}).get("tx_hash"),
                "governance_tx":       (tx_results["governance_status"] or {}).get("tx_hash"),
                "certificate_tx":      (tx_results["certificate_mint"] or {}).get("tx_hash"),
                "token_id":            token_id,
                "credits_cct":         governance_report.get("credits_cct", 0.0),
                "co2_saved_mg":        saved_mg,
                "data_hash":           sha256_hex,
            },
        }
