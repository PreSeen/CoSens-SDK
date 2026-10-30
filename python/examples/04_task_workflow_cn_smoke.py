"""CN gateway smoke test for the new Task workflow.

Run from IDEA or the repository root. Uses a unique synthetic task. If the
gateway has not deployed the new routes, exits 2 without writing remote data.
"""

import argparse
import asyncio
import json
import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

from cosens import CoSensClient


ROOT = Path(__file__).resolve().parents[2]


def save(path, report):
    if path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")


async def validate(client, report, output_path):
    ready = await client._transport.request("GET", "/health/ready")
    if ready.get("status") != "ok":
        raise AssertionError("CN gateway readiness is not ok")
    openapi = await client._transport.request("GET", "/openapi.json")
    paths = openapi.get("paths", {})
    report.update(openapi_version=openapi.get("info", {}).get("version"), path_count=len(paths),
                  workflow_paths=sorted(path for path in paths if path.startswith(("/v1/tasks", "/v1/executions", "/v1/revisions"))))
    save(output_path, report)
    if "/v1/tasks" not in paths:
        report["status"] = "workflow_not_deployed"
        save(output_path, report)
        return 2

    tag = "sdk-cn-smoke-" + uuid.uuid4().hex[:16]
    source_text = "CoSens SDK CN workflow smoke test"
    report["test_tag"] = tag
    input_artifact = await client.atoms.artifact(name=tag + "-input.txt", content=source_text)
    report["input_artifact_id"] = input_artifact["artifact_id"]
    save(output_path, report)
    task = await client.atoms.create_task(source="new_task", objective="Convert the supplied text to uppercase.",
                                          artifacts=[input_artifact["artifact_id"]], key=tag + ":task")
    if task.get("status") != "ready" or not task.get("task_id"):
        raise AssertionError(f"synthetic task is not ready: {task.get('status')}")
    report["task_id"] = task["task_id"]
    save(output_path, report)
    execution = await client.atoms.start_execution(task["task_id"], key=tag + ":execution")
    if execution.get("status") != "awaiting_execution" or not execution.get("execution_id") or not execution.get("request_sha256"):
        raise AssertionError(f"synthetic execution is not ready: {execution.get('status')}")
    report["execution_id"] = execution["execution_id"]
    report["request_sha256"] = execution["request_sha256"]
    save(output_path, report)
    fetched = await client.atoms.get_execution(execution["execution_id"])
    if fetched.get("request_sha256") != execution["request_sha256"]:
        raise AssertionError("frozen execution request hash changed")

    # This is a real local host computation over the input we just uploaded.
    output_text = source_text.upper()
    output_artifact = await client.atoms.artifact(name=tag + "-output.txt", content=output_text)
    report["output_artifact_id"] = output_artifact["artifact_id"]
    report["output_content_hash"] = output_artifact["content_hash"]
    save(output_path, report)
    body = {"request_sha256": execution["request_sha256"], "outcome": "completed",
            "artifacts": [{"artifact_id": output_artifact["artifact_id"], "role": "submitted_output"}],
            "events": [{"id": tag + ":uppercase", "kind": "tool_result",
                        "payload": {"operation": "uppercase", "input_artifact_id": input_artifact["artifact_id"]}}],
            "trace_coverage": "partial"}
    report["result_body"] = body  # Save the exact body before sending it for safe reconciliation.
    save(output_path, report)
    receipt = await client.atoms.submit_execution(execution["execution_id"], **body)
    if receipt.get("status") != "completed" or not receipt.get("revision_id"):
        raise AssertionError(f"execution submission did not complete: {receipt.get('status')}")
    report["revision_id"] = receipt["revision_id"]
    save(output_path, report)
    replay = await client.atoms.submit_execution(execution["execution_id"], **body)
    if replay.get("revision_id") != receipt["revision_id"]:
        raise AssertionError("same-body submission did not return the original revision")
    await client.atoms.get_revision(receipt["revision_id"])
    stored = await client.atoms.get_artifact(output_artifact["artifact_id"])
    if stored.get("content") != output_text or stored.get("content_hash") != output_artifact["content_hash"]:
        raise AssertionError("stored output artifact differs from the actual host result")
    report["status"] = "passed"
    save(output_path, report)
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "python/docs/testing/latest-cn/python-cn-workflow.json")
    parser.add_argument("--new-run", action="store_true", help="start a fresh synthetic task after manually reconciling an earlier incomplete run")
    args = parser.parse_args()
    if args.output and args.output.exists() and not args.new_run:
        previous = json.loads(args.output.read_text(encoding="utf-8"))
        if previous.get("test_tag") and previous.get("status") not in {"passed", "workflow_not_deployed"}:
            print("Earlier synthetic workflow is incomplete; inspect its saved IDs/result body before using --new-run.")
            return 1
    load_dotenv(ROOT / "python/.env", override=False)
    os.environ["COSENS_REGION"] = "cn"
    key = os.getenv("RSI_API_KEY_CN") or os.getenv("RSI_API_KEY")
    if not key or key.startswith("your-"):
        parser.error("Configure RSI_API_KEY_CN or RSI_API_KEY in python/.env")
    report = {"started_at": datetime.now(timezone.utc).isoformat(), "region": "cn", "status": "running"}
    try:
        async def run():
            async with CoSensClient(api_key=key) as client:
                return await validate(client, report, args.output)
        code = asyncio.run(run())
    except Exception as exc:
        report["status"] = "failed"
        report["error"] = f"{type(exc).__name__}: {exc}".replace(key, "[REDACTED]")
        save(args.output, report)
        print(json.dumps({"status": "failed", "error": report["error"], "output": str(args.output) if args.output else None}, ensure_ascii=False))
        return 1
    print(json.dumps({"status": report["status"], "openapi_version": report.get("openapi_version"),
                      "workflow_path_count": len(report.get("workflow_paths", [])),
                      "output": str(args.output) if args.output else None}, ensure_ascii=False))
    return code


if __name__ == "__main__":
    sys.exit(main())
