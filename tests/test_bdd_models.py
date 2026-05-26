from __future__ import annotations
import uuid
from app.models.bdd import BddProject, BddScenario, BddSettings


def test_bdd_project_defaults():
    p = BddProject(name="My Project")
    assert p.name == "My Project"
    assert p.description is None


def test_bdd_scenario_defaults():
    s = BddScenario(
        project_id=uuid.uuid4(),
        title="Login test",
        requirement="User logs in with valid credentials",
        source_type="text",
        gherkin="Feature: Login\n  Scenario: ...",
        status="draft",
        ai_provider="ollama",
        ai_model="llama3.2",
    )
    assert s.status == "draft"
    assert s.tags is None


def test_bdd_settings_defaults():
    s = BddSettings()
    assert s.id == 1
    assert s.gherkin_language == "it"
    assert s.max_scenarios == 5
