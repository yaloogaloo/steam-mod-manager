"""Deploy E2E stopwatch: 1:1 job_id and lifecycle events (no real copy)."""

from __future__ import annotations

from services.deploy_e2e import (
    e2e_count,
    e2e_event,
    e2e_span,
    reset_e2e,
    start_deploy_job,
)


def test_one_click_is_one_job() -> None:
    reset_e2e()
    job = start_deploy_job(internal_id="abc", action="deploy", source="ui_click")
    job.count("worker_count")
    e2e_event("worker created")
    e2e_event("worker started")
    e2e_count("deploy_mod_count")
    with e2e_span("core_deploy_mod"):
        pass
    job.core_deploy_ms = 10.0
    e2e_count("finish_signal_count")
    e2e_event("worker finished emitted")
    job.ui_slot_returned = True
    job.worker_thread_finished = True
    job.finish(reason="test")
    summary = job.summary()
    assert summary["job_id"] == job.job_id
    assert summary["deploy_action_count"] == 1
    assert summary["worker_count"] == 1
    assert summary["deploy_mod_count"] == 1
    assert summary["finish_signal_count"] == 1
    assert summary["one_to_one_ok"] is True
    labels = [ev["label"] for ev in summary["events"]]
    assert "DEPLOY_E2E_START" in labels
    assert "UI click" in labels
    assert "DEPLOY_E2E_END" in labels


def test_duplicate_worker_is_violation() -> None:
    reset_e2e()
    job = start_deploy_job(internal_id="abc", action="deploy", source="ui_click")
    job.count("worker_count")
    job.count("worker_count")
    job.count("deploy_mod_count")
    job.count("finish_signal_count")
    job.ui_slot_returned = True
    job.worker_thread_finished = True
    job.finish(reason="test")
    summary = job.summary()
    assert summary["one_to_one_ok"] is False
    assert any("worker_count" in v for v in summary["one_to_one_violations"])
