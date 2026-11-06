"""Agents use a standard AsyncOpenAI client; COSENS_API_KEY authenticates both the model gateway and CoSens."""
import os

from agents import Agent, set_default_openai_client, set_tracing_disabled
from openai import AsyncOpenAI
from cosens import cosens


async def run_task(task, model):
    set_default_openai_client(AsyncOpenAI(base_url=os.environ["COSENS_MODEL_BASE_URL"], api_key=os.environ["COSENS_API_KEY"]))
    set_tracing_disabled(True)
    agent = Agent(name="assistant", instructions="Complete the requested task.", model=model)
    client = cosens(agent, mode="auto")
    result = await client.run(agent, task)
    return client, result, client.cosens.last_turn_id


async def on_artifact_confirmed(client, turn_id, request_key):
    return await client.cosens.generate_recipe(turn_id, request_key=request_key)
