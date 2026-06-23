import traci
import logging
import requests
import time
import csv
import queue
import uuid
import argparse
import random
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed

# --- Configuração de Argumentos ---
parser = argparse.ArgumentParser(description="Benchmark SUMO -> Oracle -> Blockchain V2")
parser.add_argument("--scale", type=float, default=1.0, help="Fator de escala de tráfego do SUMO")
parser.add_argument("--workers", type=int, default=5, help="Número de threads simultâneas")
parser.add_argument("--baseline", type=int, default=120, help="Meta de emissão em g/km")
# URL Base atualizada para a nova versão da API
parser.add_argument("--endpoint", type=str, default="http://localhost:8026/blockchain/v2", help="URL Base do Oráculo")
args = parser.parse_args()

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

OUTPUT_CSV_FILE = f"latencia_files/latencia_scale{args.scale}_w{args.workers}.csv"
QUEUE_SIZE_LOG_FILE = f"fila_files/fila_scale{args.scale}_w{args.workers}.csv"

executor = ThreadPoolExecutor(max_workers=args.workers)
pending_futures = []

# Data base para simular o ISO 8601 exigido pela nova API
DATA_BASE_SIMULACAO = datetime(2024, 5, 20, 10, 0, 0)

# Endereços de teste para diversificar o payload
ENDERECOS_TESTE = [
    "0x627306090abaB3A6e1400e9345bC60c78a8BEf57",
    "0xFE3B557E8Fb62b89F4916B721be55cEb828dBd73",
    "0xf17f52151EbEF6C7334FAD080c5704D77216b732"
]

# --- Inicialização dos CSVs ---
with open(OUTPUT_CSV_FILE, 'w', newline='') as f:
    writer = csv.writer(f)
    writer.writerow(["trip_id", "veiculo_id", "latency_seconds", "timestamp_request_sent", "status_code", "api_success", "tx_hash"])

with open(QUEUE_SIZE_LOG_FILE, 'w', newline='') as f:
    writer = csv.writer(f)
    writer.writerow(["simulation_time_step", "wall_time_seconds", "request_queue_size"])

# --- Função da Thread de Envio ---
def send_trip_summary_task(payload):
    start_time = time.time()
    tx_hash = "N/A"
    latency = -1
    status = "Unknown"
    success = False
    
    try:
        url = f"{args.endpoint}/admin/registrar-viagem"
        response = requests.post(url, json=payload, headers={"Content-Type": "application/json"})
        end_time = time.time()
        
        latency = end_time - start_time
        status_code = response.status_code
        
        # Só considera sucesso se for HTTP 2xx E trouxer o Hash da transação
        if str(status_code).startswith('2'):
            try:
                resp_data = response.json()
                tx_hash = resp_data.get('tx_hash', "N/A")
                
                if tx_hash and tx_hash != "N/A":
                    success = True
                    status = status_code
                else:
                    success = False
                    status = "No_TxHash" # API falhou em devolver o hash
            except Exception:
                success = False
                status = "Invalid_JSON"
        else:
            success = False
            status = status_code
            
    except Exception as e:
        end_time = time.time()
        latency = end_time - start_time
        status = type(e).__name__
        success = False

    # Gravação Thread-Safe
    with open(OUTPUT_CSV_FILE, 'a', newline='') as f:
        writer = csv.writer(f)
        writer.writerow([payload['trip_id'], payload['veiculo_id'], latency, start_time, status, success, tx_hash])

# --- Início da Simulação ---
sumo_cmd = [
    "sumo", "-c", "./osm.sumocfg",
    "--scale", str(args.scale),
    "--emission-output", "emission.xml"
]

logging.info(f"Iniciando Teste V2 | Scale: {args.scale} | Workers: {args.workers} | URL: {args.endpoint}")
traci.start(sumo_cmd)

vehicles_trip_data = {}
wall_time_start_sim = time.time()

while traci.simulation.getMinExpectedNumber() > 0:
    current_time = traci.simulation.getTime()
    traci.simulationStep()
    current_wall_time = time.time()

    # Log da Fila
    queue_of_tasks_size = executor._work_queue.qsize()
    with open(QUEUE_SIZE_LOG_FILE, 'a', newline='') as f:
        writer = csv.writer(f)
        writer.writerow([current_time, current_wall_time, queue_of_tasks_size])

    # Registro de veículos
    for veh_id in traci.simulation.getDepartedIDList():
        vehicles_trip_data[veh_id] = { 
            "trip_id": f"TRIP_SUMO_{str(uuid.uuid4())[:8]}", # ID mais curto para logs limpos
            "co2_emission": 0.0, 
            "distance": 0.0,
            "timestamp_start": current_time
        }
        traci.vehicle.setMass(veh_id, 1000)

    # Atualização
    for veh_id in traci.vehicle.getIDList():
        if veh_id in vehicles_trip_data:
            vehicles_trip_data[veh_id]['distance'] = traci.vehicle.getDistance(veh_id)
            vehicles_trip_data[veh_id]['co2_emission'] += traci.vehicle.getCO2Emission(veh_id)

    # Chegadas ao destino e Montagem do NOVO Payload
    for veh_id in traci.simulation.getArrivedIDList():
        if veh_id in vehicles_trip_data:
            trip_data = vehicles_trip_data.pop(veh_id)
            
            distancia_km = trip_data['distance'] / 1000
            emission_meta = distancia_km * args.baseline
            co2_real_g = trip_data['co2_emission'] / 1000
            
            economia_g = emission_meta - co2_real_g
            
            # --- NOVA LÓGICA DE REGISTRO DE FALHAS ---
            if economia_g <= 0:
                # FALHA LÓGICA: Poluiu mais que a meta. Grava direto no CSV como falha.
                current_wall_time = time.time()
                with open(OUTPUT_CSV_FILE, 'a', newline='') as f:
                    writer = csv.writer(f)
                    # Latência 0, status "No_Economy", sucesso False, sem hash
                    writer.writerow([trip_data['trip_id'], veh_id, 0.0, current_wall_time, "No_Economy", False, "N/A"])
                
                logging.warning(f"Veículo {veh_id} registrou FALHA (No_Economy). Real: {co2_real_g:.1f}g | Meta: {emission_meta:.1f}g")
                continue # Pula para o próximo veículo, não chama a API
            
            # -----------------------------------------
            
            # Se chegou aqui, teve economia positiva. Prepara para chamar a API.
            start_iso = (DATA_BASE_SIMULACAO + timedelta(seconds=trip_data['timestamp_start'])).strftime("%Y-%m-%dT%H:%M:%SZ")

            payload = {
                "user_address": random.choice(ENDERECOS_TESTE),
                "trip_id": trip_data['trip_id'],
                "time_session": start_iso,
                "co2_meta_g": int(emission_meta),
                "co2_emissao_real_g": int(co2_real_g),
                "veiculo_id": veh_id
            }

            future = executor.submit(send_trip_summary_task, payload)
            pending_futures.append(future)

traci.close()
logging.info("Simulação SUMO finalizada. Aguardando Oráculo processar a fila...")

for future in as_completed(pending_futures):
    pass

executor.shutdown(wait=True)
wall_time_end_sim = time.time()

# --- TPS Final ---
total_requests = len(pending_futures)
total_wall_time = wall_time_end_sim - wall_time_start_sim
tps = total_requests / total_wall_time if total_wall_time > 0 else 0

logging.info("\n=== RESULTADOS DO BENCHMARK V2 ===")
logging.info(f"Total de Viagens (Requisições): {total_requests}")
logging.info(f"Tempo Total (Wall-clock): {total_wall_time:.2f} s")
logging.info(f"Throughput Médio: {tps:.2f} req/s")