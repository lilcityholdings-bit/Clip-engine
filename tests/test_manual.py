import base64
import io
import json
import urllib.error
import urllib.request
from pathlib import Path

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


def test_dashboard_add_a_video_card_is_always_there_and_offers_my_own_clips(server, tmp_path):
    base, _ = server
    page = _get(base + "/", "s3cret").read().decode()
    assert 'id="own-video"' in page and "My own clips (no campaign)" in page and "+ Add a campaign" in page
    assert "Pod Show" not in page
    _campaign(tmp_path)
    page = _get(base + "/", "s3cret").read().decode()
    assert "Add a video" in page and "Pod Show" in page and "My own clips (no campaign)" in page


def test_dashboard_upload_and_link_routes(server, tmp_path, monkeypatch):
    base, conn = server
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
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

    db.put(conn, "paused", "1")
    status, reply = upload("campaign=pod&name=ep1.mp4&title=Ep+1&creator=Host", b"video-bytes")
    assert status == 200 and "uploaded" in reply["message"] and "nothing else to tap" in reply["message"]
    assert db.get(db.connect(tmp_path / "clip_engine.db"), "paused") == "0"  # uploading resumes
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

    from clip_engine import strategy
    monkeypatch.setattr(strategy, "length_range", lambda arms: (20, 58))  # fixtures use long clips
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

    from clip_engine import strategy
    monkeypatch.setattr(strategy, "length_range", lambda arms: (20, 58))  # fixtures use clips up to a minute long
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


def _silent(conn, tmp_path, monkeypatch, extra=None):
    from clip_engine import ai
    monkeypatch.setattr(media, "has_audio", lambda v: False)
    monkeypatch.setattr(media, "scene_cuts", lambda v: [30.0, 60.0, 90.0, 120.0])
    monkeypatch.setattr(media, "frame_at", lambda v, out, at: b"jpeg")
    item = manual.pop(conn)
    manual.add(conn, {**item, "count": 2, **(extra or {})})
    return ai


def test_silent_footage_gets_captions_from_claude_when_you_do_not_write_one(tmp_path, monkeypatch):
    from clip_engine.config import Settings
    from tests.test_core import FakePub

    conn, claude = _produce_fixture(tmp_path, monkeypatch, {}, ["unused"])
    ai = _silent(conn, tmp_path, monkeypatch, {"notes": "show the product"})
    seen = {}
    monkeypatch.setattr(ai, "caption_from_frames", lambda c, frames, brief, notes="": (
        seen.update(n=len(frames), notes=notes), ["A look at the product", "Close up of the box and the cable"][:len(frames)])[1])
    pubs = {"youtube": FakePub("youtube"), "tiktok": FakePub("tiktok")}
    assert pipeline.produce(conn, Settings(data_dir=tmp_path), claude, None, pubs, 2) == 2
    titles = [r[0] for r in conn.execute("SELECT title FROM clips ORDER BY id")]
    assert titles[0].startswith("A look at the product") and len(set(titles)) == 2
    assert seen["n"] == 2 and seen["notes"] == "show the product"


def test_silent_footage_uses_the_captions_you_typed_and_makes_no_more_clips_than_you_wrote(tmp_path, monkeypatch):
    from clip_engine.config import Settings
    from tests.test_core import FakePub

    conn, claude = _produce_fixture(tmp_path, monkeypatch, {}, ["unused"])
    ai = _silent(conn, tmp_path, monkeypatch, {"caption": manual.clean_captions("My own caption\n\n")})
    monkeypatch.setattr(ai, "caption_from_frames", lambda *a, **k: (_ for _ in ()).throw(AssertionError("asked Claude")))
    pubs = {"youtube": FakePub("youtube"), "tiktok": FakePub("tiktok")}
    assert pipeline.produce(conn, Settings(data_dir=tmp_path), claude, None, pubs, 2) == 1
    assert conn.execute("SELECT title FROM clips").fetchone()[0] == "My own caption"


def test_silent_footage_still_gets_clips_when_claude_cannot_caption_it(tmp_path, monkeypatch):
    from clip_engine.config import Settings
    from tests.test_core import FakePub

    conn, claude = _produce_fixture(tmp_path, monkeypatch, {}, ["unused"])
    ai = _silent(conn, tmp_path, monkeypatch)
    monkeypatch.setattr(ai, "caption_from_frames", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("down")))
    pubs = {"youtube": FakePub("youtube"), "tiktok": FakePub("tiktok")}
    assert pipeline.produce(conn, Settings(data_dir=tmp_path), claude, None, pubs, 2) == 2
    assert {r[0] for r in conn.execute("SELECT title FROM clips")} == {"Ep 1 (1)", "Ep 1 (2)"}


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

def test_plan_pieces_lands_between_15_and_30_seconds_or_up_to_a_minute():
    assert media.plan_pieces([(True, 3), (False, 20), (True, 3)]) == [3.0, 20.0, 3.0]  # nothing is cut while it fits
    assert media.plan_pieces([(False, 25)]) == [25.0]  # a single video keeps its whole length
    assert media.plan_pieces([(False, 90)]) == [30.0]  # the usual cap
    assert media.plan_pieces([(False, 90)], budget=media.COMPILATION_MAX_SEC) == [59.0]  # the longer option, under a minute
    over = media.plan_pieces([(True, 3), (False, 40), (True, 3)])  # 46s of material into 30s: shrunk, nothing dropped
    assert len(over) == 3 and sum(over) <= 30.0 and over[1] > over[0]
    many = media.plan_pieces([(True, 3)] * 30)  # 90s of photos: shortened, never below the minimum, the tail dropped
    assert sum(many) <= 30.0 and all(x >= media.MIN_IMAGE_SEC for x in many)
    assert sum(media.plan_pieces([(False, 60)] * 10)) <= 30.0
    assert media.plan_pieces([(True, 3)] * 2) == [5.0, 5.0]  # too little: photos are held longer, up to 5s each
    assert media.plan_pieces([(True, 3), (False, 4)]) == [5.0, 4.0]  # videos are never stretched
    assert media.plan_pieces([(False, 6)]) == [6.0]  # a very short set stays short


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
    assert 13.5 < media.duration(out) < 14.5  # photos held to 5s each to get near 15s, plus the whole 4s video
    photos = media.assemble([tmp_path / "a.jpg", tmp_path / "b.png"], tmp_path / "p.mp4", tmp_path / "w2")
    assert not media.has_audio(photos) and 9.5 < media.duration(photos) < 10.5


def test_dashboard_upload_batch_route_queues_one_project(server, tmp_path, monkeypatch):
    base, _ = server
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
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
    assert manual.clean_length("60") == "60" and manual.clean_length("") == "30" and manual.clean_length("999") == "30"
    conn = db.connect(tmp_path / "clip_engine.db")
    assert manual.queue(conn) == []
    status, reply = upload("campaign=pod&name=b.mp4&batch=batch0001&last=1&title=Drone&length=60&count=3", b"video")
    assert status == 200 and "2 files" in reply["message"] and "3 different videos" in reply["message"]
    item = manual.queue(conn)[0]
    assert item["kind"] == "project" and len(item["files"]) == 2 and item["title"] == "Drone" and item["length"] == "60" and item["count"] == 3
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
    manual.add(conn, {"kind": "project", "files": pieces, "campaign_id": "pod", "creator": "", "title": "Drone",
                      "length": "60"})
    built, budgets = [], []

    def fake_assemble(files, out, work, budget=30.0):
        built.append([f.name for f in files])
        budgets.append(budget)
        out.write_bytes(b"combined")
        return out
    monkeypatch.setattr(media, "assemble", fake_assemble)
    monkeypatch.setattr(media, "duration", lambda p: 14.0)
    monkeypatch.setattr(media, "thumbnail", lambda src, out, photo: b"jpeg")
    from tests.test_core import _claude_returning
    claude = _claude_returning({"order": [1, 0], "dropped": []})  # Claude opens on the video
    monkeypatch.setattr(media, "transcribe", lambda *a, **k: (_ for _ in ()).throw(AssertionError("transcribed")))
    monkeypatch.setattr(media, "plan_layout", lambda *a: (_ for _ in ()).throw(AssertionError("re-framed")))
    rendered = []
    monkeypatch.setattr(media, "render", lambda video_path, out, start, end, fmt, captions, frame=None, **k: (
        rendered.append((start, end, fmt, frame.kind)), out.parent.mkdir(parents=True, exist_ok=True),
        out.write_bytes(b"c"), out)[3])
    pubs = {"youtube": FakePub("youtube"), "tiktok": FakePub("tiktok")}
    assert pipeline.produce(conn, Settings(data_dir=tmp_path), claude, None, pubs, 2) == 1
    assert built == [["b.mp4", "a.jpg"]] and budgets == [59.0]  # in the order Claude chose, for the one-minute option
    assert rendered == [(0.0, 14.0, "short", "blur")]  # the whole video, vertical, not re-framed
    sent = claude.beta.messages.create.call_args.kwargs["messages"][0]["content"]
    assert claude.beta.messages.create.call_count == 1 and sum(b["type"] == "image" for b in sent) == 2
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
    monkeypatch.setattr(media, "assemble", lambda files, out, work, budget=30.0: (out.write_bytes(b"c"), out)[1])
    monkeypatch.setattr(media, "duration", lambda p: 6.0)
    monkeypatch.setattr(media, "render", lambda video_path, out, *a, **k: (
        out.parent.mkdir(parents=True, exist_ok=True), out.write_bytes(b"c"), out)[2])
    assert pipeline.produce(conn, Settings(data_dir=tmp_path), claude, None, {"youtube": FakePub("youtube")}, 2) == 1
    row = conn.execute("SELECT title, description FROM clips").fetchone()
    assert row["title"] == "Our drone" and "#pod" in row["description"] and "#ad" in row["description"].lower()


def test_upload_without_a_claude_key_says_what_is_missing(tmp_path, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    conn = db.connect(tmp_path / "k.db")
    message = dashboard.start_now(conn, __import__("clip_engine.config", fromlist=["Settings"]).Settings(data_dir=tmp_path),
                                  "Video uploaded.")
    assert "Claude API key" in message and db.get(conn, "run_now") is None


def test_arrange_drops_unusable_pieces_and_falls_back_to_upload_order(tmp_path, monkeypatch):
    from tests.test_core import _claude_returning
    camp = campaigns.Campaign(id="r", name="Ring", program_url="https://w", rate_per_1k_views=1, sources=[])
    files = [tmp_path / n for n in ("a.jpg", "b.jpg", "c.mp4")]
    monkeypatch.setattr(media, "thumbnail", lambda src, out, photo: b"jpeg")
    monkeypatch.setattr(media, "duration", lambda p: 8.0)
    claude = _claude_returning({"order": [2, 0, 2, 9], "dropped": [{"piece": 1, "why": "blurry"}]})
    assert pipeline.arrange(claude, files, camp, tmp_path / "w") == [files[2], files[0]]  # repeats and bad numbers ignored
    assert pipeline.arrange(_claude_returning({"order": [], "dropped": []}), files, camp, tmp_path / "w") == files
    broken = _claude_returning({})
    broken.beta.messages.create.side_effect = RuntimeError("API down")
    assert pipeline.arrange(broken, files, camp, tmp_path / "w") == files  # a video is still made


def test_thumbnail_of_a_real_photo_and_video(tmp_path):
    import subprocess
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "testsrc2=s=1600x1200:d=1", "-frames:v", "1",
                    str(tmp_path / "a.jpg")], check=True)
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "testsrc=s=1280x720:d=2:r=24",
                    str(tmp_path / "v.mp4")], check=True)
    for src, photo in ((tmp_path / "a.jpg", True), (tmp_path / "v.mp4", False)):
        data = media.thumbnail(src, tmp_path / f"t-{src.stem}.jpg", photo)
        assert data[:2] == b"\xff\xd8" and media._dimensions(tmp_path / f"t-{src.stem}.jpg")[0] <= 512


# --- a campaign that wants the disclosure alone on the line after the caption ---------------------

def test_disclosed_caption_layout_and_form_field():
    cap = campaigns.disclosed_caption("Heismay in action #Fun @sega  now", ["#Ad", "#gaming", "#jrpg", "#sega", "#extra"], "#Ad")
    assert cap == "Heismay in action now\n#Ad\n#gaming #jrpg"  # #Ad alone and first; at most 2 more; text has no tags
    assert campaigns.disclosed_caption("Just text", [], "#Ad") == "Just text\n#Ad"
    form = {"name": "Sega", "program_url": "https://w", "rate": "1", "disclosure_line": " #Ad "}
    assert campaigns.from_form(form, ["tiktok"]).disclosure_line == "#Ad"
    with pytest.raises(campaigns.CampaignError, match="single hashtag"):
        campaigns.from_form({**form, "disclosure_line": "#Ad #more"}, ["tiktok"])
    assert campaigns.from_form({k: v for k, v in form.items() if k != "disclosure_line"}, ["tiktok"]).disclosure_line == ""
    from clip_engine import platforms
    clip = {"title": "Heismay in action", "description": cap}
    assert platforms.caption_for({"title": "Heismay in action", "description": "Heismay in action\n#Ad\n#gaming"}) == \
        "Heismay in action\n#Ad\n#gaming"  # not doubled
    assert platforms.caption_for({"title": "A", "description": "B"}) == "A\n\nB"  # other campaigns unchanged


def test_clips_under_a_disclosure_line_campaign_have_nothing_else_in_the_caption(tmp_path, monkeypatch):
    from clip_engine.config import Settings
    from tests.test_core import FakePub

    conn, claude = _produce_fixture(tmp_path, monkeypatch, {"disclosure_line": "#Ad"}, ["Heismay in action #Fun"])
    pubs = {"youtube": FakePub("youtube"), "tiktok": FakePub("tiktok")}
    assert pipeline.produce(conn, Settings(data_dir=tmp_path), claude, None, pubs, 1) >= 1
    row = conn.execute("SELECT title, description FROM clips ORDER BY id LIMIT 1").fetchone()
    assert row["title"] == "Heismay in action"
    assert row["description"] == "Heismay in action\n#Ad\n#podcast"  # no credit line, no #Shorts, no link


# --- a campaign that bans paid promotion: no paid-promotion switch, no added sound, no more than 5 clips per video ---

def test_paid_label_is_a_campaign_choice(tmp_path):
    form = {"name": "Sega", "program_url": "https://w", "rate": "1"}
    assert campaigns.from_form({**form, "paid_label": "on"}, ["tiktok"]).paid_label is True
    assert campaigns.from_form(form, ["tiktok"]).paid_label is False  # an unticked box is not sent
    assert campaigns.Campaign(id="a", name="a", program_url="u", rate_per_1k_views=1, sources=[]).paid_label is True


def test_publishing_follows_the_campaigns_paid_label_choice(tmp_path):
    from clip_engine.config import Settings
    from tests.test_core import FakePub
    settings = Settings(data_dir=tmp_path)
    conn = db.connect(tmp_path / "p.db")
    campaigns.save(tmp_path, campaigns.Campaign(id="ring", name="Ring", program_url="https://w", rate_per_1k_views=1, sources=[]))
    campaigns.save(tmp_path, campaigns.Campaign(id="sega", name="Sega", program_url="https://w", rate_per_1k_views=1,
                                                sources=[], paid_label=False))
    video = tmp_path / "c.mp4"
    video.write_bytes(b"x")
    for cid in ("ring", "sega"):
        clip = conn.execute("INSERT INTO clips (source_identifier, campaign_id, start_sec, end_sec, title, description,"
                            " tags, arms, file_path, media_token) VALUES ('s', ?, 0, 1, 't', 'd', '[]', '{}', ?, ?)",
                            (cid, str(video), cid * 8)).lastrowid
        conn.execute("INSERT INTO posts (clip_id, platform, status, scheduled_at) VALUES (?, 'youtube', 'queued',"
                     " datetime('now'))", (clip,))
    conn.commit()
    pub = FakePub("youtube")
    assert pipeline.publish_due(conn, settings, {"youtube": pub}) == 2
    flags = {c["campaign_id"]: c["paid_label"] for c, _, _ in pub.published}
    assert flags == {"ring": True, "sega": False}


def test_hand_posting_tips_follow_the_campaign_rules():
    ring = campaigns.Campaign(id="r", name="R", program_url="u", rate_per_1k_views=1, sources=[])
    sega = campaigns.Campaign(id="s", name="S", program_url="u", rate_per_1k_views=1, sources=[], allow_music=False,
                              paid_label=False)
    assert "trending sound" in dashboard.app_tip("tiktok", ring) and "Paid partnership" in dashboard.app_tip("tiktok", ring)
    tip = dashboard.app_tip("tiktok", sega)
    assert "Don't add any sound" in tip and "Don't switch on any paid-partnership" in tip and "Paid partnership)" not in tip
    assert "trending sound" in dashboard.app_tip("tiktok", None) and dashboard.app_tip("youtube", sega).startswith("YouTube app")


def test_one_upload_makes_two_to_four_clips():
    assert pipeline.MIN_CLIPS_PER_UPLOAD == 2 and pipeline.MAX_CLIPS_PER_UPLOAD == 4 == manual.MAX_VIDEOS


# --- several different videos from one set of files ---------------------------------------------------------

def test_split_evenly_keeps_order_and_balances_length():
    assert pipeline.split_evenly([3, 3, 3, 3, 3, 3], 2) == [[0, 1, 2], [3, 4, 5]]
    assert pipeline.split_evenly([3, 3, 3, 3, 3, 3], 3) == [[0, 1], [2, 3], [4, 5]]
    assert pipeline.split_evenly([20, 3, 3, 3, 3, 20], 2) == [[0, 1, 2], [3, 4, 5]]
    assert pipeline.split_evenly([3, 3], 5) == [[0], [1]]  # never more videos than files
    assert pipeline.split_evenly([5], 1) == [[0]]
    assert manual.clean_count("3") == 3 and manual.clean_count("99") == manual.MAX_VIDEOS and manual.clean_count("x") == 1
    assert manual.clean_count("0") == 1


def _files(tmp_path, names):
    out = []
    for n in names:
        f = tmp_path / n
        f.write_bytes(b"x")
        out.append(f)
    return out


def test_claude_splits_the_files_into_different_videos_with_their_own_captions(tmp_path, monkeypatch):
    from tests.test_core import _claude_returning
    camp = campaigns.Campaign(id="r", name="Ring", program_url="https://w", rate_per_1k_views=1, sources=[])
    files = _files(tmp_path, ["a.jpg", "b.jpg", "c.jpg", "d.mp4", "e.jpg"])
    monkeypatch.setattr(media, "thumbnail", lambda src, out, photo: b"jpeg")
    monkeypatch.setattr(media, "duration", lambda p: 12.0)
    claude = _claude_returning({"videos": [
        {"pieces": [3, 0, 3, 99], "title": "The drone at night #wow"},   # repeats and unknown pieces are ignored
        {"pieces": [4, 1, 0], "title": "  "},                           # 0 is already used; a blank caption gets a numbered one
        {"pieces": [2], "title": "A third video"}], "dropped": []})
    groups = pipeline.arrange_groups(claude, files, camp, tmp_path / "w", 3, "Drone")
    assert [[f.name for f in g] for g, _ in groups] == [["d.mp4", "a.jpg"], ["e.jpg", "b.jpg"], ["c.jpg"]]
    assert [t for _, t in groups] == ["The drone at night #wow", "Drone 2", "A third video"]
    assert claude.beta.messages.create.call_count == 1
    sent = claude.beta.messages.create.call_args.kwargs["messages"][0]["content"]
    assert sum(b["type"] == "image" for b in sent) == 5 and "exactly 3" in sent[0]["text"]
    # asking for fewer than Claude returns keeps only what was asked
    claude2 = _claude_returning({"videos": [{"pieces": [0], "title": "x"}, {"pieces": [1], "title": "y"}], "dropped": []})
    assert len(pipeline.arrange_groups(claude2, files, camp, tmp_path / "w", 1, "Drone")) == 1


def test_splitting_falls_back_to_an_even_split_when_claude_cannot_help(tmp_path, monkeypatch):
    from tests.test_core import _claude_returning
    camp = campaigns.Campaign(id="r", name="Ring", program_url="https://w", rate_per_1k_views=1, sources=[])
    files = _files(tmp_path, ["a.jpg", "b.jpg", "c.jpg", "d.jpg"])
    monkeypatch.setattr(media, "thumbnail", lambda src, out, photo: b"jpeg")
    broken = _claude_returning({})
    broken.beta.messages.create.side_effect = RuntimeError("API down")
    for claude in (broken, _claude_returning({"videos": [], "dropped": []}), None):
        groups = pipeline.arrange_groups(claude, files, camp, tmp_path / "w", 2, "Drone")
        assert [[f.name for f in g] for g, _ in groups] == [["a.jpg", "b.jpg"], ["c.jpg", "d.jpg"]]
        assert [t for _, t in groups] == ["Drone 1", "Drone 2"]


def test_assemble_many_makes_separate_videos_in_one_working_file(tmp_path):
    import subprocess
    def ff(*args):
        subprocess.run(["ffmpeg", "-y", "-v", "error", *args], check=True)
    for n in ("a", "b", "c", "d"):
        ff("-f", "lavfi", "-i", f"testsrc2=s=1200x900:d=1", "-frames:v", "1", str(tmp_path / f"{n}.jpg"))
    ff("-f", "lavfi", "-i", "testsrc=s=1280x720:d=4:r=24", "-f", "lavfi", "-i", "sine=f=440:d=4", "-shortest",
       str(tmp_path / "v.mp4"))
    groups = [[tmp_path / "a.jpg", tmp_path / "b.jpg"], [tmp_path / "v.mp4", tmp_path / "c.jpg", tmp_path / "d.jpg"]]
    out, windows = media.assemble_many(groups, tmp_path / "all.mp4", tmp_path / "w")
    assert media._dimensions(out) == (1080, 1920) and media.has_audio(out)  # one soundtrack setting for both videos
    assert len(windows) == 2 and windows[0][0] == 0 and abs(windows[0][1] - windows[1][0]) < 0.01
    assert windows[0][1] - windows[0][0] >= 9.5  # two photos held long to get near 15s
    assert abs(windows[1][1] - media.duration(out)) < 0.3


def test_produce_makes_each_built_video_its_own_clip_and_keeps_to_the_daily_limit(tmp_path, monkeypatch):
    from clip_engine.config import Settings
    from tests.test_core import FakePub, _claude_returning

    conn, claude = _produce_fixture(tmp_path, monkeypatch, {"disclosure_line": "#Ad"}, ["unused"])
    db.put(conn, manual.QUEUE_KEY, "[]")
    pieces = []
    for name in ("a.jpg", "b.jpg", "c.jpg", "d.jpg", "e.jpg", "f.jpg"):
        f = tmp_path / "uploads" / name
        f.write_bytes(b"x")
        pieces.append(str(f))
    manual.add(conn, {"kind": "project", "files": pieces, "campaign_id": "pod", "creator": "", "title": "Drone",
                      "count": 5, "length": "30"})
    seen = {}

    def fake_many(groups, out, work, budget=30.0):
        seen["groups"] = [[p.name for p in g] for g in groups]
        out.write_bytes(b"combined")
        return out, [(0.0, 20.0), (20.0, 42.0), (42.0, 61.0), (61.0, 80.0), (80.0, 99.0)]
    monkeypatch.setattr(media, "assemble_many", fake_many)
    monkeypatch.setattr(media, "thumbnail", lambda src, out, photo: b"jpeg")
    monkeypatch.setattr(media, "duration", lambda p: 99.0)
    claude = _claude_returning({"videos": [{"pieces": [i], "title": f"Look number {i} at the drone"} for i in range(5)],
                                "dropped": []})
    rendered = []
    monkeypatch.setattr(media, "render", lambda video_path, out, start, end, fmt, captions, frame=None, **k: (
        rendered.append((start, end, frame.kind)), out.parent.mkdir(parents=True, exist_ok=True),
        out.write_bytes(b"c"), out)[3])
    made = pipeline.produce(conn, Settings(data_dir=tmp_path), claude, None, {"youtube": FakePub("youtube")}, 5)
    assert made == 4  # five were asked for, but the gradual start allows 4 today
    assert [r[:2] for r in rendered] == [(0.0, 20.0), (20.0, 42.0), (42.0, 61.0), (61.0, 80.0)] and {r[2] for r in rendered} == {"blur"}
    titles = [r["title"] for r in conn.execute("SELECT title FROM clips ORDER BY id")]
    assert titles == [f"Look number {i} at the drone" for i in range(4)]  # different captions, none dropped as repeats
    descriptions = [r["description"] for r in conn.execute("SELECT description FROM clips ORDER BY id")]
    assert all(d.startswith(t + "\n#Ad") for d, t in zip(descriptions, titles))


def test_strict_caption_campaigns_tell_claude_to_stay_plain_and_true():
    plain = campaigns.Campaign(id="a", name="a", program_url="u", rate_per_1k_views=1, sources=[], rules="No gore.")
    strict = campaigns.Campaign(id="b", name="b", program_url="u", rate_per_1k_views=1, sources=[], rules="No gore.",
                                disclosure_line="#Ad")
    assert pipeline.caption_rules(plain) == "No gore." and pipeline.caption_rules(None) == ""
    assert pipeline.caption_rules(strict).startswith("No gore.") and "Quote only words" in pipeline.caption_rules(strict)


# --- posting a clip again -------------------------------------------------------------------------------

def _posted_clip(conn, tmp_path, platforms=("tiktok",), campaign="pod", created="now"):
    f = tmp_path / "clip.mp4"
    f.write_bytes(b"v")
    cid = conn.execute(
        "INSERT INTO clips (source_identifier, campaign_id, start_sec, end_sec, title, description, tags, arms, file_path,"
        " media_token, created_at) VALUES ('s', ?, 0, 1, 'Heismay clip', 'd', '[]', '{}', ?, ?, datetime('now', ?))",
        (campaign, str(f), "t" * 32, "-0 days" if created == "now" else created)).lastrowid
    for p in platforms:
        conn.execute("INSERT INTO posts (clip_id, platform, status, scheduled_at, url) VALUES (?, ?, 'posted', datetime('now'),"
                     " 'https://x')", (cid, p))
    conn.commit()
    return cid, f


def test_a_posted_campaign_clip_is_kept_for_two_weeks_then_deleted(tmp_path):
    conn = db.connect(tmp_path / "r.db")
    cid, f = _posted_clip(conn, tmp_path)
    pipeline.cleanup_files(conn)
    assert f.exists()  # kept so it can be posted again
    conn.execute("UPDATE clips SET created_at = datetime('now', '-15 days') WHERE id = ?", (cid,))
    pipeline.cleanup_files(conn)
    assert not f.exists()
    cid2, f2 = _posted_clip(conn, tmp_path, campaign=None)  # not a campaign clip: nothing to resubmit
    pipeline.cleanup_files(conn)
    assert not f2.exists()
    cid3, f3 = _posted_clip(conn, tmp_path)
    conn.execute("UPDATE posts SET status = 'rejected' WHERE clip_id = ?", (cid3,))
    pipeline.cleanup_files(conn)
    assert not f3.exists()  # never posted: deleted at once


def test_repost_puts_the_clip_back_under_ready_to_post_up_to_five_times(tmp_path):
    conn = db.connect(tmp_path / "r.db")
    db.put(conn, "auto_platforms", "youtube")
    cid, f = _posted_clip(conn, tmp_path, platforms=("youtube", "tiktok"))
    [c] = pipeline.repostable(conn)
    assert c["platforms"] == ["youtube", "tiktok"] and c["left"] == 3
    assert pipeline.repost(conn, cid, "tiktok") == "ready"
    assert [x["platform"] for x in pipeline.ready_to_post(conn)[0]["posts"]] == ["tiktok"]
    assert pipeline.repostable(conn)[0]["platforms"] == ["youtube"]  # tiktok already has one waiting
    with pytest.raises(ValueError, match="can't be posted again"):
        pipeline.repost(conn, cid, "tiktok")
    assert pipeline.repost(conn, cid, "youtube") == "queued"  # an automatic account posts it by itself
    [q] = pipeline.scheduled_posts(conn)
    assert q["platform"] == "youtube"
    conn.execute("UPDATE posts SET status = 'posted' WHERE status IN ('ready', 'queued')")
    conn.commit()
    assert pipeline.repost(conn, cid, "tiktok") == "ready"  # the 5th post of this clip
    assert pipeline.repostable(conn) == []  # that is the limit
    f.unlink()
    conn.execute("UPDATE posts SET status = 'posted'")
    assert pipeline.repostable(conn) == []  # and a clip whose video is gone can't be reposted


def test_post_again_section_and_route(server, tmp_path, monkeypatch):
    base, conn = server
    _campaign(tmp_path)
    cid, f = _posted_clip(conn, tmp_path)
    page = _get(base + "/", "s3cret").read().decode()
    assert "Post a clip again" in page and "Post again on TikTok" in page and "30 minutes" in page
    page = _get(base + "/repost", "s3cret", data=f"id={cid}&platform=tiktok".encode()).read().decode()
    assert "Post it on TikTok" in page and "Paste your TikTok post link" in page  # it lands on the clip's own page
    bad = _get(base + "/repost", "s3cret", data=f"id={cid}&platform=tiktok".encode()).read().decode()
    assert "be posted again" in bad


def test_an_old_database_is_migrated_to_allow_reposting_without_losing_posts(tmp_path):
    import sqlite3
    path = tmp_path / "old.db"
    old = sqlite3.connect(path)
    old.executescript("""
        CREATE TABLE clips (id INTEGER PRIMARY KEY);
        CREATE TABLE posts (
            id INTEGER PRIMARY KEY, clip_id INTEGER NOT NULL REFERENCES clips(id), platform TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'queued', scheduled_at TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
            external_id TEXT, url TEXT, error TEXT, posted_at TEXT, views INTEGER, submitted_at TEXT,
            UNIQUE (clip_id, platform));
        INSERT INTO clips (id) VALUES (1);
        INSERT INTO posts (id, clip_id, platform, status, scheduled_at, url, views)
            VALUES (7, 1, 'tiktok', 'posted', '2026-11-16 12:00:00', 'https://tiktok.com/t/abc', 18);
    """)
    old.commit()
    old.close()
    conn = db.connect(path)
    assert [tuple(r) for r in conn.execute("SELECT id, platform, status, url, views FROM posts")] == \
        [(7, "tiktok", "posted", "https://tiktok.com/t/abc", 18)]
    conn.execute("INSERT INTO posts (clip_id, platform, status, scheduled_at) VALUES (1, 'tiktok', 'ready', 'now')")  # again
    conn.commit()
    with pytest.raises(sqlite3.IntegrityError):  # but only one open post at a time
        conn.execute("INSERT INTO posts (clip_id, platform, status, scheduled_at) VALUES (1, 'tiktok', 'queued', 'now')")
    conn.rollback()
    assert db.connect(path).execute("SELECT COUNT(*) FROM posts").fetchone()[0] == 2  # reconnecting changes nothing


# --- skipping a campaign's intro and outro cards -------------------------------------------------------------

def test_trim_fields_in_the_campaign_form():
    form = {"name": "Sega", "program_url": "https://w", "rate": "1", "trim_start": " 5 ", "trim_end": "3.5"}
    camp = campaigns.from_form(form, ["tiktok"])
    assert camp.trim_start == 5.0 and camp.trim_end == 3.5
    assert campaigns.from_form({k: v for k, v in form.items() if not k.startswith("trim")}, ["tiktok"]).trim_start == 0.0
    for bad in ("x", "-1", "61"):
        with pytest.raises(campaigns.CampaignError, match="Skip at the start"):
            campaigns.from_form({**form, "trim_start": bad}, ["tiktok"])


def _trim_run(tmp_path, monkeypatch, campaign_kwargs, picks):
    import json
    from clip_engine.config import Settings
    from tests.test_core import FakePub

    conn, claude = _produce_fixture(tmp_path, monkeypatch, campaign_kwargs, ["unused"])
    from clip_engine import strategy
    monkeypatch.setattr(strategy, "length_range", lambda arms: (20, 58))  # fixtures use clips up to a minute long
    monkeypatch.setattr(media, "duration", lambda p: 100.0)
    rendered = []
    monkeypatch.setattr(media, "render", lambda video_path, out, start, end, fmt, captions, **k: (
        rendered.append((start, end)), out.parent.mkdir(parents=True, exist_ok=True), out.write_bytes(b"c"), out)[3])
    claude.beta.messages.create.return_value.content[0].text = json.dumps({"clips": [
        {"start": a, "end": b, "title": t, "description": "d", "tags": [], "hook_text": "", "hashtags": [],
         "score": 8, "why": ""} for a, b, t in picks]})
    monkeypatch.setattr(media, "transcribe", lambda v, m, windows=None: (  # speech covers the whole video
        [{"start": 0, "end": 100, "text": "hi", "window": 0, "energy": 2.0}],
        [{"start": 0.5 + i * 0.8, "end": 1.0 + i * 0.8, "word": f" w{i}"} for i in range(120)]))
    pipeline.produce(conn, Settings(data_dir=tmp_path), claude, None, {"youtube": FakePub("youtube")}, 4)
    return rendered


def test_clips_keep_clear_of_the_skipped_start_and_end(tmp_path, monkeypatch):
    rendered = _trim_run(tmp_path, monkeypatch, {"trim_start": 5.0, "trim_end": 4.0},
                         [(0, 40, "Opens on a rating card"), (55, 99, "Ends near a studio logo")])
    assert len(rendered) == 2 and all(a >= 4.7 and b <= 96.5 for a, b in rendered)
    assert rendered[0][0] > 4.7 and rendered[1][1] < 96.5  # the first clip lost its start, the second its end


def test_a_clip_left_too_short_by_the_skip_is_dropped(tmp_path, monkeypatch):
    rendered = _trim_run(tmp_path, monkeypatch, {"trim_start": 38.0}, [(0, 40, "Almost all of it is skipped")])
    assert rendered == []  # only 2 seconds would be left


# --- two to four clips from one upload, posted through the day --------------------------------------------

def test_pick_clips_gives_at_least_the_wanted_number_by_taking_the_next_best():
    from clip_engine import ai
    from tests.test_core import _claude_returning
    clips = [{"start": 100 * i, "end": 100 * i + 30, "title": f"t{i}", "description": "d", "tags": [], "hook_text": "h",
              "hashtags": [], "score": sc, "why": ""} for i, sc in enumerate([8, 5, 4, 2])]
    seg = [{"start": 0, "end": 5, "text": "hi"}]
    kw = dict(source_title="T", fmt="short", min_sec=20, max_sec=58, title_style="question", count=4)
    assert [c["score"] for c in ai.pick_clips(_claude_returning({"clips": clips}), seg, **kw)] == [8]  # strong ones only
    client = _claude_returning({"clips": clips})
    assert [c["score"] for c in ai.pick_clips(client, seg, want=2, **kw)] == [8, 5]  # the next best fills in
    assert [c["score"] for c in ai.pick_clips(_claude_returning({"clips": clips}), seg, want=3, **kw)] == [8, 5, 4]
    assert "at least 2 different moments" in client.beta.messages.create.call_args.kwargs["messages"][0]["content"]
    plain = _claude_returning({"clips": clips})
    ai.pick_clips(plain, seg, **kw)
    assert "at least" not in plain.beta.messages.create.call_args.kwargs["messages"][0]["content"]


def test_slot_at_spreads_a_batch_through_the_day_and_skips_the_night():
    from datetime import datetime, timezone
    from zoneinfo import ZoneInfo
    from clip_engine.platforms import slot_at
    et = ZoneInfo("America/New_York")
    first = datetime(2026, 11, 17, 14, 0, tzinfo=timezone.utc)  # 9am Eastern
    assert [slot_at(first, i).astimezone(et).hour for i in range(4)] == [9, 12, 15, 18]
    late = datetime(2026, 11, 17, 1, 0, tzinfo=timezone.utc)  # 8pm Eastern the evening before
    got = [slot_at(late, i).astimezone(et) for i in range(3)]
    assert got[0].hour == 20 and (got[1].day, got[1].hour) == (got[0].day + 1, 9) and got[2].hour == 12  # 11pm waits for 9am


def test_an_upload_makes_distinct_clips_that_post_three_hours_apart(tmp_path, monkeypatch):
    from datetime import datetime
    from clip_engine.config import Settings
    from tests.test_core import FakePub

    titles = ["Heismay steps into the light", "A quiet look at the city", "The party gathers at night"]
    conn, claude = _produce_fixture(tmp_path, monkeypatch, {}, titles)
    assert pipeline.produce(conn, Settings(data_dir=tmp_path), claude, None,
                            {"youtube": FakePub("youtube"), "tiktok": FakePub("tiktok")}, 2) == 3
    asked = claude.beta.messages.create.call_args.kwargs["messages"][0]["content"]
    assert "at least 2 different moments (up to 4)" in asked  # an uploaded video is asked for 2 to 4 clips
    rows = conn.execute("SELECT scheduled_at FROM posts WHERE platform = 'youtube' ORDER BY clip_id").fetchall()
    times = [datetime.fromisoformat(r["scheduled_at"]) for r in rows]
    gaps = [(b - a).total_seconds() / 3600 for a, b in zip(times, times[1:])]
    assert len(times) == 3 and all(2.9 <= g <= 24 for g in gaps) and times == sorted(times)


# --- every clip different; YouTube posting time chosen by the owner -------------------------------------------

def test_overlap_helpers():
    from clip_engine import moments
    assert moments.overlap((0, 30), (20, 50)) == 10 and moments.overlap((0, 30), (30, 60)) == 0
    assert moments.overlap({"start": 5, "end": 10}, (0, 100)) == 5
    assert moments.is_distinct((30, 60), [(0, 30.4)]) and not moments.is_distinct((25, 60), [(0, 30)])


def test_pick_clips_never_returns_two_clips_sharing_footage():
    from clip_engine import ai
    from tests.test_core import _claude_returning
    mk = lambda a, b, sc, t: {"start": a, "end": b, "title": t, "description": "d", "tags": [], "hook_text": "h",
                              "hashtags": [], "score": sc, "why": ""}
    clips = [mk(0, 30, 9, "best"), mk(10, 40, 8, "overlaps the best"), mk(60, 90, 7, "separate"), mk(80, 110, 5, "spare overlap")]
    seg = [{"start": 0, "end": 5, "text": "hi"}]
    got = ai.pick_clips(_claude_returning({"clips": clips}), seg, source_title="T", fmt="short", min_sec=20, max_sec=58,
                        title_style="question", count=4, want=3)
    assert [c["title"] for c in got] == ["best", "separate"]  # the overlapping ones are left out, even when more are wanted


def test_produce_skips_a_clip_that_repeats_footage_already_made(tmp_path, monkeypatch):
    from clip_engine import ai
    from clip_engine.config import Settings
    from tests.test_core import FakePub

    conn, claude = _produce_fixture(tmp_path, monkeypatch, {}, ["unused"])
    monkeypatch.setattr(ai, "pick_clips", lambda *a, **k: [  # a model that ignores the instruction
        {"start": 1, "end": 37, "title": "First look at the city", "description": "d", "tags": [], "hook_text": "",
         "hashtags": [], "score": 8}, {"start": 20, "end": 56, "title": "A quieter moment later", "description": "d",
                                       "tags": [], "hook_text": "", "hashtags": [], "score": 8},
        {"start": 60, "end": 96, "title": "The party gathers", "description": "d", "tags": [], "hook_text": "",
         "hashtags": [], "score": 8}])
    rendered = []
    monkeypatch.setattr(media, "render", lambda video_path, out, start, end, fmt, captions, **k: (
        rendered.append((start, end)), out.parent.mkdir(parents=True, exist_ok=True), out.write_bytes(b"c"), out)[3])
    assert pipeline.produce(conn, Settings(data_dir=tmp_path), claude, None, {"youtube": FakePub("youtube")}, 3) == 2
    assert len(rendered) == 2 and rendered[1][0] >= rendered[0][1] - 0.5  # the overlapping middle clip was dropped


def test_parse_local_time_and_reschedule(tmp_path):
    from datetime import datetime, timedelta, timezone
    from zoneinfo import ZoneInfo
    et = ZoneInfo("America/New_York")
    now = datetime(2026, 11, 17, 12, 0, tzinfo=timezone.utc)  # 7am Eastern
    assert pipeline.parse_local_time("2026-11-17T15:30", now) == "2026-11-17 20:30:00"  # 3:30pm ET is 20:30 UTC in November
    assert pipeline.parse_local_time("2026-11-17T05:00", now) == "2026-11-17 12:00:00"  # in the past means now
    for bad in ("", "tomorrow", "2026-13-40T99:00", "2027-02-01T10:00"):
        with pytest.raises(ValueError):
            pipeline.parse_local_time(bad, now)
    conn = db.connect(tmp_path / "s.db")
    cid, _ = _posted_clip(conn, tmp_path, platforms=("youtube",))
    conn.execute("UPDATE posts SET status = 'queued'")
    conn.commit()
    pid = conn.execute("SELECT id FROM posts").fetchone()[0]
    pipeline.reschedule(conn, pid, "2026-11-20 20:30:00")
    assert conn.execute("SELECT scheduled_at FROM posts").fetchone()[0] == "2026-11-20 20:30:00"
    conn.execute("UPDATE posts SET status = 'posted'")
    pipeline.reschedule(conn, pid, "2026-11-25 10:00:00")  # a post that already went out is left alone
    assert conn.execute("SELECT scheduled_at FROM posts").fetchone()[0] == "2026-11-20 20:30:00"


def test_dashboard_lets_you_pick_the_youtube_time_when_approving_and_afterwards(server, tmp_path):
    from datetime import datetime, timedelta, timezone
    from zoneinfo import ZoneInfo
    base, conn = server
    _campaign(tmp_path)
    db.put(conn, "auto_platforms", "youtube")
    f = tmp_path / "clip.mp4"
    f.write_bytes(b"v")
    cid = conn.execute(
        "INSERT INTO clips (source_identifier, campaign_id, start_sec, end_sec, title, description, tags, arms, file_path,"
        " media_token) VALUES ('s', 'pod', 0, 30, 'Heismay clip', 'd', '[]', '{}', ?, ?)", (str(f), "m" * 32)).lastrowid
    for p in ("youtube", "tiktok"):
        conn.execute("INSERT INTO posts (clip_id, platform, status, scheduled_at) VALUES (?, ?, 'review', datetime('now'))",
                     (cid, p))
    conn.commit()
    page = _get(base + "/", "s3cret").read().decode()
    assert page.count('type="datetime-local"') == 1 and "YouTube posts at" in page  # only the automatic account asks for a time
    target = (datetime.now(ZoneInfo("America/New_York")) + timedelta(days=3)).replace(hour=15, minute=30, second=0, microsecond=0)
    want = target.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    _get(base + "/review/approve", "s3cret", data=(
        f"id={cid}&choose=1&platforms=youtube&platforms=tiktok&at={target.strftime('%Y-%m-%dT%H:%M')}").encode())
    rows = {r["platform"]: (r["status"], r["scheduled_at"]) for r in conn.execute("SELECT * FROM posts")}
    assert rows["youtube"] == ("queued", want) and rows["tiktok"][0] == "ready"
    page = _get(base + "/", "s3cret").read().decode()
    assert "Set time" in page and "Posting automatically" in page
    later = (target + timedelta(hours=4)).strftime("%Y-%m-%dT%H:%M")
    pid = conn.execute("SELECT id FROM posts WHERE platform = 'youtube'").fetchone()[0]
    page = _get(base + "/post/schedule", "s3cret", data=f"id={pid}&at={later}".encode()).read().decode()
    assert "It will post at" in page
    assert conn.execute("SELECT scheduled_at FROM posts WHERE platform = 'youtube'").fetchone()[0] == \
        (target + timedelta(hours=4)).astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    bad = _get(base + "/post/schedule", "s3cret", data=f"id={pid}&at=nonsense".encode()).read().decode()
    assert "Pick a date and a time" in bad


# --- choose how many clips; watch each before it can post; download or delete ---------------------------------

def test_upload_and_link_forms_carry_the_clip_count_and_the_review_choice(server, tmp_path, monkeypatch):
    base, conn = server
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    _campaign(tmp_path)

    def upload(query, body=b"video"):
        req = urllib.request.Request(f"{base}/manual-video/upload?{query}", data=body, method="POST")
        req.add_header("Origin", base)
        req.add_header("Authorization", "Basic " + base64.b64encode(b"me:s3cret").decode())
        return urllib.request.urlopen(req, timeout=5).status

    assert upload("campaign=pod&name=a.mp4&count=3&review=1") == 200
    assert upload("campaign=pod&name=b.mp4&count=99") == 200  # capped at 4; review not ticked
    first, second = manual.queue(db.connect(tmp_path / "clip_engine.db"))
    assert first["count"] == 3 and first["review"] is True
    assert second["count"] == 4 and second["review"] is False
    _get(base + "/manual-video/link", "s3cret", data=b"campaign=pod&url=https%3A%2F%2Fdrive.google.com%2Ffile%2Fd%2F1&count=2&review=on")
    link = manual.queue(db.connect(tmp_path / "clip_engine.db"))[-1]
    assert link["count"] == 2 and link["review"] is True
    page = _get(base + "/", "s3cret").read().decode()
    assert "How many clips do you want?" in page and "Let me watch each clip before it can post" in page
    assert '<option value="4">' in page and '<option value="5">' not in page


def test_an_upload_makes_exactly_the_number_of_clips_asked_for(tmp_path, monkeypatch):
    from clip_engine.config import Settings
    from tests.test_core import FakePub

    titles = ["Heismay steps into the light", "A quiet look at the city", "The party gathers at night", "A fourth place"]
    conn, claude = _produce_fixture(tmp_path, monkeypatch, {}, titles)
    item = manual.pop(conn)
    manual.add(conn, {**item, "count": 3})
    produced = pipeline.produce(conn, Settings(data_dir=tmp_path), claude, None, {"youtube": FakePub("youtube")}, 2)
    asked = claude.beta.messages.create.call_args.kwargs["messages"][0]["content"]
    assert "at least 3 different moments (up to 3)" in asked and produced == 3
    conn2, claude2 = _produce_fixture(tmp_path / "one", monkeypatch, {}, titles) if (tmp_path / "one").mkdir() is None else (None, None)
    item = manual.pop(conn2)
    manual.add(conn2, {**item, "count": 1})
    assert pipeline.produce(conn2, Settings(data_dir=tmp_path / "one"), claude2, None, {"youtube": FakePub("youtube")}, 2) == 1
    assert "Pick up to 1 moments" in claude2.beta.messages.create.call_args.kwargs["messages"][0]["content"]


def test_an_upload_can_ask_for_review_even_when_the_global_setting_is_off(tmp_path, monkeypatch):
    from clip_engine.config import Settings
    from tests.test_core import FakePub

    for review, expected in ((True, "review"), (False, "queued")):
        folder = tmp_path / str(review)
        folder.mkdir()
        conn, claude = _produce_fixture(folder, monkeypatch, {"platforms": ["youtube"]} if False else {}, ["Heismay steps into the light"])
        db.put(conn, "review_mode", "off")
        db.put(conn, "auto_platforms", "youtube")
        item = manual.pop(conn)
        manual.add(conn, {**item, "count": 1, "review": review})
        pipeline.produce(conn, Settings(data_dir=folder), claude, None, {"youtube": FakePub("youtube")}, 2)
        assert {r["status"] for r in conn.execute("SELECT status FROM posts WHERE platform = 'youtube'")} == {expected}


def test_delete_clip_removes_unposted_clips_but_not_posts_that_went_out(tmp_path):
    conn = db.connect(tmp_path / "d.db")
    f = tmp_path / "c.mp4"
    f.write_bytes(b"v")
    cid = conn.execute(
        "INSERT INTO clips (source_identifier, campaign_id, start_sec, end_sec, title, description, tags, arms, file_path,"
        " media_token) VALUES ('s', 'pod', 0, 30, 'Clip', 'd', '[]', '{}', ?, ?)", (str(f), "k" * 32)).lastrowid
    for p, st in (("youtube", "queued"), ("tiktok", "ready"), ("instagram", "review")):
        conn.execute("INSERT INTO posts (clip_id, platform, status, scheduled_at) VALUES (?, ?, ?, datetime('now'))", (cid, p, st))
    conn.commit()
    pipeline.delete_clip(conn, cid)
    assert {r[0] for r in conn.execute("SELECT status FROM posts")} == {"rejected"} and not f.exists()
    keep = tmp_path / "k.mp4"
    keep.write_bytes(b"v")
    cid2 = conn.execute(
        "INSERT INTO clips (source_identifier, campaign_id, start_sec, end_sec, title, description, tags, arms, file_path,"
        " media_token) VALUES ('s', 'pod', 0, 30, 'Posted', 'd', '[]', '{}', ?, ?)", (str(keep), "j" * 32)).lastrowid
    conn.execute("INSERT INTO posts (clip_id, platform, status, scheduled_at, url) VALUES (?, 'tiktok', 'posted', datetime('now'), 'https://x')", (cid2,))
    conn.execute("INSERT INTO posts (clip_id, platform, status, scheduled_at) VALUES (?, 'youtube', 'queued', datetime('now'))", (cid2,))
    conn.commit()
    pipeline.delete_clip(conn, cid2)
    assert dict(conn.execute("SELECT platform, status FROM posts WHERE clip_id = ?", (cid2,)).fetchall()) == \
        {"tiktok": "posted", "youtube": "rejected"} and keep.exists()  # what went out stays, and the video can still be reposted


def test_clip_cards_have_download_and_delete_buttons(server, tmp_path):
    base, conn = server
    _campaign(tmp_path)
    db.put(conn, "auto_platforms", "youtube")
    f = tmp_path / "clips" / ("m" * 32 + ".mp4")
    f.parent.mkdir(exist_ok=True)
    f.write_bytes(b"v")
    cid = conn.execute(
        "INSERT INTO clips (source_identifier, campaign_id, start_sec, end_sec, title, description, tags, arms, file_path,"
        " media_token) VALUES ('s', 'pod', 0, 30, 'Heismay clip', 'd', '[]', '{}', ?, ?)", (str(f), "m" * 32)).lastrowid
    for p in ("youtube", "tiktok"):
        conn.execute("INSERT INTO posts (clip_id, platform, status, scheduled_at) VALUES (?, ?, 'review', datetime('now'))", (cid, p))
    conn.commit()
    page = _get(base + "/", "s3cret").read().decode()
    assert "Watch each clip" in page and f'download="clip-{cid}.mp4">Download</a>' in page and "Reject" in page
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo
    soon = (datetime.now(ZoneInfo("America/New_York")) + timedelta(days=2)).strftime("%Y-%m-%dT%H:%M")
    _get(base + "/review/approve", "s3cret", data=f"id={cid}&choose=1&platforms=youtube&platforms=tiktok&at={soon}".encode())
    page = _get(base + "/", "s3cret").read().decode()
    assert page.count('action="/clip/delete"') == 3  # on its Your clips card, under Ready to post and under Posting automatically
    assert page.count(f'download="clip-{cid}.mp4"') >= 2
    _get(base + "/clip/delete", "s3cret", data=f"id={cid}".encode())
    assert {r[0] for r in conn.execute("SELECT status FROM posts")} == {"rejected"} and not f.exists()
    assert "/clip/delete" not in _get(base + "/", "s3cret").read().decode()


def test_every_clip_list_has_a_delete(server, tmp_path):
    base, conn = server
    _campaign(tmp_path)
    f = tmp_path / "clips" / ("r" * 32 + ".mp4")
    f.parent.mkdir(exist_ok=True)
    f.write_bytes(b"v")
    cid = conn.execute(
        "INSERT INTO clips (source_identifier, campaign_id, start_sec, end_sec, title, description, tags, arms, file_path,"
        " media_token) VALUES ('s', 'pod', 0, 30, 'Waiting clip', 'd', '[]', '{}', ?, ?)", (str(f), "r" * 32)).lastrowid
    conn.execute("INSERT INTO posts (clip_id, platform, status, scheduled_at) VALUES (?, 'tiktok', 'review', datetime('now'))", (cid,))
    conn.commit()
    page = _get(base + "/", "s3cret").read().decode()
    assert "Reject" in page and page.count('action="/clip/delete"') == 2  # its Your clips card and its approval card
    posted_id, posted_file = _posted_clip(conn, tmp_path)  # a posted clip kept for reposting
    page = _get(base + "/", "s3cret").read().decode()
    assert "Delete video" in page and "/clip/delete-video" in page
    _get(base + "/clip/delete-video", "s3cret", data=f"id={posted_id}".encode())
    assert not posted_file.exists() and conn.execute("SELECT file_path FROM clips WHERE id = ?", (posted_id,)).fetchone()[0] is None
    assert conn.execute("SELECT status FROM posts WHERE clip_id = ?", (posted_id,)).fetchone()[0] == "posted"  # the record stays
    assert "Delete video" not in _get(base + "/", "s3cret").read().decode()
    _get(base + "/clip/delete", "s3cret", data=f"id={cid}".encode())
    assert not f.exists()


# --- Your clips: a library by campaign, and a page for each clip -----------------------------------------------

def _clip_row(conn, tmp_path, campaign, title, status, token, platform="tiktok", url=""):
    f = tmp_path / "clips" / f"{token}.mp4"
    f.parent.mkdir(exist_ok=True)
    f.write_bytes(b"video")
    cid = conn.execute(
        "INSERT INTO clips (source_identifier, campaign_id, start_sec, end_sec, title, description, tags, arms, file_path,"
        " media_token) VALUES ('s', ?, 0, 25, ?, 'caption line\n#Ad\n#tag', '[]', '{}', ?, ?)",
        (campaign, title, str(f), token)).lastrowid
    conn.execute("INSERT INTO posts (clip_id, platform, status, scheduled_at, url) VALUES (?, ?, ?, datetime('now'), ?)",
                 (cid, platform, status, url))
    conn.commit()
    return cid


def test_your_clips_lists_every_clip_by_campaign_and_each_has_its_own_page(server, tmp_path):
    base, conn = server
    _campaign(tmp_path)
    campaigns.save(tmp_path, campaigns.Campaign(id="ring", name="Ring Drone", program_url="https://w", rate_per_1k_views=1,
                                                sources=[]))
    waiting = _clip_row(conn, tmp_path, "pod", "Clip waiting", "review", "a" * 32)
    approved = _clip_row(conn, tmp_path, "pod", "Clip approved", "ready", "b" * 32)
    posted = _clip_row(conn, tmp_path, "ring", "Clip posted", "posted", "c" * 32, url="https://tiktok.com/t/abc")
    _clip_row(conn, tmp_path, "ring", "Clip removed", "rejected", "d" * 32)  # never listed
    page = _get(base + "/", "s3cret").read().decode()
    assert 'id="clips"' in page and "Your clips" in page and "Pod Show (2)" in page and "Ring Drone (1)" in page
    assert all(f'href="/clip/{i}"' in page for i in (waiting, approved, posted)) and "Clip removed" not in page.split('id="clips"')[1].split("</section>")[0]
    assert "Waiting for your approval" in page and "Approved" in page

    detail = _get(f"{base}/clip/{waiting}", "s3cret").read().decode()
    assert f'src="/media/{"a" * 32}.mp4"' in detail and "caption line" in detail and "Back to Your clips" in detail
    assert all(w in detail for w in ("Approve", "Reject", "Delete", "Download", "waiting for your approval"))
    after = _get(f"{base}/review/approve", "s3cret", data=f"id={waiting}&choose=1&platforms=tiktok&back=/clip/{waiting}".encode()).read().decode()
    assert "ready to post from your phone" in after and "Back to Your clips" in after  # approving lands back on its page

    done = _get(f"{base}/clip/{posted}", "s3cret").read().decode()
    assert "open the post" in done and "Post again on TikTok" in done and "Delete video" in done and "Approve" not in done

    after = _get(f"{base}/review/reject", "s3cret", data=f"id={approved}&back=/%23clips".encode()).read().decode()
    assert "Your clips" in after  # rejecting returns to the library
    gone = _get(f"{base}/clip/{waiting}", "s3cret").read().decode()  # approved above, still there
    assert "Clip waiting" in gone
    _get(f"{base}/clip/delete", "s3cret", data=f"id={waiting}&back=/%23clips".encode())
    try:
        _get(f"{base}/clip/{waiting}", "s3cret")
        raise AssertionError("a deleted clip's page should be gone")
    except urllib.error.HTTPError as err:
        assert err.code == 404 and "not here any more" in err.read().decode()


def test_clip_pages_need_the_password_and_the_library_says_when_clips_are_being_made(server, tmp_path):
    base, conn = server
    _campaign(tmp_path)
    with pytest.raises(urllib.error.HTTPError) as err:
        _get(f"{base}/clip/1", None)
    assert err.value.code == 401
    assert 'id="clips"' not in _get(base + "/", "s3cret").read().decode()  # nothing yet, nothing running
    pipeline.set_status(conn, "running", "Making clips now.")
    page = _get(base + "/", "s3cret").read().decode()
    assert 'id="clips"' in page and "Making clips now." in page.split('id="clips"')[1]


# --- a restart in the middle of a run must not lose the upload; one video is cut, not combined --------------------

def test_an_upload_being_clipped_when_the_server_restarts_goes_back_in_the_queue(tmp_path, monkeypatch):
    conn = db.connect(tmp_path / "i.db")
    camp = _campaign(tmp_path)
    video = tmp_path / "uploads" / "ep.mp4"
    video.parent.mkdir()
    video.write_bytes(b"v")
    manual.add(conn, {"kind": "file", "ref": str(video), "campaign_id": "pod", "creator": "", "title": "Ep", "count": 3,
                      "review": True})
    monkeypatch.setattr(media, "duration", lambda p: 90.0)
    assert pipeline.manual_job(conn, [camp], []) is not None and manual.queue(conn) == []  # popped, being clipped
    assert json.loads(db.get(conn, manual.INFLIGHT_KEY))["ref"] == str(video)
    assert manual.recover_inflight(conn) is True  # the server restarted: it is back, first in line
    [item] = manual.queue(conn)
    assert item["ref"] == str(video) and item["attempts"] == 1 and item["count"] == 3 and item["review"] is True
    assert db.get(conn, manual.INFLIGHT_KEY) == "" and manual.recover_inflight(conn) is False  # nothing twice
    manual.pop(conn)
    manual.claim(conn, {"kind": "file", "ref": str(video), "campaign_id": "pod", "attempts": 2})
    assert manual.recover_inflight(conn) is False and not video.exists() and manual.queue(conn) == []  # tried 3 times: given up


def test_a_finished_run_leaves_nothing_in_flight(tmp_path, monkeypatch):
    from clip_engine.config import Settings
    from tests.test_core import FakePub

    conn, claude = _produce_fixture(tmp_path, monkeypatch, {}, ["Heismay steps into the light"])
    assert pipeline.produce(conn, Settings(data_dir=tmp_path), claude, None, {"youtube": FakePub("youtube")}, 2) >= 1
    assert db.get(conn, manual.INFLIGHT_KEY) == "" and manual.queue(conn) == []
    assert manual.recover_inflight(conn) is False


def test_a_single_video_in_a_combine_batch_is_cut_into_clips(tmp_path):
    conn = db.connect(tmp_path / "s.db")
    video = manual.save_upload(io.BytesIO(b"x" * 10), 10, "trailer.mp4", tmp_path)
    manual.add_to_batch(conn, "onevideo1", video)
    assert manual.finish_batch(conn, "onevideo1", {"campaign_id": "pod", "count": 3, "review": True, "title": ""}) == 1
    [item] = manual.queue(conn)
    assert item["kind"] == "file" and item["ref"] == str(video) and item["count"] == 3 and item["review"] is True
    photo = manual.save_upload(io.BytesIO(b"x" * 10), 10, "one.jpg", tmp_path)  # a single photo still becomes a video
    manual.add_to_batch(conn, "onephoto11", photo)
    manual.finish_batch(conn, "onephoto11", {"campaign_id": "pod", "count": 1})
    assert manual.queue(conn)[-1]["kind"] == "project"


# --- fixes: crashed runs, safe redirects, confirm before posting again, captions for untitled combined videos ------

def test_a_run_that_died_part_way_is_retried_at_the_next_check(tmp_path, monkeypatch):
    from clip_engine import __main__ as main_mod
    from clip_engine.config import Settings
    import subprocess
    settings = Settings(data_dir=tmp_path)
    conn = db.connect(settings.db_path)
    _campaign(tmp_path)
    for name, fn in (("publish_due", lambda *a: 0), ("score", lambda *a: 0), ("refresh_views", lambda *a: None),
                     ("send_digest", lambda *a: 0), ("send_alerts", lambda *a: 0)):
        monkeypatch.setattr(pipeline, name, fn)
    monkeypatch.setattr(main_mod, "_producer", None)  # nothing running: the process that claimed the video is gone
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    started = []
    monkeypatch.setattr(subprocess, "Popen", lambda cmd, **k: started.append(cmd) or type("P", (), {"poll": lambda s: None})())
    manual.claim(conn, {"kind": "file", "ref": "/x.mp4", "campaign_id": "pod"})
    main_mod.tick(conn, settings, {"youtube": object()})
    assert manual.queue(conn)[0]["ref"] == "/x.mp4" and started  # back in line and being clipped again
    assert db.get(conn, manual.INFLIGHT_KEY) == ""


def test_back_links_only_go_to_this_sites_own_pages():
    for ok in ("/", "/#clips", "/clip/12", "/clip/12#top"):
        assert dashboard.SAFE_BACK_RE.match(ok), ok
    for bad in ("//evil.com", "/\\evil.com", "https://evil.com", "/clip/1/../../x", "/?x=1", "", "javascript:x"):
        assert not dashboard.SAFE_BACK_RE.match(bad), bad


def test_post_again_asks_before_posting(server, tmp_path):
    base, conn = server
    _campaign(tmp_path)
    cid, _ = _posted_clip(conn, tmp_path, platforms=("youtube",))
    page = _get(base + "/", "s3cret").read().decode()
    assert "return confirm(&quot;Post this clip again on YouTube? It goes up publicly within 5 minutes.&quot;)" in page
    assert "return confirm(&quot;Post this clip again on YouTube?" in _get(f"{base}/clip/{cid}", "s3cret").read().decode()


def test_an_untitled_combined_video_gets_claudes_caption_not_the_campaign_name(tmp_path, monkeypatch):
    from clip_engine.config import Settings
    from tests.test_core import FakePub, _claude_returning

    conn, _ = _produce_fixture(tmp_path, monkeypatch, {}, ["unused"])
    db.put(conn, manual.QUEUE_KEY, "[]")
    files = []
    for name in ("a.jpg", "b.jpg"):
        f = tmp_path / "uploads" / name
        f.write_bytes(b"x")
        files.append(str(f))
    manual.add(conn, {"kind": "project", "files": files, "campaign_id": "pod", "creator": "", "title": "", "count": 1})
    monkeypatch.setattr(media, "thumbnail", lambda src, out, photo: b"jpeg")
    monkeypatch.setattr(media, "duration", lambda p: 10.0)
    monkeypatch.setattr(media, "assemble", lambda files, out, work, budget=30.0: (out.write_bytes(b"c"), out)[1])
    claude = _claude_returning({"videos": [{"pieces": [1, 0], "title": "The drone lifts off the dock"}], "dropped": []})
    assert pipeline.produce(conn, Settings(data_dir=tmp_path), claude, None, {"youtube": FakePub("youtube")}, 2) == 1
    title = conn.execute("SELECT title FROM clips").fetchone()[0]
    assert title == "The drone lifts off the dock"


# --- submit timer, phone layout, posting checklist ----------------------------------------------------------------

def _strict(tmp_path, **kw):
    camp = campaigns.Campaign(id="sega", name="SEGA Metaphor", program_url="https://contentrewards.com/c/x", rate_per_1k_views=1.5,
                              sources=[], allow_music=False, paid_label=False, disclosure_line="#Ad",
                              submit_within_minutes=30, keep_live_days=30, **kw)
    campaigns.save(tmp_path, camp)
    return camp


def test_submit_deadline_and_its_label():
    from datetime import datetime, timezone
    now = datetime(2026, 11, 17, 19, 20, tzinfo=timezone.utc)  # 2:20pm ET
    deadline, left = pipeline.submit_deadline("2026-11-17 19:15:00", 30, now)
    assert deadline.hour == 19 and deadline.minute == 45 and left == 25
    assert pipeline.submit_deadline("2026-11-17 18:00:00", 30, now)[1] == 0
    assert pipeline.submit_deadline("2026-11-17 19:15:00", 0, now) is None and pipeline.submit_deadline(None, 30, now) is None
    camp = campaigns.Campaign(id="s", name="S", program_url="u", rate_per_1k_views=1, sources=[], submit_within_minutes=30)
    label, ok = dashboard.deadline_label({"posted_at": "2026-11-17 19:15:00"}, camp, now)
    assert ok and label == "Submit by 2:45pm ET, 25 min left"
    label, ok = dashboard.deadline_label({"posted_at": "2026-11-17 18:00:00"}, camp, now)
    assert not ok and label == "Too late to submit: the 30-minute window closed at 1:30pm ET."
    assert dashboard.deadline_label({"posted_at": "2026-11-17 18:00:00"}, None, now) == ("", True)


def test_links_to_submit_show_the_deadline_and_can_be_marked_one_by_one(server, tmp_path):
    base, conn = server
    _strict(tmp_path)
    ids = []
    for title in ("First post", "Second post"):
        cid = conn.execute("INSERT INTO clips (source_identifier, campaign_id, start_sec, end_sec, title, description, tags,"
                           " arms) VALUES ('s', 'sega', 0, 20, ?, 'd', '[]', '{}')", (title,)).lastrowid
        ids.append(conn.execute("INSERT INTO posts (clip_id, platform, status, scheduled_at, url, posted_at) VALUES"
                                " (?, 'tiktok', 'posted', datetime('now'), 'https://tiktok.com/t/' || ?, datetime('now', '-5 minutes'))",
                                (cid, title[:5])).lastrowid)
    conn.commit()
    page = _get(base + "/", "s3cret").read().decode()
    assert "Submit by" in page and "min left" in page and "Open campaign page" in page and "Copy link" in page
    assert "2</b> links to submit &middot; Submit by" in page  # the to-do bar shows the deadline too
    _get(base + "/submitted/one", "s3cret", data=f"id={ids[0]}&back=/%23links".encode())
    left = [p["id"] for p in pipeline.pending_submissions(conn)]
    assert left == [ids[1]]


def test_saving_a_link_tells_you_the_submit_deadline(server, tmp_path):
    base, conn = server
    _strict(tmp_path)
    cid = conn.execute("INSERT INTO clips (source_identifier, campaign_id, start_sec, end_sec, title, description, tags, arms)"
                       " VALUES ('s', 'sega', 0, 20, 'Clip', 'd', '[]', '{}')").lastrowid
    pid = conn.execute("INSERT INTO posts (clip_id, platform, status, scheduled_at) VALUES (?, 'tiktok', 'ready', datetime('now'))",
                       (cid,)).lastrowid
    conn.commit()
    page = _get(base + "/manual/save", "s3cret", data=f"id={pid}&url=https%3A%2F%2Ftiktok.com%2Ft%2Fz".encode()).read().decode()
    assert "Link saved. Now submit it on the campaign page: Submit by" in page
    assert "30 min left" in page or "29 min left" in page


def test_checklist_follows_the_campaign_rules_and_shows_before_posting(server, tmp_path):
    camp = campaigns.Campaign(id="sega", name="SEGA", program_url="u", rate_per_1k_views=1, sources=[], allow_music=False,
                              paid_label=False, disclosure_line="#Ad", submit_within_minutes=30, keep_live_days=30)
    items = dashboard.campaign_checklist(camp)
    assert any("#Ad sits alone" in i for i in items) and "Don't add any sound or music." in items
    assert any("Don't switch on Paid partnership" in i for i in items) and any("within 30 minutes" in i for i in items)
    assert any("Don't delete the post for 30 days" in i for i in items)
    plain = dashboard.campaign_checklist(campaigns.Campaign(id="r", name="R", program_url="u", rate_per_1k_views=1, sources=[]))
    assert not any("sound" in i or "30" in i for i in plain) and any("paid-partnership label" in i for i in plain)
    assert dashboard.campaign_checklist(None) == []
    base, conn = server
    _strict(tmp_path)
    f = tmp_path / "clips" / ("q" * 32 + ".mp4")
    f.parent.mkdir(exist_ok=True)
    f.write_bytes(b"v")
    cid = conn.execute("INSERT INTO clips (source_identifier, campaign_id, start_sec, end_sec, title, description, tags, arms,"
                       " file_path, media_token) VALUES ('s', 'sega', 0, 20, 'Clip', 'd', '[]', '{}', ?, ?)",
                       (str(f), "q" * 32)).lastrowid
    conn.execute("INSERT INTO posts (clip_id, platform, status, scheduled_at) VALUES (?, 'tiktok', 'ready', datetime('now'))", (cid,))
    conn.commit()
    page = _get(base + "/", "s3cret").read().decode()
    assert "Before you post" in page and "Don&#x27;t add any sound or music." in page
    assert "Before you post" in _get(f"{base}/clip/{cid}", "s3cret").read().decode()


def test_phone_layout_puts_everyday_things_first_and_the_rest_under_more(server, tmp_path):
    base, conn = server
    _campaign(tmp_path)
    page = _get(base + "/", "s3cret").read().decode()
    more = page.index('<details id="more"')
    assert page.index("Make clips now") < page.index('id="own-video"') < page.index('id="links"') < more
    for folded in ('id="campaigns"', 'id="settings"', "Trending now", "estimated earnings"):
        assert page.index(folded) > more, folded
    assert "openFor(location.hash)" in page  # the Settings shortcut opens the More box


def test_campaign_form_takes_the_submit_window_and_keep_days():
    form = {"name": "Sega", "program_url": "https://w", "rate": "1", "submit_within_minutes": "30", "keep_live_days": "30"}
    camp = campaigns.from_form(form, ["tiktok"])
    assert camp.submit_within_minutes == 30 and camp.keep_live_days == 30
    for bad in ("x", "-5", "2000", "1.5"):
        with pytest.raises(campaigns.CampaignError, match="Submit within"):
            campaigns.from_form({**form, "submit_within_minutes": bad}, ["tiktok"])


# --- Railway usage: a lean always-on process and no leftover files --------------------------------------------

def test_the_always_on_process_does_not_load_the_anthropic_library():
    import subprocess, sys
    out = subprocess.run([sys.executable, "-c", "import sys, clip_engine.__main__, clip_engine.dashboard; "
                          "print('anthropic' in sys.modules, 'faster_whisper' in sys.modules, 'cv2' in sys.modules)"],
                         capture_output=True, text=True, check=True).stdout.strip()
    assert out == "False False False"  # those load only in the clip-making process


def test_cleanup_removes_only_old_files_nothing_uses(tmp_path):
    import os, time
    from clip_engine.config import Settings
    settings = Settings(data_dir=tmp_path)
    conn = db.connect(settings.db_path)
    up = manual.upload_dir(tmp_path)
    up.mkdir(parents=True)
    (tmp_path / "clips").mkdir()
    settings.work_dir.mkdir()
    old = time.time() - 3 * 86400

    def make(path, age=old, folder=False):
        path.mkdir() if folder else path.write_bytes(b"x")
        os.utime(path, (age, age))
        return path
    queued = make(up / "queued.mp4")
    flying = make(up / "flying.mp4")
    batched = make(up / "batched.jpg")
    orphan = make(up / "orphan.mp4")
    fresh = make(up / "fresh.mp4", age=time.time())
    leftover_build = make(up / "build-abc", folder=True)
    work = make(settings.work_dir / "abcd1234", folder=True)
    clip_file = make(tmp_path / "clips" / ("k" * 32 + ".mp4"))
    stray = make(tmp_path / "clips" / ("z" * 32 + ".mp4"))
    manual.add(conn, {"kind": "file", "ref": str(queued), "campaign_id": "pod"})
    manual.claim(conn, {"kind": "project", "files": [str(flying)], "campaign_id": "pod"})
    manual.add_to_batch(conn, "batch1234", batched)
    conn.execute("INSERT INTO clips (source_identifier, start_sec, end_sec, title, description, tags, arms, file_path)"
                 " VALUES ('s', 0, 1, 't', 'd', '[]', '{}', ?)", (str(clip_file),))
    conn.commit()
    assert pipeline.cleanup_orphans(conn, settings) == 4
    assert all(p.exists() for p in (queued, flying, batched, fresh, clip_file))
    assert not any(p.exists() for p in (orphan, leftover_build, work, stray))


def test_cleanup_runs_once_a_day_and_never_during_a_clip_run(tmp_path, monkeypatch):
    from clip_engine import __main__ as main_mod
    from clip_engine.config import Settings
    settings = Settings(data_dir=tmp_path)
    conn = db.connect(settings.db_path)
    for name in ("publish_due", "score", "refresh_views", "send_digest", "send_alerts"):
        monkeypatch.setattr(pipeline, name, lambda *a: 0)
    calls = []
    monkeypatch.setattr(pipeline, "cleanup_orphans", lambda c, s: calls.append(1) or 0)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(main_mod, "_producer", None)
    main_mod.tick(conn, settings, {})
    main_mod.tick(conn, settings, {})
    assert calls == [1]  # once, not every 5 minutes
    db.put(conn, "last_cleanup", "0")
    monkeypatch.setattr(main_mod, "producing", lambda: True)
    main_mod.tick(conn, settings, {})
    assert calls == [1]  # not while clips are being made


# --- simpler: one place per clip, delete or remove everything --------------------------------------------------

def test_posted_clips_can_be_taken_off_the_list_without_touching_the_post(server, tmp_path):
    base, conn = server
    _campaign(tmp_path)
    cid, f = _posted_clip(conn, tmp_path)
    page = _get(base + "/", "s3cret").read().decode()
    assert f'href="/clip/{cid}"' in page and "Remove from list" in page and "Posted" in page
    _get(base + "/clip/hide", "s3cret", data=f"id={cid}&back=/%23clips".encode())
    assert f'href="/clip/{cid}"' not in _get(base + "/", "s3cret").read().decode()
    assert conn.execute("SELECT status FROM posts WHERE clip_id = ?", (cid,)).fetchone()[0] == "posted" and f.exists()


def test_expired_links_offer_post_again_and_remove_instead_of_mark_submitted(server, tmp_path):
    base, conn = server
    _strict(tmp_path)
    f = tmp_path / "late.mp4"
    f.write_bytes(b"v")
    cid = conn.execute("INSERT INTO clips (source_identifier, campaign_id, start_sec, end_sec, title, description, tags, arms,"
                       " file_path, media_token) VALUES ('s', 'sega', 0, 20, 'Late clip', 'd', '[]', '{}', ?, ?)",
                       (str(f), "l" * 32)).lastrowid
    late = conn.execute("INSERT INTO posts (clip_id, platform, status, scheduled_at, url, posted_at) VALUES (?, 'tiktok',"
                        " 'posted', datetime('now'), 'https://tiktok.com/t/late', datetime('now', '-2 hours'))", (cid,)).lastrowid
    conn.commit()
    links = _get(base + "/", "s3cret").read().decode().split('id="links"')[1].split("</section>")[0]
    assert "Too late to submit" in links and "Post again on TikTok" in links and ">Remove<" in links
    assert "Submitted &#10003;" not in links and "Mark submitted" not in links
    _get(base + "/submitted/one", "s3cret", data=f"id={late}&back=/%23links".encode())
    assert pipeline.pending_submissions(conn) == []  # removed from the list
    page = _get(base + "/repost", "s3cret", data=f"id={cid}&platform=tiktok".encode()).read().decode()
    assert "Post it on TikTok" in page  # Post again lands on the clip's page, ready to post


def test_a_clips_page_has_everything_needed_to_post_it(server, tmp_path):
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo
    base, conn = server
    _strict(tmp_path)
    db.put(conn, "auto_platforms", "youtube")
    f = tmp_path / "clips" / ("p" * 32 + ".mp4")
    f.parent.mkdir(exist_ok=True)
    f.write_bytes(b"v")
    cid = conn.execute("INSERT INTO clips (source_identifier, campaign_id, start_sec, end_sec, title, description, tags, arms,"
                       " file_path, media_token) VALUES ('s', 'sega', 0, 20, 'Clip', 'Heismay in the city\n#Ad', '[]', '{}', ?, ?)",
                       (str(f), "p" * 32)).lastrowid
    tiktok = conn.execute("INSERT INTO posts (clip_id, platform, status, scheduled_at) VALUES (?, 'tiktok', 'ready', datetime('now'))",
                          (cid,)).lastrowid
    yt = conn.execute("INSERT INTO posts (clip_id, platform, status, scheduled_at) VALUES (?, 'youtube', 'queued',"
                      " datetime('now', '+3 hours'))", (cid,)).lastrowid
    conn.commit()
    page = _get(f"{base}/clip/{cid}", "s3cret").read().decode()
    assert "Copy caption" in page and "Paste your TikTok post link" in page and "Not posting on TikTok" in page
    assert "YouTube posts it by itself" in page and "Set time" in page and "Post now" in page and "Before you post" in page
    soon = (datetime.now(ZoneInfo("America/New_York")) + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M")
    after = _get(base + "/post/schedule", "s3cret", data=f"id={yt}&at={soon}&back=/clip/{cid}".encode()).read().decode()
    assert "YouTube posts it by itself" in after  # setting the time keeps you on the clip's page
    _get(base + "/manual/skip", "s3cret", data=f"id={tiktok}&back=/clip/{cid}".encode())
    assert conn.execute("SELECT status FROM posts WHERE id = ?", (tiktok,)).fetchone()[0] == "skipped"


def test_the_main_page_is_short_upload_then_clips_then_links(server, tmp_path):
    base, conn = server
    _campaign(tmp_path)
    page = _get(base + "/", "s3cret").read().decode()
    form = page.split('id="own-video"')[1].split("</section>")[0]
    visible = form.split("<details")[0]  # what shows before More options is opened
    assert "Campaign" in visible and 'type="file"' in visible and "How many clips do you want?" in visible
    assert "Upload or add link</button>" in form.split("</details>")[1]  # the button is outside More options
    assert "Creator" not in visible and "Combine" not in visible  # tucked under More options
    assert "More options" in form


# --- "Do this next": one thing at a time, big buttons -------------------------------------------------------------

def test_do_this_next_shows_one_thing_at_a_time_in_order_of_urgency(tmp_path):
    from clip_engine.config import Settings
    settings = Settings(data_dir=tmp_path)
    conn = db.connect(settings.db_path)
    _strict(tmp_path)
    assert "All done" in dashboard._next_task(conn, settings) and "Add a video" in dashboard._next_task(conn, settings)
    pipeline.set_status(conn, "running", "Making clips now.")
    assert "Making your clips" in dashboard._next_task(conn, settings)
    pipeline.set_status(conn, "done", "Made 2 clips.")

    def clip(title, status, platform="tiktok", **post):
        cid = conn.execute("INSERT INTO clips (source_identifier, campaign_id, start_sec, end_sec, title, description, tags,"
                           " arms, media_token) VALUES ('s', 'sega', 0, 20, ?, 'Caption text\n#Ad', '[]', '{}', ?)",
                           (title, title[:1] * 32)).lastrowid
        pid = conn.execute("INSERT INTO posts (clip_id, platform, status, scheduled_at, url, posted_at) VALUES"
                           " (?, ?, ?, datetime('now'), ?, ?)", (cid, platform, status, post.get("url"), post.get("posted_at"))).lastrowid
        conn.commit()
        return cid, pid
    ready_clip, ready_post = clip("ready one", "ready")
    card = dashboard._next_task(conn, settings)
    assert "Post this clip on TikTok" in card and "1. Share to TikTok" in card and "2. Paste my TikTok link" in card
    assert "navigator.share" in card and f'value="{ready_post}"' in card and "Don't add music" in card
    waiting_clip, _ = clip("waiting one", "review")
    card = dashboard._next_task(conn, settings)
    assert "Do you want to post this clip?" in card and "Yes, post it" in card and "No, delete it" in card  # checking comes first
    _, sub = clip("posted one", "posted", url="https://tiktok.com/t/x",
                  posted_at=__import__("datetime").datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"))
    card = dashboard._next_task(conn, settings)
    assert "Submit your TikTok link now" in card and "min left" in card and "I submitted it" in card  # a deadline beats all
    assert "copyAndOpen" in card and "contentrewards.com" in card


def test_do_this_next_buttons_work_end_to_end(server, tmp_path):
    base, conn = server
    _strict(tmp_path)
    f = tmp_path / "clips" / ("n" * 32 + ".mp4")
    f.parent.mkdir(exist_ok=True)
    f.write_bytes(b"v")
    cid = conn.execute("INSERT INTO clips (source_identifier, campaign_id, start_sec, end_sec, title, description, tags, arms,"
                       " file_path, media_token) VALUES ('s', 'sega', 0, 20, 'New clip', 'Caption\n#Ad', '[]', '{}', ?, ?)",
                       (str(f), "n" * 32)).lastrowid
    pid = conn.execute("INSERT INTO posts (clip_id, platform, status, scheduled_at) VALUES (?, 'tiktok', 'review', datetime('now'))",
                       (cid,)).lastrowid
    conn.commit()
    page = _get(base + "/", "s3cret").read().decode()
    assert page.index('id="next"') < page.index('id="own-video"') and "Yes, post it" in page
    page = _get(base + "/review/approve", "s3cret", data=f"id={cid}&back=/".encode()).read().decode()
    assert "Post this clip on TikTok" in page  # approving moves straight on to posting it
    page = _get(base + "/manual/save", "s3cret", data=f"id={pid}&back=/&url=https%3A%2F%2Ftiktok.com%2Ft%2Fnew".encode()).read().decode()
    assert "Submit your TikTok link now" in page  # saving the link moves straight on to submitting it
    page = _get(base + "/submitted/one", "s3cret", data=f"id={pid}&back=/".encode()).read().decode()
    assert "All done" in page


# --- Clip status: every clip in order, with each app's status and times ----------------------------------------

def test_clip_status_page_lists_every_clip_in_order_with_each_app_status(server, tmp_path):
    base, conn = server
    _campaign(tmp_path)
    old, _ = _posted_clip(conn, tmp_path, platforms=("tiktok",))
    conn.execute("UPDATE posts SET posted_at = '2026-11-18 09:15:00', views = 36 WHERE clip_id = ?", (old,))
    conn.commit()
    new = _clip_row(conn, tmp_path, "pod", "Newest clip", "review", "s" * 32)
    conn.execute("INSERT INTO posts (clip_id, platform, status, scheduled_at, url, posted_at, views) VALUES"
                 " (?, 'youtube', 'queued', datetime('now', '+3 hours'), NULL, NULL, NULL)", (new,))
    conn.commit()
    page = _get(base + "/status", "s3cret").read().decode()
    assert "Clip status" in page and "Back to the dashboard" in page
    assert page.index("Newest clip") < page.index(f"clip/{old}") or page.index("Newest clip") < page.index("made")  # newest first
    assert "YouTube</b>: scheduled for" in page and "waiting for your approval" in page
    scheduled = page.split("scheduled for ")[1][:30]
    assert scheduled.split(" ET")[0].count(",") == 1 and "ET" in scheduled  # a date like "Nov 18, 1:05pm ET", not a weekday
    assert "TikTok</b>: posted on Nov 18, 4:15am ET" in page and "36 views" in page  # posted time in Eastern, and views
    assert "UTC" not in page  # no UTC left on the page
    assert "Clip status" in _get(base + "/", "s3cret").read().decode()  # the button is on the main page
    with pytest.raises(urllib.error.HTTPError) as err:
        _get(base + "/status", None)
    assert err.value.code == 401  # still needs the password


def test_status_times_are_eastern():
    assert dashboard.et_text("2026-11-18 09:15:00") == "Nov 18, 4:15am ET"  # 9:15 UTC is 4:15am Eastern in November
    assert dashboard.et_text("2026-11-18 17:05:00") == "Nov 18, 12:05pm ET"
    assert dashboard.et_text(None) == ""
    assert dashboard.et_text("2026-07-04 18:05:00") == "Jul 4, 2:05pm ET"  # July is daylight time, one hour different
    assert dashboard.et_text("2026-11-18 05:00:00") == "Nov 18, 12:00am ET"  # midnight reads as 12:00am, not 0:00


def test_short_clips_aim_for_fifteen_to_thirty_five_seconds_and_stay_off_the_edges():
    from clip_engine import strategy
    assert strategy.LENGTHS["short"] == ["15-25", "25-35"]
    assert strategy.length_range({"length": "short:25-35"}) == (25, 35)
    ass = media.captions_ass([{"start": 0.0, "end": 0.4, "word": "hi"}], 0, 1, "short", hook="wait for it")
    assert ",2,110,110,520\n" in ass and ",8,110,110,260\n" in ass


def test_refresh_views_stores_how_much_of_a_video_people_watch(tmp_path):
    conn = db.connect(tmp_path / "r.db")
    conn.execute("INSERT INTO clips (id, source_identifier, start_sec, end_sec, title, description, tags, arms, file_path,"
        " media_token) VALUES (1, 's', 0, 20, 'c', 'd', '[]', '{}', 'x', ?)", ("t" * 32,))
    conn.execute("INSERT INTO posts (clip_id, platform, status, scheduled_at, posted_at, external_id) "
                 "VALUES (1, 'youtube', 'posted', datetime('now'), datetime('now'), 'vid1')")
    conn.commit()

    class Pub:
        def stats(self, ids):
            return {i: (10, 0) for i in ids}

        def retention(self, ids):
            return {i: 61.5 for i in ids}
    pipeline.refresh_views(conn, {"youtube": Pub()})
    assert json.loads(db.get(conn, "retention", "{}")) == {"vid1": 61.5}


def test_a_hand_post_clip_page_has_a_share_button_so_no_file_picking_is_needed(server, tmp_path):
    base, conn = server
    _campaign(tmp_path)
    cid, _ = _posted_clip(conn, tmp_path, platforms=("youtube",))
    conn.execute("UPDATE posts SET status = 'ready', url = NULL WHERE clip_id = ?", (cid,))
    conn.commit()
    page = _get(f"{base}/clip/{cid}", "s3cret").read().decode()
    assert 'id="share-btn"' in page and "Share the video to YouTube" in page and "Download instead" in page
    assert f'prepareClip("/media/{"t" * 32}.mp4")' in page


def _sent_prompt(claude) -> str:
    call = claude.beta.messages.create.call_args
    return json.dumps(call.kwargs.get("messages", call.args))


def test_your_instructions_reach_claude_when_it_chooses_the_clips(tmp_path, monkeypatch):
    from clip_engine.config import Settings
    from tests.test_core import FakePub

    conn, claude = _produce_fixture(tmp_path, monkeypatch, {}, ["A good clip"])
    item = manual.pop(conn)
    manual.add(conn, {**item, "count": 1, "notes": manual.clean_notes("start at the funny part\n\nkeep it short")})
    pipeline.produce(conn, Settings(data_dir=tmp_path), claude, None, {"youtube": FakePub("youtube"), "tiktok": FakePub("tiktok")}, 2)
    assert "start at the funny part keep it short" in _sent_prompt(claude)
    assert manual.clean_notes("x" * 900) == "x" * 600


def test_a_rejected_clip_can_be_redone_with_new_instructions_while_its_video_is_kept(tmp_path, monkeypatch):
    from clip_engine.config import Settings
    from tests.test_core import FakePub

    conn, claude = _produce_fixture(tmp_path, monkeypatch, {}, ["First try"])
    item = manual.pop(conn)
    manual.add(conn, {**item, "count": 1, "review": True})
    video = Path(item["ref"])
    pipeline.produce(conn, Settings(data_dir=tmp_path), claude, None, {"youtube": FakePub("youtube"), "tiktok": FakePub("tiktok")}, 2)
    assert video.exists()  # kept so the clip can be redone
    cid = conn.execute("SELECT id FROM clips").fetchone()[0]
    assert pipeline.redo(conn, cid, "  ") == "Say what you want changed first."
    assert pipeline.redo(conn, cid, "make it shorter and start on the joke").startswith("Got it")
    assert {r[0] for r in conn.execute("SELECT status FROM posts WHERE clip_id = ?", (cid,))} == {"rejected"}
    [queued] = manual.queue(conn)
    assert queued["ref"] == str(video) and queued["count"] == 1 and queued["review"] and queued["redo"]
    assert "make it shorter and start on the joke" in queued["notes"] and "was rejected" in queued["notes"]
    # the kept copy is now in the queue; asking again for the same clip finds nothing saved
    assert pipeline.redo(conn, cid, "again").startswith("The original video is no longer saved")


def test_kept_uploads_are_deleted_after_three_days(tmp_path):
    conn = db.connect(tmp_path / "k.db")
    f = tmp_path / "up.mp4"
    f.write_bytes(b"v")
    manual.keep_for_redo(conn, "upload:up", {"kind": "file", "ref": str(f), "campaign_id": "pod"})
    manual.expire_redo(conn)
    assert f.exists() and manual.redo_files(conn) == [str(f)]
    kept = json.loads(db.get(conn, manual.REDO_KEY))
    kept["upload:up"]["until"] = 1.0
    db.put(conn, manual.REDO_KEY, json.dumps(kept))
    manual.expire_redo(conn)
    assert not f.exists() and manual.redo_files(conn) == []


def test_review_cards_offer_redo_with_changes(server, tmp_path):
    base, conn = server
    _campaign(tmp_path)
    cid, _ = _posted_clip(conn, tmp_path, platforms=("tiktok",))
    conn.execute("UPDATE posts SET status = 'review', url = NULL WHERE clip_id = ?", (cid,))
    conn.commit()
    assert "Redo with changes" in _get(base + "/", "s3cret").read().decode()
    assert "Redo with changes" in _get(f"{base}/clip/{cid}", "s3cret").read().decode()
    assert 'name="notes"' in _get(base + "/", "s3cret").read().decode()


def test_a_length_you_pick_replaces_the_one_the_engine_is_testing(tmp_path, monkeypatch):
    from clip_engine.config import Settings
    from tests.test_core import FakePub

    assert manual.clean_seconds("15-25") == "15-25" and manual.clean_seconds("1-999") == "" and manual.seconds_range("") is None
    conn, claude = _produce_fixture(tmp_path, monkeypatch, {}, ["A good clip"])
    item = manual.pop(conn)
    manual.add(conn, {**item, "count": 1, "seconds": "10-20"})
    pipeline.produce(conn, Settings(data_dir=tmp_path), claude, None, {"youtube": FakePub("youtube"), "tiktok": FakePub("tiktok")}, 2)
    assert "10-20 seconds long" in _sent_prompt(claude)


def test_a_redo_can_change_only_the_length(tmp_path, monkeypatch):
    from clip_engine.config import Settings
    from tests.test_core import FakePub

    conn, claude = _produce_fixture(tmp_path, monkeypatch, {}, ["First try"])
    item = manual.pop(conn)
    manual.add(conn, {**item, "count": 1, "review": True, "seconds": "25-35"})
    pipeline.produce(conn, Settings(data_dir=tmp_path), claude, None, {"youtube": FakePub("youtube"), "tiktok": FakePub("tiktok")}, 2)
    cid = conn.execute("SELECT id FROM clips").fetchone()[0]
    assert pipeline.redo(conn, cid, "", "").startswith("Say what")
    assert pipeline.redo(conn, cid, "", "10-20").startswith("Got it")
    [queued] = manual.queue(conn)
    assert queued["seconds"] == "10-20" and "10 to 20 seconds" in queued["notes"]


def test_a_clip_page_can_save_the_video_to_the_gallery_through_the_share_list(server, tmp_path):
    base, conn = server
    _campaign(tmp_path)
    cid, _ = _posted_clip(conn, tmp_path, platforms=("tiktok",))
    page = _get(f"{base}/clip/{cid}", "s3cret").read().decode()
    assert "Save to my gallery / Google Photos" in page and f'prepareSave("/media/{"t" * 32}.mp4"' in page
    assert "Download</a>" in page


def test_you_can_step_through_the_clips_waiting_for_you(server, tmp_path):
    base, conn = server
    _campaign(tmp_path)
    ids = []
    for n in range(3):
        cid, _ = _posted_clip(conn, tmp_path, platforms=("tiktok",))
        conn.execute("UPDATE clips SET title = ?, media_token = ? WHERE id = ?", (f"Clip number {n + 1}", f"{n:x}" * 32, cid))
        conn.execute("UPDATE posts SET status = 'review', url = NULL WHERE clip_id = ?", (cid,))
        ids.append(cid)
    conn.commit()

    def card(path):
        return _get(base + path, "s3cret").read().decode().split('id="next"')[1].split("</section>")[0]
    first = card("/")  # the oldest first
    assert "1 of 3" in first and "/?n=2#next" in first and "Previous</span>" in first
    mid = card("/?n=2")
    assert "2 of 3" in mid and "/?n=1#next" in mid and "/?n=3#next" in mid
    last = card("/?n=3")
    assert "3 of 3" in last and "Next &#8250;</span>" in last
    assert "3 of 3" in card("/?n=99") and "1 of 3" in card("/?n=abc") and "1 of 3" in card("/?n=0")
    assert mid != first != last
    conn.execute("UPDATE posts SET status = 'ready' WHERE clip_id IN (?, ?)", ids[:2])  # now two to post, one to review
    conn.commit()
    conn.execute("UPDATE posts SET status = 'ready' WHERE clip_id = ?", (ids[2],))
    conn.commit()
    assert "2 of 3" in card("/?n=2") and "Share to TikTok" in card("/?n=2")


def test_a_clip_made_as_my_own_clips_has_no_campaign_no_paid_label_and_no_link_to_submit(tmp_path, monkeypatch):
    from clip_engine.config import Settings
    from tests.test_core import FakePub

    conn, claude = _produce_fixture(tmp_path, monkeypatch, {}, ["Plain clip of my own"])
    item = manual.pop(conn)
    for c in campaigns.load(tmp_path):  # no campaign exists at all
        campaigns.remove(tmp_path, c.id)
    manual.add(conn, {**item, "campaign_id": campaigns.OWN_ID, "count": 1})
    assert campaigns.load(tmp_path) == []
    assert pipeline.produce(conn, Settings(data_dir=tmp_path), claude, None,
                            {"youtube": FakePub("youtube"), "tiktok": FakePub("tiktok")}, 2) == 1
    row = conn.execute("SELECT campaign_id, description FROM clips").fetchone()
    assert row["campaign_id"] is None
    assert "#ad" not in row["description"].lower() and "Paid clip" not in row["description"] and "program" not in row["description"]
    assert pipeline.pending_submissions(conn) == []
    prompt = _sent_prompt(claude)
    assert "My own clips" not in prompt


def test_my_own_clips_cannot_be_edited_paused_or_deleted(server, tmp_path):
    base, _ = server
    for path in ("/campaigns/toggle", "/campaigns/delete"):
        with pytest.raises(urllib.error.HTTPError):
            _get(base + path, "s3cret", data=b"id=_own")
    assert not (tmp_path / "campaigns.json").exists() or "_own" not in (tmp_path / "campaigns.json").read_text()


def test_the_add_a_video_form_has_a_link_box_next_to_the_file_chooser(server, tmp_path):
    base, _ = server
    _campaign(tmp_path)
    page = _get(base + "/", "s3cret").read().decode()
    form = page.split('<section id="own-video">')[1].split("</section>")[0]
    assert form.index('type="file"') < form.index('name="url"') and "Or paste a video link" in form
    assert "sendLink(form, btn, link)" in form and "Paste a link instead" not in form
