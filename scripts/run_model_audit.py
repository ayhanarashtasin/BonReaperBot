"""Economic audit of the fair-value models on recorded data (BR-505). Simulated, no orders.

Run: uv run python scripts/run_model_audit.py --input data/raw --workers 4
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from bone_reaper.research.model_research import run_model_audit
from bone_reaper.settings import load_settings


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", default="data/raw")
    parser.add_argument("--config", default="config/paper.yaml")
    parser.add_argument("--model", default="reports/model/logistic.json")
    parser.add_argument("--output", default="reports/model")
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--workers", type=int, default=1)
    args = parser.parse_args()
    report = run_model_audit(
        Path(args.input),
        load_settings(args.config),
        Path(args.model),
        Path(args.output),
        args.seed,
        args.workers,
    )
    print(json.dumps(report["decision"], indent=1))


if __name__ == "__main__":
    main()
