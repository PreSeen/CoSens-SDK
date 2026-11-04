"""Agents and CoSens reuse COSENS_API_KEY through the regional model gateway."""
from agents import Agent, set_default_openai_client, set_tracing_disabled
from cosens import cosens, friday_gateway_client


async def run_task(task, model):
    set_default_openai_client(friday_gateway_client(asynchronous=True))
    set_tracing_disabled(True)
    agent = Agent(name="assistant", instructions="Complete the requested task.", model=model)
    client = cosens(agent, mode="auto")
    result = await client.run(agent, task)
    return client, result, client.cosens.last_turn_id


async def on_artifact_confirmed(client, turn_id, request_key):
    return await client.cosens.generate_recipe(turn_id, request_key=request_key)
