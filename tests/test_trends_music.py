import json
import subprocess
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from clip_engine import db, media, pipeline, strategy, trends

RSS = b"""<?xml version="1.0"?><rss><channel><title>Daily Search Trends</title>
<item><title>eclipse tonight</title></item><item><title>new iphone</title></item></channel></rss>"""


def test_google_trends_parses_rss():
    http = MagicMock()
    http.get.return_value = MagicMock(content=RSS, raise_for_status=lambda: None)
    assert trends.google_trends("US", http) == ["eclipse tonight", "new iphone"]
    http.get.side_effect = RuntimeError("offline")
    assert trends.google_trends("US", http) == []


def _search_response(text, stop="end_turn"):
    return SimpleNamespace(stop_reason=stop, content=[SimpleNamespace(type="text", text=text)])


def test_research_uses_web_search_and_resumes_pause(monkeypatch):
    client = MagicMock()
    brief = {"hashtags": ["#moneytok"], "phrases": ["quiet quitting"], "title_patterns": ["POV: ..."],
             "sounds": ["sped-up pop"], "notes": "Side hustles are hot."}
    client.beta.messages.create.side_effect = [
        _search_response("", stop="pause_turn"),
        _search_response("Here you go:\n```json\n" + json.dumps(brief) + "\n```"),
    ]
    assert trends.research(client, "money podcast", ["eclipse"]) == brief
    first, second = client.beta.messages.create.call_args_list
    assert first.kwargs["tools"][0]["type"] == "web_search_20260209"
    assert second.kwargs["messages"][1]["role"] == "assistant"  # paused turn sent back to resume


def test_brief_is_cached_and_survives_failures(monkeypatch):
    conn = db.connect(":memory:")
    calls = []
    monkeypatch.setattr(trends, "google_trends", lambda geo, http=None: ["x"])
    monkeypatch.setattr(trends, "research", lambda c, n, s: calls.append(n) or {"hashtags": ["#a"], "phrases": [],
                                                                                  "title_patterns": [], "sounds": [],
                                                                                  "notes": ""})
    first = trends.brief(conn, None, "Money", now=1000.0)
    assert first["hashtags"] == ["#a"] and first["google_searches"] == ["x"]
    trends.brief(conn, None, "money", now=1000.0 + 3600)          # cached, case-insensitive
    assert calls == ["Money"]
    trends.brief(conn, None, "money", now=1000.0 + 13 * 3600)     # stale → refreshed
    assert len(calls) == 2

    def boom(*a):
        raise RuntimeError("search down")
    monkeypatch.setattr(trends, "research", boom)
    assert trends.brief(conn, None, "other", now=5.0)["hashtags"] == []


def test_as_prompt():
    text = trends.as_prompt({"hashtags": ["#a", "#b"], "phrases": ["p"], "title_patterns": [], "notes": "hot",
                             "google_searches": ["g"]})
    assert "Trending hashtags: #a #b" in text and "Hot this week: hot" in text and "g" in text
    assert trends.as_prompt({}) == ""


def test_compose_description_cleans_hashtags():
    clip = {"description": "A story.", "hashtags": ["#money", "tips", "two words", "#"]}
    assert pipeline.compose_description(clip, "credit") == "A story.\n\n#money #tips\n\ncredit"


def test_music_arm_only_when_library_has_tracks():
    conn = db.connect(":memory:")
    assert strategy.plan(conn, ["t"])["music"] == "none"
    assert strategy.plan(conn, ["t"], music=["beat"])["music"] in ("none", "beat")


def test_music_library(tmp_path):
    (tmp_path / "music").mkdir()
    for name in ("a.mp3", "b.m4a", "notes.txt"):
        (tmp_path / "music" / name).write_bytes(b"x")
    assert sorted(media.music_library(tmp_path)) == ["a", "b"]
    assert media.music_library(tmp_path / "missing") == {}


@pytest.mark.skipif(subprocess.run(["which", "ffmpeg"], capture_output=True).returncode != 0, reason="needs ffmpeg")
def test_render_mixes_ducked_music_under_speech(tmp_path):
    src, bed = tmp_path / "src.mp4", tmp_path / "bed.mp3"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "color=c=black:s=640x360:r=10:d=4",
                    "-f", "lavfi", "-i", "sine=frequency=400:duration=4", "-shortest", "-c:v", "libx264",
                    "-c:a", "aac", str(src)], check=True)
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "sine=frequency=90:duration=1",
                    str(bed)], check=True)  # shorter than the clip: must loop
    out = media.render(src, tmp_path / "out" / "c.mp4", 0.5, 3.5, "long", media.captions_ass([], 0, 3, "long"),
                       music=bed)
    probe = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration:stream=codec_type",
                            "-of", "csv=p=0", str(out)], capture_output=True, text=True).stdout
    assert "audio" in probe and "video" in probe
    duration = float([line for line in probe.splitlines() if line and line[0].isdigit()][-1])
    assert 2.8 < duration < 3.3
