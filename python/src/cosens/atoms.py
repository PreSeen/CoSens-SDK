"""Public atomic RSI resources. No atom runs an Agent or model."""

import asyncio
import os
from urllib.parse import quote

from .http import AsyncTransport, SyncTransport


def _part(value):
    if not isinstance(value, str) or not value:
        raise ValueError("resource ID must be a nonempty string")
    return quote(value, safe="")


def _key(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 256:
        raise ValueError("key must be 1–256 characters")
    return value


class AsyncAtoms:
    def __init__(self, transport):
        self._http = transport

    async def artifact(self, *, name, content):
        return await self._http.request("POST", "/v1/artifacts", body={"name": name, "content": content})

    async def prepare(self, *, objective, key, task_family="general", input_artifact_ids=None, **options):
        body = {"objective": objective, "task_family": task_family, "input_artifact_ids": input_artifact_ids or []}
        body.update(options)
        return await self._http.request("POST", "/v1/runs/prepare", body=body, key=_key(key))

    async def get_run(self, run_id):
        return await self._http.request("GET", f"/v1/runs/{_part(run_id)}")

    async def session_events(self, run_id, events):
        return await self._http.request("POST", f"/v1/runs/{_part(run_id)}/session-events", body={"events": events})

    async def initial_inputs(self, run_id, artifact_ids):
        return await self._http.request("POST", f"/v1/runs/{_part(run_id)}/initial-inputs", body={"artifact_ids": artifact_ids})

    async def submit(self, run_id, *, data=None, key=None):
        return await self._http.request("POST", f"/v1/runs/{_part(run_id)}/submit", body={"data": data or {}}, key=_key(key) if key else None)

    async def feedback(self, run_id, *, data, key=None):
        return await self._http.request("POST", f"/v1/runs/{_part(run_id)}/feedback", body={"data": data}, key=_key(key) if key else None)

    async def improve(self, run_id, *, reason, key=None):
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("reason is required")
        return await self._http.request("POST", f"/v1/runs/{_part(run_id)}/improvements", body={"data": {"reason": reason}}, key=_key(key) if key else None)

    async def get_improvement(self, job_id):
        return await self._http.request("GET", f"/v1/improvements/{_part(job_id)}")

    async def resume_improvement(self, job_id):
        return await self._http.request("POST", f"/v1/improvements/{_part(job_id)}/resume")

    async def wait_improvement(self, job_id, *, timeout=300, initial_delay=2, max_delay=30):
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        delay = initial_delay
        while True:
            job = await self.get_improvement(job_id)
            if job.status not in {"queued", "running"}:
                return job
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise TimeoutError(f"improvement {job_id} did not finish within {timeout} seconds")
            await asyncio.sleep(min(delay, remaining))
            delay = min(delay * 2, max_delay)


class AdminAtoms:
    def __init__(self, transport):
        self._http = transport

    async def publish(self, job_id, *, expected_revision, reason):
        if not self._http.token:
            raise ValueError("A separate CoSens administrator token is required")
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("reason is required")
        return await self._http.request(
            "POST", f"/internal/admin/v1/improvements/{_part(job_id)}/publish",
            body={"expected_revision": expected_revision, "reason": reason},
        )


class _AdminClient:
    def __init__(self, transport):
        self.atoms = AdminAtoms(transport)


class SyncAtoms:
    """Internal synchronous form used by wrapped synchronous OpenAI clients."""

    def __init__(self, transport):
        self._http = transport

    def artifact(self, *, name, content):
        return self._http.request("POST", "/v1/artifacts", body={"name": name, "content": content})

    def prepare(self, *, objective, key, task_family="general", input_artifact_ids=None, **options):
        body = {"objective": objective, "task_family": task_family, "input_artifact_ids": input_artifact_ids or []}
        body.update(options)
        return self._http.request("POST", "/v1/runs/prepare", body=body, key=_key(key))

    def get_run(self, run_id):
        return self._http.request("GET", f"/v1/runs/{_part(run_id)}")

    def session_events(self, run_id, events):
        return self._http.request("POST", f"/v1/runs/{_part(run_id)}/session-events", body={"events": events})

    def initial_inputs(self, run_id, artifact_ids):
        return self._http.request("POST", f"/v1/runs/{_part(run_id)}/initial-inputs", body={"artifact_ids": artifact_ids})

    def submit(self, run_id, *, data=None, key=None):
        return self._http.request("POST", f"/v1/runs/{_part(run_id)}/submit", body={"data": data or {}}, key=_key(key) if key else None)


class CoSensClient:
    def __init__(self, token=None, *, admin_token=None, state_path=None, _http_transport=None):
        self._transport = AsyncTransport(token or os.getenv("COSENS_RSI_TOKEN"), transport=_http_transport)
        self._admin_transport = AsyncTransport(admin_token or os.getenv("COSENS_RSI_ADMIN_TOKEN"), transport=_http_transport)
        self._sync_transport = SyncTransport(token or os.getenv("COSENS_RSI_TOKEN"), transport=_http_transport)
        self.atoms = AsyncAtoms(self._transport)
        self.sync_atoms = SyncAtoms(self._sync_transport)
        self.admin = _AdminClient(self._admin_transport)
        self.state_path = state_path

    async def aclose(self):
        await self._transport.aclose()
        await self._admin_transport.aclose()
        self._sync_transport.close()

    def close(self):
        self._sync_transport.close()

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        await self.aclose()
