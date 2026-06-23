"""
tools/sanitize.py
──────────────────
Input sanitization layer — called before any CSV field enters an agent prompt.

"""

from __future__ import annotations

import hashlib
import re
from typing import Optional

import pandas as pd

# ── Constants ─────────────────────────────────────────────────────────────────

# Allowlist for vehicle IDs: alphanumeric, dash, underscore, dot — max 50 chars
_VEHICLE_ID_RE = re.compile(r'^[A-Za-z0-9\-_.]{1,50}$')

# Internal fuel type names used by the CO2 model
VALID_FUEL_TYPES = {"Gasolina", "Diesel", "Etanol"}
DEFAULT_FUEL_TYPE = "Gasolina"

# Mapping from lab CSV fuel_model_prediction values → internal fuel type
FUEL_PREDICTION_MAP: dict[str, str] = {
    "gasoline": "Gasolina",
    "gasolina": "Gasolina",
    "gas":      "Gasolina",
    "petrol":   "Gasolina",
    "ethanol":  "Etanol",
    "etanol":   "Etanol",
    "flex":     "Etanol",   # flex-fuel classified as ethanol by the lab
    "diesel":   "Diesel",
}


# ── Sanitizers ────────────────────────────────────────────────────────────────

def sanitize_vehicle_id(raw: str) -> str:
    """
    Validate and return a safe vehicle ID string.
    Raises ValueError on invalid format.
    """
    if not isinstance(raw, str):
        raise ValueError("vehicle_id must be a string")
    clean = raw.strip()
    if not _VEHICLE_ID_RE.match(clean):
        raise ValueError(
            f"vehicle_id '{clean[:20]}' is invalid. "
            "Allowed: alphanumeric, dash, underscore, dot. Max 50 chars."
        )
    return clean


def resolve_fuel_type(df: pd.DataFrame) -> str:
    """
    Determine the session fuel type from the DataFrame using the following priority:

    1. fuel_model_prediction column (lab format) — majority class wins.
       Maps: 'gasoline' → 'Gasolina', 'ethanol' → 'Etanol', 'diesel' → 'Diesel'
    2. fuel_type column (legacy format) — direct value, validated against allowlist.
    3. Default: 'Gasolina'

    Returns a validated internal fuel type string.
    """
    # Priority 1: lab column with ML predictions
    if "fuel_model_prediction" in df.columns:
        predictions = (
            df["fuel_model_prediction"]
            .dropna()
            .astype(str)
            .str.strip()
            .str.lower()
        )
        if not predictions.empty:
            # Use majority class (most frequent prediction wins)
            majority = predictions.value_counts().idxmax()
            mapped = FUEL_PREDICTION_MAP.get(majority)
            if mapped:
                return mapped
            # Unknown prediction class — fall through to next priority

    # Priority 2: legacy fuel_type column
    if "fuel_type" in df.columns:
        raw = str(df["fuel_type"].dropna().iloc[0]).strip() if not df["fuel_type"].dropna().empty else ""
        if raw in VALID_FUEL_TYPES:
            return raw

    # Priority 3: safe default
    return DEFAULT_FUEL_TYPE


def sanitize_agent_text(raw: str, max_len: int = 500) -> str:
    """
    Sanitize a free-text field before it enters an agent prompt or goes on-chain.
    - Strips leading/trailing whitespace
    - Replaces newlines with spaces (prevents multi-line injection)
    - Truncates to max_len characters
    """
    if not isinstance(raw, str):
        return ""
    clean = raw.strip().replace("\n", " ").replace("\r", " ")
    # Remove any instruction-like patterns that could hijack the LLM
    clean = re.sub(r'(ignore\s+(previous|above|all)\s+instructions?)', '[REDACTED]', clean, flags=re.IGNORECASE)
    clean = re.sub(r'(system\s*prompt|you\s+are\s+now)', '[REDACTED]', clean, flags=re.IGNORECASE)
    return clean[:max_len]


def compute_csv_sha256(csv_bytes: bytes) -> bytes:
    """
    Compute SHA-256 hash of raw CSV bytes.
    Returns 32 bytes suitable for Solidity bytes32 parameter.
    """
    return hashlib.sha256(csv_bytes).digest()


def verify_lab_envelope(envelope: dict, csv_bytes: bytes) -> tuple[str, bytes]:
    """
    Verify a lab JSON envelope and return (user_address, csv_hash).

    Expected envelope format:
    {
        "csv_b64":      "<base64-encoded CSV>",
        "sha256":       "<hex SHA-256 of the CSV bytes>",
        "user_address": "0x...",
        "device_id":    "LAB-001"   (optional)
    }

    Raises ValueError if the hash doesn't match the provided CSV bytes.
    Returns (user_address, hash_bytes) on success.
    """
    claimed_hex = envelope.get("sha256", "")
    user_address = envelope.get("user_address", "")

    if not claimed_hex:
        raise ValueError("Lab envelope missing 'sha256' field")
    if not user_address or not user_address.startswith("0x") or len(user_address) != 42:
        raise ValueError("Lab envelope missing or invalid 'user_address'")

    actual_hash = hashlib.sha256(csv_bytes).hexdigest()
    if actual_hash.lower() != claimed_hex.lower():
        raise ValueError(
            f"CSV integrity check failed: claimed SHA-256 does not match file content. "
            f"Expected {claimed_hex[:16]}…, got {actual_hash[:16]}…"
        )

    return user_address, bytes.fromhex(actual_hash)
