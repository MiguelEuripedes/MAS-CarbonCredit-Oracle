"""
tools/blockchain_tools.py
─────────────────────────
Web3 interaction tools for Hyperledger Besu — v2 (ERC-721 edition).

CarbonCredit is now ERC-721:
  - issueCredit()      →  mintCertificate()  (mints one NFT per session)
  - get_credit_balance → returns NFT count + list of token IDs
  - get_issuance_history → returns certificate data for each owned token
  - issue_carbon_credits → updated signature (no amount_wei, uses co2_saved_mg)

Signing accounts:
  logEmission + validateEmission          → Account 2 (EMISSION_PRIVATE_KEY)
  updateGovernanceStatus + mintCertificate → Account 3 (GOVERNANCE_PRIVATE_KEY)

All writes retry up to 5 times with exponential backoff (tenacity).
"""

from __future__ import annotations

import json
from typing import Optional

from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type
from web3 import Web3
from web3.middleware import ExtraDataToPOAMiddleware
from langchain_core.tools import tool

import config

# ── Web3 singleton ──
_w3: Optional[Web3] = None


def get_web3() -> Web3:
    global _w3
    if _w3 is None or not _w3.is_connected():
        _w3 = Web3(Web3.HTTPProvider(config.BESU_RPC_URL))
        _w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)
    return _w3


def get_emission_account():
    """Account 2 — logs and validates emissions."""
    return get_web3().eth.account.from_key(config.EMISSION_PRIVATE_KEY)


def get_governance_account():
    """Account 3 — updates governance status and mints CCT certificates."""
    return get_web3().eth.account.from_key(config.GOVERNANCE_PRIVATE_KEY)


# ── ABI cache ───
_abis: dict[str, list] = {}


def _load_abi(contract_name: str) -> list:
    if contract_name not in _abis:
        path = config.COMPILED_DIR / f"{contract_name}.json"
        if not path.exists():
            raise FileNotFoundError(
                f"Compiled ABI not found: {path}\nRun: python deploy_contracts.py"
            )
        with open(path) as f:
            _abis[contract_name] = json.load(f)["abi"]
    return _abis[contract_name]


# ── Transaction helper with retry ───
@retry(
    stop=stop_after_attempt(5),
    wait=wait_exponential(multiplier=1, min=2, max=30),
    retry=retry_if_exception_type(Exception),
    reraise=True,
)
def _send_tx(w3: Web3, tx_func, account) -> dict:
    """Sign and broadcast a zero-gas transaction. Retries on failure."""
    tx = tx_func.build_transaction({
        "from":     account.address,
        "nonce":    w3.eth.get_transaction_count(account.address, "pending"),
        "gas":      3_000_000,
        "gasPrice": 0,
        "chainId":  config.BESU_CHAIN_ID,
    })
    signed  = account.sign_transaction(tx)
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=60)
    return dict(receipt)


# ── LangChain Tools ─────
@tool
def check_besu_connection() -> str:
    """Verify that the Hyperledger Besu node is reachable."""
    try:
        w3 = get_web3()
        if not w3.is_connected():
            return json.dumps({"connected": False, "rpc_url": config.BESU_RPC_URL})
        return json.dumps({
            "connected":    True,
            "rpc_url":      config.BESU_RPC_URL,
            "chain_id":     w3.eth.chain_id,
            "latest_block": w3.eth.block_number,
        })
    except Exception as exc:
        return json.dumps({"connected": False, "error": str(exc)})


@tool
def log_emission_to_blockchain(
    vehicle_id: str,
    co2_milligrams: int,
    fuel_type: str,
    data_hash_hex: str,
    agent_confidence: int,
    agent_decision: str,
    pipeline_metadata: str,
) -> str:
    """
    Write a CO2 emission record to EmissionsRegistry (signed by Account 2).

    Args:
        vehicle_id:         Sanitized vehicle identifier.
        co2_milligrams:     Total session CO2 in mg (integer).
        fuel_type:          "Gasolina" | "Diesel" | "Etanol".
        data_hash_hex:      SHA-256 of raw CSV as 64-char hex string.
        agent_confidence:   Statistical validator confidence 0-100.
        agent_decision:     LLM rationale text (truncated at 2000 chars).
        pipeline_metadata:  Model fingerprint JSON (truncated at 600 chars).

    Returns JSON with status, tx_hash, record_id, block_number.
    """
    try:
        abi     = _load_abi("EmissionsRegistry")
        w3      = get_web3()
        account = get_emission_account()
        contract = w3.eth.contract(
            address=Web3.to_checksum_address(config.EMISSIONS_REGISTRY_ADDRESS),
            abi=abi,
        )
        data_hash_bytes = bytes.fromhex(data_hash_hex.replace("0x", ""))
        receipt = _send_tx(
            w3,
            contract.functions.logEmission(
                vehicle_id,
                int(co2_milligrams),
                fuel_type,
                data_hash_bytes,
                int(agent_confidence),
                agent_decision[:2000],
                pipeline_metadata[:600],
            ),
            account,
        )
        events    = contract.events.EmissionLogged().process_receipt(receipt)
        record_id = int(events[0]["args"]["recordId"]) if events else -1
        return json.dumps({
            "status":       "success",
            "tx_hash":      receipt["transactionHash"].hex(),
            "record_id":    record_id,
            "block_number": receipt["blockNumber"],
            "gas_used":     receipt["gasUsed"],
            "signer":       account.address,
        })
    except Exception as exc:
        return json.dumps({"status": "error", "message": str(exc)})


@tool
def validate_emission_on_blockchain(record_id: int, approved: bool) -> str:
    """
    Mark an emission record as validated or rejected (signed by Account 2).
    """
    try:
        abi     = _load_abi("EmissionsRegistry")
        w3      = get_web3()
        account = get_emission_account()
        contract = w3.eth.contract(
            address=Web3.to_checksum_address(config.EMISSIONS_REGISTRY_ADDRESS),
            abi=abi,
        )
        receipt = _send_tx(
            w3,
            contract.functions.validateEmission(int(record_id), approved),
            account,
        )
        return json.dumps({
            "status":    "success",
            "record_id": record_id,
            "approved":  approved,
            "tx_hash":   receipt["transactionHash"].hex(),
            "signer":    account.address,
        })
    except Exception as exc:
        return json.dumps({"status": "error", "message": str(exc)})


@tool
def update_governance_status_on_blockchain(record_id: int, approved: bool) -> str:
    """
    Set governance status Pending → Approved | Denied on EmissionsRegistry
    (signed by Account 3).
    """
    try:
        abi     = _load_abi("EmissionsRegistry")
        w3      = get_web3()
        account = get_governance_account()
        contract = w3.eth.contract(
            address=Web3.to_checksum_address(config.EMISSIONS_REGISTRY_ADDRESS),
            abi=abi,
        )
        status_val = 1 if approved else 2  # 0=Pending, 1=Approved, 2=Denied
        receipt = _send_tx(
            w3,
            contract.functions.updateGovernanceStatus(int(record_id), status_val),
            account,
        )
        return json.dumps({
            "status":            "success",
            "record_id":         record_id,
            "governance_status": "Approved" if approved else "Denied",
            "tx_hash":           receipt["transactionHash"].hex(),
            "signer":            account.address,
        })
    except Exception as exc:
        return json.dumps({"status": "error", "message": str(exc)})


@tool
def issue_carbon_credits(
    recipient_address: str,
    vehicle_id: str,
    emission_record_id: int,
    co2_saved_mg: int,
    credits_equivalent_wei: int,
    reason: str,
) -> str:
    """
    Mint one ERC-721 CarbonCertificate NFT to a recipient (signed by Account 3).

    Each session produces exactly ONE certificate regardless of how many
    credits it represents — preserving the unique provenance of that session.

    Args:
        recipient_address:      Ethereum wallet to receive the NFT.
        vehicle_id:             Vehicle identifier (stored in certificate).
        emission_record_id:     Linked EmissionsRegistry record ID.
        co2_saved_mg:           Milligrams of CO2 saved vs baseline.
        credits_equivalent_wei: Reference credit value in wei (18 decimals).
        reason:                 Governance rationale (truncated at 1000 chars).

    Returns JSON with status, tx_hash, token_id, recipient.
    """
    try:
        abi     = _load_abi("CarbonCredit")
        w3      = get_web3()
        account = get_governance_account()
        contract = w3.eth.contract(
            address=Web3.to_checksum_address(config.CARBON_CREDIT_ADDRESS),
            abi=abi,
        )
        receipt = _send_tx(
            w3,
            contract.functions.mintCertificate(
                Web3.to_checksum_address(recipient_address),
                vehicle_id,
                int(emission_record_id),
                int(co2_saved_mg),
                int(credits_equivalent_wei),
                reason[:1000],
            ),
            account,
        )
        # Parse CertificateMinted event to get token_id
        events   = contract.events.CertificateMinted().process_receipt(receipt)
        token_id = int(events[0]["args"]["tokenId"]) if events else -1

        return json.dumps({
            "status":                 "success",
            "tx_hash":                receipt["transactionHash"].hex(),
            "token_id":               token_id,
            "recipient":              recipient_address,
            "co2_saved_mg":           co2_saved_mg,
            "credits_equivalent_cct": credits_equivalent_wei / 1e18,
            "emission_record_id":     emission_record_id,
            "signer":                 account.address,
        })
    except Exception as exc:
        return json.dumps({"status": "error", "message": str(exc)})


@tool
def get_emission_record(record_id: int) -> str:
    """Fetch an emission record from EmissionsRegistry (read-only)."""
    try:
        abi = _load_abi("EmissionsRegistry")
        w3  = get_web3()
        contract = w3.eth.contract(
            address=Web3.to_checksum_address(config.EMISSIONS_REGISTRY_ADDRESS),
            abi=abi,
        )
        rec = contract.functions.getRecord(int(record_id)).call()
        governance_labels = {0: "Pending", 1: "Approved", 2: "Denied"}
        return json.dumps({
            "status":               "success",
            "record_id":            record_id,
            "vehicle_id":           rec[0],
            "timestamp":            rec[1],
            "co2_milligrams":       rec[2],
            "fuel_type":            rec[3],
            "data_hash":            rec[4].hex(),
            "validated":            rec[5],
            "validator":            rec[6],
            "agent_confidence":     rec[7],
            "requires_human_review": rec[8],
            "governance_status":    governance_labels.get(rec[9], "Unknown"),
            "governance_actor":     rec[10],
            "agent_decision":       rec[11],
            "pipeline_metadata":    rec[12],
        })
    except Exception as exc:
        return json.dumps({"status": "error", "message": str(exc)})


@tool
def get_certificate(token_id: int) -> str:
    """
    Fetch a single CarbonCertificate NFT by token ID (read-only), including its
    current owner and the linked EmissionsRegistry record ID. Used for auditing
    the provenance of a credit token.
    """
    try:
        abi = _load_abi("CarbonCredit")
        w3  = get_web3()
        contract = w3.eth.contract(
            address=Web3.to_checksum_address(config.CARBON_CREDIT_ADDRESS),
            abi=abi,
        )
        cert  = contract.functions.getCertificate(int(token_id)).call()
        owner = contract.functions.ownerOf(int(token_id)).call()
        return json.dumps({
            "status":                 "success",
            "token_id":               int(token_id),
            "owner":                  owner,
            "vehicle_id":             cert[0],
            "emission_record_id":     int(cert[1]),
            "co2_saved_mg":           int(cert[2]),
            "credits_equivalent_cct": int(cert[3]) / 1e18,
            "reason":                 cert[4],
            "issued_at":              int(cert[5]),
            "recipient":              cert[6],
        })
    except Exception as exc:
        return json.dumps({"status": "error", "message": str(exc)})


@tool
def get_credit_balance(address: str) -> str:
    """
    Return the number of CCT certificate NFTs owned by a wallet,
    plus all owned token IDs.
    """
    try:
        abi = _load_abi("CarbonCredit")
        w3  = get_web3()
        contract = w3.eth.contract(
            address=Web3.to_checksum_address(config.CARBON_CREDIT_ADDRESS),
            abi=abi,
        )
        checksum_addr = Web3.to_checksum_address(address)
        nft_count  = contract.functions.balanceOf(checksum_addr).call()
        token_ids  = contract.functions.getTokensByOwner(checksum_addr).call()
        return json.dumps({
            "status":       "success",
            "address":      address,
            "nft_count":    nft_count,
            "token_ids":    [int(t) for t in token_ids],
        })
    except Exception as exc:
        return json.dumps({"status": "error", "message": str(exc)})


@tool
def get_issuance_history(address: str, limit: int = 50) -> str:
    """
    Return all CarbonCertificate NFTs currently owned by a wallet,
    including full certificate data for each token.
    """
    try:
        abi = _load_abi("CarbonCredit")
        w3  = get_web3()
        contract = w3.eth.contract(
            address=Web3.to_checksum_address(config.CARBON_CREDIT_ADDRESS),
            abi=abi,
        )
        checksum_addr = Web3.to_checksum_address(address)
        token_ids = contract.functions.getTokensByOwner(checksum_addr).call()

        certificates = []
        for tid in list(token_ids)[:limit]:
            cert = contract.functions.getCertificate(int(tid)).call()
            certificates.append({
                "token_id":               int(tid),
                "vehicle_id":             cert[0],
                "emission_record_id":     int(cert[1]),
                "co2_saved_mg":           int(cert[2]),
                "credits_equivalent_cct": int(cert[3]) / 1e18,
                "reason":                 cert[4],
                "issued_at":              int(cert[5]),
                "recipient":              cert[6],
            })

        total_co2_saved = sum(c["co2_saved_mg"] for c in certificates)
        total_credits   = sum(c["credits_equivalent_cct"] for c in certificates)

        return json.dumps({
            "status":             "success",
            "address":            address,
            "certificate_count":  len(certificates),
            "total_co2_saved_mg": total_co2_saved,
            "total_credits_cct":  round(total_credits, 6),
            "certificates":       certificates,
        })
    except Exception as exc:
        return json.dumps({"status": "error", "message": str(exc)})
