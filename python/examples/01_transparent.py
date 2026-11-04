"""Transparent capture; generate only from the host's confirmation handler."""
from openai import OpenAI
from cosens import cosens


def run_task(task):
    client = cosens(OpenAI(), mode='auto')
    response = client.responses.create(model='gpt-4.1-mini', input=task)
    return client, response, client.cosens.last_turn_id


def on_artifact_confirmed(client, turn_id, request_key):
    return client.cosens.generate_recipe(turn_id, request_key=request_key)
