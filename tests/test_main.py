import json
from argparse import Namespace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock

import pytest

import digest.main as digest_main
from digest.main import (Candidate, _deep_discover, compute_window_start,
                         feed_is_saturated, run, select_feed_candidates)
from digest.config import Config
from digest.state import State
from digest.summarize import AuthError
from digest.youtube import FeedEntry, VideoRecord, YouTubeError

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


def _notification_run(monkeypatch, tmp_path):
    cfg = Config(root=tmp_path, resend_api_key="test-resend-key", email_to="test@example.com")
    state = State(last_success=NOW - timedelta(days=1))
    state.mark_seen("previously-sent", NOW - timedelta(days=1))
    state.save(cfg.state_file)
    candidates = [Candidate("ready", NOW, record("ready", 1))]
    monkeypatch.setattr(digest_main.Config, "load", Mock(return_value=cfg))
    monkeypatch.setattr(digest_main, "_setup_logging", Mock())
    monkeypatch.setattr(digest_main, "discover_candidates", Mock(return_value=candidates))
    send_email = Mock(return_value="test-message-id")
    monkeypatch.setattr(digest_main, "send_email", send_email)
    return cfg, candidates, send_email


def test_failure_notification_explains_auth_expiry_and_related_youtube_limit(monkeypatch, tmp_path):
    cfg, candidates, send_email = _notification_run(monkeypatch, tmp_path)
    original_state = cfg.state_file.read_bytes()
    candidates.insert(0, Candidate("blocked-video", NOW))
    youtube_error = YouTubeError("Unable to download subtitles: HTTP Error 429: Too Many Requests")
    monkeypatch.setattr(digest_main.youtube, "fetch_video", Mock(side_effect=youtube_error))
    reason = "Failed to authenticate: OAuth session expired and could not be refreshed"
    runner = Mock(side_effect=AuthError(reason))
    monkeypatch.setattr(digest_main, "make_claude_runner", Mock(return_value=runner))

    assert digest_main.main([]) == 1
    assert runner.call_count == 1
    send_email.assert_called_once()
    subject, html_body, text_body = send_email.call_args.args[3:]
    assert "FAILED: Claude login expired" in subject
    for body in (html_body, text_body):
        assert "Claude summaries" in body
        assert "OAuth session expired" in body
        assert "/login" in body
        assert "uv run aie-digest --test-latest 1 --dry-run" in body
        assert "YouTube is rate-limiting requests" in body
        assert "HTTP 429" in body
        assert "https://www.youtube.com/watch?v=blocked-video" in body
        assert "test-resend-key" not in body
        assert "Traceback (most recent call last)" not in body
    assert "Affected video: ready (ready)" in text_body
    assert cfg.state_file.read_bytes() == original_state


@pytest.mark.parametrize("argv, error_emails", [(["--dry-run"], True), (["--no-send"], True), ([], False)])
def test_failure_notification_respects_no_email_settings(monkeypatch, tmp_path, argv, error_emails):
    cfg, _, send_email = _notification_run(monkeypatch, tmp_path)
    cfg.error_emails = error_emails
    monkeypatch.setattr(digest_main, "make_claude_runner",
                        Mock(return_value=Mock(side_effect=AuthError("OAuth session expired"))))

    assert digest_main.main(argv) == 1
    send_email.assert_not_called()


def test_state_save_failure_notification_acknowledges_digest_already_sent(monkeypatch, tmp_path):
    cfg, _, send_email = _notification_run(monkeypatch, tmp_path)
    original_state = cfg.state_file.read_bytes()
    summary = json.dumps({"problem": "A problem.", "solution": "A solution.", "topics": ["agents"]})
    monkeypatch.setattr(digest_main, "make_claude_runner", Mock(return_value=Mock(return_value=summary)))
    monkeypatch.setattr(digest_main.State, "save", Mock(side_effect=PermissionError("state folder is not writable")))

    assert digest_main.main([]) == 1
    assert send_email.call_count == 2  # digest, followed by the failure notification
    subject, _, text_body = send_email.call_args.args[3:]
    assert "state could not be read or saved" in subject
    assert "Saving run state" in text_body
    assert "digest email was sent" in text_body
    assert "same videos again" in text_body
    assert cfg.state_file.read_bytes() == original_state
