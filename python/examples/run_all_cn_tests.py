"""One-click IDEA validation: full Python/TypeScript suites and CN gateway smoke.

Uses python/.env for the CN Key. Reports are written to
python/docs/testing/latest-cn/. Exit 0 means every stage passed; exit 2 means
the new workflow is not deployed; exit 1 means a test failed.
"""

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from dotenv import dotenv_values


ROOT = Path(__file__).resolve().parents[2]
OUTPUT = ROOT / "python/docs/testing/latest-cn"


def build_environment(saved, current):
    env = dict(current)
    for name, value in saved.items():
        if value is not None and name not in env:
            env[name] = value
    key = current.get("RSI_API_KEY_CN") or saved.get("RSI_API_KEY_CN") or current.get("RSI_API_KEY") or saved.get("RSI_API_KEY")
    if not key or key.startswith("your-"):
        raise ValueError("Configure RSI_API_KEY_CN or RSI_API_KEY in python/.env before CN validation.")
    env["COSENS_REGION"] = "cn"
    env["RSI_API_KEY"] = key
    env["RSI_API_KEY_CN"] = key
    return env, key


def overall_status(stages):
    workflow = {"python-cn-workflow", "typescript-cn-workflow"}
    if any(stage["exit_code"] != 0 and not (stage["name"] in workflow and stage["exit_code"] == 2) for stage in stages):
        return "failed", 1
    if any(stage["name"] in workflow and stage["exit_code"] == 2 for stage in stages):
        return "workflow_not_deployed", 2
    return "passed", 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--include-model-live", action="store_true", help="also run six Python/TypeScript examples against real CN and model services")
    args = parser.parse_args()
    OUTPUT.mkdir(parents=True, exist_ok=True)
    saved = dotenv_values(ROOT / "python/.env")
    try:
        env, key = build_environment(saved, os.environ)
    except ValueError as exc:
        print(exc)
        return 1
    stages = [
        ("python-unit", [sys.executable, "-m", "pytest", "-q", "python/tests"], ROOT),
        ("typescript-check", ["npm", "run", "check"], ROOT / "typescript"),
        ("python-cn-readonly", [sys.executable, "python/examples/validate_gateway.py", "--output", str(OUTPUT / "python-cn-readonly.json")], ROOT),
        ("typescript-cn-readonly", ["node", "--import", "tsx", "examples/04_validate_gateway_readonly.ts", "--output", str(OUTPUT / "typescript-cn-readonly.json")], ROOT / "typescript"),
    ]
    if args.include_model_live:
        stages.extend((f"python-{name}-live", [sys.executable, f"python/examples/{name}.py"], ROOT)
                      for name in ("01_transparent", "02_explicit_run", "03_atomic"))
        stages.extend((f"typescript-{name}-live", ["node", "--import", "tsx", f"examples/{name}.ts"], ROOT / "typescript")
                      for name in ("01_transparent", "02_explicit", "03_atomic"))
    stages.extend([
        ("python-cn-workflow", [sys.executable, "python/examples/04_task_workflow_cn_smoke.py", "--output", str(OUTPUT / "python-cn-workflow.json")], ROOT),
        ("typescript-cn-workflow", ["node", "--import", "tsx", "examples/05_task_workflow_cn_smoke.ts", "--output", str(OUTPUT / "typescript-cn-workflow.json")], ROOT / "typescript"),
    ])
    report = {"started_at": datetime.now(timezone.utc).isoformat(), "region": "cn", "stages": []}
    for name, command, directory in stages:
        print(f"[{name}] running...", flush=True)
        try:
            completed = subprocess.run(command, cwd=directory, env=env, capture_output=True, text=True, check=False, timeout=180)
            output = (completed.stdout + completed.stderr).replace(key, "[REDACTED]")
            code = completed.returncode
        except subprocess.TimeoutExpired as exc:
            parts = (exc.stdout or b"", exc.stderr or b"")
            output = "".join(part.decode(errors="replace") if isinstance(part, bytes) else part for part in parts)
            output = (output + "\nTimed out after 180 seconds.\n").replace(key, "[REDACTED]")
            code = 1
        except OSError as exc:
            output = f"{type(exc).__name__}: {exc}".replace(key, "[REDACTED]")
            code = 1
        (OUTPUT / f"{name}.log").write_text(output, encoding="utf-8")
        lines = [line for line in output.splitlines() if line.strip()]
        detail = next((line for line in lines if line.startswith("# pass ")), None) if name == "typescript-check" else None
        if detail is None:
            detail = lines[-1] if lines else "no output"
        print(f"[{name}] exit={code}; {detail[:240]}", flush=True)
        report["stages"].append({"name": name, "exit_code": code, "log": str(OUTPUT / f"{name}.log")})
        (OUTPUT / "summary.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    status, code = overall_status(report["stages"])
    report["status"] = status
    report["finished_at"] = datetime.now(timezone.utc).isoformat()
    (OUTPUT / "summary.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"CN overall status: {status}; report: {OUTPUT / 'summary.json'}", flush=True)
    return code


if __name__ == "__main__":
    sys.exit(main())
