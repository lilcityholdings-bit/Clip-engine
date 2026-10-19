import threading

from clip_engine import __main__ as main_mod
from clip_engine import db, pipeline
from clip_engine.config import Settings


def test_production_runs_in_background_and_posting_continues(tmp_path, monkeypatch):
    settings = Settings(data_dir=tmp_path)
    conn = db.connect(settings.db_path)
    release = threading.Event()
    started, published = [], []

    def slow_produce(c, s, claude, yt, pubs, n):
        started.append(sorted(pubs))
        release.wait(5)
        return 0
    monkeypatch.setattr(pipeline, "produce", slow_produce)
    monkeypatch.setattr(pipeline, "publish_due", lambda c, s, p: published.append(1) or 0)
    monkeypatch.setattr(pipeline, "score", lambda *a: 0)
    monkeypatch.setattr(pipeline, "refresh_views", lambda *a: None)
    monkeypatch.setattr(pipeline, "send_digest", lambda *a: 0)
    monkeypatch.setattr(main_mod, "_producer", None)

    main_mod.tick(conn, settings, None, None, {"youtube": object(), "tiktok": object()})
    first = main_mod._producer
    assert first is not None and first.is_alive()
    db.put(conn, "run_now", "1")  # a second request while still producing is held, not doubled up
    main_mod.tick(conn, settings, None, None, {"youtube": object()})
    assert main_mod._producer is first and len(published) == 2  # posting kept running
    release.set()
    first.join(5)
    assert started == [["tiktok", "youtube"]]

    main_mod.tick(conn, settings, None, None, {"youtube": object()})  # held run_now now starts
    main_mod._producer.join(5)
    assert len(started) == 2


def test_paused_does_nothing(tmp_path, monkeypatch):
    settings = Settings(data_dir=tmp_path)
    conn = db.connect(settings.db_path)
    db.put(conn, "paused", "1")
    monkeypatch.setattr(pipeline, "publish_due", lambda *a: (_ for _ in ()).throw(AssertionError("posted")))
    main_mod.tick(conn, settings, None, None, {})
