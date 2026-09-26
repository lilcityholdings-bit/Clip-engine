"""Download, transcribe and render clips with ffmpeg and faster-whisper."""
import os
import subprocess
from pathlib import Path

import requests

from .sources import USER_AGENT

# Only the start of long videos is searched for clips, to bound transcription time.
MAX_TRANSCRIBE_SEC = int(os.environ.get("MAX_TRANSCRIBE_MINUTES", "60")) * 60


def download(url: str, dest: Path) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(".part")
    with requests.get(url, stream=True, headers={"User-Agent": USER_AGENT}, timeout=60) as resp:
        resp.raise_for_status()
        with open(tmp, "wb") as f:
            for chunk in resp.iter_content(chunk_size=1024 * 1024):
                f.write(chunk)
    tmp.rename(dest)
    return dest


def _run(args: list[str], cwd: Path | None = None) -> None:
    result = subprocess.run(args, capture_output=True, text=True, cwd=cwd)
    if result.returncode != 0:
        raise RuntimeError(f"{args[0]} failed: {result.stderr[-2000:]}")


def transcribe(video: Path, model_size: str) -> tuple[list[dict], list[dict]]:
    """Return (segments, words) with times in seconds."""
    from faster_whisper import WhisperModel  # heavy import, only needed here

    audio = video.with_suffix(".wav")
    _run(["ffmpeg", "-y", "-v", "error", "-i", str(video), "-t", str(MAX_TRANSCRIBE_SEC),
          "-vn", "-ac", "1", "-ar", "16000", str(audio)])
    model = WhisperModel(model_size, device="cpu", compute_type="int8")
    raw_segments, _ = model.transcribe(str(audio), word_timestamps=True, vad_filter=True)
    segments, words = [], []
    for seg in raw_segments:
        segments.append({"start": seg.start, "end": seg.end, "text": seg.text})
        for w in seg.words or []:
            words.append({"start": w.start, "end": w.end, "word": w.word})
    audio.unlink(missing_ok=True)
    return segments, words



# ASS colours are &HAABBGGRR.
WHITE, YELLOW, BLACK, SHADOW = "&H00FFFFFF", "&H0000E6FF", "&H00000000", "&H64000000"
HOOK_SECONDS = 3.0


def _ass_time(t: float) -> str:
    t = max(t, 0.0)
    h, rem = divmod(t, 3600)
    m, s = divmod(rem, 60)
    return f"{int(h)}:{int(m):02d}:{s:05.2f}"


def _clean(text: str) -> str:
    return text.strip().replace("{", "").replace("}", "").replace("\\", "")


def captions_ass(words: list[dict], start: float, end: float, fmt: str, hook: str = "") -> str:
    """Burned-in captions.

    Shorts: 3-word chunks in the lower third with the spoken word highlighted, plus
    an optional hook line at the top for the first few seconds. Long clips: plain
    8-word lines at the bottom.
    """
    short = fmt == "short"
    res, size, margin, per_chunk = ((1080, 1920), 84, 520, 3) if short else ((1920, 1080), 54, 60, 8)
    header = (
        "[Script Info]\nScriptType: v4.00+\n"
        f"PlayResX: {res[0]}\nPlayResY: {res[1]}\nWrapStyle: 0\n\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, OutlineColour, BackColour, Bold, "
        "BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV\n"
        f"Style: Default,DejaVu Sans,{size},{WHITE},{BLACK},{SHADOW},1,1,5,0,2,60,60,{margin}\n"
        f"Style: Hook,DejaVu Sans,{int(size * 0.9)},{BLACK},{WHITE},{SHADOW},1,3,18,0,8,80,80,260\n\n"
        "[Events]\nFormat: Layer, Start, End, Style, Text\n"
    )
    clip_words = [w for w in words if w["start"] >= start - 0.05 and w["end"] <= end + 0.05]
    lines = []
    if short and hook:
        lines.append(f"Dialogue: 1,{_ass_time(0)},{_ass_time(min(HOOK_SECONDS, end - start))},Hook,{_clean(hook)}")
    for i in range(0, len(clip_words), per_chunk):
        chunk = clip_words[i:i + per_chunk]
        texts = [_clean(w["word"]).upper() if short else _clean(w["word"]) for w in chunk]
        if not short:
            lines.append(f"Dialogue: 0,{_ass_time(chunk[0]['start'] - start)},"
                         f"{_ass_time(chunk[-1]['end'] - start)},Default,{' '.join(texts)}")
            continue
        # One event per word, so the word being spoken is highlighted.
        next_start = clip_words[i + per_chunk]["start"] if i + per_chunk < len(clip_words) else None
        for j, w in enumerate(chunk):
            if j + 1 < len(chunk):
                until = chunk[j + 1]["start"]
            else:  # hold the last word through short pauses so captions don't flicker off
                until = min(next_start, w["end"] + 0.6) if next_start is not None else w["end"]
            text = " ".join(f"{{\\c{YELLOW}&}}{t}{{\\c{WHITE}&}}" if k == j else t for k, t in enumerate(texts))
            lines.append(f"Dialogue: 0,{_ass_time(w['start'] - start)},{_ass_time(until - start)},Default,{text}")
    return header + "\n".join(lines) + "\n"


def face_center(video: Path, start: float, end: float, samples: int = 16) -> float | None:
    """Horizontal position (0-1) of the main speaker's face, or None if there isn't one clear face.

    Samples frames across the clip and takes the largest face in each. Returns
    None when faces are rarely found or jump around (e.g. two people far apart),
    in which case the caller falls back to the letterboxed layout.
    """
    import cv2  # heavy import, only needed here

    detector = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
    cap = cv2.VideoCapture(str(video))
    centers = []
    try:
        for i in range(samples):
            cap.set(cv2.CAP_PROP_POS_MSEC, (start + (end - start) * (i + 0.5) / samples) * 1000)
            ok, frame = cap.read()
            if not ok:
                continue
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            min_side = max(40, frame.shape[0] // 10)
            faces = detector.detectMultiScale(gray, scaleFactor=1.15, minNeighbors=6, minSize=(min_side, min_side))
            if len(faces):
                x, _, w, _ = max(faces, key=lambda f: f[2] * f[3])
                centers.append((x + w / 2) / frame.shape[1])
    finally:
        cap.release()
    return pick_center(centers, samples)


def pick_center(centers: list[float], samples: int) -> float | None:
    if len(centers) < samples / 2:
        return None
    centers = sorted(centers)
    median = centers[len(centers) // 2]
    spread = centers[int(len(centers) * 0.9) - 1] - centers[int(len(centers) * 0.1)]
    return median if spread <= 0.25 else None


def crop_box(width: int, height: int, center: float) -> tuple[int, int, int] | None:
    """9:16 crop (w, h, x) around a horizontal center, or None if the source is already narrow."""
    crop_w = int(height * 9 / 16) // 2 * 2
    if crop_w >= width:
        return None
    x = int(center * width - crop_w / 2)
    return crop_w, height, max(0, min(x, width - crop_w))


def _dimensions(video: Path) -> tuple[int, int]:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height",
         "-of", "csv=p=0", str(video)], capture_output=True, text=True, check=True,
    ).stdout.strip().split(",")
    return int(out[0]), int(out[1])


def render(video: Path, out: Path, start: float, end: float, fmt: str, captions: str) -> Path:
    """Cut [start, end] and format it.

    Shorts are 1080x1920: cropped to the speaker when one face is clearly on
    screen, otherwise the full frame over a blurred fill. Long clips are 1920x1080.
    Audio is normalized to -14 LUFS, the loudness YouTube and TikTok target.
    """
    out.parent.mkdir(parents=True, exist_ok=True)
    subs = out.with_suffix(".ass")
    subs.write_text(captions, encoding="utf-8")
    if fmt == "short":
        center = face_center(video, start, end)
        box = crop_box(*_dimensions(video), center) if center is not None else None
        if box:
            w, h, x = box
            graph = f"[0:v]crop={w}:{h}:{x}:0,scale=1080:1920,setsar=1,subtitles={subs.name}[v]"
        else:
            graph = (
                "[0:v]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,boxblur=20:5[bg];"
                "[0:v]scale=1080:-2[fg];"
                f"[bg][fg]overlay=(W-w)/2:(H-h)/2,subtitles={subs.name}[v]"
            )
    else:
        graph = (
            "[0:v]scale=1920:1080:force_original_aspect_ratio=decrease,"
            f"pad=1920:1080:(ow-iw)/2:(oh-ih)/2,subtitles={subs.name}[v]"
        )
    # Run inside the output folder so the subtitles filter gets a plain file name.
    _run([
        "ffmpeg", "-y", "-v", "error", "-ss", f"{start:.2f}", "-i", str(video.resolve()),
        "-t", f"{end - start:.2f}", "-filter_complex", graph, "-map", "[v]", "-map", "0:a?",
        "-af", "loudnorm=I=-14:TP=-1.5:LRA=11",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "21", "-c:a", "aac", "-b:a", "160k", "-ar", "48000",
        "-movflags", "+faststart", out.name,
    ], cwd=out.parent)
    subs.unlink(missing_ok=True)
    return out
