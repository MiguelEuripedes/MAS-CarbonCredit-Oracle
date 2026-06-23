"""
config.py — Central configuration for Carbon MAS v2.
All values read from .env file.
"""
import os
from pathlib import Path
from dotenv import load_dotenv

BASE_DIR = Path(__file__).parent
load_dotenv(BASE_DIR / ".env")

# ── Hyperledger Besu ──────────────────────────────────────────────────────────
BESU_RPC_URL:  str = os.getenv("BESU_RPC_URL",  "http://localhost:8545")
BESU_CHAIN_ID: int = int(os.getenv("BESU_CHAIN_ID", "1337"))

# Account 1 — owner/deployer only (no operational signing)
OWNER_PRIVATE_KEY: str = os.getenv("OWNER_PRIVATE_KEY", "")
OWNER_ADDRESS:     str = os.getenv("OWNER_ADDRESS",     "")

# Account 2 — emission logger + validator
EMISSION_PRIVATE_KEY: str = os.getenv("EMISSION_PRIVATE_KEY", "")
EMISSION_ADDRESS:     str = os.getenv("EMISSION_ADDRESS",     "")

# Account 3 — governance/minter (updateGovernanceStatus + issueCredit)
GOVERNANCE_PRIVATE_KEY: str = os.getenv("GOVERNANCE_PRIVATE_KEY", "")
GOVERNANCE_ADDRESS:     str = os.getenv("GOVERNANCE_ADDRESS",     "")

# Contract addresses (filled by deploy_contracts.py)
EMISSIONS_REGISTRY_ADDRESS: str = os.getenv("EMISSIONS_REGISTRY_ADDRESS", "")
CARBON_CREDIT_ADDRESS:      str = os.getenv("CARBON_CREDIT_ADDRESS",      "")

# ── Ollama ────────────────────────────────────────────────────────────────────
OLLAMA_BASE_URL: str = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
OLLAMA_MODEL:    str = os.getenv("OLLAMA_MODEL",    "llama3.1:8b")
# Max output tokens per LLM response. Ollama defaults to 128, which truncates
# longer agent rationales — raise via .env if explanations are getting cut off.
OLLAMA_NUM_PREDICT: int = int(os.getenv("OLLAMA_NUM_PREDICT", "1024"))
PIPELINE_VERSION: str = "2.0.0"

# ── Vehicle defaults ──────────────────────────────────────────────────────────
DEFAULT_ENGINE_CC: float = float(os.getenv("DEFAULT_ENGINE_CC", "2000"))

# ── Carbon credit policy (deterministic rule engine) ──────────────────────────
# Grams of CO2 saved = 1 CCT token (before 18-decimal wei scaling)
CO2_PER_CREDIT_GRAM: float = float(os.getenv("CO2_PER_CREDIT_GRAM", "1000"))
# Per-km baseline: 175 g CO2/km — gasoline ICEV light-duty factor per Programa MOVER
# (Brazil), consistent with the IPCC light-duty average.
# Credits are issued when actual emissions < (baseline × distance).
BASELINE_CO2_G_PER_KM: float = float(os.getenv("BASELINE_CO2_G_PER_KM", "175"))
# Fixed fallback (mg) used only when the session has no speed data (distance_km == 0)
BASELINE_CO2_MG: float = float(os.getenv("BASELINE_CO2_MG", "200000"))

# ── CCT pricing ───────────────────────────────────────────────────────────────
# Fixed EUR price per CCT. 1 CCT = 1 kg CO2 saved, so this is (exchange €/tonne ÷ 1000).
# Snapshot taken manually from the European carbon exchange — record the date in the
# dissertation for reproducibility.
CCT_EUR_RATE: float = float(os.getenv("CCT_EUR_RATE", "0.065"))
# Fixed EUR → BRL exchange rate (manual snapshot). Used to express credit value in R$.
EUR_BRL_RATE: float = float(os.getenv("EUR_BRL_RATE", "6.20"))

# ── Rate limiting ─────────────────────────────────────────────────────────────
# Max submissions per vehicle per hour
RATE_LIMIT_PER_VEHICLE_PER_HOUR: int = int(os.getenv("RATE_LIMIT_PER_VEHICLE_PER_HOUR", "5"))
# Minimum seconds between sessions for the same vehicle
MIN_SESSION_GAP_SECONDS: int = int(os.getenv("MIN_SESSION_GAP_SECONDS", "300"))  # 5 min

# ── Statistical validation ────────────────────────────────────────────────────
# Min sessions in history before Z-score validation is used (below = range check)
STAT_VALIDATION_MIN_HISTORY: int = int(os.getenv("STAT_VALIDATION_MIN_HISTORY", "5"))
# Z-score threshold for anomaly flagging
STAT_VALIDATION_Z_THRESHOLD: float = float(os.getenv("STAT_VALIDATION_Z_THRESHOLD", "3.0"))

# ── Physical plausibility checks (deterministic, history-independent) ──────────
# These guard against frauds that statistics alone miss: impossible speeds,
# motion with the engine off, hacked temperature sensors, and robotic (zero-
# variance) data. They run BEFORE the Z-score / range check and reject outright.
# Max plausible vehicle speed (km/h). Readings above this are GPS spoofing.
MAX_PLAUSIBLE_SPEED_KMH: float = float(os.getenv("MAX_PLAUSIBLE_SPEED_KMH", "200"))
# Speed (km/h) above which rpm == 0 is physically impossible (engine-off motion).
ENGINE_OFF_SPEED_KMH: float = float(os.getenv("ENGINE_OFF_SPEED_KMH", "5"))
# Plausible intake-air-temperature range (°C). Outside this = hacked sensor.
MIN_PLAUSIBLE_TEMP_C: float = float(os.getenv("MIN_PLAUSIBLE_TEMP_C", "-40"))
MAX_PLAUSIBLE_TEMP_C: float = float(os.getenv("MAX_PLAUSIBLE_TEMP_C", "80"))
# Robotic-data detection: if rpm has more rows than this and its std is below
# the threshold, the trace was machine-generated (a human foot can't hold rpm flat).
ROBOTIC_MIN_ROWS: int = int(os.getenv("ROBOTIC_MIN_ROWS", "20"))
ROBOTIC_RPM_STD_MIN: float = float(os.getenv("ROBOTIC_RPM_STD_MIN", "1.0"))

# ── Paths ─────────────────────────────────────────────────────────────────────
CONTRACTS_DIR: Path = BASE_DIR / "contracts"
COMPILED_DIR:  Path = BASE_DIR / "compiled"
COMPILED_DIR.mkdir(exist_ok=True)
DB_PATH: Path = BASE_DIR / "carbon_mas.db"   # SQLite for jobs + session history
