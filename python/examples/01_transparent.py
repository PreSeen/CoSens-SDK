"""COSENS_API_KEY is shared by the model gateway and execution capture."""
from cosens import cosens, friday_gateway_client


def run_task(task, model):
    client = cosens(friday_gateway_client(), mode="auto")
    response = client.responses.create(model=model, input=task, store=False)
    return client, response, client.cosens.last_turn_id


def on_artifact_confirmed(client, turn_id, request_key):
    return client.cosens.generate_recipe(turn_id, request_key=request_key)
