"""
scalability_analysis.py — Scalability curve of the tokenization layer.

Reads the results of the worker sweep (results/bench_w*/benchmark_tokenization.json)
and produces the key figure of the experiment: throughput and peak of tx per block as a
function of concurrency, showing the saturation point of the QBFT network.

Usage:
    python scalability_analysis.py
    python scalability_analysis.py --glob "results/bench_w*"
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import seaborn as sns

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

sns.set_theme(style="whitegrid", context="talk")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--glob", default="results/bench_w*")
    ap.add_argument("--out-dir", default="results/scalability_analysis")
    ap.add_argument("--block-period", type=float, default=4.0, help="QBFT block period (s)")
    args = ap.parse_args()

    rows = []
    for d in sorted(Path(".").glob(args.glob)):
        jf = d / "benchmark_tokenization.json"
        if not jf.exists():
            continue
        m = re.search(r"w0*(\d+)", d.name)
        if not m:
            continue
        j = json.loads(jf.read_text(encoding="utf-8"))
        rows.append({
            "workers": int(m.group(1)),
            "tx_s": j["throughput_tx_s"],
            "peak": j["peak_tx_per_block"],
            "block_mean": j["mean_tx_per_block"],
            "lat_mean": j["mean_latency_s"],
            "lat_p95": j["p95_latency_s"],
            "gas_mean": j["mean_gas"],
        })
    if not rows:
        raise SystemExit(f"No benchmark_tokenization.json found in {args.glob}")
    rows.sort(key=lambda r: r["workers"])

    out = Path(args.out_dir).resolve(); out.mkdir(parents=True, exist_ok=True)
    W = [r["workers"] for r in rows]

    # table
    print(f"{'workers':>8} {'tx/s':>7} {'peak':>6} {'mean/block':>12} {'lat_mean':>8} {'lat_p95':>8}")
    for r in rows:
        print(f"{r['workers']:>8} {r['tx_s']:>7.1f} {r['peak']:>6} {r['block_mean']:>12.1f} "
              f"{r['lat_mean']:>8.2f} {r['lat_p95']:>8.2f}")

    # estimate of the analytical ceiling (gasLimit ≈ peak_max × gas_mean)
    peak_max = max(r["peak"] for r in rows)
    gas_mean = sum(r["gas_mean"] for r in rows) / len(rows)
    tx_s_plateau = sum(r["tx_s"] for r in rows[-3:]) / min(3, len(rows))
    print(f"\nThroughput plateau (mean of the 3 largest worker counts): {tx_s_plateau:.1f} tx/s")
    print(f"Maximum peak of tx per block: {peak_max}  (≈ gasLimit / {gas_mean:.0f} gas/tx "
          f"→ gasLimit ~ {peak_max*gas_mean/1e6:.1f}M)")

    # ── Main figure: throughput and peak vs workers ───────────────────────────
    fig, ax1 = plt.subplots(figsize=(11, 6))
    c1, c2 = sns.color_palette("deep")[:2]

    ax1.plot(W, [r["tx_s"] for r in rows], "o-", color=c1, lw=2.5, ms=9, label="Throughput (tx/s)")
    ax1.axhline(tx_s_plateau, color=c1, ls=":", lw=1.5, alpha=0.7)
    ax1.set_xlabel("Concurrent workers")
    ax1.set_ylabel("Throughput (tx/s)", color=c1)
    ax1.tick_params(axis="y", labelcolor=c1)
    ax1.set_ylim(0, max(r["tx_s"] for r in rows) * 1.25)

    ax2 = ax1.twinx()
    ax2.plot(W, [r["peak"] for r in rows], "s--", color=c2, lw=2.5, ms=9, label="Peak tx/block")
    ax2.axhline(peak_max, color=c2, ls=":", lw=1.5, alpha=0.7)
    ax2.set_ylabel("Peak transactions per block", color=c2)
    ax2.tick_params(axis="y", labelcolor=c2)
    ax2.grid(False)

    ax1.set_title("Scalability of the tokenization layer (Besu/QBFT)")
    lines = ax1.get_lines()[:1] + ax2.get_lines()[:1]
    ax1.legend(lines, [l.get_label() for l in lines], loc="lower right")
    fig.tight_layout(); fig.savefig(out / "scalability.png", dpi=200); plt.close(fig)

    # ── Latency vs workers ────────────────────────────────────────────────────
    fig, ax = plt.subplots(figsize=(10, 5))
    ax.plot(W, [r["lat_mean"] for r in rows], "o-", label="mean", lw=2)
    ax.plot(W, [r["lat_p95"] for r in rows], "s--", label="p95", lw=2)
    ax.set_xlabel("Concurrent workers"); ax.set_ylabel("Confirmation latency (s)")
    ax.set_title("Latency under increasing load")
    ax.legend(); ax.set_ylim(0, max(r["lat_p95"] for r in rows) * 1.2)
    fig.tight_layout(); fig.savefig(out / "latency_vs_workers.png", dpi=200); plt.close(fig)

    import csv
    with open(out / "scalability.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys())); w.writeheader(); w.writerows(rows)
    print(f"\nArtifacts saved to: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
