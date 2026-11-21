from __future__ import annotations

import hashlib
import json
import math
import re
from pathlib import Path, PurePosixPath
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from dotenv import dotenv_values


def canonical(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def safe_path(value: str) -> str:
    path = PurePosixPath(value)
    if not value or path.is_absolute() or ".." in path.parts or "\\" in value:
        raise ValueError("path must be a relative POSIX path without traversal")
    if any(p.startswith(".") for p in path.parts):
        raise ValueError("hidden paths are not part of the task contract")
    return str(path)


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


MODEL_TASK_VIEW = "issue_files_and_public_tests_v1"


def model_task_view(task: dict) -> dict:
    """Allowlist what a model can see, including when replaying an older START event."""
    return {
        key: task[key]
        for key in ("repository", "issue", "files", "editable", "public_command")
        if key in task
    }


class Task(Strict):
    id: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,80}$")
    source_group: str = Field(min_length=1, max_length=160)
    repository: str = Field(min_length=1, max_length=160)
    split: Literal["learn", "dev", "test"]
    issue: str = Field(min_length=1, max_length=16000)
    files: dict[str, str]
    editable: list[str] = Field(min_length=1, max_length=80)
    public_command: list[str] = Field(min_length=1, max_length=30)
    grader_files: dict[str, str]
    grader_command: list[str] = Field(min_length=1, max_length=30)

    @model_validator(mode="after")
    def validate_files(self):
        for path in [*self.files, *self.grader_files, *self.editable]:
            safe_path(path)
        if not set(self.editable) <= set(self.files):
            raise ValueError("editable files must exist in frozen input")
        if set(self.grader_files) & set(self.files):
            raise ValueError("grader files must be absent from solver files")
        if sum(len(v.encode()) for v in [*self.files.values(), *self.grader_files.values()]) > 2_000_000:
            raise ValueError("task snapshot exceeds 2MB limit")
        return self

    def public(self):
        """Local trace view, including the group used by the evaluator."""
        return {"id": self.id, "source_group": self.source_group,
                "repository": self.repository, "issue": self.issue,
                "files": sorted(self.files), "editable": self.editable,
                "public_command": self.public_command}


class Action(Strict):
    tool: Literal["read", "search", "write", "edit", "command", "test", "finish"]
    path: str = Field(default="", description="Relative file path for read/write/edit; optional file or directory scope for search")
    content: str = Field(default="", max_length=100000)
    old_text: str = Field(default="", max_length=20000, description="For edit: nonempty exact source fragment occurring once")
    new_text: str = Field(default="", max_length=20000, description="For edit: replacement text, different from old_text")
    start_line: int = Field(default=1, ge=1)
    line_count: int = Field(default=120, ge=1, le=200)
    query: str = Field(default="", max_length=200)
    argv: list[str] = Field(default_factory=list, max_length=30)
    reason: str = Field(default="[model omitted action rationale]", min_length=1, max_length=1500)

    @model_validator(mode="after")
    def validate_action(self):
        if self.tool in {"read", "write", "edit"}:
            safe_path(self.path)
        if self.tool == "search" and self.path:
            safe_path(self.path)
        if self.tool == "edit" and (not self.old_text or self.old_text == self.new_text):
            raise ValueError("edit needs a nonempty unique old_text and a changed new_text")
        if self.tool == "command" and not self.argv:
            raise ValueError("command requires argv")
        if self.tool == "search" and not self.query:
            raise ValueError("search requires a literal query")
        return self


class CheckpointAction(Action):
    """Opt-in solver contract; existing experiments retain the original wire schema."""
    tool: Literal["read", "search", "write", "edit", "command", "test", "finish", "restore_checkpoint"]
    checkpoint_id: str = Field(default="", pattern=r"^(?:[0-9a-f]{64})?$",
                               description="For restore_checkpoint only: a prior green hash from this run, or omit for latest")


class CheckAction(CheckpointAction):
    """Optional self-authored, replayable Python CLI check."""
    tool: Literal["read", "search", "write", "edit", "command", "test", "finish", "restore_checkpoint", "cli_check"]

    @model_validator(mode="after")
    def check_source(self):
        if self.tool == "cli_check" and (not self.content.strip() or len(self.content) > 12000):
            raise ValueError("cli_check requires a standalone Python script in content, up to 12000 characters")
        return self


class SkillContent(Strict):
    """Model-generated procedure; lineage is immutable runtime metadata."""

    name: str = Field(pattern=r"^[a-z][a-z0-9-]{2,60}$")
    applicability: list[str] = Field(min_length=1, max_length=8)
    steps: list[str] = Field(min_length=1, max_length=12)
    verification: list[str] = Field(min_length=1, max_length=8)
    exclusions: list[str] = Field(min_length=1, max_length=8)
    counterexamples: list[str] = Field(default_factory=list, max_length=8)
    evidence_refs: list[str] = Field(min_length=1, max_length=12)
    change_summary: str = Field(min_length=10, max_length=2000)

    @field_validator("applicability", "steps", "verification", "exclusions", "counterexamples")
    @classmethod
    def bounded_items(cls, values):
        if any(not v.strip() or len(v) > 1000 for v in values):
            raise ValueError("skill item must contain 1..1000 characters")
        return values

class Skill(SkillContent):
    parent: str | None

    @property
    def hash(self):
        return digest(self.model_dump())


class RoutingEvidence(Strict):
    condition_indices: list[int] = Field(max_length=8, description="Zero-based applicability clauses actually supported by the issue, not all clauses checked")
    exclusion_indices: list[int] = Field(max_length=8, description="Zero-based exclusions that ACTUALLY APPLY and block using the skill. Not exclusions merely checked. Must be [] when decision is apply")
    task_quotes: list[str] = Field(max_length=6, description="Exact literal substrings from the issue supporting the decision; do not paraphrase")
    reason: str = Field(min_length=1, max_length=1200)

    @field_validator("task_quotes")
    @classmethod
    def bounded_quotes(cls, values):
        if any(not v.strip() or len(v) > 400 for v in values):
            raise ValueError("task quotes must have 1..400 characters")
        return values


class SkillRoute(RoutingEvidence):
    decision: Literal["apply", "skip", "uncertain"]


class LibraryRoute(SkillRoute):
    skill_id: str | None


class LibrarySelection(RoutingEvidence):
    """Wire decision: choose a bounded integer; runtime binds the artifact ID."""

    skill_index: int = Field(
        ge=-2, le=7,
        description="Zero-based input skill number; -1 to skip as inapplicable, -2 when uncertain",
    )
    exclusion_indices: list[int] = Field(max_length=0, description="Return an empty array; explain exclusions in reason")

    @property
    def decision(self) -> Literal["apply", "skip", "uncertain"]:
        if self.skill_index == -2:
            return "uncertain"
        return "skip" if self.skill_index == -1 else "apply"

    @model_validator(mode="after")
    def validate_selection(self, info: ValidationInfo):
        if info.context and self.skill_index >= info.context["skill_count"]:
            raise ValueError("skill index is outside the supplied library")
        return self


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="EVOCODER_", env_file=".env", extra="ignore")
    mode: Literal["mock", "live"] = "mock"
    base_url: str = ""
    api_key: str = ""
    model: str = ""
    provider: Literal["compatible", "glm"] = "compatible"
    disable_thinking: bool = False
    reasoning_effort: Literal["low", "high", "max"] | None = None
    temperature: float = Field(default=0, ge=0, le=2)
    billing_mode: Literal["token_usd", "subscription_credits"] = "token_usd"
    credit_budget: float = 0
    unlimited_credits: bool = False
    input_credits_per_million: float = 0
    cached_input_credits_per_million: float = 0
    output_credits_per_million: float = 0
    budget_usd: float = 0
    input_price_per_million: float = 0
    cached_input_price_per_million: float = Field(default=0, ge=0)
    output_price_per_million: float = 0
    max_output_tokens: int = Field(default=4096, ge=256, le=16000)
    max_input_bytes: int = Field(default=65536, ge=4096, le=262144)
    model_timeout: int = Field(default=60, ge=1, le=300)
    max_steps: int = Field(default=16, ge=4, le=100)
    command_timeout: int = Field(default=20, ge=1, le=120)
    skill_routing: Literal["off", "semantic"] = "semantic"
    recover_actions: bool = True
    public_checkpoints: bool = False
    authored_cli_checks: bool = False
    live_switch_feedback: bool = False
    max_memory_bytes: int = Field(default=16384, ge=1024, le=32768)
    image: str = "python:3.12-slim"
    state_dir: Path = Path(".state")

    @classmethod
    def from_env_file(cls, path: Path):
        if not path.is_file():
            raise ValueError("explicit provider configuration file is missing")
        values = {
            key.removeprefix("EVOCODER_").lower(): value
            for key, value in dotenv_values(path).items()
            if key.startswith("EVOCODER_") and value is not None
        }
        if "api_key" not in values:
            raise ValueError("explicit provider configuration must contain EVOCODER_API_KEY")
        return cls(_env_file=None, **values)

    def validate_live(self):
        if self.mode != "live":
            return
        parts = urlsplit(self.base_url)
        if (parts.scheme not in {"https", "http"} or not parts.hostname or parts.username
                or parts.password or parts.query or parts.fragment):
            raise ValueError("live base URL must be HTTP(S), without embedded credentials/query")
        if not self.api_key or not self.model or not re.search(r"\S", self.model):
            raise ValueError("live mode requires API key and model; no mock fallback")
        if self.provider == "glm" and self.model.startswith("glm-5.3") and self.disable_thinking:
            raise ValueError("GLM-5.3 requires thinking enabled")
        if self.billing_mode == "subscription_credits":
            if (
                self.provider != "glm" or parts.scheme != "https"
                or parts.hostname not in {"open.bigmodel.cn", "api.z.ai"}
                or parts.path.rstrip("/") != "/api/coding/paas/v4"
            ):
                raise ValueError("subscription credits require an official GLM Coding endpoint")
            cap, incoming, cached, outgoing = self.accounting_rates()
            numbers = [incoming, outgoing] + ([] if self.unlimited_credits else [cap])
        else:
            cap, incoming, cached, outgoing = self.accounting_rates()
            numbers = [cap, incoming, outgoing]
        for number in numbers:
            if not math.isfinite(number) or number <= 0:
                raise ValueError("live mode requires positive finite budget and token prices or credit rates")
        if not math.isfinite(cached) or not 0 <= cached <= incoming:
            raise ValueError("cached input price must be finite and no higher than ordinary input price")

    @property
    def accounting_unit(self):
        return "credits" if self.billing_mode == "subscription_credits" else "usd"

    def accounting_rates(self):
        if self.billing_mode == "subscription_credits":
            return (self.credit_budget, self.input_credits_per_million,
                    self.cached_input_credits_per_million, self.output_credits_per_million)
        return (self.budget_usd, self.input_price_per_million,
                self.cached_input_price_per_million, self.output_price_per_million)

    def public(self):
        excluded = {"api_key"}
        if not self.live_switch_feedback:
            excluded.add("live_switch_feedback")
        if not self.authored_cli_checks:
            excluded.add("authored_cli_checks")
        if not self.public_checkpoints:
            # Preserve the configuration identity of historical runs with the feature off.
            excluded.add("public_checkpoints")
        return self.model_dump(mode="json", exclude=excluded)
