import json
from datetime import date, timedelta

import pytest

import route

TODAY = date(2026, 10, 10)


@pytest.fixture
def store(tmp_path):
    s = route.Store(path=tmp_path / "employees.json")
    s.data = {"employees": {}}
    s.add({"id": "anna", "name": "Анна", "position": "Маркетолог", "department": "Маркетинг",
           "start_date": (TODAY - timedelta(days=5)).isoformat(), "manager": "Ольга", "tg": "anna"})
    s.add({"id": "ivan", "name": "Иван", "position": "Разработчик", "department": "IT",
           "start_date": (TODAY + timedelta(days=10)).isoformat(), "manager": "Сергей"})
    return s


def test_route_depends_on_department(store):
    anna = [s["id"] for s in store.route(store.get("anna"))]
    ivan = [s["id"] for s in store.route(store.get("ivan"))]
    assert "marketing_pd" in anna and "marketing_pd" not in ivan


def test_employee_cannot_confirm_mandatory_training(store):
    with pytest.raises(PermissionError):
        store.confirm("anna", "safety_briefing", "сотрудник", "anna", TODAY)
    store.confirm("anna", "safety_briefing", "от", "ОТ", TODAY)
    store.confirm("anna", "vpn", "сотрудник", "anna", TODAY)
    with pytest.raises(ValueError):
        store.confirm("anna", "vpn", "сотрудник", "anna", TODAY)


def test_statuses(store):
    anna = store.get("anna")
    by = store.by_id
    assert route.stage_status(anna, by["route_confirm"], TODAY) == "overdue"
    assert route.stage_status(anna, by["probation_goals"], TODAY - timedelta(days=1)) == "today"
    assert route.stage_status(anna, by["infosec_test"], TODAY) == "upcoming"
    store.confirm("anna", "route_confirm", "hr", "hr", TODAY)
    assert route.stage_status(anna, by["route_confirm"], TODAY) == "late_done"


def test_reminders_are_idempotent_and_escalate(store):
    first = route.due_reminders(store, TODAY)
    kinds = {(r.emp_id, r.stage_id, r.kind, r.to) for r in first}
    # у Анны маршрут не подтверждён с дня −5: руководителю — просрочка, HR — эскалация (≥3 дн.)
    assert ("anna", "route_confirm", "overdue", "manager") in kinds
    assert ("anna", "route_confirm", "escalation", "hr") in kinds
    assert route.due_reminders(store, TODAY) == []                        # в тот же день — без повторов
    again = route.due_reminders(store, TODAY + timedelta(days=1))
    assert any(r.kind == "overdue" for r in again)                        # просрочка напоминается ежедневно
    assert not any(r.kind == "escalation" and r.stage_id == "route_confirm" for r in again)  # эскалация — один раз


def test_no_reminders_for_far_future(store):
    assert not [r for r in route.due_reminders(store, TODAY, mark=False) if r.emp_id == "ivan"]


def test_metrics(store, tmp_path):
    store.confirm("anna", "access_standard", "it", "it", TODAY - timedelta(days=7))   # за 2 дня до выхода
    trace = tmp_path / "trace.jsonl"
    trace.write_text("\n".join(json.dumps({"route": r}) for r in ["ANSWER", "ANSWER", "TO_HR", "BLOCK"]))
    m = route.metrics(store, TODAY, trace)
    assert m["access_days_vs_start"] == -2
    assert m["questions_without_hr_pct"] == 50.0
    assert 0 <= m["mandatory_on_time_pct"] < 100


def test_example_is_resolved_relative_to_today(tmp_path):
    s = route.Store(path=tmp_path / "e.json")
    for e in s.employees.values():
        date.fromisoformat(e["start_date"])
        for d in e["done"].values():
            date.fromisoformat(d["at"])
