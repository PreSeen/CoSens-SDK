"""Wrap a standard OpenAI client; COSENS_API_KEY authenticates both the model gateway and CoSens."""
import os

from openai import OpenAI
from cosens import cosens


def run_task(task, model):
    model_client = OpenAI(base_url=os.environ["COSENS_MODEL_BASE_URL"], api_key=os.environ["COSENS_API_KEY"])
    client = cosens(model_client, mode="auto")
    response = client.responses.create(model=model, input=task, store=False)
    return client, response, client.cosens.last_turn_id


def on_artifact_confirmed(client, turn_id, request_key):
    return client.cosens.generate_recipe(turn_id, request_key=request_key)
