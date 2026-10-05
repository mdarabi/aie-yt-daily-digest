import json
from argparse import Namespace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock

import pytest

from digest.main import (Candidate, _deep_discover, compute_window_start,
                         feed_is_saturated, run, select_feed_candidates)
from digest.config import Config
from digest.state import State
from digest.summarize import AuthError
from digest.youtube import FeedEntry, VideoRecord

NOW = datetime(2026, 7, 8, 6, 0, 0, tzinfo=timezone.utc)


def entry(video_id, hours_ago):
    return FeedEntry(
        video_id=video_id,
        title=video_id,
        url=f"https://www.youtube.com/watch?v={video_id}",
        published=NOW - timedelta(hours=hours_ago),
    )


# --- window -----------------------------------------------------------------------

def test_window_first_run_uses_lookback():
    start = compute_window_start(NOW, None, lookback_hours=26, max_catchup_hours=168)
    assert start == NOW - timedelta(hours=26)


def test_window_resumes_from_last_success_with_overlap():
    last = NOW - timedelta(hours=24)
    start = compute_window_start(NOW, last, lookback_hours=26, max_catchup_hours=168)
    assert start == last - timedelta(hours=1)


def test_window_catchup_capped():
    last = NOW - timedelta(days=30)  # machine was off for a month
    start = compute_window_start(NOW, last, lookback_hours=26, max_catchup_hours=168)
    assert start == NOW - timedelta(hours=168)


def test_window_backfill_override():
    start = compute_window_start(NOW, NOW - timedelta(hours=2), lookback_hours=26,
                                 max_catchup_hours=168, backfill_hours=72)
    assert start == NOW - timedelta(hours=72)


# --- candidate selection ------------------------------------------------------------

def test_select_feed_candidates_filters_seen_and_old():
    feed = [entry("new", 2), entry("seen", 3), entry("old", 50)]
    fresh = select_feed_candidates(feed, seen={"seen"}, window_start=NOW - timedelta(hours=26))
    assert [e.video_id for e in fresh] == ["new"]


def test_feed_not_saturated_when_some_entries_known():
    feed = [entry(f"v{i}", i) for i in range(15)]
    fresh = feed[:14]  # one entry was already seen
    assert not feed_is_saturated(feed, fresh)


def test_feed_saturated_when_all_15_are_new():
    feed = [entry(f"v{i}", i) for i in range(15)]
    assert feed_is_saturated(feed, list(feed))


def test_small_feed_never_saturates():
    feed = [entry(f"v{i}", i) for i in range(3)]
    assert not feed_is_saturated(feed, list(feed))


# --- deep discovery -----------------------------------------------------------------

def record(video_id, hours_ago):
    return VideoRecord(
        video_id=video_id, title=video_id,
        url=f"https://www.youtube.com/watch?v={video_id}",
        published=NOW - timedelta(hours=hours_ago),
        duration=600, duration_string="10:00", description="",
        live_status="not_live", transcript="t", transcript_source="auto",
    )


def _patched_playlist(monkeypatch, playlist, records):
    monkeypatch.setattr(
        "digest.youtube.list_uploads",
        lambda pid, start, end: playlist[start - 1:end] if start <= len(playlist) else [],
    )
    monkeypatch.setattr(
        "digest.youtube.fetch_video",
        lambda vid, hint=None, timeout=300: records[vid],
    )


def test_deep_discover_stops_at_seen_video(monkeypatch):
    # Daily saturation mode: everything past the first seen id is already known.
    state = State()
    state.mark_seen("seen1", NOW)
    playlist = ["new1", "new2", "seen1", "new3"]
    records = {vid: record(vid, i + 1) for i, vid in enumerate(playlist)}
    _patched_playlist(monkeypatch, playlist, records)

    cfg = Config(root=Path("."))
    found = _deep_discover(cfg, state, NOW - timedelta(days=14), set())
    assert [c.video_id for c in found] == ["new1", "new2"]


def test_deep_discover_backfill_skips_seen_and_stops_at_window(monkeypatch):
    # Backfill mode: seen ids are a recent stripe to step over, not a terminator.
    state = State()
    state.mark_seen("seen1", NOW)
    state.mark_seen("seen2", NOW)
    playlist = ["new1", "seen1", "seen2", "new2", "too_old", "never_reached"]
    records = {
        "new1": record("new1", hours_ago=2),
        "new2": record("new2", hours_ago=24 * 10),
        "too_old": record("too_old", hours_ago=24 * 20),
    }
    _patched_playlist(monkeypatch, playlist, records)

    cfg = Config(root=Path("."))
    found = _deep_discover(cfg, state, NOW - timedelta(days=14), set(), skip_seen=True)
    assert [c.video_id for c in found] == ["new1", "new2"]
    assert all(c.record is not None for c in found)


# --- authentication failures -------------------------------------------------------

@pytest.mark.parametrize(
    "batch_size, successful_summaries",
    [(1, 0), (3, 1)],
    ids=["single-video", "mid-batch"],
)
def test_run_auth_failure_aborts_batch_without_sending_or_saving_state(
        tmp_path, monkeypatch, batch_size, successful_summaries):
    cfg = Config(root=tmp_path)
    records = [record(f"video{i}", hours_ago=i + 1) for i in range(batch_size)]
    state = State(last_success=NOW - timedelta(days=1))
    state.mark_seen("previously-sent", NOW - timedelta(days=1))
    for rec in records:
        state.defer(rec.video_id, rec.title, "captions pending", rec.published, NOW)
    state.save(cfg.state_file)
    original_state = cfg.state_file.read_bytes()

    candidates = [Candidate(rec.video_id, rec.published, rec) for rec in records]
    monkeypatch.setattr("digest.main.discover_candidates", Mock(return_value=candidates))
    summary = json.dumps({
        "problem": "A problem worth solving.",
        "solution": "An approach to solving it.",
        "topics": ["agents"],
    })
    runner = Mock(side_effect=[
        *([summary] * successful_summaries),
        AuthError("Not logged in · Please run /login"),
    ])
    monkeypatch.setattr("digest.main.make_claude_runner", Mock(return_value=runner))
    send_email = Mock()
    monkeypatch.setattr("digest.main.send_email", send_email)
    args = Namespace(test_latest=None, dry_run=False, no_send=False, backfill_hours=None)

    with pytest.raises(AuthError, match="Not logged in"):
        run(cfg, args)

    assert runner.call_count == successful_summaries + 1
    send_email.assert_not_called()
    assert cfg.state_file.read_bytes() == original_state
