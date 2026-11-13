import subprocess

from clip_engine import __main__ as main_mod
from clip_engine import db, pipeline
from clip_engine.config import Settings


class FakeProcess:
    def __init__(self):
        self.done = False

    def poll(self):
        return 0 if self.done else None


def _quiet(monkeypatch, published):
    monkeypatch.setattr(pipeline, "publish_due", lambda c, s, p: published.append(1) or 0)
    monkeypatch.setattr(pipeline, "score", lambda *a: 0)
    monkeypatch.setattr(pipeline, "refresh_views", lambda *a: None)
    monkeypatch.setattr(pipeline, "send_digest", lambda *a: 0)
    monkeypatch.setattr(main_mod, "_producer", None)


def test_production_runs_in_its_own_process_and_posting_continues(tmp_path, monkeypatch):
    settings = Settings(data_dir=tmp_path)
    conn = db.connect(settings.db_path)
    published, launched = [], []
    _quiet(monkeypatch, published)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    monkeypatch.setattr(pipeline, "nothing_to_do", lambda c, s: "")

    def popen(cmd, **kw):
        launched.append(cmd[1:])
        return FakeProcess()
    monkeypatch.setattr(subprocess, "Popen", popen)

    main_mod.tick(conn, settings, {"youtube": object()})
    first = main_mod._producer
    assert launched == [["-m", "clip_engine", "produce"]] and main_mod.producing()
    db.put(conn, "run_now", "1")  # a second request while still producing is held, not doubled up
    main_mod.tick(conn, settings, {"youtube": object()})
    assert main_mod._producer is first and len(launched) == 1 and len(published) == 2  # posting kept running
    first.done = True
    main_mod.tick(conn, settings, {"youtube": object()})  # held run_now now starts
    assert len(launched) == 2


def test_no_production_without_a_claude_key(tmp_path, monkeypatch):
    settings = Settings(data_dir=tmp_path)
    conn = db.connect(settings.db_path)
    published = []
    _quiet(monkeypatch, published)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: (_ for _ in ()).throw(AssertionError("produced")))
    db.put(conn, "run_now", "1")
    main_mod.tick(conn, settings, {"youtube": object()})
    assert published == [1] and not main_mod.producing()
    assert db.get(conn, "run_now") == "1"  # Make clips now still runs once the key is added


def test_keep_awake_never_raises(monkeypatch):
    def boom(*a, **k):
        raise main_mod.requests.ConnectionError("offline")
    monkeypatch.setattr(main_mod.requests, "head", boom)
    main_mod.keep_awake()


def test_paused_does_nothing(tmp_path, monkeypatch):
    settings = Settings(data_dir=tmp_path)
    conn = db.connect(settings.db_path)
    db.put(conn, "paused", "1")
    monkeypatch.setattr(pipeline, "publish_due", lambda *a: (_ for _ in ()).throw(AssertionError("posted")))
    main_mod.tick(conn, settings, {})


def test_hourly_wake_a_little_early_still_counts(tmp_path, monkeypatch):
    conn = db.connect(tmp_path / "x.db")
    now = [1_000_000.0]
    monkeypatch.setattr(main_mod.time, "time", lambda: now[0])
    assert main_mod.due(conn, "last_score", 1)
    now[0] += 3600 - 25  # the next wake-up lands 25 seconds short of the hour
    assert main_mod.due(conn, "last_score", 1)
    now[0] += 5 * 60  # but a tick a few minutes later doesn't run it twice
    assert not main_mod.due(conn, "last_score", 1)


def test_nothing_is_started_when_there_is_nothing_to_clip(tmp_path, monkeypatch):
    settings = Settings(data_dir=tmp_path)
    conn = db.connect(settings.db_path)
    _quiet(monkeypatch, [])
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    monkeypatch.delenv("CAMPAIGNS_JSON", raising=False)
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: (_ for _ in ()).throw(AssertionError("started")))
    main_mod.tick(conn, settings, {"youtube": object()})  # the 3-hourly run: silent
    assert pipeline.status(conn) == {}
    db.put(conn, "run_now", "1")
    main_mod.tick(conn, settings, {"youtube": object()})  # asked for by the owner: say why
    assert "Add a campaign first" in pipeline.status(conn)["text"] and db.get(conn, "run_now") == "0"


def test_nothing_to_do_reasons(tmp_path, monkeypatch):
    from clip_engine import campaigns, manual
    monkeypatch.delenv("CAMPAIGNS_JSON", raising=False)
    settings = Settings(data_dir=tmp_path)
    conn = db.connect(settings.db_path)
    assert "Add a campaign" in pipeline.nothing_to_do(conn, settings)
    assert pipeline.nothing_to_do(conn, Settings(data_dir=tmp_path, archive_mode=True)) == ""
    campaigns.save(tmp_path, campaigns.Campaign(id="r", name="R", program_url="https://w", rate_per_1k_views=1, sources=[]))
    assert "Upload a video" in pipeline.nothing_to_do(conn, settings)
    manual.add(conn, {"kind": "file", "ref": "/x.mp4", "campaign_id": "r"})
    assert pipeline.nothing_to_do(conn, settings) == ""
    monkeypatch.setattr(pipeline, "clips_today", lambda c: 99)
    assert "limit" in pipeline.nothing_to_do(conn, settings)


def test_make_clips_now_wakes_the_scheduler_early(tmp_path, monkeypatch):
    conn = db.connect(tmp_path / "w.db")
    monkeypatch.setattr(main_mod, "_producer", None)
    naps = []

    def nap(seconds):
        naps.append(seconds)
        if len(naps) == 3:
            db.put(conn, "run_now", "1")
    main_mod.wait_for_next_tick(conn, sleep=nap)
    assert len(naps) == 3  # woke 30 seconds in, not after 5 minutes
    naps.clear()
    db.put(conn, "paused", "1")
    main_mod.wait_for_next_tick(conn, seconds=50, sleep=naps.append)
    assert len(naps) == 5  # paused: waits the whole time


def test_stale_running_status_reads_as_interrupted(tmp_path):
    import json
    from datetime import datetime, timedelta
    conn = db.connect(tmp_path / "s.db")
    pipeline.set_status(conn, "running", "Making clips now.")
    assert pipeline.status(conn)["state"] == "running"
    old = (datetime.utcnow() - timedelta(hours=2)).isoformat()
    db.put(conn, "produce_status", json.dumps({"state": "running", "text": "x", "at": old}))
    assert pipeline.status(conn)["state"] == "failed" and "interrupted" in pipeline.status(conn)["text"]


def test_automatic_posts_are_listed_and_can_be_sent_now(tmp_path):
    from clip_engine import dashboard
    settings = Settings(data_dir=tmp_path)
    conn = db.connect(settings.db_path)
    clip = conn.execute(
        "INSERT INTO clips (source_identifier, start_sec, end_sec, title, description, tags, arms)"
        " VALUES ('s', 0, 1, 'Ring first look', 'd', '[]', '{}')").lastrowid
    post = conn.execute("INSERT INTO posts (clip_id, platform, status, scheduled_at) VALUES (?, 'youtube', 'queued',"
                        " datetime('now', '+9 hours'))", (clip,)).lastrowid
    conn.commit()
    page = dashboard._scheduled(conn, settings)
    assert "Ring first look" in page and "posts automatically" in page and "Post now" in page
    pipeline.post_now(conn, post)
    assert "posting within 5 minutes" in dashboard._scheduled(conn, settings)
    assert dashboard._scheduled(db.connect(tmp_path / "empty.db"), settings) == ""
