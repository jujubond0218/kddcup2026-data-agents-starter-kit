from __future__ import annotations

from data_agent_baseline.benchmark.schema import PublicTask


REACT_SYSTEM_PROMPT = """
You are a ReAct-style data agent.

You are solving a task from a public dataset. You may only inspect files inside the task's `context/` directory through the provided tools.

Rules:
1. Inspect the task context through the provided tools before answering.
2. Base your answer only on information you can observe through the provided tools.
3. The task is complete only when you call the `answer` tool.
4. The `answer` tool must receive a table with `columns` and `rows`.
5. Call exactly one tool in each turn through the native tool-calling interface.
6. Do not write or simulate tool calls in plain text or JSON.

Keep reasoning concise and grounded in the observed data.
""".strip()


def build_system_prompt(
    system_prompt: str | None = None,
    *,
    explore_available: bool = False,
    explore_required: bool = False,
) -> str:
    prompt = system_prompt or REACT_SYSTEM_PROMPT
    if explore_available:
        prompt += (
            "\n7. Your first tool call must be `explore({})`. It launches a discovery-only "
            "sub-agent and is available for exactly one call.\n"
            "8. Use its task_requirements, recommended_sources, source-anchored knowledge "
            "rules, schemas, and value samples as a compact reference guide. Prioritize "
            "confirmed sources, independently check candidate sources and uncertainties with "
            "normal tools, treat joins and ETL entries as advisory, and prefer actual queried "
            "data on conflict."
        )
    return prompt


def build_task_prompt(task: PublicTask) -> str:
    return (
        f"Question: {task.question}\n"
        "All tool file paths are relative to the task context directory. "
        "When you have the final table, call the `answer` tool."
    )
