"""
main.py v2 — CLI entrypoint for Carbon MAS.

Usage:
  # Full pipeline (all 4 agents + blockchain)
  python main.py --csv data/session.csv --vehicle VIN123 --recipient 0xABC...

  # Dry-run (agents 1-3 only, no blockchain)
  python main.py --csv data/session.csv --vehicle VIN123 --recipient 0xABC... --dry-run

  # Save full JSON report
  python main.py --csv data/session.csv --vehicle VIN123 --recipient 0xABC... --output report.json

  # Generate sample CSV
  python main.py --generate-sample

  # Query portfolio
  python main.py --portfolio 0xYOUR_ADDRESS
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich import box

console = Console()


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="carbon-mas",
        description="Carbon MAS v2 — Agentic Oracle for Vehicular Carbon Microcredits",
    )
    p.add_argument("--csv",               metavar="PATH",    help="OBD-II CSV file path")
    p.add_argument("--vehicle",           metavar="ID",      help="Vehicle identifier")
    p.add_argument("--recipient",         metavar="ADDRESS", help="Ethereum wallet for CCT credits")
    p.add_argument("--engine-cc",         metavar="CC",      type=float, default=None)
    p.add_argument("--output",            metavar="PATH",    help="Save full JSON report to file")
    p.add_argument("--dry-run",           action="store_true", help="Skip blockchain writes")
    p.add_argument("--generate-sample",   action="store_true", help="Write data/sample_obd.csv and exit")
    p.add_argument("--portfolio",         metavar="ADDRESS", help="Show CCT portfolio for a wallet")
    return p


def generate_sample_csv() -> None:
    import pandas as pd
    import numpy as np

    Path("data").mkdir(exist_ok=True)
    n   = 120
    rng = np.random.default_rng(42)
    df  = pd.DataFrame({
        "rpm":                              rng.integers(800, 3500, n).astype(float),
        "mass_air_flow":                    rng.uniform(5, 30, n),
        "intake_air_temperature":           rng.uniform(20, 45, n),
        "intake_manifold_absolut_pressure": rng.uniform(50, 100, n),
        "vehicle_speed":                    rng.integers(0, 120, n).astype(float),
        "engine_load":                      rng.uniform(10, 80, n),
        "coolant_temperature":              rng.uniform(75, 95, n),
        # Lab-format fuel prediction column (majority = gasoline)
        "fuel_model_prediction": (
            ["gasoline"] * 100 + ["ethanol"] * 20
        ),
    })
    # ~10% NaN MAF to exercise fallback path
    null_idx = rng.choice(n, size=12, replace=False)
    df.loc[null_idx, "mass_air_flow"] = float("nan")
    # Shuffle fuel predictions
    df["fuel_model_prediction"] = rng.permutation(df["fuel_model_prediction"].values)

    out = Path("data/sample_obd.csv")
    df.to_csv(out, index=False)
    console.print(f"\n[green]✓ Sample CSV → {out}[/green]")
    console.print(f"  {n} rows | ~10% NaN MAF | fuel_model_prediction: gasoline×100 ethanol×20")
    console.print("\nRun with:")
    console.print(f"  [bold]python main.py --csv {out} --vehicle TEST-001 --recipient 0xYOUR_ADDR[/bold]\n")


def show_portfolio(address: str) -> None:
    import json as _json
    from tools.blockchain_tools import get_credit_balance, get_issuance_history
    import config

    console.print(f"\n[bold cyan]CCT Portfolio:[/bold cyan] {address}")

    bal = _json.loads(get_credit_balance.invoke({"address": address}))
    if bal.get("status") == "error":
        console.print(f"[red]Error:[/red] {bal.get('message')}")
        return

    history = _json.loads(get_issuance_history.invoke({"address": address, "limit": 20}))

    t = Table(box=box.SIMPLE_HEAD, header_style="bold cyan")
    t.add_column("Field",   style="dim")
    t.add_column("Value",   justify="right")
    nft_count    = bal.get("nft_count", 0)
    total_credits = history.get("total_credits_cct", 0.0)
    eur_value = total_credits * config.CCT_EUR_RATE
    brl_value = eur_value * config.EUR_BRL_RATE
    total_co2_g = history.get("total_co2_saved_mg", 0) / 1000
    t.add_row("Certificates (NFTs)",  str(nft_count))
    t.add_row("Total credits (CCT)",  f"{total_credits:.6f}")
    t.add_row("Total CO₂ saved",      f"{total_co2_g:,.2f} g")
    t.add_row("EUR rate",             f"€{config.CCT_EUR_RATE:.4f}/CCT")
    t.add_row("EUR value",            f"€{eur_value:.4f}")
    t.add_row("EUR→BRL rate",         f"R${config.EUR_BRL_RATE:.4f}/€")
    t.add_row("BRL value",            f"R${brl_value:.4f}")
    console.print(t)

    certificates = history.get("certificates", [])
    if certificates:
        console.print("\n[bold]Certificates:[/bold]")
        for cert in certificates[:5]:
            console.print(
                f"  NFT #{cert['token_id']} | Record #{cert['emission_record_id']} | "
                f"{cert['co2_saved_mg'] / 1000:,.2f} g saved | "
                f"{cert['credits_equivalent_cct']:.4f} CCT | "
                f"{cert['reason'][:50]}…"
            )


def print_summary(report: dict) -> None:
    s = report.get("summary", {})
    p = report.get("phases", {})

    status = report.get("pipeline_status", "?")
    colors = {"success": "green", "partial": "yellow", "failed": "red",
              "aborted": "red", "dry_run": "yellow"}
    color  = colors.get(status, "white")

    console.print(Panel(
        f"[{color}]{status.upper()}[/{color}]  │  "
        f"Vehicle: [bold]{report.get('vehicle_id')}[/bold]  │  "
        f"SHA-256: {report.get('csv_sha256', '')[:12]}…",
        title="Carbon MAS v2 — Session Report", border_style=color,
    ))

    t = Table(box=box.SIMPLE_HEAD, header_style="bold cyan")
    t.add_column("Field", style="dim")
    t.add_column("Value", justify="right")
    t.add_row("Total CO₂",        f"{s.get('total_co2_mg', 0) / 1000:,.2f} g")
    t.add_row("Fuel type",         str(s.get("fuel_type", "—")))
    t.add_row("Distance",          f"{s.get('distance_km', 0):,.2f} km")
    t.add_row("Data quality",      str(s.get("data_quality", "—")))
    t.add_row("MAF estimated",     "Yes" if s.get("maf_estimated") else "No")
    t.add_row("Validation method", str(s.get("validation_method", "—")))
    t.add_row("Validated",         "✓ Yes" if s.get("validated") else "✗ No")
    t.add_row("Confidence",        f"{s.get('confidence', '?')}/100")
    t.add_row("Anomaly",           str(s.get("anomaly", "none")))
    t.add_row("Credits (CCT equiv)", f"{s.get('credits_cct', 0):.6f} CCT")
    t.add_row("CO₂ saved",         f"{s.get('co2_saved_mg', 0) / 1000:,.2f} g")
    t.add_row("Governance",         str(s.get("governance_decision", "—")))
    t.add_row("Certificate NFT #",  str(s.get("token_id", "—")))
    t.add_row("Emission record #",  str(s.get("emission_record_id", "—")))
    t.add_row("Data hash",          str(s.get("data_hash", ""))[:16] + "…")
    console.print(t)

    if s.get("emission_tx"):
        console.print("[bold]On-chain transactions:[/bold]")
        console.print(f"  Emission    : {s.get('emission_tx')}")
        if s.get("governance_tx"):
            console.print(f"  Governance  : {s.get('governance_tx')}")
        if s.get("certificate_tx"):
            console.print(f"  Certificate : {s.get('certificate_tx')}")

    audit = p.get("blockchain", {}).get("audit_summary", "")
    if audit:
        console.print(Panel(audit, title="Audit Summary", border_style="dim"))


def main():
    parser = build_parser()
    args   = parser.parse_args()

    if args.generate_sample:
        generate_sample_csv()
        sys.exit(0)

    if args.portfolio:
        show_portfolio(args.portfolio)
        sys.exit(0)

    missing = [(f, v) for f, v in [
        ("--csv", args.csv), ("--vehicle", args.vehicle), ("--recipient", args.recipient)
    ] if not v]
    if missing:
        console.print(f"[red]Missing: {', '.join(f for f, _ in missing)}[/red]")
        parser.print_help()
        sys.exit(1)

    if not Path(args.csv).exists():
        console.print(f"[red]File not found: {args.csv}[/red]")
        sys.exit(1)

    console.print(f"\n[bold cyan]Carbon MAS v2[/bold cyan]")
    console.print(f"  CSV      : {args.csv}")
    console.print(f"  Vehicle  : {args.vehicle}")
    console.print(f"  Recipient: {args.recipient}")
    if args.dry_run:
        console.print("  Mode     : [yellow]DRY RUN[/yellow] (no blockchain writes)")

    try:
        from session_store import init_db
        init_db()

        csv_bytes = Path(args.csv).read_bytes()

        from orchestrator import CarbonMASOrchestrator
        orch   = CarbonMASOrchestrator()
        report = orch.run(
            csv_bytes=csv_bytes,
            vehicle_id=args.vehicle,
            recipient_address=args.recipient,
            engine_cc=args.engine_cc,
            dry_run=args.dry_run,
        )

        print_summary(report)

        if args.output:
            Path(args.output).write_text(json.dumps(report, indent=2, default=str))
            console.print(f"\n[green]Report saved → {args.output}[/green]")

    except KeyboardInterrupt:
        console.print("\n[yellow]Interrupted.[/yellow]")
        sys.exit(0)
    except Exception as exc:
        console.print(f"\n[red]Fatal error: {exc}[/red]")
        raise


if __name__ == "__main__":
    main()
