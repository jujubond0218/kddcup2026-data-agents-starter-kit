from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from data_agent_baseline.config import AgentConfig, AppConfig, DatasetConfig, RunConfig
from data_agent_baseline.run import runner


def _config(tmp_path: Path) -> AppConfig:
    return AppConfig(
        dataset=DatasetConfig(root_path=tmp_path / "data"),
        agent=AgentConfig(api_key="test-key"),
        run=RunConfig(output_dir=tmp_path / "runs", task_timeout_seconds=1),
    )


def test_parent_cleans_unique_artifact_roots_for_parallel_attempts(tmp_path, monkeypatch):
    observed_roots: list[Path] = []

    def fake_run_with_timeout(*, task_id, config, task_output_dir, artifact_root):
        del task_output_dir
        observed_roots.append(artifact_root)
        (artifact_root / "answer.csv").write_text(f"value\n{task_id}\n", encoding="utf-8")
        (config.dataset.root_path / task_id / "context" / "answer.csv").write_text(
            "stray\n",
            encoding="utf-8",
        )
        return {
            "task_id": task_id,
            "answer": None,
            "steps": [],
            "failure_reason": "synthetic",
            "succeeded": False,
        }

    monkeypatch.setattr(runner, "_run_single_task_with_timeout", fake_run_with_timeout)
    run_output_dir = tmp_path / "run"
    task_ids = [f"task_{index}" for index in range(4)]
    for task_id in task_ids:
        (tmp_path / "data" / task_id / "context").mkdir(parents=True)

    with ThreadPoolExecutor(max_workers=4) as executor:
        results = list(
            executor.map(
                lambda task_id: runner.run_single_task(
                    task_id=task_id,
                    config=_config(tmp_path),
                    run_output_dir=run_output_dir,
                ),
                task_ids,
            )
        )

    assert len(results) == 4
    assert len({path.as_posix() for path in observed_roots}) == 4
    assert all(not path.exists() for path in observed_roots)
    assert all(
        not list((run_output_dir / task_id).glob(".answer-artifact-*")) for task_id in task_ids
    )
    assert all(
        not (tmp_path / "data" / task_id / "context" / "answer.csv").exists()
        for task_id in task_ids
    )


def test_parent_preserves_preexisting_context_answer_file(tmp_path, monkeypatch):
    context_answer = tmp_path / "data" / "task_1" / "context" / "answer.csv"
    context_answer.parent.mkdir(parents=True)
    context_answer.write_text("source\nkeep\n", encoding="utf-8")

    def fake_run_with_timeout(*, task_id, config, task_output_dir, artifact_root):
        del config
        del task_output_dir
        del artifact_root
        context_answer.write_text("source\nkeep\n", encoding="utf-8")
        return {
            "task_id": task_id,
            "answer": None,
            "steps": [],
            "failure_reason": "synthetic",
            "succeeded": False,
        }

    monkeypatch.setattr(runner, "_run_single_task_with_timeout", fake_run_with_timeout)

    runner.run_single_task(
        task_id="task_1",
        config=_config(tmp_path),
        run_output_dir=tmp_path / "run",
    )

    assert context_answer.read_text(encoding="utf-8") == "source\nkeep\n"
