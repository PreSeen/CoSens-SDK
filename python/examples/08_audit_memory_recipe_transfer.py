"""Read back this experiment's artifacts and independently recheck saved outputs."""
import hashlib
from importlib import import_module
import json
import time

from dotenv import load_dotenv

runner = import_module("06_memory_recipe_transfer")


def main():
    load_dotenv(runner.ROOT / ".env", override=False)
    folder = runner.ROOT / "python/docs/testing/latest-memory-recipe-20261102"
    state = json.loads((folder / "state.json").read_text())
    case = runner.build_case(state["tag"])
    sdk = runner.CoSensClient(state_path=str(folder / "audit-state.sqlite3"))
    sources = {state["tag"] + ":" + key: text for key,text in case["sources"]}
    report = {"audited_at": time.time(), "tag": state["tag"], "sdk_source": str(runner.ROOT / "python/src/cosens"),
              "business_task_count": 2, "synthetic": True, "model": state["protocol"]["model"],
              "results": {}, "blockers": list({runner.dump(b):b for b in state["blockers"]}.values()),
              "source_files": {}, "recipe_transfer_validated": False, "human_acceptance": False,
              "interpretation": ["Original and supplemental runs remain separate; no best-of selection.",
                  "No-evidence abstention is not a fabricated business error; report completion separately.",
                  "Model output with a prose wrapper is a format violation, even when its existing JSON can be evaluated.",
                  "Task1 passed initially; no observed corrective learning gain.",
                  "No native initial or final Recipe was generated, so no Recipe transfer effect is estimated."]}
    try:
        for name,actor in state["actors"].items():
            item = {"execution_status": actor["status"], "model_calls": len(actor["calls"]),
                    "usage": {field: sum((c.get("usage") or {}).get(field,0) for c in actor["calls"])
                              for field in ("prompt_tokens", "completion_tokens", "total_tokens")},
                    "memory_recall_calls": sum(t["name"] == "memory_recall" for t in actor["tools"]),
                    "model_extra_body": actor.get("model_extra_body"), "memory_masked": actor.get("memory_masked",False),
                    "execution_id": state["calls"][name + ":execution"]["response"]["execution_id"]}
            execution = sdk.sync_atoms.get_execution(item["execution_id"])
            item["remote_execution_status"] = execution.get("status")
            if actor["status"] != "completed":
                item["failure_reason"] = actor.get("execution_failure")
                report["results"][name] = item
                continue
            evaluation = runner.grade(case, actor["task"], actor["output"], set(actor["evidence_ids"]))
            item["evaluation"] = {k:v for k,v in evaluation.items() if k != "checks"}
            artifact = state["calls"][name + ":output"]["response"]
            remote = sdk.sync_atoms.get_artifact(artifact["artifact_id"])
            item["artifact_id"] = artifact["artifact_id"]
            item["artifact_content_readback_matches"] = remote.get("content") == runner.dump(actor["output"])
            item["artifact_hash_receipt_matches"] = remote.get("content_hash") == artifact["content_hash"]
            item["output_hash"] = hashlib.sha256(runner.dump(actor["output"]).encode()).hexdigest()
            path = folder / name.replace(":", "-")
            response = sorted(path.glob("response-*.json"))[-1]
            text = json.loads(response.read_text())["choices"][0]["message"].get("content") or ""
            parsed, strict = runner.parse_report(text)
            item["raw_model_json_matches_evaluated_object"] = parsed == actor["output"]
            item["strict_json"] = strict
            item["delivery_strict_pass"] = strict and evaluation["strict_pass"]
            evidence = {i["id"]:i for tool in actor["tools"] if tool["name"] == "memory_recall"
                        for i in tool["result"].get("items",[]) if i.get("id")}
            item["recalled_source_count"] = len(evidence)
            item["memory_source_integrity"] = all(
                eid in sources and record.get("text") == sources[eid]
                and any(ref.get("source_id") == eid and ref.get("revision") == 1
                        and ref.get("content_hash") == hashlib.sha256(sources[eid].encode()).hexdigest()
                        for ref in record.get("refs", [])) for eid,record in evidence.items())
            cited = {ref for row in actor["output"].get("rows",[]) for ref in row.get("evidence_ids",[])}
            item["cited_expired_or_other_company"] = sorted(ref for ref in cited if ref.endswith((":expired-policy",":other-company")))
            item["row_statuses"] = {row["batch_id"]:row["status"] for row in actor["output"]["rows"]}
            # Match the type of supporting source to each asserted field, in
            # addition to the evaluator's basic ID existence check.
            support = {}
            for row in actor["output"]["rows"]:
                if row["status"] == "unknown":
                    continue
                required = {"accounting", "current-policy"}
                if row.get("received_cents",0) or row["status"] == "unknown_party":
                    required.add("counterparties")
                actual = {r.rsplit(":",1)[-1] for r in row.get("evidence_ids",[])}
                support[row["batch_id"]] = {"required_sources": sorted(required), "cited": sorted(actual), "supported": required <= actual}
            item["field_source_support"] = support
            report["results"][name] = item
        report["task_ids"] = {t:state["calls"][t+":task"]["response"]["task_id"] for t in ("task1","task2")}
        report["native_recipe_candidates"] = sdk.sync_atoms.list_recipe_candidates(task_id=report["task_ids"]["task1"])
        attribution_id = state["calls"]["task1:attribution"]["response"]["attribution_id"]
        report["live_attribution_status"] = sdk.sync_atoms.get_attribution(attribution_id).get("status")
        report["live_readiness"] = sdk.sync_atoms._http.request("GET","/health/ready")
        for filename in ("06_memory_recipe_transfer.py", "07_validate_memory_dependency.py", "08_audit_memory_recipe_transfer.py", "memory_recipe_case_fixture.py"):
            content = (runner.ROOT / "python/examples" / filename).read_bytes()
            report["source_files"][filename] = hashlib.sha256(content).hexdigest()
            # Final harness snapshot; the first run's protocol and each raw API
            # response remain preserved separately, including harness failures.
            snapshot = folder / "harness-snapshot"
            snapshot.mkdir(exist_ok=True)
            (snapshot / filename).write_bytes(content)
        (folder / "audit.json").write_text(runner.dump(report))
        (folder / "audit.json").chmod(0o600)
        print(runner.dump({"audit":str(folder / "audit.json"), "native_recipe_candidates":report["native_recipe_candidates"],
            "results": {k:{"execution_status":v["execution_status"],"evaluation":v.get("evaluation"),"strict_json":v.get("strict_json"),
                           "remote_status":v["remote_execution_status"],"source_integrity":v.get("memory_source_integrity"),
                           "artifact_matches":v.get("artifact_content_readback_matches")} for k,v in report["results"].items()}}))
    finally:
        sdk.close()


if __name__ == "__main__":
    main()
