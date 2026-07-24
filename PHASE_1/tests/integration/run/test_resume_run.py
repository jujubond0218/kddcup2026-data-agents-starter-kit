import json
from pathlib import Path

from data_agent_baseline.agents.model import (
    ModelResponse,
    ModelToolCall,
    ScriptedModelAdapter,
)
from data_agent_baseline.config import AgentConfig, AppConfig, DatasetConfig, RunConfig
from data_agent_baseline.run.runner import run_benchmark
from data_agent_baseline.tools.registry import create_default_tool_registry


def _write_task(dataset_root: Path, task_id: str) -> None:
    task_dir = dataset_root / task_id
    (task_dir / "context").mkdir(parents=True)
    (task_dir / "task.json").write_text(
        json.dumps(
            {
                "task_id": task_id,
                "difficulty": "easy",
                "question": f"Question for {task_id}",
            }
        ),
        encoding="utf-8",
    )


def _config(tmp_path: Path, *, run_id: str, max_steps: int = 1) -> AppConfig:
    dataset_root = tmp_path / "data"
    dataset_root.mkdir(exist_ok=True)
    return AppConfig(
        dataset=DatasetConfig(root_path=dataset_root),
        agent=AgentConfig(api_key="test-key", max_steps=max_steps),
        run=RunConfig(
            output_dir=tmp_path / "runs",
            run_id=run_id,
            max_workers=1,
            task_timeout_seconds=1.0,
        ),
    )


def _answer_response(value: str) -> ModelResponse:
    arguments = json.dumps(
        {
            "columns": ["value"],
            "rows": [[value]],
        },
        separators=(",", ":"),
    )
    call = ModelToolCall(
        id=f"call_answer_{value}",
        name="answer",
        arguments=arguments,
    )
    return ModelResponse(
        content="",
        tool_calls=(call,),
        raw_response=json.dumps({"tool_calls": [call.to_openai_dict()]}),
        finish_reason="tool_calls",
    )


def _invalid_answer_response() -> ModelResponse:
    arguments = json.dumps(
        {
            "columns": ["value", " value "],
            "rows": [["invalid", "invalid"]],
        },
        separators=(",", ":"),
    )
    call = ModelToolCall(
        id="call_invalid_answer",
        name="answer",
        arguments=arguments,
    )
    return ModelResponse(
        content="",
        tool_calls=(call,),
        raw_response=json.dumps({"tool_calls": [call.to_openai_dict()]}),
        finish_reason="tool_calls",
    )


def _no_tool_response() -> ModelResponse:
    return ModelResponse(
        content="I cannot call a tool.",
        tool_calls=(),
        raw_response='{"content":"I cannot call a tool.","tool_calls":[]}',
        finish_reason="stop",
    )


def test_resume_skips_completed_tasks(tmp_path):
    config = _config(tmp_path, run_id="resume-success")
    _write_task(config.dataset.root_path, "task_1")
    _write_task(config.dataset.root_path, "task_2")
    task_ids = ["task_1", "task_2"]

    run_output_dir, first_artifacts = run_benchmark(
        config=config,
        model=ScriptedModelAdapter([_answer_response("one"), _answer_response("two")]),
        tools=create_default_tool_registry(),
        task_ids=task_ids,
    )
    _, resumed_artifacts = run_benchmark(
        config=config,
        model=ScriptedModelAdapter([]),
        tools=create_default_tool_registry(),
        resume=True,
    )

    assert all(artifact.succeeded for artifact in first_artifacts)
    assert all(artifact.succeeded for artifact in resumed_artifacts)
    summary = json.loads((run_output_dir / "summary.json").read_text(encoding="utf-8"))
    assert summary["resumed"] is True
    assert summary["resumed_task_count"] == 2
    assert summary["executed_task_count"] == 0


def test_retry_failed_archives_previous_attempt(tmp_path):
    config = _config(tmp_path, run_id="retry-failure")
    _write_task(config.dataset.root_path, "task_1")

    run_output_dir, first_artifacts = run_benchmark(
        config=config,
        model=ScriptedModelAdapter([_no_tool_response()]),
        tools=create_default_tool_registry(),
        task_ids=["task_1"],
    )
    _, skipped_artifacts = run_benchmark(
        config=config,
        model=ScriptedModelAdapter([]),
        tools=create_default_tool_registry(),
        resume=True,
    )
    _, retried_artifacts = run_benchmark(
        config=config,
        model=ScriptedModelAdapter([_answer_response("fixed")]),
        tools=create_default_tool_registry(),
        resume=True,
        retry_failed=True,
    )

    assert first_artifacts[0].succeeded is False
    assert skipped_artifacts[0].succeeded is False
    assert retried_artifacts[0].succeeded is True
    archived_trace = run_output_dir / "task_1" / "attempts" / "attempt_001" / "trace.json"
    assert archived_trace.is_file()
    archived_payload = json.loads(archived_trace.read_text(encoding="utf-8"))
    assert archived_payload["succeeded"] is False
    summary = json.loads((run_output_dir / "summary.json").read_text(encoding="utf-8"))
    assert summary["resumed_task_count"] == 0
    assert summary["executed_task_count"] == 1


def test_resume_reruns_success_with_missing_prediction(tmp_path):
    config = _config(tmp_path, run_id="missing-prediction")
    _write_task(config.dataset.root_path, "task_1")

    run_output_dir, _ = run_benchmark(
        config=config,
        model=ScriptedModelAdapter([_answer_response("initial")]),
        tools=create_default_tool_registry(),
        task_ids=["task_1"],
    )
    (run_output_dir / "task_1" / "prediction.csv").unlink()

    _, resumed_artifacts = run_benchmark(
        config=config,
        model=ScriptedModelAdapter([_answer_response("replacement")]),
        tools=create_default_tool_registry(),
        resume=True,
    )

    assert resumed_artifacts[0].succeeded is True
    assert (run_output_dir / "task_1" / "attempts" / "attempt_001" / "trace.json").is_file()
    summary = json.loads((run_output_dir / "summary.json").read_text(encoding="utf-8"))
    assert summary["resumed_task_count"] == 0
    assert summary["executed_task_count"] == 1


def test_runner_writes_only_the_corrected_verified_answer(tmp_path):
    config = _config(tmp_path, run_id="verified-answer", max_steps=2)
    _write_task(config.dataset.root_path, "task_1")

    run_output_dir, artifacts = run_benchmark(
        config=config,
        model=ScriptedModelAdapter([_invalid_answer_response(), _answer_response("fixed")]),
        tools=create_default_tool_registry(),
        task_ids=["task_1"],
    )

    artifact = artifacts[0]
    assert artifact.succeeded is True
    assert artifact.prediction_csv_path is not None
    assert artifact.prediction_csv_path.read_text(encoding="utf-8") == "value\nfixed\n"
    trace = json.loads((run_output_dir / "task_1" / "trace.json").read_text(encoding="utf-8"))
    assert trace["steps"][0]["observation"]["content"]["error"]["code"] == (
        "ANSWER_VERIFICATION_ERROR"
    )
    events = [
        json.loads(line)
        for line in (run_output_dir / "task_1" / "events.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert any(event["event_type"] == "answer_verification_rejected" for event in events)
    assert any(event["event_type"] == "answer_verification_passed" for event in events)


def test_runner_does_not_write_a_rejected_answer(tmp_path):
    config = _config(tmp_path, run_id="rejected-answer", max_steps=1)
    _write_task(config.dataset.root_path, "task_1")

    run_output_dir, artifacts = run_benchmark(
        config=config,
        model=ScriptedModelAdapter([_invalid_answer_response()]),
        tools=create_default_tool_registry(),
        task_ids=["task_1"],
    )

    artifact = artifacts[0]
    assert artifact.succeeded is False
    assert artifact.prediction_csv_path is None
    assert not (run_output_dir / "task_1" / "prediction.csv").exists()
