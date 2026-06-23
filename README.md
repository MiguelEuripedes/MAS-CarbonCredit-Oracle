# Carbon MAS Oracle
## An Agentic Oracle Architecture for the Tokenization of Vehicular Carbon Microcredits

> Master's Dissertation Prototype · Hyperledger Besu · LangChain · Ollama · Solidity

---

## Table of Contents

1. [What This System Does](#1-what-this-system-does)
2. [Project Structure](#2-project-structure)
3. [File-by-File Reference](#3-file-by-file-reference)
4. [How the Pipeline Works](#4-how-the-pipeline-works)
5. [Setup: Step by Step](#5-setup-step-by-step)
6. [Running Without Blockchain (Dry-Run)](#6-running-without-blockchain-dry-run)
7. [Running With Blockchain](#7-running-with-blockchain)
8. [The API Server](#8-the-api-server)
9. [Testing the Smart Contracts (Hardhat)](#9-testing-the-smart-contracts-hardhat)
9b. [Batch System Testing & Result Analysis](#9b-batch-system-testing--result-analysis)
10. [The CSV Format](#10-the-csv-format)
11. [The 3-Account Role Model](#11-the-3-account-role-model)
12. [Security Improvements in v2](#12-security-improvements-in-v2)
13. [Troubleshooting](#13-troubleshooting)

---

## 1. What This System Does

A vehicle records a driving session via an OBD-II adapter. The raw sensor data
(RPM, MAF airflow, temperature, pressure) is saved as a CSV file. This system:

1. **Calculates CO₂ emissions** using a physics-based model (Ideal Gas Law + fuel factors)
2. **Validates the reading** with physical-plausibility checks plus a statistical Z-score test against the vehicle's own history
3. **Decides whether to issue carbon credits** using a deterministic rule engine with a distance-based baseline (175 g CO₂/km)
4. **Writes everything to a blockchain** — permanently, with a cryptographic link to the original data
5. **Mints one ERC-721 certificate NFT** (`CarbonCertificate`) to the vehicle owner's wallet if the session beats the CO₂ baseline

The LLM agents (powered by Ollama locally) do **not** make any numerical decisions.
They read the deterministic outputs and write plain-English explanations that are
stored on-chain as the human-readable audit trail. This is the core design principle: **deterministic layer decides, LLM layer explains**.

---

## 2. Project Structure

```
carbon-mas-v2/
│
├── contracts/                    ← Solidity smart contracts
│   ├── EmissionsRegistry.sol     ← Immutable CO₂ ledger (3-role access control)
│   └── CarbonCredit.sol          ← ERC-721 carbon session certificate NFT
│
├── compiled/                     ← Auto-generated ABIs (created by deploy_contracts.py)
│
├── tools/                        ← Deterministic tool functions (no LLM)
│   ├── __init__.py
│   ├── co2_tools.py              ← Physics CO₂ engine + physical-plausibility checks
│   ├── csv_tools.py              ← CSV loader, SHA-256, validation
│   ├── sanitize.py               ← Input sanitization (prompt injection defence)
│   └── blockchain_tools.py       ← Web3 calls to Besu (with retry logic)
│
├── agents/                       ← LLM agent layer (explanation only)
│   ├── __init__.py
│   ├── base.py                   ← Ollama LLM factory + pipeline metadata
│   ├── sensor_agent.py           ← Phase 1: physics + plausibility + data quality
│   ├── validator_agent.py        ← Phase 2: plausibility + statistical validation
│   ├── governance_agent.py       ← Phase 3: credit rationale writing
│   └── blockchain_agent.py       ← Phase 4: audit summary writing + tx execution
│
├── hardhat/                      ← Smart contract test suite
│   ├── package.json
│   ├── hardhat.config.js
│   └── test/
│       ├── EmissionsRegistry.test.js   ← 37 tests for the ledger contract
│       └── CarbonCredit.test.js        ← 57 tests for the ERC-721 certificate
│
├── orchestrator.py               ← Coordinates all 4 agents in sequence
├── session_store.py              ← SQLite: job history + vehicle session history
├── deploy_contracts.py           ← Compiles + deploys contracts to Besu
├── api.py                        ← FastAPI server (async job queue + REST, incl. POST /verify)
├── main.py                       ← CLI entrypoint
├── run_system_tests.py           ← Batch-runs the pipeline over a data folder → JSON reports
├── config.py                     ← All settings from .env
│
├── benchmarks/                   ← Scalability experiments (blockchain layer)
│   ├── benchmark_tokenizacao.py  ← Synthetic load sweep: TPS, tx/block, nonce manager
│   ├── benchmark_sumo.py         ← SUMO-driven live tokenization (urban traffic)
│   └── benchmark_blockchain.py   ← Legacy benchmark (previous HTTP API)
│
├── audit/                        ← Traceability & on-chain auditability
│   ├── auditar_tokens.py         ← Batch SHA-256 integrity check of minted tokens (via API)
│   ├── rastreabilidade_token.py  ← Provenance-chain diagram for a token
│   └── relatorio_auditoria_onchain.py ← Per-trip audit report read from the chain
│
├── analysis/                     ← Results analysis + figures (notebooks & scripts)
│   ├── analise_acertividade.ipynb  ← Fraud detection / credit correctness
│   ├── analise_latencia.ipynb      ← Per-phase latency (5 runs)
│   ├── analise_escalabilidade.py   ← Worker-sweep scalability curve
│   ├── analise_resultados.py       ← Multi-run consolidation
│   ├── replot_figuras.py           ← Re-styles figures for the dissertation
│   ├── plot_sumo_mapa.py           ← Renders the SUMO road network + bounding box
│   └── manifest_analysis.ipynb     ← Single-run manifest analysis
│
├── requirements.txt
└──.env.example                  ← Template for your configuration
```

> **Run location:** the scripts in `benchmarks/`, `audit/` and `analysis/` are meant
> to be run **from the repository root** (e.g. `python benchmarks/benchmark_sumo.py …`);
> they resolve `config`, `tools/` and `results/` relative to the project root.


---

## 3. File-by-File Reference

### `contracts/EmissionsRegistry.sol`

The permanent on-chain CO₂ ledger. Every driving session logged through
the system produces one `EmissionRecord` stored here.

Key fields per record:
| Field | Type | Description |
|-------|------|-------------|
| `vehicleId` | string | Off-chain vehicle identifier (VIN, plate, etc.) |
| `co2Milligrams` | uint256 | Total session CO₂ in milligrams |
| `fuelType` | string | Gasolina / Diesel / Etanol |
| `dataHash` | bytes32 | SHA-256 of the original raw CSV file |
| `agentConfidence` | uint8 | Statistical validator confidence (0-100) |
| `requiresHumanReview` | bool | True when confidence < 70 |
| `governanceStatus` | enum | Pending → Approved or Denied |
| `agentDecision` | string | LLM-written audit rationale (on-chain) |
| `pipelineMetadata` | string | Model name + prompt hashes fingerprint |

Role model (3 accounts):
- **Owner (Account 1)**: can grant/revoke roles only. Never signs operational transactions.
- **Emission agents (Account 2)**: `logEmission()` + `validateEmission()`
- **Governance agents (Account 3)**: `updateGovernanceStatus()`

### `contracts/CarbonCredit.sol`

An **ERC-721** non-fungible certificate (`CarbonCertificate`), implemented from
scratch (no OpenZeppelin) with ERC-165 `supportsInterface`. Each driving session
that beats the baseline mints **one unique NFT** rather than fungible tokens —
preserving provenance (a trip in vehicle A at 09:00 is not interchangeable with a
trip in vehicle B at 14:00, even if they save the same CO₂ mass).

Each certificate stores the vehicle ID, the linked EmissionsRegistry record ID,
CO₂ saved (mg), and the equivalent credit value (`creditsEquivalentWei`). Tokens
can be transferred (`transferFrom`/`safeTransferFrom`) or burned (retired).

Minting (`mintCertificate`) is restricted to Account 3 (governance/minter role).

---

### `tools/co2_tools.py`

The physics engine. Contains:

- `estimate_maf()` — estimates Mass Air Flow via the speed-density equation when
  the MAF sensor reading is missing or zero. Uses intake temp, pressure, RPM,
  engine displacement, and volumetric efficiency. **Note:** the formula expects
  displacement in **litres**, so `engine_cc` (cm³) is divided by 1000 internally —
  omitting this conversion overestimates MAF (and CO₂) by ~1000× on estimated rows.

- `calculate_co2_physics(df, engine_cc)` — integrates per-row fuel consumption over
  all CSV rows to produce total session CO₂ in milligrams, plus the trip distance.
  Distance is `sum(speed_kmh)/3600` (1 row ≈ 1 s); implausible speeds are clipped to
  `MAX_PLAUSIBLE_SPEED_KMH` so GPS spoofing cannot inflate the distance-based
  baseline. Returns `(total_co2_mg, fuel_type, distance_km)`.

- `check_physical_plausibility(df)` — deterministic, history-independent sanity
  checks that catch frauds statistics alone miss: `impossible_speed`,
  `engine_off_motion` (speed > 0 with rpm == 0), `temperature_hack` (intake temp out
  of range), and `robotic_data` (near-zero rpm variance over a long trace). Returns
  `{"plausible", "anomaly_type", "details"}`.

This module is **pure Python** with no LLM involvement. It is the single source
of truth for all CO₂ numbers in the system.

### `tools/csv_tools.py`

Handles CSV loading with two entry points:

- `load_csv_bytes(csv_bytes)` — used by the API and orchestrator. Takes raw bytes,
  computes SHA-256, returns `(DataFrame, fuel_type, sha256_bytes)`.

- `validate_dataframe(df)` — checks whether the DataFrame has the required columns
  for CO₂ calculation and returns a validation report dict.

Fuel type is resolved in priority order:
1. `fuel_model_prediction` column (lab ML classifier format, e.g. `"gasoline"`) — majority class wins
2. `fuel_type` column (legacy format, Portuguese names)
3. Default: `"Gasolina"`

### `tools/sanitize.py`

All string fields pass through here before entering any LLM prompt or contract call.

- `sanitize_vehicle_id()` — validates against a strict alphanumeric regex, raises ValueError on anything suspicious
- `resolve_fuel_type()` — handles fuel_model_prediction column mapping and validation
- `sanitize_agent_text()` — strips injection patterns from any free-text field
- `compute_csv_sha256()` — returns 32-byte SHA-256 of raw CSV
- `verify_lab_envelope()` — verifies the SHA-256 in a lab JSON envelope matches the actual CSV bytes

### `tools/blockchain_tools.py`

All Web3 interaction with Hyperledger Besu. Every write function uses the correct
signing account:

| Function | Signer | Contract |
|----------|--------|----------|
| `log_emission_to_blockchain()` | Account 2 | EmissionsRegistry |
| `validate_emission_on_blockchain()` | Account 2 | EmissionsRegistry |
| `update_governance_status_on_blockchain()` | Account 3 | EmissionsRegistry |
| `issue_carbon_credits()` | Account 3 | CarbonCredit |
| `get_emission_record()` | none (read) | EmissionsRegistry |
| `get_credit_balance()` | none (read) | CarbonCredit |
| `get_issuance_history()` | none (read) | CarbonCredit |

All write transactions use gasPrice=0 (free-gas Besu network) and retry
automatically up to 5 times with exponential backoff (via `tenacity`).

---

### `agents/base.py`

Creates the Ollama LLM instance and provides two utilities:

- `get_llm(temperature)` — returns a `ChatOllama` instance using your configured model
- `extract_json(text)` — robustly parses JSON from LLM output (handles fenced code blocks, embedded JSON, etc.)
- `build_pipeline_metadata(prompts)` — creates a fingerprint dict with model name, pipeline version, and SHA-256 hashes of the agent system prompts. This is stored on-chain with every record.

### `agents/sensor_agent.py` — Phase 1

**What it does**: receives the pre-loaded DataFrame, runs the deterministic CO₂
physics calculation and the physical-plausibility checks, then asks the LLM to
assess data quality only. The plausibility result (`physical_anomaly`) is passed
to the ValidatorAgent as a hard-reject signal.

**What the LLM sees**: the computed CO₂ total (as a fact, not to be questioned),
CSV validation results, sensor column null percentages, and vehicle history.

**What the LLM produces**: `data_quality` label (good/fair/poor), `quality_notes`,
and an `assessment` sentence for the audit record.

**What it returns**: all physics results + LLM quality assessment + pipeline metadata.

### `agents/validator_agent.py` — Phase 2

This agent has a two-layer design:

**Layer 1 (deterministic — `_statistical_decision()` function)**:
- **Physical plausibility first**: if the SensorAgent flagged a physical anomaly
  (`impossible_speed`, `engine_off_motion`, `temperature_hack`, `robotic_data`), the
  session is rejected outright with confidence 0 — no matter what its CO₂ total or
  history looks like. This is history-independent, so it works even on a vehicle's
  very first session.
- Otherwise, if vehicle has ≥ 5 historical sessions: Z-score test against the
  vehicle's own mean/std.
- Otherwise (< 5 sessions): range check (10 g – 50 kg).
- Returns `(approved, anomaly_type, confidence_0_100)` — always the same for the same input.

**Layer 2 (LLM — explanation only)**:
- Receives the decision as a fixed fact
- Cannot change the decision
- Writes a 2-3 sentence plain-English explanation for the audit record

**Anomaly types**: `none`, `too_low`, `too_high`, `statistical_outlier`,
`impossible_speed`, `engine_off_motion`, `temperature_hack`, `robotic_data`

### `agents/governance_agent.py` — Phase 3

Also has a two-layer design:

**Layer 1 (deterministic — `_credit_decision()` function)**:
```
# Dynamic baseline scales with trip distance (175 g CO₂/km, Programa MOVER).
# Falls back to the fixed BASELINE_CO2_MG only when the session has no speed data.
baseline_mg = distance_km * BASELINE_CO2_G_PER_KM * 1000   if distance_km > 0
            = BASELINE_CO2_MG                              otherwise

saved_mg    = baseline_mg - actual_co2_mg
credits_cct = saved_mg / 1000 / CO2_PER_CREDIT_GRAM
credits_wei = int(credits_cct * 1e18)
```
Only issues credits if: (1) validator approved the record AND (2) saved_mg > 0.

**Layer 2 (LLM)**:
- Receives the credit decision as a fixed fact
- Writes the governance rationale that goes on-chain

### `agents/blockchain_agent.py` — Phase 4

Executes the on-chain transaction sequence in strict order:

1. `logEmission()` — **always** called, even if validation failed. Record starts as `Pending`.
2. `validateEmission()` — marks record as validated or rejected (Account 2)
3. `updateGovernanceStatus()` — resolves Pending → Approved or Denied (Account 3)
4. `mintCertificate()` — mints one ERC-721 certificate NFT only if governance approved (Account 3)
5. LLM writes the audit summary

The key design decision: **records are always logged**. Even rejected sessions
appear on-chain as `Denied`, creating a complete audit trail with no gaps.

---

### `orchestrator.py`

Coordinates all 4 agents. Key responsibilities:

- Sanitizes vehicle_id before anything else runs
- Loads CSV bytes and computes SHA-256 once (the same hash flows through all agents)
- Runs agents 1→2→3→4 in sequence
- Applies a **moderator check** between each phase: if an agent returns malformed output, the orchestrator applies a safe default rather than propagating the error
- Saves the session to SQLite after every run (including dry-runs), so Z-score validation accumulates history
- Accepts `dry_run=True` to skip Phase 4 (blockchain) while still running all 3 LLM agents

### `session_store.py`

SQLite-backed persistence layer with two tables:

**`sessions`** — vehicle CO₂ history for Z-score validation
- Keyed by `vehicle_id`
- `get_vehicle_history(vehicle_id, n=20)` returns the last N validated CO₂ values for a vehicle
- Used by ValidatorAgent to build the statistical baseline

**`jobs`** — API pipeline job tracking (survives server restarts)
- Replaces the in-memory dict from v1
- `count_vehicle_jobs_last_hour(vehicle_id)` powers the rate limiting

### `deploy_contracts.py`

One-time setup script. Runs once per Besu network:

1. Downloads solc 0.8.19 compiler (cached after first run)
2. Compiles both contracts → saves ABIs to `compiled/`
3. Deploys both contracts (signed by Account 1)
4. Calls `authorizeEmissionAgent(Account2)`, `authorizeGovernanceAgent(Account3)`,
   `authorizeMinter(Account3)` — all signed by Account 1
5. Writes the two contract addresses into your `.env` file

### `api.py`

FastAPI server with async background pipeline execution.

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/process` | POST | Upload CSV (multipart form), returns `job_id` immediately |
| `/process-envelope` | POST | Lab JSON envelope (base64 CSV + SHA-256 + user address) |
| `/status/{job_id}` | GET | Poll job status: pending / running / done / failed |
| `/result/{job_id}` | GET | Full pipeline report once done |
| `/record/{id}` | GET | Query any emission record from blockchain |
| `/portfolio/{address}` | GET | NFT count + certificates + CO₂ saved (mg & g) + EUR & BRL value |
| `/price/cct-eur` | GET | CCT price in EUR & BRL (fixed `.env` snapshots, no external API) |
| `/jobs` | GET | List recent jobs |
| `/health` | GET | Checks Besu + Ollama + contracts + job queue |

Rate limiting: 20 requests/minute per IP (global), plus per-vehicle limits
configured in `.env` (`RATE_LIMIT_PER_VEHICLE_PER_HOUR`, `MIN_SESSION_GAP_SECONDS`).

### `main.py`

CLI entrypoint. Wraps the orchestrator with argument parsing and formatted output.

```bash
# Generate sample data
python main.py --generate-sample

# Dry-run (no blockchain)
python main.py --csv data/sample_obd.csv --vehicle VIN-001 --recipient 0x... --dry-run

# Full pipeline
python main.py --csv data/sample_obd.csv --vehicle VIN-001 --recipient 0x...

# With custom engine and save report
python main.py --csv data/session.csv --vehicle VIN-001 --recipient 0x... \
               --engine-cc 1600 --output report.json

# Query portfolio from CLI (no pipeline)
python main.py --portfolio 0xYOUR_ADDRESS
```

---

## 4. How the Pipeline Works

```
CSV file (bytes)
      │
      ▼
[orchestrator.py]
  1. sanitize_vehicle_id()          ← reject invalid IDs before anything starts
  2. load_csv_bytes()               ← parse DataFrame + compute SHA-256 once
      │
      ▼
[SensorAgent]
  3. calculate_co2_physics()        ← deterministic physics + distance (no LLM)
  4. check_physical_plausibility()  ← deterministic fraud sanity checks
  5. validate_dataframe()           ← column checks, null percentages
  6. get_vehicle_history()          ← load past sessions from SQLite
  7. LLM: assess data quality only  ← produces quality label + assessment text
      │ moderator check: is total_co2_mg a valid number?
      ▼
[ValidatorAgent]
  8. _statistical_decision()        ← plausibility hard-reject → Z-score/range (pure Python)
  9. LLM: explain the decision only ← produces reasoning text for audit
      │ moderator check: does 'approved' key exist?
      ▼
[GovernanceAgent]
 10. dynamic baseline (175 g/km)    ← distance_km × BASELINE_CO2_G_PER_KM
 11. _credit_decision()             ← deterministic formula (pure Python)
 12. LLM: write governance rationale ← on-chain text explaining the decision
      │ moderator check: is credits_wei a valid integer?
      ▼
[BlockchainAgent]  ← skipped entirely in dry-run mode
 13. logEmission()                  ← Account 2 signs (always logged)
 14. validateEmission()             ← Account 2 signs
 15. updateGovernanceStatus()       ← Account 3 signs (Pending → Approved/Denied)
 16. mintCertificate()              ← Account 3 signs (only if approved)
 17. LLM: write audit summary
      │
      ▼
[session_store.save_session()]      ← always runs (including dry-run)
      │
      ▼
Full pipeline report (JSON)
```

---

## 5. Setup: Step by Step

### Step 1 — Install Python dependencies

```bash
pip install -r requirements.txt
```

Requires Python 3.10+.

### Step 2 — Install and start Ollama

```bash
# Download Ollama from https://ollama.com/download
ollama pull llama3.1:8b       # ~4.7 GB, recommended model
# alternatives if disk is tight:
ollama pull mistral         # ~4 GB
ollama pull phi3            # ~2.3 GB, lightweight

# Make sure it's running:
ollama serve                # (in a separate terminal if not auto-started)
```

### Step 3 — Create and fill `.env`

```bash
cp .env.example .env
```

Open `.env` and fill in your values:

```env
# Your Besu node
BESU_RPC_URL=http://localhost:8545
BESU_CHAIN_ID=1337

# Account 1 — OWNER (deploys contracts, grants roles, NOTHING ELSE)
OWNER_PRIVATE_KEY=0xYOUR_ACCOUNT_1_PRIVATE_KEY
OWNER_ADDRESS=0xYOUR_ACCOUNT_1_ADDRESS

# Account 2 — EMISSION LOGGER + VALIDATOR
EMISSION_PRIVATE_KEY=0xYOUR_ACCOUNT_2_PRIVATE_KEY
EMISSION_ADDRESS=0xYOUR_ACCOUNT_2_ADDRESS

# Account 3 — GOVERNANCE + MINTER (receives authority to issue CCT tokens)
GOVERNANCE_PRIVATE_KEY=0xYOUR_ACCOUNT_3_PRIVATE_KEY
GOVERNANCE_ADDRESS=0xYOUR_ACCOUNT_3_ADDRESS

# Ollama model (must match what you pulled)
OLLAMA_MODEL=llama3.1:8b
```

Leave `EMISSIONS_REGISTRY_ADDRESS` and `CARBON_CREDIT_ADDRESS` blank —
the deploy script fills them in.

---

## 6. Running Without Blockchain (Dry-Run)

The dry-run mode runs the first 3 agents (CO₂ physics, statistical validation,
governance credit decision) and stops before any blockchain writes. You do not
need a running Besu node or deployed contracts. You do need Ollama.

### Generate sample data first

```bash
python main.py --generate-sample
# Creates: data/sample_obd.csv (120 rows, realistic OBD-II data)
```

### Run dry-run

```bash
python main.py \
  --csv data/sample_obd.csv \
  --vehicle VIN-TEST-001 \
  --recipient 0xANY_VALID_ADDRESS \
  --dry-run
```

The output shows:
- Total CO₂ calculated by the physics engine
- Validation result (Z-score or range check, since this is the first session: range check)
- How many CCT tokens would have been issued
- All three LLM-generated texts (quality assessment, validation reasoning, governance rationale)

### Dry-run via API

```bash
# Start the API
uvicorn api:app --port 8000 --workers 1

# Submit a job in dry-run mode
curl -X POST http://localhost:8000/process \
  -F "file=@data/sample_obd.csv" \
  -F "vehicle_id=VIN-DRY-001" \
  -F "recipient=0xfe3b557e8fb62b89f4916b721be55ceb828dbd73" \
  -F "dry_run=true"

# Returns: {"job_id": "abc-123", "status": "pending", ...}

# Poll status:
curl http://localhost:8000/status/abc-123

# Get result when done:
curl http://localhost:8000/result/abc-123
```

---

## 7. Running With Blockchain

### Step 1 — Deploy contracts (run ONCE)

```bash
python deploy_contracts.py
```

Expected output:
```
════════════════════════════════════════════════════════════
  Carbon MAS v2 — Contract Deployment (3-account roles)
  Target : http://localhost:8545  (chainId 1337)
════════════════════════════════════════════════════════════

[1/5] Checking Solidity compiler …
  ✓ solc 0.8.19 ready

[2/5] Connecting to Besu …
  ✓ Connected  (block 42)
  Account 1 (owner)      : 0x...
  Account 2 (emission)   : 0x...
  Account 3 (governance) : 0x...

[3/5] Compiling contracts …
  ✓ EmissionsRegistry compiled → compiled/EmissionsRegistry.json
  ✓ CarbonCredit compiled → compiled/CarbonCredit.json

[4/5] Deploying ...
  ✓ EmissionsRegistry → 0xABC...  (block 43)
  ✓ CarbonCredit → 0xDEF...       (block 44)

[5/5] Granting roles …
  ✓ EmissionsRegistry.authorizeEmissionAgent(0x...)
  ✓ EmissionsRegistry.authorizeGovernanceAgent(0x...)
  ✓ CarbonCredit.authorizeMinter(0x...)
```

Your `.env` is automatically updated with both contract addresses.

### Step 2 — Run the full pipeline

```bash
python main.py \
  --csv data/sample_obd.csv \
  --vehicle VIN-001 \
  --recipient 0xYOUR_ACCOUNT_2_ADDRESS
```

The output shows all 4 phases running, with transaction hashes at the end.

### Step 3 — Verify on-chain

```bash
# Query the record by its ID (shown in the output)
curl http://localhost:8000/record/0

# Check the certificate portfolio (NFTs, CO₂ saved, EUR & BRL value)
curl http://localhost:8000/portfolio/0xYOUR_ACCOUNT_2_ADDRESS

# CCT price in EUR & BRL (fixed .env snapshots)
curl http://localhost:8000/price/cct-eur
```

### Running subsequent sessions (Z-score kicks in)

After the first session, run more with the same vehicle ID. From the 5th session
onwards, the ValidatorAgent switches from range-check to Z-score validation using
the vehicle's own CO₂ history. You can watch the confidence score change:

```bash
# Run 5+ times with different CSVs or modified sample data
for i in 1 2 3 4 5 6; do
  python main.py --csv data/sample_obd.csv --vehicle VIN-001 \
                 --recipient 0xACCOUNT_2 --output "reports/session_$i.json"
  sleep 10  # respect MIN_SESSION_GAP_SECONDS if testing locally
done
```

---

## 8. The API Server

### Start

```bash
uvicorn api:app --host 0.0.0.0 --port 8000 --workers 1
```

Use `--workers 1` — Ollama handles one inference at a time; more workers cause
GPU contention and slower responses.

### Key API flows

**Standard CSV upload:**
```bash
# 1. Submit
curl -X POST http://localhost:8000/process \
  -F "file=@data/sample_obd.csv" \
  -F "vehicle_id=VIN-001" \
  -F "recipient=0xACCOUNT_2" \
  -F "engine_cc=2000"

# 2. Poll
curl http://localhost:8000/status/{job_id}

# 3. Get result
curl http://localhost:8000/result/{job_id}
```

**Lab envelope (with SHA-256 integrity verification):**
```bash
# The lab system sends a JSON body — no multipart form
curl -X POST http://localhost:8000/process-envelope \
  -H "Content-Type: application/json" \
  -d '{
    "csv_b64": "<base64-encoded CSV>",
    "sha256":  "<hex SHA-256 of the decoded CSV bytes>",
    "user_address": "0xACCOUNT_2",
    "vehicle_id": "LAB-VIN-001",
    "engine_cc": 2000
  }'
```

**Portfolio query:**
```bash
curl http://localhost:8000/portfolio/0xACCOUNT_2
# Returns NFT count + certificates, total CO₂ saved (mg & g), and EUR & BRL value
```

---

## 9. Testing the Smart Contracts (Hardhat)

The Hardhat tests run on a local in-memory network — no Besu, no Ollama needed.

```bash
cd hardhat
npm install
npx hardhat test
```

Expected output:
```
  EmissionsRegistry
    Deployment
      ✓ sets the deployer as owner
      ✓ starts with zero records
      ✓ has correct CONFIDENCE_THRESHOLD
    Role management
      ✓ owner can authorize and revoke an emission agent
      ✓ owner can authorize and revoke a governance agent
      ✓ stranger cannot authorize agents
      ✓ emits EmissionAgentAuthorized event
    logEmission
      ✓ emission agent can log a record
      ✓ stranger cannot log a record
      ✓ stores all fields correctly
      ✓ sets requiresHumanReview=true when confidence < 70
      ... (and more)

  CarbonCredit
    Deployment
      ✓ sets deployer as governance
      ✓ has correct name and symbol
      ✓ starts with zero total supply
      ✓ supports the ERC-721 and ERC-165 interfaces
    ... (and more)

  94 passing (3s)
```

`EmissionsRegistry.test.js` has 37 tests; `CarbonCredit.test.js` has 57 (ERC-721
transfers, approvals, `getTokensByOwner`, burn, ERC-165, edge cases).

To run against your actual Besu node instead of the local Hardhat network:

```bash
npx hardhat test --network besu
```

---

## 9b. Batch System Testing & Result Analysis

To validate the whole pipeline over many sessions at once, `run_system_tests.py`
runs every CSV under a data folder and writes one JSON report per session plus a
`_manifest.json` summary into a timestamped folder under `results/`.

It auto-detects two layouts:

- **Real data** — `data/<vehicle>/<trip>.csv` → `vehicle_id = "<vehicle>-<VIN>"`.
- **Synthetic** — `data_synthetic/csv_testes_<vehicle>_viagem_<n>/<NN_scenario>.csv`
  → `vehicle_id = "<vehicle>-<VIN>-rodada-<NN>"`. Each scenario is isolated so
  fraud/anomaly cases don't pollute one another's Z-score history.

**History isolation**: if the data-dir name contains `synthetic`, the SQLite
history defaults to a separate DB (`carbon_mas_synthetic.db`) so synthetic frauds
never contaminate real-vehicle history. Override with `--db`.

```bash
# Real data → blockchain
python run_system_tests.py

# Synthetic scenarios → separate DB, no blockchain
python run_system_tests.py --data-dir data_synthetic --dry-run

# Custom recipient / engine size
python run_system_tests.py --recipient 0xACCOUNT_2 --engine-cc 1600
```

Each manifest session row records `total_co2_g`, `co2_saved_g`, `distance_km`,
`confidence`, `anomaly`, `governance_decision`, `credits_cct`, `token_id`, and
timing.

### Analysing the results

Open `manifest_analysis.ipynb` (needs only `pandas` + `matplotlib`). It loads the
latest `_manifest.json` and produces, for the dissertation:

- An **accuracy / confusion matrix** of the agentic oracle on synthetic scenarios
  (expected vs. actual decision), including **false positives** (frauds that wrongly
  earned credits) and **false negatives** (legitimate trips wrongly denied).
- A **CO₂-savings bar chart** per trip.
- **Emission intensity** (g CO₂/km) vs. the 175 g/km baseline, anomaly-type counts,
  and confidence/timing histograms.
- An exported `analise_consolidada.csv` next to the manifest.

```bash
jupyter notebook manifest_analysis.ipynb
```

---

## 10. The CSV Format

### Standard format (any OBD-II adapter)

| Column | Required | Description |
|--------|----------|-------------|
| `mass_air_flow` | Optional* | MAF sensor reading in g/s |
| `rpm` | Required if no MAF | Engine speed |
| `intake_air_temperature` | Required if no MAF | °C |
| `intake_manifold_absolut_pressure` | Required if no MAF | kPa |
| `fuel_type` | Optional | Gasolina / Diesel / Etanol |
| `speed` | Optional | km/h — drives trip distance and the dynamic credit baseline (175 g/km × distance). Without it, the fixed `BASELINE_CO2_MG` fallback is used |
| `engine_load` | Optional | For statistics |

*If `mass_air_flow` is missing or all-zero, MAF is estimated from RPM + temperature + pressure using the Ideal Gas Law.

### Lab format (university lab adapter)

The system also accepts the lab's ML classifier output column:

| Column | Values | Description |
|--------|--------|-------------|
| `fuel_model_prediction` | `gasoline`, `ethanol` | Per-row ML fuel classification |

When this column is present, the majority class across all rows becomes the session fuel type. It takes priority over `fuel_type` if both are present.

---

## 11. The 3-Account Role Model

This is how the 3 accounts are used, and why:

| | Account 1 | Account 2 | Account 3 |
|---|---|---|---|
| **Role** | Owner / Deployer | Emission Agent | Governance / Minter |
| **Deploys contracts** | ✅ | ❌ | ❌ |
| **Grants roles** | ✅ | ❌ | ❌ |
| **logEmission()** | ❌ | ✅ | ❌ |
| **validateEmission()** | ❌ | ✅ | ❌ |
| **updateGovernanceStatus()** | ❌ | ❌ | ✅ |
| **issueCredit() (mint CCT)** | ❌ | ❌ | ✅ |
| **Private key in .env** | `OWNER_PRIVATE_KEY` | `EMISSION_PRIVATE_KEY` | `GOVERNANCE_PRIVATE_KEY` |

**Why separate accounts?** If the emission agent key is compromised, the attacker
can log fraudulent records but cannot mint tokens — that requires the separate
governance key. If the governance key is compromised, the attacker can mint tokens
but cannot log the fraudulent records needed to justify them (the emission agent
key is required for that). Compromise of either operational key does not affect
the owner's ability to revoke that key's role.

---

## 13. Troubleshooting

**`solcx.exceptions.SolcInstallationError`**
→ Run `pip install py-solc-x --upgrade` then retry `deploy_contracts.py`

**`Cannot connect to http://localhost:8545`**
→ Besu must be started with `--rpc-http-enabled --min-gas-price=0 --host-allowlist="*"`

**`Compiled ABI not found: compiled/EmissionsRegistry.json`**
→ Run `python deploy_contracts.py` first

**`Registry: not emission agent`**
→ The key used for logEmission doesn't match the authorized Account 2.
  Check `EMISSION_PRIVATE_KEY` in `.env`

**`ollama: connection refused`**
→ Start Ollama: `ollama serve`

**`LLM parse error — using physics values directly`**
→ Normal fallback. The system continues with deterministic values.
  Try `llama3:8b` or `mistral` for better JSON compliance.

**First session always uses range-check, not Z-score**
→ Expected. Z-score requires `STAT_VALIDATION_MIN_HISTORY` (default: 5) validated
  sessions for that vehicle. Run the pipeline 5+ times with the same vehicle ID.

**`HTTP 429 — rate limit`**
→ Wait the indicated seconds, or lower `MIN_SESSION_GAP_SECONDS` in `.env` for testing.


