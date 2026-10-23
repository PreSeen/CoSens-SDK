import json

import pytest

from memory.models import ClaimDraft, InvalidEvidence, SourceRef
from memory.verification import SemanticVerifier, prepare_verification


def candidate():
    return ClaimDraft(
        subject="Project",
        predicate="OWNER",
        value="Alice",
        text="Project OWNER Alice",
        refs=(SourceRef("s", 1, 0, 19, "hash"),),
        valid_from="2025-01-01T00:00:00Z",
    )


def verdict(**changes):
    return dict(text_supported=True, relation_supported=True, time_supported=True, no_contradiction=True, **changes)


@pytest.mark.parametrize("failed", [None, "text_supported", "relation_supported", "time_supported", "no_contradiction"])
def test_all_support_dimensions_required(failed):
    result = verdict()
    if failed:
        result[failed] = False
    seen = []

    def complete(messages, **kwargs):
        seen.append((messages, kwargs))
        return json.dumps(result)

    verifier = SemanticVerifier(complete, model_id="test-only")
    d = candidate()
    prepared = prepare_verification(verifier, [d], [[d.text]])
    assert prepared.supports(d, [d.text]) == (failed is None)
    payload = json.loads(seen[0][0][1]["content"])
    assert payload["candidate"]["valid_from"] == d.valid_from
    assert payload["quotes"] == [d.text]
    assert seen[0][1] == {"max_tokens": 512, "temperature": 0}


@pytest.mark.parametrize("raw", ["invalid", "{}", "[]", '{"text_supported":"true"}'])
def test_malformed_verdict_is_not_negative_success(raw):
    verifier = SemanticVerifier(lambda *a, **k: raw, model_id="test-only")
    with pytest.raises(InvalidEvidence):
        verifier.supports(candidate(), ["quote"])


def test_input_cap_prevents_model_call():
    def unexpected(*args, **kwargs):
        raise AssertionError("must not call model")

    verifier = SemanticVerifier(unexpected, model_id="test-only", max_input_chars=10)
    with pytest.raises(InvalidEvidence):
        verifier.supports(candidate(), ["quote"])
