"""YouTube access: RSS discovery, yt-dlp metadata/captions, transcript + link parsing.

No YouTube Data API is used. Discovery goes through the channel's public RSS feed
(15 newest uploads, exact timestamps); when that saturates, the channel's uploads
playlist (no size cap) is walked via yt-dlp. Per-video metadata and captions come
from one yt-dlp invocation each.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
import sys
import tempfile
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)

USER_AGENT = "aie-yt-daily-digest/0.1 (personal RSS digest)"
FEED_URL = "https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"
WATCH_URL = "https://www.youtube.com/watch?v={video_id}"
PLAYLIST_URL = "https://www.youtube.com/playlist?list={playlist_id}"

_NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "yt": "http://www.youtube.com/xml/schemas/2015",
}

# Live statuses that mean "come back later": the video has no final form yet
# (post_live = stream just ended, captions still processing).
DEFER_LIVE_STATUSES = {"is_upcoming", "is_live", "post_live"}


class YouTubeError(Exception):
    pass


@dataclass
class FeedEntry:
    video_id: str
    title: str
    url: str
    published: datetime


@dataclass
class VideoRecord:
    video_id: str
    title: str
    url: str
    published: datetime
    duration: int | None
    duration_string: str
    description: str
    live_status: str
    transcript: str | None
    transcript_source: str | None  # "manual" | "auto" | None
    links: list[str] = field(default_factory=list)


# --- RSS feed ----------------------------------------------------------------

def fetch_feed(channel_id: str, timeout: int = 30) -> list[FeedEntry]:
    url = FEED_URL.format(channel_id=channel_id)
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        xml_bytes = resp.read()
    return parse_feed(xml_bytes)


def parse_feed(xml_bytes: bytes) -> list[FeedEntry]:
    root = ET.fromstring(xml_bytes)
    entries = []
    for entry in root.findall("atom:entry", _NS):
        video_id_el = entry.find("yt:videoId", _NS)
        title_el = entry.find("atom:title", _NS)
        published_el = entry.find("atom:published", _NS)
        if video_id_el is None or published_el is None:
            continue
        video_id = (video_id_el.text or "").strip()
        if not video_id:
            continue
        entries.append(FeedEntry(
            video_id=video_id,
            title=(title_el.text or "").strip() if title_el is not None else "",
            url=WATCH_URL.format(video_id=video_id),
            published=datetime.fromisoformat(published_el.text.strip()),
        ))
    return entries


# --- yt-dlp ------------------------------------------------------------------

def _run_ytdlp(args: list[str], timeout: int = 300) -> subprocess.CompletedProcess:
    cmd = [sys.executable, "-m", "yt_dlp", "--no-progress", "--no-warnings", *args]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if proc.returncode != 0:
        tail = (proc.stderr or "").strip().splitlines()[-5:]
        raise YouTubeError(f"yt-dlp failed ({proc.returncode}): " + " | ".join(tail))
    return proc


def list_uploads(playlist_id: str, start: int, end: int) -> list[str]:
    """Video ids from the channel's uploads playlist, newest first, 1-indexed inclusive."""
    proc = _run_ytdlp([
        "--flat-playlist",
        "--playlist-items", f"{start}:{end}",
        "--print", "%(id)s",
        PLAYLIST_URL.format(playlist_id=playlist_id),
    ])
    return [line.strip() for line in proc.stdout.splitlines() if line.strip()]


def fetch_video(video_id: str, published_hint: datetime | None = None,
                timeout: int = 300) -> VideoRecord:
    """Fetch metadata + English captions for one video in a single yt-dlp call."""
    url = WATCH_URL.format(video_id=video_id)
    with tempfile.TemporaryDirectory(prefix="aie-digest-") as tmp:
        tmpdir = Path(tmp)
        _run_ytdlp([
            "--skip-download",
            "--write-info-json",
            "--write-subs",
            "--write-auto-subs",
            "--sub-langs", "en.*,-live_chat",
            "--sub-format", "json3",
            "-o", str(tmpdir / "%(id)s"),
            url,
        ], timeout=timeout)

        info_path = tmpdir / f"{video_id}.info.json"
        if not info_path.exists():
            raise YouTubeError(f"yt-dlp produced no info.json for {video_id}")
        info = json.loads(info_path.read_text())

        transcript = None
        sub_path = _pick_subtitle_file(tmpdir, video_id)
        if sub_path is not None:
            transcript = json3_to_text(json.loads(sub_path.read_text()))

    return parse_info(info, transcript, published_hint)


def _pick_subtitle_file(tmpdir: Path, video_id: str) -> Path | None:
    candidates = sorted(tmpdir.glob(f"{video_id}.en*.json3"))
    if not candidates:
        return None
    for preferred in (f"{video_id}.en.json3", f"{video_id}.en-orig.json3"):
        p = tmpdir / preferred
        if p in candidates:
            return p
    return candidates[0]


def parse_info(info: dict, transcript: str | None,
               published_hint: datetime | None = None) -> VideoRecord:
    """Build a VideoRecord from a yt-dlp info dict (pure; unit-tested)."""
    video_id = info["id"]
    published = published_hint or _published_from_info(info)
    description = info.get("description") or ""
    manual_subs = info.get("subtitles") or {}
    has_manual_en = any(lang == "en" or lang.startswith("en-") for lang in manual_subs)
    return VideoRecord(
        video_id=video_id,
        title=info.get("title") or video_id,
        url=info.get("webpage_url") or WATCH_URL.format(video_id=video_id),
        published=published,
        duration=info.get("duration"),
        duration_string=info.get("duration_string") or _format_duration(info.get("duration")),
        description=description,
        live_status=info.get("live_status") or "not_live",
        transcript=transcript,
        transcript_source=("manual" if has_manual_en else "auto") if transcript else None,
        links=extract_links(description),
    )


def _published_from_info(info: dict) -> datetime:
    for key in ("release_timestamp", "timestamp"):
        if info.get(key):
            return datetime.fromtimestamp(info[key], tz=timezone.utc)
    upload_date = info.get("upload_date")  # YYYYMMDD, date-only
    if upload_date:
        return datetime.strptime(upload_date, "%Y%m%d").replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc)


def _format_duration(seconds: int | None) -> str:
    if not seconds:
        return ""
    h, rem = divmod(int(seconds), 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


# --- transcript parsing --------------------------------------------------------

def json3_to_text(data: dict) -> str:
    """Flatten a YouTube json3 caption document to plain text.

    Segments within an event concatenate directly (they carry their own spaces),
    but consecutive events don't — joining events without a separator glues words
    together ("Iwanted"). Events are joined with a space and whitespace collapsed.
    """
    parts: list[str] = []
    for event in data.get("events", []):
        segs = event.get("segs") or []
        text = "".join(seg.get("utf8", "") for seg in segs).strip()
        if text:
            parts.append(text)
    return re.sub(r"\s+", " ", " ".join(parts)).strip()


# --- description links ---------------------------------------------------------

_URL_RE = re.compile(r"https?://[^\s<>\"')\]}]+")

# The channel's own recurring promo links; anything hosted on these is never a
# talk-specific resource. Kept small — the per-run frequency filter below catches
# whatever boilerplate this list misses.
_STATIC_BLOCKLIST_HOSTS = {
    "ai.engineer",
    "www.ai.engineer",
    "twitter.com",
    "x.com",
    "www.linkedin.com",
    "linkedin.com",
}


def extract_links(description: str) -> list[str]:
    """URLs from a video description, order-preserving, deduplicated."""
    seen: set[str] = set()
    links: list[str] = []
    for match in _URL_RE.finditer(description or ""):
        url = match.group(0).rstrip(".,;:!?")
        key = _normalize_url(url)
        if key not in seen:
            seen.add(key)
            links.append(url)
    return links


def _normalize_url(url: str) -> str:
    return url.lower().rstrip("/")


def _host(url: str) -> str:
    m = re.match(r"https?://([^/]+)", url.lower())
    return m.group(1) if m else ""


def filter_boilerplate(links_by_video: dict[str, list[str]]) -> dict[str, list[str]]:
    """Drop channel boilerplate links: static blocklist hosts, plus any URL that
    appears in >= 3 videos of the same run (when the run has >= 3 videos)."""
    counts: dict[str, int] = {}
    for links in links_by_video.values():
        for url in {_normalize_url(u) for u in links}:
            counts[url] = counts.get(url, 0) + 1
    apply_frequency = len(links_by_video) >= 3

    def keep(url: str) -> bool:
        if _host(url) in _STATIC_BLOCKLIST_HOSTS:
            return False
        if apply_frequency and counts.get(_normalize_url(url), 0) >= 3:
            return False
        return True

    return {vid: [u for u in links if keep(u)] for vid, links in links_by_video.items()}
