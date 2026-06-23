"""
CO2 emission calculator based on OBD-II physics.
"""

from __future__ import annotations

import json
import pandas as pd
import numpy as np
from typing import Dict
from langchain_core.tools import tool

from tools.sanitize import resolve_fuel_type
import config

# ── Constants ─────────────────────────────────────────────────────────────────

# [density_kg_m3, emission_factor_gCO2_per_liter_fuel]
DENSITY_AND_EMISSION: Dict[str, list] = {
    "Gasolina": [737,  2310],
    "Diesel":   [850,  2660],
    "Etanol":   [789,  1510],
}

AFR: Dict[str, float] = {
    "Gasolina": 14.7,
    "Diesel":   14.7,
    "Etanol":    9.1,
}

# ── Physics functions ─────────────────────────────────────────────────────────

def estimate_maf(rpm: float, temp: float, pressure: float, engine_cc: float) -> float:
    """
    Estimate MAF (g/s) via the speed-density equation when the sensor is absent/zero.

        MAF = (MAP[kPa] × displacement[L] × M_air[g/mol] × VE × RPM) / (R × IAT[K] × 120)

    Note the displacement term is in LITERS. `engine_cc` is supplied in cubic
    centimetres (e.g. 2000 cc = 2.0 L), so we divide by 1000 before applying the
    formula. Omitting this conversion overestimates MAF — and therefore CO₂ — by
    ~1000× on every row that falls back to estimation.
    """
    VE = 0.8; R = 8.3146; M_air = 28.87
    engine_litres = engine_cc / 1000.0
    temp_k = temp + 273.15
    return (pressure * engine_litres * M_air * VE * rpm) / (R * temp_k * 120)


def calculate_co2_physics(df: pd.DataFrame, engine_cc: float) -> tuple[float, str, float]:
    """
    Calculate total CO2 emissions (mg) and distance (km) for a driving session.

    Assumptions:
      - Each row represents ~1 second of OBD-II telemetry.
      - mass_air_flow is in g/s. When absent, estimated via Ideal Gas Law.
      - speed column is in km/h; distance = sum(speed) / 3600.

    Returns:
        (total_co2_mg, fuel_type_used, distance_km)
    """
    fuel_type = resolve_fuel_type(df)
    fuel_density, emission_factor_CO2 = DENSITY_AND_EMISSION[fuel_type]
    afr = AFR[fuel_type]

    co2_emissions: list[float] = []
    for _, row in df.iterrows():
        maf = row.get("mass_air_flow", None)
        if maf is None or (isinstance(maf, float) and pd.isna(maf)) or maf == 0:
            rpm      = row.get("rpm", 0)
            temp     = row.get("intake_air_temperature", 25)
            pressure = row.get("intake_manifold_absolut_pressure", 100)
            maf = estimate_maf(rpm, temp, pressure, engine_cc) if rpm > 0 else 0.0

        if maf > 0:
            co2_emissions.append(maf / (afr * fuel_density) * emission_factor_CO2)
        else:
            co2_emissions.append(0.0)

    # Distance: sum of speed (km/h) × 1 s per row ÷ 3600 s/h.
    # Implausible readings (GPS spoofing, e.g. 350 km/h) are clipped to the max
    # plausible speed so a fraudster cannot inflate distance — and therefore the
    # distance-proportional governance baseline — to manufacture fake credits.
    if "speed" in df.columns:
        speed = df["speed"].fillna(0).clip(lower=0, upper=config.MAX_PLAUSIBLE_SPEED_KMH)
        distance_km = round(float(speed.sum() / 3600.0), 3)
    else:
        distance_km = 0.0

    return round(float(np.sum(co2_emissions)) * 1000, 2), fuel_type, distance_km  # g → mg


def check_physical_plausibility(df: pd.DataFrame) -> dict:
    """
    Deterministic, history-independent sanity checks on raw telemetry.

    Catches frauds that purely statistical validation misses, especially when a
    vehicle has no history yet (the Z-score can't run with n<MIN_HISTORY):
      - impossible_speed : any speed > MAX_PLAUSIBLE_SPEED_KMH (GPS spoofing)
      - engine_off_motion: speed > ENGINE_OFF_SPEED_KMH while rpm == 0 (towed/simulated)
      - temperature_hack : intake_air_temperature outside [MIN, MAX]_PLAUSIBLE_TEMP_C
      - robotic_data     : long trace with near-zero rpm variance (machine-generated)

    Returns:
        {"plausible": bool, "anomaly_type": str, "details": str}
        anomaly_type is "none" when every check passes.
    """
    # Impossible speed
    if "speed" in df.columns:
        speed = df["speed"].dropna()
        if not speed.empty and float(speed.max()) > config.MAX_PLAUSIBLE_SPEED_KMH:
            return {
                "plausible": False,
                "anomaly_type": "impossible_speed",
                "details": f"max speed {float(speed.max()):.1f} km/h exceeds "
                           f"{config.MAX_PLAUSIBLE_SPEED_KMH:.0f} km/h limit",
            }

    # Motion with engine off
    if "speed" in df.columns and "rpm" in df.columns:
        moving_off = df[(df["speed"].fillna(0) > config.ENGINE_OFF_SPEED_KMH)
                        & (df["rpm"].fillna(0) == 0)]
        if not moving_off.empty:
            return {
                "plausible": False,
                "anomaly_type": "engine_off_motion",
                "details": f"{len(moving_off)} row(s) show motion above "
                           f"{config.ENGINE_OFF_SPEED_KMH:.0f} km/h with rpm == 0",
            }

    # Hacked temperature sensor
    if "intake_air_temperature" in df.columns:
        temp = df["intake_air_temperature"].dropna()
        if not temp.empty and (float(temp.min()) < config.MIN_PLAUSIBLE_TEMP_C
                               or float(temp.max()) > config.MAX_PLAUSIBLE_TEMP_C):
            return {
                "plausible": False,
                "anomaly_type": "temperature_hack",
                "details": f"intake_air_temperature out of range "
                           f"[{config.MIN_PLAUSIBLE_TEMP_C:.0f}, {config.MAX_PLAUSIBLE_TEMP_C:.0f}] °C "
                           f"(observed {float(temp.min()):.1f}…{float(temp.max()):.1f})",
            }

    # Robotic (zero-variance) data
    if "rpm" in df.columns:
        rpm = df["rpm"].dropna()
        if len(rpm) >= config.ROBOTIC_MIN_ROWS and float(rpm.std()) < config.ROBOTIC_RPM_STD_MIN:
            return {
                "plausible": False,
                "anomaly_type": "robotic_data",
                "details": f"rpm std {float(rpm.std()):.3f} over {len(rpm)} rows is below "
                           f"{config.ROBOTIC_RPM_STD_MIN} — trace appears machine-generated",
            }

    return {"plausible": True, "anomaly_type": "none", "details": ""}


# ── LangChain Tools ───────────────────────────────────────────────────────────

@tool
def calculate_co2_from_dataframe(csv_path: str, engine_cc: float = 2000.0) -> str:
    """
    Calculate total CO2 emissions from an OBD-II CSV file.
    Handles fuel_model_prediction column (lab format) and legacy fuel_type column.

    Returns JSON with: status, total_co2_mg, fuel_type, rows_processed.
    """
    try:
        df = pd.read_csv(csv_path)
        total_co2, fuel_type, distance_km = calculate_co2_physics(df, engine_cc)
        maf_estimated = (
            "mass_air_flow" not in df.columns
            or df["mass_air_flow"].isna().all()
            or (df["mass_air_flow"] == 0).all()
        )
        return json.dumps({
            "status":         "success",
            "total_co2_mg":   total_co2,
            "total_co2_g":    round(total_co2 / 1000, 2),
            "fuel_type":      fuel_type,
            "distance_km":    distance_km,
            "rows_processed": len(df),
            "maf_estimated":  maf_estimated,
        })
    except Exception as exc:
        return json.dumps({"status": "error", "message": str(exc)})


@tool
def get_emission_benchmarks() -> str:
    """Return CO2 emission benchmarks and fuel constants used by the physics model."""
    return json.dumps({
        "fuel_constants": {
            fuel: {
                "density_kg_m3":             vals[0],
                "emission_factor_gCO2_per_L": vals[1],
                "air_fuel_ratio":             AFR[fuel],
            }
            for fuel, vals in DENSITY_AND_EMISSION.items()
        },
        "session_benchmarks_mg": {
            "typical_min":  50_000,
            "typical_max": 500_000,
            "alert_above": 1_000_000,
            "alert_below":    500,
        },
        "note": "Values are per OBD session, not per kilometre.",
    })
