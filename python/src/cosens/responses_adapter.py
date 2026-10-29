"""Normalize supported OpenAI generation requests without changing originals."""

import json


SUPPORTED = {
    ("responses", "create"),
    ("responses", "parse"),
    ("responses", "stream"),
    ("chat", "completions", "create"),
    ("chat", "completions", "parse"),
    ("chat", "completions", "stream"),
    ("completions", "create"),
}


def base_path(path):
    return tuple(p for p in path if p not in {"with_raw_response", "with_streaming_response"})


def user_text(path, kwargs):
    base = base_path(path)
    if base[0] == "responses":
        value = kwargs.get("input")
    elif base[:2] == ("chat", "completions"):
        value = kwargs.get("messages")
    else:
        value = kwargs.get("prompt")
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        texts = []
        item = next((item for item in reversed(value) if isinstance(item, dict) and item.get("role") == "user"), None)
        content = item.get("content") if item else None
        if isinstance(content, str):
            texts.append(content)
        elif isinstance(content, list):
            texts.extend(part["text"] for part in content if isinstance(part, dict) and part.get("type") in {"text", "input_text"} and isinstance(part.get("text"), str))
        return "\n".join(texts)[-8000:]
    return ""


def tool_names(kwargs):
    names = []
    for tool in kwargs.get("tools") or []:
        if isinstance(tool, dict):
            name = tool.get("name") or (tool.get("function") or {}).get("name")
            if isinstance(name, str):
                names.append(name)
    return names


def inject(path, kwargs, run):
    frozen = {"recipe": run.get("recipe"), "snapshot": run.get("snapshot"), "context": run.get("context")}
    if not any(frozen.values()):
        return dict(kwargs)
    source = "RSI Run " + str(run["run_id"]) if run.get("run_id") else "RSI standalone recall"
    block = "\n\n[CoSens frozen context; source: " + source + "]\n" + json.dumps(frozen, ensure_ascii=False, default=str)
    updated = dict(kwargs)
    base = base_path(path)
    if base[0] == "responses":
        instructions = updated.get("instructions")
        if instructions is not None and not isinstance(instructions, str):
            raise TypeError("Cannot safely append CoSens context to non-text Responses instructions")
        updated["instructions"] = (instructions or "") + block
    elif base[:2] == ("chat", "completions"):
        messages = updated.get("messages")
        if not isinstance(messages, list):
            raise TypeError("Cannot safely append CoSens context to non-list chat messages")
        updated["messages"] = list(messages) + [{"role": "developer", "content": block}]
    elif base[0] == "completions":
        prompt = updated.get("prompt")
        if not isinstance(prompt, str):
            raise TypeError("Cannot safely append CoSens context to non-text completion prompt")
        updated["prompt"] = block + "\n" + prompt
    return updated


def terminal(response):
    if getattr(response, "status", "completed") not in {"completed", None}:
        return False
    outputs = getattr(response, "output", None)
    if outputs is not None:
        types = [getattr(item, "type", None) if not isinstance(item, dict) else item.get("type") for item in outputs]
        return "message" in types and "function_call" not in types
    choices = getattr(response, "choices", None)
    if choices is not None:
        return bool(choices) and all(getattr(choice, "finish_reason", None) not in {"tool_calls", "function_call", None} for choice in choices)
    return bool(getattr(response, "output_text", None))


def tool_continuation(response):
    if getattr(response, "status", "completed") not in {"completed", None}:
        return False
    outputs = getattr(response, "output", None)
    if outputs is not None:
        return any((item.get("type") if isinstance(item, dict) else getattr(item, "type", None)) == "function_call" for item in outputs)
    choices = getattr(response, "choices", None)
    if choices is not None:
        return any(getattr(choice, "finish_reason", None) in {"tool_calls", "function_call"} for choice in choices)
    return False


def summary(response):
    text = getattr(response, "output_text", None)
    if text is not None:
        return str(text)
    choices = getattr(response, "choices", None)
    if choices:
        message = getattr(choices[0], "message", None)
        if message is not None:
            return str(getattr(message, "content", ""))
        return str(getattr(choices[0], "text", ""))
    return ""
