"""Smoke tests for `ap2/cron.default.yaml` and first-start bootstrap.

Ideation lives in `ap2/ideation.py` (and `ap2/ideation.default.md`) — see
`test_ideation_defaults.py` for the prompt-content pins. cron.yaml is now
status-report only.
"""
from __future__ import annotations

from pathlib import Path

from ap2.cron import CronJob, bootstrap, load_jobs


DEFAULT = Path(__file__).resolve().parent.parent / "cron.default.yaml"


def test_default_cron_file_exists():
    assert DEFAULT.exists()


def test_default_cron_parses_cleanly():
    jobs = load_jobs(DEFAULT)
    names = {j.name for j in jobs}
    assert "status-report" in names
    # Ideation is no longer a cron job — see `ap2/ideation.py`.
    assert "ideation" not in names
    for j in jobs:
        assert isinstance(j, CronJob)
        assert j.interval_s > 0
        assert j.prompt.strip()


def test_default_cron_intervals_are_sane():
    jobs = {j.name: j for j in load_jobs(DEFAULT)}
    assert 600 <= jobs["status-report"].interval_s <= 8 * 3600


def test_default_cron_ships_real_sdk_smoke_job_at_6h():
    """TB-350: the shipped template seeds a `real-sdk-smoke` job on a 6h
    interval. New projects bootstrap it inert (the routine no-ops unless
    `AP2_REAL_SDK` is set); existing projects activate via the operator
    CLI after the daemon picks up the `run_cron` routine branch.
    """
    jobs = {j.name: j for j in load_jobs(DEFAULT)}
    assert "real-sdk-smoke" in jobs
    assert jobs["real-sdk-smoke"].interval_s == 6 * 3600


def test_bootstrap_copies_default(tmp_path: Path):
    target = tmp_path / "cron.yaml"
    assert not target.exists()

    copied = bootstrap(target)
    assert copied is True
    assert target.exists()

    # Re-run: should be a no-op now that the file exists.
    copied2 = bootstrap(target)
    assert copied2 is False

    # And the file should parse as valid jobs.
    jobs = load_jobs(target)
    # TB-350 seeds the `real-sdk-smoke` job alongside `status-report`.
    assert {j.name for j in jobs} == {"status-report", "real-sdk-smoke"}


def test_bootstrap_creates_parent_dir(tmp_path: Path):
    target = tmp_path / "sub" / "deeper" / "cron.yaml"
    assert bootstrap(target) is True
    assert target.exists()
