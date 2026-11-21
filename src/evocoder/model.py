from __future__ import annotations

from decimal import Decimal
from hashlib import sha256
import re

import httpx
from pydantic import BaseModel, ValidationError

from evocoder.contracts import (
    Action,
    CheckpointAction,
    CheckAction,
    LibraryRoute,
    LibrarySelection,
    Settings,
    Skill,
    SkillContent,
    SkillRoute,
    canonical,
)
from evocoder.step_contract import StepRevision
from evocoder.context import compact
from evocoder.errors import ActionRejected, RoutingRejected
from evocoder.store import Store, nanos


class Model:
    def __init__(self, settings: Settings, store: Store, transport=None):
        settings.validate_live()
        if settings.accounting_unit != store.unit:
            raise ValueError("model billing mode differs from accounting ledger unit")
        self.settings, self.store, self.transport = settings, store, transport

    def request(self, schema: type[BaseModel], system: str, data: dict, run_id: str):
        if self.settings.mode == "mock":
            if schema is Action:
                return self.mock_action(data)
            if schema is SkillRoute:
                if "offline contract fixture" not in data["task"]["issue"]:
                    raise ValueError("mock routing only supports labelled engineering fixtures")
                return SkillRoute(
                    decision="apply",
                    condition_indices=[0],
                    exclusion_indices=[],
                    task_quotes=[data["task"]["issue"][:120]],
                    reason="Scripted fixture routing; not a relevance quality measurement",
                )
            if schema is LibraryRoute:
                raise ValueError(
                    "multi-skill routing requires live mode or an explicit controlled test model"
                )
            if schema is StepRevision:
                if not all("offline contract fixture" in e["task"]["issue"] for e in data["evidence"]["episodes"]):
                    raise ValueError("mock procedural proposals require labelled fixtures")
                return StepRevision.model_validate({"steps": [{"when": "after_edit", "action": "run_suite",
                    "target": "reported_case", "observable": "output"}], "facts": [],
                    "evidence_refs": data["allowed_evidence_refs"]})
            return self.mock_skill(data)
        s = self.settings
        cap, input_rate, cache_rate, output_rate = s.accounting_rates()
        functions = {
            Action: "submit_action",
            Skill: "submit_skill",
            StepRevision: "submit_procedure",
            SkillRoute: "select_skill",
            LibraryRoute: "select_library_skill",
        }
        if schema not in functions:
            raise ValueError("unsupported model decision schema")
        function = functions[schema]
        wire_schema = {Skill: SkillContent, LibraryRoute: LibrarySelection}.get(schema, schema)
        if schema is Action and s.public_checkpoints:
            wire_schema = CheckpointAction
        if schema is Action and (s.authored_cli_checks or s.live_switch_feedback):
            wire_schema = CheckAction
        wire_data = data
        parameters = wire_schema.model_json_schema()
        if schema is Action and (s.authored_cli_checks or s.live_switch_feedback) and not s.public_checkpoints:
            parameters["properties"]["tool"]["enum"].remove("restore_checkpoint")
        if schema is Skill:
            expected_parent = data["parent"]["id"] if data.get("parent") else None
            system += (
                "\nGenerate only the SkillContent fields defined by the tool. Do not return a parent "
                "field: the runtime owns and binds immutable lineage metadata. The input parent "
                "is read-only revision context, not an output field."
            )
        if schema in {Skill, StepRevision} and data.get("allowed_evidence_refs"):
            parameters["properties"]["evidence_refs"]["items"]["enum"] = data["allowed_evidence_refs"]
        if schema is SkillRoute:
            parameters["properties"]["condition_indices"]["items"]["enum"] = list(
                range(len(data["skill"]["applicability"]))
            )
            parameters["properties"]["exclusion_indices"]["items"]["enum"] = list(
                range(len(data["skill"]["exclusions"]))
            )
        if schema is LibraryRoute:
            if not 1 <= len(data["skills"]) <= 8:
                raise ValueError("routing requires 1..8 skills")
            parameters["properties"]["skill_index"]["enum"] = [-2, -1, *range(len(data["skills"]))]
            wire_data = {
                **data,
                "skills": [
                    {**{key: value for key, value in skill.items() if key != "id"}, "skill_index": index}
                    for index, skill in enumerate(data["skills"])
                ],
            }
            system += (
                "\nReturn only the required tool fields. skill_index is the decision: "
                "choose an input index to apply, -1 to skip, or -2 if uncertain. "
                "Use an integer. The runtime binds artifact IDs."
            )
        body = {
            "model": s.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": canonical(wire_data)},
            ],
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": function,
                        "description": "Return exactly one complete structured result",
                        "parameters": parameters,
                    },
                }
            ],
            "tool_choice": {"type": "function", "function": {"name": function}},
            "max_tokens": s.max_output_tokens,
            "temperature": s.temperature,
        }
        if s.provider == "glm":
            body["thinking"] = {"type": "disabled" if s.disable_thinking else "enabled"}
            body["tool_choice"] = "auto"
            if s.reasoning_effort:
                body["reasoning_effort"] = s.reasoning_effort
            body["messages"][0]["content"] += (
                "\nReturn exactly one call to the provided function "
                + function
                + "; put the complete result in its arguments. Do not answer in plain text."
            )
        elif s.disable_thinking:
            body["thinking"] = {"type": "disabled"}
        # Byte length is a conservative input bound for common byte-tokenized providers,
        # with extra protocol overhead. An observed overrun seals the cumulative budget.
        encoded = canonical(body).encode()
        original_size = len(encoded)
        if schema is Action and data.get("history"):
            for level in (1, 2, 3):
                if len(encoded) <= s.max_input_bytes:
                    break
                projection = compact(data, level)
                body["messages"][1]["content"] = canonical(projection)
                encoded = canonical(body).encode()
            if len(encoded) != original_size:
                self.store.event(
                    run_id,
                    "MODEL_INPUT_COMPACTED",
                    {
                        "before_bytes": original_size,
                        "after_bytes": len(encoded),
                        "projection": projection["history_compaction"],
                    },
                )
        if len(encoded) > s.max_input_bytes:
            raise ValueError("model input exceeds configured byte budget")
        maximum = nanos(
            (
                Decimal(len(encoded) + 2048) * Decimal(str(input_rate))
                + Decimal(s.max_output_tokens) * Decimal(str(output_rate))
            )
            / 1_000_000
        )
        limit = (
            (1 << 63) - 1 if s.billing_mode == "subscription_credits" and s.unlimited_credits else nanos(cap)
        )
        call_id = self.store.reserve(run_id, limit, maximum)
        # Uncertain HTTP responses keep reservations. No automatic retry/fallback.
        with httpx.Client(
            timeout=s.model_timeout, transport=self.transport, follow_redirects=False
        ) as client:
            response = client.post(
                s.base_url.rstrip("/") + "/chat/completions",
                headers={"Authorization": "Bearer " + s.api_key},
                json=body,
            )
        if response.status_code != 200:
            raise RuntimeError(f"model_http_{response.status_code}: reservation retained")
        payload = response.json()
        usage = payload.get("usage", {})
        incoming, outgoing = usage.get("prompt_tokens"), usage.get("completion_tokens")
        if type(incoming) is not int or type(outgoing) is not int or incoming < 0 or outgoing < 0:
            raise ValueError("invalid model usage: reservation retained")
        hit, miss = usage.get("prompt_cache_hit_tokens"), usage.get("prompt_cache_miss_tokens")
        if s.provider == "glm":
            details = usage.get("prompt_tokens_details")
            hit = details.get("cached_tokens") if isinstance(details, dict) else None
            miss = incoming - hit if type(hit) is int else None
        cache_valid = (
            cache_rate > 0
            and type(hit) is int
            and type(miss) is int
            and hit >= 0
            and miss >= 0
            and hit + miss == incoming
        )
        input_cost = (
            (Decimal(hit) * Decimal(str(cache_rate)) + Decimal(miss) * Decimal(str(input_rate)))
            if cache_valid
            else (Decimal(incoming) * Decimal(str(input_rate)))
        )
        actual = nanos((input_cost + Decimal(outgoing) * Decimal(str(output_rate))) / 1_000_000)
        self.store.settle(
            call_id,
            actual,
            {
                "input_tokens": incoming,
                "output_tokens": outgoing,
                "cache_pricing_applied": cache_valid,
                "cache_hit_tokens": hit if cache_valid else None,
                "cache_miss_tokens": miss if cache_valid else None,
                "accounting_unit": s.accounting_unit,
                "input_rate_per_million": input_rate,
                "cached_input_rate_per_million": cache_rate,
                "output_rate_per_million": output_rate,
            },
        )
        choices = payload.get("choices", [])
        calls = choices[0].get("message", {}).get("tool_calls", []) if len(choices) == 1 else []
        diagnostic = {
            "call_id": call_id,
            "request_body_hash": sha256(encoded).hexdigest(),
            "choice_count": len(choices),
            "provider_model": str(payload.get("model", ""))[:200].replace(s.api_key, "[redacted]"),
            "system_fingerprint": str(payload.get("system_fingerprint", ""))[:200].replace(
                s.api_key, "[redacted]"
            ),
            "finish_reason": choices[0].get("finish_reason") if len(choices) == 1 else None,
            "function_count": len(calls),
            "expected_function": function,
            "function_names": [
                re.sub(r"[^A-Za-z0-9_-]", "?", str(c.get("function", {}).get("name", "")))[:80]
                for c in calls[:8]
            ],
        }
        self.store.event(run_id, "MODEL_RESPONSE", diagnostic)
        if len(choices) != 1 or choices[0].get("finish_reason") != "tool_calls":
            if schema is Action:
                issues = [
                    {
                        "loc": ["finish_reason"],
                        "type": "incomplete_decision",
                        "msg": "Expected one complete tool-call decision. No tools executed. "
                        "Use a short exact edit or submit a valid finish within the remaining steps. "
                        f"Received finish_reason={diagnostic['finish_reason']}",
                    }
                ]
                artifact = self.store.artifact(
                    "rejected_model_arguments",
                    {
                        "schema": "Action",
                        "tool_calls": canonical(calls)[:64000].replace(s.api_key, "[redacted]"),
                        "finish_reason": diagnostic["finish_reason"],
                    },
                )
                self.store.event(
                    run_id,
                    "MODEL_ARGUMENTS_REJECTED",
                    {"schema": "Action", "issues": issues, "artifact": artifact, "call_id": call_id},
                )
                raise ActionRejected(issues, artifact)
            if schema in {SkillRoute, LibraryRoute}:
                raise RoutingRejected("invalid routing envelope; memory skipped without retry")
            raise ValueError("model_contract: expected one complete tool-call choice")
        if len(calls) != 1 or calls[0].get("function", {}).get("name") != function:
            if schema is Action:
                issues = [
                    {
                        "loc": ["function"],
                        "type": "invalid_tool_call",
                        "msg": f"Expected exactly one {function}. No tools executed. Received: "
                        f"{diagnostic['function_names']}",
                    }
                ]
                raw = canonical(calls)[:64000].replace(s.api_key, "[redacted]")
                artifact = self.store.artifact(
                    "rejected_model_arguments", {"schema": "Action", "tool_calls": raw}
                )
                self.store.event(
                    run_id,
                    "MODEL_ARGUMENTS_REJECTED",
                    {"schema": "Action", "issues": issues, "artifact": artifact, "call_id": call_id},
                )
                raise ActionRejected(issues, artifact)
            if schema in {SkillRoute, LibraryRoute}:
                raise RoutingRejected("unexpected routing function; memory skipped without retry")
            raise ValueError(
                f"model_contract: expected exactly one {function}; "
                f"count={len(calls)}, names={diagnostic['function_names']}"
            )
        arguments = calls[0]["function"].get("arguments", "")
        try:
            decision = wire_schema.model_validate_json(
                arguments,
                context={"skill_count": len(data["skills"])} if schema is LibraryRoute else None,
            )
            if schema is Skill:
                artifact = self.store.artifact("model_proposal_content", decision.model_dump())
                self.store.event(
                    run_id,
                    "PROPOSAL_RUNTIME_BINDING",
                    {"contract": "skill_content_v2", "parent": expected_parent, "content_artifact": artifact},
                )
                return Skill.model_validate({**decision.model_dump(), "parent": expected_parent})
            if schema is LibraryRoute:
                selected = data["skills"][decision.skill_index]["id"] if decision.skill_index >= 0 else None
                artifact = self.store.artifact("model_library_selection", decision.model_dump())
                self.store.event(
                    run_id,
                    "ROUTING_RUNTIME_BINDING",
                    {
                        "contract": "integer_library_selection_v3",
                        "skill_index": decision.skill_index,
                        "decision": decision.decision,
                        "skill_id": selected,
                        "content_artifact": artifact,
                    },
                )
                return LibraryRoute.model_validate(
                    {**decision.model_dump(exclude={"skill_index"}), "decision": decision.decision, "skill_id": selected}
                )
            return decision
        except ValidationError as exc:
            if schema not in {Action, Skill, StepRevision, SkillRoute, LibraryRoute}:
                raise
            issues = exc.errors(include_input=False, include_context=False, include_url=False)
            issues = [{"loc": list(e["loc"]), "type": e["type"], "msg": e["msg"][:500]} for e in issues[:8]]
            raw = str(arguments)[:64000].replace(s.api_key, "[redacted]")
            artifact = self.store.artifact(
                "rejected_model_arguments", {"schema": schema.__name__, "arguments": raw}
            )
            self.store.event(
                run_id,
                "MODEL_ARGUMENTS_REJECTED",
                {"schema": schema.__name__, "issues": issues, "artifact": artifact, "call_id": call_id},
            )
            if schema in {Skill, StepRevision}:
                raise
            if schema is Action:
                raise ActionRejected(issues, artifact) from None
            raise RoutingRejected("invalid routing response; memory skipped") from None

    @staticmethod
    def mock_action(data):
        """Scripted fixture driver, NOT an autonomous-learning performance measurement."""
        if "offline contract fixture" not in data["task"]["issue"]:
            raise ValueError("mock only supports the labelled fixture tasks; use live for real repositories")
        history = data["history"]
        tools = [h["action"]["tool"] for h in history]
        source = data["task"]["editable"][0]
        if "read" not in tools:
            return Action(tool="read", path=source, reason="Inspect implementation")
        if "write" not in tools:
            robust = any("explicit_none" in step for skill in data["skills"] for step in skill["steps"])
            text = next(h["observation"]["output"] for h in history if h["action"]["tool"] == "read")
            function = text.split("def ", 1)[1].split("(", 1)[0]
            expression = "('' if value is None else str(value))" if robust else "(value or '')"
            code = f"def {function}(value):\n    return {expression}.strip().lower()\n"
            return Action(tool="write", path=source, content=code, reason="Handle optional input")
        if "test" not in tools:
            return Action(tool="test", reason="Run repository public tests")
        return Action(tool="finish", reason="Submit current patch; evaluator determines outcome")

    @staticmethod
    def mock_skill(data):
        refs = [e["run_id"] for e in data["evidence"]["episodes"]]
        parent = data["parent"]
        failed = any(e["public_failed"] for e in data["evidence"]["episodes"])
        robust = parent is not None and failed
        return Skill(
            name="optional-input-normalization",
            parent=parent["id"] if parent else None,
            applicability=["Normalization functions accepting optional text input"],
            steps=(
                [
                    "explicit_none: distinguish None from valid falsy values; inspect the input contract",
                    "Convert accepted non-text scalars explicitly before string methods",
                ]
                if robust
                else ["Use a fallback for absent values before applying text normalization"]
            ),
            verification=["Run public tests for None, empty text and supported scalar boundaries"],
            exclusions=[
                "Do not assume every falsy value denotes absence"
                if robust
                else "Not verified for numeric or boolean inputs"
            ],
            counterexamples=["Zero and False are valid values; broad truthiness loses them"]
            if robust
            else [],
            evidence_refs=refs,
            change_summary=(
                "Revise the parent using observed scalar boundary failures; preserve valid falsy values."
                if robust
                else "Extract a provisional optional-input procedure from the successful trace."
            ),
        )
