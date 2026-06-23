"""
analise_escalabilidade.py — Curva de escalabilidade da tokenização (Exp. 3).

Lê os resultados da varredura de workers (results/bench_w*/benchmark_tokenizacao.json)
e produz a figura-chave do experimento: throughput e pico de tx/bloco em função da
concorrência, evidenciando o ponto de saturação da rede QBFT.

Uso:
    python analise_escalabilidade.py
    python analise_escalabilidade.py --glob "results/bench_w*"
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
    ap.add_argument("--out-dir", default="results/analise_escalabilidade")
    ap.add_argument("--block-period", type=float, default=4.0, help="período de bloco QBFT (s)")
    args = ap.parse_args()

    rows = []
    for d in sorted(Path(".").glob(args.glob)):
        jf = d / "benchmark_tokenizacao.json"
        if not jf.exists():
            continue
        m = re.search(r"w0*(\d+)", d.name)
        if not m:
            continue
        j = json.loads(jf.read_text(encoding="utf-8"))
        rows.append({
            "workers": int(m.group(1)),
            "tx_s": j["throughput_tx_s"],
            "pico": j["tx_por_bloco_pico"],
            "media_bloco": j["tx_por_bloco_media"],
            "lat_med": j["latencia_media_s"],
            "lat_p95": j["latencia_p95_s"],
            "gas_medio": j["gas_medio"],
        })
    if not rows:
        raise SystemExit(f"Nenhum benchmark_tokenizacao.json em {args.glob}")
    rows.sort(key=lambda r: r["workers"])

    out = Path(args.out_dir).resolve(); out.mkdir(parents=True, exist_ok=True)
    W = [r["workers"] for r in rows]

    # tabela
    print(f"{'workers':>8} {'tx/s':>7} {'pico':>6} {'média/bloco':>12} {'lat_med':>8} {'lat_p95':>8}")
    for r in rows:
        print(f"{r['workers']:>8} {r['tx_s']:>7.1f} {r['pico']:>6} {r['media_bloco']:>12.1f} "
              f"{r['lat_med']:>8.2f} {r['lat_p95']:>8.2f}")

    # estimativa do teto analítico (gasLimit ≈ pico_max × gas_medio)
    pico_max = max(r["pico"] for r in rows)
    gas_med = sum(r["gas_medio"] for r in rows) / len(rows)
    tx_s_plateau = sum(r["tx_s"] for r in rows[-3:]) / min(3, len(rows))
    print(f"\nPlatô de throughput (média dos 3 maiores): {tx_s_plateau:.1f} tx/s")
    print(f"Pico máximo de tx/bloco: {pico_max}  (≈ gasLimit / {gas_med:.0f} gás/tx "
          f"→ gasLimit ~ {pico_max*gas_med/1e6:.1f}M)")

    # ── Figura principal: throughput e pico vs workers ────────────────────────
    fig, ax1 = plt.subplots(figsize=(11, 6))
    c1, c2 = sns.color_palette("deep")[:2]

    ax1.plot(W, [r["tx_s"] for r in rows], "o-", color=c1, lw=2.5, ms=9, label="Throughput (tx/s)")
    ax1.axhline(tx_s_plateau, color=c1, ls=":", lw=1.5, alpha=0.7)
    ax1.set_xlabel("Workers concorrentes")
    ax1.set_ylabel("Throughput (tx/s)", color=c1)
    ax1.tick_params(axis="y", labelcolor=c1)
    ax1.set_ylim(0, max(r["tx_s"] for r in rows) * 1.25)

    ax2 = ax1.twinx()
    ax2.plot(W, [r["pico"] for r in rows], "s--", color=c2, lw=2.5, ms=9, label="Pico tx/bloco")
    ax2.axhline(pico_max, color=c2, ls=":", lw=1.5, alpha=0.7)
    ax2.set_ylabel("Pico de transações por bloco", color=c2)
    ax2.tick_params(axis="y", labelcolor=c2)
    ax2.grid(False)

    ax1.set_title("Escalabilidade da camada de tokenização (Besu/QBFT)")
    lines = ax1.get_lines()[:1] + ax2.get_lines()[:1]
    ax1.legend(lines, [l.get_label() for l in lines], loc="lower right")
    fig.tight_layout(); fig.savefig(out / "escalabilidade.png", dpi=200); plt.close(fig)

    # ── Latência vs workers ───────────────────────────────────────────────────
    fig, ax = plt.subplots(figsize=(10, 5))
    ax.plot(W, [r["lat_med"] for r in rows], "o-", label="mediana", lw=2)
    ax.plot(W, [r["lat_p95"] for r in rows], "s--", label="p95", lw=2)
    ax.set_xlabel("Workers concorrentes"); ax.set_ylabel("Latência de confirmação (s)")
    ax.set_title("Latência sob carga crescente")
    ax.legend(); ax.set_ylim(0, max(r["lat_p95"] for r in rows) * 1.2)
    fig.tight_layout(); fig.savefig(out / "latencia_vs_workers.png", dpi=200); plt.close(fig)

    import csv
    with open(out / "escalabilidade.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys())); w.writeheader(); w.writerows(rows)
    print(f"\nArtefatos salvos em: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
