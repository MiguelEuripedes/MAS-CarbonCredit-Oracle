"""
benchmark_tokenization.py — Scalability of the tokenization layer.
"""
from __future__ import annotations

import sys as _sys, pathlib as _pathlib
_sys.path.insert(0, str(_pathlib.Path(__file__).resolve().parent.parent))  # repository root on the path (config, tools, ...)

import argparse
import json
import os
import random
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import requests
from web3 import Web3
from web3.middleware import ExtraDataToPOAMiddleware

import config
from tools.blockchain_tools import _load_abi


def build_w3(pool_size: int) -> Web3:
    """
    Web3 with a keep-alive HTTP session pool sized to the number of workers. Without it,
    under high concurrency web3 opens and recycles many sockets (each wait_for_receipt
    makes dozens of polls), exceeding the connection limit of the Besu RPC.
    """
    session = requests.Session()
    adapter = requests.adapters.HTTPAdapter(pool_connections=pool_size, pool_maxsize=pool_size)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    w3 = Web3(Web3.HTTPProvider(config.BESU_RPC_URL, session=session))
    w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)
    return w3

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass


# ── Nonce management (thread-safe, per account) ───────────────────────────────

class NonceManager:
    """
    Allocates sequential nonces per account. CRITICAL: a nonce is only consumed (commit)
    AFTER a successful send. A failed send (e.g. the RPC refusing connections under
    load) does NOT advance the nonce, which avoids "gaps" that stall the whole
    transaction queue of the account (symptom: empty blocks with N pending in the
    validator log).
    """

    def __init__(self, w3: Web3, addresses: list[str]):
        self._w3 = w3
        self._locks = {a: threading.Lock() for a in addresses}
        self._next = {a: w3.eth.get_transaction_count(a, "pending") for a in addresses}

    def lock(self, addr: str) -> threading.Lock:
        return self._locks[addr]

    def peek(self, addr: str) -> int:
        return self._next[addr]

    def commit(self, addr: str) -> None:
        self._next[addr] += 1

    def resync(self, addr: str) -> None:
        """Reloads the nonce from the chain (self-healing when they diverge)."""
        self._next[addr] = self._w3.eth.get_transaction_count(addr, "pending")


# ── Transaction sending ───────────────────────────────────────────────────────

class Chain:
    def __init__(self, pool_size: int = 64):
        self.w3 = build_w3(pool_size)
        self.reg = self.w3.eth.contract(
            address=Web3.to_checksum_address(config.EMISSIONS_REGISTRY_ADDRESS),
            abi=_load_abi("EmissionsRegistry"))
        self.cct = self.w3.eth.contract(
            address=Web3.to_checksum_address(config.CARBON_CREDIT_ADDRESS),
            abi=_load_abi("CarbonCredit"))
        self.emission = self.w3.eth.account.from_key(config.EMISSION_PRIVATE_KEY)
        self.governance = self.w3.eth.account.from_key(config.GOVERNANCE_PRIVATE_KEY)
        self.nm = NonceManager(self.w3, [self.emission.address, self.governance.address])

    def send(self, fn, account, attempts: int = 5) -> tuple:
        """
        Sends without waiting. The nonce only advances AFTER the node accepts the send.
        Retries on transient errors (e.g. the RPC refusing connections under load),
        always reusing the same nonce, so no gaps are created. Returns (hash, t_submit).
        """
        addr = account.address
        last_exc = None
        for attempt in range(attempts):
            try:
                with self.nm.lock(addr):
                    nonce = self.nm.peek(addr)
                    tx = fn.build_transaction({
                        "from": addr, "nonce": nonce, "gas": 3_000_000,
                        "gasPrice": 0, "chainId": config.BESU_CHAIN_ID,
                    })
                    signed = account.sign_transaction(tx)
                    t0 = time.perf_counter()
                    h = self.w3.eth.send_raw_transaction(signed.raw_transaction)
                    self.nm.commit(addr)          # only advance the nonce on success
                    return h, t0
            except Exception as exc:
                last_exc = exc
                msg = str(exc).lower()
                if "nonce too low" in msg or "already known" in msg or "replacement" in msg:
                    # the tx probably went through; resync and continue without stalling the queue
                    with self.nm.lock(addr):
                        self.nm.resync(addr)
                    raise
                time.sleep(0.4 * (attempt + 1))    # backoff for transient connection errors
        raise last_exc

    def confirm(self, h, t0) -> dict:
        # poll_latency 0.5 s (instead of 0.1) cuts the number of RPC requests about 5x
        r = self.w3.eth.wait_for_transaction_receipt(h, timeout=180, poll_latency=0.5)
        return {"block": r["blockNumber"], "gas": r["gasUsed"],
                "latency": time.perf_counter() - t0, "receipt": r}


# ── Tokenization of one trip (full sequence) ──────────────────────────────────

def tokenize_trip(chain: Chain, trip: dict, baseline_g: float) -> list[dict]:
    """Runs the on-chain sequence of one trip and returns the transaction records."""
    txs = []
    co2_mg = int(trip["co2_real_g"] * 1000)
    baseline_mg = baseline_g * trip["distance_km"] * 1000
    saved_mg = max(0, baseline_mg - co2_mg)
    approved = saved_mg > 0
    data_hash = os.urandom(32)

    # 1) logEmission (Account 2)
    h, t0 = chain.send(chain.reg.functions.logEmission(
        trip["vehicle_id"], co2_mg, "Gasolina", data_hash, 65,
        "benchmark", "benchmark"), chain.emission)
    r = chain.confirm(h, t0)
    events = chain.reg.events.EmissionLogged().process_receipt(r["receipt"])
    record_id = int(events[0]["args"]["recordId"])
    txs.append({"phase": "logEmission", **_strip(r), "submit": t0})

    # 2) validateEmission (Account 2) — the SUMO data is valid
    h, t0 = chain.send(chain.reg.functions.validateEmission(record_id, True), chain.emission)
    txs.append({"phase": "validateEmission", **_strip(chain.confirm(h, t0)), "submit": t0})

    # 3) updateGovernanceStatus (Account 3)
    status = 1 if approved else 2
    h, t0 = chain.send(chain.reg.functions.updateGovernanceStatus(record_id, status), chain.governance)
    txs.append({"phase": "updateGovernanceStatus", **_strip(chain.confirm(h, t0)), "submit": t0})

    # 4) mintCertificate (Account 3) — only when there were savings
    if approved:
        credits_wei = int((saved_mg / 1000 / 1000) * 1e18)  # saved_g/1000 CCT -> wei
        h, t0 = chain.send(chain.cct.functions.mintCertificate(
            Web3.to_checksum_address(trip["recipient"]), trip["vehicle_id"],
            record_id, int(saved_mg), credits_wei, "benchmark"), chain.governance)
        txs.append({"phase": "mintCertificate", **_strip(chain.confirm(h, t0)), "submit": t0})
    return txs


def _strip(r: dict) -> dict:
    return {"block": r["block"], "gas": r["gas"], "latency": r["latency"]}


# ── Trip generation / loading ─────────────────────────────────────────────────

def load_trips(args) -> list[dict]:
    recipients = [a.strip() for a in args.recipient.split(",") if a.strip()]
    if args.trips_csv:
        import csv
        trips = []
        with open(args.trips_csv, newline="", encoding="utf-8") as f:
            for i, row in enumerate(csv.DictReader(f)):
                trips.append({
                    "vehicle_id": row.get("vehicle_id", f"SUMO-{i}")[:48],
                    "co2_real_g": float(row["co2_real_g"]),
                    "distance_km": float(row["distance_km"]),
                    "recipient": row.get("recipient") or random.choice(recipients),
                })
        return trips
    # Synthetic: distance 2–20 km and CO2 around the baseline, to mix approved and denied trips
    trips = []
    for i in range(args.n):
        dist = round(random.uniform(2, 20), 2)
        gkm = random.uniform(0.5, 1.4) * args.baseline  # 50%–140% of the baseline
        trips.append({
            "vehicle_id": f"SUMO-{i:05d}",
            "co2_real_g": round(gkm * dist, 1),
            "distance_km": dist,
            "recipient": random.choice(recipients),
        })
    return trips


# ── Analysis ──────────────────────────────────────────────────────────────────

def analyze(all_txs: list[dict], n_trips: int, wall: float, chain: Chain, out_dir: Path):
    import statistics as st
    lat = [t["latency"] for t in all_txs]
    gas = [t["gas"] for t in all_txs]
    n_tx = len(all_txs)

    print("\n" + "=" * 60)
    print("RESULTS — TOKENIZATION SCALABILITY")
    print("=" * 60)
    print(f"Tokenized trips     : {n_trips}")
    print(f"Total transactions  : {n_tx}")
    print(f"Total time (wall)   : {wall:.2f} s")
    print(f"Throughput          : {n_trips/wall:.2f} trips/s | {n_tx/wall:.2f} tx/s")
    print(f"Confirmation latency (s): mean {st.mean(lat):.2f} | "
          f"p50 {st.median(lat):.2f} | p95 {sorted(lat)[int(0.95*len(lat))-1]:.2f} | max {max(lat):.2f}")
    print(f"Gas per transaction : mean {st.mean(gas):.0f} | total {sum(gas):,}")

    # ── Transactions per block (actual network peak) ──────────────────────────
    blocks = [t["block"] for t in all_txs]
    lo, hi = min(blocks), max(blocks)
    per_block = {}
    for n in range(lo, hi + 1):
        try:
            blk = chain.w3.eth.get_block(n)
            per_block[n] = len(blk["transactions"])
        except Exception:
            per_block[n] = sum(1 for b in blocks if b == n)  # fallback: only our own txs
    counts = list(per_block.values())
    peak = max(counts) if counts else 0
    print(f"\nBlocks used         : {lo}–{hi} ({len(per_block)} blocks)")
    print(f"Tx per block        : PEAK {peak} | mean {st.mean(counts):.1f} | "
          f"median {st.median(counts):.0f}")

    # tx per block chart
    fig, ax = plt.subplots(figsize=(12, 5))
    ax.bar(list(per_block.keys()), counts, color="#4C72B0")
    ax.axhline(peak, color="crimson", ls="--", lw=1.5, label=f"peak = {peak} tx/block")
    ax.set_xlabel("Block number"); ax.set_ylabel("Mined transactions")
    ax.set_title("Transactions per block during the tokenization benchmark")
    ax.legend(); fig.tight_layout()
    fig.savefig(out_dir / "tx_per_block.png", dpi=200); plt.close(fig)

    # saves the raw per-transaction latencies for later re-plotting
    (out_dir / "tx_latencies.csv").write_text(
        "latency_s\n" + "\n".join(f"{v:.4f}" for v in lat), encoding="utf-8")

    # latency histogram (per transaction)
    fig, ax = plt.subplots(figsize=(10, 5))
    ax.hist(lat, bins=60, color="#55A868", edgecolor="#2f4f3e", linewidth=0.4)
    ax.set_xlabel("Confirmation latency per transaction (s)"); ax.set_ylabel("Frequency")
    for s in ("top", "right", "left"):
        ax.spines[s].set_visible(False)
    ax.grid(axis="y", color="0.85", linewidth=0.9); ax.set_axisbelow(True)
    fig.tight_layout(); fig.savefig(out_dir / "confirmation_latency.png", dpi=200); plt.close(fig)

    summary = {
        "trips": n_trips, "transactions": n_tx, "wall_s": round(wall, 2),
        "throughput_trips_s": round(n_trips / wall, 3),
        "throughput_tx_s": round(n_tx / wall, 3),
        "mean_latency_s": round(st.mean(lat), 3),
        "p95_latency_s": round(sorted(lat)[int(0.95 * len(lat)) - 1], 3),
        "mean_gas": round(st.mean(gas)), "total_gas": sum(gas),
        "blocks": {"start": lo, "end": hi, "count": len(per_block)},
        "peak_tx_per_block": peak,
        "mean_tx_per_block": round(st.mean(counts), 2),
        "tx_per_block": per_block,
    }
    (out_dir / "benchmark_tokenization.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\nArtifacts saved to: {out_dir}")


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(description="Scalability benchmark of the tokenization layer (Besu).")
    ap.add_argument("--trips-csv", default=None, help="SUMO CSV (vehicle_id, co2_real_g, distance_km)")
    ap.add_argument("--n", type=int, default=100, help="number of synthetic trips when --trips-csv is not given")
    ap.add_argument("--workers", type=int, default=10, help="concurrent threads")
    ap.add_argument("--baseline", type=float, default=175.0, help="baseline gCO2/km (MOVER)")
    ap.add_argument("--recipient", required=True, help="recipient address(es), comma-separated")
    ap.add_argument("--out-dir", default="results/benchmark_tokenization")
    args = ap.parse_args()

    chain = Chain(pool_size=args.workers + 8)
    if not chain.w3.is_connected():
        print("ERROR: Besu not connected at", config.BESU_RPC_URL); return 1
    if not config.EMISSIONS_REGISTRY_ADDRESS or not config.CARBON_CREDIT_ADDRESS:
        print("ERROR: empty contract addresses in .env (run deploy_contracts.py)"); return 1

    trips = load_trips(args)
    out_dir = Path(args.out_dir).resolve(); out_dir.mkdir(parents=True, exist_ok=True)
    print(f"Trips: {len(trips)} | workers: {args.workers} | baseline: {args.baseline} gCO2/km")
    print(f"Besu: {config.BESU_RPC_URL} | starting block: {chain.w3.eth.block_number}\n")

    all_txs, ok, err = [], 0, 0
    t_start = time.perf_counter()
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(tokenize_trip, chain, t, args.baseline): t for t in trips}
        for i, fut in enumerate(as_completed(futs), 1):
            try:
                all_txs.extend(fut.result()); ok += 1
            except Exception as exc:
                err += 1
                if err <= 5:
                    print(f"  [!] trip failed: {type(exc).__name__}: {exc}")
            if i % 25 == 0:
                print(f"  {i}/{len(trips)} trips processed…")
    wall = time.perf_counter() - t_start
    print(f"\nDone: {ok} ok, {err} error(s).")

    if all_txs:
        analyze(all_txs, ok, wall, chain, out_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
