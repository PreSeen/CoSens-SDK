"""Shared fakes for the local RSI tests."""


class FakeModel:
    identity = "fake-model"
    timeout = 5

    def __init__(self, *replies, feedback_actionable=False):
        self.replies = list(replies)
        self.calls = []
        # With feedback_actionable, an attribution call the test did not script with attribution_reply is answered as
        # actionable and kept in ``attributions``, not ``calls``: every learning now has feedback on its chain.
        self.feedback_actionable = feedback_actionable
        self.attributions = []

    async def call(self, *, instruction, schema, context):
        if (self.feedback_actionable and context["role"] == "workflow_recipe_attribution"
                and not (self.replies and getattr(self.replies[0], "attribution", False))):
            self.attributions.append(context)
            return attribution_reply(True)(context)
        self.calls.append({"instruction": instruction, "schema": schema, "context": context})
        reply = self.replies.pop(0)
        if isinstance(reply, BaseException):
            raise reply
        return reply(context) if callable(reply) else reply


def recipe_reply(title="Recipe"):
    def reply(payload):
        latest = payload["context"]["history"][-1]
        ref = {"execution_id": latest["execution_id"], "event_id": latest["events"][0]["id"]}
        return {"title": title, "when": "always", "when_not": "never",
                "steps": [{"id": "s1", "instruction": "do it", "evidence_refs": [ref]}], "lessons": []}
    return reply


def attribution_reply(actionable, *, verified_success=False, new_evidence=False):
    def reply(payload):
        feedback = next(f for h in payload["context"]["history"] for f in h["feedback"])
        found = [{"observation": "o", "why_missing": "w", "evidence_refs": [{"feedback_id": feedback["feedback_id"]}]}]
        return {"actionable": actionable, "verified_success": verified_success, "new_evidence": found if new_evidence else [],
                "limitations": "",
                "possible_causes": [{"reason": "r", "uncertainty": "u", "evidence_refs": [{"feedback_id": feedback["feedback_id"]}]}]}
    reply.attribution = True
    return reply


def make_rsi(tmp_path, *replies, client=None, name="state.sqlite3"):
    from cosens.local import LocalRSI

    model = FakeModel(*replies, feedback_actionable=True)
    return LocalRSI(client=client, model=model, state_path=tmp_path / name), model


async def complete_execution(rsi, started, key="out", feedback="Keep this method"):
    """Submit the execution's result and, unless ``feedback`` is None, record it as the user's words: learning needs
    feedback on the chain."""
    artifact = await rsi.artifact(name=key, content="answer " + key)
    done = await rsi.submit_execution(
        started.execution_id, request_sha256=started.request_sha256, outcome="completed",
        artifacts=[{"artifact_id": artifact.artifact_id, "role": "submitted_output"}],
        events=[{"id": "e1", "kind": "tool_call", "payload": {"name": "x"}}], trace_coverage="full")
    if feedback is not None:
        await rsi.record_feedback(done.revision_id, feedback=feedback, source="human", key=key + "-feedback")
    return done


async def first_revision(rsi, key="t1", objective="Do the task", recall_memory=False):
    task = await rsi.create_task(key=key, objective=objective)
    started = await rsi.start_execution(task.task_id, key=key + "-e", source="new_task", objective=objective,
                                       recall_memory=recall_memory)
    done = await complete_execution(rsi, started, key=key + "-out")
    return task, started, done
