"""Explain run failures from the failing step and the original service errors."""

from __future__ import annotations

import json
import re
import subprocess
import urllib.error
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field, replace

from .config import Config
from .emailer import EmailError
from .summarize import AuthError, BatchSummarizeError, SummarizeError


@dataclass
class RunIssue:
    stage: str
    error: Exception
    video_id: str = ""


@dataclass
class RunContext:
    stage: str = "Starting the digest"
    video: str = ""
    issues: list[RunIssue] = field(default_factory=list)
    digest_sent: bool = False
    state_saved: bool = False

    def at(self, stage: str, video: str = "") -> None:
        self.stage, self.video = stage, video


@dataclass(frozen=True)
class Diagnosis:
    title: str
    cause: str
    actions: tuple[str, ...]
    evidence: str


@dataclass(frozen=True)
class RelatedProblem:
    diagnosis: Diagnosis
    stage: str
    occurrences: int
    video_count: int
    example_ids: tuple[str, ...]


@dataclass(frozen=True)
class FailureReport:
    diagnosis: Diagnosis
    stage: str
    video: str
    impact: str
    related: tuple[RelatedProblem, ...] = ()


def _chain(exc: Exception) -> list[Exception]:
    errors = []
    while exc not in errors:
        errors.append(exc)
        if exc.__cause__ is None:
            break
        exc = exc.__cause__
    return errors


def diagnose(exc: Exception, stage: str) -> Diagnosis:
    if isinstance(exc, BatchSummarizeError):
        diagnoses = [diagnose(error, stage) for _, error in exc.failures]
        if len({d.title for d in diagnoses}) == 1:
            examples = "\n".join(
                f"{video.title} ({video.video_id}): {diagnosis.evidence}"
                for (video, _), diagnosis in zip(exc.failures[:3], diagnoses[:3])
            )
            return replace(diagnoses[0], evidence=f"{exc}\n{examples}")
        return Diagnosis(
            "Multiple Claude summary failures",
            "Several videos could not be summarized for different reasons; each issue is listed below.",
            ("Address the individual issues below, then retry the digest.",), str(exc),
        )

    errors = _chain(exc)
    evidence = "\n".join(dict.fromkeys(f"{type(error).__name__}: {error}" for error in errors))
    reason = "\n".join(str(error) for error in errors).lower()
    claude = stage.startswith("Claude") or isinstance(exc, SummarizeError)
    youtube = stage.startswith("YouTube")
    email = stage.startswith("Resend") or isinstance(exc, EmailError)
    http_status = next((e.code for e in errors if isinstance(e, urllib.error.HTTPError)), None)
    timed_out = any(isinstance(e, (subprocess.TimeoutExpired, TimeoutError)) for e in errors)

    if claude:
        if isinstance(exc, AuthError):
            expired = "expired" in reason
            return Diagnosis(
                "Claude login expired" if expired else "Claude login unavailable",
                "The stored Claude login expired and could not be refreshed." if expired else
                "Claude could not authenticate with its stored subscription login.",
                ("On the Mac running the digest, run claude, enter /login, and sign in to your Claude subscription.",
                 "From the digest folder, verify with: uv run aie-digest --test-latest 1 --dry-run",
                 "If that works but the schedule still fails, re-run the scheduler installer with your existing schedule."),
                evidence,
            )
        if ("claude cli not found" in reason or "claude_bin points to a missing file" in reason
                or any(isinstance(e, FileNotFoundError) for e in errors)):
            return Diagnosis(
                "Claude CLI not found", "The digest could not find the configured Claude executable.",
                ("Install the Claude CLI if needed; set CLAUDE_BIN in .env to its existing absolute path.",
                 "Verify with: uv run aie-digest --test-latest 1 --dry-run"), evidence,
            )
        if any(marker in reason for marker in (
                "usage limit", "5-hour limit", "weekly limit", "hit your limit",
                "rate limit", "rate_limit", "too many requests")):
            return Diagnosis(
                "Claude usage or rate limit reached", "Claude refused requests because a usage or rate limit was reached.",
                ("Check the limit/reset time shown by Claude and wait for it to reset before retrying.",
                 "Then verify with: uv run aie-digest --test-latest 1 --dry-run"), evidence,
            )
        if timed_out:
            return Diagnosis(
                "Claude request timed out", "Claude did not finish within the configured time limit.",
                ("Retry once when Claude is responsive; verify with: uv run aie-digest --test-latest 1 --dry-run",
                 "If valid summaries consistently take longer, increase CLAUDE_TIMEOUT in .env."), evidence,
            )
        if any(isinstance(e, json.JSONDecodeError) for e in errors) or any(
                marker in reason for marker in ("no json object", "summary json", "reply json")):
            return Diagnosis(
                "Claude returned an unreadable summary", "Claude's response did not match the required summary format.",
                ("Retry with: uv run aie-digest --test-latest 1 --dry-run",
                 "If it repeats, include the evidence below when reporting a summary-parser issue."), evidence,
            )
        return Diagnosis(
            "Claude summarization failed", "Claude could not produce the required summaries; the error does not establish a more specific cause.",
            ("Check the service error below and verify with: uv run aie-digest --test-latest 1 --dry-run",
             "If it repeats, report this evidence and the failing video."), evidence,
        )

    if youtube:
        if http_status == 429 or re.search(r"http(?: error)?[ :]+429\b", reason) or "too many requests" in reason:
            return Diagnosis(
                "YouTube is rate-limiting requests", "YouTube rejected video or caption requests with HTTP 429 (Too Many Requests).",
                ("Allow the limit to clear before retrying; avoid repeatedly running the entire backlog immediately.",
                 "Open an affected video in a browser on this Mac and complete any CAPTCHA YouTube presents.",
                 "If automated downloads still fail, report this evidence so caption request pacing or browser-cookie support can be addressed."),
                evidence,
            )
        if http_status == 403 or "http error 403" in reason or "confirm you" in reason:
            return Diagnosis(
                "YouTube denied access", "YouTube blocked the automated request or requires a browser sign-in/check.",
                ("Open the affected channel/video in a browser on this Mac and check that it is accessible.",
                 "Complete any sign-in or CAPTCHA requirement; report the evidence if yt-dlp still cannot access it."), evidence,
            )
        if http_status == 404 and stage == "YouTube channel discovery":
            return Diagnosis(
                "YouTube channel feed not found", "YouTube returned HTTP 404 for the configured channel feed.",
                ("Check CHANNEL_ID in .env against the intended YouTube channel.",
                 "Retry after verifying that channel's RSS feed is accessible."), evidence,
            )
        if http_status is not None and http_status >= 500:
            return Diagnosis(
                "YouTube service error", f"YouTube returned a server error (HTTP {http_status}).",
                ("Retry later when YouTube's feed/video requests respond normally.",), evidence,
            )
        if timed_out or any(isinstance(e, urllib.error.URLError) and not isinstance(e, urllib.error.HTTPError)
                            for e in errors):
            return Diagnosis(
                "YouTube could not be reached", "The channel/video request timed out or failed to connect.",
                ("Check this Mac's network connection and that YouTube opens in a browser.",
                "Retry later if YouTube or the connection is temporarily unavailable."), evidence,
            )
        if stage == "YouTube channel discovery" and "yt-dlp" not in reason:
            return Diagnosis(
                "YouTube channel feed unreadable",
                "YouTube's channel feed was not valid XML." if any(isinstance(e, ET.ParseError) for e in errors)
                else "The configured channel's RSS feed could not be read; the exact reason is shown below.",
                ("Check CHANNEL_ID in .env and that the channel's RSS feed opens in a browser.",
                 "If it keeps returning an invalid response, report the evidence below."), evidence,
            )
        return Diagnosis(
            "YouTube video/feed could not be read", "YouTube or yt-dlp could not provide the required video, captions, or channel data.",
            ("Check the affected video/channel in a browser for removal, privacy, or a scheduled premiere.",
             "If it is publicly available, update yt-dlp: uv lock --upgrade-package yt-dlp && uv sync",
             "If the problem remains, report the evidence below."), evidence,
        )

    if email:
        if http_status == 401 or "resend_api_key is not set" in reason:
            return Diagnosis(
                "Resend API key unavailable or rejected", "Resend could not authenticate the digest's email request.",
                ("Set a valid RESEND_API_KEY in .env on the Mac running the digest.",), evidence,
            )
        if http_status == 403:
            return Diagnosis(
                "Resend denied the email request", "Resend rejected the request with HTTP 403; the service error below explains the restriction.",
                ("Check Resend's sending-domain verification and API-key permissions for EMAIL_FROM.",
                 "Correct the restriction named in the service error before retrying."), evidence,
            )
        if "email_to is not set" in reason:
            return Diagnosis("Email recipient missing", "No recipient was configured for the digest.",
                             ("Set EMAIL_TO in .env to the intended recipient(s).",), evidence)
        if http_status == 429:
            return Diagnosis(
                "Resend rate limit reached", "Resend rejected the email request with HTTP 429.",
                ("Wait for Resend's sending limit to reset, then retry the digest.",), evidence,
            )
        if http_status is not None and http_status >= 500:
            return Diagnosis(
                "Resend service unavailable", "Resend returned a server error after the delivery retries.",
                ("Retry later when Resend is available.",), evidence,
            )
        return Diagnosis(
            "Digest email could not be delivered", "Resend rejected the message or could not be reached; delivery is not confirmed.",
            ("Check the Resend error below and this Mac's network connection.",
             "Retry after correcting the service restriction or waiting for a temporary outage/limit to clear."), evidence,
        )

    if "saved state" in stage or "run state" in stage:
        return Diagnosis(
            "Digest state could not be read or saved", "The local state file could not be processed at this step.",
            ("Check that state/state.json and its folder are readable and writable by the account running the digest.",
             "If the evidence says the JSON is corrupt, restore state/state.json from a valid backup."), evidence,
        )
    return Diagnosis(
        "Unexpected digest error", "The error does not establish a known cause; the failing step and original error are shown below.",
        ("Include the failing step and evidence below when reporting the issue.",
         "The complete traceback is in logs/digest.log on the Mac running the digest."), evidence,
    )


def make_failure_report(exc: Exception, context: RunContext, cfg: Config) -> FailureReport:
    def safe(diagnosis: Diagnosis) -> Diagnosis:
        evidence = diagnosis.evidence
        if cfg.resend_api_key:
            evidence = evidence.replace(cfg.resend_api_key, "[redacted]")
        evidence = re.sub(r"(?i)Bearer\s+[^\s\"'<>]+", "Bearer [redacted]", evidence)
        evidence = re.sub(r"\bsk-ant-[A-Za-z0-9_-]+", "[redacted]", evidence)
        return replace(diagnosis, evidence=evidence[:1500])

    primary = safe(diagnose(exc, context.stage))
    groups: dict[tuple[str, str], list[RunIssue]] = {}
    for issue in context.issues:
        diagnosis = diagnose(issue.error, issue.stage)
        if diagnosis.title == primary.title and issue.stage == context.stage:
            continue
        groups.setdefault((diagnosis.title, issue.stage), []).append(issue)
    related = tuple(
        RelatedProblem(safe(diagnose(issues[0].error, stage)), stage, len(issues),
                       len({issue.video_id for issue in issues if issue.video_id}),
                       tuple(dict.fromkeys(issue.video_id for issue in issues if issue.video_id))[:3])
        for (_, stage), issues in groups.items()
    )
    if context.state_saved:
        impact = "The digest's state was saved before this error. Check delivery before rerunning."
    elif context.digest_sent:
        impact = "The digest email was sent, but saving its state failed. A retry may email the same videos again."
    else:
        impact = "This failed run did not advance saved state. Pending videos remain eligible for retry."
    return FailureReport(primary, context.stage, context.video, impact, related)
