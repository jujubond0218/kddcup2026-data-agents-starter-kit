from __future__ import annotations

import json
import multiprocessing
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from data_agent_baseline.tools import python_exec
from data_agent_baseline.tools.python_exec import execute_python_code


def _nested_python_execution(context_root: str, result_path: str, marker: int) -> None:
    result = execute_python_code(
        Path(context_root),
        f"print('nested-{marker}')",
        timeout_seconds=5,
    )
    Path(result_path).write_text(json.dumps(result), encoding="utf-8")


def test_python_execution_uses_spawn_context(monkeypatch, tmp_path):
    requested_methods = []
    original_get_context = multiprocessing.get_context

    def recording_get_context(method=None):
        requested_methods.append(method)
        return original_get_context(method)

    monkeypatch.setattr(python_exec.multiprocessing, "get_context", recording_get_context)

    result = execute_python_code(tmp_path, "print('ready')", timeout_seconds=5)

    assert result["success"] is True
    assert result["output"].strip() == "ready"
    assert requested_methods == ["spawn"]


def test_python_execution_captures_stdout_stderr_and_exception(tmp_path):
    success = execute_python_code(
        tmp_path,
        "import sys\nprint('stdout-value')\nprint('stderr-value', file=sys.stderr)",
        timeout_seconds=5,
    )
    failure = execute_python_code(
        tmp_path,
        "raise ValueError('synthetic failure')",
        timeout_seconds=5,
    )

    assert success == {
        "success": True,
        "output": "stdout-value\n",
        "stderr": "stderr-value\n",
    }
    assert failure["success"] is False
    assert failure["error"] == "synthetic failure"
    assert "ValueError: synthetic failure" in failure["traceback"]


def test_python_execution_returns_when_child_exits_without_result(tmp_path):
    started_at = time.perf_counter()

    result = execute_python_code(
        tmp_path,
        "import os\nos._exit(3)",
        timeout_seconds=5,
    )

    assert time.perf_counter() - started_at < 3
    assert result["success"] is False
    assert result["error"] == "Python execution exited without returning a result."


def test_python_execution_timeout_has_bounded_cleanup(tmp_path):
    started_at = time.perf_counter()

    result = execute_python_code(
        tmp_path,
        "while True:\n    pass",
        timeout_seconds=0.2,
    )

    assert time.perf_counter() - started_at < 3
    assert result["success"] is False
    assert result["error"] == "Python execution timed out after 0.2 seconds."


@pytest.mark.skipif(
    "fork" not in multiprocessing.get_all_start_methods(),
    reason="The nested Runner regression requires the Linux fork start method.",
)
@pytest.mark.filterwarnings(
    "ignore:This process .* is multi-threaded, use of fork\\(\\) may lead to deadlocks:DeprecationWarning"
)
def test_parallel_nested_python_execution_always_returns(tmp_path):
    fork_context = multiprocessing.get_context("fork")

    def run_nested(marker: int):
        result_path = tmp_path / f"result-{marker}.json"
        process = fork_context.Process(
            target=_nested_python_execution,
            args=(str(tmp_path), str(result_path), marker),
        )
        started_at = time.perf_counter()
        process.start()
        process.join(timeout=10)
        elapsed_seconds = time.perf_counter() - started_at
        if process.is_alive():
            process.terminate()
            process.join(timeout=1)
            if process.is_alive():
                process.kill()
                process.join(timeout=1)
        return process.exitcode, elapsed_seconds, result_path

    with ThreadPoolExecutor(max_workers=4) as executor:
        attempts = list(executor.map(run_nested, range(20)))

    assert all(exit_code == 0 for exit_code, _, _ in attempts)
    assert all(elapsed_seconds < 10 for _, elapsed_seconds, _ in attempts)
    results = [json.loads(path.read_text(encoding="utf-8")) for _, _, path in attempts]
    assert all(result["success"] is True for result in results)
