"""Wrap a standard OpenAI client; COSENS_API_KEY authenticates both the model gateway and CoSens."""
import os

from openai import OpenAI
from cosens import cosens


def run_task(task, model):
    model_client = OpenAI(base_url=os.environ["COSENS_MODEL_BASE_URL"], api_key=os.environ["COSENS_API_KEY"])
    client = cosens(model_client, mode="auto")
    response = client.responses.create(model=model, input=task, store=False)
    return client, response, client.cosens.last_turn_id


def on_artifact_confirmed(client, turn_id):
    """Call only after the user confirmed the artifact; an execution learns once."""
    turn = client.cosens.get_turn(turn_id)
    if not turn or turn.get("state") != "completed" or not turn.get("execution_id"):
        return None  # nothing was recorded for this turn
    client.cosens.generate_recipe(turn_id)
    return client.cosens.wait_learning(turn_id)
