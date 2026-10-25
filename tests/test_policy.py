import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from clip_engine import ai, campaigns, dashboard, db, pipeline, policy
from clip_engine.config import Settings


def test_hashtags_are_specific_unique_and_capped():
    tags = ["#fyp", "#Money", "money", "#viral", "two words", "#", "#reels", "#investing", "#a1", "#b2", "#c3", "#d4"]
    assert policy.clean_hashtags(tags) == ["#Money", "#investing", "#a1", "#b2", "#c3"]
    assert pipeline.compose_description({"description": "D", "hashtags": ["#fyp", "#explore"]}, "credit") == "D\n\ncredit"


def test_watermarked_hosts():
    assert policy.watermarked("https://www.tiktok.com/@x/video/1")
    assert policy.watermarked("https://instagram.com/reel/abc")
    assert not policy.watermarked("https://www.youtube.com/@x/videos")
    assert not policy.watermarked("https://nottiktok.com/x")


def test_campaign_form_rejects_watermarked_sources():
    form = {"name": "Pod", "program_url": "https://whop.com/x", "rate": "1.5",
            "sources": "https://www.youtube.com/@pod https://www.tiktok.com/@pod"}
    with pytest.raises(campaigns.CampaignError, match="watermark"):
        campaigns.from_form(form, ["youtube"])
    form["sources"] = "https://www.youtube.com/@pod"
    assert campaigns.from_form(form, ["youtube"]).sources == ["https://www.youtube.com/@pod"]


def test_near_duplicate_titles():
    others = ["Why he sold his company for $1", "The best morning routine"]
    assert policy.near_duplicate("Why He Sold His Company For $1!", others) == others[0]
    assert policy.near_duplicate("Why she quit her job at Google", others) is None
    conn = db.connect(":memory:")
    conn.execute("INSERT INTO sources VALUES ('s', 't', 'c', 'l', 'u', 'x', NULL)")
    conn.execute("INSERT INTO clips (source_identifier, start_sec, end_sec, title, description, tags, arms)"
                 " VALUES ('s', 0, 1, 'Old title here', 'd', '[]', '{}')")
    assert policy.recent_titles(conn) == ["Old title here"]


def test_prompt_lists_recent_titles_and_asks_for_an_angle():
    client = MagicMock()
    client.beta.messages.create.return_value = SimpleNamespace(
        stop_reason="end_turn", stop_details=None,
        content=[SimpleNamespace(type="text", text=json.dumps({"clips": []}))])
    ai.pick_clips(client, [], source_title="T", fmt="short", min_sec=35, max_sec=58, title_style="question",
                  count=2, recent_titles=["Earlier title one"])
    prompt = json.dumps(client.beta.messages.create.call_args.kwargs["messages"])
    assert "Earlier title one" in prompt and "mass-produced" in prompt and "#fyp" in prompt
    assert "own voice" in prompt


def test_getting_started_points_to_the_next_step(tmp_path, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    settings = Settings(data_dir=tmp_path, youtube_refresh_token="", youtube_client_id="c", youtube_client_secret="s")
    conn = db.connect(settings.db_path)
    html = dashboard._getting_started(conn, settings)
    assert "0 of 6 done" in html and "Next: Add your Claude API key" in html
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    db.put(conn, "youtube_refresh_token", "R")
    db.put(conn, "youtube_test_result", "uploads are not locked (status: unlisted)")
    html = dashboard._getting_started(conn, settings)
    assert "3 of 6 done" in html and "Next: Turn on automatic YouTube posting" in html
    db.put(conn, "youtube_auto", "on")
    assert "Next: Add a campaign" in dashboard._getting_started(conn, settings)
