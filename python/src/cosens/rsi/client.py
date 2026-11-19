"""Host-facing API for local RSI."""

from typing import Annotated, Literal

from pydantic import Field, StrictBool, StrictInt, StringConstraints, TypeAdapter

from ._queue_client import RSIClient as QueueClient
from .models import Contract

Text = Annotated[str, StringConstraints(min_length=1)]
Positive = Annotated[StrictInt, Field(gt=0)]
Offset = Annotated[StrictInt, Field(ge=0)]


class ModelEndpoint(Contract):
    model: Text
    base_url: Text
    auto_start: StrictBool


class File(Contract):
    path: Text
    role: Literal["input", "artifact", "reference", "feedback"]


class ExecutionEvidence(Contract):
    context_path: Text
    files: list[File] = Field(max_length=1000)


class Page(Contract):
    offset: Offset
    archived: StrictBool


_TEXT = TypeAdapter(Text)
_ANSWER = TypeAdapter(Text | Annotated[list[Text], Field(min_length=1)])
_REVISION = TypeAdapter(Positive)
_ARCHIVED = TypeAdapter(StrictBool)


class RSIClient:
    def __init__(self, *, data_dir, config: dict | None):
        settings = ModelEndpoint.model_validate(config) if config is not None else None
        self._queue = QueueClient(
            data_dir=data_dir,
            config={"model": {"model": settings.model, "base_url": settings.base_url}}
            if settings
            else None,
            auto_start=settings.auto_start if settings else True,
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

    def execution_create(self, *, evidence, base_execution_id, idempotency_key):
        submission_id = _TEXT.validate_python(idempotency_key)
        execution = self._queue.execution_create_with_evidence(
            evidence=ExecutionEvidence.model_validate(evidence).model_dump(),
            confirmation_ref=submission_id,
            idempotency_key=submission_id,
            base={"kind": "execution", "id": _TEXT.validate_python(base_execution_id)}
            if base_execution_id is not None
            else None,
        )
        return self._present_execution(execution)

    def execution_get(self, execution_id):
        return self._present_execution(self._queue.execution_get(execution_id))

    def execution_list(self, *, offset, archived):
        rows = self._queue.execution_list(
            **Page(offset=offset, archived=archived).model_dump()
        )
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

    def recipe_list(self):
        return self._queue.recipe_list_all()

    def recipe_get(self, recipe_id, *, version):
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

    def recover(self):
        return self._queue.recover()
