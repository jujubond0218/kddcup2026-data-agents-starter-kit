from __future__ import annotations

import json
from dataclasses import dataclass

from data_agent_baseline.agents.evidence_plan import EvidencePlanController
from data_agent_baseline.agents.model import (
    ModelAdapter,
    ModelMessage,
    ModelResponse,
    ModelToolCall,
)
from data_agent_baseline.agents.prompt import (
    REACT_SYSTEM_PROMPT,
    build_system_prompt,
    build_task_prompt,
)
from data_agent_baseline.agents.runtime import AgentRunResult, AgentRuntimeState, StepRecord
from data_agent_baseline.benchmark.schema import AnswerTable, PublicTask
from data_agent_baseline.events import EventSink, emit_event
from data_agent_baseline.tools.registry import ToolExecutionResult, ToolRegistry
from data_agent_baseline.verification import AnswerVerifier, AnswerVerificationFailure


@dataclass(frozen=True, slots=True)
class ReActAgentConfig:
    max_steps: int = 16
    evidence_plan_enabled: bool = False
    evidence_plan_max_commit_attempts: int = 2
    evidence_plan_strict_keys: bool = True
    evidence_plan_verification_gate: bool = True


def _step_budget_reminder(
    *,
    used_steps: int,
    max_steps: int,
    warning_fired: bool,
    critical_fired: bool,
) -> tuple[str, str] | None:
    if max_steps <= 0:
        return None

    ratio = used_steps / max_steps
    remaining_steps = max_steps - used_steps
    if ratio >= 0.90 and not critical_fired:
        return (
            "critical",
            (
                f"STEP BUDGET CRITICAL: {used_steps}/{max_steps} normal steps used; "
                f"{remaining_steps} remain. Prioritize submitting the best evidence-backed "
                "table with `answer`. Do not start broad new exploration."
            ),
        )
    if ratio >= 0.70 and not warning_fired:
        return (
            "warning",
            (
                f"STEP BUDGET WARNING: {used_steps}/{max_steps} normal steps used; "
                f"{remaining_steps} remain. If the observed evidence already supports a "
                "final table, call `answer` now. Otherwise perform only the highest-value "
                "remaining check, avoid repeating completed work, and converge."
            ),
        )
    return None


def _assistant_message(response: ModelResponse) -> ModelMessage:
    return ModelMessage(
        role="assistant",
        content=response.content,
        tool_calls=response.tool_calls,
    )


def _tool_message(call: ModelToolCall, observation: dict[str, object]) -> ModelMessage:
    return ModelMessage(
        role="tool",
        content=json.dumps(observation, ensure_ascii=False, separators=(",", ":"), default=str),
        tool_call_id=call.id,
    )


def _protocol_error_observation(code: str, message: str) -> dict[str, object]:
    return {
        "ok": False,
        "content": {
            "error": {
                "code": code,
                "message": message,
                "recoverable": True,
            }
        },
    }


def _plan_pending_result(tool_name: str) -> ToolExecutionResult:
    """Recoverable correction for an unrelated tool called while the plan is pending.

    Runtime enforcement: the wrong call is not executed, does not consume a bounded
    commit attempt, and the agent is told to commit the plan instead. Only the step
    budget fails the phase open.
    """
    return ToolExecutionResult(
        ok=False,
        content={
            "error": {
                "code": "EVIDENCE_PLAN_PENDING",
                "message": (
                    f"The plan is still pending and `{tool_name}` is not available. "
                    "Call `commit_evidence_plan` now to commit a deterministic plan "
                    "covering every pending requirement and uncertainty from the "
                    "explore report."
                ),
                "recoverable": True,
                "do_not_retry_same_call": True,
            }
        },
        error_code="EVIDENCE_PLAN_PENDING",
        recoverable=True,
    )


def _final_step_guard_observation(tool_name: str) -> dict[str, object]:
    return {
        "ok": False,
        "tool": tool_name,
        "content": {
            "error": {
                "code": "FINAL_STEP_REQUIRES_ANSWER",
                "message": (
                    "The last normal step is reserved for submitting the final answer. "
                    f"The requested non-terminal tool `{tool_name}` was not executed."
                ),
                "guidance": (
                    "Call `answer` now using the strongest evidence already obtained. "
                    "Do not call another analysis tool."
                ),
                "required_tool": "answer",
                "remaining_normal_steps": 0,
                "recoverable": True,
                "do_not_retry_same_call": True,
            }
        },
    }


def _repeatable_call_identity(
    call: ModelToolCall,
    tools: ToolRegistry,
) -> tuple[str, dict[str, object]] | None:
    spec = tools.specs.get(call.name)
    if spec is None or spec.is_terminal or call.name in {"explore", "commit_evidence_plan"}:
        return None
    try:
        raw_arguments = json.loads(call.arguments or "{}")
        if not isinstance(raw_arguments, dict):
            return None
        validated = spec.input_model.model_validate(raw_arguments)
    except (json.JSONDecodeError, ValueError):
        return None
    normalized = validated.model_dump(mode="json")
    fingerprint = json.dumps(
        {"tool": call.name, "arguments": normalized},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return fingerprint, normalized


def _repeated_call_observation(
    *,
    tool_name: str,
    repeat_count: int,
    first_step: int,
    previous_step: int,
) -> dict[str, object]:
    return {
        "ok": False,
        "tool": tool_name,
        "content": {
            "error": {
                "code": "REPEATED_IDENTICAL_TOOL_CALL",
                "message": (
                    "Identical call blocked after two consecutive attempts. Reuse the previous "
                    "observations instead of retrying unchanged. Review the available evidence, "
                    "Explorer report, or current plan, then change the arguments, source, or "
                    "approach—or submit the best supported answer."
                ),
                "recoverable": True,
                "do_not_retry_same_call": True,
            },
            "repeat_count": repeat_count,
            "first_step": first_step,
            "previous_step": previous_step,
        },
    }


def _answer_verification_event_payload(
    *,
    call: ModelToolCall,
    answer: AnswerTable,
    step_index: int,
    failure: AnswerVerificationFailure,
) -> dict[str, object]:
    return {
        "step_index": step_index,
        "tool_call_id": call.id,
        "column_count": len(answer.columns),
        "row_count": len(answer.rows),
        "verification_code": failure.code,
        "error": failure.message,
    }


class ReActAgent:
    def __init__(
        self,
        *,
        model: ModelAdapter,
        tools: ToolRegistry,
        config: ReActAgentConfig | None = None,
        system_prompt: str | None = None,
        event_sink: EventSink | None = None,
        answer_verifier: AnswerVerifier | None = None,
    ) -> None:
        self.model = model
        self.tools = tools
        self.config = config or ReActAgentConfig()
        self.system_prompt = system_prompt or REACT_SYSTEM_PROMPT
        self.event_sink = event_sink
        self.answer_verifier = answer_verifier or AnswerVerifier()
        self.evidence_plan: EvidencePlanController | None = None

    def _exploration_required(self) -> bool:
        return "explore" in self.tools.specs

    def _active_tools(
        self,
        *,
        exploration_pending: bool,
        exploration_required: bool,
        evidence_plan_pending: bool = False,
    ) -> ToolRegistry:
        if evidence_plan_pending and self.evidence_plan is not None:
            return self.evidence_plan.registry()
        if exploration_pending:
            return ToolRegistry(specs={"explore": self.tools.specs["explore"]})
        if exploration_required:
            return ToolRegistry(
                specs={name: spec for name, spec in self.tools.specs.items() if name != "explore"}
            )
        return self.tools

    def _initial_messages(self, task: PublicTask) -> list[ModelMessage]:
        exploration_required = self._exploration_required()
        return [
            ModelMessage(
                role="system",
                content=build_system_prompt(
                    system_prompt=self.system_prompt,
                    explore_available="explore" in self.tools.specs,
                    explore_required=exploration_required,
                    evidence_plan_enabled=self.config.evidence_plan_enabled,
                ),
            ),
            ModelMessage(
                role="user",
                content=build_task_prompt(task),
            ),
        ]

    def _record_protocol_error(
        self,
        *,
        state: AgentRuntimeState,
        messages: list[ModelMessage],
        response: ModelResponse,
        step_index: int,
        code: str,
        message: str,
    ) -> None:
        observation = _protocol_error_observation(code, message)
        emit_event(
            self.event_sink,
            "protocol_error",
            {
                "step_index": step_index,
                "error_code": code,
                "error": message,
                "tool_call_count": len(response.tool_calls),
            },
        )
        calls_are_replayable = bool(response.tool_calls) and all(
            call.id and call.name for call in response.tool_calls
        )
        if calls_are_replayable:
            messages.append(_assistant_message(response))
            for call in response.tool_calls:
                messages.append(_tool_message(call, observation))
        else:
            if response.content:
                messages.append(ModelMessage(role="assistant", content=response.content))
            messages.append(
                ModelMessage(
                    role="user",
                    content=(
                        f"Protocol error ({code}): {message} "
                        "Call exactly one provided tool through the native tool interface."
                    ),
                )
            )

        step_record = StepRecord(
            step_index=step_index,
            thought=response.content,
            action="__error__",
            action_input={},
            raw_response=response.raw_response,
            observation=observation,
            ok=False,
            finish_reason=response.finish_reason,
        )
        state.steps.append(step_record)
        emit_event(
            self.event_sink,
            "step_completed",
            {
                "step_index": step_index,
                "step": step_record.to_dict(),
            },
        )

    def _verify_terminal_answer(
        self,
        *,
        call: ModelToolCall,
        tool_result: ToolExecutionResult,
        step_index: int,
        answer_projection: dict[str, object] | None,
    ) -> ToolExecutionResult:
        answer = tool_result.answer
        if answer is None:
            return tool_result

        projection_columns = self._confirmed_projection_columns(answer_projection)
        if projection_columns is not None and len(answer.columns) != len(projection_columns):
            output_names = [str(item.get("name", "")) for item in projection_columns]
            helper_fields = self._helper_field_labels(answer_projection)
            emit_event(
                self.event_sink,
                "answer_projection_rejected",
                {
                    "step_index": step_index,
                    "tool_call_id": call.id,
                    "submitted_column_count": len(answer.columns),
                    "expected_column_count": len(projection_columns),
                },
            )
            return ToolExecutionResult(
                ok=False,
                content={
                    "error": {
                        "code": "OUTPUT_COLUMN_MISMATCH",
                        "message": (
                            f"The submitted table has {len(answer.columns)} columns, but the "
                            f"confirmed Explorer answer projection has {len(projection_columns)}."
                        ),
                        "expected_outputs": output_names,
                        "helper_fields_to_omit": helper_fields,
                        "guidance": (
                            "Submit one column per confirmed output projection. Do not merge "
                            "separate direct/semantic outputs, and omit fields used only for "
                            "filtering, joining, sorting, or grouping. Column labels may be "
                            "descriptive; derived outputs are allowed when backed by the "
                            "declared sources and operation."
                        ),
                        "recoverable": True,
                        "do_not_retry_same_call": True,
                    }
                },
                action_input=tool_result.action_input,
                error_code="OUTPUT_COLUMN_MISMATCH",
                recoverable=True,
            )

        failure = self.answer_verifier.verify(answer)
        if failure is None:
            emit_event(
                self.event_sink,
                "answer_verification_passed",
                {
                    "step_index": step_index,
                    "tool_call_id": call.id,
                    "column_count": len(answer.columns),
                    "row_count": len(answer.rows),
                },
            )
            return tool_result

        emit_event(
            self.event_sink,
            "answer_verification_rejected",
            _answer_verification_event_payload(
                call=call,
                answer=answer,
                step_index=step_index,
                failure=failure,
            ),
        )
        return ToolExecutionResult(
            ok=False,
            content={
                "error": {
                    "code": "ANSWER_VERIFICATION_ERROR",
                    "verification_code": failure.code,
                    "message": failure.message,
                    "recoverable": True,
                }
            },
            action_input=tool_result.action_input,
            error_code="ANSWER_VERIFICATION_ERROR",
            recoverable=True,
        )

    @staticmethod
    def _confirmed_projection_columns(
        answer_projection: dict[str, object] | None,
    ) -> list[dict[str, object]] | None:
        if not isinstance(answer_projection, dict):
            return None
        if answer_projection.get("enforceable") is not True:
            return None
        raw_columns = answer_projection.get("columns")
        if not isinstance(raw_columns, list) or not raw_columns:
            return None
        columns = [item for item in raw_columns if isinstance(item, dict)]
        if len(columns) != len(raw_columns):
            return None
        if any(item.get("status") != "confirmed" for item in columns):
            return None
        return columns

    @staticmethod
    def _helper_field_labels(
        answer_projection: dict[str, object] | None,
    ) -> list[str]:
        if not isinstance(answer_projection, dict):
            return []
        raw_helpers = answer_projection.get("helper_fields")
        if not isinstance(raw_helpers, list):
            return []
        labels: list[str] = []
        for item in raw_helpers:
            if not isinstance(item, dict):
                continue
            source = item.get("source")
            if not isinstance(source, dict):
                continue
            field = source.get("field")
            if isinstance(field, str) and field:
                labels.append(field)
        return list(dict.fromkeys(labels))

    def _activate_evidence_plan(
        self,
        *,
        report: dict[str, object],
        step_index: int,
    ) -> bool:
        """Build the deterministic plan controller and decide whether it is pending.

        The plan only activates when the feature is enabled, the report carries an
        unresolved requirement or uncertainty, and enough normal steps remain after
        explore. It is an opt-in experiment hook: after a successful commit the
        answer gate (Option C) may reject the first answer once while any declared
        verification is still unobserved.
        """
        if not self.config.evidence_plan_enabled:
            return False
        controller = EvidencePlanController(
            report=report,
            strict_keys=self.config.evidence_plan_strict_keys,
        )
        if not controller.triggered():
            return False
        remaining_steps = self.config.max_steps - step_index
        if remaining_steps < 2:
            emit_event(
                self.event_sink,
                "evidence_plan_skipped",
                {
                    "step_index": step_index,
                    "reason": "INSUFFICIENT_STEP_BUDGET",
                    "remaining_steps": remaining_steps,
                    "pending_requirement_count": len(controller.pending_ids),
                },
            )
            return False
        self.evidence_plan = controller
        emit_event(
            self.event_sink,
            "evidence_plan_required",
            {
                "step_index": step_index,
                "remaining_steps": remaining_steps,
                "requirement_count": len(controller.pending_ids),
                "max_commit_attempts": self.config.evidence_plan_max_commit_attempts,
            },
        )
        return True

    def _record_plan_phase_failure(
        self,
        *,
        error_code: str,
        step_index: int,
        commit_attempts: int,
        tool_call_id: str | None = None,
    ) -> tuple[bool, int]:
        """Count one plan-phase failure and return (still_pending, new_attempts).

        An invalid commit or protocol error consumes one bounded retry so a flailing
        model cannot keep the plan tool exclusive until the final normal step. A
        well-formed call to a normal tool is corrected separately by Option A and
        does not consume a commit attempt.
        """
        next_attempt = commit_attempts + 1
        remaining_steps = self.config.max_steps - step_index
        can_retry = (
            next_attempt < self.config.evidence_plan_max_commit_attempts and remaining_steps >= 2
        )
        payload: dict[str, object] = {
            "step_index": step_index,
            "error_code": error_code,
            "attempt": next_attempt,
            "max_attempts": self.config.evidence_plan_max_commit_attempts,
            "fail_open": not can_retry,
            "remaining_steps": remaining_steps,
        }
        if tool_call_id is not None:
            payload["tool_call_id"] = tool_call_id
        emit_event(
            self.event_sink,
            "evidence_plan_commit_rejected",
            payload,
        )
        return can_retry, next_attempt

    def _handle_evidence_plan_commit(
        self,
        *,
        call: ModelToolCall,
        tool_result: ToolExecutionResult,
        step_index: int,
        commit_attempts: int,
    ) -> tuple[bool, int]:
        """Process one commit_evidence_plan result; return (still_pending, attempts)."""
        if tool_result.ok:
            emit_event(
                self.event_sink,
                "evidence_plan_committed",
                {
                    "step_index": step_index,
                    "tool_call_id": call.id,
                    "item_count": int(tool_result.content.get("item_count", 0)),
                    "candidate_count": int(tool_result.content.get("candidate_count", 0)),
                    "verification_count": int(tool_result.content.get("verification_count", 0)),
                    "requirement_ids": tool_result.content.get("requirement_ids", []),
                },
            )
            return False, 0
        return self._record_plan_phase_failure(
            error_code=tool_result.error_code or "EVIDENCE_PLAN_COMMIT_REJECTED",
            step_index=step_index,
            commit_attempts=commit_attempts,
            tool_call_id=call.id,
        )

    @staticmethod
    def _call_path(action_input: dict[str, object] | None) -> str | None:
        if not isinstance(action_input, dict):
            return None
        raw_path = action_input.get("path")
        return str(raw_path) if isinstance(raw_path, str) else None

    def _emit_evidence_plan_answered(
        self,
        *,
        step_index: int,
        observed_verifications: set[tuple[str, str]],
    ) -> None:
        if self.evidence_plan is None or self.evidence_plan.committed_items is None:
            return
        committed = self.evidence_plan.committed_verifications
        emit_event(
            self.event_sink,
            "evidence_plan_answered",
            {
                "step_index": step_index,
                "plan_committed": True,
                "requirement_count": len(self.evidence_plan.pending_ids),
                "verification_total_count": len(committed),
                "verification_observed_count": len(observed_verifications),
                "all_verifications_observed": all(
                    (tool, path) in observed_verifications for tool, path in committed
                ),
            },
        )

    def _unobserved_verifications(
        self,
        observed_verifications: set[tuple[str, str]],
    ) -> list[tuple[str, str]]:
        """Unique committed (tool, path) checks not yet observed by a successful call."""
        if self.evidence_plan is None or self.evidence_plan.committed_items is None:
            return []
        committed = self.evidence_plan.committed_verifications
        return [
            (tool, path)
            for tool, path in dict.fromkeys(committed)
            if (tool, path) not in observed_verifications
        ]

    def _verification_gate_result(
        self,
        *,
        call: ModelToolCall,
        step_index: int,
        observed_verifications: set[tuple[str, str]],
        answer_blocks: int,
        finalization_retry: bool,
    ) -> ToolExecutionResult | None:
        """Reject the first unverified answer until every committed verification runs.

        Returns the recoverable EVIDENCE_PLAN_VERIFICATION_PENDING rejection when the
        plan is committed, at least one declared verification is still unobserved,
        this is the first answer after commit, and the remaining step budget can
        cover each unique missing verification plus one final answer step
        (remaining_steps >= len(missing) + 1). Every other case returns None so the
        normal terminal flow (projection gate + Answer Verifier) runs unchanged. The
        gate never reads observation content or runs new tool calls; it only reuses
        the existing successful-tool/`tool`+`path` observation set.
        """
        missing = self._unobserved_verifications(observed_verifications)
        if not missing or not self.config.evidence_plan_verification_gate:
            return None
        if answer_blocks >= 1:
            self._skip_verification_gate(step_index, "MAX_ANSWER_BLOCKS")
            return None
        if finalization_retry:
            self._skip_verification_gate(step_index, "FINALIZATION_RETRY")
            return None
        remaining_steps = self.config.max_steps - step_index
        required_steps = len(missing) + 1
        if remaining_steps < required_steps:
            self._skip_verification_gate(step_index, "INSUFFICIENT_STEP_BUDGET")
            return None
        emit_event(
            self.event_sink,
            "evidence_plan_answer_blocked",
            {
                "step_index": step_index,
                "tool_call_id": call.id,
                "missing_count": len(missing),
                "missing_verifications": [{"tool": tool, "path": path} for tool, path in missing],
            },
        )
        return ToolExecutionResult(
            ok=False,
            content={
                "error": {
                    "code": "EVIDENCE_PLAN_VERIFICATION_PENDING",
                    "message": (
                        "The committed evidence plan still has "
                        f"{len(missing)} unobserved verification(s): "
                        + ", ".join(f"`{tool}` on `{path}`" for tool, path in missing)
                        + ". Complete each missing verification with a successful "
                        "read_csv/read_json/read_doc/inspect_sqlite_schema/"
                        "execute_context_sql call on the declared path, then call "
                        "`answer` again."
                    ),
                    "recoverable": True,
                    "do_not_retry_same_call": True,
                }
            },
            error_code="EVIDENCE_PLAN_VERIFICATION_PENDING",
            recoverable=True,
        )

    def _skip_verification_gate(self, step_index: int, reason: str) -> None:
        emit_event(
            self.event_sink,
            "evidence_plan_verification_gate_skipped",
            {"step_index": step_index, "reason": reason},
        )

    def run(self, task: PublicTask) -> AgentRunResult:
        state = AgentRuntimeState()
        messages = self._initial_messages(task)
        exploration_required = self._exploration_required()
        exploration_pending = exploration_required
        answer_projection: dict[str, object] | None = None
        budget_warning_fired = False
        budget_critical_fired = False
        finalization_retry_pending = False
        evidence_plan_pending = False
        evidence_plan_commit_attempts = 0
        evidence_plan_answer_blocks = 0
        observed_verifications: set[tuple[str, str]] = set()
        last_call_fingerprint: str | None = None
        repeated_call_count = 0
        repeated_call_first_step = 0
        previous_identical_call_step = 0
        self.evidence_plan = None
        step_index = 1

        while step_index <= self.config.max_steps or finalization_retry_pending:
            finalization_retry = step_index > self.config.max_steps
            if finalization_retry:
                finalization_retry_pending = False
            emit_event(
                self.event_sink,
                "step_started",
                {
                    "step_index": step_index,
                    **({"finalization_retry": True} if finalization_retry else {}),
                },
            )
            if finalization_retry:
                active_tools = ToolRegistry(specs={"answer": self.tools.specs["answer"]})
            else:
                if evidence_plan_pending and step_index >= self.config.max_steps:
                    # Fail-open: never let the plan tool consume the final normal
                    # step, so the deterministic answer guard can still fire.
                    evidence_plan_pending = False
                reminder = _step_budget_reminder(
                    used_steps=step_index - 1,
                    max_steps=self.config.max_steps,
                    warning_fired=budget_warning_fired,
                    critical_fired=budget_critical_fired,
                )
                if reminder is not None:
                    level, content = reminder
                    messages.append(ModelMessage(role="user", content=content))
                    if level == "warning":
                        budget_warning_fired = True
                    else:
                        budget_critical_fired = True
                    emit_event(
                        self.event_sink,
                        "step_budget_warning",
                        {
                            "step_index": step_index,
                            "level": level,
                            "used_steps": step_index - 1,
                            "remaining_steps": self.config.max_steps - step_index + 1,
                            "max_steps": self.config.max_steps,
                        },
                    )
                active_tools = self._active_tools(
                    exploration_pending=exploration_pending,
                    exploration_required=exploration_required,
                    evidence_plan_pending=evidence_plan_pending,
                )
            request_context: dict[str, object] = {
                "task_id": task.task_id,
                "step_index": step_index,
            }
            if finalization_retry:
                request_context["finalization_retry"] = True
            response = self.model.complete(
                messages,
                tools=active_tools,
                request_context=request_context,
            )

            if not response.tool_calls:
                self._record_protocol_error(
                    state=state,
                    messages=messages,
                    response=response,
                    step_index=step_index,
                    code="NO_TOOL_CALL",
                    message="The model response did not contain a native tool call.",
                )
                if evidence_plan_pending:
                    evidence_plan_pending, evidence_plan_commit_attempts = (
                        self._record_plan_phase_failure(
                            error_code="NO_TOOL_CALL",
                            step_index=step_index,
                            commit_attempts=evidence_plan_commit_attempts,
                        )
                    )
                if finalization_retry:
                    break
                step_index += 1
                continue
            if len(response.tool_calls) != 1:
                self._record_protocol_error(
                    state=state,
                    messages=messages,
                    response=response,
                    step_index=step_index,
                    code="MULTIPLE_TOOL_CALLS",
                    message=(
                        "Parallel tool calls are disabled; the model must call exactly one "
                        "tool per turn."
                    ),
                )
                if evidence_plan_pending:
                    evidence_plan_pending, evidence_plan_commit_attempts = (
                        self._record_plan_phase_failure(
                            error_code="MULTIPLE_TOOL_CALLS",
                            step_index=step_index,
                            commit_attempts=evidence_plan_commit_attempts,
                        )
                    )
                if finalization_retry:
                    break
                step_index += 1
                continue

            call = response.tool_calls[0]
            if not call.id or not call.name:
                self._record_protocol_error(
                    state=state,
                    messages=messages,
                    response=response,
                    step_index=step_index,
                    code="INVALID_TOOL_CALL",
                    message="A native tool call must include a non-empty id and function name.",
                )
                if evidence_plan_pending:
                    evidence_plan_pending, evidence_plan_commit_attempts = (
                        self._record_plan_phase_failure(
                            error_code="INVALID_TOOL_CALL",
                            step_index=step_index,
                            commit_attempts=evidence_plan_commit_attempts,
                        )
                    )
                if finalization_retry:
                    break
                step_index += 1
                continue

            messages.append(_assistant_message(response))
            if (
                not finalization_retry
                and step_index == self.config.max_steps
                and call.name != "answer"
                and "answer" in active_tools.specs
            ):
                observation = _final_step_guard_observation(call.name)
                messages.append(_tool_message(call, observation))
                emit_event(
                    self.event_sink,
                    "final_step_guard_triggered",
                    {
                        "step_index": step_index,
                        "tool_call_id": call.id,
                        "blocked_tool": call.name,
                        "retry_step_index": step_index + 1,
                    },
                )
                step_record = StepRecord(
                    step_index=step_index,
                    thought=response.content,
                    action=call.name,
                    action_input={},
                    raw_response=response.raw_response,
                    observation=observation,
                    ok=False,
                    tool_call_id=call.id,
                    finish_reason=response.finish_reason,
                )
                state.steps.append(step_record)
                emit_event(
                    self.event_sink,
                    "step_completed",
                    {
                        "step_index": step_index,
                        "step": step_record.to_dict(),
                    },
                )
                finalization_retry_pending = True
                step_index += 1
                continue

            call_identity = _repeatable_call_identity(call, active_tools)
            normalized_call_input: dict[str, object] = {}
            if call_identity is None:
                last_call_fingerprint = None
                repeated_call_count = 0
                repeated_call_first_step = 0
                previous_identical_call_step = 0
            else:
                call_fingerprint, normalized_call_input = call_identity
                if call_fingerprint == last_call_fingerprint:
                    repeated_call_count += 1
                else:
                    last_call_fingerprint = call_fingerprint
                    repeated_call_count = 1
                    repeated_call_first_step = step_index

                if repeated_call_count >= 3:
                    observation = _repeated_call_observation(
                        tool_name=call.name,
                        repeat_count=repeated_call_count,
                        first_step=repeated_call_first_step,
                        previous_step=previous_identical_call_step,
                    )
                    messages.append(_tool_message(call, observation))
                    emit_event(
                        self.event_sink,
                        "repeated_identical_tool_call_blocked",
                        {
                            "step_index": step_index,
                            "tool_call_id": call.id,
                            "blocked_tool": call.name,
                            "repeat_count": repeated_call_count,
                            "first_step": repeated_call_first_step,
                            "previous_step": previous_identical_call_step,
                        },
                    )
                    step_record = StepRecord(
                        step_index=step_index,
                        thought=response.content,
                        action=call.name,
                        action_input=normalized_call_input,
                        raw_response=response.raw_response,
                        observation=observation,
                        ok=False,
                        tool_call_id=call.id,
                        finish_reason=response.finish_reason,
                    )
                    state.steps.append(step_record)
                    emit_event(
                        self.event_sink,
                        "step_completed",
                        {
                            "step_index": step_index,
                            "step": step_record.to_dict(),
                        },
                    )
                    previous_identical_call_step = step_index
                    if finalization_retry:
                        break
                    step_index += 1
                    continue
                previous_identical_call_step = step_index

            emit_event(
                self.event_sink,
                "tool_started",
                {
                    "step_index": step_index,
                    "tool_call_id": call.id,
                    "tool": call.name,
                    "arguments": call.arguments,
                },
            )
            tool_result = active_tools.execute(task, call)
            artifact_submission = bool(
                call.name == "answer"
                and tool_result.action_input
                and tool_result.action_input.get("from_csv") == "answer.csv"
            )
            artifact_metadata = (
                dict(tool_result.content)
                if artifact_submission and tool_result.content.get("source") == "artifact"
                else {}
            )
            detected_artifact = tool_result.content.get("answer_artifact")
            if call.name == "execute_python" and isinstance(detected_artifact, dict):
                emit_event(
                    self.event_sink,
                    "answer_artifact_detected",
                    {
                        "step_index": step_index,
                        "tool_call_id": call.id,
                        "byte_count": detected_artifact.get("byte_count"),
                    },
                )
            if exploration_pending and call.name == "explore":
                exploration_pending = False
                report = tool_result.content.get("report")
                if isinstance(report, dict):
                    raw_projection = report.get("answer_projection")
                    if isinstance(raw_projection, dict):
                        answer_projection = raw_projection
                    evidence_plan_pending = self._activate_evidence_plan(
                        report=report,
                        step_index=step_index,
                    )
            elif evidence_plan_pending and call.name == "commit_evidence_plan":
                evidence_plan_pending, evidence_plan_commit_attempts = (
                    self._handle_evidence_plan_commit(
                        call=call,
                        tool_result=tool_result,
                        step_index=step_index,
                        commit_attempts=evidence_plan_commit_attempts,
                    )
                )
            elif evidence_plan_pending:
                # Runtime enforcement: a wrong tool during the plan phase is a
                # compliance failure, not an invalid plan. Do not consume a bounded
                # commit attempt; inject a correction and re-ask. Only the step
                # budget fails the phase open, preserving one normal answer step.
                if self.config.max_steps - step_index >= 2:
                    tool_result = _plan_pending_result(call.name)
                else:
                    evidence_plan_pending = False
            if tool_result.is_terminal:
                gate = self._verification_gate_result(
                    call=call,
                    step_index=step_index,
                    observed_verifications=observed_verifications,
                    answer_blocks=evidence_plan_answer_blocks,
                    finalization_retry=finalization_retry,
                )
                if gate is not None:
                    evidence_plan_answer_blocks += 1
                    tool_result = gate
                else:
                    tool_result = self._verify_terminal_answer(
                        call=call,
                        tool_result=tool_result,
                        step_index=step_index,
                        answer_projection=answer_projection,
                    )
            if artifact_submission:
                if tool_result.is_terminal:
                    emit_event(
                        self.event_sink,
                        "answer_artifact_submitted",
                        {
                            "step_index": step_index,
                            "tool_call_id": call.id,
                            "byte_count": artifact_metadata.get("byte_count"),
                            "row_count": artifact_metadata.get("row_count"),
                            "column_count": artifact_metadata.get("column_count"),
                            "sha256": artifact_metadata.get("sha256"),
                        },
                    )
                else:
                    emit_event(
                        self.event_sink,
                        "answer_artifact_rejected",
                        {
                            "step_index": step_index,
                            "tool_call_id": call.id,
                            "byte_count": artifact_metadata.get("byte_count"),
                            "row_count": artifact_metadata.get("row_count"),
                            "column_count": artifact_metadata.get("column_count"),
                            "error_code": tool_result.error_code,
                        },
                    )
            if (
                not tool_result.is_terminal
                and tool_result.ok
                and self.evidence_plan is not None
                and self.evidence_plan.committed_items is not None
            ):
                path = self._call_path(tool_result.action_input)
                if (
                    path is not None
                    and self.evidence_plan.matches_verification(tool=call.name, path=path)
                    and (call.name, path) not in observed_verifications
                ):
                    observed_verifications.add((call.name, path))
                    emit_event(
                        self.event_sink,
                        "evidence_plan_verification_observed",
                        {
                            "step_index": step_index,
                            "tool_call_id": call.id,
                            "tool": call.name,
                            "path": path,
                        },
                    )
            tool_event_payload = {
                "step_index": step_index,
                "tool_call_id": call.id,
                "tool": call.name,
                "ok": tool_result.ok,
                "is_terminal": tool_result.is_terminal,
                "error_code": tool_result.error_code,
                "recoverable": tool_result.recoverable,
            }
            if tool_result.error_code is not None:
                emit_event(
                    self.event_sink,
                    "tool_failed",
                    {
                        **tool_event_payload,
                        "error": tool_result.content,
                    },
                )
            else:
                emit_event(
                    self.event_sink,
                    "tool_completed",
                    tool_event_payload,
                )

            observation: dict[str, object] = {
                "ok": tool_result.ok,
                "tool": call.name,
                "content": tool_result.content,
            }
            messages.append(_tool_message(call, observation))
            step_record = StepRecord(
                step_index=step_index,
                thought=response.content,
                action=call.name,
                action_input=tool_result.action_input or {},
                raw_response=response.raw_response,
                observation=observation,
                ok=tool_result.ok,
                tool_call_id=call.id,
                finish_reason=response.finish_reason,
            )
            state.steps.append(step_record)
            emit_event(
                self.event_sink,
                "step_completed",
                {
                    "step_index": step_index,
                    "step": step_record.to_dict(),
                },
            )
            if tool_result.is_terminal:
                state.answer = tool_result.answer
                self._emit_evidence_plan_answered(
                    step_index=step_index,
                    observed_verifications=observed_verifications,
                )
                break
            if finalization_retry:
                break
            step_index += 1

        if state.answer is None and state.failure_reason is None:
            state.failure_reason = "Agent did not submit an answer within max_steps."

        return AgentRunResult(
            task_id=task.task_id,
            answer=state.answer,
            steps=list(state.steps),
            failure_reason=state.failure_reason,
        )
