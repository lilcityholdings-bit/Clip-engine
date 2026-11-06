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
