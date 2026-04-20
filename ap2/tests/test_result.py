from ap2.result import parse


def test_parse_complete():
    text = """
Some preamble.

RESULT:
status: complete
commit: abcd1234
summary: did the thing
files_changed: a.py, b.py
tests_passed: true
"""
    r = parse(text)
    assert r.status == "complete"
    assert r.commit == "abcd1234"
    assert r.summary == "did the thing"
    assert r.files_changed == ["a.py", "b.py"]
    assert r.tests_passed is True


def test_parse_fenced():
    text = """
```
RESULT:
status: blocked
commit: none
summary: hit a wall
```
"""
    r = parse(text)
    assert r.status == "blocked"
    assert r.commit == ""
    assert r.summary == "hit a wall"


def test_parse_missing():
    r = parse("no result here")
    assert r.status == "unknown"


def test_parse_failed():
    r = parse("RESULT:\nstatus: failed\nsummary: nope")
    assert r.status == "failed"
    assert r.summary == "nope"


# ---- cron directives (TB-52) ----


def test_parse_cron_add():
    text = (
        "RESULT:\n"
        "status: complete\n"
        "summary: added a job\n"
        "cron: add name=nightly interval=1d prompt=\"run nightly report\"\n"
    )
    r = parse(text)
    assert r.status == "complete"
    assert len(r.cron) == 1
    d = r.cron[0]
    assert d == {
        "action": "add",
        "name": "nightly",
        "interval": "1d",
        "prompt": "run nightly report",
    }


def test_parse_cron_remove_and_update():
    text = (
        "RESULT:\n"
        "status: complete\n"
        "cron: remove name=old-job\n"
        "cron: update name=status-report interval=4h\n"
    )
    r = parse(text)
    assert len(r.cron) == 2
    assert r.cron[0] == {"action": "remove", "name": "old-job"}
    assert r.cron[1] == {"action": "update", "name": "status-report", "interval": "4h"}


def test_parse_cron_malformed_unknown_action():
    r = parse("RESULT:\nstatus: complete\ncron: bogus name=x\n")
    assert len(r.cron) == 1
    assert "_error" in r.cron[0]
    assert "bogus" in r.cron[0]["_error"]


def test_parse_cron_missing_name():
    r = parse("RESULT:\nstatus: complete\ncron: add interval=1h prompt=x\n")
    assert len(r.cron) == 1
    assert r.cron[0]["_error"] == "missing name"


def test_parse_cron_add_missing_required():
    r = parse("RESULT:\nstatus: complete\ncron: add name=only\n")
    assert len(r.cron) == 1
    assert "interval and prompt" in r.cron[0]["_error"]


def test_parse_no_cron_defaults_to_empty():
    r = parse("RESULT:\nstatus: complete\nsummary: regular task\n")
    assert r.cron == []
