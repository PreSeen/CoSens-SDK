"""Cloud RSI: the same atoms as the local client, served by CoSens.

Learning runs on the CoSens service with CoSens models. The SDK uploads files
listed in `evidence` and the optional `recipe_reference`. Authentication uses
`COSENS_API_KEY`, as the Memory functions do.
"""

from __future__ import annotations

import base64
import os
import re
from pathlib import Path
from urllib.parse import quote, urlsplit
from urllib.request import url2pathname

import httpx

from .chat_completions import USER_AGENT
from .models import Conflict, InvalidState, NotFound, RecipeReference, RSIError
from .recipe_file import read_recipe_file

DEFAULT_BASE_URL = "https://rsi.cosens.ai/api"
# The CoSens gateway accepts request bodies up to 10 MiB.
MAX_REQUEST_BYTES = 10 * 1024 * 1024
MAX_RECIPE_ZIP_BYTES = 8 * 1024 * 1024
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


def _evidence(evidence, recipe_reference=None):
    if not isinstance(evidence, dict) or not evidence.get("context_path"):
        raise ValueError("evidence needs context_path and files")
    files = []
    for item in evidence.get("files", []):
        if not isinstance(item, dict) or not item.get("path") or not item.get("role"):
            raise ValueError("each evidence file needs path and role")
        if item["role"] == "recipe":
            raise ValueError("supply the Recipe using execution_create.recipe_reference")
        files.append(_upload(item["path"], {"role": item["role"]}))
    if recipe_reference is not None:
        reference = RecipeReference.model_validate(recipe_reference)
        extra = {"role": "recipe"}
        if reference.recipe_id is not None:
            extra["recipe_id"] = reference.recipe_id
        files.append(_upload(reference.path, extra))
    return {"context": _upload(evidence["context_path"]), "files": files}


def _read_recipe_zip(path):
    if isinstance(path, str) and path.lower().startswith("file:"):
        url = urlsplit(path)
        if (
            url.netloc.lower() not in {"", "localhost"}
            or not url.path.startswith("/")
            or url.query
            or url.fragment
        ):
            raise ValueError("Recipe file URI must refer to a local path")
        path = url2pathname(url.path)
    file = Path(path)
    with file.open("rb") as stream:
        zip_bytes = stream.read(MAX_RECIPE_ZIP_BYTES + 1)
    if not 0 < len(zip_bytes) <= MAX_RECIPE_ZIP_BYTES:
        raise ValueError("Recipe ZIP must be nonempty and at most 8 MiB")
    return file.name, zip_bytes


def _validate_recipe_upload(recipe_id, idempotency_key):
    if recipe_id is not None and (
        not isinstance(recipe_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", recipe_id)
    ):
        raise ValueError("recipe_id must contain 1–64 letters, digits, underscores or hyphens")
    if idempotency_key is not None and (
        not isinstance(idempotency_key, str)
        or not idempotency_key
        or idempotency_key.strip() != idempotency_key
        or any(not 32 <= ord(char) <= 126 for char in idempotency_key)
    ):
        raise ValueError("idempotency_key must be a nonempty HTTP header value")


class CloudRSIClient:
    """Cloud mode of RSIClient, backed by the CoSens service."""

    def __init__(
        self, *, api_key=None, base_url=None, timeout=60.0, transport=None,
        virtual_user_id=None, _shared_headers=None,
    ):
        headers = httpx.Headers(_shared_headers, encoding="iso-8859-1")
        if not headers:
            key = api_key or os.environ.get("COSENS_API_KEY")
            if not key:
                raise ValueError("cloud mode needs COSENS_API_KEY (or api_key=...)")
            headers.update({"Authorization": f"Bearer {key}", "X-Auth-Type": "api_key"})
        for name in ("Content-Type", "Content-Length", "Transfer-Encoding", "Idempotency-Key"):
            headers.pop(name, None)
        headers.setdefault("User-Agent", USER_AGENT)
        if virtual_user_id is not None and (not isinstance(virtual_user_id, str) or not virtual_user_id or any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in virtual_user_id)):
            raise ValueError("virtual_user_id must be a nonempty header value")
        if virtual_user_id is not None:
            headers["X-Virtual-User-Id"] = virtual_user_id
        self._http = httpx.Client(
            base_url=(base_url or os.environ.get("COSENS_RSI_BASE_URL") or DEFAULT_BASE_URL).rstrip("/"),
            headers=headers,
            timeout=timeout,
            transport=transport,
        )

    mode = "cloud"
    data_dir = None

    def close(self):
        self._http.close()

    def _request(self, method, path, *, json=None, params=None, files=None, data=None, headers=None, binary=False):
        content = None
        headers = dict(headers or {})
        if json is not None:
            import json as _json

            content = _json.dumps(json, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
            if len(content) > MAX_REQUEST_BYTES:
                raise ValueError(
                    "upload exceeds the 10 MiB cloud request limit; send fewer or smaller files, or use local mode"
                )
        try:
            response = self._http.request(
                method,
                path,
                content=content,
                params=params,
                files=files,
                data=data,
                headers=headers,
            )
        except httpx.HTTPError:
            error = RSIError("CoSens RSI service could not be reached")
            error.code = "service_unavailable"
            raise error from None
        if response.is_success:
            return response.content if binary else response.json()
        try:
            detail = response.json().get("error") or {}
        except ValueError:
            detail = {}
        code = detail.get("code") or f"http_{response.status_code}"
        message = detail.get("message") or f"CoSens RSI returned HTTP {response.status_code}"
        if response.status_code == 422 and code == "invalid_request":
            error = ValueError(message)
            error.status = response.status_code
            raise error
        error = _ERRORS.get(code, RSIError)(message)
        error.code = code
        error.status = response.status_code
        raise error

    # Execution

    def execution_create(self, *, evidence, idempotency_key, recipe_reference=None):
        if not isinstance(idempotency_key, str) or not idempotency_key:
            raise ValueError("idempotency_key must be a nonempty string")
        body = {"idempotency_key": idempotency_key, **_evidence(evidence, recipe_reference)}
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

    def recipe_insert(self, path: str | Path):
        # Validate locally first, so a malformed file fails the same way in both modes.
        _, markdown = read_recipe_file(path)
        return self._request("POST", "/recipes", json={"markdown": markdown})

    def recipes_upsert(
        self, path: str | Path, *, recipe_id: str | None = None, idempotency_key: str | None = None
    ) -> dict[str, str]:
        """Upload a Recipe ZIP as a new Recipe or a new version of an existing one."""
        filename, zip_bytes = _read_recipe_zip(path)
        return self._recipes_upsert_bytes(
            filename, zip_bytes, recipe_id=recipe_id, idempotency_key=idempotency_key
        )

    def _recipe_upload_destination(self):
        url = self._http.base_url
        if url.scheme not in {"http", "https"} or not url.host or url.userinfo or url.query or url.fragment:
            raise ValueError("local Recipe upload requires a cloud base_url without credentials, query or fragment")
        return {
            "base_url": str(url).rstrip("/"),
            "virtual_user_id": self._http.headers.get("X-Virtual-User-Id"),
        }

    def _recipes_upsert_bytes(self, filename, zip_bytes, *, recipe_id, idempotency_key):
        _validate_recipe_upload(recipe_id, idempotency_key)
        return self._request(
            "POST",
            "/recipes_upsert",
            files={"file": (filename, zip_bytes, "application/zip")},
            data={"recipe_id": recipe_id} if recipe_id is not None else None,
            headers={"Idempotency-Key": idempotency_key} if idempotency_key is not None else None,
        )

    def recipe_get(self, recipe_id, *, version="active"):
        return self._request("GET", f"/recipes/{_segment(recipe_id)}", params={"version": version})

    def recipe_download(self, recipe_id: str, *, recipe_version_id: str) -> bytes:
        """Download the ZIP bytes for an exact Recipe version."""
        return self._request(
            "GET",
            f"/recipes/{_segment(recipe_id)}/versions/{_segment(recipe_version_id)}/download",
            binary=True,
        )

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

    def recover(
        self,
        *,
        recipe_id: str | None = None,
        previous_headers: dict[str, str] | None = None,
        retry_key: str | None = None,
    ):
        """The service recovers its own Workers; nothing to start locally."""
        if retry_key is not None:
            raise InvalidState("recover retry_key requires local mode")
        return {"started": 0}
