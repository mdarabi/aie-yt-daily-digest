import subprocess
import urllib.error
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

import pytest

from digest.config import Config
from digest.diagnostics import RunContext, RunIssue, diagnose, make_failure_report
from digest.emailer import EmailError
from digest.render import render_error_email
from digest.summarize import AuthError, BatchSummarizeError, SummarizeError
from digest.youtube import YouTubeError, parse_info


def resend_error(status, detail):
    error = EmailError(f"Resend rejected the email (HTTP {status}): {detail}")
    error.__cause__ = urllib.error.HTTPError("https://api.resend.com/emails", status,
                                            "request rejected", None, None)
    return error


def video(video_id):
    return parse_info({"id": video_id, "title": video_id, "upload_date": "20261004"}, transcript="t")


@pytest.mark.parametrize("error, stage, title, action", [
    (AuthError("Failed to authenticate: OAuth session expired and could not be refreshed"),
     "Claude summaries", "Claude login expired", "/login"),
    (AuthError("Not logged in · Please run /login"),
     "Claude summaries", "Claude login unavailable", "/login"),
    (SummarizeError("CLAUDE_BIN points to a missing file: /missing/claude"),
     "Claude summaries", "Claude CLI not found", "CLAUDE_BIN"),
    (SummarizeError("5-hour limit reached"),
     "Claude summaries", "Claude usage or rate limit reached", "reset"),
    (subprocess.TimeoutExpired(["claude", "-p"], 60),
     "Claude summaries", "Claude request timed out", "CLAUDE_TIMEOUT"),
    (SummarizeError("summary JSON missing 'solution'"),
     "Claude summaries", "Claude returned an unreadable summary", "summary-parser"),
    (YouTubeError("yt-dlp failed (1): Unable to download subtitles: HTTP Error 429: Too Many Requests"),
     "YouTube video and captions", "YouTube is rate-limiting requests", "CAPTCHA"),
    (YouTubeError("yt-dlp failed (1): HTTP Error 403: Forbidden"),
     "YouTube video and captions", "YouTube denied access", "browser"),
    (urllib.error.HTTPError("https://www.youtube.com/feeds/videos.xml", 404, "Not Found", None, None),
     "YouTube channel discovery", "YouTube channel feed not found", "CHANNEL_ID"),
    (urllib.error.URLError("network unreachable"),
     "YouTube channel discovery", "YouTube could not be reached", "network"),
    (urllib.error.HTTPError("https://www.youtube.com/feeds/videos.xml", 503, "Unavailable", None, None),
     "YouTube channel discovery", "YouTube service error", "Retry later"),
    (ET.ParseError("no element found"),
     "YouTube channel discovery", "YouTube channel feed unreadable", "CHANNEL_ID"),
    (resend_error(401, "Invalid API key"),
     "Resend email delivery", "Resend API key unavailable or rejected", "RESEND_API_KEY"),
    (resend_error(403, "Sending domain is not verified"),
     "Resend email delivery", "Resend denied the email request", "sending-domain"),
    (resend_error(429, "Too many requests"),
     "Resend email delivery", "Resend rate limit reached", "reset"),
    (resend_error(503, "Temporarily unavailable"),
     "Resend email delivery", "Resend service unavailable", "Retry later"),
    (EmailError("EMAIL_TO is not set — add it to .env"),
     "Resend email delivery", "Email recipient missing", "EMAIL_TO"),
    (PermissionError("state/state.json: Permission denied"),
     "Saving run state", "Digest state could not be read or saved", "writable"),
    (RuntimeError("unexpected internal failure"),
     "Email rendering", "Unexpected digest error", "reporting"),
])
def test_diagnosis_identifies_service_and_action(error, stage, title, action):
    diagnosis = diagnose(error, stage)
    assert diagnosis.title == title
    assert action in " ".join(diagnosis.actions)
    assert str(error) in diagnosis.evidence


def test_resend_diagnosis_preserves_response_detail_from_wrapper():
    diagnosis = diagnose(resend_error(403, "Sending domain is not verified"), "Resend email delivery")
    assert "Sending domain is not verified" in diagnosis.evidence


def test_unknown_claude_error_does_not_invent_an_auth_problem():
    diagnosis = diagnose(SummarizeError("claude exited 1: unexplained service error"), "Claude summaries")
    assert "does not establish a more specific cause" in diagnosis.cause
    assert "/login" not in " ".join(diagnosis.actions)


def test_unavailable_model_is_not_misidentified_as_missing_cli():
    diagnosis = diagnose(SummarizeError("Claude model not found"), "Claude summaries")
    assert diagnosis.title == "Claude summarization failed"
    assert "Install the Claude CLI" not in " ".join(diagnosis.actions)


def test_claude_timeout_cause_survives_retry_wrapper():
    error = SummarizeError("claude call failed after 2 attempts")
    error.__cause__ = subprocess.TimeoutExpired(["claude", "-p"], 60)
    diagnosis = diagnose(error, "Claude summaries")
    assert diagnosis.title == "Claude request timed out"
    assert "60 seconds" in diagnosis.evidence


def test_batch_preserves_underlying_failures_and_distinguishes_mixed_causes():
    failures = [(video("one"), SummarizeError("5-hour limit reached")),
                (video("two"), SummarizeError("weekly limit reached"))]
    diagnosis = diagnose(BatchSummarizeError(3, failures), "Claude summaries")
    assert diagnosis.title == "Claude usage or rate limit reached"
    assert "2/3 summaries failed" in diagnosis.evidence
    assert "5-hour limit reached" in diagnosis.evidence
    assert "weekly limit reached" in diagnosis.evidence

    failures[1] = (video("two"), SummarizeError("summary JSON missing 'problem'"))
    context = RunContext(stage="Claude summaries", issues=[
        RunIssue("Claude summaries", error, rec.video_id) for rec, error in failures
    ])
    report = make_failure_report(BatchSummarizeError(3, failures), context, Config(root=Path(".")))
    assert report.diagnosis.title == "Multiple Claude summary failures"
    assert {p.diagnosis.title for p in report.related} == {
        "Claude usage or rate limit reached", "Claude returned an unreadable summary"
    }


def test_related_issues_count_requests_and_unique_videos():
    youtube_error = YouTubeError("Unable to download subtitles: HTTP Error 429: Too Many Requests")
    context = RunContext(stage="Claude summaries", issues=[
        RunIssue("YouTube video and captions", youtube_error, video)
        for video in ["one", "one", "two"]
    ])
    report = make_failure_report(AuthError("OAuth session expired"), context, Config(root=Path(".")))
    assert len(report.related) == 1
    problem = report.related[0]
    assert problem.occurrences == 3
    assert problem.video_count == 2
    assert problem.example_ids == ("one", "two")


def test_failure_evidence_redacts_credentials():
    cfg = Config(root=Path("."), resend_api_key="private-resend-key")
    error = RuntimeError("private-resend-key Bearer private-token sk-ant-private-token")
    report = make_failure_report(error, RunContext(stage="Email rendering"), cfg)
    _, html_body, text_body = render_error_email(datetime(2026, 10, 4, tzinfo=timezone.utc), report)
    for secret in ("private-resend-key", "private-token", "sk-ant-private-token"):
        assert secret not in html_body
        assert secret not in text_body
    assert "[redacted]" in text_body
