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
from pathlib import Path

from cosens.rsi import LocalConfig, ModelConfig, RSIClient


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
        help="File used to reproduce the test task",
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
    parser.add_argument(
        "--confirmation-ref",
        required=True,
        help="Host's actual artifact confirmation record",
    )
    parser.add_argument(
        "--test-instruction", help="A task the file-tools RSI executor can reproduce"
    )
    parser.add_argument("--auto-activate", action="store_true")
    parser.add_argument("--wait", action="store_true")
    args = parser.parse_args()
    client = RSIClient(
        data_dir=args.data_dir,
        config=LocalConfig(
            model=ModelConfig(
                model=os.environ["RSI_MODEL_NAME"],
                base_url=os.environ.get("RSI_MODEL_BASE_URL"),
            ),
            max_executions=2,
            max_model_requests=1,
        ),
    )
    material = client.material_create(
        manifest={
            "context_path": str(Path(args.context).resolve()),
            "source_session_id": args.session_id,
            "capture_boundary": args.delivery_id,
            "files": [
                {"path": str(Path(p).resolve()), "role": "input"} for p in args.input
            ]
            + [
                {"path": str(Path(p).resolve()), "role": "artifact"}
                for p in args.artifact
            ],
        },
        idempotency_key="material:" + args.session_id + ":" + args.delivery_id,
    )
    execution = client.execution_create(
        material_ids=[material["id"]],
        source_session_id=args.session_id,
        idempotency_key="execution:" + args.session_id + ":" + args.delivery_id,
        policy={
            "artifact_confirmed": True,
            "confirmation_ref": args.confirmation_ref,
            "auto_activate": args.auto_activate,
            "test_task": {
                "instruction": args.test_instruction,
                "input_locators": [
                    file["locator"]
                    for file in material["files"]
                    if file["role"] == "input"
                ],
            }
            if args.test_instruction
            else None,
        },
    )
    print(
        json.dumps(
            {"execution_id": execution["id"], "status": execution["status"]},
            ensure_ascii=False,
        )
    )
    if args.wait:
        print(json.dumps(client.wait(execution["id"]), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
