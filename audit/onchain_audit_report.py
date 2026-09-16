"""
onchain_audit_report.py — Audit report READ FROM THE BLOCKCHAIN.

For each trip of a run, reads the EmissionRecord (and the certificate, when there is
one) from Besu and builds an audit report with what is actually stored on-chain.
"""
from __future__ import annotations

import sys as _sys, pathlib as _pathlib
_sys.path.insert(0, str(_pathlib.Path(__file__).resolve().parent.parent))  # repository root on the path (config, tools, ...)

import argparse
import hashlib
import json
import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass


def main() -> int:
    ap = argparse.ArgumentParser(description="On-chain audit report per trip.")
    ap.add_argument("--manifest", required=True, help="_manifest.json of the run (e.g. real trips)")
    ap.add_argument("--data-dir", default="data", help="Original submitted CSVs")
    ap.add_argument("--out", default=None, help="output prefix (default: next to the manifest)")
    args = ap.parse_args()

    from tools.blockchain_tools import get_emission_record, get_certificate

    mpath = Path(args.manifest).resolve()
    man = json.loads(mpath.read_text(encoding="utf-8"))
    data_dir = Path(args.data_dir).resolve()

    sessions = [s for s in man["sessions"]
                if s.get("status") == "ok" and s.get("emission_record_id") is not None]
    print(f"Manifest: {mpath}")
    print(f"Trips with an on-chain record: {len(sessions)}\n")

    entries = []
    for s in sessions:
        rid = s["emission_record_id"]
        rec = json.loads(get_emission_record.invoke({"record_id": rid}))
        if rec.get("status") != "success":
            print(f"  [!] record #{rid}: {rec.get('message')}")
            continue

        # integrity check
        csv_path = data_dir / s["csv"].replace("\\", "/")
        recomputed = hashlib.sha256(csv_path.read_bytes()).hexdigest() if csv_path.exists() else None
        on_chain_hash = rec["data_hash"].replace("0x", "")
        intact = (recomputed == on_chain_hash) if recomputed else None

        # certificate (if approved)
        cert = None
        if s.get("token_id") is not None:
            c = json.loads(get_certificate.invoke({"token_id": s["token_id"]}))
            if c.get("status") == "success":
                cert = c

        entry = {
            "csv": s["csv"], "vehicle_id": rec["vehicle_id"],
            "record_id": rid, "token_id": s.get("token_id"),
            "co2_mg": rec["co2_milligrams"], "fuel_type": rec["fuel_type"],
            "confidence": rec["agent_confidence"],
            "governance_status": rec["governance_status"],
            "validated": rec["validated"],
            "data_hash_onchain": on_chain_hash,
            "sha256_recomputed": recomputed,
            "integrity": intact,
            "agent_decision_onchain": rec["agent_decision"],
            "pipeline_metadata_onchain": rec["pipeline_metadata"],
            "certificate_reason_onchain": cert["reason"] if cert else None,
            "credits_cct": cert["credits_equivalent_cct"] if cert else 0.0,
        }
        entries.append(entry)

        # readable printout
        sel = "✓ intact" if intact else ("✗ mismatch" if intact is False else "? no CSV")
        print("=" * 70)
        print(f"Trip: {s['csv']}  |  vehicle: {rec['vehicle_id']}")
        print(f"  EmissionRecord #{rid}  | governance: {rec['governance_status']} "
              f"| confidence: {rec['agent_confidence']}/100 | token: {s.get('token_id')}")
        print(f"  CO2: {rec['co2_milligrams']/1000:.1f} g ({rec['fuel_type']})")
        print(f"  on-chain dataHash: {on_chain_hash}")
        print(f"  integrity: {sel}")
        print(f"  [LLM on-chain] agentDecision:\n    {rec['agent_decision']}")
        print(f"  [on-chain] pipelineMetadata: {rec['pipeline_metadata'][:120]}")
        if cert:
            print(f"  [LLM on-chain] certificate.reason:\n    {cert['reason']}")

    # outputs
    base = Path(args.out).resolve() if args.out else mpath.parent / "onchain_audit"
    (base.with_suffix(".json")).write_text(
        json.dumps({"manifest": str(mpath), "total": len(entries), "trips": entries},
                   indent=2, ensure_ascii=False), encoding="utf-8")

    # readable markdown report
    md = ["# On-chain audit report\n",
          f"Source: Besu chain | manifest: `{mpath.name}` | {len(entries)} trips\n"]
    for e in entries:
        md.append(f"\n## {e['vehicle_id']} — record #{e['record_id']}\n")
        md.append(f"- **CSV:** `{e['csv']}`")
        md.append(f"- **CO2:** {e['co2_mg']/1000:.1f} g ({e['fuel_type']})")
        md.append(f"- **Governance:** {e['governance_status']} | confidence {e['confidence']}/100 "
                  f"| token {e['token_id']}")
        ig = "✓ intact" if e["integrity"] else ("✗ mismatch" if e["integrity"] is False else "no CSV")
        md.append(f"- **On-chain dataHash:** `{e['data_hash_onchain']}` ({ig})")
        md.append(f"- **Agent justification (on-chain):** {e['agent_decision_onchain']}")
        md.append(f"- **Model metadata (on-chain):** {e['pipeline_metadata_onchain']}")
        if e["certificate_reason_onchain"]:
            md.append(f"- **Certificate rationale (on-chain):** {e['certificate_reason_onchain']}")
    (base.with_suffix(".md")).write_text("\n".join(md), encoding="utf-8")

    print(f"\nReports saved: {base.with_suffix('.json')} and {base.with_suffix('.md')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
