from __future__ import annotations

import json
from dataclasses import dataclass

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

    def _exploration_required(self) -> bool:
        return "explore" in self.tools.specs

    def _active_tools(
        self, *, exploration_pending: bool, exploration_required: bool
    ) -> ToolRegistry:
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

    def run(self, task: PublicTask) -> AgentRunResult:
        state = AgentRuntimeState()
        messages = self._initial_messages(task)
        exploration_required = self._exploration_required()
        exploration_pending = exploration_required
        answer_projection: dict[str, object] | None = None

        for step_index in range(1, self.config.max_steps + 1):
            emit_event(
                self.event_sink,
                "step_started",
                {"step_index": step_index},
            )
            active_tools = self._active_tools(
                exploration_pending=exploration_pending,
                exploration_required=exploration_required,
            )
            response = self.model.complete(
                messages,
                tools=active_tools,
                request_context={
                    "task_id": task.task_id,
                    "step_index": step_index,
                },
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
                continue

            messages.append(_assistant_message(response))
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
            if exploration_pending and call.name == "explore":
                exploration_pending = False
                report = tool_result.content.get("report")
                if isinstance(report, dict):
                    raw_projection = report.get("answer_projection")
                    if isinstance(raw_projection, dict):
                        answer_projection = raw_projection
            if tool_result.is_terminal:
                tool_result = self._verify_terminal_answer(
                    call=call,
                    tool_result=tool_result,
                    step_index=step_index,
                    answer_projection=answer_projection,
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
                break

        if state.answer is None and state.failure_reason is None:
            state.failure_reason = "Agent did not submit an answer within max_steps."

        return AgentRunResult(
            task_id=task.task_id,
            answer=state.answer,
            steps=list(state.steps),
            failure_reason=state.failure_reason,
        )
