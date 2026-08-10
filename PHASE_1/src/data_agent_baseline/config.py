from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _default_dataset_root() -> Path:
    return PROJECT_ROOT / "data" / "public" / "input"


def _default_run_output_dir() -> Path:
    return PROJECT_ROOT / "artifacts" / "runs"


@dataclass(frozen=True, slots=True)
class DatasetConfig:
    root_path: Path = field(default_factory=_default_dataset_root)


@dataclass(frozen=True, slots=True)
class AgentConfig:
    model: str = "gpt-4.1-mini"
    api_base: str = "https://api.openai.com/v1"
    api_key: str = ""
    max_steps: int = 20
    temperature: float = 0.0
    model_request_timeout_seconds: float = 20.0
    model_max_retries: int = 1
    model_retry_backoff_seconds: float = 1.0


@dataclass(frozen=True, slots=True)
class RunConfig:
    output_dir: Path = field(default_factory=_default_run_output_dir)
    run_id: str | None = None
    max_workers: int = 2
    task_timeout_seconds: float = 120.0


@dataclass(frozen=True, slots=True)
class ExplorerConfig:
    enabled: bool = True
    max_steps: int = 2
    max_duration_seconds: float = 60.0
    max_files: int = 64
    max_preview_calls: int = 2
    max_preview_chars: int = 2_000
    max_inventory_chars: int = 12_000
    max_report_chars: int = 4_000
    max_total_read_bytes: int = 4 * 1024 * 1024
    max_single_file_bytes: int = 256 * 1024
    max_pdf_bytes: int = 2 * 1024 * 1024
    max_pdf_pages: int = 3


@dataclass(frozen=True, slots=True)
class EvidencePlanConfig:
    enabled: bool = False
    max_commit_attempts: int = 2
    strict_keys: bool = True
    verification_gate: bool = True


@dataclass(frozen=True, slots=True)
class AppConfig:
    dataset: DatasetConfig = field(default_factory=DatasetConfig)
    agent: AgentConfig = field(default_factory=AgentConfig)
    run: RunConfig = field(default_factory=RunConfig)
    explorer: ExplorerConfig = field(default_factory=ExplorerConfig)
    evidence_plan: EvidencePlanConfig = field(default_factory=EvidencePlanConfig)


def _path_value(raw_value: str | None, default_value: Path) -> Path:
    if not raw_value:
        return default_value
    candidate = Path(raw_value)
    if candidate.is_absolute():
        return candidate
    return (PROJECT_ROOT / candidate).resolve()


def load_app_config(config_path: Path) -> AppConfig:
    payload = yaml.safe_load(config_path.read_text()) or {}
    dataset_defaults = DatasetConfig()
    agent_defaults = AgentConfig()
    run_defaults = RunConfig()
    explorer_defaults = ExplorerConfig()
    evidence_plan_defaults = EvidencePlanConfig()

    dataset_payload = payload.get("dataset", {})
    agent_payload = payload.get("agent", {})
    run_payload = payload.get("run", {})
    explorer_payload = payload.get("explorer", {})
    evidence_plan_payload = payload.get("evidence_plan", {})

    dataset_config = DatasetConfig(
        root_path=_path_value(dataset_payload.get("root_path"), dataset_defaults.root_path),
    )
    agent_config = AgentConfig(
        model=str(agent_payload.get("model", agent_defaults.model)),
        api_base=str(agent_payload.get("api_base", agent_defaults.api_base)),
        api_key=str(agent_payload.get("api_key", agent_defaults.api_key)),
        max_steps=int(agent_payload.get("max_steps", agent_defaults.max_steps)),
        temperature=float(agent_payload.get("temperature", agent_defaults.temperature)),
        model_request_timeout_seconds=float(
            agent_payload.get(
                "model_request_timeout_seconds",
                agent_defaults.model_request_timeout_seconds,
            )
        ),
        model_max_retries=int(
            agent_payload.get("model_max_retries", agent_defaults.model_max_retries)
        ),
        model_retry_backoff_seconds=float(
            agent_payload.get(
                "model_retry_backoff_seconds",
                agent_defaults.model_retry_backoff_seconds,
            )
        ),
    )
    raw_run_id = run_payload.get("run_id")
    run_id = run_defaults.run_id
    if raw_run_id is not None:
        normalized_run_id = str(raw_run_id).strip()
        run_id = normalized_run_id or None

    run_config = RunConfig(
        output_dir=_path_value(run_payload.get("output_dir"), run_defaults.output_dir),
        run_id=run_id,
        max_workers=int(run_payload.get("max_workers", run_defaults.max_workers)),
        task_timeout_seconds=float(
            run_payload.get("task_timeout_seconds", run_defaults.task_timeout_seconds)
        ),
    )
    explorer_config = ExplorerConfig(
        enabled=bool(explorer_payload.get("enabled", explorer_defaults.enabled)),
        max_steps=int(explorer_payload.get("max_steps", explorer_defaults.max_steps)),
        max_duration_seconds=float(
            explorer_payload.get(
                "max_duration_seconds",
                explorer_defaults.max_duration_seconds,
            )
        ),
        max_files=int(explorer_payload.get("max_files", explorer_defaults.max_files)),
        max_preview_calls=int(
            explorer_payload.get("max_preview_calls", explorer_defaults.max_preview_calls)
        ),
        max_preview_chars=int(
            explorer_payload.get("max_preview_chars", explorer_defaults.max_preview_chars)
        ),
        max_inventory_chars=int(
            explorer_payload.get("max_inventory_chars", explorer_defaults.max_inventory_chars)
        ),
        max_report_chars=int(
            explorer_payload.get("max_report_chars", explorer_defaults.max_report_chars)
        ),
        max_total_read_bytes=int(
            explorer_payload.get("max_total_read_bytes", explorer_defaults.max_total_read_bytes)
        ),
        max_single_file_bytes=int(
            explorer_payload.get("max_single_file_bytes", explorer_defaults.max_single_file_bytes)
        ),
        max_pdf_bytes=int(explorer_payload.get("max_pdf_bytes", explorer_defaults.max_pdf_bytes)),
        max_pdf_pages=int(explorer_payload.get("max_pdf_pages", explorer_defaults.max_pdf_pages)),
    )
    raw_max_commit_attempts = int(
        evidence_plan_payload.get(
            "max_commit_attempts",
            evidence_plan_defaults.max_commit_attempts,
        )
    )
    if raw_max_commit_attempts not in (1, 2):
        raise ValueError("evidence_plan.max_commit_attempts must be 1 or 2.")
    evidence_plan_config = EvidencePlanConfig(
        enabled=bool(evidence_plan_payload.get("enabled", evidence_plan_defaults.enabled)),
        max_commit_attempts=raw_max_commit_attempts,
        strict_keys=bool(
            evidence_plan_payload.get("strict_keys", evidence_plan_defaults.strict_keys)
        ),
        verification_gate=bool(
            evidence_plan_payload.get("verification_gate", evidence_plan_defaults.verification_gate)
        ),
    )
    return AppConfig(
        dataset=dataset_config,
        agent=agent_config,
        run=run_config,
        explorer=explorer_config,
        evidence_plan=evidence_plan_config,
    )
