from clip_engine import layout, media, moments

HEAT = [{"start_time": t, "end_time": t + 10, "value": v}
        for t, v in [(0, 0.1), (100, 0.9), (110, 1.0), (1000, 0.8), (2000, 0.3), (3000, 0.75)]]


def test_peaks_are_spread_out():
    got = moments.peaks(HEAT)
    assert got[0] == 115 and 105 not in got  # the neighbouring point is too close to the top peak
    assert all(abs(a - b) >= 300 for i, a in enumerate(got) for b in got[i + 1:])


def test_windows_merge_and_clamp():
    wins = moments.windows(HEAT, duration=3100)
    assert wins[0] == (0.0, 265.0)
    assert wins[-1] == (2855.0, 3100)
    assert all(a[1] < b[0] for a, b in zip(wins, wins[1:]))
    assert moments.windows([], 3600) == []


def test_mark_replayed_and_same_window():
    segs = [{"start": 105, "end": 112, "window": 0}, {"start": 500, "end": 505, "window": 0},
            {"start": 1001, "end": 1004, "window": 1}]
    moments.mark_replayed(segs, HEAT)
    assert [s["replayed"] for s in segs] == [True, False, True]
    assert moments.same_window({"start": 100, "end": 130}, segs)
    assert not moments.same_window({"start": 500, "end": 1003}, segs)


def test_energy_is_relative_to_median():
    segs = [{"rms": 100.0}, {"rms": 100.0}, {"rms": 250.0}]
    media.add_energy(segs)
    assert [s["energy"] for s in segs] == [1.0, 1.0, 2.5]


def _samples(centers_per_frame):
    return [(i * 0.5, [(c, 10000.0) for c in cs]) for i, cs in enumerate(centers_per_frame)]


def test_two_people_get_stacked_layout():
    got = layout.choose(_samples([[0.25, 0.75]] * 8 + [[0.25]] * 2))
    assert got.kind == "stack" and got.left == 0.25 and got.right == 0.75


def test_single_speaker_tracks_camera_cuts():
    frames = [[0.3]] * 6 + [[0.7]] * 6 + [[0.72]] + [[0.3]] * 6
    got = layout.choose(_samples(frames))
    assert got.kind == "track"
    assert [round(c, 2) for _, c in got.keys] == [0.3, 0.7, 0.3]
    assert [t for t, _ in got.keys] == [0.0, 3.0, 6.5]


def test_flicker_is_ignored_and_no_faces_blurs():
    got = layout.choose(_samples([[0.3]] * 5 + [[0.8]] + [[0.3]] * 5))
    assert got.kind == "track" and len(got.keys) == 1
    assert layout.choose(_samples([[]] * 8 + [[0.5]])).kind == "blur"
    assert layout.choose([]).kind == "blur"


def test_small_background_faces_are_ignored():
    frames = [(i * 0.5, [(0.4, 40000.0), (0.9, 2000.0)]) for i in range(8)]
    assert layout.choose(frames).kind == "track"


def test_track_expression_and_graphs():
    keys = [(0.0, 0.3), (3.0, 0.7), (6.5, 0.3)]
    expr = layout.track_expr(1920, 606, keys)
    assert expr == "if(lt(t\\,3.00)\\,273\\,if(lt(t\\,6.50)\\,1041\\,273))"
    assert "crop=606:1080:if(" in layout.filter_graph(layout.Layout("track", keys=keys), 1920, 1080, "s.ass")
    stack = layout.filter_graph(layout.Layout("stack", left=0.25, right=0.75), 1920, 1080, "s.ass")
    assert "vstack" in stack and "crop=1214:1080:0:0" in stack and "crop=1214:1080:706:0" in stack
    assert "boxblur" in layout.filter_graph(layout.Layout("blur"), 1920, 1080, "s.ass")
    # already-vertical source: tracking would be pointless, use the fill layout
    assert "boxblur" in layout.filter_graph(layout.Layout("track", keys=[(0, 0.5)]), 1080, 1920, "s.ass")


def test_stacked_captions_sit_on_the_seam():
    words = [{"start": 0.0, "end": 0.4, "word": "hi"}]
    assert ",5,60,60,0\n" in media.captions_ass(words, 0, 1, "short", center=True)
    assert ",2,60,60,520\n" in media.captions_ass(words, 0, 1, "short")


def test_real_detector_tuples_with_vertical_position():
    frames = [(i * 0.5, [(0.25, 9000.0, 0.45, 0.2), (0.75, 8500.0, 0.5, 0.2)]) for i in range(8)]
    got = layout.choose(frames)
    assert (got.kind, got.left_y, got.right_y, got.face_h) == ("stack", 0.45, 0.5, 0.2)
    graph = layout.filter_graph(got, 1920, 1080, "s.ass")
    assert "crop=728:648:116:213" in graph  # zoomed to ~3 face-heights, not the full frame
    single = [(i * 0.5, [(0.4, 9000.0, 0.4, 0.2)]) for i in range(8)]
    assert layout.choose(single).kind == "track"
