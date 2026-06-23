"""
relatorio_auditoria_onchain.py — Relatório de auditoria LIDO DA BLOCKCHAIN.

Para cada viagem de uma rodada, lê do Besu o EmissionRecord (e o certificado, se
houver) e monta um relatório de auditoria com o que realmente está gravado on-chai
"""
from __future__ import annotations

import sys as _sys, pathlib as _pathlib
_sys.path.insert(0, str(_pathlib.Path(__file__).resolve().parent.parent))  # raiz do repo no path (config, tools, ...)

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
    ap = argparse.ArgumentParser(description="Relatório de auditoria on-chain por viagem.")
    ap.add_argument("--manifest", required=True, help="_manifest.json da rodada (ex.: viagens reais)")
    ap.add_argument("--data-dir", default="data", help="CSVs originais submetidos")
    ap.add_argument("--out", default=None, help="prefixo de saída (default: ao lado do manifesto)")
    args = ap.parse_args()

    from tools.blockchain_tools import get_emission_record, get_certificate

    mpath = Path(args.manifest).resolve()
    man = json.loads(mpath.read_text(encoding="utf-8"))
    data_dir = Path(args.data_dir).resolve()

    sessoes = [s for s in man["sessions"]
               if s.get("status") == "ok" and s.get("emission_record_id") is not None]
    print(f"Manifesto: {mpath}")
    print(f"Viagens com registro on-chain: {len(sessoes)}\n")

    entradas = []
    for s in sessoes:
        rid = s["emission_record_id"]
        rec = json.loads(get_emission_record.invoke({"record_id": rid}))
        if rec.get("status") != "success":
            print(f"  [!] record #{rid}: {rec.get('message')}")
            continue

        # verificação de integridade
        csv_path = data_dir / s["csv"].replace("\\", "/")
        recomputed = hashlib.sha256(csv_path.read_bytes()).hexdigest() if csv_path.exists() else None
        on_chain_hash = rec["data_hash"].replace("0x", "")
        integro = (recomputed == on_chain_hash) if recomputed else None

        # certificado (se aprovada)
        cert = None
        if s.get("token_id") is not None:
            c = json.loads(get_certificate.invoke({"token_id": s["token_id"]}))
            if c.get("status") == "success":
                cert = c

        entrada = {
            "csv": s["csv"], "vehicle_id": rec["vehicle_id"],
            "record_id": rid, "token_id": s.get("token_id"),
            "co2_mg": rec["co2_milligrams"], "fuel_type": rec["fuel_type"],
            "confidence": rec["agent_confidence"],
            "governance_status": rec["governance_status"],
            "validated": rec["validated"],
            "data_hash_onchain": on_chain_hash,
            "sha256_recalculado": recomputed,
            "integridade": integro,
            "agent_decision_onchain": rec["agent_decision"],
            "pipeline_metadata_onchain": rec["pipeline_metadata"],
            "certificate_reason_onchain": cert["reason"] if cert else None,
            "credits_cct": cert["credits_equivalent_cct"] if cert else 0.0,
        }
        entradas.append(entrada)

        # impressão legível
        sel = "✓ íntegro" if integro else ("✗ diverge" if integro is False else "? sem CSV")
        print("=" * 70)
        print(f"Viagem: {s['csv']}  |  veículo: {rec['vehicle_id']}")
        print(f"  EmissionRecord #{rid}  | governança: {rec['governance_status']} "
              f"| confiança: {rec['agent_confidence']}/100 | token: {s.get('token_id')}")
        print(f"  CO2: {rec['co2_milligrams']/1000:.1f} g ({rec['fuel_type']})")
        print(f"  dataHash on-chain: {on_chain_hash}")
        print(f"  integridade: {sel}")
        print(f"  [LLM on-chain] agentDecision:\n    {rec['agent_decision']}")
        print(f"  [on-chain] pipelineMetadata: {rec['pipeline_metadata'][:120]}")
        if cert:
            print(f"  [LLM on-chain] certificado.reason:\n    {cert['reason']}")

    # saídas
    base = Path(args.out).resolve() if args.out else mpath.parent / "auditoria_onchain"
    (base.with_suffix(".json")).write_text(
        json.dumps({"manifest": str(mpath), "total": len(entradas), "viagens": entradas},
                   indent=2, ensure_ascii=False), encoding="utf-8")

    # relatório markdown legível
    md = ["# Relatório de auditoria on-chain\n",
          f"Fonte: cadeia Besu | manifesto: `{mpath.name}` | {len(entradas)} viagens\n"]
    for e in entradas:
        md.append(f"\n## {e['vehicle_id']} — registro #{e['record_id']}\n")
        md.append(f"- **CSV:** `{e['csv']}`")
        md.append(f"- **CO2:** {e['co2_mg']/1000:.1f} g ({e['fuel_type']})")
        md.append(f"- **Governança:** {e['governance_status']} | confiança {e['confidence']}/100 "
                  f"| token {e['token_id']}")
        ig = "✓ íntegro" if e["integridade"] else ("✗ diverge" if e["integridade"] is False else "sem CSV")
        md.append(f"- **dataHash on-chain:** `{e['data_hash_onchain']}` ({ig})")
        md.append(f"- **Justificativa dos agentes (on-chain):** {e['agent_decision_onchain']}")
        md.append(f"- **Metadados do modelo (on-chain):** {e['pipeline_metadata_onchain']}")
        if e["certificate_reason_onchain"]:
            md.append(f"- **Rationale do certificado (on-chain):** {e['certificate_reason_onchain']}")
    (base.with_suffix(".md")).write_text("\n".join(md), encoding="utf-8")

    print(f"\nRelatórios salvos: {base.with_suffix('.json')} e {base.with_suffix('.md')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
