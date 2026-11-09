import json
from pathlib import Path

import pytest

from cosens.local import recipe_stages as stages
from local_rsi_helpers import FakeModel

CONTRACT = Path(__file__).resolve().parents[2] / "contract" / "local-rsi"
VALIDATION = json.loads((CONTRACT / "validation.json").read_text())
EVENT_REF = {"revision_id": "rev1", "event_id": "e1"}


def context(feedback=()):
    return {"history": [{"revision_id": "rev1", "materials": [], "outputs": [{"artifact_id": "out1"}],
                         "events": [{"id": "e1"}], "feedback": list(feedback)}],
            "memory": {"status": "not_requested", "items": []}}


NEW_EVIDENCE = [{"observation": "O", "why_missing": "W", "evidence_refs": [EVENT_REF]}]


def attribution(*, verified_success=False, new_evidence=()):
    return {"actionable": False, "verified_success": verified_success, "new_evidence": list(new_evidence),
            "limitations": "", "possible_causes": []}


def recipe(**changes):
    return {"title": "T", "when": "W", "when_not": "N", "lessons": [],
            "steps": [{"id": "s1", "instruction": "I", "evidence_refs": [EVENT_REF]}], **changes}


@pytest.mark.parametrize("case", VALIDATION["cases"], ids=lambda c: c["name"])
def test_contract_validation(case):
    assert stages.classify_output(case["stage"], case["output"], VALIDATION["context"]) == case["expected"]


def test_contract_rejects_recipe_over_the_text_limit():
    long_title = {"title": "T" * 300, "when": "W" * 600, "when_not": "N" * 600}
    steps = [{"id": f"s{i}", "instruction": "I" * 600, "evidence_refs": [EVENT_REF]} for i in range(8)]
    assert stages.classify_output("recipe", recipe(**long_title, steps=steps), context()) == "too_long"


def test_prompts_match_the_contract():
    prompts = json.loads((CONTRACT / "prompts.json").read_text())
    assert prompts == {"attribution": stages.ATTRIBUTION_INSTRUCTION, "generation_start": stages.GENERATION_START,
                       "verified_lesson_source": stages.VERIFIED_LESSON_SOURCE, "generation_end": stages.GENERATION_END,
                       "verified_distill": stages.VERIFIED_DISTILL_INSTRUCTION,
                       "acknowledged_only": stages.ACKNOWLEDGED_ONLY_INSTRUCTION, "improve": stages.IMPROVE_INSTRUCTION}


def test_every_schema_field_is_required_for_structured_output():  # providers reject optional properties
    for schema in (stages.RECIPE_SCHEMA, stages.ATTRIBUTION_SCHEMA):
        assert set(schema["required"]) == set(schema["properties"])


async def test_no_feedback_skips_attribution_and_generates():
    model = FakeModel(recipe())
    phases = []
    outcome = await stages.run_recipe_stages(model, context(), None, on_phase=phases.append)
    assert outcome.recipe["title"] == "T"
    assert phases == ["generating"]
    assert model.calls[0]["context"]["role"] == "workflow_recipe_distillation"
    assert outcome.models == {"attribution": None, "generation": "fake-model"}


async def test_gate_keeps_previous_version_when_feedback_needs_no_change():
    feedback = [{"feedback_id": "fb1"}]
    model = FakeModel(attribution())
    outcome = await stages.run_recipe_stages(model, context(feedback), {"recipe": recipe()}, on_phase=lambda _: None)
    assert outcome.recipe is None and outcome.material_gap == stages.NO_ACTIONABLE_FEEDBACK
    assert len(model.calls) == 1


async def test_verified_new_evidence_updates_the_recipe():
    feedback = [{"feedback_id": "fb1", "source": "evaluator"}]
    model = FakeModel(attribution(verified_success=True, new_evidence=NEW_EVIDENCE), recipe())
    outcome = await stages.run_recipe_stages(model, context(feedback), {"recipe": recipe()}, on_phase=lambda _: None)
    assert outcome.recipe is not None
    instruction = model.calls[1]["instruction"]
    assert stages.VERIFIED_DISTILL_INSTRUCTION in instruction and stages.VERIFIED_LESSON_SOURCE in instruction
    assert stages.ACKNOWLEDGED_ONLY_INSTRUCTION not in instruction


@pytest.mark.parametrize("source,verified,evidence,gap", [
    ("human", True, NEW_EVIDENCE, stages.UNVERIFIED_NEW_EVIDENCE),
    ("evaluator", False, NEW_EVIDENCE, stages.UNVERIFIED_NEW_EVIDENCE),
    ("evaluator", True, [], stages.NO_ACTIONABLE_FEEDBACK),
], ids=["human_approval", "model_did_not_confirm", "nothing_new"])
async def test_new_evidence_is_kept_out_unless_verified(source, verified, evidence, gap):
    feedback = [{"feedback_id": "fb1", "source": source}]
    model = FakeModel(attribution(verified_success=verified, new_evidence=evidence))
    outcome = await stages.run_recipe_stages(model, context(feedback), {"recipe": recipe()}, on_phase=lambda _: None)
    assert outcome.recipe is None and outcome.material_gap == gap


async def test_only_the_latest_revisions_feedback_can_verify():
    older = {"revision_id": "rev0", "materials": [], "outputs": [], "events": [], "feedback": [{"feedback_id": "fb0", "source": "evaluator"}]}
    latest = context([{"feedback_id": "fb1", "source": "human"}])
    latest["history"].insert(0, older)
    model = FakeModel(attribution(verified_success=True, new_evidence=NEW_EVIDENCE))
    outcome = await stages.run_recipe_stages(model, latest, {"recipe": recipe()}, on_phase=lambda _: None)
    assert outcome.recipe is None


async def test_new_evidence_with_an_unknown_reference_is_rewritten():
    feedback = [{"feedback_id": "fb1", "source": "evaluator"}]
    bad = [{"observation": "o", "why_missing": "w", "evidence_refs": [{"artifact_id": "nope"}]}]
    model = FakeModel(attribution(verified_success=True, new_evidence=bad), attribution(), )
    await stages.run_recipe_stages(model, context(feedback), {"recipe": recipe()}, on_phase=lambda _: None)
    assert "Previous response was rejected" in model.calls[1]["instruction"]


async def test_without_a_parent_unactionable_feedback_still_generates_without_lessons():
    feedback = [{"feedback_id": "fb1"}]
    model = FakeModel(attribution(), recipe())
    outcome = await stages.run_recipe_stages(model, context(feedback), None, on_phase=lambda _: None)
    assert outcome.recipe is not None
    assert stages.ACKNOWLEDGED_ONLY_INSTRUCTION in model.calls[1]["instruction"]


async def test_parent_uses_the_improvement_role():
    model = FakeModel(recipe())
    await stages.run_recipe_stages(model, context(), {"recipe": recipe()}, on_phase=lambda _: None)
    assert model.calls[0]["context"]["role"] == "workflow_recipe_improvement"
    assert stages.IMPROVE_INSTRUCTION in model.calls[0]["instruction"]


async def test_rejected_output_is_retried_with_the_reason():
    bad = recipe(steps=[{"id": "s1", "instruction": "I", "evidence_refs": [{"artifact_id": "nope"}]}])
    model = FakeModel(bad, recipe())
    outcome = await stages.run_recipe_stages(model, context(), None, on_phase=lambda _: None)
    assert outcome.recipe["title"] == "T"
    assert "Previous response was rejected" in model.calls[1]["instruction"]
    assert "nope" in model.calls[1]["instruction"]


async def test_three_rejections_raise():
    bad = recipe(steps=[{"id": "s1", "instruction": "I", "evidence_refs": [{"artifact_id": "nope"}]}])
    model = FakeModel(bad, bad, bad)
    with pytest.raises(stages.ModelOutputInvalid):
        await stages.run_recipe_stages(model, context(), None, on_phase=lambda _: None)
    assert len(model.calls) == 3


async def test_invalid_json_counts_as_a_rejection():
    model = FakeModel(ValueError("not json"), recipe())
    outcome = await stages.run_recipe_stages(model, context(), None, on_phase=lambda _: None)
    assert outcome.recipe is not None


def memory_item(item_id, score, text):
    return {"id": item_id, "score": score, "text": text,
            "refs": [{"source_id": item_id, "revision": 1, "content_hash": "h", "start": 0, "end": 1}]}


def test_memory_snapshot_filters_weak_duplicate_and_unreferenced_items():
    items = [memory_item("a", 0.9, "alpha beta gamma"), memory_item("b", 0.1, "weak"),
             memory_item("c", 0.8, "alpha beta gamma"), {"id": "d", "score": 0.9, "text": "no refs"}]
    snapshot = stages.memory_snapshot(items)
    assert [item["id"] for item in snapshot["items"]] == ["a"]
    assert (snapshot["omitted_low_relevance"], snapshot["omitted_duplicate"], snapshot["omitted_unreferenced"]) == (1, 1, 1)


