from __future__ import annotations
import uuid
from app.schemas.bdd import ProjectOut, ScenarioCreate, SettingsOut, SettingsUpdate


def test_project_out_from_dict():
    import datetime
    data = {
        "id": uuid.uuid4(),
        "name": "Test",
        "description": None,
        "created_at": datetime.datetime.now(),
        "updated_at": datetime.datetime.now(),
        "scenario_count": 3,
    }
    p = ProjectOut(**data)
    assert p.name == "Test"
    assert p.scenario_count == 3


def test_scenario_create_defaults():
    s = ScenarioCreate(
        project_id=uuid.uuid4(),
        title="Login",
        requirement="User authenticates",
        source_type="text",
        gherkin="Feature: Login",
        ai_provider="ollama",
        ai_model="llama3.2",
    )
    assert s.status == "draft"
    assert s.tags == []


def test_settings_update_partial():
    u = SettingsUpdate(ollama_model="mistral")
    d = u.model_dump(exclude_unset=True)
    assert d == {"ollama_model": "mistral"}


def test_settings_out_masks_key():
    s = SettingsOut(
        ai_provider="claude",
        claude_api_key_set=True,
        claude_model="claude-sonnet-4-6",
        ollama_base_url="http://localhost:11434",
        ollama_model="llama3.2",
        confluence_email=None,
        confluence_token_set=False,
        gherkin_language="it",
        max_scenarios=5,
    )
    assert s.claude_api_key_set is True
