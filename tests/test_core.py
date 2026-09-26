import json
import random
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from clip_engine import ai, media, pipeline, sources, strategy
from clip_engine.config import Settings
from clip_engine.db import connect


@pytest.fixture
def conn():
    return connect(":memory:")


# --- licensing -----------------------------------------------------------

@pytest.mark.parametrize("url,ok", [
    ("http://creativecommons.org/licenses/by/4.0/", True),
    ("https://creativecommons.org/licenses/by-sa/3.0/", True),
    ("http://creativecommons.org/publicdomain/zero/1.0/", True),
    ("http://creativecommons.org/publicdomain/mark/1.0/", True),
    ("http://creativecommons.org/licenses/by-nc/4.0/", False),
    ("http://creativecommons.org/licenses/by-nd/4.0/", False),
    ("http://creativecommons.org/licenses/by-nc-sa/4.0/", False),
    ("http://creativecommons.org/licenses/publicdomain/", True),
    ("", False),
    (None, False),
])
def test_license_allowed(url, ok):
    assert sources.license_allowed(url) is ok


def _meta_response(license_url, files):
    resp = MagicMock()
    resp.json.return_value = {"metadata": {"title": "Talk", "creator": ["A", "B"], "licenseurl": license_url},
                              "files": files}
    return resp


def test_fetch_picks_largest_mp4_under_cap_and_rejects_short():
    http = MagicMock()
    http.get.return_value = _meta_response("http://creativecommons.org/licenses/by/4.0/", [
        {"name": "small.mp4", "size": "1000", "length": "1800"},
        {"name": "big.mp4", "size": "5000", "length": "30:00"},
        {"name": "huge.mp4", "size": str(sources.MAX_DOWNLOAD_BYTES + 1), "length": "1800"},
    ])
    src = sources.fetch("item1", session=http)
    assert src.video_url.endswith("/item1/big.mp4")
    assert src.creator == "A, B"
    assert src.duration == 1800

    http.get.return_value = _meta_response("http://creativecommons.org/licenses/by/4.0/", [
        {"name": "clip.mp4", "size": "10", "length": "120"}])
    assert sources.fetch("item2", session=http) is None


def test_fetch_rejects_noncommercial():
    http = MagicMock()
    http.get.return_value = _meta_response("http://creativecommons.org/licenses/by-nc/4.0/", [
        {"name": "a.mp4", "size": "10", "length": "3600"}])
    assert sources.fetch("item", session=http) is None


def test_attribution_includes_source_and_license():
    src = sources.Source("id1", "Great Talk", "Jane", "https://creativecommons.org/licenses/by-sa/4.0/",
                         "https://archive.org/download/id1/a.mp4", 3600)
    text = pipeline.attribution(src, "short")
    assert "Jane" in text and "https://archive.org/details/id1" in text
    assert "CC BY-SA" in text and "same license" in text and "#Shorts" in text


# --- strategy ------------------------------------------------------------

def test_plan_is_consistent(conn):
    arms = strategy.plan(conn, ["space"], random.Random(1))
    assert arms["length"].startswith(arms["format"] + ":")
    assert arms["topic"] == "space"
    lo, hi = strategy.length_range(arms)
    assert lo < hi and (arms["format"] != "short" or hi < 60)


def test_bandit_learns_best_arm(conn):
    rng = random.Random(0)
    for _ in range(30):
        strategy.record(conn, json.dumps({"title_style": "question"}), strategy.reward(10000, 50))
        strategy.record(conn, json.dumps({"title_style": "number"}), strategy.reward(50, 0))
    picks = [strategy.choose(conn, "title_style", ["question", "number"], rng) for _ in range(200)]
    assert picks.count("question") > 190


def test_reward_values_subscribers():
    assert strategy.reward(100, 2) > strategy.reward(150, 0)
    assert strategy.reward(0, 0) == 0


# --- captions ------------------------------------------------------------

def test_captions_are_relative_to_clip_start():
    words = [{"start": 10 + i * 0.5, "end": 10.4 + i * 0.5, "word": f" w{i}"} for i in range(7)]
    ass = media.captions_ass(words, 10.0, 14.0, "short")
    dialogue = [l for l in ass.splitlines() if l.startswith("Dialogue")]
    assert len(dialogue) == 3
    assert dialogue[0].startswith("Dialogue: 0,0:00:00.00,0:00:01.40,Default,W0 W1 W2")


# --- Claude calls --------------------------------------------------------

def _claude_returning(payload, stop_reason="end_turn"):
    client = MagicMock()
    client.beta.messages.create.return_value = SimpleNamespace(
        stop_reason=stop_reason, stop_details=None,
        content=[SimpleNamespace(type="text", text=json.dumps(payload))],
    )
    return client


def test_pick_clips_filters_bad_lengths_and_caps_shorts():
    client = _claude_returning({"clips": [
        {"start": 0, "end": 40, "title": "ok", "description": "d", "tags": [], "why": ""},
        {"start": 100, "end": 400, "title": "too long", "description": "d", "tags": [], "why": ""},
        {"start": 200, "end": 261, "title": "a bit long", "description": "d", "tags": [], "why": ""},
    ]})
    clips = ai.pick_clips(client, [{"start": 0, "end": 5, "text": "hi"}], source_title="T", fmt="short",
                          min_sec=35, max_sec=58, title_style="question", count=5)
    assert [c["title"] for c in clips] == ["ok", "a bit long"]
    assert clips[1]["end"] == 259
    kwargs = client.beta.messages.create.call_args.kwargs
    assert kwargs["model"] == "claude-opus-5" and kwargs["fallbacks"] == "default"


def test_refusal_raises():
    client = _claude_returning({}, stop_reason="refusal")
    with pytest.raises(ai.ClaudeRefused):
        ai.trend_topics(client, ["x"], [])


def test_trend_topics_without_trends_uses_evergreen():
    assert ai.trend_topics(MagicMock(), [], []) == ai.EVERGREEN_TOPICS


# --- pipeline ------------------------------------------------------------

def test_produce_respects_daily_limit(conn, tmp_path):
    settings = Settings(data_dir=tmp_path, max_uploads_per_day=1)
    conn.execute("INSERT INTO sources VALUES ('s', 't', 'c', 'l', 'u', 'x', NULL)")
    conn.execute("INSERT INTO uploads (video_id, source_identifier, start_sec, end_sec, title, arms)"
                 " VALUES ('v', 's', 0, 1, 't', '{}')")
    yt, claude = MagicMock(), MagicMock()
    assert pipeline.produce(conn, settings, claude, yt, 2) == 0
    yt.upload.assert_not_called()


def test_score_updates_strategy(conn, tmp_path):
    settings = Settings(data_dir=tmp_path, score_after_hours=48)
    conn.execute("INSERT INTO sources VALUES ('s', 't', 'c', 'l', 'u', 'x', NULL)")
    arms = json.dumps({"format": "short", "topic": "space"})
    conn.execute("INSERT INTO uploads (video_id, source_identifier, start_sec, end_sec, title, arms, uploaded_at)"
                 " VALUES ('old', 's', 0, 1, 't', ?, datetime('now', '-3 days'))", (arms,))
    conn.execute("INSERT INTO uploads (video_id, source_identifier, start_sec, end_sec, title, arms)"
                 " VALUES ('new', 's', 0, 1, 't', ?)", (arms,))
    yt = MagicMock()
    yt.performance.return_value = {"old": (500, 3)}
    assert pipeline.score(conn, settings, yt) == 1
    assert yt.performance.call_args.args[0] == ["old"]
    board = {(r["dimension"], r["arm"]): r for r in strategy.leaderboard(conn)}
    assert board[("topic", "space")]["n"] == 1
    row = conn.execute("SELECT views, subscribers_gained FROM uploads WHERE video_id='old'").fetchone()
    assert tuple(row) == (500, 3)
