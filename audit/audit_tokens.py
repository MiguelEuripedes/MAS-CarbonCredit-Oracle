"""
audit_tokens.py — Integrity audit of the tokens through the API.

For every certificate issued in a run (manifest entries with a token_id), resubmits the
original CSV to the POST /verify endpoint of the API and confirms that the recomputed
SHA-256 matches the dataHash stored on-chain. It demonstrates the use case of an
auditing organization: "does this token really correspond to this data?".

Requirements:
  - API running (uvicorn api:app ...), connected to the Besu network where the tokens were minted.
  - The same --data-dir used to generate the tokens (identical bytes).

Usage:
    python audit_tokens.py --data-dir data
    python audit_tokens.py --manifest results/run_XXXX/_manifest.json --data-dir data
    python audit_tokens.py --data-dir data --api-url http://localhost:8006
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
        raise SystemExit(f"No _manifest.json in {results_dir}")
    return manifests[0]


def main() -> int:
    ap = argparse.ArgumentParser(description="Audits token integrity through the /verify API.")
    ap.add_argument("--manifest", default=None, help="Run manifest (default: most recent)")
    ap.add_argument("--results-dir", default="results")
    ap.add_argument("--data-dir", default="data", help="Folder with the original submitted CSVs")
    ap.add_argument("--api-url", default="http://localhost:8000")
    ap.add_argument("--out", default=None, help="Output JSON (default: next to the manifest)")
    args = ap.parse_args()

    mpath = Path(args.manifest).resolve() if args.manifest \
        else find_latest_manifest(Path(args.results_dir).resolve())
    man = json.loads(mpath.read_text(encoding="utf-8"))
    data_dir = Path(args.data_dir).resolve()
    base = args.api_url.rstrip("/")

    tokens = [s for s in man["sessions"] if s.get("token_id") is not None]
    print(f"Manifest: {mpath}")
    print(f"Tokens to audit: {len(tokens)}  | API: {base}\n")

    results = []
    with httpx.Client(timeout=30.0) as client:
        for s in tokens:
            csv_path = data_dir / s["csv"].replace("\\", "/")
            tid = s["token_id"]
            if not csv_path.exists():
                print(f"  token #{tid:>3}  [!] CSV not found: {csv_path}")
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
                mark = "OK ✓" if ok else "MISMATCH ✗"
                print(f"  token #{tid:>3}  {mark}  vehicle={r.get('vehicle_id')}  "
                      f"({csv_path.name})")
                results.append({"token_id": tid, "csv": s["csv"], "verified": ok,
                                "vehicle_id": r.get("vehicle_id"),
                                "on_chain_data_hash": r.get("on_chain_data_hash"),
                                "recomputed_sha256": r.get("recomputed_sha256")})
            except Exception as exc:
                print(f"  token #{tid:>3}  [!] error: {exc}")
                results.append({"token_id": tid, "csv": s["csv"], "verified": None,
                                "error": str(exc)})

    verified = sum(1 for r in results if r.get("verified") is True)
    mismatched = sum(1 for r in results if r.get("verified") is False)
    errors = sum(1 for r in results if r.get("verified") is None)
    print(f"\nSummary: {verified} intact, {mismatched} mismatched, {errors} error(s) "
          f"out of {len(results)} tokens.")

    out = Path(args.out).resolve() if args.out else mpath.parent / "token_audit.json"
    out.write_text(json.dumps({
        "manifest": str(mpath), "api_url": base, "data_dir": str(data_dir),
        "verified": verified, "mismatched": mismatched, "errors": errors,
        "total": len(results), "results": results,
    }, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Report saved to: {out}")
    return 0 if mismatched == 0 and errors == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
