import base64
import threading
import time
import urllib.error
import urllib.request
from unittest.mock import MagicMock

import pytest

from clip_engine import dashboard, db, platforms
from clip_engine.config import Settings


def _resp(payload, status=200):
    r = MagicMock()
    r.json.return_value = payload
    r.status_code = status
    return r


@pytest.fixture
def conn():
    return db.connect(":memory:")


CLIP = {"title": "Big moment", "description": "desc\n\n#ad", "tags": "[]", "campaign_id": "pod"}


def test_tiktok_chunking():
    assert platforms.TikTokPublisher.chunking(20_000_000) == (20_000_000, 1)
    size = 150 * 1024 * 1024
    chunk, count = platforms.TikTokPublisher.chunking(size)
    assert chunk == 10 * 1024 * 1024 and count == 15  # last chunk takes the remainder


def test_tiktok_publish_flow(conn, tmp_path, monkeypatch):
    monkeypatch.setattr(platforms.time, "sleep", lambda s: None)
    video = tmp_path / "c.mp4"
    video.write_bytes(b"x" * 1000)
    http = MagicMock()
    http.post.side_effect = [
        _resp({"access_token": "AT", "expires_in": 86400, "refresh_token": "RT2"}),
        _resp({"data": {"publish_id": "p1", "upload_url": "https://upload.example"}, "error": {"code": "ok"}}),
        _resp({"data": {"status": "PROCESSING_UPLOAD"}, "error": {"code": "ok"}}),
        _resp({"data": {"status": "PUBLISH_COMPLETE", "publicaly_available_post_id": [7123]}, "error": {"code": "ok"}}),
    ]
    http.put.return_value = _resp({}, 201)
    pub = platforms.TikTokPublisher(conn, "key", "secret", "RT1", "PUBLIC_TO_EVERYONE", 15, http=http)

    assert pub.publish(CLIP, video, "") == ("7123", "https://www.tiktok.com/video/7123")
    init = http.post.call_args_list[1].kwargs["json"]
    assert init["source_info"] == {"source": "FILE_UPLOAD", "video_size": 1000, "chunk_size": 1000,
                                   "total_chunk_count": 1}
    assert init["post_info"]["brand_content_toggle"] is True  # paid campaign clip
    assert http.put.call_args.kwargs["headers"]["Content-Range"] == "bytes 0-999/1000"
    # rotated refresh token is kept for next time
    assert db.get(conn, "tiktok_refresh_token") == "RT2"


def test_tiktok_error_raises(conn, tmp_path):
    video = tmp_path / "c.mp4"
    video.write_bytes(b"x")
    db.put(conn, "tiktok_access_token", "AT")
    db.put(conn, "tiktok_access_expires", str(time.time() + 9999))
    http = MagicMock()
    http.post.return_value = _resp({"error": {"code": "unaudited_client_can_only_post_to_private_accounts",
                                              "message": "private only"}})
    pub = platforms.TikTokPublisher(conn, "k", "s", "r", "PUBLIC_TO_EVERYONE", 15, http=http)
    with pytest.raises(platforms.PublishError, match="unaudited"):
        pub.publish(CLIP, video, "")


def test_instagram_publish_flow(conn, tmp_path, monkeypatch):
    monkeypatch.setattr(platforms.time, "sleep", lambda s: None)
    db.put(conn, "instagram_token_refreshed", str(time.time()))
    http = MagicMock()
    http.request.side_effect = [
        _resp({"id": "container1"}),
        _resp({"status_code": "IN_PROGRESS"}),
        _resp({"status_code": "FINISHED"}),
        _resp({"id": "media9"}),
        _resp({"permalink": "https://instagram.com/reel/abc"}),
    ]
    pub = platforms.InstagramPublisher(conn, "ig123", "TOKEN", 25, http=http)
    assert pub.publish(CLIP, tmp_path / "c.mp4", "https://engine.example/media/x.mp4") == (
        "media9", "https://instagram.com/reel/abc")
    create = http.request.call_args_list[0]
    assert create.args[1].endswith("/ig123/media")
    assert create.kwargs["params"]["media_type"] == "REELS"
    assert create.kwargs["params"]["video_url"] == "https://engine.example/media/x.mp4"


def test_instagram_needs_public_url(conn, tmp_path):
    pub = platforms.InstagramPublisher(conn, "ig", "t", 25, http=MagicMock())
    with pytest.raises(platforms.PublishError, match="PUBLIC_BASE_URL"):
        pub.publish(CLIP, tmp_path / "c.mp4", "")


def test_enabled_depends_on_credentials(conn, monkeypatch):
    for k in ("TIKTOK_CLIENT_KEY", "TIKTOK_REFRESH_TOKEN", "INSTAGRAM_USER_ID", "INSTAGRAM_ACCESS_TOKEN"):
        monkeypatch.delenv(k, raising=False)
    def auto(pubs):
        return [n for n, p in pubs.items() if not platforms.is_manual(p)]
    pubs = platforms.enabled(Settings(youtube_refresh_token=""), conn, None)
    assert auto(pubs) == [] and sorted(pubs) == ["instagram", "tiktok", "youtube"]  # all posted from the phone
    monkeypatch.setenv("TIKTOK_CLIENT_KEY", "k")
    monkeypatch.setenv("TIKTOK_REFRESH_TOKEN", "r")
    # Connected but not switched on: YouTube stays a phone platform until Google approves the project.
    assert auto(platforms.enabled(Settings(youtube_refresh_token="x"), conn, MagicMock())) == ["tiktok"]
    db.put(conn, "youtube_auto", "on")
    assert auto(platforms.enabled(Settings(youtube_refresh_token="x"), conn, MagicMock())) == ["youtube", "tiktok"]


# --- dashboard -------------------------------------------------------------

@pytest.fixture
def server(tmp_path):
    settings = Settings(data_dir=tmp_path, dashboard_password="s3cret", port=0)
    conn = db.connect(settings.db_path)
    conn.execute("INSERT INTO sources VALUES ('s', 't', 'c', 'l', 'u', 'x', NULL)")
    conn.execute("INSERT INTO clips (source_identifier, start_sec, end_sec, title, description, tags, arms)"
                 " VALUES ('s', 0, 1, '<script>alert(1)</script>', 'd', '[]', '{}')")
    conn.commit()
    (tmp_path / "clips").mkdir()
    (tmp_path / "clips" / ("a" * 32 + ".mp4")).write_bytes(b"MP4DATA")
    from http.server import ThreadingHTTPServer
    srv = ThreadingHTTPServer(("127.0.0.1", 0), dashboard.make_handler(settings))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}", conn
    srv.shutdown()


def _get(url, password=None, data=None, origin="same"):
    req = urllib.request.Request(url, data=data, method="POST" if data is not None else "GET")
    if data is not None and origin:
        host = url.split("/")[2]
        req.add_header("Origin", f"http://{host}" if origin == "same" else origin)
    if password:
        req.add_header("Authorization", "Basic " + base64.b64encode(f"me:{password}".encode()).decode())
    return urllib.request.urlopen(req, timeout=5)


def test_dashboard_requires_password_and_escapes(server):
    base, _ = server
    with pytest.raises(urllib.error.HTTPError) as err:
        _get(base + "/")
    assert err.value.code == 401
    with pytest.raises(urllib.error.HTTPError):
        _get(base + "/", "wrong")
    page = _get(base + "/", "s3cret").read().decode()
    assert "Clip Engine" in page and "&lt;script&gt;" in page and "<script>" not in page


def test_media_is_public_but_only_by_token(server):
    base, _ = server
    assert _get(base + "/media/" + "a" * 32 + ".mp4").read() == b"MP4DATA"
    for bad in ("/media/../clip_engine.db", "/media/" + "b" * 32 + ".mp4"):
        with pytest.raises(urllib.error.HTTPError):
            _get(base + bad)
    assert _get(base + "/health").read() == b"ok"


def test_pause_and_run_buttons(server):
    base, conn = server
    _get(base + "/pause", "s3cret", data=b"")
    assert db.get(db.connect(conn.execute("PRAGMA database_list").fetchone()[2]), "paused") == "1"
    _get(base + "/run", "s3cret", data=b"")
    fresh = db.connect(conn.execute("PRAGMA database_list").fetchone()[2])
    assert db.get(fresh, "run_now") == "1"
    with pytest.raises(urllib.error.HTTPError):
        _get(base + "/resume", None, data=b"")


def test_dashboard_disabled_without_password(tmp_path):
    handler = dashboard.make_handler(Settings(data_dir=tmp_path, dashboard_password=""))
    from http.server import ThreadingHTTPServer
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        with pytest.raises(urllib.error.HTTPError) as err:
            _get(f"http://127.0.0.1:{srv.server_address[1]}/", "")
        assert err.value.code == 401
    finally:
        srv.shutdown()



def test_ayrshare_publish_and_stats(tmp_path):
    http = MagicMock()
    http.post.side_effect = [
        _resp({"status": "success", "id": "ayr1",
               "postIds": [{"platform": "tiktok", "status": "success", "postUrl": "https://tiktok.com/@me/video/1"}]}),
        _resp({"status": "success", "tiktok": {"analytics": {"videoViews": 4321}}}),
    ]
    pub = platforms.AyrsharePublisher("tiktok", "KEY", 15, http=http)
    assert pub.publish(CLIP, tmp_path / "c.mp4", "https://engine.example/media/x.mp4") == (
        "ayr1", "https://tiktok.com/@me/video/1")
    body = http.post.call_args_list[0].kwargs["json"]
    assert body["platforms"] == ["tiktok"] and body["mediaUrls"] == ["https://engine.example/media/x.mp4"]
    assert body["tikTokOptions"]["isBrandedContent"] is True
    assert http.post.call_args_list[0].kwargs["headers"]["Authorization"] == "Bearer KEY"
    assert pub.stats(["ayr1"]) == {"ayr1": (4321, 0)}


def test_ayrshare_youtube_short_and_errors(tmp_path):
    http = MagicMock()
    http.post.return_value = _resp({"status": "error", "errors": [{"message": "not linked"}]})
    pub = platforms.AyrsharePublisher("youtube", "KEY", 5, http=http)
    with pytest.raises(platforms.PublishError, match="not linked"):
        pub.publish({**CLIP, "tags": '["ab", "x"]'}, tmp_path / "c.mp4", "https://e.example/m.mp4")
    yt = http.post.call_args.kwargs["json"]["youTubeOptions"]
    assert yt == {"title": "Big moment", "visibility": "public", "shorts": True, "tags": ["ab"], "madeForKids": False}


def test_ayrshare_fills_platforms_not_connected_directly(conn, monkeypatch):
    for k in ("TIKTOK_CLIENT_KEY", "TIKTOK_REFRESH_TOKEN", "INSTAGRAM_USER_ID", "INSTAGRAM_ACCESS_TOKEN"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("AYRSHARE_API_KEY", "KEY")
    pubs = platforms.enabled(Settings(youtube_refresh_token=""), conn, None)
    assert sorted(pubs) == ["instagram", "tiktok", "youtube"]
    assert all(isinstance(p, platforms.AyrsharePublisher) for p in pubs.values())


def test_dashboard_shows_trends(tmp_path):
    settings = Settings(data_dir=tmp_path)
    conn = db.connect(settings.db_path)
    db.put(conn, "trends:money podcast", '{"at": 1, "brief": {"hashtags": ["#moneytok"], "phrases": [], '
                                         '"title_patterns": [], "sounds": ["sped-up pop <b>"], "notes": "hot"}}')
    page = dashboard.render(conn, settings)
    assert "money podcast" in page and "#moneytok" in page and "sped-up pop &lt;b&gt;" in page


# --- campaign form ---------------------------------------------------------

from urllib.parse import urlencode  # noqa: E402

from clip_engine import campaigns  # noqa: E402

FORM = {"name": "Deep Talk Podcast", "program_url": "https://whop.com/deep-talk", "rate": "$1.50",
        "sources": "https://www.youtube.com/@deeptalk/videos\nhttps://drive.google.com/file/d/abc",
        "caption_required": "@deeptalk #deeptalk", "rules": "No sponsor reads.", "niche": "money podcast",
        "allow_music": "on", "active": "on", "ends_on": ""}


def _post_form(base, path, fields, platforms=("youtube", "tiktok")):
    body = urlencode(list(fields.items()) + [("platforms", p) for p in platforms]).encode()
    return _get(base + path, "s3cret", data=body)


def test_add_edit_pause_delete_campaign(server, tmp_path, monkeypatch):
    monkeypatch.delenv("CAMPAIGNS_JSON", raising=False)
    base, _ = server
    page = _post_form(base, "/campaigns/save", FORM).read().decode()  # follows the redirect
    assert "Campaign Deep Talk Podcast added" in page
    [c] = campaigns.load(tmp_path)
    assert (c.id, c.rate_per_1k_views, c.platforms) == ("deep-talk-podcast", 1.5, ["youtube", "tiktok"])
    assert c.sources == ["https://www.youtube.com/@deeptalk/videos", "https://drive.google.com/file/d/abc"]
    assert c.allow_music and c.active and c.niche == "money podcast"

    edit = _get(base + "/?edit=deep-talk-podcast", "s3cret").read().decode()
    assert "Edit campaign" in edit and 'value="1.5"' in edit and "No sponsor reads." in edit

    _post_form(base, "/campaigns/save", {**FORM, "id": "deep-talk-podcast", "rate": "2", "allow_music": "off"},
               platforms=("instagram",))
    [c] = campaigns.load(tmp_path)
    assert (c.rate_per_1k_views, c.platforms, c.allow_music) == (2.0, ["instagram"], False)

    _get(base + "/campaigns/toggle", "s3cret", data=b"id=deep-talk-podcast")
    assert campaigns.load(tmp_path)[0].active is False
    assert campaigns.live(campaigns.load(tmp_path)) == []
    _get(base + "/campaigns/delete", "s3cret", data=b"id=deep-talk-podcast")
    assert campaigns.load(tmp_path) == []


@pytest.mark.parametrize("change,message", [
    ({"name": ""}, "name"),
    ({"program_url": "whop.com/x"}, "campaign's page link"),
    ({"rate": "lots"}, "must be a number"),
    ({"rate": "0"}, "more than 0"),
    ({"sources": "not a link"}, "creator's channel"),
    ({"ends_on": "next week"}, "2026-12-31"),
])
def test_form_errors_keep_what_was_typed(server, tmp_path, change, message):
    base, _ = server
    page = _post_form(base, "/campaigns/save", {**FORM, **change}).read().decode()
    assert message in html_unescape(page)
    assert campaigns.load(tmp_path) == []
    if "rules" not in change:
        assert "No sponsor reads." in page  # the form is refilled


def test_form_needs_a_platform(server, tmp_path):
    base, _ = server
    page = _post_form(base, "/campaigns/save", FORM, platforms=()).read().decode()
    assert "at least one platform" in page


def test_posts_from_other_sites_are_rejected(server, tmp_path):
    base, _ = server
    for origin in ("https://evil.example", None):
        with pytest.raises(urllib.error.HTTPError) as err:
            _get(base + "/campaigns/save", "s3cret", data=urlencode(FORM).encode(), origin=origin)
        assert err.value.code == 403
    assert campaigns.load(tmp_path) == []


def test_env_campaigns_show_but_cannot_be_deleted(server, tmp_path, monkeypatch):
    monkeypatch.setenv("CAMPAIGNS_JSON", '[{"id": "envpod", "name": "Env Pod", "program_url": "https://w.example",'
                                         ' "rate_per_1k_views": 1, "sources": ["https://y.example"]}]')
    base, _ = server
    page = _get(base + "/", "s3cret").read().decode()
    assert "Env Pod" in page and "/campaigns/delete" not in page
    with pytest.raises(urllib.error.HTTPError):
        _get(base + "/campaigns/delete", "s3cret", data=b"id=envpod")
    assert [c.id for c in campaigns.load(tmp_path)] == ["envpod"]


def html_unescape(text):
    import html
    return html.unescape(text)


# --- review mode -----------------------------------------------------------

def _queue_for_review(conn, tmp_path, token="c" * 32):
    db.put(conn, "auto_platforms", "youtube,tiktok")
    (tmp_path / "clips" / f"{token}.mp4").write_bytes(b"0123456789")
    cid = conn.execute(
        "INSERT INTO clips (source_identifier, campaign_id, start_sec, end_sec, title, description, tags, arms,"
        " file_path, media_token, ai_score, hook_text) VALUES ('s', 'pod', 0, 30, 'Big reveal', 'desc #ad', '[]',"
        " '{}', ?, ?, 8, 'wait')", (str(tmp_path / "clips" / f"{token}.mp4"), token)).lastrowid
    for p in ("youtube", "tiktok"):
        conn.execute("INSERT INTO posts (clip_id, platform, status, scheduled_at) VALUES (?, ?, 'review',"
                     " datetime('now'))", (cid, p))
    conn.commit()
    return cid


def test_review_card_approve_and_reject(server, tmp_path):
    base, conn = server
    first = _queue_for_review(conn, tmp_path)
    second = _queue_for_review(conn, tmp_path, token="d" * 32)
    page = _get(base + "/", "s3cret").read().decode()
    assert "Waiting for your approval (2)" in page and "Big reveal" in page
    assert f'src="/media/{"c" * 32}.mp4"' in page and "posts to YouTube, TikTok" in page and "On-screen hook: <b>wait</b>" in page

    _get(base + "/review/approve", "s3cret", data=f"id={first}".encode())
    _get(base + "/review/reject", "s3cret", data=f"id={second}".encode())
    rows = dict(conn.execute("SELECT clip_id, GROUP_CONCAT(DISTINCT status) FROM posts GROUP BY clip_id").fetchall())
    assert rows == {first: "queued", second: "rejected"}
    assert not (tmp_path / "clips" / ("d" * 32 + ".mp4")).exists()
    assert "Waiting for your approval" not in _get(base + "/", "s3cret").read().decode()


def test_approve_all(server, tmp_path):
    base, conn = server
    _queue_for_review(conn, tmp_path)
    _queue_for_review(conn, tmp_path, token="d" * 32)
    _get(base + "/review/approve-all", "s3cret", data=b"")
    assert {r[0] for r in conn.execute("SELECT status FROM posts")} == {"queued"}


def test_settings_switches(server, tmp_path):
    base, conn = server
    page = _get(base + "/", "s3cret").read().decode()
    assert "Turn off (post automatically)" in page and "up to 4 clips today" in page
    _get(base + "/settings/review", "s3cret", data=b"")
    _get(base + "/settings/ramp", "s3cret", data=b"")
    fresh = db.connect(tmp_path / "clip_engine.db")
    assert (db.get(fresh, "review_mode"), db.get(fresh, "ramp")) == ("off", "off")


def test_media_supports_byte_ranges(server, tmp_path):
    base, _ = server
    url = base + "/media/" + "a" * 32 + ".mp4"  # contains b"MP4DATA"
    for header, status, body, content_range in (
            ("bytes=0-1", 206, b"MP", "bytes 0-1/7"),
            ("bytes=3-", 206, b"DATA", "bytes 3-6/7"),
            ("bytes=-2", 206, b"TA", "bytes 5-6/7")):
        req = urllib.request.Request(url, headers={"Range": header})
        resp = urllib.request.urlopen(req, timeout=5)
        assert (resp.status, resp.read(), resp.headers["Content-Range"]) == (status, body, content_range)
    with pytest.raises(urllib.error.HTTPError) as err:
        urllib.request.urlopen(urllib.request.Request(url, headers={"Range": "bytes=50-60"}), timeout=5)
    assert err.value.code == 416
    full = urllib.request.urlopen(url, timeout=5)
    assert full.status == 200 and full.headers["Accept-Ranges"] == "bytes"


# --- phone posting ---------------------------------------------------------

def test_phone_posting_flow(server, tmp_path):
    base, conn = server
    cid = _queue_for_review(conn, tmp_path)
    db.put(conn, "auto_platforms", "")  # nothing connected: everything is posted from the phone
    _get(base + "/review/approve", "s3cret", data=f"id={cid}".encode())
    assert {r[0] for r in conn.execute("SELECT status FROM posts")} == {"ready"}

    page = _get(base + "/", "s3cret").read().decode()
    assert "Ready to post from your phone (1)" in page
    assert f'href="/media/{"c" * 32}.mp4?download=1"' in page and "Copy caption" in page
    assert "Paste your YouTube post link" in page and "Paste your TikTok post link" in page
    assert "youtube: from your phone" in page.lower()

    yt_post, tt_post = [r[0] for r in conn.execute("SELECT id FROM posts ORDER BY platform DESC")]
    bad = _get(base + "/manual/save", "s3cret", data=f"id={yt_post}&url=not-a-link".encode()).read().decode()
    assert "Paste the full link" in bad
    _get(base + "/manual/save", "s3cret",
         data=urlencode({"id": yt_post, "url": "https://youtube.com/shorts/abc"}).encode())
    _get(base + "/manual/skip", "s3cret", data=f"id={tt_post}".encode())
    rows = dict(conn.execute("SELECT platform, status FROM posts").fetchall())
    assert rows == {"youtube": "posted", "tiktok": "skipped"}
    from clip_engine import pipeline
    [pending] = pipeline.pending_submissions(conn)
    assert pending["url"] == "https://youtube.com/shorts/abc"
    assert not (tmp_path / "clips" / ("c" * 32 + ".mp4")).exists()  # nothing left to post: file removed
    assert "Ready to post" not in _get(base + "/", "s3cret").read().decode()


def test_download_header(server):
    base, _ = server
    resp = _get(base + "/media/" + "a" * 32 + ".mp4?download=1")
    assert resp.headers["Content-Disposition"].startswith("attachment") and resp.read() == b"MP4DATA"


def test_stale_clips_expire(tmp_path):
    from clip_engine import pipeline
    conn = db.connect(tmp_path / "x.db")
    (tmp_path / "clips").mkdir()
    cid = _queue_for_review(conn, tmp_path)
    conn.execute("UPDATE posts SET status = 'ready', scheduled_at = datetime('now', '-8 days') WHERE clip_id = ?",
                 (cid,))
    pipeline.expire_stale(conn)
    assert {r[0] for r in conn.execute("SELECT status FROM posts")} == {"expired"}
    assert not (tmp_path / "clips" / ("c" * 32 + ".mp4")).exists()


def test_manual_views_come_from_public_page(monkeypatch):
    from clip_engine import campaigns as camp

    class FakeYDL:
        def __init__(self, url_views):
            self.url_views = url_views

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def extract_info(self, url, download=False):
            if url not in self.url_views:
                raise RuntimeError("login required")
            return {"view_count": self.url_views[url]}
    monkeypatch.setattr(camp, "_ydl", lambda opts: FakeYDL({"https://tiktok.com/v/1": 1234}))
    pub = platforms.ManualPublisher("tiktok")
    assert pub.stats(["https://tiktok.com/v/1", "https://instagram.com/reel/x"]) == {
        "https://tiktok.com/v/1": (1234, 0), "https://instagram.com/reel/x": (0, 0)}
    with pytest.raises(platforms.PublishError):
        pub.publish({}, None, "")


def test_produce_without_any_connection_makes_phone_posts(tmp_path, monkeypatch):
    from clip_engine import pipeline
    conn = db.connect(":memory:")
    pubs = {p: platforms.ManualPublisher(p) for p in platforms.ALL_PLATFORMS}
    db.put(conn, "review_mode", "off")
    monkeypatch.setattr(pipeline, "campaign_job", lambda *a: None)
    monkeypatch.setattr(pipeline, "archive_job", lambda *a: None)
    # no source found → nothing made, but it no longer refuses for lack of an automatic platform
    assert pipeline.produce(conn, Settings(data_dir=tmp_path), MagicMock(), None, pubs, 2) == 0


def test_post_time_label():
    from datetime import datetime, timezone
    now = datetime(2026, 10, 20, 12, 0, tzinfo=timezone.utc)
    assert dashboard.post_time_label("2026-10-20 19:00:00", now) == "Tue 3:00pm ET"
    assert dashboard.post_time_label("2026-10-20 11:00:00", now) == "now"
    assert dashboard.post_time_label(None, now) == ""


# --- Connect YouTube -------------------------------------------------------

from urllib.parse import parse_qs, urlsplit  # noqa: E402

from clip_engine import youtube  # noqa: E402


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def _raw(url, password="s3cret"):
    """GET without following redirects; returns (status, location)."""
    req = urllib.request.Request(url)
    if password:
        req.add_header("Authorization", "Basic " + base64.b64encode(f"me:{password}".encode()).decode())
    try:
        resp = urllib.request.build_opener(_NoRedirect).open(req, timeout=5)
        return resp.status, resp.headers.get("Location")
    except urllib.error.HTTPError as err:
        return err.code, err.headers.get("Location")


@pytest.fixture
def yt_server(tmp_path):
    settings = Settings(data_dir=tmp_path, dashboard_password="s3cret", port=0, youtube_client_id="cid",
                        youtube_client_secret="csecret", public_base_url="https://engine.example",
                        youtube_refresh_token="")
    conn = db.connect(settings.db_path)
    from http.server import ThreadingHTTPServer
    srv = ThreadingHTTPServer(("127.0.0.1", 0), dashboard.make_handler(settings))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}", conn, settings
    srv.shutdown()


def test_authorize_url_asks_for_offline_access():
    settings = Settings(youtube_client_id="cid", public_base_url="https://engine.example")
    q = parse_qs(urlsplit(youtube.authorize_url(settings, "st8")).query)
    assert q["client_id"] == ["cid"] and q["state"] == ["st8"]
    assert q["redirect_uri"] == ["https://engine.example/oauth/youtube/callback"]
    assert q["access_type"] == ["offline"] and q["prompt"] == ["consent"]
    assert "youtube.upload" in q["scope"][0]


def test_exchange_code_returns_refresh_token_or_explains():
    settings = Settings(youtube_client_id="cid", youtube_client_secret="sec", public_base_url="https://e.example")
    http = MagicMock()
    http.post.return_value = _resp({"access_token": "a", "refresh_token": "R"})
    assert youtube.exchange_code(settings, "code1", http) == "R"
    assert http.post.call_args.kwargs["data"]["grant_type"] == "authorization_code"
    http.post.return_value = _resp({"error": "invalid_grant", "error_description": "Bad code"})
    with pytest.raises(ValueError, match="Bad code"):
        youtube.exchange_code(settings, "x", http)


def test_connect_youtube_end_to_end(yt_server, monkeypatch):
    base, conn, settings = yt_server
    monkeypatch.setattr(youtube, "exchange_code", lambda s, code, http=None: f"refresh-for-{code}")
    status, location = _raw(base + "/connect/youtube")
    assert status == 302 and location.startswith("https://accounts.google.com/o/oauth2/v2/auth?")
    state = parse_qs(urlsplit(location).query)["state"][0]
    assert db.get(conn, "youtube_oauth_state") == state

    status, location = _raw(f"{base}/oauth/youtube/callback?code=abc&state={state}")
    assert status == 303 and "YouTube+connected" in location
    assert db.get(conn, "youtube_refresh_token") == "refresh-for-abc"
    assert youtube.stored_token(conn, settings) == "refresh-for-abc"
    assert youtube.effective_settings(conn, settings).youtube_refresh_token == "refresh-for-abc"
    assert not youtube.auto_posting_on(conn)  # connected, but still a phone platform

    # the state is single use
    status, location = _raw(f"{base}/oauth/youtube/callback?code=abc&state={state}")
    assert "expired" in location
    page = _get(base + "/", "s3cret").read().decode()
    assert "YouTube connection:" in page and "connected" in page


def test_connect_rejects_wrong_state_denial_and_strangers(yt_server, monkeypatch):
    base, conn, _ = yt_server
    monkeypatch.setattr(youtube, "exchange_code", lambda *a, **k: "never")
    _raw(base + "/connect/youtube")
    status, location = _raw(base + "/oauth/youtube/callback?code=abc&state=forged")
    assert "expired" in location and db.get(conn, "youtube_refresh_token") is None
    state = parse_qs(urlsplit(_raw(base + "/connect/youtube")[1]).query)["state"][0]
    status, location = _raw(f"{base}/oauth/youtube/callback?error=access_denied&state={state}")
    assert "access_denied" in location and db.get(conn, "youtube_refresh_token") is None
    assert _raw(base + "/connect/youtube", password=None)[0] == 401
    assert _raw(base + "/oauth/youtube/callback?code=a&state=b", password=None)[0] == 401


def test_exchange_failure_is_reported(yt_server, monkeypatch):
    base, conn, _ = yt_server

    def boom(*a, **k):
        raise ValueError("redirect_uri_mismatch")
    monkeypatch.setattr(youtube, "exchange_code", boom)
    state = parse_qs(urlsplit(_raw(base + "/connect/youtube")[1]).query)["state"][0]
    _, location = _raw(f"{base}/oauth/youtube/callback?code=abc&state={state}")
    assert "redirect_uri_mismatch" in location and db.get(conn, "youtube_refresh_token") is None


def test_youtube_card_guides_setup_and_toggle(yt_server, tmp_path):
    base, conn, _ = yt_server
    assert "Connect YouTube</a>" in _get(base + "/", "s3cret").read().decode()
    db.put(conn, "youtube_refresh_token", "R")
    page = _get(base + "/", "s3cret").read().decode()
    assert "Automatic posting: off" in page and "Turn automatic posting on" in page and "Test a YouTube upload" in page
    _get(base + "/settings/youtube-auto", "s3cret", data=b"")
    assert youtube.auto_posting_on(db.connect(tmp_path / "clip_engine.db"))


def test_card_explains_missing_google_keys(server):
    base, _ = server
    page = _get(base + "/", "s3cret").read().decode()
    assert "YOUTUBE_CLIENT_ID" in page and "/oauth/youtube/callback" in page


def test_privacy_page_is_public(server):
    base, _ = server
    page = _get(base + "/privacy").read().decode()
    assert "privacy policy" in page and "Limited Use" in page


def test_main_rebuilds_publishers_after_connect(tmp_path, monkeypatch):
    from clip_engine import __main__ as m
    settings = Settings(data_dir=tmp_path, youtube_client_id="c", youtube_client_secret="s", youtube_refresh_token="")
    conn = db.connect(settings.db_path)
    monkeypatch.setattr(m, "YouTube", lambda s: MagicMock(name="yt"))
    before = m.connection_signature(conn, settings)
    _, yt, pubs = m.build_publishers(conn, settings)
    assert yt is None and platforms.is_manual(pubs["youtube"])
    db.put(conn, "youtube_refresh_token", "R")
    assert m.connection_signature(conn, settings) != before
    _, yt, pubs = m.build_publishers(conn, settings)
    assert yt is not None and platforms.is_manual(pubs["youtube"])  # connected, still waiting for the switch
    db.put(conn, "youtube_auto", "on")
    _, _, pubs = m.build_publishers(conn, settings)
    assert not platforms.is_manual(pubs["youtube"]) and db.get(conn, "auto_platforms") == "youtube"


# --- YouTube upload test and private-lock detection --------------------------

class _Req:
    def __init__(self, response):
        self.response = response

    def next_chunk(self):
        return None, self.response


def _yt_with_response(tmp_path, response):
    yt = youtube.YouTube.__new__(youtube.YouTube)
    yt.settings = Settings(data_dir=tmp_path, youtube_refresh_token="R", privacy_status="public")
    inserts = []
    yt.api = MagicMock()
    yt.api.videos.return_value.insert.side_effect = lambda **kw: inserts.append(kw) or _Req(response)
    return yt, inserts


def test_upload_records_the_privacy_google_applied(tmp_path):
    video = tmp_path / "v.mp4"
    video.write_bytes(b"x")
    yt, inserts = _yt_with_response(tmp_path, {"id": "abc", "status": {"privacyStatus": "private"}})
    assert yt.upload(video, "t", "d", []) == "abc"
    assert yt.last_privacy == "private" and inserts[0]["body"]["status"]["privacyStatus"] == "public"
    assert yt.upload(video, "t", "d", [], privacy="unlisted") == "abc"
    assert inserts[1]["body"]["status"]["privacyStatus"] == "unlisted"


def test_test_upload_reports_lock_or_success(tmp_path, monkeypatch):
    import subprocess
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: (tmp_path / "w" / "connection-test.mp4").write_bytes(b"v"))
    for applied, locked in (("private", True), ("unlisted", False)):
        yt, _ = _yt_with_response(tmp_path, {"id": "vid1", "status": {"privacyStatus": applied}})
        result = yt.test_upload(tmp_path / "w")
        assert result == {"video_id": "vid1", "requested": "unlisted", "applied": applied, "locked": locked}
        assert not (tmp_path / "w" / "connection-test.mp4").exists()


def test_publisher_switches_off_when_google_keeps_upload_private(tmp_path):
    conn = db.connect(":memory:")
    db.put(conn, "youtube_auto", "on")
    yt, _ = _yt_with_response(tmp_path, {"id": "abc", "status": {"privacyStatus": "private"}})
    clip = {"title": "T", "description": "D", "tags": "[]"}
    pubs = platforms.enabled(yt.settings, conn, yt)
    video = tmp_path / "v.mp4"
    video.write_bytes(b"x")
    assert pubs["youtube"].publish(clip, video, "") == ("abc", "https://youtu.be/abc")
    assert db.get(conn, "youtube_auto") == "off" and db.get(conn, "youtube_private_lock") == "abc"
    # public stays public: nothing changes
    db.put(conn, "youtube_auto", "on")
    db.put(conn, "youtube_private_lock", "")
    yt2, _ = _yt_with_response(tmp_path, {"id": "def", "status": {"privacyStatus": "public"}})
    platforms.enabled(yt2.settings, conn, yt2)["youtube"].publish(clip, video, "")
    assert db.get(conn, "youtube_auto") == "on"


def test_dashboard_test_button(yt_server, monkeypatch):
    base, conn, _ = yt_server
    _, location = _raw(base + "/", password="s3cret")
    page = _get(base + "/settings/youtube-test", "s3cret", data=b"").read().decode()
    assert "Connect YouTube first" in page

    db.put(conn, "youtube_refresh_token", "R")
    results = iter([{"video_id": "v", "requested": "unlisted", "applied": "unlisted", "locked": False},
                    {"video_id": "v", "requested": "unlisted", "applied": "private", "locked": True}])

    class FakeYT:
        def __init__(self, settings):
            pass

        def test_upload(self, workdir):
            return next(results)
    monkeypatch.setattr(youtube, "YouTube", FakeYT)
    ok = _get(base + "/settings/youtube-test", "s3cret", data=b"").read().decode()
    assert "Test passed" in ok and "not locked" in _get(base + "/", "s3cret").read().decode()
    bad = _get(base + "/settings/youtube-test", "s3cret", data=b"").read().decode()
    assert "kept the test video private" in bad
    assert "audit needed" in _get(base + "/", "s3cret").read().decode()
    db.put(conn, "youtube_private_lock", "vid9")
    assert "Google kept an automatic upload private" in _get(base + "/", "s3cret").read().decode()


# --- security: cancel the YouTube login, lock out password guessing ------------

def test_revoke_asks_google_to_cancel_the_token():
    http = MagicMock()
    http.post.return_value = MagicMock(status_code=200)
    assert youtube.revoke("R", http) is True
    assert http.post.call_args.args[0] == youtube.REVOKE_URL and http.post.call_args.kwargs["data"] == {"token": "R"}
    http.post.return_value = MagicMock(status_code=400)
    assert youtube.revoke("R", http) is False
    assert youtube.revoke("", http) is False


def test_disconnect_cancels_and_forgets_the_login(yt_server, monkeypatch):
    base, conn, _ = yt_server
    db.put(conn, "youtube_refresh_token", "R")
    db.put(conn, "youtube_auto", "on")
    revoked = []
    monkeypatch.setattr(youtube, "revoke", lambda token, http=None: revoked.append(token) or True)
    page = _get(base + "/settings/youtube-disconnect", "s3cret", data=b"").read().decode()
    assert revoked == ["R"] and "login cancelled at Google" in page
    assert not youtube.stored_token(conn, Settings(youtube_refresh_token="")) and not youtube.auto_posting_on(conn)
    assert "Connect YouTube</a>" in page


def test_disconnect_says_so_when_google_does_not_confirm(yt_server, monkeypatch):
    base, conn, _ = yt_server
    db.put(conn, "youtube_refresh_token", "R")
    monkeypatch.setattr(youtube, "revoke", lambda token, http=None: (_ for _ in ()).throw(OSError("offline")))
    page = _get(base + "/settings/youtube-disconnect", "s3cret", data=b"").read().decode()
    assert "myaccount.google.com/permissions" in page and not db.get(conn, "youtube_refresh_token")


def test_login_throttle_locks_out_after_five_wrong_passwords():
    now = [0.0]
    throttle = dashboard.LoginThrottle(clock=lambda: now[0])
    for _ in range(dashboard.MAX_BAD_PASSWORDS):
        assert not throttle.blocked("1.2.3.4")
        throttle.fail("1.2.3.4")
    assert throttle.blocked("1.2.3.4") and not throttle.blocked("5.6.7.8")
    now[0] += dashboard.LOCKOUT_SECONDS + 1
    assert not throttle.blocked("1.2.3.4")
    throttle.fail("1.2.3.4")
    throttle.reset("1.2.3.4")
    assert not throttle.blocked("1.2.3.4")


def test_dashboard_locks_out_a_guesser_but_not_the_owner_elsewhere(tmp_path):
    from http.server import ThreadingHTTPServer
    settings = Settings(data_dir=tmp_path, dashboard_password="s3cret", port=0)
    db.connect(settings.db_path)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), dashboard.make_handler(settings))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        def status(password, forwarded):
            req = urllib.request.Request(base + "/")
            req.add_header("X-Forwarded-For", f"9.9.9.9, {forwarded}")
            req.add_header("Authorization", "Basic " + base64.b64encode(f"me:{password}".encode()).decode())
            try:
                return urllib.request.urlopen(req, timeout=5).status
            except urllib.error.HTTPError as err:
                return err.code
        assert [status("wrong", "6.6.6.6") for _ in range(dashboard.MAX_BAD_PASSWORDS)] == [401] * 5
        assert status("wrong", "6.6.6.6") == 429
        assert status("s3cret", "6.6.6.6") == 429  # even the right password waits out the lockout
        assert status("s3cret", "7.7.7.7") == 200  # a different address is unaffected
    finally:
        srv.shutdown()
