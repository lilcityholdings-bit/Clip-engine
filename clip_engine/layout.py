"""Choose how a Short frames the people on screen, and build the ffmpeg filter for it.

Three layouts, best first:
- "stack":  two people (a typical podcast two-shot) → each gets their own half of
            the vertical frame, top and bottom, with captions on the seam.
- "track":  one main speaker → a 9:16 crop that follows their face, jumping when
            the camera cuts to a different angle or person.
- "blur":   no clear faces → the whole frame over a blurred fill.
"""
from dataclasses import dataclass, field
from pathlib import Path

SAMPLES_PER_SEC = 2
MAX_SAMPLES = 120
MIN_SEGMENT_SEC = 1.0      # shorter camera "shots" are merged into their neighbour
JUMP = 0.12                # horizontal move (fraction of width) that counts as a new shot
TWO_SHOT_GAP = 0.25        # how far apart two faces must be to count as two people
FACE_ZOOM = 3.0            # stacked halves are this many face-heights tall


@dataclass
class Layout:
    kind: str                                              # stack | track | blur
    keys: list[tuple[float, float]] = field(default_factory=list)  # track: (seconds, center)
    left: float = 0.0                                      # stack: face centers (fraction of width)
    right: float = 0.0
    left_y: float = 0.4                                    # stack: face centers (fraction of height)
    right_y: float = 0.4
    face_h: float = 0.0                                    # stack: typical face height (fraction of height)


def sample_faces(video: Path, start: float, end: float) -> list[tuple[float, list[tuple]]]:
    """[(t_rel, [(center_x, area, center_y, face_height), ...]), ...]; x as a fraction of
    frame width, y and height as fractions of frame height."""
    import cv2  # heavy import, only needed here

    detector = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
    cap = cv2.VideoCapture(str(video))
    n = max(4, min(MAX_SAMPLES, int((end - start) * SAMPLES_PER_SEC)))
    out = []
    try:
        for i in range(n):
            t = (end - start) * (i + 0.5) / n
            cap.set(cv2.CAP_PROP_POS_MSEC, (start + t) * 1000)
            ok, frame = cap.read()
            if not ok:
                out.append((t, []))
                continue
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            side = max(40, frame.shape[0] // 12)
            faces = detector.detectMultiScale(gray, scaleFactor=1.15, minNeighbors=6, minSize=(side, side))
            fh, fw = frame.shape[:2]
            out.append((t, [((x + w / 2) / fw, float(w * h), (y + h / 2) / fh, h / fh) for x, y, w, h in faces]))
    finally:
        cap.release()
    return out


def _median(values: list[float]) -> float:
    values = sorted(values)
    return values[len(values) // 2]


def choose(samples: list[tuple[float, list[tuple[float, float]]]]) -> Layout:
    """Pick a layout from sampled face positions."""
    if not samples:
        return Layout("blur")
    # Ignore small background faces: keep those at least 40% the size of the biggest in the frame.
    significant = []
    for t, faces in samples:
        if faces:
            biggest = max(f[1] for f in faces)
            faces = [f for f in faces if f[1] >= 0.4 * biggest]
        significant.append((t, faces))

    two = [sorted(faces) for _, faces in significant
           if len(faces) >= 2 and max(f[0] for f in faces) - min(f[0] for f in faces) > TWO_SHOT_GAP]
    if len(two) >= 0.5 * len(samples):
        def y(face) -> float:
            return face[2] if len(face) > 2 else 0.4
        heights = [f[3] for pair in two for f in (pair[0], pair[-1]) if len(f) > 3]
        return Layout("stack", left=_median([p[0][0] for p in two]), right=_median([p[-1][0] for p in two]),
                      left_y=_median([y(p[0]) for p in two]), right_y=_median([y(p[-1]) for p in two]),
                      face_h=_median(heights) if heights else 0.0)

    centers = [(t, max(faces, key=lambda f: f[1])[0] if faces else None) for t, faces in significant]
    found = [c for _, c in centers if c is not None]
    if len(found) < 0.5 * len(samples):
        return Layout("blur")

    # Fill gaps with the last known position, then smooth single-sample flicker.
    filled, last = [], found[0]
    for t, c in centers:
        last = c if c is not None else last
        filled.append((t, last))
    smooth = [(t, _median([filled[j][1] for j in range(max(0, i - 1), min(len(filled), i + 2))]))
              for i, (t, _) in enumerate(filled)]

    # Split into shots wherever the face jumps, starting each shot at time 0 or the jump.
    keys = [(0.0, smooth[0][1])]
    members = [smooth[0][1]]
    for t, c in smooth[1:]:
        if abs(c - _median(members)) > JUMP:
            keys[-1] = (keys[-1][0], _median(members))
            keys.append((t, c))
            members = [c]
        else:
            members.append(c)
    keys[-1] = (keys[-1][0], _median(members))

    # Merge shots that are too short to be real camera cuts.
    merged = [keys[0]]
    for i, (t, c) in enumerate(keys[1:], start=1):
        next_t = keys[i + 1][0] if i + 1 < len(keys) else float("inf")
        if next_t - t < MIN_SEGMENT_SEC:
            continue
        merged.append((t, c))
    return Layout("track", keys=merged)


def crop_width(height: int) -> int:
    return int(height * 9 / 16) // 2 * 2


def crop_x(width: int, crop_w: int, center: float) -> int:
    return max(0, min(int(center * width - crop_w / 2), width - crop_w))


def track_expr(width: int, crop_w: int, keys: list[tuple[float, float]]) -> str:
    """ffmpeg expression for the crop's x position over time (t = seconds into the clip)."""
    expr = str(crop_x(width, crop_w, keys[-1][1]))
    for (t_next, _), (_, c) in zip(reversed(keys[1:]), reversed(keys[:-1])):
        expr = f"if(lt(t\\,{t_next:.2f})\\,{crop_x(width, crop_w, c)}\\,{expr})"
    return expr


def filter_graph(layout: Layout, width: int, height: int, subs: str) -> str:
    """ffmpeg filter_complex producing [v] at 1080x1920 with subtitles burned in."""
    if layout.kind == "track" and crop_width(height) < width:
        w = crop_width(height)
        return f"[0:v]crop={w}:{height}:{track_expr(width, w, layout.keys)}:0,scale=1080:1920,setsar=1,subtitles={subs}[v]"
    if layout.kind == "stack":
        # Each person gets a 1080x960 half: a 9:8 region around their face, zoomed so the
        # face fills about a third of the height (head and shoulders), never beyond the frame.
        h = height if not layout.face_h else min(height, int(layout.face_h * height * FACE_ZOOM))
        w = min(width, int(h * 9 / 8))
        h = int(w * 8 / 9) // 2 * 2
        w = w // 2 * 2

        def region(cx: float, cy: float) -> str:
            y = max(0, min(int(cy * height - h * 0.42), height - h))  # face a little above center
            return f"crop={w}:{h}:{crop_x(width, w, cx)}:{y}"
        return (f"[0:v]split=2[a][b];[a]{region(layout.left, layout.left_y)},scale=1080:960,setsar=1[top];"
                f"[b]{region(layout.right, layout.right_y)},scale=1080:960,setsar=1[bot];"
                f"[top][bot]vstack,subtitles={subs}[v]")
    return ("[0:v]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,boxblur=20:5[bg];"
            "[0:v]scale=1080:-2[fg];"
            f"[bg][fg]overlay=(W-w)/2:(H-h)/2,subtitles={subs}[v]")
