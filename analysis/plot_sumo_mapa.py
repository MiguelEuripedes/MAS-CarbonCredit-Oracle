"""
plot_sumo_mapa.py — Renderiza a malha viária da simulação SUMO + a bounding box
"""
from __future__ import annotations

import argparse
import gzip
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.collections import LineCollection

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass


def _open(path: Path):
    return gzip.open(path, "rt", encoding="utf-8") if path.suffix == ".gz" \
        else open(path, "r", encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser(description="Renderiza a malha viária SUMO + bounding box.")
    ap.add_argument("--net", required=True, help="caminho para osm.net.xml(.gz)")
    ap.add_argument("--out", default="mapa_sumo.png")
    ap.add_argument("--linewidth", type=float, default=0.3)
    args = ap.parse_args()

    net = Path(args.net)
    if not net.exists():
        print("ERRO: arquivo não encontrado:", net); return 1

    segs = []
    boundary_orig = boundary_conv = None
    with _open(net) as f:
        for event, elem in ET.iterparse(f, events=("end",)):
            if elem.tag == "location":
                boundary_orig = elem.get("origBoundary")   # lon,lat (OSM)
                boundary_conv = elem.get("convBoundary")    # x,y (metros)
            elif elem.tag == "lane":
                shape = elem.get("shape")
                if shape:
                    pts = [tuple(map(float, p.split(","))) for p in shape.split()]
                    segs.extend([(pts[i], pts[i + 1]) for i in range(len(pts) - 1)])
                elem.clear()

    print(f"Segmentos de via: {len(segs)}")
    if boundary_orig:
        lon0, lat0, lon1, lat1 = map(float, boundary_orig.split(","))
        print("\nRetângulo selecionado no OpenStreetMap (graus):")
        print(f"  Longitude: {lon0:.6f} … {lon1:.6f}")
        print(f"  Latitude : {lat0:.6f} … {lat1:.6f}")
        print(f"  (canto SO: {lat0:.6f}, {lon0:.6f}  |  canto NE: {lat1:.6f}, {lon1:.6f})")
        print(f"  Visualizar: https://www.openstreetmap.org/?minlon={lon0}&minlat={lat0}"
              f"&maxlon={lon1}&maxlat={lat1}&box=yes")
    if boundary_conv:
        x0, y0, x1, y1 = map(float, boundary_conv.split(","))
        print(f"\nDimensões aproximadas da área: {abs(x1-x0)/1000:.2f} km × {abs(y1-y0)/1000:.2f} km")

    if not segs:
        print("Nenhuma geometria de via encontrada."); return 1

    fig, ax = plt.subplots(figsize=(10, 10))
    ax.add_collection(LineCollection(segs, colors="#333333", linewidths=args.linewidth))
    ax.autoscale()
    ax.set_aspect("equal")
    ax.axis("off")
    ax.set_title("Malha viária simulada (SUMO)", fontsize=14, fontweight="bold")
    fig.tight_layout()
    fig.savefig(args.out, dpi=300, bbox_inches="tight")
    print(f"\nMapa salvo em: {Path(args.out).resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
