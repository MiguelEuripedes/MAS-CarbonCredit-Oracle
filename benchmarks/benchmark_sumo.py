"""
benchmark_sumo.py — Experimento 3 dirigido pela simulação SUMO ao vivo.
"""
from __future__ import annotations

import sys as _sys, pathlib as _pathlib
_sys.path.insert(0, str(_pathlib.Path(__file__).resolve().parent.parent))  # raiz do repo no path (config, tools, ...)

import argparse
import csv
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import traci  # SUMO/TraCI

from benchmark_tokenizacao import Chain, tokenize_trip, analyze

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass


def tokenize_task(chain: Chain, trip: dict, baseline_g: float) -> dict:
    """Tokeniza uma viagem on-chain e devolve métricas da viagem + as tx individuais."""
    t0 = time.perf_counter()
    try:
        txs = tokenize_trip(chain, trip, baseline_g)
        return {
            "trip_id": trip["trip_id"], "vehicle_id": trip["vehicle_id"],
            "n_tx": len(txs), "latency_s": time.perf_counter() - t0,
            "decision": "approved" if len(txs) == 4 else "denied",
            "success": True, "error": "", "txs": txs,
        }
    except Exception as exc:
        return {
            "trip_id": trip["trip_id"], "vehicle_id": trip["vehicle_id"],
            "n_tx": 0, "latency_s": time.perf_counter() - t0,
            "decision": "error", "success": False, "error": f"{type(exc).__name__}: {exc}",
            "txs": [],
        }


def main() -> int:
    ap = argparse.ArgumentParser(description="Benchmark de tokenização dirigido pelo SUMO.")
    ap.add_argument("--sumocfg", default="./osm.sumocfg", help="arquivo de configuração do SUMO")
    ap.add_argument("--scale", type=float, default=1.0, help="fator de escala de tráfego do SUMO")
    ap.add_argument("--workers", type=int, default=20, help="threads concorrentes de tokenização")
    ap.add_argument("--baseline", type=float, default=175.0, help="baseline gCO2/km (MOVER)")
    ap.add_argument("--recipient", required=True, help="endereço(s) destinatário(s), por vírgula")
    ap.add_argument("--mass", type=float, default=1000.0, help="massa atribuída aos veículos (kg)")
    ap.add_argument("--out-dir", default="results/benchmark_sumo")
    args = ap.parse_args()

    import random
    recipients = [a.strip() for a in args.recipient.split(",") if a.strip()]
    out_dir = Path(args.out_dir).resolve(); out_dir.mkdir(parents=True, exist_ok=True)

    chain = Chain(pool_size=args.workers + 8)
    if not chain.w3.is_connected():
        print("ERRO: Besu não conectado em", __import__("config").BESU_RPC_URL); return 1

    executor = ThreadPoolExecutor(max_workers=args.workers)
    pending, vehicles = [], {}

    fila_path = out_dir / "fila.csv"
    with open(fila_path, "w", newline="", encoding="utf-8") as f:
        csv.writer(f).writerow(["sim_time", "wall_time", "queue_size"])

    print(f"SUMO: {args.sumocfg} | scale: {args.scale} | workers: {args.workers} "
          f"| baseline: {args.baseline} gCO2/km")
    print(f"Bloco inicial Besu: {chain.w3.eth.block_number}\n")

    traci.start(["sumo", "-c", args.sumocfg, "--scale", str(args.scale)])
    wall_start = time.perf_counter()

    while traci.simulation.getMinExpectedNumber() > 0:
        sim_time = traci.simulation.getTime()
        traci.simulationStep()

        # log do tamanho da fila de tarefas
        with open(fila_path, "a", newline="", encoding="utf-8") as f:
            csv.writer(f).writerow([sim_time, time.perf_counter(), executor._work_queue.qsize()])

        # novos veículos
        for vid in traci.simulation.getDepartedIDList():
            vehicles[vid] = {"co2_mg": 0.0, "dist_m": 0.0}
            try:
                traci.vehicle.setMass(vid, args.mass)
            except Exception:
                pass

        # acumula emissão e distância
        for vid in traci.vehicle.getIDList():
            if vid in vehicles:
                vehicles[vid]["dist_m"] = traci.vehicle.getDistance(vid)
                vehicles[vid]["co2_mg"] += traci.vehicle.getCO2Emission(vid)  # mg por passo (1s)

        # veículos que chegaram -> tokeniza on-chain agora
        for vid in traci.simulation.getArrivedIDList():
            if vid not in vehicles:
                continue
            d = vehicles.pop(vid)
            trip = {
                "trip_id": f"TRIP_{uuid.uuid4().hex[:8]}",
                "vehicle_id": f"SUMO-{vid}"[:48],
                "co2_real_g": d["co2_mg"] / 1000.0,     # mg -> g
                "distance_km": d["dist_m"] / 1000.0,    # m -> km
                "recipient": random.choice(recipients),
            }
            pending.append(executor.submit(tokenize_task, chain, trip, args.baseline))

    traci.close()
    print(f"Simulação SUMO encerrada. Tokenizações em voo: {len(pending)}. Drenando fila…")

    # coleta resultados
    resultados, all_txs = [], []
    for fut in as_completed(pending):
        r = fut.result()
        resultados.append(r)
        all_txs.extend(r["txs"])
    executor.shutdown(wait=True)
    wall = time.perf_counter() - wall_start

    # CSV de latência por viagem
    lat_path = out_dir / "latencia_viagens.csv"
    with open(lat_path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["trip_id", "vehicle_id", "n_tx", "latency_s", "decision", "success", "error"])
        for r in resultados:
            w.writerow([r["trip_id"], r["vehicle_id"], r["n_tx"],
                        round(r["latency_s"], 3), r["decision"], r["success"], r["error"]])

    ok = sum(1 for r in resultados if r["success"])
    err = len(resultados) - ok
    print(f"Viagens tokenizadas: {ok} ok, {err} erro(s). CSV: {lat_path}")

    if all_txs:
        analyze(all_txs, ok, wall, chain, out_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
