"""
rastreabilidade_token.py — Demonstra a trilha de auditoria de um certificado.

Reconstrói a cadeia de proveniência completa de um token de crédito de carbono e
verifica a integridade criptográfica do dado de origem:

    Token NFT  ->  Certificado  ->  EmissionRecord  ->  CSV bruto
                                         (dataHash)        (SHA-256 recalculado)
"""
from __future__ import annotations

import sys as _sys, pathlib as _pathlib
_sys.path.insert(0, str(_pathlib.Path(__file__).resolve().parent.parent))  # raiz do repo no path (config, tools, ...)

import argparse
import hashlib
import json
import sys
import textwrap
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")  # console Windows imprime CO₂, ✓, etc.
except Exception:
    pass

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import FancyBboxPatch, FancyArrowPatch


def find_manifest(results_dir: Path) -> Path:
    unica = sorted(results_dir.glob("rodada_unica*/**/_manifest.json"))
    if unica:
        return unica[0]
    any_run = sorted(results_dir.glob("rodada_*/**/_manifest.json"))
    if not any_run:
        any_run = sorted(results_dir.glob("**/_manifest.json"))
    if not any_run:
        raise SystemExit(f"Nenhum _manifest.json em {results_dir}")
    return any_run[0]


def load_session(manifest_path: Path, token: int | None):
    man = json.loads(manifest_path.read_text(encoding="utf-8"))
    sessoes = [s for s in man["sessions"] if s.get("token_id") is not None]
    if not sessoes:
        raise SystemExit("Nenhuma sessão com token nesta rodada.")
    if token is not None:
        sessoes = [s for s in sessoes if s.get("token_id") == token]
        if not sessoes:
            raise SystemExit(f"Token {token} não encontrado nesta rodada.")
    sess = sessoes[0]
    rep_path = next(manifest_path.parent.rglob(sess["report_file"]))
    report = json.loads(rep_path.read_text(encoding="utf-8"))
    return man, sess, report


def verify_hash(sess: dict, data_dir: Path, data_hash: str):
    csv_path = data_dir / sess["csv"].replace("\\", "/")
    if not csv_path.exists():
        return csv_path, None, None
    recomputed = hashlib.sha256(csv_path.read_bytes()).hexdigest()
    return csv_path, recomputed, (recomputed == data_hash)


def query_besu(record_id: int, token_id: int):
    """Consulta opcional on-chain. Retorna (record, certificate) ou None em falha."""
    try:
        from tools.blockchain_tools import get_emission_record, get_issuance_history
        import config
        rec = json.loads(get_emission_record.invoke({"record_id": record_id}))
        return rec
    except Exception as exc:  # Besu fora do ar / .env ausente
        print(f"  [i] Consulta on-chain indisponível: {exc}")
        return None


def draw_chain(report: dict, sess: dict, match, out_path: Path):
    s = report["summary"]
    gov = report.get("phases", {}).get("governance", {})
    rationale = gov.get("rationale", "") or ""
    rationale = textwrap.fill(rationale[:240] + ("…" if len(rationale) > 240 else ""), 60)

    boxes = [
        ("Token NFT  #{}".format(s["token_id"]),
         f"Certificado ERC-721\nrecipient: {report['recipient'][:14]}…\ntx: {s['certificate_tx'][:16]}…",
         "#4C72B0"),
        ("Certificado de crédito",
         f"veículo: {report['vehicle_id']}\nCO₂ economizado: {s['co2_saved_mg']/1000:.1f} g\n"
         f"créditos: {s['credits_cct']:.4f} CCT\njustificativa (LLM):\n{rationale}",
         "#55A868"),
        ("EmissionRecord  #{}".format(s["emission_record_id"]),
         f"CO₂: {s['total_co2_mg']/1000:.1f} g | {s['fuel_type']}\n"
         f"confiança: {s['confidence']}/100 | {s['governance_decision']}\n"
         f"governance_tx: {s['governance_tx'][:16]}…\nemission_tx: {s['emission_tx'][:16]}…",
         "#8172B3"),
        ("dataHash (on-chain)",
         f"SHA-256:\n{s['data_hash'][:32]}…\n{s['data_hash'][32:]}",
         "#937860"),
        ("CSV bruto original",
         f"{sess['csv']}\nSHA-256 recalculado e comparado",
         "#C44E52"),
    ]

    fig, ax = plt.subplots(figsize=(8.5, 12))
    ax.set_xlim(0, 10); ax.set_ylim(0, len(boxes) * 2.4)
    ax.axis("off")
    y = len(boxes) * 2.4 - 1.0
    centers = []
    for title, body, color in boxes:
        box = FancyBboxPatch((1, y - 1.4), 8, 1.7, boxstyle="round,pad=0.1",
                             linewidth=2, edgecolor=color, facecolor=color + "22")
        ax.add_patch(box)
        ax.text(5, y - 0.05, title, ha="center", va="top", fontsize=12,
                fontweight="bold", color=color)
        ax.text(5, y - 0.45, body, ha="center", va="top", fontsize=8.5)
        centers.append(y - 1.4)
        y -= 2.4

    for i in range(len(boxes) - 1):
        ar = FancyArrowPatch((5, centers[i]), (5, centers[i] + 2.4 - 1.7),
                             arrowstyle="-|>", mutation_scale=20, lw=2, color="#444")
        ax.add_patch(ar)

    # selo de verificação entre dataHash e CSV
    if match is True:
        ax.text(5, 0.3, "✓ SHA-256 confere — dado íntegro e ancorado on-chain",
                ha="center", fontsize=12, fontweight="bold", color="#2E7D32",
                bbox=dict(boxstyle="round", facecolor="#E8F5E9", edgecolor="#2E7D32"))
    elif match is False:
        ax.text(5, 0.3, "✗ SHA-256 difere — bytes do CSV não são os submetidos\n"
                        "(dado sintético regenerado; use o data-dir da rodada)",
                ha="center", fontsize=10, fontweight="bold", color="#B71C1C",
                bbox=dict(boxstyle="round", facecolor="#FFEBEE", edgecolor="#B71C1C"))
    else:
        ax.text(5, 0.3, "CSV original não encontrado no data-dir informado",
                ha="center", fontsize=10, color="#555")

    ax.set_title("Rastreabilidade de um certificado de crédito de carbono",
                 fontsize=14, fontweight="bold", pad=15)
    fig.tight_layout()
    fig.savefig(out_path, dpi=200, bbox_inches="tight")
    print(f"\nDiagrama salvo em: {out_path}")


def main() -> int:
    ap = argparse.ArgumentParser(description="Trilha de auditoria de um token de crédito.")
    ap.add_argument("--results-dir", default="results")
    ap.add_argument("--manifest", default=None,
                    help="Caminho do _manifest.json da rodada (default: auto-descoberta)")
    ap.add_argument("--data-dir", default="data_synthetic",
                    help="Onde estão os CSVs originais submetidos (default: data_synthetic)")
    ap.add_argument("--token", type=int, default=None, help="token_id específico")
    ap.add_argument("--besu", action="store_true", help="também consultar on-chain")
    args = ap.parse_args()

    results_dir = Path(args.results_dir).resolve()
    mpath = Path(args.manifest).resolve() if args.manifest else find_manifest(results_dir)
    man, sess, report = load_session(mpath, args.token)
    s = report["summary"]
    try:
        run_label = mpath.relative_to(results_dir).parts[0]
    except ValueError:
        run_label = mpath.parent.name

    print("=" * 64)
    print(f"RASTREABILIDADE — Token NFT #{s['token_id']}  (rodada: {run_label})")
    print("=" * 64)
    print(f"Veículo            : {report['vehicle_id']}")
    print(f"Cenário            : {sess.get('scenario')}")
    print(f"CO₂ total          : {s['total_co2_mg']/1000:.1f} g ({s['fuel_type']})")
    print(f"CO₂ economizado    : {s['co2_saved_mg']/1000:.1f} g")
    print(f"Créditos emitidos  : {s['credits_cct']:.6f} CCT")
    print(f"Confiança/decisão  : {s['confidence']}/100 | {s['governance_decision']}")
    print(f"EmissionRecord     : #{s['emission_record_id']}")
    print(f"Transações         : emission={s['emission_tx'][:18]}…")
    print(f"                     governance={s['governance_tx'][:18]}…")
    print(f"                     certificate={s['certificate_tx'][:18]}…")
    print(f"dataHash (on-chain): {s['data_hash']}")

    csv_path, recomputed, match = verify_hash(sess, Path(args.data_dir), s["data_hash"])
    print("\n-- Verificação de integridade --")
    print(f"CSV original       : {csv_path}")
    if recomputed is None:
        print("  [!] CSV não encontrado no data-dir.")
    else:
        print(f"SHA-256 recalculado: {recomputed}")
        print(f"INTEGRIDADE        : {'CONFERE ✓' if match else 'DIFERE ✗ (bytes não são os submetidos)'}")

    if args.besu:
        print("\n-- Consulta on-chain (Besu) --")
        rec = query_besu(s["emission_record_id"], s["token_id"])
        if rec and rec.get("status") == "success":
            print(f"  record on-chain: vehicle={rec['vehicle_id']} co2_mg={rec['co2_milligrams']} "
                  f"status={rec['governance_status']} dataHash={rec['data_hash'][:16]}…")

    out = results_dir / "analise_acertividade" / f"rastreabilidade_token_{s['token_id']}.png"
    out.parent.mkdir(parents=True, exist_ok=True)
    draw_chain(report, sess, match, out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
