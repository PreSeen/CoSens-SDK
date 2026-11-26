"""Host-facing API for local RSI."""

import os
from typing import Annotated, Literal

from pydantic import (
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    StringConstraints,
    TypeAdapter,
)

from . import task
from ._queue_client import RSIClient as QueueClient
from .model_headers import (
    bind_model_headers,
    credential_scope_id,
    normalize_request_headers,
)
from .models import Contract, InvalidState, NotFound, RecipeReference

Text = Annotated[str, StringConstraints(min_length=1)]
NonBlankText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
Positive = Annotated[StrictInt, Field(gt=0)]


class ModelEndpoint(Contract):
    model_config = ConfigDict(hide_input_in_errors=True)

    model_id: NonBlankText
    api_key: NonBlankText
    base_url: NonBlankText


class LocalOptions(Contract):
    model_config = ConfigDict(hide_input_in_errors=True)

    headers: dict[str, str] = Field(default_factory=dict)
    credential_scope: Text | None = None
    auto_start: StrictBool = True
    max_tokens: Positive | None = None
    timeout: Annotated[float, Field(gt=0, le=3600)] | None = None


class File(Contract):
    path: Text
    role: Literal["input", "artifact", "reference", "feedback"]


class ExecutionEvidence(Contract):
    context_path: Text
    files: list[File] = Field(max_length=1000)


class ExecutionListQuery(Contract):
    execution_ids: list[Text]


class RecipeListQuery(Contract):
    recipe_ids: list[Text]


_TEXT = TypeAdapter(Text)
_HEADERS = TypeAdapter(dict[str, str], config=ConfigDict(hide_input_in_errors=True))
_ANSWER = TypeAdapter(Text | Annotated[list[Text], Field(min_length=1)])
_REVISION = TypeAdapter(Positive)
_ARCHIVED = TypeAdapter(StrictBool)


class RSIClient:
    """RSI atoms in cloud mode (default) or local mode.

    `RSIClient()` uses the CoSens service with `COSENS_API_KEY`.
    `RSIClient(data_dir=..., model_id=..., api_key=..., base_url=...)` runs
    the local Worker with your own model. Local Recipe reads include a
    storage_path relative to data_dir.
    """

    def __new__(
        cls, *, mode=None, data_dir=None, config=None, model_id=None,
        api_key=None, base_url=None, virtual_user_id=None,
    ):
        if mode is None:
            mode = "local" if any(value is not None for value in (data_dir, config, model_id)) else "cloud"
        if mode == "cloud":
            if any(value is not None for value in (data_dir, config, model_id)):
                raise ValueError("data_dir, config and model_id are for mode='local'")
            from .cloud import CloudRSIClient

            return CloudRSIClient(api_key=api_key, base_url=base_url, virtual_user_id=virtual_user_id)
        if mode != "local":
            raise ValueError("mode must be 'cloud' or 'local'")
        if virtual_user_id is not None:
            raise ValueError("virtual_user_id is for mode='cloud'")
        if data_dir is None:
            raise ValueError("mode='local' needs data_dir")
        return super().__new__(cls)

    mode = "local"

    def __init__(
        self, *, mode=None, data_dir, config: dict | None = None, model_id=None,
        api_key=None, base_url=None, virtual_user_id=None,
    ):
        settings = LocalOptions.model_validate(config if config is not None else {})
        endpoint_requested = any(value is not None for value in (model_id, api_key, base_url))
        worker_settings = config is not None and settings.model_fields_set != {"headers"}
        endpoint = (
            ModelEndpoint(model_id=model_id, api_key=api_key, base_url=base_url)
            if endpoint_requested or worker_settings
            else None
        )
        scope = credential_scope_id(settings.credential_scope)
        binding = (
            bind_model_headers(
                endpoint.base_url,
                endpoint.model_id,
                {"Authorization": f"Bearer {endpoint.api_key}"},
                credential_scope=scope,
            )
            if endpoint is not None
            else None
        )
        self._upload_headers = normalize_request_headers(settings.headers)
        self._queue = QueueClient(
            data_dir=data_dir,
            config={
                "model": {
                    "model": endpoint.model_id,
                    "base_url": endpoint.base_url,
                    "api_key_env": binding[0],
                    "credential_scope": scope,
                    **settings.model_dump(
                        include={"max_tokens", "timeout"}, exclude_none=True
                    ),
                },
            }
            if endpoint and binding
            else None,
            auto_start=settings.auto_start,
            model_header_binding=binding,
        )

    @property
    def data_dir(self):
        return self._queue.data_dir

    def _present_execution(self, execution):
        fields = (
            "id",
            "base_execution_id",
            "status",
            "stage",
            "input_revision",
            "questions",
            "answers",
            "recipe_id",
            "recipe_version_id",
            "rubrics_version_id",
            "test_results",
            "result",
            "error",
            "created_at",
            "updated_at",
        )
        return {
            **{field: execution[field] for field in fields},
            "name": execution.get("name") or "首次学习",
            "progress_percent": execution["progress_percent"],
            "evidence": [
                {
                    key: file[key]
                    for key in ("path", "locator", "role", "sha256", "size")
                }
                for file in self._queue.execution_evidence(execution)
            ],
        }

    def execution_create(self, *, evidence, idempotency_key, recipe_reference=None):
        submission_id = _TEXT.validate_python(idempotency_key)
        submission = ExecutionEvidence.model_validate(evidence).model_dump()
        base = None
        if recipe_reference is not None:
            reference = RecipeReference.model_validate(recipe_reference)
            submission["files"].append({"path": reference.path, "role": "recipe"})
            if reference.recipe_id is not None:
                base = {"kind": "recipe", "id": reference.recipe_id}
                if reference.recipe_version_id is not None:
                    base["version_id"] = reference.recipe_version_id
        execution = self._queue.execution_create_with_evidence(
            evidence=submission,
            confirmation_ref=submission_id,
            idempotency_key=submission_id,
            base=base,
            resolve_base=self._resolve_recipe_base,
        )
        return self._present_execution(execution)

    def _resolve_recipe_base(self, base):
        from .cloud import CloudRSIClient
        from .recipe_bootstrap import resolve_base

        def cloud_client():
            if not self._upload_headers and not os.environ.get("COSENS_API_KEY"):
                raise NotFound("recipe not found locally; configure CoSens credentials to fetch it")
            return CloudRSIClient(_shared_headers=self._upload_headers)

        return resolve_base(self._queue.store, base, cloud_client)

    def execution_get(self, execution_id):
        return self._present_execution(self._queue.execution_get(execution_id))

    def execution_list(self, *, query=None):
        execution_ids = (
            ExecutionListQuery.model_validate(query).execution_ids
            if query is not None
            else None
        )
        rows = self._queue.execution_list_all(execution_ids=execution_ids)
        return [self._present_execution(row) for row in rows]

    def task_list(self):
        """List local Tasks with the current Execution for each Task."""
        return task.task_list(self._queue.store)

    def task_get(self, task_id: str):
        """Read a local Task by its original Execution ID."""
        store = self._queue.store
        with store.transaction() as conn:
            return task.detail(store, conn, task.task_chain(store, conn, _TEXT.validate_python(task_id)))

    def task_restart(self, task_id: str, *, idempotency_key: str, evidence=None):
        """Start a fresh Execution in the same Task, carrying accepted answers and evidence."""
        return self._queue.task_restart(
            _TEXT.validate_python(task_id),
            idempotency_key=_TEXT.validate_python(idempotency_key),
            evidence=ExecutionEvidence.model_validate(evidence).model_dump() if evidence is not None else None,
        )

    def execution_append_evidence(self, execution_id, *, evidence):
        return self._present_execution(
            self._queue.execution_append_evidence(
                execution_id, ExecutionEvidence.model_validate(evidence).model_dump()
            )
        )

    def execution_cancel(self, execution_id):
        return self._present_execution(self._queue.execution_cancel(execution_id))

    def execution_clarification_answer(
        self, execution_id, *, question_id, question_revision, answer
    ):
        value = _ANSWER.validate_python(answer)
        return self._queue.execution_clarification_answer(
            execution_id,
            question_id=_TEXT.validate_python(question_id),
            question_revision=_REVISION.validate_python(question_revision),
            selected_option_ids=[] if isinstance(value, str) else value,
            text=value if isinstance(value, str) else None,
        )

    def rubrics_get(self, recipe_id: str, *, recipe_version_id: str):
        """Get the Rubrics used to generate the specified Recipe version."""
        return self._queue.rubrics_for_recipe(
            _TEXT.validate_python(recipe_id), _TEXT.validate_python(recipe_version_id)
        )

    def recipe_list(self, *, query=None):
        recipe_ids = (
            RecipeListQuery.model_validate(query).recipe_ids
            if query is not None
            else None
        )
        return [
            {
                "recipe_id": row["recipe_id"],
                "execution_ids": row["execution_ids"],
                "name": row["name"] or "",
                "active_version_id": row.get("active_version_id"),
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
            }
            for row in self._queue.recipe_list_all(recipe_ids=recipe_ids)
        ]

    def recipes_upsert(
        self,
        task_id: str,
        *,
        recipe_id: str | None = None,
        idempotency_key: str | None = None,
        expected_latest_version_id: str | None = None,
    ) -> dict[str, str]:
        """Publish the approved Recipe ZIP for a local Task."""
        from .cloud import CloudRSIClient, _local_upload_destination
        from .recipe_sync import upload_task

        task_id = _TEXT.validate_python(task_id)
        destination = _local_upload_destination(self._upload_headers)

        def cloud_client():
            if not self._upload_headers:
                raise InvalidState("configure CoSens request headers in local RSIClient config.headers to upload a Recipe")
            return CloudRSIClient(
                base_url=destination["base_url"],
                _shared_headers=self._upload_headers,
            )

        return upload_task(
            self._queue.store, task_id, idempotency_key, destination, cloud_client,
            target_recipe_id=recipe_id, expected_latest_version_id=expected_latest_version_id,
        )

    def recipe_get(self, recipe_id, *, version="active"):
        version = _TEXT.validate_python(version)
        if version in {"active", "latest"}:
            return self._queue.recipe_get(recipe_id, version_selector=version)
        return self._queue.recipe_get(recipe_id, version_id=version)

    def recipe_test(self, *, execution_id, recipe_id, version_id, rubrics_version_id):
        return self._queue.recipe_test(
            execution_id=execution_id,
            recipe_id=recipe_id,
            version_id=version_id,
            rubrics_version_id=rubrics_version_id,
        )

    def recipe_version_set_archived(self, recipe_id, *, version_id, archived):
        return self._queue.recipe_version_set_archived(
            _TEXT.validate_python(recipe_id),
            version_id=_TEXT.validate_python(version_id),
            archived=_ARCHIVED.validate_python(archived),
        )

    def recover(
        self,
        *,
        recipe_id: str | None = None,
        previous_headers: dict[str, str] | None = None,
        retry_key: str | None = None,
    ):
        return self._queue.recover(
            recipe_id=_TEXT.validate_python(recipe_id) if recipe_id is not None else None,
            previous_headers=(
                _HEADERS.validate_python(previous_headers)
                if previous_headers is not None
                else None
            ),
            retry_key=_TEXT.validate_python(retry_key) if retry_key is not None else None,
            refresh=True,
        )
