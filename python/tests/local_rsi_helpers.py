"""Shared fakes for the local RSI tests."""


class FakeModel:
    identity = "fake-model"
    timeout = 5

    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls = []

    async def call(self, *, instruction, schema, context):
        self.calls.append({"instruction": instruction, "schema": schema, "context": context})
        reply = self.replies.pop(0)
        if isinstance(reply, BaseException):
            raise reply
        return reply(context) if callable(reply) else reply


def recipe_reply(title="Recipe"):
    def reply(payload):
        latest = payload["context"]["history"][-1]
        ref = {"revision_id": latest["revision_id"], "event_id": latest["events"][0]["id"]}
        return {"title": title, "when": "always", "when_not": "never",
                "steps": [{"id": "s1", "instruction": "do it", "evidence_refs": [ref]}], "lessons": []}
    return reply


def attribution_reply(actionable):
    def reply(payload):
        feedback = next(f for h in payload["context"]["history"] for f in h["feedback"])
        return {"actionable": actionable, "limitations": "",
                "possible_causes": [{"reason": "r", "uncertainty": "u", "evidence_refs": [{"feedback_id": feedback["feedback_id"]}]}]}
    return reply


def make_rsi(tmp_path, *replies, memory=None, name="state.sqlite3"):
    from cosens.local import LocalRSI

    model = FakeModel(*replies)
    return LocalRSI(memory=memory, model=model, state_path=tmp_path / name), model


async def complete_execution(rsi, started, key="out"):
    artifact = await rsi.artifact(name=key, content="answer " + key)
    return await rsi.submit_execution(
        started.execution_id, request_sha256=started.request_sha256, outcome="completed",
        artifacts=[{"artifact_id": artifact.artifact_id, "role": "submitted_output"}],
        events=[{"id": "e1", "kind": "tool_call", "payload": {"name": "x"}}], trace_coverage="full")


async def first_revision(rsi, key="t1", objective="Do the task", recall_memory=False):
    task = await rsi.create_task(source="new_task", key=key, objective=objective)
    started = await rsi.start_execution(task.task_id, key=key + "-e", recall_memory=recall_memory)
    done = await complete_execution(rsi, started, key=key + "-out")
    return task, started, done
