"""Learn a Recipe on this machine. Set LOCAL_RSI_MODEL=codex to use your Codex login, or MODEL_BASE_URL, MODEL_API_KEY
and MODEL_ID for any OpenAI-compatible provider. COSENS_API_KEY is optional and only enables Memory."""

import asyncio
import os
import tempfile

from openai import AsyncOpenAI

from cosens import CoSensClient
from cosens.local import ChatCompletionsModel, CodexCLIModel, LocalRSI


def build_model():
    if os.getenv("LOCAL_RSI_MODEL") == "codex":
        return CodexCLIModel()
    client = AsyncOpenAI(base_url=os.environ["MODEL_BASE_URL"], api_key=os.environ["MODEL_API_KEY"])
    return ChatCompletionsModel(client, os.environ["MODEL_ID"])


async def main():
    memory = CoSensClient() if os.getenv("COSENS_API_KEY") else None
    with tempfile.TemporaryDirectory() as directory:
        rsi = LocalRSI(memory=memory, model=build_model(), state_path=f"{directory}/state.sqlite3")
        task = await rsi.create_task(source="new_task", key="demo", objective="Sort a list of words alphabetically.")
        started = await rsi.start_execution(task.task_id, key="demo-run")
        # Your own Agent does the work here; this example records a finished result.
        output = await rsi.artifact(name="answer.txt", content="apple, banana, cherry")
        finished = await rsi.submit_execution(
            started.execution_id, request_sha256=started.request_sha256, outcome="completed",
            artifacts=[{"artifact_id": output.artifact_id, "role": "submitted_output"}], trace_coverage="full",
            events=[{"id": "sort", "kind": "agent_message", "payload": {"text": "Sorted the words with a case-insensitive comparison."}}])
        candidate = await rsi.start_recipe_candidate(task.task_id, revision_id=finished.revision_id)
        job = await rsi.wait_workflow_job("recipe_candidate", candidate.recipe_candidate_id)
        print(job.status, job.recipe.title)
        rsi.close()
    if memory is not None:
        await memory.aclose()


asyncio.run(main())
