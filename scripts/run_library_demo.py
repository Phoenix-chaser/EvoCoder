"""Run an explicitly scripted, zero-provider-cost multi-family engineering demo."""
import argparse
from pathlib import Path

from evocoder.library_demo import run_demo

parser = argparse.ArgumentParser()
parser.add_argument("--output", type=Path, required=True)
parser.add_argument("--image", default="python:3.12-slim")
args = parser.parse_args()
result = run_demo(args.output, args.image)
print({"completed": result["completed"], "error": result["error"], "active_library": result["active_library"],
       "proposals": result["proposal_count"], "final_passed": sum(r["result"]["passed"] for r in result["final_runs"])})
raise SystemExit(0 if result["completed"] else 1)
