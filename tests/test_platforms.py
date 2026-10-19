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
    assert platforms.enabled(Settings(youtube_refresh_token=""), conn, None) == {}
    monkeypatch.setenv("TIKTOK_CLIENT_KEY", "k")
    monkeypatch.setenv("TIKTOK_REFRESH_TOKEN", "r")
    assert list(platforms.enabled(Settings(youtube_refresh_token="x"), conn, MagicMock())) == ["youtube", "tiktok"]


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
