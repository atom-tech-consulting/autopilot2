from __future__ import annotations

import time

import pytest

from ap2 import cron


def test_parse_interval():
    assert cron.parse_interval("30s") == 30
    assert cron.parse_interval("5m") == 300
    assert cron.parse_interval("2h") == 7200
    assert cron.parse_interval("1d") == 86400
    assert cron.parse_interval(45) == 45


def test_interval_bad():
    with pytest.raises(ValueError):
        cron.parse_interval("abc")


def test_roundtrip_yaml(tmp_path):
    f = tmp_path / "cron.yaml"
    cron.save_jobs(
        f,
        [
            cron.CronJob(name="a", interval_s=60, prompt="x", max_turns=5),
            cron.CronJob(name="b", interval_s=3600, prompt="y", active_when="foo exists"),
        ],
    )
    jobs = cron.load_jobs(f)
    assert [j.name for j in jobs] == ["a", "b"]
    assert jobs[0].interval_s == 60
    assert jobs[1].active_when == "foo exists"


def test_update_add_remove(tmp_path):
    f = tmp_path / "cron.yaml"
    cron.save_jobs(f, [])
    msg, jobs = cron.update_job(f, "add", name="job1", interval="1m", prompt="hi")
    assert "added" in msg and len(jobs) == 1
    with pytest.raises(ValueError):
        cron.update_job(f, "add", name="job1", interval="1m", prompt="hi")
    msg, jobs = cron.update_job(f, "update", name="job1", interval="5m")
    assert jobs[0].interval_s == 300
    msg, jobs = cron.update_job(f, "remove", name="job1")
    assert jobs == []


def test_evaluate_condition_exists(tmp_path):
    (tmp_path / "flag").write_text("")
    assert cron.evaluate_condition("flag exists", tmp_path)
    assert not cron.evaluate_condition("nothere exists", tmp_path)
    assert cron.evaluate_condition("nothere missing", tmp_path)


def test_evaluate_condition_shell(tmp_path):
    assert cron.evaluate_condition("sh:true", tmp_path)
    assert not cron.evaluate_condition("sh:false", tmp_path)


def test_due_jobs(tmp_path):
    jobs = [
        cron.CronJob(name="quick", interval_s=10, prompt=""),
        cron.CronJob(name="slow", interval_s=3600, prompt=""),
    ]
    state = {"quick": time.time(), "slow": 0}
    due = cron.due_jobs(jobs, state, tmp_path)
    assert [j.name for j in due] == ["slow"]


def test_due_jobs_active_when(tmp_path):
    jobs = [
        cron.CronJob(name="gated", interval_s=1, prompt="", active_when="flag exists"),
    ]
    state = {"gated": 0}
    assert cron.due_jobs(jobs, state, tmp_path) == []
    (tmp_path / "flag").write_text("")
    due = cron.due_jobs(jobs, state, tmp_path)
    assert len(due) == 1
