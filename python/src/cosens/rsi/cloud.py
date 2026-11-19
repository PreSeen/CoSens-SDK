"""Cloud RSI: the same atoms as the local client, served by CoSens.

Learning runs on the CoSens service with CoSens models. Only the files named in
`evidence` leave the machine: `execution_create` and
`execution_append_evidence` read and upload them. Authentication uses
`COSENS_API_KEY`, as the Memory functions do.
"""

from __future__ import annotations

import base64
import os
from pathlib import Path
from urllib.parse import quote

import httpx

from .chat_completions import USER_AGENT
from .models import Conflict, InvalidState, NotFound, RSIError

DEFAULT_BASE_URL = "https://rsi.cosens.ai/api"
_ERRORS = {"not_found": NotFound, "conflict": Conflict, "invalid_state": InvalidState}


def _segment(value):
    if not isinstance(value, str) or not value or value in {".", ".."}:
        raise ValueError("ID must be a nonempty string")
    return quote(value, safe="")


def _upload(path, extra=None):
    file = Path(path)
    data = file.read_bytes()
    try:
        content, encoding = data.decode("utf-8"), "utf-8"
    except UnicodeDecodeError:
        content, encoding = base64.b64encode(data).decode("ascii"), "base64"
    return {"name": file.name, "content": content, "encoding": encoding, **(extra or {})}


def _evidence(evidence):
    if not isinstance(evidence, dict) or not evidence.get("context_path"):
        raise ValueError("evidence needs context_path and files")
    files = []
    for item in evidence.get("files", []):
        if not isinstance(item, dict) or not item.get("path") or not item.get("role"):
            raise ValueError("each evidence file needs path and role")
        extra = {"role": item["role"]}
        if item.get("recipe_id"):
            extra["recipe_id"] = item["recipe_id"]
        files.append(_upload(item["path"], extra))
    if sum(file["role"] == "recipe" for file in files) > 1:
        raise ValueError("evidence can contain at most one recipe file")
    return {"context": _upload(evidence["context_path"]), "files": files}


class CloudRSIClient:
    """Cloud mode of RSIClient: same methods and results, no local Worker."""

    def __init__(self, *, api_key=None, base_url=None, timeout=60.0, transport=None):
        key = api_key or os.environ.get("COSENS_API_KEY")
        if not key:
            raise ValueError("cloud mode needs COSENS_API_KEY (or api_key=...)")
        self._http = httpx.Client(
            base_url=(base_url or os.environ.get("COSENS_RSI_BASE_URL") or DEFAULT_BASE_URL).rstrip("/"),
            headers={
                "Authorization": f"Bearer {key}",
                "X-Auth-Type": "api_key",
                "User-Agent": USER_AGENT,
            },
            timeout=timeout,
            transport=transport,
        )

    mode = "cloud"
    data_dir = None

    def close(self):
        self._http.close()

    def _request(self, method, path, *, json=None, params=None):
        try:
            response = self._http.request(method, path, json=json, params=params)
        except httpx.HTTPError:
            error = RSIError("CoSens RSI service could not be reached")
            error.code = "service_unavailable"
            raise error from None
        if response.is_success:
            return response.json()
        try:
            detail = response.json().get("error") or {}
        except ValueError:
            detail = {}
        code = detail.get("code") or f"http_{response.status_code}"
        message = detail.get("message") or f"CoSens RSI returned HTTP {response.status_code}"
        if response.status_code == 422 and code == "invalid_request":
            raise ValueError(message)
        error = _ERRORS.get(code, RSIError)(message)
        error.code = code
        error.status = response.status_code
        raise error

    # Execution

    def execution_create(self, *, evidence, idempotency_key):
        if not isinstance(idempotency_key, str) or not idempotency_key:
            raise ValueError("idempotency_key must be a nonempty string")
        body = {"idempotency_key": idempotency_key, **_evidence(evidence)}
        return self._request("POST", "/executions", json=body)

    def execution_get(self, execution_id):
        return self._request("GET", f"/executions/{_segment(execution_id)}")

    def execution_list(self, *, query=None):
        params = None
        if query is not None:
            ids = query.get("execution_ids")
            if not isinstance(ids, list):
                raise ValueError("query.execution_ids must be a list")
            if not ids:
                return []
            params = {"execution_ids": ",".join(ids)}
        return self._request("GET", "/executions", params=params)

    def execution_append_evidence(self, execution_id, *, evidence):
        body = _evidence(evidence)
        if any(file["role"] == "recipe" for file in body["files"]):
            raise ValueError("a recipe file can only be supplied when the execution is created")
        return self._request("POST", f"/executions/{_segment(execution_id)}/evidence", json=body)

    def execution_cancel(self, execution_id):
        return self._request("POST", f"/executions/{_segment(execution_id)}/cancel")

    def execution_clarification_answer(self, execution_id, *, question_id, question_revision, answer):
        return self._request(
            "POST",
            f"/executions/{_segment(execution_id)}/clarifications",
            json={"question_id": question_id, "question_revision": question_revision, "answer": answer},
        )

    # Rubrics

    def rubrics_list(self, *, offset, archived):
        return self._request("GET", "/rubrics", params={"offset": offset, "archived": str(bool(archived)).lower()})

    def rubrics_get(self, rubrics_id, *, version):
        return self._request("GET", f"/rubrics/{_segment(rubrics_id)}", params={"version": version})

    # Recipe

    def recipe_list(self, *, query=None):
        params = None
        if query is not None:
            ids = query.get("recipe_ids")
            if not isinstance(ids, list):
                raise ValueError("query.recipe_ids must be a list")
            if not ids:
                return []
            params = {"recipe_ids": ",".join(ids)}
        return self._request("GET", "/recipes", params=params)

    def recipe_get(self, recipe_id, *, version):
        return self._request("GET", f"/recipes/{_segment(recipe_id)}", params={"version": version})

    def recipe_test(self, *, execution_id, recipe_id, version_id, rubrics_version_id):
        return self._request(
            "POST",
            f"/executions/{_segment(execution_id)}/recipe-tests",
            json={"recipe_id": recipe_id, "version_id": version_id, "rubrics_version_id": rubrics_version_id},
        )

    def recipe_version_set_archived(self, recipe_id, *, version_id, archived):
        return self._request(
            "PUT",
            f"/recipes/{_segment(recipe_id)}/versions/{_segment(version_id)}/archived",
            json={"archived": archived},
        )

    def recover(self):
        """The service recovers its own Workers; nothing to start locally."""
        return {"started": 0}
