"""Local RSI contracts. No model credentials or model instances are serialized."""

from __future__ import annotations

from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, model_validator


class RSIError(RuntimeError):
    """Base error with a stable, transport-independent code."""

    code = "rsi_error"


class NotFound(RSIError):
    code = "not_found"


class Conflict(RSIError):
    code = "conflict"


class InvalidState(RSIError):
    code = "invalid_state"


class LeaseLost(RSIError):
    code = "lease_lost"


class InputChanged(RSIError):
    code = "input_changed"


class ModelOutputError(RSIError):
    code = "invalid_model_output"


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ModelConfig(Contract):
    model: str = Field(min_length=1)
    base_url: str | None = None
    api_key_env: str = "RSI_MODEL_API_KEY"
    timeout: float = Field(default=120, gt=0, le=3600)
    max_tokens: int = Field(default=4096, gt=0)

    @model_validator(mode="after")
    def public_endpoint(self):
        if self.base_url:
            url = urlsplit(self.base_url)
            if (
                url.scheme not in {"http", "https"}
                or not url.hostname
                or url.username
                or url.password
                or url.query
                or url.fragment
            ):
                raise ValueError(
                    "base_url must be an HTTP(S) endpoint without credentials, query or fragment"
                )
        if not self.api_key_env.isidentifier():
            raise ValueError("api_key_env must name an environment variable")
        return self


class LocalConfig(Contract):
    model: ModelConfig
    max_executions: int = Field(default=2, ge=1, le=32)
    max_model_requests: int = Field(default=2, ge=1, le=32)
    lease_seconds: float = Field(default=30, ge=3, le=3600)
    max_file_bytes: int = Field(default=4 * 1024 * 1024, ge=1)
    max_bundle_bytes: int = Field(default=32 * 1024 * 1024, ge=1)
    recursion_limit: int = Field(default=40, ge=4, le=500)


class FileRef(Contract):
    path: str
    role: Literal["input", "artifact", "reference", "feedback"] = "input"
    description: str = ""


class MaterialManifest(Contract):
    context_path: str
    files: list[FileRef] = Field(default_factory=list, max_length=1000)
    source_session_id: str | None = None
    source_task_id: str | None = None
    capture_boundary: str | None = None
    feedback_context: dict | None = None


class EvidenceRef(Contract):
    material_id: str = Field(
        pattern=r"^material_[0-9a-f]{32}$",
        description="The catalog's material_id, without any /file-index suffix.",
    )
    locator: str = Field(
        min_length=1,
        description="The exact file locator from the catalog: material_id/file-index.",
    )

    @model_validator(mode="after")
    def belongs_to_material(self):
        if not self.locator.startswith(self.material_id + "/"):
            raise ValueError(
                "locator must belong to material_id; copy the two separate catalog fields"
            )
        return self


class Criterion(Contract):
    id: str = Field(min_length=1)
    description: str = Field(min_length=1)
    source: Literal["task", "user_feedback", "trajectory", "agent_inference"]
    evidence: list[EvidenceRef] = Field(default_factory=list)
    required: bool = True


class RubricsContent(Contract):
    criteria: list[Criterion] = Field(min_length=1)
    pass_threshold: float = Field(default=0.8, ge=0, le=1)

    @model_validator(mode="after")
    def unique_ids(self):
        if len({c.id for c in self.criteria}) != len(self.criteria):
            raise ValueError("criterion IDs must be unique")
        return self


class Option(Contract):
    id: str = Field(min_length=1)
    label: str = Field(min_length=1)


class Question(Contract):
    id: str = Field(min_length=1)
    revision: int = Field(default=1, ge=1)
    question: str = Field(min_length=1)
    options: list[Option] = Field(min_length=2, max_length=3)
    assumption: str = Field(min_length=1)

    @model_validator(mode="after")
    def unique_ids(self):
        if len({o.id for o in self.options}) != len(self.options):
            raise ValueError("option IDs must be unique")
        return self


class PossibleCause(Contract):
    observed_issue: str = Field(min_length=1)
    reason: str = Field(min_length=1)
    uncertainty: str = Field(min_length=1)
    evidence_refs: list[EvidenceRef] = Field(min_length=1)


class Attribution(Contract):
    possible_causes: list[PossibleCause] = Field(min_length=1)


class Analysis(Contract):
    sufficient_information: bool
    hypothesis: str | None = None
    questions: list[Question] = Field(default_factory=list)
    rubrics: RubricsContent
    attribution: Attribution | None = None

    @model_validator(mode="after")
    def necessary_questions(self):
        if self.sufficient_information and (self.questions or self.hypothesis):
            raise ValueError(
                "sufficient information must not produce hypotheses or questions"
            )
        if not self.sufficient_information and (
            not self.questions or not self.hypothesis
        ):
            raise ValueError(
                "missing necessary information requires a hypothesis and selectable questions"
            )
        if len({q.id for q in self.questions}) != len(self.questions):
            raise ValueError("question IDs must be unique")
        return self


class RecipeContent(Contract):
    title: str = Field(min_length=1)
    applicability: str = Field(min_length=1)
    instructions: str = Field(min_length=1)


class ObjectiveCheck(Contract):
    kind: Literal["exists", "text_equals", "text_contains", "sha256"]
    path: str = Field(min_length=1)
    value: str | None = None

    @model_validator(mode="after")
    def valid_path_and_value(self):
        from pathlib import PurePosixPath

        path = PurePosixPath(self.path)
        if (
            path.is_absolute()
            or ".." in path.parts
            or not path.name
            or "\\" in self.path
            or ":" in self.path
        ):
            raise ValueError("check path must be a relative workspace file")
        if self.kind != "exists" and self.value is None:
            raise ValueError("this objective check requires a value")
        return self


class TestTask(Contract):
    instruction: str = Field(min_length=1)
    input_locators: list[str] | None = None
    shared_reference_locators: list[str] | None = None
    # Deterministic checks on files produced in a fresh test workspace.
    checks: list[ObjectiveCheck] = Field(default_factory=list)


class ExecutionPolicy(Contract):
    artifact_confirmed: Literal[True]
    confirmation_ref: str = Field(min_length=1)
    max_rounds: int = Field(default=2, ge=1, le=10)
    auto_activate: bool = False
    test_task: TestTask | None = None


class CriterionScore(Contract):
    criterion_id: str
    score: float = Field(ge=0, le=1)
    evidence: str = Field(min_length=1)


class Assessment(Contract):
    verifiable: bool
    scores: list[CriterionScore]
    reason: str


TERMINAL = {"completed", "failed", "cancelled"}
