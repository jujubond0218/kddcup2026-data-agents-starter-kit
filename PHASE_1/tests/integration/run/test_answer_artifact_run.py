from __future__ import annotations

import csv
import json
from pathlib import Path

from data_agent_baseline.agents.model import (
    ModelResponse,
    ModelToolCall,
    ScriptedModelAdapter,
)
from data_agent_baseline.config import (
    AgentConfig,
    AppConfig,
    DatasetConfig,
    ExplorerConfig,
    RunConfig,
)
from data_agent_baseline.run.runner import run_benchmark


def _response(name: str, arguments: dict[str, object], call_id: str) -> ModelResponse:
    call = ModelToolCall(
        id=call_id,
        name=name,
        arguments=json.dumps(arguments, separators=(",", ":")),
    )
    return ModelResponse(
        content="",
        tool_calls=(call,),
        raw_response=json.dumps({"tool_calls": [call.to_openai_dict()]}),
        finish_reason="tool_calls",
    )


def _write_task(dataset_root: Path) -> None:
    task_dir = dataset_root / "task_1"
    (task_dir / "context").mkdir(parents=True)
    (task_dir / "task.json").write_text(
        json.dumps(
            {
                "task_id": "task_1",
                "difficulty": "easy",
                "question": "Return all generated codes.",
            }
        ),
        encoding="utf-8",
    )


def test_runner_preserves_large_artifact_answer_and_cleans_attempt_directory(tmp_path):
    dataset_root = tmp_path / "data"
    _write_task(dataset_root)
    model = ScriptedModelAdapter(
        [
            _response(
                "execute_python",
                {
                    "code": (
                        "answer_csv_path.write_text('code\\n' + "
                        "'\\n'.join(f'{i:05d}' for i in range(10000)) + '\\n', "
                        "encoding='utf-8')"
                    )
                },
                "call_write",
            ),
            _response("answer", {"from_csv": "answer.csv"}, "call_submit"),
        ]
    )
    config = AppConfig(
        dataset=DatasetConfig(root_path=dataset_root),
        agent=AgentConfig(api_key="test-key", max_steps=2),
        run=RunConfig(output_dir=tmp_path / "runs", run_id="artifact", max_workers=1),
        explorer=ExplorerConfig(enabled=False),
    )

    run_output_dir, artifacts = run_benchmark(
        config=config,
        model=model,
        task_ids=["task_1"],
    )

    assert artifacts[0].succeeded is True
    task_output_dir = run_output_dir / "task_1"
    with (task_output_dir / "prediction.csv").open(newline="", encoding="utf-8") as stream:
        prediction = list(csv.reader(stream))
    assert prediction == [["code"], *[[f"{index:05d}"] for index in range(10_000)]]

    trace = json.loads((task_output_dir / "trace.json").read_text(encoding="utf-8"))
    assert trace["answer"] == {
        "columns": ["code"],
        "rows": [[f"{index:05d}"] for index in range(10_000)],
    }
    assert len(json.dumps(trace["steps"][1]["action_input"]).encode("utf-8")) < 100
    assert not list(task_output_dir.glob(".answer-artifact-*"))

    events = [
        json.loads(line)
        for line in (task_output_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    submitted = next(
        event for event in events if event["event_type"] == "answer_artifact_submitted"
    )
    assert submitted["row_count"] == 10_000
    assert submitted["column_count"] == 1
    assert len(submitted["sha256"]) == 64


def test_retry_failed_attempt_does_not_reuse_stale_artifact(tmp_path):
    dataset_root = tmp_path / "data"
    _write_task(dataset_root)
    config = AppConfig(
        dataset=DatasetConfig(root_path=dataset_root),
        agent=AgentConfig(api_key="test-key", max_steps=1),
        run=RunConfig(output_dir=tmp_path / "runs", run_id="retry", max_workers=1),
        explorer=ExplorerConfig(enabled=False),
    )
    first_model = ScriptedModelAdapter(
        [
            _response(
                "execute_python",
                {"code": ("answer_csv_path.write_text('value\\nstale\\n', encoding='utf-8')")},
                "call_write_stale",
            )
        ]
    )

    run_output_dir, first = run_benchmark(
        config=config,
        model=first_model,
        task_ids=["task_1"],
    )
    second_model = ScriptedModelAdapter(
        [_response("answer", {"columns": ["value"], "rows": [["fresh"]]}, "call_inline")]
    )
    _, second = run_benchmark(
        config=config,
        model=second_model,
        task_ids=["task_1"],
        resume=True,
        retry_failed=True,
    )

    assert first[0].succeeded is False
    assert second[0].succeeded is True
    assert (run_output_dir / "task_1" / "prediction.csv").read_text(
        encoding="utf-8"
    ).splitlines() == ["value", "fresh"]
    assert not list((run_output_dir / "task_1").glob(".answer-artifact-*"))
