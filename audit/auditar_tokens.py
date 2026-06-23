"""
auditar_tokens.py — Auditoria de integridade dos tokens via API.

Para cada certificado emitido em uma rodada (manifesto com token_id), reenvia o
CSV original ao endpoint POST /verify da API e confirma que o SHA-256 recalculado
bate com o dataHash gravado on-chain. É a demonstração do caso de uso de uma
organização auditora: "este token corresponde mesmo a este dado?".

Pré-requisitos:
  - API no ar (uvicorn api:app ...), conectada ao Besu onde os tokens foram cunhados.
  - O mesmo --data-dir usado para gerar os tokens (bytes idênticos).

Uso:
    python auditar_tokens.py --data-dir data
    python auditar_tokens.py --manifest results/run_XXXX/_manifest.json --data-dir data
    python auditar_tokens.py --data-dir data --api-url http://localhost:8006
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import httpx

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass


def find_latest_manifest(results_dir: Path) -> Path:
    manifests = sorted(results_dir.glob("**/_manifest.json"),
                       key=lambda p: p.stat().st_mtime, reverse=True)
    if not manifests:
        raise SystemExit(f"Nenhum _manifest.json em {results_dir}")
    return manifests[0]


def main() -> int:
    ap = argparse.ArgumentParser(description="Audita integridade de tokens via API /verify.")
    ap.add_argument("--manifest", default=None, help="Manifesto da rodada (default: mais recente)")
    ap.add_argument("--results-dir", default="results")
    ap.add_argument("--data-dir", default="data", help="Onde estão os CSVs originais submetidos")
    ap.add_argument("--api-url", default="http://localhost:8000")
    ap.add_argument("--out", default=None, help="JSON de saída (default: ao lado do manifesto)")
    args = ap.parse_args()

    mpath = Path(args.manifest).resolve() if args.manifest \
        else find_latest_manifest(Path(args.results_dir).resolve())
    man = json.loads(mpath.read_text(encoding="utf-8"))
    data_dir = Path(args.data_dir).resolve()
    base = args.api_url.rstrip("/")

    tokens = [s for s in man["sessions"] if s.get("token_id") is not None]
    print(f"Manifesto: {mpath}")
    print(f"Tokens a auditar: {len(tokens)}  | API: {base}\n")

    results = []
    with httpx.Client(timeout=30.0) as client:
        for s in tokens:
            csv_path = data_dir / s["csv"].replace("\\", "/")
            tid = s["token_id"]
            if not csv_path.exists():
                print(f"  token #{tid:>3}  [!] CSV não encontrado: {csv_path}")
                results.append({"token_id": tid, "csv": s["csv"], "verified": None,
                                "error": "csv_not_found"})
                continue
            try:
                resp = client.post(
                    f"{base}/verify",
                    files={"file": (csv_path.name, csv_path.read_bytes(), "text/csv")},
                    data={"token_id": str(tid)},
                )
                resp.raise_for_status()
                r = resp.json()
                ok = r.get("integrity_verified")
                mark = "OK ✓" if ok else "DIVERGE ✗"
                print(f"  token #{tid:>3}  {mark}  veículo={r.get('vehicle_id')}  "
                      f"({csv_path.name})")
                results.append({"token_id": tid, "csv": s["csv"], "verified": ok,
                                "vehicle_id": r.get("vehicle_id"),
                                "on_chain_data_hash": r.get("on_chain_data_hash"),
                                "recomputed_sha256": r.get("recomputed_sha256")})
            except Exception as exc:
                print(f"  token #{tid:>3}  [!] erro: {exc}")
                results.append({"token_id": tid, "csv": s["csv"], "verified": None,
                                "error": str(exc)})

    verificados = sum(1 for r in results if r.get("verified") is True)
    divergentes = sum(1 for r in results if r.get("verified") is False)
    erros = sum(1 for r in results if r.get("verified") is None)
    print(f"\nResumo: {verificados} íntegros, {divergentes} divergentes, {erros} erro(s) "
          f"de {len(results)} tokens.")

    out = Path(args.out).resolve() if args.out else mpath.parent / "auditoria_tokens.json"
    out.write_text(json.dumps({
        "manifest": str(mpath), "api_url": base, "data_dir": str(data_dir),
        "verificados": verificados, "divergentes": divergentes, "erros": erros,
        "total": len(results), "resultados": results,
    }, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Relatório salvo em: {out}")
    return 0 if divergentes == 0 and erros == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
