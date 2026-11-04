"""Atomic recording and explicitly requested Recipe generation."""


async def record_task(client, objective, output, request_key):
    atoms = client.atoms
    task = await atoms.create_task(source='new_task', objective=objective, self_contained=True, key=request_key+':task')
    execution = await atoms.start_execution(task['task_id'], key=request_key+':execution')
    artifact = await atoms.artifact(name='output.txt', content=output)
    result = await atoms.submit_execution(execution['execution_id'], request_sha256=execution['request_sha256'],
        outcome='completed', artifacts=[{'artifact_id':artifact['artifact_id'],'role':'submitted_output'}])
    return task['task_id'], result['revision_id']


async def on_artifact_confirmed(client, task_id, revision_id, request_key):
    return await client.atoms.start_recipe_candidate(task_id, revision_id=revision_id, key=request_key)
