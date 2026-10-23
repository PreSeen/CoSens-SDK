from dataclasses import replace

import pytest

from memory.models import Evidence, SourceRef
from memory.retrieval import RetrievalConfig, rank


def sample():
    text = "Zephyr launch audit failed. " * 50
    return [
        Evidence("a", "source", text, (SourceRef("s", 1, 0, len(text), "h"),)),
        Evidence("b", "source", "Zephyr release owner is Mei.", (SourceRef("t", 1, 0, 28, "j"),)),
    ]


def test_default_is_exact_backward_compatible():
    assert rank("Zephyr launch", sample(), {}) == rank("Zephyr launch", sample(), {}, RetrievalConfig())


def test_dense_only_and_missing_dense():
    config = RetrievalConfig(lexical_weight=0)
    assert rank("Zephyr launch", sample(), {}, config) == []
    assert [x.id for x in rank("Zephyr launch", sample(), {"b": 0.8}, config)] == ["b"]


def test_zero_channels_rejected():
    with pytest.raises(ValueError):
        RetrievalConfig(lexical_weight=0, dense_weight=0)


def test_modules_disable_by_numeric_config_without_case_routing():
    config = RetrievalConfig(passage_chars=2_000_000, redundancy_weight=0, source_weight=0)
    output = rank("Zephyr launch", sample(), {}, config)
    assert output[0].text == sample()[0].text
    assert output[0].score >= output[1].score
    compressed = rank("Zephyr launch", sample(), {}, replace(config, passage_chars=560))
    assert all(len(x.text) <= 560 for x in compressed)


@pytest.mark.parametrize(
    "kwargs", [{"redundancy_weight": float("nan")}, {"source_weight": -1}, {"passage_chars": 0}, {"rerank_limit": 1000}]
)
def test_invalid_config_fails_before_evaluation(kwargs):
    with pytest.raises(ValueError):
        RetrievalConfig(**kwargs)
