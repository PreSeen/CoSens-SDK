"""Atomic recording, an explicit correction and explicitly requested learning."""


async def record_task(client, objective, output, request_key, model):
    atoms = client.atoms
    task = await atoms.create_task(objective=objective, key=request_key+':task')
    execution = await atoms.start_execution(task['task_id'], key=request_key+':execution', model=model,
                                            source='new_task', objective=objective, self_contained=True)
    artifact = await atoms.artifact(name='output.txt', content=output)
    result = await atoms.submit_execution(execution['execution_id'], request_sha256=execution['request_sha256'],
        outcome='completed', artifacts=[{'artifact_id':artifact['artifact_id'],'role':'submitted_output'}])
    if result['status'] != 'completed':
        raise RuntimeError('The execution result was not confirmed')
    return task['task_id'], result['execution_id']


async def correct(client, execution_id, feedback, request_key):
    """Record feedback on an execution and start its correction; the host runs the correction and submits it."""
    await client.atoms.record_feedback(execution_id, feedback=feedback, source='human', key=request_key+':feedback')
    return await client.atoms.start_execution(key=request_key+':correction', previous_execution_id=execution_id)


async def on_artifact_confirmed(client, execution_id, user_words, request_key):
    """Call with the user's own words about the artifact; learning needs that feedback and is once per execution."""
    await client.atoms.record_feedback(execution_id, feedback=user_words, source='human', key=request_key+':confirmed')
    await client.atoms.learn(execution_id)
    return await client.atoms.wait_learning(execution_id)
