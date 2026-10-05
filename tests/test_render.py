from datetime import datetime, timezone
from pathlib import Path

from digest.config import Config
from digest.diagnostics import RunContext, make_failure_report
from digest.render import DigestItem, render_email, render_empty_email, render_error_email, subject_line
from digest.summarize import Theme, VideoSummary
from digest.youtube import VideoRecord

DATE = datetime(2026, 7, 8, 6, 0, 0, tzinfo=timezone.utc)


def make_item(video_id="vid1", title="A Talk", summary=True, links=(), note=""):
    video = VideoRecord(
        video_id=video_id,
        title=title,
        url=f"https://www.youtube.com/watch?v={video_id}",
        published=DATE,
        duration=1148,
        duration_string="19:08",
        description="",
        live_status="not_live",
        transcript="t",
        transcript_source="auto",
    )
    return DigestItem(
        video=video,
        summary=VideoSummary(problem="The problem text.", solution="The solution text.",
                             topics=["agents"]) if summary else None,
        links=list(links),
        note=note,
    )


def test_subject_line():
    assert subject_line(DATE, 9) == "AI Engineer digest — Wed Jul 8 · 9 new videos"
    assert subject_line(DATE, 1) == "AI Engineer digest — Wed Jul 8 · 1 new video"


def test_render_email_contains_template_fields():
    items = {"vid1": make_item(links=["https://github.com/example/repo"])}
    themes = [Theme(title="Coding Agents", video_ids=["vid1"])]
    subject, html_body, text_body = render_email(themes, items, DATE)

    assert "1 new video" in subject
    # theme heading
    assert "Coding Agents" in html_body
    # the five per-video template fields
    assert "A Talk" in html_body
    assert "Problem</strong> — The problem text." in html_body
    assert "Solution</strong> — The solution text." in html_body
    assert "▶ Watch (19:08)" in html_body
    assert "https://github.com/example/repo" in html_body
    assert "Links from the description" in html_body
    # text alternative mirrors it
    assert "Problem:  The problem text." in text_body
    assert "Solution: The solution text." in text_body
    assert "https://www.youtube.com/watch?v=vid1" in text_body


def test_render_email_escapes_html():
    items = {"vid1": make_item(title='<script>alert("x")</script> & more')}
    themes = [Theme(title="T<h1>", video_ids=["vid1"])]
    _, html_body, _ = render_email(themes, items, DATE)
    assert "<script>" not in html_body
    assert "&lt;script&gt;" in html_body
    assert "T&lt;h1&gt;" in html_body


def test_render_email_failed_summary():
    items = {"vid1": make_item(summary=False)}
    themes = [Theme(title="Also uploaded (summary unavailable)", video_ids=["vid1"])]
    _, html_body, text_body = render_email(themes, items, DATE)
    assert "Summary unavailable" in html_body
    assert "summary unavailable" in text_body
    assert "https://www.youtube.com/watch?v=vid1" in html_body


def test_render_email_note_shown():
    items = {"vid1": make_item(note="No transcript was available")}
    themes = [Theme(title="X", video_ids=["vid1"])]
    _, html_body, text_body = render_email(themes, items, DATE)
    assert "No transcript was available" in html_body
    assert "[!] No transcript was available" in text_body


def test_render_email_skips_unknown_ids():
    items = {"vid1": make_item()}
    themes = [Theme(title="X", video_ids=["vid1", "ghost"]),
              Theme(title="Empty", video_ids=["ghost2"])]
    _, html_body, _ = render_email(themes, items, DATE)
    assert "A Talk" in html_body
    assert "Empty" not in html_body  # theme with no renderable items is dropped


def test_render_empty_email():
    subject, html_body, text_body = render_empty_email(DATE)
    assert "no new videos" in subject
    assert "No new videos" in text_body


def test_failure_email_puts_cause_and_action_before_technical_evidence():
    from digest.summarize import AuthError

    report = make_failure_report(AuthError("OAuth session expired and could not be refreshed"),
                                 RunContext(stage="Claude summaries", video="Talk (vid1)"),
                                 Config(root=Path(".")))
    subject, html_body, text_body = render_error_email(DATE, report)
    assert "FAILED: Claude login expired" in subject
    for body in (html_body, text_body):
        assert body.index("Claude login expired") < body.index("What you can do") < body.index("Technical evidence")
        assert "/login" in body
        assert "Talk (vid1)" in body
        assert "Pending videos remain eligible for retry" in body


def test_failure_email_escapes_untrusted_error_and_video_text():
    report = make_failure_report(RuntimeError('<script>alert("error")</script>'),
                                 RunContext(stage="Email rendering", video="<img src=x onerror=bad()>"),
                                 Config(root=Path(".")))
    _, html_body, text_body = render_error_email(DATE, report)
    assert "<script>" not in html_body
    assert "<img " not in html_body
    assert "&lt;script&gt;" in html_body
    assert "&lt;img " in html_body
    assert '<script>alert("error")</script>' in text_body
