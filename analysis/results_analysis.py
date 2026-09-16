"""
results_analysis.py — Multi-run consolidation of the agentic oracle experiments.

Automatically discovers every run in results/ (folders that contain a
_manifest.json) and produces an aggregated analysis:

  1. Determinism  — is the decision (CO2, governance, credits) identical across
                    runs? (central property of the architecture)
  2. Latency      — mean ± std per phase, aggregated over all runs;
                    shows WHERE the end-to-end time is spent.
  3. Correctness  — confusion matrix (legitimate→approved / fraud→denied),
                    with tables of false positives and false negatives.
  4. Savings      — CO2 saved and credits per scenario (mean across runs).

Outputs (in results/results_analysis/):
  - consolidated.csv         : every session of every run
  - latency_by_phase.png
  - savings_by_scenario.png
  - confusion_matrix.png

Usage:
    python results_analysis.py                    # discovers the rodada_* run folders in results/
    python results_analysis.py --pattern "run_*"  # another folder pattern
    python results_analysis.py --results-dir results --pattern "rodada_*"
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import pandas as pd
import matplotlib
matplotlib.use("Agg")  # backend without a display, saves the PNGs directly
import matplotlib.pyplot as plt

# Expected label per scenario (numeric prefix of the file name).
EXPECTED = {
    "01": ("legitimate", "approved"),
    "02": ("legitimate", "approved"),
    "03": ("legitimate", "approved"),
    "04": ("legitimate", "approved"),
    "05": ("aggressive", "approved"),
    "06": ("fraud", "denied"),
    "07": ("fraud", "denied"),
    "08": ("fraud", "denied"),
    "09": ("fraud", "denied"),
    "10": ("empty", "error"),
}

LAT_COLS = ["lat_sensor_s", "lat_validator_s", "lat_governance_s", "lat_blockchain_s"]
PHASE_NAMES = ["sensor", "validator", "governance", "blockchain"]


def _expected(scenario: str) -> tuple[str, str]:
    key = scenario[:2] if scenario else ""
    return EXPECTED.get(key, ("unknown", "?"))


def load_runs(results_dir: Path, pattern: str) -> pd.DataFrame:
    """Loads every session of every run into a single DataFrame."""
    manifests = sorted(results_dir.glob(f"{pattern}/**/_manifest.json"))
    if not manifests:
        # fallback: any manifest under results/
        manifests = sorted(results_dir.glob("**/_manifest.json"))
    if not manifests:
        raise SystemExit(f"No _manifest.json found in {results_dir}")

    rows = []
    for mpath in manifests:
        data = json.loads(mpath.read_text(encoding="utf-8"))
        # run name = top-level folder inside results/
        try:
            run_label = mpath.relative_to(results_dir).parts[0]
        except ValueError:
            run_label = mpath.parent.name
        for s in data.get("sessions", []):
            cat, exp_dec = _expected(s.get("scenario", ""))
            rows.append({
                "run":        run_label,
                "vehicle":    s.get("vehicle"),
                "trip":       s.get("viagem"),
                "scenario":   s.get("scenario"),
                "category":   cat,
                "expected":   exp_dec,
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
    print(f"Loaded {len(manifests)} run(s): {sorted(df['run'].unique())}")
    print(f"Total sessions: {len(df)}")
    return df


def check_determinism(df: pd.DataFrame) -> None:
    """The decision must be identical across runs for the same scenario/vehicle."""
    print("\n=== 1. DETERMINISM (decision across runs) ===")
    ok = df[df["status"] == "ok"].copy()
    key = ["vehicle", "trip", "scenario"]
    grp = ok.groupby(key).agg(
        n_runs=("run", "nunique"),
        decisions=("decision", lambda x: set(x.dropna())),
        co2=("co2_g", lambda x: round(x.dropna().std() or 0, 6)),
        credits=("credits_cct", lambda x: round((x.dropna().std() or 0), 9)),
    )
    divergent = grp[(grp["decisions"].apply(len) > 1) | (grp["co2"] > 0) | (grp["credits"] > 0)]
    if divergent.empty:
        print("  [OK] Decision, CO2 and credits IDENTICAL in all runs (determinism confirmed).")
    else:
        print("  [X] Divergences found across runs:")
        print(divergent.to_string())


def latency_stats(df: pd.DataFrame, out_dir: Path) -> None:
    print("\n=== 2. LATENCY PER PHASE (s) — all runs ===")
    ok = df[df["status"] == "ok"]
    means = [ok[c].mean() for c in LAT_COLS]
    stds = [ok[c].std() for c in LAT_COLS]
    for name, m, sd in zip(PHASE_NAMES, means, stds):
        print(f"  {name:11}: {m:6.2f} ± {sd:5.2f}")
    total = sum(m for m in means if pd.notna(m))
    print(f"  {'TOTAL':11}: {total:6.2f}")
    print("  Share:")
    for name, m in zip(PHASE_NAMES, means):
        if pd.notna(m):
            print(f"    {name:11}: {100*m/total:5.1f}%")

    fig, ax = plt.subplots(figsize=(7, 4))
    ax.bar(PHASE_NAMES, means, yerr=stds, capsize=4, color="#4C72B0")
    ax.set_ylabel("Mean latency (s)")
    ax.set_title("Latency per pipeline phase (mean ± std)")
    for i, m in enumerate(means):
        if pd.notna(m):
            ax.text(i, m, f"{m:.1f}s", ha="center", va="bottom")
    fig.tight_layout()
    fig.savefig(out_dir / "latency_by_phase.png", dpi=150)
    plt.close(fig)


def correctness(df: pd.DataFrame, out_dir: Path) -> None:
    print("\n=== 3. CORRECTNESS (expected vs. observed) ===")
    # Uses the first run as the reference (decisions are deterministic).
    ref_run = sorted(df["run"].unique())[0]
    d = df[df["run"] == ref_run].copy()
    print(f"  (reference: {ref_run} — identical decisions in the other runs)")

    # Normalized observed result
    def observed(row):
        if row["status"] == "error":
            return "error"
        return row["decision"]
    d["observed"] = d.apply(observed, axis=1)

    # Hit: matches the expected result
    d["correct"] = d["observed"] == d["expected"]

    # Matrix per category
    tab = pd.crosstab(d["category"], d["observed"])
    print("\n  Observed decision per category:")
    print(tab.to_string())

    # False negatives: legitimate/aggressive that was denied
    fn = d[(d["category"].isin(["legitimate", "aggressive"])) & (d["observed"] == "denied")]
    if not fn.empty:
        print("\n  [!] False negatives (legitimate DENIED):")
        print(fn[["vehicle", "trip", "scenario", "co2_g", "anomaly"]].to_string(index=False))

    # False positives: fraud that was approved
    fp = d[(d["category"] == "fraud") & (d["observed"] == "approved")]
    if not fp.empty:
        print("\n  [!] False positives (fraud APPROVED):")
        print(fp[["vehicle", "trip", "scenario", "co2_g", "anomaly"]].to_string(index=False))
    else:
        print("\n  [OK] No fraud approved (all frauds correctly denied).")

    # Simple chart of the matrix
    fig, ax = plt.subplots(figsize=(6, 4))
    tab.plot(kind="bar", stacked=True, ax=ax, colormap="Set2")
    ax.set_ylabel("Number of sessions")
    ax.set_title(f"Decision per scenario category ({ref_run})")
    ax.set_xlabel("")
    fig.tight_layout()
    fig.savefig(out_dir / "confusion_matrix.png", dpi=150)
    plt.close(fig)


def savings(df: pd.DataFrame, out_dir: Path) -> None:
    print("\n=== 4. CO2 SAVINGS AND CREDITS (mean across runs) ===")
    ok = df[(df["status"] == "ok") & (df["decision"] == "approved")].copy()
    if ok.empty:
        print("  (no approved session)")
        return
    ok["label"] = ok["vehicle"] + " v" + ok["trip"].astype(str) + " / " + ok["scenario"].str[:2]
    g = ok.groupby("label").agg(
        saved_g=("saved_g", "mean"),
        credits=("credits_cct", "mean"),
    ).sort_values("saved_g", ascending=True)
    print(g.round(3).to_string())

    fig, ax = plt.subplots(figsize=(8, max(4, 0.3 * len(g))))
    ax.barh(g.index, g["saved_g"], color="#55A868")
    ax.set_xlabel("CO2 saved (g) — mean across runs")
    ax.set_title("Savings per approved session")
    fig.tight_layout()
    fig.savefig(out_dir / "savings_by_scenario.png", dpi=150)
    plt.close(fig)


def main() -> int:
    ap = argparse.ArgumentParser(description="Consolidates the runs of the experiments.")
    ap.add_argument("--results-dir", default="results")
    ap.add_argument("--pattern", default="rodada_*",
                    help="Pattern of the run folders (default: rodada_*)")
    args = ap.parse_args()

    results_dir = Path(args.results_dir).resolve()
    out_dir = results_dir / "results_analysis"
    out_dir.mkdir(parents=True, exist_ok=True)

    df = load_runs(results_dir, args.pattern)
    df.to_csv(out_dir / "consolidated.csv", index=False, encoding="utf-8")

    check_determinism(df)
    latency_stats(df, out_dir)
    correctness(df, out_dir)
    savings(df, out_dir)

    print(f"\nArtifacts saved to: {out_dir}")
    print("  consolidated.csv, latency_by_phase.png, confusion_matrix.png, savings_by_scenario.png")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
