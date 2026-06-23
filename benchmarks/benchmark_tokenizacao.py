"""
benchmark_tokenizacao.py — Experimento 3: escalabilidade da camada de tokenização.
"""
from __future__ import annotations

import sys as _sys, pathlib as _pathlib
_sys.path.insert(0, str(_pathlib.Path(__file__).resolve().parent.parent))  # raiz do repo no path (config, tools, ...)

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
    Web3 com sessão HTTP de pool keep-alive dimensionado ao nº de workers. Sem isto,
    sob alta concorrência o web3 abre/recicla muitos sockets (cada wait_for_receipt
    faz dezenas de polls), estourando o limite de conexões do RPC do Besu.
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


# ── Gestão de nonce (thread-safe, por conta) ──────────────────────────────────

class NonceManager:
    """
    Aloca nonces sequenciais por conta. CRÍTICO: o nonce só é consumido (commit)
    APÓS o envio bem-sucedido. Um envio que falha (ex.: RPC recusando conexão sob
    carga) NÃO avança o nonce — evitando "buracos" que travam toda a fila de
    transações da conta (sintoma: blocos vazios com N pending no log do validador).
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
        """Recarrega o nonce da cadeia (auto-cura em caso de divergência)."""
        self._next[addr] = self._w3.eth.get_transaction_count(addr, "pending")


# ── Envio de transações ───────────────────────────────────────────────────────

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
        Envia (sem esperar). O nonce só avança APÓS o envio confirmado pelo nó.
        Tenta novamente em erros transitórios (ex.: RPC recusando conexão sob carga),
        sempre reusando o mesmo nonce — sem criar buracos. Retorna (hash, t_submit).
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
                    self.nm.commit(addr)          # só avança o nonce no sucesso
                    return h, t0
            except Exception as exc:
                last_exc = exc
                msg = str(exc).lower()
                if "nonce too low" in msg or "already known" in msg or "replacement" in msg:
                    # tx provavelmente já entrou; ressincroniza e segue sem travar a fila
                    with self.nm.lock(addr):
                        self.nm.resync(addr)
                    raise
                time.sleep(0.4 * (attempt + 1))    # backoff p/ erro transitório de conexão
        raise last_exc

    def confirm(self, h, t0) -> dict:
        # poll_latency 0.5s (em vez de 0.1) reduz ~5x o volume de requisições ao RPC
        r = self.w3.eth.wait_for_transaction_receipt(h, timeout=180, poll_latency=0.5)
        return {"block": r["blockNumber"], "gas": r["gasUsed"],
                "latency": time.perf_counter() - t0, "receipt": r}


# ── Tokenização de uma viagem (sequência completa) ────────────────────────────

def tokenize_trip(chain: Chain, trip: dict, baseline_g: float) -> list[dict]:
    """Executa a sequência on-chain de uma viagem e devolve os registros de tx."""
    txs = []
    co2_mg = int(trip["co2_real_g"] * 1000)
    baseline_mg = baseline_g * trip["distance_km"] * 1000
    saved_mg = max(0, baseline_mg - co2_mg)
    approved = saved_mg > 0
    data_hash = os.urandom(32)

    # 1) logEmission (Conta 2)
    h, t0 = chain.send(chain.reg.functions.logEmission(
        trip["vehicle_id"], co2_mg, "Gasolina", data_hash, 65,
        "benchmark", "benchmark"), chain.emission)
    r = chain.confirm(h, t0)
    events = chain.reg.events.EmissionLogged().process_receipt(r["receipt"])
    record_id = int(events[0]["args"]["recordId"])
    txs.append({"phase": "logEmission", **_strip(r), "submit": t0})

    # 2) validateEmission (Conta 2) — dado do SUMO é válido
    h, t0 = chain.send(chain.reg.functions.validateEmission(record_id, True), chain.emission)
    txs.append({"phase": "validateEmission", **_strip(chain.confirm(h, t0)), "submit": t0})

    # 3) updateGovernanceStatus (Conta 3)
    status = 1 if approved else 2
    h, t0 = chain.send(chain.reg.functions.updateGovernanceStatus(record_id, status), chain.governance)
    txs.append({"phase": "updateGovernanceStatus", **_strip(chain.confirm(h, t0)), "submit": t0})

    # 4) mintCertificate (Conta 3) — só se houve economia
    if approved:
        credits_wei = int((saved_mg / 1000 / 1000) * 1e18)  # saved_g/1000 CCT -> wei
        h, t0 = chain.send(chain.cct.functions.mintCertificate(
            Web3.to_checksum_address(trip["recipient"]), trip["vehicle_id"],
            record_id, int(saved_mg), credits_wei, "benchmark"), chain.governance)
        txs.append({"phase": "mintCertificate", **_strip(chain.confirm(h, t0)), "submit": t0})
    return txs


def _strip(r: dict) -> dict:
    return {"block": r["block"], "gas": r["gas"], "latency": r["latency"]}


# ── Geração / carga das viagens ───────────────────────────────────────────────

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
    # Sintético: distância 2–20 km; CO2 em torno da baseline para misturar aprovado/negado
    trips = []
    for i in range(args.n):
        dist = round(random.uniform(2, 20), 2)
        gkm = random.uniform(0.5, 1.4) * args.baseline  # 50%–140% da baseline
        trips.append({
            "vehicle_id": f"SUMO-{i:05d}",
            "co2_real_g": round(gkm * dist, 1),
            "distance_km": dist,
            "recipient": random.choice(recipients),
        })
    return trips


# ── Análise ───────────────────────────────────────────────────────────────────

def analyze(all_txs: list[dict], n_trips: int, wall: float, chain: Chain, out_dir: Path):
    import statistics as st
    lat = [t["latency"] for t in all_txs]
    gas = [t["gas"] for t in all_txs]
    n_tx = len(all_txs)

    print("\n" + "=" * 60)
    print("RESULTADOS — ESCALABILIDADE DA TOKENIZAÇÃO (Exp. 3)")
    print("=" * 60)
    print(f"Viagens tokenizadas : {n_trips}")
    print(f"Transações totais   : {n_tx}")
    print(f"Tempo total (wall)  : {wall:.2f} s")
    print(f"Throughput          : {n_trips/wall:.2f} viagens/s | {n_tx/wall:.2f} tx/s")
    print(f"Latência de confirmação (s): média {st.mean(lat):.2f} | "
          f"p50 {st.median(lat):.2f} | p95 {sorted(lat)[int(0.95*len(lat))-1]:.2f} | máx {max(lat):.2f}")
    print(f"Gas por transação   : média {st.mean(gas):.0f} | total {sum(gas):,}")

    # ── Transações por bloco (pico real da rede) ──────────────────────────────
    blocks = [t["block"] for t in all_txs]
    lo, hi = min(blocks), max(blocks)
    por_bloco = {}
    for n in range(lo, hi + 1):
        try:
            blk = chain.w3.eth.get_block(n)
            por_bloco[n] = len(blk["transactions"])
        except Exception:
            por_bloco[n] = sum(1 for b in blocks if b == n)  # fallback: só nossas tx
    counts = list(por_bloco.values())
    pico = max(counts) if counts else 0
    print(f"\nBlocos utilizados   : {lo}–{hi} ({len(por_bloco)} blocos)")
    print(f"Tx por bloco        : PICO {pico} | média {st.mean(counts):.1f} | "
          f"mediana {st.median(counts):.0f}")

    # gráfico tx por bloco
    fig, ax = plt.subplots(figsize=(12, 5))
    ax.bar(list(por_bloco.keys()), counts, color="#4C72B0")
    ax.axhline(pico, color="crimson", ls="--", lw=1.5, label=f"pico = {pico} tx/bloco")
    ax.set_xlabel("Número do bloco"); ax.set_ylabel("Transações mineradas")
    ax.set_title("Transações por bloco durante o benchmark de tokenização")
    ax.legend(); fig.tight_layout()
    fig.savefig(out_dir / "tx_por_bloco.png", dpi=200); plt.close(fig)

    # salva as latências por-transação (bruto) para re-plotagem futura
    (out_dir / "latencias_tx.csv").write_text(
        "latency_s\n" + "\n".join(f"{v:.4f}" for v in lat), encoding="utf-8")

    # histograma de latência (por transação)
    fig, ax = plt.subplots(figsize=(10, 5))
    ax.hist(lat, bins=60, color="#55A868", edgecolor="#2f4f3e", linewidth=0.4)
    ax.set_xlabel("Latência de confirmação por transação (s)"); ax.set_ylabel("Frequência")
    for s in ("top", "right", "left"):
        ax.spines[s].set_visible(False)
    ax.grid(axis="y", color="0.85", linewidth=0.9); ax.set_axisbelow(True)
    fig.tight_layout(); fig.savefig(out_dir / "latencia_confirmacao.png", dpi=200); plt.close(fig)

    resumo = {
        "viagens": n_trips, "transacoes": n_tx, "wall_s": round(wall, 2),
        "throughput_viagens_s": round(n_trips / wall, 3),
        "throughput_tx_s": round(n_tx / wall, 3),
        "latencia_media_s": round(st.mean(lat), 3),
        "latencia_p95_s": round(sorted(lat)[int(0.95 * len(lat)) - 1], 3),
        "gas_medio": round(st.mean(gas)), "gas_total": sum(gas),
        "blocos": {"inicio": lo, "fim": hi, "qtd": len(por_bloco)},
        "tx_por_bloco_pico": pico,
        "tx_por_bloco_media": round(st.mean(counts), 2),
        "tx_por_bloco": por_bloco,
    }
    (out_dir / "benchmark_tokenizacao.json").write_text(
        json.dumps(resumo, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\nArtefatos salvos em: {out_dir}")


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(description="Benchmark de escalabilidade da tokenização (Besu).")
    ap.add_argument("--trips-csv", default=None, help="CSV do SUMO (vehicle_id, co2_real_g, distance_km)")
    ap.add_argument("--n", type=int, default=100, help="nº de viagens sintéticas se sem --trips-csv")
    ap.add_argument("--workers", type=int, default=10, help="threads concorrentes")
    ap.add_argument("--baseline", type=float, default=175.0, help="baseline gCO2/km (MOVER)")
    ap.add_argument("--recipient", required=True, help="endereço(s) destinatário(s), separados por vírgula")
    ap.add_argument("--out-dir", default="results/benchmark_tokenizacao")
    args = ap.parse_args()

    chain = Chain(pool_size=args.workers + 8)
    if not chain.w3.is_connected():
        print("ERRO: Besu não conectado em", config.BESU_RPC_URL); return 1
    if not config.EMISSIONS_REGISTRY_ADDRESS or not config.CARBON_CREDIT_ADDRESS:
        print("ERRO: endereços de contrato vazios no .env (rode deploy_contracts.py)"); return 1

    trips = load_trips(args)
    out_dir = Path(args.out_dir).resolve(); out_dir.mkdir(parents=True, exist_ok=True)
    print(f"Viagens: {len(trips)} | workers: {args.workers} | baseline: {args.baseline} gCO2/km")
    print(f"Besu: {config.BESU_RPC_URL} | bloco inicial: {chain.w3.eth.block_number}\n")

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
                    print(f"  [!] viagem falhou: {type(exc).__name__}: {exc}")
            if i % 25 == 0:
                print(f"  {i}/{len(trips)} viagens processadas…")
    wall = time.perf_counter() - t_start
    print(f"\nConcluído: {ok} ok, {err} erro(s).")

    if all_txs:
        analyze(all_txs, ok, wall, chain, out_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
