import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from l_assistant.memory.user_profile import UserProfileStore


@pytest.fixture
def profile_path(tmp_path: Path) -> Path:
    return tmp_path / "profile.json"


def test_starts_empty_when_no_file_exists(profile_path: Path):
    store = UserProfileStore(profile_path)
    assert store.all_facts() == {}
    assert store.as_prompt_fragment() == ""


def test_remember_then_read_back(profile_path: Path):
    store = UserProfileStore(profile_path)
    store.remember("name", "Alif")
    assert store.all_facts() == {"name": "Alif"}


def test_remember_persists_across_instances(profile_path: Path):
    UserProfileStore(profile_path).remember("name", "Alif")
    reloaded = UserProfileStore(profile_path)
    assert reloaded.all_facts() == {"name": "Alif"}


def test_remember_overwrites_existing_key(profile_path: Path):
    store = UserProfileStore(profile_path)
    store.remember("temperature_unit", "Fahrenheit")
    store.remember("temperature_unit", "Celsius")
    assert store.all_facts() == {"temperature_unit": "Celsius"}


def test_key_normalization_is_consistent(profile_path: Path):
    store = UserProfileStore(profile_path)
    store.remember("Favorite Color", "blue")
    assert store.all_facts() == {"favorite_color": "blue"}
    # Different casing/spacing of the "same" key must hit the same entry.
    store.remember("favorite color", "green")
    assert store.all_facts() == {"favorite_color": "green"}


def test_remember_rejects_empty_key(profile_path: Path):
    store = UserProfileStore(profile_path)
    with pytest.raises(ValueError):
        store.remember("   ", "something")


def test_remember_rejects_empty_value(profile_path: Path):
    store = UserProfileStore(profile_path)
    with pytest.raises(ValueError):
        store.remember("name", "   ")


def test_forget_removes_a_fact_and_reports_true(profile_path: Path):
    store = UserProfileStore(profile_path)
    store.remember("name", "Alif")
    assert store.forget("name") is True
    assert store.all_facts() == {}


def test_forget_nonexistent_key_reports_false_and_is_not_an_error(profile_path: Path):
    store = UserProfileStore(profile_path)
    assert store.forget("nothing_here") is False


def test_forget_all_clears_everything(profile_path: Path):
    store = UserProfileStore(profile_path)
    store.remember("name", "Alif")
    store.remember("timezone", "GMT+6")
    store.forget_all()
    assert store.all_facts() == {}


def test_as_prompt_fragment_empty_profile(profile_path: Path):
    store = UserProfileStore(profile_path)
    assert store.as_prompt_fragment() == ""


def test_as_prompt_fragment_lists_known_facts_readably(profile_path: Path):
    store = UserProfileStore(profile_path)
    store.remember("name", "Alif")
    store.remember("preferred_language", "English")
    fragment = store.as_prompt_fragment()
    assert "name: Alif" in fragment
    assert "preferred language: English" in fragment  # underscore rendered as space


def test_corrupted_file_starts_empty_instead_of_crashing(profile_path: Path):
    profile_path.write_text("{not valid json", encoding="utf-8")
    store = UserProfileStore(profile_path)
    assert store.all_facts() == {}


def test_file_containing_a_json_list_not_object_starts_empty(profile_path: Path):
    profile_path.write_text(json.dumps(["not", "a", "dict"]), encoding="utf-8")
    store = UserProfileStore(profile_path)
    assert store.all_facts() == {}


def test_write_is_atomic_no_temp_file_left_behind(profile_path: Path):
    store = UserProfileStore(profile_path)
    store.remember("name", "Alif")
    assert profile_path.exists()
    assert not profile_path.with_suffix(".tmp").exists()


def test_file_is_genuinely_human_readable_json(profile_path: Path):
    store = UserProfileStore(profile_path)
    store.remember("name", "Alif")
    on_disk = json.loads(profile_path.read_text(encoding="utf-8"))
    assert on_disk == {"name": "Alif"}
