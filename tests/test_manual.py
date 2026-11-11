import base64
import io
import json
import urllib.error
import urllib.request

import pytest

from clip_engine import campaigns, dashboard, db, manual, media, pipeline
from tests.test_platforms import _get, server  # noqa: F401  (the dashboard fixture)


def _campaign(tmp_path, **kw):
    c = campaigns.Campaign(id="pod", name="Pod Show", program_url="https://whop.com/pod", rate_per_1k_views=1.5,
                           sources=["https://youtube.com/@pod"], caption_required="#pod", **kw)
    campaigns.save(tmp_path, c)
    return c


def test_save_upload_streams_to_disk_and_checks_what_it_gets(tmp_path):
    path = manual.save_upload(io.BytesIO(b"x" * 3_000_000), 3_000_000, "Episode 1.MP4", tmp_path)
    assert path.parent == tmp_path / "uploads" and path.suffix == ".mp4" and path.stat().st_size == 3_000_000
    for name, length in (("notes.txt", 10), ("a.mp4", 0)):
        with pytest.raises(manual.UploadError):
            manual.save_upload(io.BytesIO(b"x" * 10), length, name, tmp_path)
    with pytest.raises(manual.UploadError, match="cut off"):
        manual.save_upload(io.BytesIO(b"x" * 5), 100, "a.mp4", tmp_path)
    assert len(list((tmp_path / "uploads").iterdir())) == 1  # the cut-off upload left nothing behind


def test_queue_order_and_link_cleaning():
    conn = db.connect(":memory:")
    manual.add(conn, {"n": 1})
    manual.add(conn, {"n": 2})
    manual.add(conn, {"n": 0}, front=True)
    assert [manual.pop(conn) for _ in range(4)] == [{"n": 0}, {"n": 1}, {"n": 2}, None]
    assert manual.clean_link(" https://drive.google.com/file/d/x ") == "https://drive.google.com/file/d/x"
    with pytest.raises(manual.UploadError):
        manual.clean_link("drive.google.com/x")


def test_manual_job_uses_uploaded_file_and_the_campaign_credit(tmp_path, monkeypatch):
    conn = db.connect(tmp_path / "m.db")
    camp = _campaign(tmp_path)
    video = tmp_path / "uploads" / "abc.mp4"
    video.parent.mkdir()
    video.write_bytes(b"v")
    monkeypatch.setattr(media, "duration", lambda p: 3000.0)
    manual.add(conn, {"kind": "file", "ref": str(video), "campaign_id": "pod", "creator": "Host Name", "title": "Ep 1"})
    job = pipeline.manual_job(conn, [camp], [])
    assert job.campaign.id == "pod" and job.src.identifier == "upload:abc" and job.src.duration == 3000.0
    assert job.src.video_url == str(video) and "Host Name" in job.footer and "#ad" in job.footer and "#pod" in job.footer
    assert job.item["title"] == "Ep 1" and pipeline.manual_job(conn, [camp], []) is None


def test_manual_job_drops_videos_whose_campaign_ended(tmp_path):
    conn = db.connect(tmp_path / "m.db")
    video = tmp_path / "uploads" / "old.mp4"
    video.parent.mkdir()
    video.write_bytes(b"v")
    manual.add(conn, {"kind": "file", "ref": str(video), "campaign_id": "gone"})
    assert pipeline.manual_job(conn, [], []) is None and not video.exists()


def test_manual_link_goes_through_yt_dlp_describe(tmp_path, monkeypatch):
    conn = db.connect(tmp_path / "m.db")
    camp = _campaign(tmp_path)
    seen = []

    def describe(campaign, url):
        seen.append(url)
        return pipeline.sources.Source(url, "Title", "Uploader", campaign.program_url, url, 1800, page=url)
    monkeypatch.setattr(campaigns, "describe", describe)
    manual.add(conn, {"kind": "url", "ref": "https://drive.google.com/file/d/1", "campaign_id": "pod", "creator": ""})
    job = pipeline.manual_job(conn, [camp], [])
    assert seen == ["https://drive.google.com/file/d/1"] and job.src.creator == "Uploader"
    manual.add(conn, {"kind": "url", "ref": "https://www.tiktok.com/@a/video/1", "campaign_id": "pod"})
    assert pipeline.manual_job(conn, [camp], []) is None  # watermarked sources are skipped


def test_dashboard_card_appears_only_with_a_live_campaign(server, tmp_path):
    base, _ = server
    assert 'id="own-video"' not in _get(base + "/", "s3cret").read().decode()
    _campaign(tmp_path)
    page = _get(base + "/", "s3cret").read().decode()
    assert 'id="own-video"' in page and "Upload a video from this phone" in page and "Pod Show" in page


def test_dashboard_upload_and_link_routes(server, tmp_path):
    base, conn = server
    _campaign(tmp_path)

    def upload(query, body, password="s3cret"):
        req = urllib.request.Request(f"{base}/manual-video/upload?{query}", data=body, method="POST")
        req.add_header("Origin", base)
        if password:
            req.add_header("Authorization", "Basic " + base64.b64encode(f"me:{password}".encode()).decode())
        try:
            resp = urllib.request.urlopen(req, timeout=5)
            return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as err:
            return err.code, json.loads(err.read() or b"{}")

    status, reply = upload("campaign=pod&name=ep1.mp4&title=Ep+1&creator=Host", b"video-bytes")
    assert status == 200 and "uploaded" in reply["message"]
    queued = manual.queue(db.connect(tmp_path / "clip_engine.db"))
    assert queued[0]["kind"] == "file" and queued[0]["title"] == "Ep 1" and queued[0]["creator"] == "Host"
    assert open(queued[0]["ref"], "rb").read() == b"video-bytes"
    assert db.get(db.connect(tmp_path / "clip_engine.db"), "run_now") == "1"

    assert upload("campaign=nope&name=ep1.mp4", b"x")[0] == 400
    assert upload("campaign=pod&name=ep1.txt", b"x")[0] == 400
    assert upload("campaign=pod&name=ep1.mp4", b"x", password=None)[0] == 401

    page = _get(base + "/manual-video/link", "s3cret",
                data=b"campaign=pod&url=https%3A%2F%2Fdrive.google.com%2Ffile%2Fd%2F1&creator=&title=").read().decode()
    assert "Video added" in page
    assert manual.queue(db.connect(tmp_path / "clip_engine.db"))[-1]["kind"] == "url"
    bad = _get(base + "/manual-video/link", "s3cret", data=b"campaign=pod&url=notalink").read().decode()
    assert "starting with https" in bad
    tik = _get(base + "/manual-video/link", "s3cret",
               data=b"campaign=pod&url=https%3A%2F%2Fwww.tiktok.com%2F%40a%2Fvideo%2F1").read().decode()
    assert "watermark" in tik


def test_produce_clips_an_uploaded_file_then_deletes_it(tmp_path, monkeypatch):
    from clip_engine import layout
    from clip_engine.config import Settings
    from tests.test_core import FakePub, _claude_returning

    conn = db.connect(tmp_path / "e2e.db")
    _campaign(tmp_path, platforms=["youtube", "tiktok"])
    video = tmp_path / "uploads" / "ep1.mp4"
    video.parent.mkdir()
    video.write_bytes(b"source")
    manual.add(conn, {"kind": "file", "ref": str(video), "campaign_id": "pod", "creator": "Host Name", "title": "Ep 1"})
    monkeypatch.setattr(media, "duration", lambda p: 3000.0)
    monkeypatch.setattr(campaigns, "latest_videos", lambda url: (_ for _ in ()).throw(AssertionError("looked up channel")))
    monkeypatch.setattr(campaigns, "download", lambda *a: (_ for _ in ()).throw(AssertionError("downloaded")))
    words = [{"start": 1.0 + i, "end": 1.5 + i, "word": f" w{i}"} for i in range(40)]
    transcribed = []

    def fake_transcribe(v, m, windows=None):
        transcribed.append((v, windows))
        return [{"start": 0, "end": 50, "text": "hi", "window": 0, "energy": 2.0}], words
    monkeypatch.setattr(media, "transcribe", fake_transcribe)
    monkeypatch.setattr(media, "plan_layout", lambda v, s, e: layout.Layout("stack", left=0.3, right=0.7))

    def fake_render(video_path, out, *a, frame=None, music=None):
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"clip")
        return out
    monkeypatch.setattr(media, "render", fake_render)
    from clip_engine import trends
    monkeypatch.setattr(trends, "brief", lambda *a: {"hashtags": [], "phrases": [], "title_patterns": [], "notes": "", "sounds": []})
    claude = _claude_returning({"clips": [
        {"start": 1, "end": 37, "title": "Clip", "description": "desc", "tags": ["a"], "hook_text": "Wait",
         "hashtags": ["#podcast"], "score": 8, "why": ""}]})
    pubs = {"youtube": FakePub("youtube"), "tiktok": FakePub("tiktok")}

    assert pipeline.produce(conn, Settings(data_dir=tmp_path), claude, None, pubs, 2) == 1
    assert transcribed == [(video, None)]  # the uploaded file itself, no most-replayed windows
    description = conn.execute("SELECT description FROM clips").fetchone()[0]
    assert "Host Name" in description and "#ad" in description
    assert not video.exists() and manual.queue(conn) == []


def test_form_accepts_approved_captions_and_no_channel(tmp_path):
    form = {"name": "Ring Drone", "program_url": "https://whop.com/ring", "rate": "2", "sources": "",
            "approved_captions": "first look at the Ring Drone @Ring #ad\r\n\r\n  first look at the Ring Drone @Ring #ad \nRing just announced the Ring Drone @Ring #ad\n"}
    c = campaigns.from_form(form, ["youtube"])
    assert c.sources == [] and c.approved_captions == ["first look at the Ring Drone @Ring #ad",
                                                       "Ring just announced the Ring Drone @Ring #ad"]
    with pytest.raises(campaigns.CampaignError, match="https://"):
        campaigns.from_form({**form, "sources": "nope"}, ["youtube"])


def _produce_fixture(tmp_path, monkeypatch, campaign_kwargs, titles):
    from clip_engine import layout, trends
    from tests.test_core import _claude_returning

    conn = db.connect(tmp_path / "caps.db")
    _campaign(tmp_path, platforms=["youtube", "tiktok"], **campaign_kwargs)
    video = tmp_path / "uploads" / "ep1.mp4"
    video.parent.mkdir(exist_ok=True)
    video.write_bytes(b"source")
    manual.add(conn, {"kind": "file", "ref": str(video), "campaign_id": "pod", "creator": "", "title": "Ep 1"})
    monkeypatch.setattr(media, "duration", lambda p: 3000.0)
    words = [{"start": 1.0 + i, "end": 1.5 + i, "word": f" w{i}"} for i in range(120)]
    monkeypatch.setattr(media, "transcribe", lambda v, m, windows=None: (
        [{"start": 0, "end": 150, "text": "hi", "window": 0, "energy": 2.0}], words))
    monkeypatch.setattr(media, "plan_layout", lambda v, s, e: layout.Layout("stack", left=0.3, right=0.7))
    monkeypatch.setattr(media, "render", lambda video_path, out, *a, **k: (out.parent.mkdir(parents=True, exist_ok=True),
                                                                          out.write_bytes(b"clip"), out)[2])
    monkeypatch.setattr(trends, "brief", lambda *a: {"hashtags": [], "phrases": [], "title_patterns": [], "notes": "", "sounds": []})
    claude = _claude_returning({"clips": [
        {"start": 1 + 40 * i, "end": 37 + 40 * i, "title": t, "description": "ai words", "tags": ["a"],
         "hook_text": "Wait", "hashtags": ["#podcast"], "score": 8, "why": ""} for i, t in enumerate(titles)]})
    return conn, claude


def test_approved_captions_are_used_word_for_word_and_nothing_else(tmp_path, monkeypatch):
    from clip_engine.config import Settings
    from tests.test_core import FakePub

    caps = ["first look at the Ring Drone @Ring #ad", "Ring just announced the Ring Drone @Ring #ad"]
    conn, claude = _produce_fixture(tmp_path, monkeypatch, {"approved_captions": caps},
                                    ["Same title here", "Same title here again"])  # near-identical AI titles
    pubs = {"youtube": FakePub("youtube"), "tiktok": FakePub("tiktok")}
    assert pipeline.produce(conn, Settings(data_dir=tmp_path), claude, None, pubs, 3) == 2
    rows = conn.execute("SELECT title, description, tags, hook_text FROM clips ORDER BY id").fetchall()
    assert [(r["title"], r["description"]) for r in rows] == [(caps[0], caps[0]), (caps[1], caps[1])]  # in turn
    assert all(r["tags"] == "[]" and r["hook_text"] == "" for r in rows)
    assert not any(x in r["description"] for r in rows for x in ("http", "#Shorts", "#podcast", "Paid clip"))


def test_without_approved_captions_the_engine_writes_its_own(tmp_path, monkeypatch):
    from clip_engine.config import Settings
    from tests.test_core import FakePub

    conn, claude = _produce_fixture(tmp_path, monkeypatch, {}, ["A good clip"])
    pubs = {"youtube": FakePub("youtube"), "tiktok": FakePub("tiktok")}
    assert pipeline.produce(conn, Settings(data_dir=tmp_path), claude, None, pubs, 3) == 1
    description = conn.execute("SELECT description FROM clips").fetchone()[0]
    assert "ai words" in description and "#ad" in description and "#podcast" in description


def test_video_you_added_is_kept_when_no_account_is_ticked(tmp_path, monkeypatch):
    from clip_engine.config import Settings
    from tests.test_core import FakePub

    conn, claude = _produce_fixture(tmp_path, monkeypatch, {}, ["A good clip"])
    pipeline.set_post_to(conn, ["instagram"])  # the campaign pays on YouTube/TikTok only
    pubs = {"youtube": FakePub("youtube"), "tiktok": FakePub("tiktok"), "instagram": FakePub("instagram")}
    assert pipeline.produce(conn, Settings(data_dir=tmp_path), claude, None, pubs, 3) == 0
    assert manual.queue(conn)[0]["title"] == "Ep 1" and (tmp_path / "uploads" / "ep1.mp4").exists()
