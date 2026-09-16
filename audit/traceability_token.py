"""
traceability_token.py — Demonstrates the audit trail of a certificate.

Rebuilds the full provenance chain of a carbon credit token and verifies the
cryptographic integrity of the source data:

    NFT token  ->  Certificate  ->  EmissionRecord  ->  Raw CSV
                                         (dataHash)        (recomputed SHA-256)
"""
from __future__ import annotations

import sys as _sys, pathlib as _pathlib
_sys.path.insert(0, str(_pathlib.Path(__file__).resolve().parent.parent))  # repository root on the path (config, tools, ...)

import argparse
import hashlib
import json
import sys
import textwrap
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")  # lets the Windows console print CO₂, ✓, etc.
except Exception:
    pass

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import FancyBboxPatch, FancyArrowPatch


def find_manifest(results_dir: Path) -> Path:
    # Run folders follow the lab naming: rodada_unica (single run) and rodada_N (run N)
    single_run = sorted(results_dir.glob("rodada_unica*/**/_manifest.json"))
    if single_run:
        return single_run[0]
    any_run = sorted(results_dir.glob("rodada_*/**/_manifest.json"))
    if not any_run:
        any_run = sorted(results_dir.glob("**/_manifest.json"))
    if not any_run:
        raise SystemExit(f"No _manifest.json in {results_dir}")
    return any_run[0]


def load_session(manifest_path: Path, token: int | None):
    man = json.loads(manifest_path.read_text(encoding="utf-8"))
    sessions = [s for s in man["sessions"] if s.get("token_id") is not None]
    if not sessions:
        raise SystemExit("No session with a token in this run.")
    if token is not None:
        sessions = [s for s in sessions if s.get("token_id") == token]
        if not sessions:
            raise SystemExit(f"Token {token} not found in this run.")
    sess = sessions[0]
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
    """Optional on-chain query. Returns the record, or None on failure."""
    try:
        from tools.blockchain_tools import get_emission_record, get_issuance_history
        import config
        rec = json.loads(get_emission_record.invoke({"record_id": record_id}))
        return rec
    except Exception as exc:  # Besu down / missing .env
        print(f"  [i] On-chain query unavailable: {exc}")
        return None


def draw_chain(report: dict, sess: dict, match, out_path: Path):
    s = report["summary"]
    gov = report.get("phases", {}).get("governance", {})
    rationale = gov.get("rationale", "") or ""
    rationale = textwrap.fill(rationale[:240] + ("…" if len(rationale) > 240 else ""), 60)

    boxes = [
        ("NFT token  #{}".format(s["token_id"]),
         f"ERC-721 certificate\nrecipient: {report['recipient'][:14]}…\ntx: {s['certificate_tx'][:16]}…",
         "#4C72B0"),
        ("Credit certificate",
         f"vehicle: {report['vehicle_id']}\nCO₂ saved: {s['co2_saved_mg']/1000:.1f} g\n"
         f"credits: {s['credits_cct']:.4f} CCT\njustification (LLM):\n{rationale}",
         "#55A868"),
        ("EmissionRecord  #{}".format(s["emission_record_id"]),
         f"CO₂: {s['total_co2_mg']/1000:.1f} g | {s['fuel_type']}\n"
         f"confidence: {s['confidence']}/100 | {s['governance_decision']}\n"
         f"governance_tx: {s['governance_tx'][:16]}…\nemission_tx: {s['emission_tx'][:16]}…",
         "#8172B3"),
        ("dataHash (on-chain)",
         f"SHA-256:\n{s['data_hash'][:32]}…\n{s['data_hash'][32:]}",
         "#937860"),
        ("Original raw CSV",
         f"{sess['csv']}\nSHA-256 recomputed and compared",
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

    # verification seal between the dataHash and the CSV
    if match is True:
        ax.text(5, 0.3, "✓ SHA-256 matches — data intact and anchored on-chain",
                ha="center", fontsize=12, fontweight="bold", color="#2E7D32",
                bbox=dict(boxstyle="round", facecolor="#E8F5E9", edgecolor="#2E7D32"))
    elif match is False:
        ax.text(5, 0.3, "✗ SHA-256 differs — the CSV bytes are not the submitted ones\n"
                        "(regenerated synthetic data; use the data-dir of the run)",
                ha="center", fontsize=10, fontweight="bold", color="#B71C1C",
                bbox=dict(boxstyle="round", facecolor="#FFEBEE", edgecolor="#B71C1C"))
    else:
        ax.text(5, 0.3, "Original CSV not found in the given data-dir",
                ha="center", fontsize=10, color="#555")

    ax.set_title("Traceability of a carbon credit certificate",
                 fontsize=14, fontweight="bold", pad=15)
    fig.tight_layout()
    fig.savefig(out_path, dpi=200, bbox_inches="tight")
    print(f"\nDiagram saved to: {out_path}")


def main() -> int:
    ap = argparse.ArgumentParser(description="Audit trail of a credit token.")
    ap.add_argument("--results-dir", default="results")
    ap.add_argument("--manifest", default=None,
                    help="Path to the _manifest.json of the run (default: auto-discovery)")
    ap.add_argument("--data-dir", default="data_synthetic",
                    help="Folder with the original submitted CSVs (default: data_synthetic)")
    ap.add_argument("--token", type=int, default=None, help="specific token_id")
    ap.add_argument("--besu", action="store_true", help="also query on-chain")
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
    print(f"TRACEABILITY — NFT token #{s['token_id']}  (run: {run_label})")
    print("=" * 64)
    print(f"Vehicle            : {report['vehicle_id']}")
    print(f"Scenario           : {sess.get('scenario')}")
    print(f"Total CO₂          : {s['total_co2_mg']/1000:.1f} g ({s['fuel_type']})")
    print(f"CO₂ saved          : {s['co2_saved_mg']/1000:.1f} g")
    print(f"Credits issued     : {s['credits_cct']:.6f} CCT")
    print(f"Confidence/decision: {s['confidence']}/100 | {s['governance_decision']}")
    print(f"EmissionRecord     : #{s['emission_record_id']}")
    print(f"Transactions       : emission={s['emission_tx'][:18]}…")
    print(f"                     governance={s['governance_tx'][:18]}…")
    print(f"                     certificate={s['certificate_tx'][:18]}…")
    print(f"dataHash (on-chain): {s['data_hash']}")

    csv_path, recomputed, match = verify_hash(sess, Path(args.data_dir), s["data_hash"])
    print("\n-- Integrity check --")
    print(f"Original CSV       : {csv_path}")
    if recomputed is None:
        print("  [!] CSV not found in the data-dir.")
    else:
        print(f"Recomputed SHA-256 : {recomputed}")
        print(f"INTEGRITY          : {'MATCHES ✓' if match else 'DIFFERS ✗ (the bytes are not the submitted ones)'}")

    if args.besu:
        print("\n-- On-chain query (Besu) --")
        rec = query_besu(s["emission_record_id"], s["token_id"])
        if rec and rec.get("status") == "success":
            print(f"  record on-chain: vehicle={rec['vehicle_id']} co2_mg={rec['co2_milligrams']} "
                  f"status={rec['governance_status']} dataHash={rec['data_hash'][:16]}…")

    out = results_dir / "accuracy_analysis" / f"token_traceability_{s['token_id']}.png"
    out.parent.mkdir(parents=True, exist_ok=True)
    draw_chain(report, sess, match, out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
