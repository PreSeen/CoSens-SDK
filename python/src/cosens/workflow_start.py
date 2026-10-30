"""Task workflow entry point availability."""

async def start_workflow(atoms, session_context, *, phase='prepare', mode='auto', request_key=None, authorize=None):
    raise NotImplementedError('CoSensClient.start is unavailable: the workflow phase contract is missing; use client.atoms to orchestrate explicitly')
