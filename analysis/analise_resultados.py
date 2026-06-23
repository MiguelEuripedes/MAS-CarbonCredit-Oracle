"""
analise_resultados.py — Consolidação multi-rodada do Experimento 1.

Descobre automaticamente todas as rodadas em results/ (pastas que contêm um
_manifest.json) e produz uma análise agregada pronta para o capítulo de
resultados da dissertação:

  1. Determinismo  — a decisão (CO2, governança, créditos) é idêntica entre
                     rodadas? (propriedade central da arquitetura)
  2. Latência      — média ± desvio por fase, agregada sobre todas as rodadas;
                     mostra ONDE o tempo fim-a-fim é gasto.
  3. Corretude     — matriz de confusão (legítimo→aprovado / fraude→negado),
                     com tabelas de falsos positivos e falsos negativos.
  4. Economia      — CO2 economizado e créditos por cenário (média entre rodadas).

Saídas (em results/analise/):
  - consolidado.csv          : todas as sessões de todas as rodadas
  - latencia_por_fase.png
  - economia_por_cenario.png
  - matriz_confusao.png

Uso:
    python analise_resultados.py                 # descobre rodada_* em results/
    python analise_resultados.py --glob "run_*"  # outro padrão de pastas
    python analise_resultados.py --results-dir results --pattern "rodada_*"
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import pandas as pd
import matplotlib
matplotlib.use("Agg")  # backend sem display — salva PNGs direto
import matplotlib.pyplot as plt

# Rótulo esperado por cenário (prefixo numérico do nome do arquivo).
EXPECTED = {
    "01": ("legitimo", "approved"),
    "02": ("legitimo", "approved"),
    "03": ("legitimo", "approved"),
    "04": ("legitimo", "approved"),
    "05": ("agressivo", "approved"),
    "06": ("fraude", "denied"),
    "07": ("fraude", "denied"),
    "08": ("fraude", "denied"),
    "09": ("fraude", "denied"),
    "10": ("vazio", "error"),
}

LAT_COLS = ["lat_sensor_s", "lat_validator_s", "lat_governance_s", "lat_blockchain_s"]
PHASE_NAMES = ["sensor", "validator", "governance", "blockchain"]


def _expected(scenario: str) -> tuple[str, str]:
    key = scenario[:2] if scenario else ""
    return EXPECTED.get(key, ("desconhecido", "?"))


def load_runs(results_dir: Path, pattern: str) -> pd.DataFrame:
    """Carrega todas as sessões de todas as rodadas num único DataFrame."""
    manifests = sorted(results_dir.glob(f"{pattern}/**/_manifest.json"))
    if not manifests:
        # fallback: qualquer manifest sob results/
        manifests = sorted(results_dir.glob("**/_manifest.json"))
    if not manifests:
        raise SystemExit(f"Nenhum _manifest.json encontrado em {results_dir}")

    rows = []
    for mpath in manifests:
        data = json.loads(mpath.read_text(encoding="utf-8"))
        # nome da rodada = pasta de nível superior dentro de results/
        try:
            run_label = mpath.relative_to(results_dir).parts[0]
        except ValueError:
            run_label = mpath.parent.name
        for s in data.get("sessions", []):
            cat, exp_dec = _expected(s.get("scenario", ""))
            rows.append({
                "run":        run_label,
                "vehicle":    s.get("vehicle"),
                "viagem":     s.get("viagem"),
                "scenario":   s.get("scenario"),
                "categoria":  cat,
                "esperado":   exp_dec,
                "status":     s.get("status"),
                "decision":   s.get("governance_decision"),
                "anomaly":    s.get("anomaly"),
                "confidence": s.get("confidence"),
                "co2_g":      s.get("total_co2_g"),
                "saved_g":    s.get("co2_saved_g"),
                "distance_km": s.get("distance_km"),
                "credits_cct": s.get("credits_cct"),
                "elapsed_s":  s.get("elapsed_s"),
                **{c: s.get(c) for c in LAT_COLS},
            })
    df = pd.DataFrame(rows)
    print(f"Carregadas {len(manifests)} rodada(s): {sorted(df['run'].unique())}")
    print(f"Total de sessões: {len(df)}")
    return df


def check_determinism(df: pd.DataFrame) -> None:
    """A decisão deve ser idêntica entre rodadas para o mesmo cenário/veículo."""
    print("\n=== 1. DETERMINISMO (decisão entre rodadas) ===")
    ok = df[df["status"] == "ok"].copy()
    key = ["vehicle", "viagem", "scenario"]
    grp = ok.groupby(key).agg(
        n_runs=("run", "nunique"),
        decisoes=("decision", lambda x: set(x.dropna())),
        co2=("co2_g", lambda x: round(x.dropna().std() or 0, 6)),
        creditos=("credits_cct", lambda x: round((x.dropna().std() or 0), 9)),
    )
    divergentes = grp[(grp["decisoes"].apply(len) > 1) | (grp["co2"] > 0) | (grp["creditos"] > 0)]
    if divergentes.empty:
        print("  [OK] Decisão, CO2 e créditos IDÊNTICOS em todas as rodadas (determinismo confirmado).")
    else:
        print("  [X] Divergências encontradas entre rodadas:")
        print(divergentes.to_string())


def latency_stats(df: pd.DataFrame, out_dir: Path) -> None:
    print("\n=== 2. LATÊNCIA POR FASE (s) — todas as rodadas ===")
    ok = df[df["status"] == "ok"]
    means = [ok[c].mean() for c in LAT_COLS]
    stds = [ok[c].std() for c in LAT_COLS]
    for name, m, sd in zip(PHASE_NAMES, means, stds):
        print(f"  {name:11}: {m:6.2f} ± {sd:5.2f}")
    total = sum(m for m in means if pd.notna(m))
    print(f"  {'TOTAL':11}: {total:6.2f}")
    print("  Participação:")
    for name, m in zip(PHASE_NAMES, means):
        if pd.notna(m):
            print(f"    {name:11}: {100*m/total:5.1f}%")

    fig, ax = plt.subplots(figsize=(7, 4))
    ax.bar(PHASE_NAMES, means, yerr=stds, capsize=4, color="#4C72B0")
    ax.set_ylabel("Latência média (s)")
    ax.set_title("Latência por fase do pipeline (média ± desvio)")
    for i, m in enumerate(means):
        if pd.notna(m):
            ax.text(i, m, f"{m:.1f}s", ha="center", va="bottom")
    fig.tight_layout()
    fig.savefig(out_dir / "latencia_por_fase.png", dpi=150)
    plt.close(fig)


def correctness(df: pd.DataFrame, out_dir: Path) -> None:
    print("\n=== 3. CORRETUDE (esperado vs. obtido) ===")
    # Usa a primeira rodada como referência (decisões são determinísticas).
    ref_run = sorted(df["run"].unique())[0]
    d = df[df["run"] == ref_run].copy()
    print(f"  (referência: {ref_run} — decisões idênticas nas demais)")

    # Resultado observado normalizado
    def observed(row):
        if row["status"] == "error":
            return "error"
        return row["decision"]
    d["obtido"] = d.apply(observed, axis=1)

    # Acerto: bateu com o esperado
    d["acerto"] = d["obtido"] == d["esperado"]

    # Matriz por categoria
    tab = pd.crosstab(d["categoria"], d["obtido"])
    print("\n  Decisão obtida por categoria:")
    print(tab.to_string())

    # Falsos negativos: legítimo/agressivo que foi negado
    fn = d[(d["categoria"].isin(["legitimo", "agressivo"])) & (d["obtido"] == "denied")]
    if not fn.empty:
        print("\n  [!] Falsos negativos (legítimo NEGADO):")
        print(fn[["vehicle", "viagem", "scenario", "co2_g", "anomaly"]].to_string(index=False))

    # Falsos positivos: fraude que foi aprovada
    fp = d[(d["categoria"] == "fraude") & (d["obtido"] == "approved")]
    if not fp.empty:
        print("\n  [!] Falsos positivos (fraude APROVADA):")
        print(fp[["vehicle", "viagem", "scenario", "co2_g", "anomaly"]].to_string(index=False))
    else:
        print("\n  [OK] Nenhuma fraude aprovada (todas as fraudes corretamente negadas).")

    # Gráfico simples da matriz
    fig, ax = plt.subplots(figsize=(6, 4))
    tab.plot(kind="bar", stacked=True, ax=ax, colormap="Set2")
    ax.set_ylabel("Nº de sessões")
    ax.set_title(f"Decisão por categoria de cenário ({ref_run})")
    ax.set_xlabel("")
    fig.tight_layout()
    fig.savefig(out_dir / "matriz_confusao.png", dpi=150)
    plt.close(fig)


def savings(df: pd.DataFrame, out_dir: Path) -> None:
    print("\n=== 4. ECONOMIA DE CO2 E CRÉDITOS (média entre rodadas) ===")
    ok = df[(df["status"] == "ok") & (df["decision"] == "approved")].copy()
    if ok.empty:
        print("  (nenhuma sessão aprovada)")
        return
    ok["rotulo"] = ok["vehicle"] + " v" + ok["viagem"].astype(str) + " / " + ok["scenario"].str[:2]
    g = ok.groupby("rotulo").agg(
        saved_g=("saved_g", "mean"),
        credits=("credits_cct", "mean"),
    ).sort_values("saved_g", ascending=True)
    print(g.round(3).to_string())

    fig, ax = plt.subplots(figsize=(8, max(4, 0.3 * len(g))))
    ax.barh(g.index, g["saved_g"], color="#55A868")
    ax.set_xlabel("CO2 economizado (g) — média entre rodadas")
    ax.set_title("Economia por sessão aprovada")
    fig.tight_layout()
    fig.savefig(out_dir / "economia_por_cenario.png", dpi=150)
    plt.close(fig)


def main() -> int:
    ap = argparse.ArgumentParser(description="Consolida rodadas do Experimento 1.")
    ap.add_argument("--results-dir", default="results")
    ap.add_argument("--pattern", default="rodada_*",
                    help="Padrão das pastas de rodada (default: rodada_*)")
    args = ap.parse_args()

    results_dir = Path(args.results_dir).resolve()
    out_dir = results_dir / "analise"
    out_dir.mkdir(parents=True, exist_ok=True)

    df = load_runs(results_dir, args.pattern)
    df.to_csv(out_dir / "consolidado.csv", index=False, encoding="utf-8")

    check_determinism(df)
    latency_stats(df, out_dir)
    correctness(df, out_dir)
    savings(df, out_dir)

    print(f"\nArtefatos salvos em: {out_dir}")
    print("  consolidado.csv, latencia_por_fase.png, matriz_confusao.png, economia_por_cenario.png")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
