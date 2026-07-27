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
  max_steps: 3
  max_preview_calls: 2
  max_prompt_inventory_chars: 5000
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
    assert config.explorer.max_steps == 3
    assert config.explorer.max_preview_calls == 2
    assert config.explorer.max_prompt_inventory_chars == 5000


def test_can_disable_explorer(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text("explorer:\n  enabled: false\n", encoding="utf-8")

    assert load_app_config(config_path).explorer.enabled is False
