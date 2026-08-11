from __future__ import annotations

import ast
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
PYTHON_CAPTURE_STREAM_MAX_BYTES = 64 * 1024
_TRUNCATION_MARKER_TEMPLATE = "\n... [TRUNCATED: {omitted_bytes} bytes omitted] ...\n"
ANSWER_CSV_PATH_NAME = "answer_csv_path"


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


def _decode_head(payload: bytes) -> tuple[str, int]:
    max_boundary_bytes = min(3, len(payload))
    for trimmed_bytes in range(max_boundary_bytes + 1):
        candidate = payload[: len(payload) - trimmed_bytes] if trimmed_bytes else payload
        try:
            return candidate.decode("utf-8"), len(candidate)
        except UnicodeDecodeError as exc:
            if exc.end != len(candidate):
                return candidate.decode("utf-8", errors="replace"), len(candidate)
    return "", 0


def _decode_tail(payload: bytes) -> tuple[str, int]:
    max_boundary_bytes = min(3, len(payload))
    for skipped_bytes in range(max_boundary_bytes + 1):
        candidate = payload[skipped_bytes:]
        try:
            return candidate.decode("utf-8"), len(candidate)
        except UnicodeDecodeError as exc:
            if exc.start != 0:
                return candidate.decode("utf-8", errors="replace"), len(candidate)
    return "", 0


def _read_stream_edges(path: Path, head_bytes: int, tail_bytes: int) -> tuple[str, int, str, int]:
    with path.open("rb") as stream:
        head_payload = stream.read(head_bytes)
        if tail_bytes:
            stream.seek(-tail_bytes, os.SEEK_END)
            tail_payload = stream.read(tail_bytes)
        else:
            tail_payload = b""
    head_text, kept_head_bytes = _decode_head(head_payload)
    tail_text, kept_tail_bytes = _decode_tail(tail_payload)
    return head_text, kept_head_bytes, tail_text, kept_tail_bytes


def _bounded_capture(path: Path) -> dict[str, Any]:
    original_bytes = path.stat().st_size
    if original_bytes <= PYTHON_CAPTURE_STREAM_MAX_BYTES:
        text = _read_captured_stream(path)
        returned_bytes = len(text.encode("utf-8"))
        if returned_bytes <= PYTHON_CAPTURE_STREAM_MAX_BYTES:
            return {
                "text": text,
                "truncated": False,
                "original_bytes": original_bytes,
                "returned_bytes": returned_bytes,
                "omitted_bytes": 0,
            }

    marker = _TRUNCATION_MARKER_TEMPLATE.format(omitted_bytes=original_bytes)
    content_budget = PYTHON_CAPTURE_STREAM_MAX_BYTES - len(marker.encode("utf-8"))
    source_budget = min(original_bytes, content_budget)
    head_bytes = (source_budget + 1) // 2
    tail_bytes = source_budget // 2

    while True:
        head_text, kept_head_bytes, tail_text, kept_tail_bytes = _read_stream_edges(
            path,
            head_bytes,
            tail_bytes,
        )
        omitted_bytes = original_bytes - kept_head_bytes - kept_tail_bytes
        marker = _TRUNCATION_MARKER_TEMPLATE.format(omitted_bytes=omitted_bytes)
        text = f"{head_text}{marker}{tail_text}"
        returned_bytes = len(text.encode("utf-8"))
        if returned_bytes <= PYTHON_CAPTURE_STREAM_MAX_BYTES:
            return {
                "text": text,
                "truncated": True,
                "original_bytes": original_bytes,
                "returned_bytes": returned_bytes,
                "omitted_bytes": omitted_bytes,
            }

        overflow = returned_bytes - PYTHON_CAPTURE_STREAM_MAX_BYTES
        head_reduction = min(head_bytes, (overflow + 1) // 2)
        tail_reduction = min(tail_bytes, overflow // 2)
        if head_reduction + tail_reduction == 0:
            raise AssertionError("Unable to fit captured Python stream within its byte budget.")
        head_bytes -= head_reduction
        tail_bytes -= tail_reduction


def _run_python_code(
    context_root: str,
    code: str,
    stdout_path: str,
    stderr_path: str,
    answer_csv_path: str | None,
    result_connection: Connection,
) -> None:
    namespace: dict[str, Any] = {
        "__builtins__": __builtins__,
        "__name__": "__main__",
        "context_root": context_root,
        "Path": Path,
    }
    if answer_csv_path is not None:
        namespace["answer_csv_path"] = Path(answer_csv_path)
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
    output_capture = _bounded_capture(stdout_path)
    stderr_capture = _bounded_capture(stderr_path)
    result: dict[str, Any] = {
        "success": success,
        "output": output_capture["text"],
        "stderr": stderr_capture["text"],
    }
    if error is not None:
        result["error"] = error
    if traceback_text is not None:
        result["traceback"] = traceback_text
    if output_capture["truncated"] or stderr_capture["truncated"]:
        result["truncated"] = True
        result["capture"] = {
            "output": {key: value for key, value in output_capture.items() if key != "text"},
            "stderr": {key: value for key, value in stderr_capture.items() if key != "text"},
        }
    return result


def _assigns_reserved_answer_csv_path(code: str) -> bool:
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return False
    return any(
        isinstance(node, ast.Name)
        and node.id == ANSWER_CSV_PATH_NAME
        and isinstance(node.ctx, (ast.Store, ast.Del))
        for node in ast.walk(tree)
    )


def execute_python_code(
    context_root: Path,
    code: str,
    *,
    timeout_seconds: float = 30,
    answer_csv_path: Path | None = None,
) -> dict[str, Any]:
    resolved_context_root = context_root.resolve()
    with tempfile.TemporaryDirectory() as temp_dir:
        stdout_path = Path(temp_dir) / "stdout.txt"
        stderr_path = Path(temp_dir) / "stderr.txt"
        stdout_path.write_text("")
        stderr_path.write_text("")

        if answer_csv_path is not None and _assigns_reserved_answer_csv_path(code):
            return _captured_result(
                stdout_path,
                stderr_path,
                success=False,
                error=(
                    "answer_csv_path is a predefined Path and cannot be assigned or deleted. "
                    "Write the complete CSV directly to it."
                ),
            )

        process_context = multiprocessing.get_context(PYTHON_PROCESS_START_METHOD)
        parent_connection, child_connection = process_context.Pipe(duplex=False)
        process = process_context.Process(
            target=_run_python_code,
            args=(
                resolved_context_root.as_posix(),
                code,
                stdout_path.as_posix(),
                stderr_path.as_posix(),
                answer_csv_path.resolve().as_posix() if answer_csv_path else None,
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
