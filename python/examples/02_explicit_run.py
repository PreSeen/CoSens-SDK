"""The host executes Agents; the service only records Task/Execution."""
from agents import Agent
from cosens import cosens


async def run_task(task):
    agent = Agent(name='assistant', instructions='Complete the requested task.')
    client = cosens(agent, mode='auto')
    result = await client.run(agent, task)
    return client, result, client.cosens.last_turn_id


async def on_artifact_confirmed(client, turn_id, request_key):
    return await client.cosens.generate_recipe(turn_id, request_key=request_key)
