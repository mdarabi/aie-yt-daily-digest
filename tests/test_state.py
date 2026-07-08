from datetime import datetime, timedelta, timezone

from digest.state import State

NOW = datetime(2026, 7, 8, 6, 0, 0, tzinfo=timezone.utc)


def test_roundtrip(tmp_path):
    path = tmp_path / "state" / "state.json"
    state = State()
    state.last_success = NOW
    state.mark_seen("vid1", NOW)
    state.defer("vid2", "A pending talk", "captions pending",
                NOW - timedelta(hours=2), NOW)
    state.save(path)

    loaded = State.load(path)
    assert loaded.last_success == NOW
    assert loaded.seen == {"vid1": NOW}
    assert set(loaded.deferred) == {"vid2"}
    d = loaded.deferred["vid2"]
    assert d.title == "A pending talk"
    assert d.reason == "captions pending"
    assert d.published == NOW - timedelta(hours=2)
    assert d.first_seen == NOW


def test_load_missing_file(tmp_path):
    state = State.load(tmp_path / "nope.json")
    assert state.last_success is None
    assert state.seen == {}
    assert state.deferred == {}


def test_mark_seen_clears_deferral():
    state = State()
    state.defer("vid1", "t", "captions pending", None, NOW)
    state.mark_seen("vid1", NOW)
    assert "vid1" in state.seen
    assert "vid1" not in state.deferred


def test_defer_keeps_first_seen():
    state = State()
    state.defer("vid1", "t", "captions pending", None, NOW)
    state.defer("vid1", "t", "captions pending", None, NOW + timedelta(days=1))
    assert state.deferred["vid1"].first_seen == NOW


def test_prune():
    state = State()
    state.mark_seen("old", NOW - timedelta(days=200))
    state.mark_seen("recent", NOW - timedelta(days=5))
    state.defer("stale", "t", "captions pending", None, NOW - timedelta(days=10))
    state.defer("fresh", "t", "captions pending", None, NOW - timedelta(days=1))
    state.prune(NOW, seen_retention_days=120, deferred_retention_days=7)
    assert set(state.seen) == {"recent"}
    assert set(state.deferred) == {"fresh"}
