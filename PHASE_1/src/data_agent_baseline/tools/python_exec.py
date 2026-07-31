from __future__ import annotations

import contextlib
import io
import multiprocessing
import os
import sys
import tempfile
import traceback
from multiprocessing.connection import Connection
from pathlib import Path
from typing import Any

PYTHON_PROCESS_START_METHOD = "spawn"
PROCESS_STOP_GRACE_SECONDS = 1.0


@contextlib.contextmanager
def _capture_process_streams(stdout_path: Path, stderr_path: Path):
    original_stdout = sys.stdout
    original_stderr = sys.stderr
    saved_stdout_fd = os.dup(1)
    saved_stderr_fd = os.dup(2)

    with stdout_path.open("w+b") as stdout_file, stderr_path.open("w+b") as stderr_file:
        try:
            if original_stdout is not None:
                original_stdout.flush()
            if original_stderr is not None:
                original_stderr.flush()

            os.dup2(stdout_file.fileno(), 1)
            os.dup2(stderr_file.fileno(), 2)

            stdout_encoding = getattr(original_stdout, "encoding", None) or "utf-8"
            stderr_encoding = getattr(original_stderr, "encoding", None) or "utf-8"

            sys.stdout = io.TextIOWrapper(
                os.fdopen(os.dup(1), "wb"),
                encoding=stdout_encoding,
                errors="replace",
                line_buffering=True,
                write_through=True,
            )
            sys.stderr = io.TextIOWrapper(
                os.fdopen(os.dup(2), "wb"),
                encoding=stderr_encoding,
                errors="replace",
                line_buffering=True,
                write_through=True,
            )
            yield
        finally:
            if sys.stdout is not None:
                sys.stdout.flush()
            if sys.stderr is not None:
                sys.stderr.flush()

            if sys.stdout is not original_stdout:
                sys.stdout.close()
            if sys.stderr is not original_stderr:
                sys.stderr.close()

            sys.stdout = original_stdout
            sys.stderr = original_stderr
            os.dup2(saved_stdout_fd, 1)
            os.dup2(saved_stderr_fd, 2)
            os.close(saved_stdout_fd)
            os.close(saved_stderr_fd)


def _read_captured_stream(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def _run_python_code(
    context_root: str,
    code: str,
    stdout_path: str,
    stderr_path: str,
    result_connection: Connection,
) -> None:
    namespace: dict[str, Any] = {
        "__builtins__": __builtins__,
        "__name__": "__main__",
        "context_root": context_root,
        "Path": Path,
    }
    resolved_stdout_path = Path(stdout_path)
    resolved_stderr_path = Path(stderr_path)

    try:
        os.chdir(context_root)
        with _capture_process_streams(resolved_stdout_path, resolved_stderr_path):
            exec(code, namespace, namespace)
        result_connection.send({"success": True})
    except BaseException as exc:  # noqa: BLE001
        try:
            result_connection.send(
                {
                    "success": False,
                    "error": str(exc),
                    "traceback": traceback.format_exc(),
                }
            )
        except (BrokenPipeError, EOFError, OSError):
            pass
    finally:
        result_connection.close()


def _stop_process(process: multiprocessing.Process) -> None:
    if not process.is_alive():
        process.join(timeout=PROCESS_STOP_GRACE_SECONDS)
        return

    process.terminate()
    process.join(timeout=PROCESS_STOP_GRACE_SECONDS)
    if process.is_alive():
        process.kill()
        process.join(timeout=PROCESS_STOP_GRACE_SECONDS)


def _captured_result(
    stdout_path: Path,
    stderr_path: Path,
    *,
    success: bool,
    error: str | None = None,
    traceback_text: str | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "success": success,
        "output": _read_captured_stream(stdout_path),
        "stderr": _read_captured_stream(stderr_path),
    }
    if error is not None:
        result["error"] = error
    if traceback_text is not None:
        result["traceback"] = traceback_text
    return result


def execute_python_code(
    context_root: Path,
    code: str,
    *,
    timeout_seconds: float = 30,
) -> dict[str, Any]:
    resolved_context_root = context_root.resolve()
    with tempfile.TemporaryDirectory() as temp_dir:
        stdout_path = Path(temp_dir) / "stdout.txt"
        stderr_path = Path(temp_dir) / "stderr.txt"
        stdout_path.write_text("")
        stderr_path.write_text("")

        process_context = multiprocessing.get_context(PYTHON_PROCESS_START_METHOD)
        parent_connection, child_connection = process_context.Pipe(duplex=False)
        process = process_context.Process(
            target=_run_python_code,
            args=(
                resolved_context_root.as_posix(),
                code,
                stdout_path.as_posix(),
                stderr_path.as_posix(),
                child_connection,
            ),
        )
        try:
            process.start()
            child_connection.close()

            if not parent_connection.poll(timeout_seconds):
                _stop_process(process)
                return _captured_result(
                    stdout_path,
                    stderr_path,
                    success=False,
                    error=f"Python execution timed out after {timeout_seconds:g} seconds.",
                )

            try:
                child_result = parent_connection.recv()
            except (EOFError, OSError):
                child_result = None

            process.join(timeout=PROCESS_STOP_GRACE_SECONDS)
            if process.is_alive():
                _stop_process(process)

            if not isinstance(child_result, dict):
                return _captured_result(
                    stdout_path,
                    stderr_path,
                    success=False,
                    error="Python execution exited without returning a result.",
                )

            return _captured_result(
                stdout_path,
                stderr_path,
                success=bool(child_result.get("success")),
                error=child_result.get("error"),
                traceback_text=child_result.get("traceback"),
            )
        finally:
            parent_connection.close()
            child_connection.close()
