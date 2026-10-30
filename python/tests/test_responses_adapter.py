from cosens.responses_adapter import user_text


def test_user_text_ignores_non_string_multimodal_payloads():
    value = user_text(("responses", "create"), {"input": [
        {"role": "user", "content": [
            {"type": "input_text", "text": "hello"},
            {"type": "input_text", "text": {"value": "not text"}},
            {"type": "input_image", "image_url": "data:image/png;base64,..."},
        ]},
    ]})
    assert value == "hello"


def test_chat_classification_uses_only_latest_user_message():
    value = user_text(("chat", "completions", "create"), {"messages": [
        {"role": "user", "content": "old task"},
        {"role": "assistant", "content": "old answer"},
        {"role": "user", "content": [{"type": "text", "text": "current "}, {"type": "text", "text": "task"}]},
    ]})
    assert value == "current \ntask"
