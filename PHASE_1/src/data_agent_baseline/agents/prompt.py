from __future__ import annotations

import json
from typing import Any

from data_agent_baseline.benchmark.schema import PublicTask


REACT_SYSTEM_PROMPT = """
You are a ReAct-style data agent.

You are solving a task from a public dataset. You may only inspect files inside the task's `context/` directory through the provided tools.

Rules:
1. Use the provided Context Inventory when present, and use tools to inspect any remaining
   context needed before answering.
2. Base your answer only on information you can observe through the provided tools.
3. The task is complete only when you call the `answer` tool.
4. The `answer` tool must receive a table with `columns` and `rows`.
5. Call exactly one tool in each turn through the native tool-calling interface.
6. Do not write or simulate tool calls in plain text or JSON.

Keep reasoning concise and grounded in the observed data.
""".strip()


def build_system_prompt(
    system_prompt: str | None = None, *, explore_available: bool = False
) -> str:
    prompt = system_prompt or REACT_SYSTEM_PROMPT
    if explore_available:
        prompt += (
            "\n7. A deterministic Context Inventory is included in the task message. "
            "Use its paths, schemas, and samples directly; do not repeat discovery that it "
            "already provides.\n"
            "8. Call `explore` only when that Inventory leaves a specific ambiguity about "
            "source selection, field semantics, joins, or document mapping. Pass a concise "
            "focus and candidate_paths copied exactly from the Inventory.\n"
            "9. Treat Inventory and returned evidence observations as facts. Treat key-field "
            "and join candidates as hypotheses that must be verified with the normal tools "
            "before computation. Inventory samples prove only the displayed values; when a "
            "file is truncated or row_count is null, use a normal tool for complete filtering "
            "or aggregation. Actual queried data wins on conflict."
        )
    return prompt


def build_task_prompt(
    task: PublicTask,
    *,
    context_inventory: dict[str, Any] | None = None,
) -> str:
    prompt = (
        f"Question: {task.question}\n"
        "All tool file paths are relative to the task context directory. "
        "When you have the final table, call the `answer` tool."
    )
    if context_inventory is not None:
        rendered = json.dumps(
            context_inventory,
            ensure_ascii=False,
            separators=(",", ":"),
            default=str,
        )
        prompt += (
            "\n\nContext Inventory (deterministic, bounded, and already scanned; "
            "it does not consume an Agent step):\n"
            f"{rendered}"
        )
    return prompt
