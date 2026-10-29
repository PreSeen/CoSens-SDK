"""Validate the supplied curl case through the SDK and selected gateway.

Reads RSI_API_KEY (or prompts without echo). Default is read-only. --write
creates an isolated synthetic Recipe, Memory source, Run and evaluations.
No curl command from the transcript is executed; demo tool results are
recomputed locally with Decimal. No real model or financial tool is called.
"""

import argparse
import asyncio
import getpass
import hashlib
import json
import os
import re
import shlex
import time
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from cosens import CoSensClient
from cosens._config import resolve_service
from urllib.parse import urlsplit
from cosens.workflow import freeze_run_context, prepare_ready_async


def dump(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value):
    return hashlib.sha256(dump(value).encode()).hexdigest()


def section(text, number, *, request=False):
    part = next(s for s in re.split(r"(?m)^## ", text)[1:] if s.startswith(f"{number}. "))
    if request:
        words = shlex.split(re.search(r"```bash\n(.*?)\n```", part, re.S)[1])
        return json.loads(words[words.index("--data-binary") + 1])
    return json.loads(re.search(r"```json\n(.*?)\n```", part, re.S)[1])


async def poll(call, ready, *, timeout=30):
    deadline = time.monotonic() + timeout
    while True:
        value = await call()
        if ready(value):
            return value
        if time.monotonic() >= deadline:
            raise TimeoutError("Gateway asynchronous state did not become ready within 30 seconds")
        await asyncio.sleep(0.5)


async def validate(args, key, report):
    host = urlsplit(resolve_service().url).hostname
    async def observe(response):
        request = response.request
        identity = any(k.lower().replace("_", "-") == "x-user-id" for k in request.headers)
        assert request.headers.get("x-auth-type") == "api_key"
        assert request.headers.get("authorization") == f"Bearer {key}"
        entry = {"method": request.method, "path": request.url.raw_path.decode(), "http_status": response.status_code,
                 "request_id": response.headers.get("x-request-id"), "identity_header_sent": identity}
        report["requests"].append(entry)
        print(dump(entry), flush=True)
        assert not identity, "SDK must not send X-User-Id"
        assert request.url.scheme == "https" and request.url.host == host

    async with CoSensClient(api_key=key) as client:
        client._transport.client.event_hooks["response"].append(observe)
        client._admin_transport.client.event_hooks["response"].append(observe)
        atoms = client.atoms
        ready = await client._transport.request("GET", "/health/ready")
        assert ready["status"] == "ok"
        # This demonstrates the gateway supplied a Memory identity without SDK input.
        report["gateway_memory_project_present"] = bool(ready.get("memory", {}).get("project"))
        context = await atoms.recall_context(query="CoSens SDK gateway validation", token_budget=200)
        assert context.get("run_id") is None
        report["standalone_recall"] = "passed"
        items = await atoms.get_memory_items(limit=1)
        assert isinstance(items.get("sources"), list) and isinstance(items.get("experiences"), list)
        assert items.get("limit") == 1
        report["memory_items"] = "passed"
        if not args.write:
            return

        transcript = args.transcript.read_text()
        tag = "sdk-gateway-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:6]
        report["test_tag"] = tag
        bootstrap = section(transcript, 3, request=True)
        bootstrap.update(task_family=tag, reason="User-requested SDK gateway validation; isolated synthetic Recipe")
        seeded = await client.admin.atoms.bootstrap_bundle(**bootstrap)
        report["recipe_id"] = seeded["recipe_id"]
        memory = await atoms.memory_ingest(content=f"{tag}: synthetic SDK validation only. Match bank payout transfers by batch and transfer IDs; separate account fees are excluded.", request_key=tag + ":memory")
        job = await poll(lambda: atoms.get_memory_job(memory["job_id"]), lambda x: x.get("status", x.get("state")) in {"committed", "done", "completed"})
        report["memory_job"] = {"job_id": memory["job_id"], "status": job.get("status", job.get("state"))}
        recalled = await atoms.memory_recall(query=tag, token_budget=1000)
        assert any(tag in item.get("text", "") for item in recalled["items"])
        await atoms.get_memory()

        fixture = section(transcript, 9)["data"]
        source = await atoms.artifact(name=tag + "-input.json", content=dump(fixture))
        params = section(transcript, 11, request=True)
        params.update(task_family=tag, task_lineage_id=tag + ":A", objective=tag + ": " + params["objective"], input_artifact_ids=[source["artifact_id"]])
        prepared = await prepare_ready_async(atoms, key=tag + ":prepare", **params)
        run_id = prepared["run_id"]
        report["run_id"] = run_id
        replay = await atoms.prepare(key=tag + ":prepare", **params)
        assert replay["run_id"] == run_id
        current = await atoms.get_run(run_id)
        frozen = freeze_run_context(prepared, current)
        assert frozen["recipe"]["recipe_id"] == seeded["recipe_id"]
        for node in ("prepare", "produce", "verify"):
            step = await atoms.get_recipe_step(run_id, node)
            assert step["node"]["id"] == node

        guidance_data = section(transcript, 15, request=True)["data"]
        guidance_data["request_key"] = tag + ":choose"
        guidance = await atoms.guidance(run_id, data=guidance_data)
        decision_id = guidance["decision_id"]
        action = guidance["decision"]["recommended_action_id"]
        assert action in {"daily_aggregate", "transfer_reference_match"}
        candidates = fixture["bank_entries"]
        selected = [e for e in candidates if e["currency"] == fixture["currency"] and
                    ((e["date"] == fixture["settlement_date"]) if action == "daily_aggregate" else
                     (e["batch_id"] == fixture["batch_id"] and e["transfer_id"] == fixture["transfer_id"]))]
        net = sum((Decimal(o["gross"]) - Decimal(o["commission"]) for o in fixture["orders"]), Decimal(0))
        bank = sum((Decimal(e["amount"]) for e in selected), Decimal(0))
        delta = bank - Decimal(fixture["platform_payout"])
        actual = {"order_net": str(net), "platform_payout": fixture["platform_payout"], "bank_amount": str(bank),
                  "delta": f"{delta:.2f}", "classification": "matched" if delta == 0 else "shortfall" if delta < 0 else "surplus",
                  "selected_bank_entry_ids": [e["id"] for e in selected], "read_only": True}
        events = [{"id": tag + ":plan", "kind": "agent_action", "payload": {"decision_id": decision_id, "action": action, "executor": "local Decimal validation"}},
                  {"id": tag + ":result", "kind": "tool_result", "payload": actual}]
        for _ in range(2):
            ack = await atoms.session_events(run_id, events)
            assert set(ack["durable_ack"]) == {e["id"] for e in events}
        await atoms.checkpoint(run_id, data={"phase": "validation-complete", "decision_id": decision_id, "actual_action_id": action, "next_state": actual})
        decision = await poll(lambda: atoms.get_decision(run_id, decision_id), lambda x: bool(x.get("experience", {}).get("experience_id")) and any(w.get("kind") == "upsert" and w.get("state") == "done" for w in x.get("writes", [])))
        experience_id = decision["experience"]["experience_id"]
        pending = await atoms.get_memory_experience(experience_id)
        report["pending_experience"] = pending
        output = await atoms.artifact(name=tag + "-result.json", content=dump(actual))
        submitted = await atoms.submit(run_id, data={"summary": tag + " synthetic check", "business_result": actual, "artifacts": [dict(output)]})
        assert submitted["status"] == "completed"
        expected = section(transcript, 26)["expected"]
        checks = {k: actual[k] == expected[k] for k in expected}
        passed = all(checks.values())
        evidence_data = {"expected": expected, "actual": actual, "checks": checks, "passed": passed, "reward": 1 if passed else -1, "scope": "synthetic fixture only; locally recomputed, no real financial tool or model called"}
        evidence = await atoms.artifact(name=tag + "-evaluation.json", content=dump(evidence_data))
        await atoms.feedback(run_id, data={"type": "acceptance" if passed else "correction", "source": "local_synthetic_fixture", "checks": checks, "evidence_artifact_id": evidence["artifact_id"], "actual_user_acceptance": False})
        before_evaluation = await atoms.get_memory_experience(experience_id)
        assert before_evaluation["revision"] == pending["revision"], "Feedback must not self-verify the experience"
        await client.admin.atoms.evaluate_decision(run_id, decision_id, evaluation={"evaluation_id": tag + ":oracle", "reward_spec": "verified-decision-return-v1", "status": "verified", "value": evidence_data["reward"], "evidence_artifact_id": evidence["artifact_id"], "evidence_content_hash": evidence["content_hash"]})
        verified = await poll(lambda: atoms.get_memory_experience(experience_id), lambda x: x.get("revision", 0) > pending["revision"])
        assert verified["record"]["outcome"]["status"] == "verified"
        assert verified["record"]["outcome"]["value"] == evidence_data["reward"]
        historical = await atoms.get_memory_experience(experience_id, revision=1)
        assert historical["revision"] == 1
        report["experience_id"] = experience_id
        report["experience_revision"] = verified["revision"]
        report["evaluation"] = {"action": action, "passed": passed, "reward": evidence_data["reward"]}
        report.pop("pending_experience", None)

        record = {"evaluation_id": tag + ":recipe", "task_id": fixture["batch_id"], "task_lineage_id": params["task_lineage_id"],
                  "task_sha256": prepared["task_package_hash"].removeprefix("sha256:"), "bundle_sha256": seeded["bundle_hash"].removeprefix("sha256:"),
                  "protocol_sha256": digest(expected), "session_sha256": digest(events), "task_prompt": params["objective"],
                  "judge_valid": True, "score": int(passed), "task_success": passed, "actual_action_id": action,
                  "observed_result": actual, "checks": checks, "evaluator": "local-synthetic-fixture-v1"}
        episode = await atoms.artifact(name=tag + "-recipe-episode.json", content=dump(record))
        recipe_write = await client.admin.atoms.evaluate_recipe(run_id, record=record, artifact_id=episode["artifact_id"], content_hash=episode["content_hash"])
        report["recipe_evaluation"] = dict(recipe_write)
        trace = await atoms.get_trace(run_id)
        ids = [event["id"] for event in trace["session_events"]]
        assert all(ids.count(event["id"]) == 1 for event in events)
        report["trace_keys"] = list(trace)
        report["prepare_idempotency"] = "passed"
        report["durable_ack_replay"] = "passed"
        report["run_context_validation"] = "passed"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--transcript", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--write", action="store_true")
    args = parser.parse_args()
    if args.write and not args.transcript:
        parser.error("--write requires --transcript")
    key = os.getenv("RSI_API_KEY") or os.getenv("COSENS_API_KEY") or getpass.getpass("Friday API Key (hidden): ")
    service = resolve_service()
    report = {"started_at": datetime.now(timezone.utc).isoformat(), "gateway": service.url, "region": service.region, "requests": []}
    try:
        asyncio.run(validate(args, key, report))
        report["result"] = "passed"
    except Exception as exc:
        report["result"] = "failed"
        report["error"] = f"{type(exc).__name__}: {exc}".replace(key, "<redacted>")
        raise
    finally:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2).replace(key, "<redacted>"))
        print(f"Saved validation report: {args.output}", flush=True)


if __name__ == "__main__":
    main()
