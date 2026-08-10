from data_agent_baseline.config import load_app_config


def test_loads_model_retry_and_runner_timeout_settings(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
agent:
  model: test-model
  api_base: https://example.test/v1
  api_key: test-key
  model_request_timeout_seconds: 12.5
  model_max_retries: 2
  model_retry_backoff_seconds: 0.5
run:
  max_workers: 3
  task_timeout_seconds: 45.5
explorer:
  enabled: true
  max_steps: 10
  max_duration_seconds: 55
  max_preview_calls: 2
""".strip(),
        encoding="utf-8",
    )

    config = load_app_config(config_path)

    assert config.agent.model_request_timeout_seconds == 12.5
    assert config.agent.model_max_retries == 2
    assert config.agent.model_retry_backoff_seconds == 0.5
    assert config.run.max_workers == 3
    assert config.run.task_timeout_seconds == 45.5
    assert config.explorer.enabled is True
    assert config.explorer.max_steps == 10
    assert config.explorer.max_duration_seconds == 55
    assert config.explorer.max_preview_calls == 2


def test_can_disable_explorer(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text("explorer:\n  enabled: false\n", encoding="utf-8")

    assert load_app_config(config_path).explorer.enabled is False


def test_evidence_plan_defaults_disabled(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text("", encoding="utf-8")

    config = load_app_config(config_path)

    assert config.evidence_plan.enabled is False
    assert config.evidence_plan.max_commit_attempts == 2
    assert config.evidence_plan.strict_keys is True
    assert config.evidence_plan.verification_gate is True


def test_evidence_plan_yaml_enabled(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "evidence_plan:\n"
        "  enabled: true\n"
        "  max_commit_attempts: 1\n"
        "  strict_keys: false\n"
        "  verification_gate: false\n",
        encoding="utf-8",
    )

    config = load_app_config(config_path)

    assert config.evidence_plan.enabled is True
    assert config.evidence_plan.max_commit_attempts == 1
    assert config.evidence_plan.strict_keys is False
    assert config.evidence_plan.verification_gate is False


def test_rejects_invalid_evidence_plan_max_commit_attempts(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "evidence_plan:\n  max_commit_attempts: 3\n",
        encoding="utf-8",
    )

    try:
        load_app_config(config_path)
    except ValueError as exc:
        assert "max_commit_attempts" in str(exc)
    else:
        raise AssertionError("expected ValueError for max_commit_attempts=3")
