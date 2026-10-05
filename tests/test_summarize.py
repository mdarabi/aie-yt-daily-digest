import json
import os
import pwd
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock

import pytest

import digest.summarize as summarize
from digest.config import Config
from digest.summarize import (FALLBACK_THEME_TITLE, MORE_THEME_TITLE, AuthError,
                              SummarizeError, Theme, VideoSummary, _failure_reason,
                              _unwrap_cli_envelope, claude_env, extract_json,
                              group_into_themes, summarize_video)
from digest.youtube import VideoRecord


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(summarize.time, "sleep", lambda _: None)


def make_video(video_id="vid1", transcript="some transcript"):
    return VideoRecord(
        video_id=video_id,
        title=f"Talk {video_id}",
        url=f"https://www.youtube.com/watch?v={video_id}",
        published=datetime(2026, 7, 8, tzinfo=timezone.utc),
        duration=1148,
        duration_string="19:08",
        description="A talk description",
        live_status="not_live",
        transcript=transcript,
        transcript_source="auto" if transcript else None,
    )


GOOD_SUMMARY = json.dumps({
    "problem": "Agents struggle with spreadsheets.",
    "solution": "A diff-based harness plus an eval suite.",
    "topics": ["coding agents", "evals"],
})


# --- extract_json / envelope ------------------------------------------------------

def test_extract_json_plain():
    assert extract_json('{"a": 1}') == {"a": 1}


def test_extract_json_with_fences_and_prose():
    text = "Here you go:\n```json\n{\"a\": 1}\n```"
    assert extract_json(text) == {"a": 1}


def test_extract_json_prose_around_object():
    assert extract_json('Sure! {"a": 1} Hope that helps.') == {"a": 1}


def test_extract_json_rejects_garbage():
    with pytest.raises(SummarizeError):
        extract_json("no json here at all")


def test_unwrap_cli_envelope():
    envelope = json.dumps({"type": "result", "is_error": False, "result": "{\"a\": 1}"})
    assert _unwrap_cli_envelope(envelope) == '{"a": 1}'


def test_unwrap_cli_envelope_error():
    envelope = json.dumps({"type": "result", "is_error": True, "result": "usage limit reached"})
    with pytest.raises(SummarizeError, match="usage limit"):
        _unwrap_cli_envelope(envelope)


def test_unwrap_cli_envelope_non_json_passthrough():
    assert _unwrap_cli_envelope("plain text") == "plain text"


def test_unwrap_cli_envelope_login_error_is_auth_error():
    envelope = json.dumps({"type": "result", "is_error": True,
                           "result": "Not logged in · Please run /login"})
    with pytest.raises(AuthError):
        _unwrap_cli_envelope(envelope)


# --- claude subprocess environment ------------------------------------------------

def test_claude_env_sets_user_even_when_absent(monkeypatch):
    """A LaunchAgent passes only PATH and HOME; the CLI needs USER to find its
    Keychain login, so the runner must supply it itself."""
    monkeypatch.delenv("USER", raising=False)
    monkeypatch.delenv("LOGNAME", raising=False)

    env = claude_env()
    assert env["USER"] == pwd.getpwuid(os.getuid()).pw_name
    assert env["LOGNAME"] == env["USER"]


def test_claude_env_strips_api_key_and_claudecode(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-should-not-leak")
    monkeypatch.setenv("CLAUDECODE", "1")

    env = claude_env()
    assert "ANTHROPIC_API_KEY" not in env
    assert "CLAUDECODE" not in env


# --- failure reporting ------------------------------------------------------------

def _proc(stdout="", stderr="", returncode=1):
    return subprocess.CompletedProcess(
        args=["claude"], returncode=returncode, stdout=stdout, stderr=stderr)


def test_failure_reason_reads_result_from_the_envelope():
    """The CLI puts the reason near the front of a ~900-char envelope; tailing
    the output drops it and leaves only zeroed token counters."""
    envelope = json.dumps({
        "is_error": True,
        "result": "Not logged in · Please run /login",
        "usage": {"input_tokens": 0, "output_tokens": 0},
        "terminal_reason": "api_error",
        "uuid": "f76154c7-ac96-4106-8682-4d97f58d824d",
    })
    assert _failure_reason(_proc(stdout=envelope)) == "Not logged in · Please run /login"


def test_failure_reason_falls_back_to_raw_text():
    assert _failure_reason(_proc(stderr="command not found")) == "command not found"


def test_failure_reason_handles_empty_output():
    assert _failure_reason(_proc()) == "no output"


def test_auth_failure_is_not_retried():
    calls = {"n": 0}

    def runner(prompt):
        calls["n"] += 1
        raise AuthError("claude CLI is not authenticated: Not logged in · Please run /login")

    with pytest.raises(AuthError):
        summarize_video(make_video(), runner)
    assert calls["n"] == 1, "a broken login must not burn a second attempt and a backoff"


@pytest.mark.parametrize("returncode", [0, 1])
def test_real_oauth_expiry_message_is_recognized_without_retry(monkeypatch, returncode):
    reason = "Failed to authenticate: OAuth session expired and could not be refreshed"
    proc = _proc(stdout=json.dumps({"is_error": True, "result": reason}), returncode=returncode)
    subprocess_run = Mock(return_value=proc)
    monkeypatch.setattr(summarize, "find_claude_bin", lambda configured: "claude")
    monkeypatch.setattr(summarize.subprocess, "run", subprocess_run)
    runner = summarize.make_claude_runner(Config(root=Path(".")))

    with pytest.raises(AuthError, match="OAuth session expired"):
        summarize_video(make_video(), runner)
    assert subprocess_run.call_count == 1


def test_usage_limit_still_retries():
    calls = {"n": 0}

    def runner(prompt):
        calls["n"] += 1
        if calls["n"] == 1:
            raise SummarizeError("claude -p exited 1: 5-hour limit reached")
        return GOOD_SUMMARY

    assert summarize_video(make_video(), runner).problem
    assert calls["n"] == 2


# --- summarize_video --------------------------------------------------------------

def test_summarize_video_happy_path():
    prompts = []

    def runner(prompt):
        prompts.append(prompt)
        return GOOD_SUMMARY

    summary = summarize_video(make_video(), runner)
    assert summary.problem == "Agents struggle with spreadsheets."
    assert summary.topics == ["coding agents", "evals"]
    assert "some transcript" in prompts[0]
    assert "Talk vid1" in prompts[0]


def test_summarize_video_retries_bad_json_once():
    replies = iter(["not json at all", GOOD_SUMMARY])

    def runner(prompt):
        return next(replies)

    summary = summarize_video(make_video(), runner)
    assert summary.solution.startswith("A diff-based")


def test_summarize_video_fails_after_two_bad_replies():
    def runner(prompt):
        return "still not json"

    with pytest.raises(SummarizeError):
        summarize_video(make_video(), runner)


def test_summarize_video_missing_key_rejected():
    def runner(prompt):
        return json.dumps({"problem": "p"})  # no solution

    with pytest.raises(SummarizeError):
        summarize_video(make_video(), runner)


def test_summarize_video_no_transcript_placeholder():
    prompts = []

    def runner(prompt):
        prompts.append(prompt)
        return GOOD_SUMMARY

    summarize_video(make_video(transcript=None), runner)
    assert "no transcript is available" in prompts[0]


def test_summarize_video_truncates_huge_transcript():
    prompts = []

    def runner(prompt):
        prompts.append(prompt)
        return GOOD_SUMMARY

    summarize_video(make_video(transcript="x" * 500), runner, transcript_char_limit=100)
    assert "(transcript truncated)" in prompts[0]


def test_runner_exception_retried_then_succeeds():
    calls = {"n": 0}

    def runner(prompt):
        calls["n"] += 1
        if calls["n"] == 1:
            raise SummarizeError("claude -p exited 1: transient")
        return GOOD_SUMMARY

    summary = summarize_video(make_video(), runner)
    assert summary.problem
    assert calls["n"] == 2


# --- grouping ---------------------------------------------------------------------

def _summarized(n):
    return [
        (make_video(f"vid{i}"), VideoSummary(problem=f"p{i}", solution=f"s{i}", topics=["t"]))
        for i in range(1, n + 1)
    ]


def test_grouping_small_batch_skips_llm():
    def runner(prompt):
        raise AssertionError("runner must not be called for <= 2 videos")

    themes = group_into_themes(_summarized(2), runner)
    assert themes == [Theme(title=FALLBACK_THEME_TITLE, video_ids=["vid1", "vid2"])]


def test_grouping_happy_path():
    def runner(prompt):
        return json.dumps({"themes": [
            {"title": "Agents", "video_ids": ["vid1", "vid3"]},
            {"title": "Evals", "video_ids": ["vid2"]},
        ]})

    themes = group_into_themes(_summarized(3), runner)
    assert [t.title for t in themes] == ["Agents", "Evals"]
    assert themes[0].video_ids == ["vid1", "vid3"]


def test_grouping_missing_ids_repaired_into_catch_all():
    def runner(prompt):
        return json.dumps({"themes": [{"title": "Agents", "video_ids": ["vid1"]}]})

    themes = group_into_themes(_summarized(3), runner)
    assert themes == [
        Theme(title="Agents", video_ids=["vid1"]),
        Theme(title=MORE_THEME_TITLE, video_ids=["vid2", "vid3"]),
    ]


def test_grouping_duplicate_id_keeps_first_placement():
    def runner(prompt):
        return json.dumps({"themes": [
            {"title": "Agents", "video_ids": ["vid1", "vid2"]},
            {"title": "Evals", "video_ids": ["vid2", "vid3"]},
        ]})

    themes = group_into_themes(_summarized(3), runner)
    assert themes == [
        Theme(title="Agents", video_ids=["vid1", "vid2"]),
        Theme(title="Evals", video_ids=["vid3"]),
    ]


def test_grouping_unknown_ids_dropped_and_empty_theme_removed():
    def runner(prompt):
        return json.dumps({"themes": [
            {"title": "Agents", "video_ids": ["vid1", "vid2", "vid3"]},
            {"title": "Hallucinated", "video_ids": ["ghost"]},
        ]})

    themes = group_into_themes(_summarized(3), runner)
    assert themes == [Theme(title="Agents", video_ids=["vid1", "vid2", "vid3"])]


def test_grouping_all_unknown_ids_falls_back():
    def runner(prompt):
        return json.dumps({"themes": [{"title": "X", "video_ids": ["ghost1", "ghost2"]}]})

    themes = group_into_themes(_summarized(3), runner)
    assert themes == [Theme(title=FALLBACK_THEME_TITLE, video_ids=["vid1", "vid2", "vid3"])]


def test_grouping_retry_then_valid():
    replies = iter([
        "garbage",
        json.dumps({"themes": [{"title": "All", "video_ids": ["vid1", "vid2", "vid3"]}]}),
    ])

    def runner(prompt):
        return next(replies)

    themes = group_into_themes(_summarized(3), runner)
    assert themes == [Theme(title="All", video_ids=["vid1", "vid2", "vid3"])]
