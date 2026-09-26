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


def _ass_time(t: float) -> str:
    t = max(t, 0.0)
    h, rem = divmod(t, 3600)
    m, s = divmod(rem, 60)
    return f"{int(h)}:{int(m):02d}:{s:05.2f}"


def captions_ass(words: list[dict], start: float, end: float, fmt: str) -> str:
    """Burned-in captions: big 3-word chunks for Shorts, bottom lines for long clips."""
    if fmt == "short":
        res, size, margin, per_chunk, align = (1080, 1920), 84, 700, 3, 2
    else:
        res, size, margin, per_chunk, align = (1920, 1080), 54, 60, 8, 2
    header = (
        "[Script Info]\nScriptType: v4.00+\n"
        f"PlayResX: {res[0]}\nPlayResY: {res[1]}\n\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, OutlineColour, BackColour, Bold, "
        "BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV\n"
        f"Style: Default,DejaVu Sans,{size},&H00FFFFFF,&H00000000,&H64000000,1,1,5,0,{align},60,60,{margin}\n\n"
        "[Events]\nFormat: Layer, Start, End, Style, Text\n"
    )
    clip_words = [w for w in words if w["start"] >= start - 0.05 and w["end"] <= end + 0.05]
    lines = []
    for i in range(0, len(clip_words), per_chunk):
        chunk = clip_words[i:i + per_chunk]
        text = " ".join(w["word"].strip() for w in chunk).replace("{", "").replace("}", "")
        if fmt == "short":
            text = text.upper()
        lines.append(
            f"Dialogue: 0,{_ass_time(chunk[0]['start'] - start)},{_ass_time(chunk[-1]['end'] - start)},Default,{text}"
        )
    return header + "\n".join(lines) + "\n"


def render(video: Path, out: Path, start: float, end: float, fmt: str, captions: str) -> Path:
    """Cut [start, end] and format it: 1080x1920 with blurred fill for Shorts, 1920x1080 otherwise."""
    out.parent.mkdir(parents=True, exist_ok=True)
    subs = out.with_suffix(".ass")
    subs.write_text(captions, encoding="utf-8")
    if fmt == "short":
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
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "21", "-c:a", "aac", "-b:a", "160k",
        "-movflags", "+faststart", out.name,
    ], cwd=out.parent)
    subs.unlink(missing_ok=True)
    return out
