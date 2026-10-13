import importlib

import pytest

from app import agent, config, store

PRIYA = "priya@northbeam.com"


def test_demo_defaults():
    assert config.FIRM_NAME == "Northbeam Capital"
    assert config.deal_group("falcon") == "group:northbeam:falcon"
    assert config.is_demo()


def test_the_demo_is_set_on_its_own_day(monkeypatch):
    monkeypatch.delenv("BRAIN_TODAY", raising=False)
    assert agent.today() == "Monday, October 12, 2026"


def test_today_elsewhere_is_today(monkeypatch):
    monkeypatch.delenv("BRAIN_TODAY", raising=False)
    monkeypatch.setattr(config, "is_demo", lambda: False)
    import datetime as dt

    day = dt.date.today()
    assert agent.today() == f"{day:%A}, {day:%B} {day.day}, {day.year}"
    monkeypatch.setenv("BRAIN_TODAY", "2027-01-04")
    assert agent.today() == "Monday, January 4, 2027"


def test_a_firm_names_itself(monkeypatch):
    monkeypatch.setattr(config, "FIRM_NAME", "Harrow Partners")
    monkeypatch.setattr(config, "FIRM_DESCRIPTION", "a growth equity firm")
    monkeypatch.setattr(config, "FIRM_SLUG", "harrow")
    assert "company brain of Harrow Partners, a growth equity firm" in agent._system_prompt(PRIYA)
    assert "group:harrow:falcon" in store.user_principals(PRIYA)


@pytest.fixture
def reloaded_config(monkeypatch, tmp_path):
    yield lambda: importlib.reload(config)
    monkeypatch.delenv("BRAIN_IMPORT_ROOT", raising=False)
    importlib.reload(config)


def test_import_root_from_the_environment(monkeypatch, tmp_path, reloaded_config):
    monkeypatch.setenv("BRAIN_IMPORT_ROOT", str(tmp_path))
    cfg = reloaded_config()
    assert cfg.IMPORT_ROOT == tmp_path
    assert not cfg.is_demo()
