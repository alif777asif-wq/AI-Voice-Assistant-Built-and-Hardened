import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from l_assistant.config import AppConfig, load_config


def test_defaults_load_without_a_file():
    config = load_config(None)
    assert config.name == "L"
    assert config.audio.sample_rate == 16000
    assert config.wake_word.model == "hey_jarvis"
    assert config.llm.model == "llama3.2:3b"


def test_missing_file_path_raises_clearly_instead_of_silently_using_defaults(tmp_path: Path):
    missing = tmp_path / "does_not_exist.yaml"
    with pytest.raises(FileNotFoundError, match="Config file not found"):
        load_config(missing)


def test_none_path_uses_defaults_without_error():
    # The one legitimate "missing" case: no --config was given at all.
    config = load_config(None)
    assert config == AppConfig()


def test_partial_yaml_overrides_only_specified_keys(tmp_path: Path):
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(yaml.safe_dump({"llm": {"model": "qwen2.5:3b-instruct"}}))
    config = load_config(cfg_path)
    assert config.llm.model == "qwen2.5:3b-instruct"
    # Untouched sibling keys keep their defaults.
    assert config.llm.temperature == 0.6
    assert config.stt.model_size == "small.en"


def test_frame_samples_must_divide_into_20ms_subframes():
    with pytest.raises(ValueError):
        AppConfig(audio={"frame_samples": 1234})


def test_vad_aggressiveness_out_of_range_rejected():
    with pytest.raises(ValueError):
        AppConfig(vad={"aggressiveness": 4})


def test_env_override_takes_precedence(tmp_path: Path, monkeypatch):
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(yaml.safe_dump({"llm": {"model": "from-yaml"}}))
    monkeypatch.setenv("L_LLM_MODEL", "from-env")
    config = load_config(cfg_path)
    assert config.llm.model == "from-env"
