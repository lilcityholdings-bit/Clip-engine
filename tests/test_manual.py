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
    assert 'id="own-video"' in page and "Upload videos or photos from this phone" in page and "Pod Show" in page


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
    monkeypatch.setattr(media, "has_audio", lambda v: True)
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
    monkeypatch.setattr(media, "has_audio", lambda v: True)
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


# --- silent footage -----------------------------------------------------------

def test_visual_windows_cut_on_shot_changes_and_spread_out():
    from clip_engine import moments
    # a 100 s video with shot changes every 12 s
    cuts = [12.0 * i for i in range(1, 9)]
    pieces = moments.visual_windows(cuts, 100.0, max_sec=30, count=10)
    assert pieces[0][0] == 0.0 and all(b - a <= 30 for a, b in pieces) and all(b - a >= 10 for a, b in pieces)
    assert all(pieces[i][1] == pieces[i + 1][0] for i in range(len(pieces) - 1))  # back to back, no overlap
    assert all(end in cuts + [100.0] for _, end in pieces[:-1])  # each clip ends on a shot change
    picked = moments.visual_windows(cuts, 100.0, max_sec=30, count=2)
    assert len(picked) == 2 and picked[0] == pieces[0] and picked[1][0] > pieces[0][1]  # spread across the video
    assert moments.visual_windows([], 3.0, max_sec=30, count=3) == []  # too short to clip
    assert moments.visual_windows([], 70.0, max_sec=30, count=5) == [(0.0, 30.0), (30.0, 60.0), (60.0, 70.0)]  # one long shot is split


def test_has_speech_needs_real_words():
    from clip_engine import moments
    assert not moments.has_speech([]) and not moments.has_speech([{"word": "x"}] * 5)
    assert moments.has_speech([{"word": "x"}] * 40)


def test_scene_cuts_finds_the_shot_changes_in_a_real_video(tmp_path):
    import subprocess
    video = tmp_path / "cuts.mp4"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "color=c=red:s=160x120:r=10:d=3",
                    "-f", "lavfi", "-i", "color=c=blue:s=160x120:r=10:d=3", "-filter_complex", "[0][1]concat=n=2:v=1[v]",
                    "-map", "[v]", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(video)], check=True)
    assert media.has_audio(video) is False
    cuts = media.scene_cuts(video)
    assert len(cuts) == 1 and 2.5 < cuts[0] < 3.5


def test_silent_footage_is_cut_by_picture_when_captions_are_approved(tmp_path, monkeypatch):
    from clip_engine.config import Settings
    from tests.test_core import FakePub

    caps = ["first look at the Ring Drone @Ring #ad"]
    conn, claude = _produce_fixture(tmp_path, monkeypatch, {"approved_captions": caps}, ["unused"])
    monkeypatch.setattr(media, "has_audio", lambda v: False)
    monkeypatch.setattr(media, "transcribe", lambda *a, **k: (_ for _ in ()).throw(AssertionError("transcribed silence")))
    monkeypatch.setattr(media, "scene_cuts", lambda v: [15.0, 40.0, 70.0, 110.0, 150.0])
    monkeypatch.setattr(media, "duration", lambda p: 180.0)
    rendered = []
    monkeypatch.setattr(media, "render", lambda video_path, out, start, end, fmt, captions, **k: (
        rendered.append((start, end, captions)), out.parent.mkdir(parents=True, exist_ok=True), out.write_bytes(b"c"), out)[3])
    pubs = {"youtube": FakePub("youtube"), "tiktok": FakePub("tiktok")}
    assert pipeline.produce(conn, Settings(data_dir=tmp_path), claude, None, pubs, 2) == 4  # all of today's 4
    assert not claude.beta.messages.create.called  # no Claude call, so no cost
    assert len(rendered) == 4 and all(10 <= b - a <= 58 for a, b, _ in rendered)
    assert all("Dialogue" not in text for _, _, text in rendered)  # no burned-in words, no hook text
    rows = conn.execute("SELECT title, description FROM clips").fetchall()
    assert all(r["title"] == caps[0] == r["description"] for r in rows)


def test_silent_footage_without_approved_captions_is_skipped(tmp_path, monkeypatch):
    from clip_engine.config import Settings
    from tests.test_core import FakePub

    conn, claude = _produce_fixture(tmp_path, monkeypatch, {}, ["unused"])
    monkeypatch.setattr(media, "has_audio", lambda v: False)
    pubs = {"youtube": FakePub("youtube"), "tiktok": FakePub("tiktok")}
    assert pipeline.produce(conn, Settings(data_dir=tmp_path), claude, None, pubs, 2) == 0


def test_a_failed_run_keeps_your_upload_for_another_try(tmp_path, monkeypatch):
    from clip_engine.config import Settings
    from tests.test_core import FakePub

    conn, claude = _produce_fixture(tmp_path, monkeypatch, {}, ["A good clip"])
    video = tmp_path / "uploads" / "ep1.mp4"
    monkeypatch.setattr(media, "render", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("ffmpeg died")))
    pubs = {"youtube": FakePub("youtube"), "tiktok": FakePub("tiktok")}
    for attempt in (1, 2):
        with pytest.raises(RuntimeError):
            pipeline.produce(conn, Settings(data_dir=tmp_path), claude, None, pubs, 2)
        assert video.exists() and manual.queue(conn)[0]["attempts"] == attempt
    with pytest.raises(RuntimeError):
        pipeline.produce(conn, Settings(data_dir=tmp_path), claude, None, pubs, 2)
    assert not video.exists() and manual.queue(conn) == []  # third failure: given up


def test_an_uploaded_video_gets_all_of_todays_clips(tmp_path, monkeypatch):
    from clip_engine.config import Settings
    from tests.test_core import FakePub

    conn, claude = _produce_fixture(tmp_path, monkeypatch, {}, ["One thing", "Another topic", "Third subject"])
    pubs = {"youtube": FakePub("youtube"), "tiktok": FakePub("tiktok")}
    assert pipeline.produce(conn, Settings(data_dir=tmp_path), claude, None, pubs, 2) == 3  # not capped at 2


def test_silent_upload_without_captions_explains_itself(tmp_path, monkeypatch):
    from clip_engine.config import Settings
    from tests.test_core import FakePub

    conn, claude = _produce_fixture(tmp_path, monkeypatch, {}, ["unused"])
    monkeypatch.setattr(media, "has_audio", lambda v: False)
    pubs = {"youtube": FakePub("youtube"), "tiktok": FakePub("tiktok")}
    assert pipeline.produce(conn, Settings(data_dir=tmp_path), claude, None, pubs, 2) == 0
    assert "approved captions" in db.get(conn, "produce_note")


def test_caption_is_not_doubled_when_the_campaign_dictates_it():
    from clip_engine import platforms
    cap = "first look at the Ring Drone @Ring #ad"
    assert platforms.caption_for({"title": cap, "description": cap}) == cap
    assert platforms.caption_for({"title": "T", "description": "D"}) == "T\n\nD"


def test_bullets_pasted_from_a_google_doc_are_not_part_of_the_caption():
    pasted = "● first look at the Ring Drone \U0001F440 @Ring #ad\n• Ring just announced the Ring Drone @Ring #ad\n  - this is the new Ring Drone from @Ring #ad\n\n"
    assert campaigns.approved_captions(pasted) == [
        "first look at the Ring Drone \U0001F440 @Ring #ad",
        "Ring just announced the Ring Drone @Ring #ad",
        "this is the new Ring Drone from @Ring #ad"]
    assert campaigns.approved_captions("  a caption  \na caption\n") == ["a caption"]


def test_short_videos_are_posted_whole_and_queued_videos_run_back_to_back(tmp_path):
    from clip_engine import moments
    assert moments.visual_windows([], 7.0, max_sec=58, count=3) == [(0.0, 7.0)]  # a 7 s product teaser is one clip
    assert moments.visual_windows([], 4.0, max_sec=58, count=3) == []
    conn = db.connect(tmp_path / "c.db")
    assert pipeline.chain_next(conn, 2) is False  # nothing queued
    manual.add(conn, {"kind": "file", "ref": "/x.mp4", "campaign_id": "r"})
    assert pipeline.chain_next(conn, 0) is False and db.get(conn, "run_now") != "1"  # that run made nothing: don't loop
    assert pipeline.chain_next(conn, 2) is True and db.get(conn, "run_now") == "1"


# --- several videos and photos made into one video -------------------------------------------

def test_plan_pieces_fits_one_short():
    assert media.plan_pieces([(True, 3), (False, 40), (True, 3)]) == [3.0, 15.0, 3.0]  # photo 3s, video's first 15s
    assert media.plan_pieces([(False, 1.0)]) == [1.0]  # a very short video is used whole
    many = media.plan_pieces([(True, 3)] * 30)  # 90s of photos: shortened, never below the minimum
    assert sum(many) <= media.COMPILATION_MAX_SEC and all(x >= media.MIN_IMAGE_SEC for x in many)
    mixed = media.plan_pieces([(False, 60)] * 10)  # 150s wanted: shrunk in proportion and the tail dropped if needed
    assert sum(mixed) <= media.COMPILATION_MAX_SEC and len(mixed) <= 10


def test_photos_are_accepted_and_batches_become_one_project(tmp_path):
    conn = db.connect(tmp_path / "b.db")
    assert manual.is_image("IMG_1.JPG") and not manual.is_image("a.mp4")
    paths = [manual.save_upload(io.BytesIO(b"x" * 10), 10, name, tmp_path) for name in ("a.jpg", "b.mp4", "c.png")]
    for p in paths:
        manual.add_to_batch(conn, "abc123xyz", p)
    assert manual.queue(conn) == []  # nothing is queued until the last file lands
    assert manual.finish_batch(conn, "abc123xyz", {"campaign_id": "pod"}) == 3
    item = manual.queue(conn)[0]
    assert item["kind"] == "project" and item["files"] == [str(p) for p in paths]  # upload order kept
    with pytest.raises(manual.UploadError):
        manual.finish_batch(conn, "abc123xyz", {})
    with pytest.raises(manual.UploadError):
        manual.clean_batch_id("../../x")
    manual.discard(item)
    assert not any(p.exists() for p in paths)


def test_unfinished_batches_are_cleaned_up_after_a_day(tmp_path):
    conn = db.connect(tmp_path / "b.db")
    leftover = manual.save_upload(io.BytesIO(b"x" * 10), 10, "a.jpg", tmp_path)
    manual.add_to_batch(conn, "oldbatch1", leftover)
    manual.expire_batches(conn, now=__import__("time").time() + 25 * 3600)
    assert not leftover.exists() and manual._batches(conn) == {}


def test_assemble_builds_one_vertical_video_from_photos_and_a_video(tmp_path):
    import subprocess
    def ff(*args):
        subprocess.run(["ffmpeg", "-y", "-v", "error", *args], check=True)
    ff("-f", "lavfi", "-i", "testsrc2=s=1600x1200:d=1", "-frames:v", "1", str(tmp_path / "a.jpg"))
    ff("-f", "lavfi", "-i", "smptebars=s=900x1600", "-frames:v", "1", str(tmp_path / "b.png"))
    ff("-f", "lavfi", "-i", "testsrc=s=1280x720:d=4:r=24", "-f", "lavfi", "-i", "sine=f=440:d=4", "-shortest",
       str(tmp_path / "v.mp4"))
    out = media.assemble([tmp_path / "a.jpg", tmp_path / "v.mp4", tmp_path / "b.png"], tmp_path / "out.mp4", tmp_path / "w")
    assert media._dimensions(out) == (1080, 1920) and media.has_audio(out)
    assert 9.5 < media.duration(out) < 10.5  # 3s photo + 4s video + 3s photo
    photos = media.assemble([tmp_path / "a.jpg", tmp_path / "b.png"], tmp_path / "p.mp4", tmp_path / "w2")
    assert not media.has_audio(photos) and 5.5 < media.duration(photos) < 6.5


def test_dashboard_upload_batch_route_queues_one_project(server, tmp_path):
    base, _ = server
    _campaign(tmp_path)

    def upload(query, body):
        req = urllib.request.Request(f"{base}/manual-video/upload?{query}", data=body, method="POST")
        req.add_header("Origin", base)
        req.add_header("Authorization", "Basic " + base64.b64encode(b"me:s3cret").decode())
        try:
            resp = urllib.request.urlopen(req, timeout=5)
            return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as err:
            return err.code, json.loads(err.read() or b"{}")

    assert upload("campaign=pod&name=a.jpg", b"photo")[0] == 400  # a photo alone can't be clipped
    assert upload("campaign=pod&name=a.jpg&batch=batch0001&last=", b"photo") == (200, {"message": "File added."})
    conn = db.connect(tmp_path / "clip_engine.db")
    assert manual.queue(conn) == []
    status, reply = upload("campaign=pod&name=b.mp4&batch=batch0001&last=1&title=Drone", b"video")
    assert status == 200 and "2 files" in reply["message"] and "one video" in reply["message"]
    item = manual.queue(conn)[0]
    assert item["kind"] == "project" and len(item["files"]) == 2 and item["title"] == "Drone"
    assert db.get(conn, "run_now") == "1"


def test_produce_posts_a_combined_video_whole_with_the_approved_caption(tmp_path, monkeypatch):
    from clip_engine.config import Settings
    from tests.test_core import FakePub

    caps = ["first look at the Ring Drone @Ring #ad"]
    conn, claude = _produce_fixture(tmp_path, monkeypatch, {"approved_captions": caps}, ["unused"])
    db.put(conn, manual.QUEUE_KEY, "[]")  # drop the fixture's own upload; only the combined video is queued
    pieces = []
    for name in ("a.jpg", "b.mp4"):
        f = tmp_path / "uploads" / name
        f.parent.mkdir(exist_ok=True)
        f.write_bytes(b"piece")
        pieces.append(str(f))
    manual.add(conn, {"kind": "project", "files": pieces, "campaign_id": "pod", "creator": "", "title": "Drone"})
    built = []

    def fake_assemble(files, out, work):
        built.append([f.name for f in files])
        out.write_bytes(b"combined")
        return out
    monkeypatch.setattr(media, "assemble", fake_assemble)
    monkeypatch.setattr(media, "duration", lambda p: 14.0)
    monkeypatch.setattr(media, "transcribe", lambda *a, **k: (_ for _ in ()).throw(AssertionError("transcribed")))
    monkeypatch.setattr(media, "plan_layout", lambda *a: (_ for _ in ()).throw(AssertionError("re-framed")))
    rendered = []
    monkeypatch.setattr(media, "render", lambda video_path, out, start, end, fmt, captions, frame=None, **k: (
        rendered.append((start, end, fmt, frame.kind)), out.parent.mkdir(parents=True, exist_ok=True),
        out.write_bytes(b"c"), out)[3])
    pubs = {"youtube": FakePub("youtube"), "tiktok": FakePub("tiktok")}
    assert pipeline.produce(conn, Settings(data_dir=tmp_path), claude, None, pubs, 2) == 1
    assert built == [["a.jpg", "b.mp4"]]  # in upload order
    assert rendered == [(0.0, 14.0, "short", "blur")]  # the whole video, vertical, not re-framed
    assert not claude.beta.messages.create.called
    row = conn.execute("SELECT title, description FROM clips").fetchone()
    assert row["title"] == caps[0] == row["description"]
    left = {f.name for f in (tmp_path / "uploads").iterdir()}
    assert manual.queue(conn) == [] and left == {"ep1.mp4"}  # your files and the combined copy are gone


def test_combined_video_without_approved_captions_uses_your_title_and_the_campaign_credit(tmp_path, monkeypatch):
    from clip_engine.config import Settings
    from tests.test_core import FakePub

    conn, claude = _produce_fixture(tmp_path, monkeypatch, {}, ["unused"])
    db.put(conn, manual.QUEUE_KEY, "[]")
    f = tmp_path / "uploads" / "a.jpg"
    f.parent.mkdir(exist_ok=True)
    f.write_bytes(b"piece")
    manual.add(conn, {"kind": "project", "files": [str(f)], "campaign_id": "pod", "creator": "", "title": "Our drone"})
    monkeypatch.setattr(media, "assemble", lambda files, out, work: (out.write_bytes(b"c"), out)[1])
    monkeypatch.setattr(media, "duration", lambda p: 6.0)
    monkeypatch.setattr(media, "render", lambda video_path, out, *a, **k: (
        out.parent.mkdir(parents=True, exist_ok=True), out.write_bytes(b"c"), out)[2])
    assert pipeline.produce(conn, Settings(data_dir=tmp_path), claude, None, {"youtube": FakePub("youtube")}, 2) == 1
    row = conn.execute("SELECT title, description FROM clips").fetchone()
    assert row["title"] == "Our drone" and "#pod" in row["description"] and "#ad" in row["description"].lower()
