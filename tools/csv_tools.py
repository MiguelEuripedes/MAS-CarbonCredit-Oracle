"""
CSV loading and validation tools for OBD-II vehicle data.

"""

from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path

import pandas as pd
from langchain_core.tools import tool

from tools.sanitize import resolve_fuel_type, VALID_FUEL_TYPES

PRIMARY_COLS  = ["mass_air_flow"]
FALLBACK_COLS = ["rpm", "intake_air_temperature", "intake_manifold_absolut_pressure"]


# ── Core CSV loader (used by agents and API) ──────────────────────────────────

def load_csv_bytes(csv_bytes: bytes) -> tuple[pd.DataFrame, str, bytes]:
    """
    Load CSV from raw bytes, compute SHA-256, and return (df, fuel_type, sha256_bytes).
    This is the primary entry point for the API — receives file content, not a path.
    """
    sha256_hash = hashlib.sha256(csv_bytes).digest()  # 32 bytes → bytes32 on-chain
    df = pd.read_csv(io.BytesIO(csv_bytes))
    fuel_type = resolve_fuel_type(df)
    return df, fuel_type, sha256_hash


def validate_dataframe(df: pd.DataFrame) -> dict:
    """
    Validate a DataFrame for CO2 calculation readiness.
    Returns a validation result dict (same schema as the @tool version below).
    """
    columns = list(df.columns)
    has_maf      = "mass_air_flow" in columns
    has_fallback = all(c in columns for c in FALLBACK_COLS)
    has_lab_fuel = "fuel_model_prediction" in columns
    has_leg_fuel = "fuel_type" in columns

    warnings: list[str] = []

    if not has_maf and not has_fallback:
        return {
            "status":  "invalid",
            "message": (
                "Missing required columns. Need 'mass_air_flow' OR all of: "
                + ", ".join(FALLBACK_COLS)
            ),
            "columns": columns,
        }

    if not has_maf:
        warnings.append("'mass_air_flow' absent — MAF will be estimated via Ideal Gas Law.")

    if has_lab_fuel:
        fuel_type = resolve_fuel_type(df)
        predictions = df["fuel_model_prediction"].dropna().astype(str).str.lower()
        counts = predictions.value_counts().to_dict()
        warnings.append(
            f"fuel_model_prediction: majority class = '{fuel_type}' "
            f"from counts {counts}"
        )
    elif has_leg_fuel:
        raw = str(df["fuel_type"].dropna().iloc[0]) if not df["fuel_type"].dropna().empty else ""
        if raw not in VALID_FUEL_TYPES:
            warnings.append(f"Unknown fuel_type '{raw}' — defaulting to 'Gasolina'.")
        fuel_type = raw if raw in VALID_FUEL_TYPES else "Gasolina"
    else:
        fuel_type = "Gasolina"
        warnings.append("No fuel column found — defaulting to 'Gasolina'.")

    relevant = [c for c in PRIMARY_COLS + FALLBACK_COLS if c in columns]
    null_pct = {col: round(df[col].isna().mean() * 100, 1) for col in relevant}
    for col, pct in null_pct.items():
        if pct > 50:
            warnings.append(f"Column '{col}' is {pct}% null.")

    return {
        "status":       "valid_with_warnings" if warnings else "valid",
        "rows":         len(df),
        "columns":      columns,
        "has_maf":      has_maf,
        "has_fallback": has_fallback,
        "fuel_type":    fuel_type,
        "null_pct":     null_pct,
        "warnings":     warnings,
        "preview":      df.head(3).fillna("null").to_dict(orient="records"),
    }


# ── LangChain Tools ───────────────────────────────────────────────────────────

@tool
def load_and_validate_csv(csv_path: str) -> str:
    """
    Load an OBD-II CSV file and validate its structure for CO2 calculation.
    Handles both fuel_model_prediction (lab) and fuel_type (legacy) columns.
    Returns JSON validation result.
    """
    try:
        path = Path(csv_path)
        if not path.exists():
            return json.dumps({"status": "error", "message": f"File not found: {csv_path}"})
        if path.suffix.lower() != ".csv":
            return json.dumps({"status": "error", "message": "File must be a .csv"})

        csv_bytes = path.read_bytes()
        sha256_hex = hashlib.sha256(csv_bytes).hexdigest()
        df = pd.read_csv(csv_path)
        result = validate_dataframe(df)
        result["sha256"] = sha256_hex
        return json.dumps(result)
    except Exception as exc:
        return json.dumps({"status": "error", "message": str(exc)})


@tool
def get_csv_statistics(csv_path: str) -> str:
    """
    Compute descriptive statistics for key OBD-II sensor columns.
    Used by ValidatorAgent for statistical anomaly context.
    """
    try:
        df = pd.read_csv(csv_path)
        sensor_cols = [
            c for c in [
                "rpm", "mass_air_flow", "intake_air_temperature",
                "intake_manifold_absolut_pressure", "vehicle_speed",
                "engine_load", "coolant_temperature",
            ]
            if c in df.columns
        ]
        stats = (
            df[sensor_cols].describe().loc[["min", "max", "mean", "std"]].round(2).to_dict()
        )
        return json.dumps({"status": "success", "statistics": stats, "rows": len(df)})
    except Exception as exc:
        return json.dumps({"status": "error", "message": str(exc)})
