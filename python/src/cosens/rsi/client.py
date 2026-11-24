"""Host-facing API for local RSI."""

from pathlib import Path
from typing import Annotated, Literal

from pydantic import (
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    StringConstraints,
    TypeAdapter,
)

from ._queue_client import RSIClient as QueueClient
from .model_headers import bind_model_headers, credential_scope_id
from .models import Contract, PythonImage, RecipeReference
from .recipe_file import read_recipe_file

Text = Annotated[str, StringConstraints(min_length=1)]
Positive = Annotated[StrictInt, Field(gt=0)]
Offset = Annotated[StrictInt, Field(ge=0)]


class ModelEndpoint(Contract):
    model_config = ConfigDict(hide_input_in_errors=True)

    model: Text
    base_url: Text
    headers: dict[str, str] = Field(default_factory=dict)
    credential_scope: Text | None = None
    auto_start: StrictBool
    max_tokens: Positive | None = None
    timeout: Annotated[float, Field(gt=0, le=3600)] | None = None
    test_python_image: PythonImage | None = None


class File(Contract):
    path: Text
    role: Literal["input", "artifact", "reference", "feedback"]


class ExecutionEvidence(Contract):
    context_path: Text
    files: list[File] = Field(max_length=1000)


class Page(Contract):
    offset: Offset
    archived: StrictBool


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
    `RSIClient(data_dir=..., config=...)` (or `mode="local"`) runs the local
    Worker with your own model. Local Recipe reads include a storage_path
    relative to data_dir.
    """

    def __new__(cls, *, mode=None, data_dir=None, config=None, api_key=None, base_url=None, virtual_user_id=None):
        if mode is None:
            mode = "local" if data_dir is not None or config is not None else "cloud"
        if mode == "cloud":
            if data_dir is not None or config is not None:
                raise ValueError("data_dir and config are for mode='local'")
            from .cloud import CloudRSIClient

            return CloudRSIClient(api_key=api_key, base_url=base_url, virtual_user_id=virtual_user_id)
        if mode != "local":
            raise ValueError("mode must be 'cloud' or 'local'")
        if api_key is not None or base_url is not None or virtual_user_id is not None:
            raise ValueError("api_key and base_url are for mode='cloud'")
        if data_dir is None:
            raise ValueError("mode='local' needs data_dir")
        return super().__new__(cls)

    mode = "local"

    def __init__(self, *, mode=None, data_dir, config: dict | None = None, api_key=None, base_url=None, virtual_user_id=None):
        settings = ModelEndpoint.model_validate(config) if config is not None else None
        scope = credential_scope_id(settings.credential_scope) if settings else None
        binding = (
            bind_model_headers(
                settings.base_url,
                settings.model,
                settings.headers,
                credential_scope=scope,
            )
            if settings is not None
            else None
        )
        self._queue = QueueClient(
            data_dir=data_dir,
            config={
                "model": {
                    "model": settings.model,
                    "base_url": settings.base_url,
                    "api_key_env": binding[0],
                    "credential_scope": scope,
                    **settings.model_dump(
                        include={"max_tokens", "timeout"}, exclude_none=True
                    ),
                },
                **settings.model_dump(include={"test_python_image"}, exclude_none=True),
            }
            if settings and binding
            else None,
            auto_start=settings.auto_start if settings else True,
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
        recipe_id = None
        if recipe_reference is not None:
            reference = RecipeReference.model_validate(recipe_reference)
            submission["files"].append({"path": reference.path, "role": "recipe"})
            recipe_id = reference.recipe_id
        execution = self._queue.execution_create_with_evidence(
            evidence=submission,
            confirmation_ref=submission_id,
            idempotency_key=submission_id,
            base={"kind": "recipe", "id": recipe_id} if recipe_id else None,
        )
        return self._present_execution(execution)

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

    def rubrics_list(self, *, offset, archived):
        return self._queue.rubrics_list(
            **Page(offset=offset, archived=archived).model_dump()
        )

    def rubrics_get(self, rubrics_id, *, version):
        version = _TEXT.validate_python(version)
        return self._queue.rubrics_get(
            rubrics_id, version_id=None if version == "latest" else version
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
                "status": row["status"],
                "active_version_id": row.get("active_version_id"),
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
            }
            for row in self._queue.recipe_list_all(recipe_ids=recipe_ids)
        ]

    def recipe_insert(self, path: str | Path):
        content, markdown = read_recipe_file(path)
        return self._queue.recipe_insert(content, markdown)

    def recipes_upsert(
        self,
        path: str | Path,
        *,
        local_recipe_id: str,
        local_recipe_version_id: str,
        idempotency_key: str | None = None,
        api_key: str | None = None,
        base_url: str | None = None,
        virtual_user_id: str | None = None,
    ) -> dict[str, str]:
        """Upload an existing local Recipe version and adopt the IDs from the cloud receipt."""
        from .cloud import CloudRSIClient, _read_recipe_zip
        from .recipe_sync import upload_recipe

        filename, zip_bytes = _read_recipe_zip(path)
        client = CloudRSIClient(api_key=api_key, base_url=base_url, virtual_user_id=virtual_user_id)
        try:
            return upload_recipe(
                self._queue.store,
                client,
                filename,
                zip_bytes,
                _TEXT.validate_python(local_recipe_id),
                _TEXT.validate_python(local_recipe_version_id),
                idempotency_key,
            )
        finally:
            client.close()

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
