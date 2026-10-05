"""Render the digest email (HTML + plain-text alternative).

Per-video template (user-specified):
  Title -> Problem Statement Summary -> Solution Summary -> Watch link -> Description links
Videos are grouped under theme headings.
"""

from __future__ import annotations

import html
from dataclasses import dataclass, field
from datetime import datetime

from .diagnostics import Diagnosis, FailureReport
from .summarize import Theme, VideoSummary
from .youtube import WATCH_URL, VideoRecord

_FONT = "-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif"


@dataclass
class DigestItem:
    video: VideoRecord
    summary: VideoSummary | None      # None => summarization failed
    links: list[str] = field(default_factory=list)
    note: str = ""                    # e.g. "no transcript available yet"


def subject_line(date: datetime, count: int) -> str:
    plural = "" if count == 1 else "s"
    return f"AI Engineer digest — {date:%a %b} {date.day} · {count} new video{plural}"


def render_email(themes: list[Theme], items_by_id: dict[str, DigestItem],
                 date: datetime) -> tuple[str, str, str]:
    """Returns (subject, html_body, text_body)."""
    count = len(items_by_id)
    subject = subject_line(date, count)
    return subject, _render_html(themes, items_by_id, date, count), \
        _render_text(themes, items_by_id, date, count)


def render_empty_email(date: datetime) -> tuple[str, str, str]:
    subject = f"AI Engineer digest — {date:%a %b} {date.day} · no new videos"
    body = "No new videos were uploaded to the AI Engineer channel in the last day."
    html_body = (
        f'<div style="font-family:{_FONT};font-size:15px;color:#333;">'
        f"<p>{body}</p></div>"
    )
    return subject, html_body, body


def render_error_email(date: datetime, report: FailureReport) -> tuple[str, str, str]:
    diagnosis = report.diagnosis
    subject = f"AI Engineer digest — {date:%a %b} {date.day} · run FAILED: {diagnosis.title}"
    lines = ["The daily digest run failed.", "", f"Cause: {diagnosis.title}",
             diagnosis.cause, "", f"Failing step: {report.stage}"]
    if report.video:
        lines.append(f"Affected video: {report.video}")
    lines.extend(["", "What you can do:"])
    lines.extend(f"{i}. {action}" for i, action in enumerate(diagnosis.actions, 1))
    lines.extend(["", f"Run impact: {report.impact}"])

    parts = [
        f'<div style="font-family:{_FONT};font-size:15px;line-height:1.55;'
        'color:#24292f;max-width:680px;margin:0 auto;padding:8px 16px;">',
        f'<h1 style="font-size:21px;color:#cf222e;">{html.escape(diagnosis.title)}</h1>',
        f"<p>{html.escape(diagnosis.cause)}</p>",
        f"<p><strong>Failing step:</strong> {html.escape(report.stage)}</p>",
    ]
    if report.video:
        parts.append(f"<p><strong>Affected video:</strong> {html.escape(report.video)}</p>")
    parts.extend(["<h2 style=\"font-size:17px;\">What you can do</h2>",
                  _actions_html(diagnosis),
                  f"<p><strong>Run impact:</strong> {html.escape(report.impact)}</p>"])

    if report.related:
        lines.extend(["", "Other problems found during this run:"])
        parts.append('<h2 style="font-size:17px;">Other problems found during this run</h2>')
        for problem in report.related:
            other = problem.diagnosis
            count = f"{problem.occurrences} failed request(s)"
            if problem.video_count:
                count += f" across {problem.video_count} video(s)"
            lines.extend(["", f"{other.title} — {count}", other.cause,
                          f"Step: {problem.stage}"])
            examples = [WATCH_URL.format(video_id=vid) for vid in problem.example_ids]
            if examples:
                lines.append("Example videos: " + ", ".join(examples))
            lines.extend(f"- {action}" for action in other.actions)
            lines.extend([f"Evidence: {other.evidence}"])
            parts.extend([
                f'<h3 style="font-size:15px;">{html.escape(other.title)} — {count}</h3>',
                f"<p>{html.escape(other.cause)}</p>",
                f"<p><strong>Step:</strong> {html.escape(problem.stage)}</p>",
            ])
            if examples:
                links = ", ".join(f'<a href="{html.escape(url, quote=True)}">{html.escape(url)}</a>'
                                  for url in examples)
                parts.append(f"<p><strong>Example videos:</strong> {links}</p>")
            parts.extend([_actions_html(other), _evidence_html(other.evidence)])

    lines.extend(["", "Technical evidence:", diagnosis.evidence, "",
                  "The complete traceback is in logs/digest.log on the Mac running the digest."])
    parts.extend(['<h2 style="font-size:17px;">Technical evidence</h2>',
                  _evidence_html(diagnosis.evidence),
                  "<p>The complete traceback is in <code>logs/digest.log</code> "
                  "on the Mac running the digest.</p></div>"])
    return subject, "\n".join(parts), "\n".join(lines)


def _actions_html(diagnosis: Diagnosis) -> str:
    return "<ol>" + "".join(f"<li>{html.escape(action)}</li>" for action in diagnosis.actions) + "</ol>"


def _evidence_html(evidence: str) -> str:
    return ('<pre style="background:#f6f6f6;padding:12px;border-radius:6px;'
            f'white-space:pre-wrap;word-break:break-word;">{html.escape(evidence)}</pre>')


# --- HTML ------------------------------------------------------------------------


def _render_html(themes: list[Theme], items_by_id: dict[str, DigestItem],
                 date: datetime, count: int) -> str:
    parts: list[str] = []
    parts.append(
        f'<div style="font-family:{_FONT};font-size:15px;line-height:1.55;'
        f'color:#24292f;max-width:680px;margin:0 auto;padding:8px 16px;">'
    )
    plural = "" if count == 1 else "s"
    parts.append(
        f'<h1 style="font-size:21px;margin:8px 0 2px;">AI Engineer — daily digest</h1>'
        f'<p style="margin:0 0 18px;color:#57606a;">{date:%A, %B} {date.day}, {date:%Y}'
        f" · {count} new video{plural}</p>"
    )

    for theme in themes:
        theme_items = [items_by_id[vid] for vid in theme.video_ids if vid in items_by_id]
        if not theme_items:
            continue
        parts.append(
            f'<h2 style="font-size:17px;margin:26px 0 4px;padding-bottom:5px;'
            f'border-bottom:2px solid #e1e4e8;text-transform:uppercase;'
            f'letter-spacing:0.4px;color:#1f6feb;">{html.escape(theme.title)}</h2>'
        )
        for item in theme_items:
            parts.append(_render_video_html(item))

    parts.append(
        '<p style="margin-top:30px;color:#8b949e;font-size:12px;'
        'border-top:1px solid #e1e4e8;padding-top:10px;">'
        "Generated by aie-yt-daily-digest · summaries by Claude · "
        'source: <a href="https://www.youtube.com/@aiDotEngineer/videos" '
        'style="color:#8b949e;">@aiDotEngineer</a></p>'
    )
    parts.append("</div>")
    return "\n".join(parts)


def _render_video_html(item: DigestItem) -> str:
    v = item.video
    title = html.escape(v.title)
    url = html.escape(v.url, quote=True)
    duration = f" ({v.duration_string})" if v.duration_string else ""
    out: list[str] = []
    out.append('<div style="margin:16px 0 22px;">')
    out.append(
        f'<h3 style="font-size:15.5px;margin:0 0 6px;">'
        f'<a href="{url}" style="color:#24292f;text-decoration:none;">{title}</a></h3>'
    )
    if item.note:
        out.append(
            f'<p style="margin:0 0 6px;color:#9a6700;font-size:13px;">'
            f"⚠ {html.escape(item.note)}</p>"
        )
    if item.summary is not None:
        out.append(
            f'<p style="margin:0 0 6px;"><strong style="color:#cf222e;">Problem</strong> — '
            f"{html.escape(item.summary.problem)}</p>"
        )
        out.append(
            f'<p style="margin:0 0 8px;"><strong style="color:#1a7f37;">Solution</strong> — '
            f"{html.escape(item.summary.solution)}</p>"
        )
    else:
        out.append(
            '<p style="margin:0 0 8px;color:#57606a;">Summary unavailable for this '
            "video (summarization failed) — the title and link are included so it "
            "isn't lost.</p>"
        )
    out.append(
        f'<p style="margin:0 0 4px;">'
        f'<a href="{url}" style="color:#1f6feb;font-weight:600;text-decoration:none;">'
        f"▶ Watch{html.escape(duration)}</a></p>"
    )
    if item.links:
        links_html = "".join(
            f'<li style="margin:2px 0;"><a href="{html.escape(link, quote=True)}" '
            f'style="color:#1f6feb;">{html.escape(_display_url(link))}</a></li>'
            for link in item.links
        )
        out.append(
            f'<p style="margin:6px 0 2px;color:#57606a;font-size:13px;">'
            f"Links from the description:</p>"
            f'<ul style="margin:0;padding-left:20px;font-size:13px;">{links_html}</ul>'
        )
    out.append("</div>")
    return "\n".join(out)


def _display_url(url: str) -> str:
    shown = url.split("://", 1)[-1]
    if shown.startswith("www."):
        shown = shown[4:]
    return shown if len(shown) <= 70 else shown[:67] + "…"


# --- plain text --------------------------------------------------------------------


def _render_text(themes: list[Theme], items_by_id: dict[str, DigestItem],
                 date: datetime, count: int) -> str:
    plural = "" if count == 1 else "s"
    lines: list[str] = [
        f"AI ENGINEER — DAILY DIGEST",
        f"{date:%A, %B} {date.day}, {date:%Y} · {count} new video{plural}",
        "=" * 60,
    ]
    for theme in themes:
        theme_items = [items_by_id[vid] for vid in theme.video_ids if vid in items_by_id]
        if not theme_items:
            continue
        lines.append("")
        lines.append(theme.title.upper())
        lines.append("-" * len(theme.title))
        for item in theme_items:
            v = item.video
            duration = f" ({v.duration_string})" if v.duration_string else ""
            lines.append("")
            lines.append(f"* {v.title}")
            if item.note:
                lines.append(f"  [!] {item.note}")
            if item.summary is not None:
                lines.append(f"  Problem:  {item.summary.problem}")
                lines.append(f"  Solution: {item.summary.solution}")
            else:
                lines.append("  (summary unavailable — summarization failed)")
            lines.append(f"  Watch{duration}: {v.url}")
            if item.links:
                lines.append("  Links from the description:")
                lines.extend(f"    - {link}" for link in item.links)
    lines.append("")
    lines.append("—")
    lines.append("Generated by aie-yt-daily-digest · summaries by Claude")
    return "\n".join(lines)
