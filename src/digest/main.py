"""CLI entry point and run orchestration.

A run: discover new videos (RSS, uploads-playlist fallback on saturation, plus
deferred retries) -> fetch metadata/captions -> summarize each with `claude -p`
-> group into themes -> render -> send via Resend -> commit state.

State is saved only after a fully successful run, so a crash anywhere simply
means the next run redoes the work — no video is ever silently lost.
"""

from __future__ import annotations

import argparse
import logging
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from . import youtube
from .config import Config
from .emailer import send_email
from .render import DigestItem, render_email, render_empty_email, render_error_email
from .state import State
from .summarize import (SummarizeError, Theme, group_into_themes,
                        make_claude_runner, summarize_video)
from .youtube import DEFER_LIVE_STATUSES, FeedEntry, VideoRecord, YouTubeError

log = logging.getLogger("digest")

MAX_DEEP_PAGES = 10  # 10 x 100 uploads; far beyond any realistic backlog


@dataclass
class Candidate:
    video_id: str
    published: datetime | None
    record: VideoRecord | None = None  # prefetched during deep discovery


# --- pure helpers (unit-tested) -------------------------------------------------

def compute_window_start(now: datetime, last_success: datetime | None,
                         lookback_hours: int, max_catchup_hours: int,
                         backfill_hours: int | None = None) -> datetime:
    """Start of the 'new video' window for this run.

    Normally picks up where the last successful run left off (minus 1h overlap;
    the seen-set dedups the overlap), capped at max_catchup so a machine revived
    after weeks doesn't produce a monster email.
    """
    if backfill_hours:
        return now - timedelta(hours=backfill_hours)
    if last_success is None:
        return now - timedelta(hours=lookback_hours)
    return max(last_success - timedelta(hours=1), now - timedelta(hours=max_catchup_hours))


def select_feed_candidates(feed: list[FeedEntry], seen: set[str],
                           window_start: datetime) -> list[FeedEntry]:
    return [e for e in feed if e.video_id not in seen and e.published >= window_start]


def feed_is_saturated(feed: list[FeedEntry], fresh: list[FeedEntry]) -> bool:
    """True when every feed entry is new — the 15-item RSS cap may be hiding more."""
    return len(feed) >= 15 and len(fresh) == len(feed)


# --- discovery -------------------------------------------------------------------

def discover_candidates(cfg: Config, state: State, window_start: datetime,
                        force_deep: bool = False) -> list[Candidate]:
    feed = youtube.fetch_feed(cfg.channel_id)
    log.info("RSS feed: %d entries", len(feed))
    fresh = select_feed_candidates(feed, set(state.seen), window_start)
    candidates: dict[str, Candidate] = {
        e.video_id: Candidate(e.video_id, e.published) for e in fresh
    }

    if force_deep:
        # Backfill: the window reaches far past the 15-entry feed, and recent
        # videos may already be seen — walk the whole window, skipping seen ids.
        log.info("backfill: walking uploads playlist back to %s", window_start.isoformat())
        for cand in _deep_discover(cfg, state, window_start, set(candidates), skip_seen=True):
            candidates[cand.video_id] = cand
    elif feed_is_saturated(feed, fresh):
        log.info("RSS feed saturated (all %d entries are new) — walking uploads playlist", len(feed))
        for cand in _deep_discover(cfg, state, window_start, set(candidates)):
            candidates[cand.video_id] = cand

    for vid, deferred in state.deferred.items():
        if vid not in candidates and vid not in state.seen:
            log.info("retrying deferred video %s (%s)", vid, deferred.reason)
            candidates[vid] = Candidate(vid, deferred.published)

    ordered = sorted(
        candidates.values(),
        key=lambda c: c.published or datetime.now(timezone.utc),
    )
    log.info("%d candidate video(s) to process", len(ordered))
    return ordered


def _deep_discover(cfg: Config, state: State, window_start: datetime,
                   already: set[str], skip_seen: bool = False) -> list[Candidate]:
    """Walk the uploads playlist newest-first until we cross back into known
    territory (a seen video, or one published before the window).

    With skip_seen (backfill mode), seen videos are stepped over instead of
    terminating the walk — the seen-set is then a recent stripe, not everything
    older — and only a video published before the window ends it.
    """
    found: list[Candidate] = []
    page_size = 100
    for page in range(MAX_DEEP_PAGES):
        start = page * page_size + 1
        ids = youtube.list_uploads(cfg.uploads_playlist_id, start, start + page_size - 1)
        if not ids:
            return found
        for vid in ids:
            if vid in state.seen:
                if skip_seen:
                    continue
                return found
            if vid in already:
                continue
            try:
                record = youtube.fetch_video(vid)
            except YouTubeError as exc:
                # Typically a scheduled premiere ("Premieres in N hours"), which
                # yt-dlp refuses to fetch. Defer by id so it's retried on later
                # runs — its RSS timestamp may fall outside tomorrow's window.
                log.warning("deep discovery: could not fetch %s, deferring: %s", vid, exc)
                state.defer(vid, "", f"fetch failed: {exc}"[:200], None, datetime.now(timezone.utc))
                continue
            if record.published < window_start:
                return found
            log.info("deep discovery found %s (%s)", vid, record.title)
            found.append(Candidate(vid, record.published, record))
            already.add(vid)
    log.warning("deep discovery stopped after %d pages", MAX_DEEP_PAGES)
    return found


# --- processing -------------------------------------------------------------------

def process_candidates(cfg: Config, state: State, candidates: list[Candidate],
                       now: datetime, test_mode: bool) -> list[VideoRecord]:
    """Fetch each candidate and decide: process, defer, or skip."""
    ready: list[VideoRecord] = []
    for cand in candidates:
        try:
            rec = cand.record or youtube.fetch_video(cand.video_id, cand.published)
        except YouTubeError as exc:
            log.error("could not fetch %s, deferring to next run: %s", cand.video_id, exc)
            if not test_mode:
                state.defer(cand.video_id, "", "fetch failed", cand.published, now)
            continue

        if rec.live_status in DEFER_LIVE_STATUSES:
            log.info("deferring %s: live_status=%s (%s)", rec.video_id, rec.live_status, rec.title)
            if not test_mode:
                state.defer(rec.video_id, rec.title, f"live_status={rec.live_status}",
                            rec.published, now)
            continue

        if (not cfg.include_shorts and rec.duration
                and rec.duration < cfg.shorts_max_seconds):
            log.info("skipping short %s (%ss): %s", rec.video_id, rec.duration, rec.title)
            if not test_mode:
                state.mark_seen(rec.video_id, now)
            continue

        if rec.transcript is None and not test_mode:
            age = now - rec.published
            if age < timedelta(hours=cfg.caption_grace_hours):
                log.info("deferring %s: captions not ready yet (age %.1fh): %s",
                         rec.video_id, age.total_seconds() / 3600, rec.title)
                state.defer(rec.video_id, rec.title, "captions pending", rec.published, now)
                continue
            log.warning("no captions for %s after %dh grace — summarizing from description",
                        rec.video_id, cfg.caption_grace_hours)

        ready.append(rec)
    return ready


def build_digest(cfg: Config, records: list[VideoRecord]) -> tuple[list[Theme], dict[str, DigestItem]]:
    runner = make_claude_runner(cfg)
    links_map = youtube.filter_boilerplate({r.video_id: r.links for r in records})

    items: dict[str, DigestItem] = {}
    summarized: list[tuple[VideoRecord, object]] = []
    for i, rec in enumerate(records, 1):
        log.info("summarizing %d/%d: %s", i, len(records), rec.title)
        note = ""
        if rec.transcript is None:
            note = "No transcript was available — summarized from the title and description only."
        try:
            summary = summarize_video(rec, runner, cfg.transcript_char_limit)
            summarized.append((rec, summary))
        except SummarizeError as exc:
            log.error("summarization failed for %s: %s", rec.video_id, exc)
            summary = None
        items[rec.video_id] = DigestItem(
            video=rec, summary=summary, links=links_map.get(rec.video_id, []), note=note,
        )

    failed_count = sum(1 for item in items.values() if item.summary is None)
    if failed_count >= 2 and failed_count > len(records) // 2:
        # A mostly-failed batch (usage limits, auth expiry) must not be sent:
        # sending would mark everything seen and the summaries would be lost.
        # Aborting keeps the videos unseen so the next run redoes them.
        raise SummarizeError(
            f"{failed_count}/{len(records)} summaries failed — aborting the run "
            "so these videos are retried next time"
        )

    if summarized:
        log.info("grouping %d video(s) into themes", len(summarized))
        max_themes = 6 if len(summarized) <= 20 else 12
        themes = group_into_themes(summarized, runner, max_themes)  # type: ignore[arg-type]
    else:
        themes = []

    failed_ids = [vid for vid, item in items.items() if item.summary is None]
    if failed_ids:
        themes.append(Theme(title="Also uploaded (summary unavailable)", video_ids=failed_ids))

    # Chronological order within each theme.
    for theme in themes:
        theme.video_ids.sort(key=lambda vid: items[vid].video.published)
    return themes, items


# --- run --------------------------------------------------------------------------

def run(cfg: Config, args: argparse.Namespace) -> None:
    now = datetime.now(timezone.utc)
    today = datetime.now().astimezone()
    test_mode = args.test_latest is not None
    persist = not (args.dry_run or args.no_send or test_mode)
    state = State.load(cfg.state_file)

    if test_mode:
        feed = youtube.fetch_feed(cfg.channel_id)
        candidates = [Candidate(e.video_id, e.published) for e in feed[:args.test_latest]]
        log.info("test mode: processing the %d most recent upload(s), state untouched",
                 len(candidates))
    else:
        window_start = compute_window_start(
            now, state.last_success, cfg.lookback_hours, cfg.max_catchup_hours,
            args.backfill_hours,
        )
        log.info("window start: %s (last success: %s)", window_start.isoformat(),
                 state.last_success.isoformat() if state.last_success else "never")
        candidates = discover_candidates(cfg, state, window_start,
                                         force_deep=bool(args.backfill_hours))

    records = process_candidates(cfg, state, candidates, now, test_mode)

    if not records:
        log.info("no videos ready for the digest today")
        if cfg.send_empty_digest and not args.dry_run and not args.no_send:
            subject, html_body, text_body = render_empty_email(today)
            send_email(cfg.resend_api_key, cfg.email_from, cfg.email_to,
                       subject, html_body, text_body)
        if persist:
            state.last_success = now
            state.prune(now, cfg.seen_retention_days, cfg.deferred_retention_days)
            state.save(cfg.state_file)
        return

    themes, items = build_digest(cfg, records)
    subject, html_body, text_body = render_email(themes, items, today)

    if args.dry_run:
        preview_html = cfg.root / "preview.html"
        preview_txt = cfg.root / "preview.txt"
        preview_html.write_text(html_body)
        preview_txt.write_text(f"Subject: {subject}\n\n{text_body}")
        log.info("dry run: wrote %s and %s", preview_html, preview_txt)
        print(f"\nSubject: {subject}\n\n{text_body}")
        return

    if args.no_send:
        log.info("--no-send: skipping email and state update")
        return

    send_email(cfg.resend_api_key, cfg.email_from, cfg.email_to,
               subject, html_body, text_body)
    log.info("digest sent: %s (%d videos)", subject, len(items))

    if persist:
        for vid in items:
            state.mark_seen(vid, now)
        state.last_success = now
        state.prune(now, cfg.seen_retention_days, cfg.deferred_retention_days)
        state.save(cfg.state_file)
        log.info("state saved (%d seen, %d deferred)", len(state.seen), len(state.deferred))


# --- CLI --------------------------------------------------------------------------

def _setup_logging(cfg: Config, verbose: bool) -> None:
    cfg.log_file.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        handlers=[logging.StreamHandler(sys.stderr), logging.FileHandler(cfg.log_file)],
    )
    logging.getLogger("urllib3").setLevel(logging.WARNING)


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="aie-digest",
        description="Daily email digest of new AI Engineer YouTube uploads.",
    )
    parser.add_argument("--dry-run", action="store_true",
                        help="full pipeline, but print the email and write preview.html/"
                             "preview.txt instead of sending; state is not updated")
    parser.add_argument("--no-send", action="store_true",
                        help="skip sending and state update (quieter than --dry-run)")
    parser.add_argument("--test-latest", type=int, metavar="N",
                        help="ignore state/window and process the N most recent uploads; "
                             "state is not updated")
    parser.add_argument("--backfill-hours", type=int, metavar="H",
                        help="override the discovery window to the last H hours")
    parser.add_argument("--verbose", action="store_true", help="debug logging")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    cfg = Config.load()
    _setup_logging(cfg, args.verbose)
    try:
        run(cfg, args)
        return 0
    except Exception as exc:  # noqa: BLE001 — last-resort handler for cron
        log.exception("digest run failed")
        if cfg.error_emails and cfg.resend_api_key and not args.dry_run and not args.no_send:
            try:
                subject, html_body, text_body = render_error_email(
                    datetime.now().astimezone(), f"{type(exc).__name__}: {exc}")
                send_email(cfg.resend_api_key, cfg.email_from, cfg.email_to,
                           subject, html_body, text_body)
            except Exception:
                log.exception("could not send the failure notification email")
        return 1


if __name__ == "__main__":
    sys.exit(main())
