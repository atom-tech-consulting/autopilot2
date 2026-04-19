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
