"""Host Hook integration. Runs only after the host has confirmed the artifact.

Install: pip install -e ./python
Configure RSI_MODEL_NAME, RSI_MODEL_BASE_URL and RSI_MODEL_API_KEY in the Hook's
environment. No .env file is read implicitly. The configured model must support
OpenAI-compatible chat completions and tool calling.
"""

from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path

from cosens.rsi import RSIClient


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", default=str(Path.home() / ".cosens" / "rsi"))
    parser.add_argument(
        "--context", required=True, help="Complete host context text file"
    )
    parser.add_argument(
        "--input",
        action="append",
        default=[],
        help="Current task input, used for learning and replay",
    )
    parser.add_argument(
        "--artifact",
        action="append",
        default=[],
        help="Confirmed host deliverable, learning only",
    )
    parser.add_argument("--session-id", required=True)
    parser.add_argument(
        "--delivery-id",
        required=True,
        help="Stable ID, changes for each confirmed delivery",
    )
    parser.add_argument("--wait", action="store_true")
    args = parser.parse_args()
    client = RSIClient(
        data_dir=args.data_dir,
        config={
            "model": os.environ["RSI_MODEL_NAME"],
            "base_url": os.environ["RSI_MODEL_BASE_URL"],
            "auto_start": True,
        },
    )
    inputs = [str(Path(p).resolve()) for p in args.input]
    execution = client.execution_create(
        evidence={
            "context_path": str(Path(args.context).resolve()),
            "files": [{"path": path, "role": "input"} for path in inputs]
            + [
                {"path": str(Path(p).resolve()), "role": "artifact"}
                for p in args.artifact
            ],
        },
        idempotency_key="execution:" + args.session_id + ":" + args.delivery_id,
        base_recipe_id=None,
    )
    print(
        json.dumps(
            {"execution_id": execution["id"], "status": execution["status"]},
            ensure_ascii=False,
        )
    )
    if args.wait:
        deadline = time.monotonic() + 300
        while True:
            result = client.execution_get(execution["id"])
            if result["status"] in {"completed", "failed", "cancelled"}:
                print(json.dumps(result, ensure_ascii=False, indent=2))
                break
            if time.monotonic() >= deadline:
                raise TimeoutError(
                    "execution is still pending; background work continues"
                )
            time.sleep(0.2)


if __name__ == "__main__":
    main()
