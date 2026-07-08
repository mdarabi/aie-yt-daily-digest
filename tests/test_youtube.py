from datetime import datetime, timezone
from pathlib import Path

from digest.youtube import (VideoRecord, extract_links, filter_boilerplate,
                            json3_to_text, parse_feed, parse_info)

FIXTURES = Path(__file__).parent / "fixtures"


def test_parse_feed():
    entries = parse_feed((FIXTURES / "feed.xml").read_bytes())
    assert [e.video_id for e in entries] == ["VIDEOID0001", "VIDEOID0002", "VIDEOID0003"]
    first = entries[0]
    assert first.title.startswith("Teaching Coding Agents")
    assert first.url == "https://www.youtube.com/watch?v=VIDEOID0001"
    assert first.published == datetime(2026, 7, 8, 9, 3, 25, tzinfo=timezone.utc)
    assert first.published.tzinfo is not None


def test_json3_to_text_joins_events_with_spaces():
    data = {
        "events": [
            {"segs": [{"utf8": "My name is Nuno and I"}]},
            {"segs": [{"utf8": "wanted to talk to you"}]},
            {"tStartMs": 100},  # event without segs (e.g. window definition)
            {"segs": [{"utf8": "about\nspreadsheets"}, {"utf8": " today"}]},
            {"segs": [{"utf8": "   "}]},  # whitespace-only event dropped
        ]
    }
    text = json3_to_text(data)
    assert text == "My name is Nuno and I wanted to talk to you about spreadsheets today"


def test_json3_to_text_empty():
    assert json3_to_text({}) == ""
    assert json3_to_text({"events": []}) == ""


def test_parse_info_builds_record():
    info = {
        "id": "VIDEOID0001",
        "title": "A Talk",
        "webpage_url": "https://www.youtube.com/watch?v=VIDEOID0001",
        "duration": 1148,
        "duration_string": "19:08",
        "description": "Slides: https://example.com/slides.",
        "live_status": "not_live",
        "upload_date": "20260708",
        "subtitles": {},
    }
    rec = parse_info(info, transcript="hello world")
    assert rec.video_id == "VIDEOID0001"
    assert rec.duration == 1148
    assert rec.transcript == "hello world"
    assert rec.transcript_source == "auto"
    assert rec.links == ["https://example.com/slides"]
    assert rec.published == datetime(2026, 7, 8, tzinfo=timezone.utc)


def test_parse_info_prefers_feed_published_and_manual_subs():
    hint = datetime(2026, 7, 8, 9, 3, 25, tzinfo=timezone.utc)
    info = {"id": "x", "title": "t", "upload_date": "20260708",
            "subtitles": {"en": [{"url": "..."}]}}
    rec = parse_info(info, transcript="text", published_hint=hint)
    assert rec.published == hint
    assert rec.transcript_source == "manual"


def test_parse_info_no_transcript():
    rec = parse_info({"id": "x", "title": "t", "upload_date": "20260708"}, transcript=None)
    assert rec.transcript is None
    assert rec.transcript_source is None


def test_extract_links_dedup_and_trailing_punctuation():
    desc = (
        "Check https://example.com/repo, and (https://docs.example.com/guide) "
        "plus https://example.com/repo again.\nAlso http://foo.bar/baz!"
    )
    assert extract_links(desc) == [
        "https://example.com/repo",
        "https://docs.example.com/guide",
        "http://foo.bar/baz",
    ]


def test_extract_links_empty():
    assert extract_links("") == []
    assert extract_links("no links here") == []


def test_filter_boilerplate_static_hosts():
    links = {"v1": ["https://ai.engineer/", "https://x.com/aiDotEngineer",
                    "https://github.com/witanlabs/research-log"]}
    result = filter_boilerplate(links)
    assert result == {"v1": ["https://github.com/witanlabs/research-log"]}


def test_filter_boilerplate_frequency():
    shared = "https://example.com/newsletter"
    links = {
        "v1": [shared, "https://a.example/one"],
        "v2": [shared, "https://b.example/two"],
        "v3": [shared + "/"],  # normalization: trailing slash still counts as the same URL
    }
    result = filter_boilerplate(links)
    assert result["v1"] == ["https://a.example/one"]
    assert result["v2"] == ["https://b.example/two"]
    assert result["v3"] == []


def test_filter_boilerplate_frequency_needs_three_videos():
    shared = "https://example.com/newsletter"
    links = {"v1": [shared], "v2": [shared]}
    result = filter_boilerplate(links)
    assert result == {"v1": [shared], "v2": [shared]}
