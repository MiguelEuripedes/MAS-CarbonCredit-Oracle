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

# --- Argument configuration ---
parser = argparse.ArgumentParser(description="Benchmark SUMO -> Oracle -> Blockchain V2")
parser.add_argument("--scale", type=float, default=1.0, help="SUMO traffic scale factor")
parser.add_argument("--workers", type=int, default=5, help="Number of concurrent threads")
parser.add_argument("--baseline", type=int, default=120, help="Emission target in g/km")
# Base URL updated for the new API version
parser.add_argument("--endpoint", type=str, default="http://localhost:8026/blockchain/v2", help="Base URL of the oracle")
args = parser.parse_args()

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

OUTPUT_CSV_FILE = f"latency_files/latency_scale{args.scale}_w{args.workers}.csv"
QUEUE_SIZE_LOG_FILE = f"queue_files/queue_scale{args.scale}_w{args.workers}.csv"

executor = ThreadPoolExecutor(max_workers=args.workers)
pending_futures = []

# Base date used to build the ISO 8601 timestamps required by the new API
SIMULATION_BASE_DATE = datetime(2024, 5, 20, 10, 0, 0)

# Test addresses to diversify the payload
TEST_ADDRESSES = [
    "0x627306090abaB3A6e1400e9345bC60c78a8BEf57",
    "0xFE3B557E8Fb62b89F4916B721be55cEb828dBd73",
    "0xf17f52151EbEF6C7334FAD080c5704D77216b732"
]

# --- CSV initialization ---
with open(OUTPUT_CSV_FILE, 'w', newline='') as f:
    writer = csv.writer(f)
    writer.writerow(["trip_id", "vehicle_id", "latency_seconds", "timestamp_request_sent", "status_code", "api_success", "tx_hash"])

with open(QUEUE_SIZE_LOG_FILE, 'w', newline='') as f:
    writer = csv.writer(f)
    writer.writerow(["simulation_time_step", "wall_time_seconds", "request_queue_size"])

# --- Sending thread function ---
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

        # Only counts as success when the response is HTTP 2xx AND carries the transaction hash
        if str(status_code).startswith('2'):
            try:
                resp_data = response.json()
                tx_hash = resp_data.get('tx_hash', "N/A")

                if tx_hash and tx_hash != "N/A":
                    success = True
                    status = status_code
                else:
                    success = False
                    status = "No_TxHash" # the API did not return the hash
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

    # Thread-safe write
    with open(OUTPUT_CSV_FILE, 'a', newline='') as f:
        writer = csv.writer(f)
        writer.writerow([payload['trip_id'], payload['veiculo_id'], latency, start_time, status, success, tx_hash])

# --- Simulation start ---
sumo_cmd = [
    "sumo", "-c", "./osm.sumocfg",
    "--scale", str(args.scale),
    "--emission-output", "emission.xml"
]

logging.info(f"Starting V2 test | Scale: {args.scale} | Workers: {args.workers} | URL: {args.endpoint}")
traci.start(sumo_cmd)

vehicles_trip_data = {}
wall_time_start_sim = time.time()

while traci.simulation.getMinExpectedNumber() > 0:
    current_time = traci.simulation.getTime()
    traci.simulationStep()
    current_wall_time = time.time()

    # Queue log
    queue_of_tasks_size = executor._work_queue.qsize()
    with open(QUEUE_SIZE_LOG_FILE, 'a', newline='') as f:
        writer = csv.writer(f)
        writer.writerow([current_time, current_wall_time, queue_of_tasks_size])

    # Vehicle registration
    for veh_id in traci.simulation.getDepartedIDList():
        vehicles_trip_data[veh_id] = {
            "trip_id": f"TRIP_SUMO_{str(uuid.uuid4())[:8]}", # shorter ID for clean logs
            "co2_emission": 0.0,
            "distance": 0.0,
            "timestamp_start": current_time
        }
        traci.vehicle.setMass(veh_id, 1000)

    # Update
    for veh_id in traci.vehicle.getIDList():
        if veh_id in vehicles_trip_data:
            vehicles_trip_data[veh_id]['distance'] = traci.vehicle.getDistance(veh_id)
            vehicles_trip_data[veh_id]['co2_emission'] += traci.vehicle.getCO2Emission(veh_id)

    # Arrivals at the destination and assembly of the NEW payload
    for veh_id in traci.simulation.getArrivedIDList():
        if veh_id in vehicles_trip_data:
            trip_data = vehicles_trip_data.pop(veh_id)

            distance_km = trip_data['distance'] / 1000
            emission_target = distance_km * args.baseline
            co2_real_g = trip_data['co2_emission'] / 1000

            savings_g = emission_target - co2_real_g

            # --- NEW FAILURE LOGGING LOGIC ---
            if savings_g <= 0:
                # LOGICAL FAILURE: emitted more than the target. Written directly to the CSV as a failure.
                current_wall_time = time.time()
                with open(OUTPUT_CSV_FILE, 'a', newline='') as f:
                    writer = csv.writer(f)
                    # Latency 0, status "No_Economy", success False, no hash
                    writer.writerow([trip_data['trip_id'], veh_id, 0.0, current_wall_time, "No_Economy", False, "N/A"])

                logging.warning(f"Vehicle {veh_id} logged a FAILURE (No_Economy). Actual: {co2_real_g:.1f}g | Target: {emission_target:.1f}g")
                continue # Skip to the next vehicle without calling the API

            # -----------------------------------------

            # If it gets here, the savings are positive. Prepare the API call.
            start_iso = (SIMULATION_BASE_DATE + timedelta(seconds=trip_data['timestamp_start'])).strftime("%Y-%m-%dT%H:%M:%SZ")

            # Field names follow the payload of the legacy HTTP API
            payload = {
                "user_address": random.choice(TEST_ADDRESSES),
                "trip_id": trip_data['trip_id'],
                "time_session": start_iso,
                "co2_meta_g": int(emission_target),
                "co2_emissao_real_g": int(co2_real_g),
                "veiculo_id": veh_id
            }

            future = executor.submit(send_trip_summary_task, payload)
            pending_futures.append(future)

traci.close()
logging.info("SUMO simulation finished. Waiting for the oracle to process the queue...")

for future in as_completed(pending_futures):
    pass

executor.shutdown(wait=True)
wall_time_end_sim = time.time()

# --- Final TPS ---
total_requests = len(pending_futures)
total_wall_time = wall_time_end_sim - wall_time_start_sim
tps = total_requests / total_wall_time if total_wall_time > 0 else 0

logging.info("\n=== V2 BENCHMARK RESULTS ===")
logging.info(f"Total trips (requests): {total_requests}")
logging.info(f"Total time (wall-clock): {total_wall_time:.2f} s")
logging.info(f"Mean throughput: {tps:.2f} req/s")