import json
import random
from datetime import datetime
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

def test_short_captions_highlight_each_word_and_show_hook():
    words = [{"start": 10 + i * 0.5, "end": 10.4 + i * 0.5, "word": f" w{i}"} for i in range(7)]
    ass = media.captions_ass(words, 10.0, 14.0, "short", hook="You won't believe")
    dialogue = [l for l in ass.splitlines() if l.startswith("Dialogue")]
    assert dialogue[0] == "Dialogue: 1,0:00:00.00,0:00:03.00,Hook,You won't believe"
    words_events = dialogue[1:]
    assert len(words_events) == 7  # one event per spoken word
    # times are relative to the clip start; the first word is highlighted first
    assert words_events[0].startswith("Dialogue: 0,0:00:00.00,0:00:00.50,Default,{\\c&H0000E6FF&}W0")
    assert words_events[1].endswith("W0 {\\c&H0000E6FF&}W1{\\c&H00FFFFFF&} W2")


def test_long_captions_are_plain_lines():
    words = [{"start": i, "end": i + 0.5, "word": f" w{i}"} for i in range(10)]
    ass = media.captions_ass(words, 0, 10, "long", hook="ignored")
    dialogue = [l for l in ass.splitlines() if l.startswith("Dialogue")]
    assert len(dialogue) == 2 and dialogue[0].endswith("w0 w1 w2 w3 w4 w5 w6 w7")


def test_face_center_needs_one_steady_face():
    assert media.pick_center([0.5] * 10, 16) == 0.5
    assert media.pick_center([0.5] * 5, 16) is None             # face rarely found
    assert media.pick_center([0.2, 0.8] * 6, 16) is None        # two people far apart


def test_crop_box_stays_inside_frame():
    assert media.crop_box(1920, 1080, 0.5) == (606, 1080, 657)
    assert media.crop_box(1920, 1080, 0.0)[2] == 0
    assert media.crop_box(1920, 1080, 1.0)[2] == 1920 - 606
    assert media.crop_box(1080, 1920, 0.5) is None              # already vertical


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
        {"start": 0, "end": 40, "title": "ok", "description": "d", "tags": [], "hook_text": "h", "score": 7, "why": ""},
        {"start": 100, "end": 400, "title": "too long", "description": "d", "tags": [], "hook_text": "h", "score": 7, "why": ""},
        {"start": 200, "end": 261, "title": "a bit long", "description": "d", "tags": [], "hook_text": "h", "score": 7, "why": ""},
    ]})
    clips = ai.pick_clips(client, [{"start": 0, "end": 5, "text": "hi"}], source_title="T", fmt="short",
                          min_sec=35, max_sec=58, title_style="question", count=5)
    assert [c["title"] for c in clips] == ["ok", "a bit long"]
    assert clips[1]["end"] == 259


def test_pick_clips_drops_weak_and_sorts_best_first():
    client = _claude_returning({"clips": [
        {"start": 0, "end": 40, "title": "meh", "description": "d", "tags": [], "hook_text": "", "score": 5, "why": ""},
        {"start": 50, "end": 90, "title": "good", "description": "d", "tags": [], "hook_text": "", "score": 7, "why": ""},
        {"start": 100, "end": 140, "title": "great", "description": "d", "tags": [], "hook_text": "", "score": 9, "why": ""},
    ]})
    clips = ai.pick_clips(client, [], source_title="T", fmt="short", min_sec=35, max_sec=58,
                          title_style="number", count=5)
    assert [c["title"] for c in clips] == ["great", "good"]
    kwargs = client.beta.messages.create.call_args.kwargs
    assert kwargs["model"] == "claude-opus-5" and kwargs["fallbacks"] == "default"


def test_refusal_raises():
    client = _claude_returning({}, stop_reason="refusal")
    with pytest.raises(ai.ClaudeRefused):
        ai.trend_topics(client, ["x"], [])


def test_notify_never_raises():
    http = MagicMock()
    http.post.side_effect = RuntimeError("down")
    pipeline.notify("https://hooks.example/x", "boom", http)
    pipeline.notify("", "no webhook set")


def test_trend_topics_without_trends_uses_evergreen():
    assert ai.trend_topics(MagicMock(), [], []) == ai.EVERGREEN_TOPICS


# --- pipeline ------------------------------------------------------------

class FakePub:
    def __init__(self, name, limit=10, fail=False):
        self.name, self.daily_limit, self.fail = name, limit, fail
        self.published, self.views_by_id = [], {}

    def publish(self, clip, path, public_url):
        if self.fail:
            raise RuntimeError("platform down")
        self.published.append((clip, path, public_url))
        vid = f"{self.name}-{len(self.published)}"
        return vid, f"https://{self.name}.example/{vid}"

    def stats(self, ids):
        return {i: self.views_by_id.get(i, (0, 0)) for i in ids}


def _add_clip(conn, tmp_path, arms=None, campaign=None, scheduled="datetime('now', '-1 minute')"):
    conn.execute("INSERT OR IGNORE INTO sources VALUES ('s', 't', 'c', 'l', 'u', 'x', NULL)")
    f = tmp_path / "clip.mp4"
    f.write_bytes(b"video")
    cur = conn.execute(
        "INSERT INTO clips (source_identifier, campaign_id, start_sec, end_sec, title, description, tags, arms,"
        " file_path, media_token) VALUES ('s', ?, 0, 30, 'T', 'D', '[\"a\"]', ?, ?, 'ab' )",
        (campaign, json.dumps(arms or {"format": "short", "topic": "space"}), str(f)))
    return cur.lastrowid, f


def test_produce_respects_daily_limit(conn, tmp_path):
    settings = Settings(data_dir=tmp_path, max_clips_per_day=1)
    _add_clip(conn, tmp_path)
    claude = MagicMock()
    assert pipeline.produce(conn, settings, claude, None, {"youtube": FakePub("youtube")}, 2) == 0
    claude.beta.messages.create.assert_not_called()


def test_produce_needs_a_platform(conn, tmp_path):
    assert pipeline.produce(conn, Settings(data_dir=tmp_path), MagicMock(), None, {}, 2) == 0


def test_publish_due_posts_everywhere_then_deletes_file(conn, tmp_path):
    settings = Settings(data_dir=tmp_path, public_base_url="https://engine.example")
    clip_id, f = _add_clip(conn, tmp_path)
    for p in ("youtube", "tiktok"):
        conn.execute("INSERT INTO posts (clip_id, platform, scheduled_at) VALUES (?, ?, datetime('now', '-1 minute'))",
                     (clip_id, p))
    pubs = {"youtube": FakePub("youtube"), "tiktok": FakePub("tiktok")}
    assert pipeline.publish_due(conn, settings, pubs) == 2
    assert pubs["tiktok"].published[0][2] == "https://engine.example/media/ab.mp4"
    assert pubs["tiktok"].published[0][0]["title"] == "T"
    assert not f.exists()
    assert conn.execute("SELECT file_path FROM clips").fetchone()[0] is None


def test_publish_waits_for_scheduled_time_and_daily_limit(conn, tmp_path):
    settings = Settings(data_dir=tmp_path)
    clip_id, f = _add_clip(conn, tmp_path)
    conn.execute("INSERT INTO posts (clip_id, platform, scheduled_at) VALUES (?, 'youtube', datetime('now', '+2 hours'))",
                 (clip_id,))
    pub = FakePub("youtube")
    assert pipeline.publish_due(conn, settings, {"youtube": pub}) == 0
    assert f.exists()  # still queued, file kept
    conn.execute("UPDATE posts SET scheduled_at = datetime('now', '-1 minute')")
    pub.daily_limit = 0
    assert pipeline.publish_due(conn, settings, {"youtube": pub}) == 0


def test_publish_retries_then_gives_up(conn, tmp_path):
    settings = Settings(data_dir=tmp_path)
    clip_id, _ = _add_clip(conn, tmp_path)
    conn.execute("INSERT INTO posts (clip_id, platform, scheduled_at) VALUES (?, 'tiktok', datetime('now', '-1 minute'))",
                 (clip_id,))
    pub = FakePub("tiktok", fail=True)
    for attempt in range(pipeline.MAX_ATTEMPTS):
        pipeline.publish_due(conn, settings, {"tiktok": pub})
        conn.execute("UPDATE posts SET scheduled_at = datetime('now', '-1 minute')")
    row = conn.execute("SELECT status, attempts, error FROM posts").fetchone()
    assert (row["status"], row["attempts"]) == ("failed", pipeline.MAX_ATTEMPTS)
    assert "platform down" in row["error"]


def test_score_sums_views_across_platforms(conn, tmp_path):
    settings = Settings(data_dir=tmp_path, score_after_hours=48)
    clip_id, _ = _add_clip(conn, tmp_path)
    fresh_id, _ = _add_clip(conn, tmp_path)
    conn.executemany(
        "INSERT INTO posts (clip_id, platform, status, scheduled_at, external_id, posted_at)"
        " VALUES (?, ?, 'posted', datetime('now'), ?, ?)",
        [(clip_id, "youtube", "y1", "2020-01-01 00:00:00"), (clip_id, "tiktok", "t1", "2020-01-01 00:00:00"),
         (fresh_id, "youtube", "y2", datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"))])
    yt, tt = FakePub("youtube"), FakePub("tiktok")
    yt.views_by_id = {"y1": (500, 3)}
    tt.views_by_id = {"t1": (1500, 0)}
    assert pipeline.score(conn, settings, {"youtube": yt, "tiktok": tt}) == 1
    row = conn.execute("SELECT views, subscribers_gained FROM clips WHERE id = ?", (clip_id,)).fetchone()
    assert tuple(row) == (2000, 3)
    board = {(r["dimension"], r["arm"]): r for r in strategy.leaderboard(conn)}
    assert board[("topic", "space")]["n"] == 1
    assert pytest.approx(board[("topic", "space")]["mean"]) == strategy.reward(2000, 3)


def test_snap_to_words_removes_dead_air():
    words = [{"start": 2.0, "end": 2.4, "word": "a"}, {"start": 9.0, "end": 9.5, "word": "b"},
             {"start": 20.0, "end": 20.4, "word": "c"}]
    clip = pipeline.snap_to_words({"start": 1.0, "end": 12.0}, words)
    assert clip["start"] == pytest.approx(1.95) and clip["end"] == pytest.approx(9.75)


def test_next_slot():
    from datetime import timezone
    from clip_engine.platforms import next_slot
    now = datetime(2026, 1, 1, 15, 30, tzinfo=timezone.utc)
    assert next_slot(19, now) == datetime(2026, 1, 1, 19, 0, tzinfo=timezone.utc)
    assert next_slot(15, now) == now                      # inside the hour: post now
    assert next_slot(13, now) == datetime(2026, 1, 2, 13, 0, tzinfo=timezone.utc)


# --- campaigns -----------------------------------------------------------

from clip_engine import campaigns  # noqa: E402

CAMPAIGN = {
    "id": "pod", "name": "Pod clips", "program_url": "https://whop.com/pod",
    "rate_per_1k_views": 2.0, "sources": ["https://www.youtube.com/@pod/videos"],
    "caption_required": "@pod #pod", "rules": "No sponsor reads.",
}


def test_campaign_loading_and_expiry(tmp_path, monkeypatch):
    monkeypatch.setenv("CAMPAIGNS_JSON", json.dumps([CAMPAIGN, {**CAMPAIGN, "id": "old", "ends_on": "2000-01-01"}]))
    loaded = campaigns.load(tmp_path)
    assert [c.id for c in campaigns.live(loaded)] == ["pod"]


def test_campaign_caption_has_credit_tags_and_disclosure():
    c = campaigns.Campaign(**CAMPAIGN)
    src = sources.Source("u", "Ep 1", "Pod Host", c.program_url, "u", 3600, page="https://youtu.be/x")
    text = campaigns.caption_lines(c, src, "short")
    assert "Pod Host" in text and "https://youtu.be/x" in text
    assert "@pod #pod" in text and "#ad" in text and "#Shorts" in text


def test_campaign_reward_uses_pay_rate():
    assert strategy.reward(1000, 0, rate_per_1k=3.0) > strategy.reward(1000, 0, rate_per_1k=1.0)


def test_produce_campaign_mode_end_to_end(conn, tmp_path, monkeypatch):
    monkeypatch.setenv("CAMPAIGNS_JSON", json.dumps([{**CAMPAIGN, "platforms": ["youtube", "tiktok"]}]))
    settings = Settings(data_dir=tmp_path)
    src = sources.Source("https://youtu.be/ep1", "Ep 1", "Pod Host", CAMPAIGN["program_url"],
                         "https://youtu.be/ep1", 3600, page="https://youtu.be/ep1")
    monkeypatch.setattr(campaigns, "latest_videos", lambda url: ["https://youtu.be/ep1"])
    monkeypatch.setattr(campaigns, "describe", lambda c, url: src)
    monkeypatch.setattr(campaigns, "download", lambda url, dest: dest)
    words = [{"start": 1.0 + i, "end": 1.5 + i, "word": f" w{i}"} for i in range(40)]
    monkeypatch.setattr(media, "transcribe", lambda v, m: ([{"start": 0, "end": 50, "text": "hi"}], words))

    def fake_render(video, out, *a):
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"clip")
        return out
    monkeypatch.setattr(media, "render", fake_render)
    captured_hooks = []
    real_captions = media.captions_ass
    monkeypatch.setattr(media, "captions_ass",
                        lambda w, s, e, f, hook="": captured_hooks.append(hook) or real_captions(w, s, e, f, hook))
    claude = _claude_returning({"clips": [
        {"start": 1, "end": 37, "title": "Clip", "description": "desc", "tags": ["a"], "hook_text": "Wait for it",
         "score": 8, "why": ""}]})
    pubs = {"youtube": FakePub("youtube"), "tiktok": FakePub("tiktok"), "instagram": FakePub("instagram")}

    assert pipeline.produce(conn, settings, claude, None, pubs, 2) == 1
    assert captured_hooks == ["Wait for it"]
    assert "No sponsor reads." in claude.beta.messages.create.call_args.kwargs["messages"][0]["content"]
    clip = conn.execute("SELECT * FROM clips").fetchone()
    assert clip["campaign_id"] == "pod" and json.loads(clip["arms"])["campaign"] == "pod"
    assert "#ad" in clip["description"] and "@pod #pod" in clip["description"]
    # only the platforms the campaign pays for
    assert sorted(r[0] for r in conn.execute("SELECT platform FROM posts")) == ["tiktok", "youtube"]
    # the same video is never clipped twice
    assert pipeline.produce(conn, settings, claude, None, pubs, 2) == 0

    conn.execute("UPDATE posts SET scheduled_at = datetime('now', '-1 minute')")
    assert pipeline.publish_due(conn, settings, pubs) == 2
    conn.execute("UPDATE posts SET views = 1000")
    assert pipeline.earnings(conn, settings)[0] == {"campaign": "pod", "clips": 1, "posts": 2, "views": 2000,
                                                    "estimated_usd": 4.0}
    http = MagicMock()
    assert pipeline.send_digest(conn, "https://hooks.example/x", http) == 2
    assert "https://tiktok.example/tiktok-1" in http.post.call_args.kwargs["json"]["content"]
    assert pipeline.pending_submissions(conn) == []
