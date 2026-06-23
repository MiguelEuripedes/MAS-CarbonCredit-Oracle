"""
deploy_contracts.py v2
───────────────────────
Compiles and deploys both contracts to Hyperledger Besu with 3-account role separation.

Account 1 (owner) deploys, then grants:
  - Account 2 → emissionAgent  on EmissionsRegistry
  - Account 3 → governanceAgent on EmissionsRegistry
  - Account 3 → minter         on CarbonCredit

Run ONCE per Besu network:
    python deploy_contracts.py
"""

from __future__ import annotations
import json, re, sys
from pathlib import Path

from web3 import Web3
from web3.middleware import ExtraDataToPOAMiddleware
from solcx import compile_standard, install_solc, get_installed_solc_versions

import config

SOLC_VERSION = "0.8.19"


def ensure_solc():
    if SOLC_VERSION not in [str(v) for v in get_installed_solc_versions()]:
        print(f"  Installing solc {SOLC_VERSION} …")
        install_solc(SOLC_VERSION)
    print(f"  ✓ solc {SOLC_VERSION} ready")


def compile_contract(name: str) -> tuple[list, str]:
    sol_path = config.CONTRACTS_DIR / f"{name}.sol"
    # Standard-JSON input so we can enable the optimizer + viaIR (avoids
    # "stack too deep" on functions with many params, e.g. mintCertificate).
    # These settings MUST match hardhat.config.js so tested and deployed
    # bytecode are identical.
    input_json = {
        "language": "Solidity",
        "sources": {f"{name}.sol": {"content": sol_path.read_text()}},
        "settings": {
            "optimizer": {"enabled": True, "runs": 200},
            "viaIR": True,
            "outputSelection": {"*": {"*": ["abi", "evm.bytecode.object"]}},
        },
    }
    compiled = compile_standard(input_json, solc_version=SOLC_VERSION)
    contract = compiled["contracts"][f"{name}.sol"][name]
    abi      = contract["abi"]
    bytecode = contract["evm"]["bytecode"]["object"]
    out_path = config.COMPILED_DIR / f"{name}.json"
    out_path.write_text(json.dumps({"abi": abi, "bytecode": bytecode}, indent=2))
    print(f"  ✓ {name} compiled → {out_path}")
    return abi, bytecode


def get_web3() -> Web3:
    w3 = Web3(Web3.HTTPProvider(config.BESU_RPC_URL))
    w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)
    return w3


def deploy(w3, account, abi, bytecode, label) -> str:
    contract = w3.eth.contract(abi=abi, bytecode=bytecode)
    tx = contract.constructor().build_transaction({
        "from":     account.address,
        "nonce":    w3.eth.get_transaction_count(account.address),
        "gas":      4_000_000,
        "gasPrice": 0,
        "chainId":  config.BESU_CHAIN_ID,
    })
    signed  = account.sign_transaction(tx)
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=60)
    addr    = receipt["contractAddress"]
    print(f"  ✓ {label} → {addr}  (block {receipt['blockNumber']})")
    return addr


def send_tx(w3, account, tx_func, label: str) -> None:
    tx = tx_func.build_transaction({
        "from":     account.address,
        "nonce":    w3.eth.get_transaction_count(account.address),
        "gas":      300_000,
        "gasPrice": 0,
        "chainId":  config.BESU_CHAIN_ID,
    })
    signed  = account.sign_transaction(tx)
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    w3.eth.wait_for_transaction_receipt(tx_hash, timeout=60)
    print(f"  ✓ {label}")


def update_env(key: str, value: str) -> None:
    env_path = Path(".env")
    if not env_path.exists():
        example = Path(".env.example")
        env_path.write_text(example.read_text() if example.exists() else "")
    content = env_path.read_text()
    pattern = rf"^{re.escape(key)}\s*=.*$"
    new_line = f"{key}={value}"
    if re.search(pattern, content, re.MULTILINE):
        content = re.sub(pattern, new_line, content, flags=re.MULTILINE)
    else:
        content = content.rstrip("\n") + f"\n{new_line}\n"
    env_path.write_text(content)


def main():
    print("═" * 62)
    print("  Carbon MAS v2 — Contract Deployment (3-account roles)")
    print(f"  Target : {config.BESU_RPC_URL}  (chainId {config.BESU_CHAIN_ID})")
    print("═" * 62)

    # Validate all 3 keys are set
    for label, key in [
        ("OWNER_PRIVATE_KEY",      config.OWNER_PRIVATE_KEY),
        ("EMISSION_PRIVATE_KEY",   config.EMISSION_PRIVATE_KEY),
        ("GOVERNANCE_PRIVATE_KEY", config.GOVERNANCE_PRIVATE_KEY),
    ]:
        if not key:
            print(f"  ✗ {label} not set in .env")
            sys.exit(1)

    print("\n[1/5] Checking Solidity compiler …")
    ensure_solc()

    print("\n[2/5] Connecting to Besu …")
    w3 = get_web3()
    if not w3.is_connected():
        print(f"  ✗ Cannot connect to {config.BESU_RPC_URL}")
        sys.exit(1)

    owner_acc      = w3.eth.account.from_key(config.OWNER_PRIVATE_KEY)
    emission_acc   = w3.eth.account.from_key(config.EMISSION_PRIVATE_KEY)
    governance_acc = w3.eth.account.from_key(config.GOVERNANCE_PRIVATE_KEY)

    print(f"  ✓ Connected  (block {w3.eth.block_number})")
    print(f"  Account 1 (owner)      : {owner_acc.address}")
    print(f"  Account 2 (emission)   : {emission_acc.address}")
    print(f"  Account 3 (governance) : {governance_acc.address}")

    for label, acc in [
        ("Account 1", owner_acc),
        ("Account 2", emission_acc),
        ("Account 3", governance_acc),
    ]:
        bal = w3.from_wei(w3.eth.get_balance(acc.address), "ether")
        print(f"  {label} balance: {bal} ETH")

    print("\n[3/5] Compiling contracts …")
    registry_abi, registry_bin = compile_contract("EmissionsRegistry")
    credit_abi,   credit_bin   = compile_contract("CarbonCredit")

    print("\n[4/5] Deploying (signed by Account 1 — owner) …")
    registry_addr = deploy(w3, owner_acc, registry_abi, registry_bin, "EmissionsRegistry")
    credit_addr   = deploy(w3, owner_acc, credit_abi,   credit_bin,   "CarbonCredit")

    print("\n[5/5] Granting roles …")
    registry = w3.eth.contract(address=Web3.to_checksum_address(registry_addr), abi=registry_abi)
    credit   = w3.eth.contract(address=Web3.to_checksum_address(credit_addr),   abi=credit_abi)

    # Account 2 → emission agent on EmissionsRegistry
    send_tx(w3, owner_acc,
            registry.functions.authorizeEmissionAgent(emission_acc.address),
            f"EmissionsRegistry.authorizeEmissionAgent({emission_acc.address[:10]}…)")

    # Account 3 → governance agent on EmissionsRegistry
    send_tx(w3, owner_acc,
            registry.functions.authorizeGovernanceAgent(governance_acc.address),
            f"EmissionsRegistry.authorizeGovernanceAgent({governance_acc.address[:10]}…)")

    # Account 3 → minter on CarbonCredit
    send_tx(w3, owner_acc,
            credit.functions.authorizeMinter(governance_acc.address),
            f"CarbonCredit.authorizeMinter({governance_acc.address[:10]}…)")

    # Write addresses to .env
    update_env("EMISSIONS_REGISTRY_ADDRESS", registry_addr)
    update_env("CARBON_CREDIT_ADDRESS",      credit_addr)

    print("\n" + "═" * 62)
    print("  ✅ Deployment complete!")
    print(f"  EmissionsRegistry : {registry_addr}")
    print(f"  CarbonCredit      : {credit_addr}")
    print("  Role assignments:")
    print(f"    Owner      (Account 1): {owner_acc.address}")
    print(f"    Emission   (Account 2): {emission_acc.address}")
    print(f"    Governance (Account 3): {governance_acc.address}")
    print("\n  Next step:")
    print("    python main.py --csv data/sample_obd.csv \\")
    print(f"                  --vehicle VIN-001 --recipient {emission_acc.address}")
    print("═" * 62)


if __name__ == "__main__":
    main()
