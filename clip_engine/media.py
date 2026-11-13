"""Download, transcribe and render clips with ffmpeg and faster-whisper."""
import os
import re
import subprocess
from pathlib import Path

import requests

from . import layout
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


def transcribe(video: Path, model_size: str,
               windows: list[tuple[float, float]] | None = None) -> tuple[list[dict], list[dict]]:
    """Return (segments, words) with times in seconds from the start of the video.

    Only the given (start, end) windows are transcribed, e.g. the most replayed parts
    of a long podcast; by default the first MAX_TRANSCRIBE_MINUTES. Each segment gets
    "window" (its index) and "energy" (loudness relative to the median segment), so
    laughs and raised voices stand out even though Claude only reads the text.
    """
    import numpy as np
    from faster_whisper import WhisperModel  # heavy import, only needed here

    windows = windows or [(0.0, float(MAX_TRANSCRIBE_SEC))]
    model = WhisperModel(model_size, device="cpu", compute_type="int8")
    segments, words = [], []
    audio = video.with_suffix(".wav")
    for index, (w_start, w_end) in enumerate(windows):
        _run(["ffmpeg", "-y", "-v", "error", "-ss", f"{w_start:.2f}", "-i", str(video), "-t", f"{w_end - w_start:.2f}",
              "-vn", "-ac", "1", "-ar", "16000", "-f", "wav", str(audio)])
        samples = _read_wav(audio)
        raw_segments, _ = model.transcribe(str(audio), word_timestamps=True, vad_filter=True)
        for seg in raw_segments:
            # Over music or noise Whisper invents words. Its own confidence numbers flag those segments.
            if getattr(seg, "no_speech_prob", 0.0) > 0.6 and getattr(seg, "avg_logprob", 0.0) < -1.0:
                continue
            chunk = samples[int(seg.start * 16000):int(seg.end * 16000)]
            rms = float(np.sqrt(np.mean(chunk.astype(np.float64) ** 2))) if len(chunk) else 0.0
            segments.append({"start": float(seg.start + w_start), "end": float(seg.end + w_start), "text": seg.text,
                             "window": index, "rms": rms})
            for w in seg.words or []:
                words.append({"start": float(w.start + w_start), "end": float(w.end + w_start), "word": w.word})
        audio.unlink(missing_ok=True)
    add_energy(segments)
    return segments, words


def _read_wav(path: Path):
    import wave

    import numpy as np

    with wave.open(str(path), "rb") as f:
        return np.frombuffer(f.readframes(f.getnframes()), dtype=np.int16)


def add_energy(segments: list[dict]) -> None:
    """energy = segment loudness / median loudness (1.0 = typical)."""
    levels = sorted(s.get("rms", 0.0) for s in segments if s.get("rms"))
    median = levels[len(levels) // 2] if levels else 0.0
    for s in segments:
        s["energy"] = round(s.pop("rms", 0.0) / median, 2) if median else 1.0


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


def captions_ass(words: list[dict], start: float, end: float, fmt: str, hook: str = "",
                 center: bool = False) -> str:
    """Burned-in captions.

    Shorts: 3-word chunks in the lower third with the spoken word highlighted, plus
    an optional hook line at the top for the first few seconds. Long clips: plain
    8-word lines at the bottom.
    """
    short = fmt == "short"
    res, size, margin, per_chunk = ((1080, 1920), 84, 520, 3) if short else ((1920, 1080), 54, 60, 8)
    # In the two-person layout, captions sit on the seam between the speakers.
    align = 5 if (short and center) else 2
    margin = 0 if align == 5 else margin
    header = (
        "[Script Info]\nScriptType: v4.00+\n"
        f"PlayResX: {res[0]}\nPlayResY: {res[1]}\nWrapStyle: 0\n\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, OutlineColour, BackColour, Bold, "
        "BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV\n"
        f"Style: Default,DejaVu Sans,{size},{WHITE},{BLACK},{SHADOW},1,1,5,0,{align},60,60,{margin}\n"
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


def duration(video: Path) -> float:
    """Length of a video file in seconds."""
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(video)],
        capture_output=True, text=True, check=True).stdout.strip()
    return float(out)


def _dimensions(video: Path) -> tuple[int, int]:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height",
         "-of", "csv=p=0", str(video)], capture_output=True, text=True, check=True,
    ).stdout.strip().split(",")
    return int(out[0]), int(out[1])


def plan_layout(video: Path, start: float, end: float) -> layout.Layout:
    """How to frame a Short: stacked two-shot, speaker tracking, or blurred fill."""
    return layout.choose(layout.sample_faces(video, start, end))


MUSIC_EXTENSIONS = {".mp3", ".m4a", ".wav", ".aac", ".ogg"}
MUSIC_VOLUME = 0.22  # the bed sits well under the speech, and ducks further while someone talks


def music_library(data_dir: Path) -> dict[str, Path]:
    """Licensed background tracks in DATA_DIR/music, by name. Only put tracks here that the
    company has a license to use in monetized social videos."""
    folder = Path(os.environ.get("MUSIC_DIR", data_dir / "music"))
    if not folder.is_dir():
        return {}
    return {p.stem: p for p in sorted(folder.iterdir()) if p.suffix.lower() in MUSIC_EXTENSIONS}


def audio_graph(music: bool) -> str:
    """Audio filter: speech normalized to -14 LUFS, optionally over a ducked music bed."""
    if not music:
        return "[0:a]loudnorm=I=-14:TP=-1.5:LRA=11[aout]"
    return (f"[1:a]volume={MUSIC_VOLUME}[bed];[0:a]asplit=2[voice][key];"
            "[bed][key]sidechaincompress=threshold=0.03:ratio=6:attack=20:release=400[ducked];"
            "[voice][ducked]amix=inputs=2:duration=first:normalize=0,"
            "loudnorm=I=-14:TP=-1.5:LRA=11[aout]")


def scene_cuts(video: Path, threshold: float = 0.3) -> list[float]:
    """Times (seconds) where the picture changes shot, found on a small copy of the video so it is quick."""
    result = subprocess.run(
        ["ffmpeg", "-v", "info", "-an", "-i", str(video), "-vf", f"scale=320:-2,select='gt(scene,{threshold})',showinfo",
         "-f", "null", "-"], capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg scene detection failed: {result.stderr[-1000:]}")
    return sorted({round(float(t), 2) for t in re.findall(r"pts_time:([0-9.]+)", result.stderr)})


def has_audio(video: Path) -> bool:
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries", "stream=index",
                          "-of", "csv=p=0", str(video)], capture_output=True, text=True).stdout
    return bool(out.strip())


def render(video: Path, out: Path, start: float, end: float, fmt: str, captions: str,
           frame: layout.Layout | None = None, music: Path | None = None) -> Path:
    """Cut [start, end] and format it.

    Shorts are 1080x1920 using the given layout (see layout.py). Long clips are
    1920x1080. Audio is normalized to -14 LUFS, the loudness YouTube and TikTok target.
    """
    out.parent.mkdir(parents=True, exist_ok=True)
    subs = out.with_suffix(".ass")
    subs.write_text(captions, encoding="utf-8")
    if fmt == "short":
        width, height = _dimensions(video)
        graph = layout.filter_graph(frame or plan_layout(video, start, end), width, height, subs.name)
    else:
        graph = (
            "[0:v]scale=1920:1080:force_original_aspect_ratio=decrease,"
            f"pad=1920:1080:(ow-iw)/2:(oh-ih)/2,subtitles={subs.name}[v]"
        )
    inputs = ["-ss", f"{start:.2f}", "-i", str(video.resolve())]
    audio_maps: list[str] = []
    if has_audio(video):
        if music:
            inputs += ["-stream_loop", "-1", "-i", str(Path(music).resolve())]
        graph += ";" + audio_graph(bool(music))
        audio_maps = ["-map", "[aout]"]
    # Run inside the output folder so the subtitles filter gets a plain file name.
    _run([
        "ffmpeg", "-y", "-v", "error", *inputs,
        "-t", f"{end - start:.2f}", "-filter_complex", graph, "-map", "[v]", *audio_maps,
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "21", "-c:a", "aac", "-b:a", "160k", "-ar", "48000",
        "-movflags", "+faststart", out.name,
    ], cwd=out.parent)
    subs.unlink(missing_ok=True)
    return out


# --- one video from several files -------------------------------------------

IMAGE_SEC = 3.0        # each photo is on screen this long (a slow zoom keeps it moving)
MIN_IMAGE_SEC = 1.5
VIDEO_MAX_SEC = 15.0   # the most taken from the start of any one video
MIN_VIDEO_SEC = 2.0
COMPILATION_MAX_SEC = 58.0  # the finished video must stay a Short


def plan_pieces(pieces: list[tuple[bool, float]], budget: float = COMPILATION_MAX_SEC) -> list[float]:
    """Seconds to use from each (is_photo, length) piece so the whole fits in one Short.

    Photos get IMAGE_SEC and videos their first VIDEO_MAX_SEC. When that is too long, everything is
    shortened in proportion (never below a minimum), and if it still doesn't fit, the last pieces are dropped.
    """
    want = [IMAGE_SEC if photo else min(length, VIDEO_MAX_SEC) for photo, length in pieces]
    if sum(want) > budget:
        scale = budget / sum(want)
        want = [round(w * scale, 2) if photo else min(length, max(MIN_VIDEO_SEC, round(w * scale, 2)))
                for (photo, length), w in zip(pieces, want)]
        want = [max(MIN_IMAGE_SEC, w) if photo else w for (photo, _), w in zip(pieces, want)]
        while sum(want) > budget and len(want) > 1:
            want.pop()
    return want


_FIT = ("split[a][b];[a]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,boxblur=20:5[bg];"
        "[b]scale=1080:1920:force_original_aspect_ratio=decrease[fg];[bg][fg]overlay=(W-w)/2:(H-h)/2")
_VIDEO_ENCODE = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-pix_fmt", "yuv420p", "-r", "30"]
_AUDIO_ENCODE = ["-c:a", "aac", "-b:a", "160k", "-ar", "48000", "-ac", "2"]


def _segment(src: Path, out: Path, seconds: float, photo: bool, index: int, audio: bool) -> None:
    """One piece as a 1080x1920, 30 fps clip: a photo with a slow zoom, or the start of a video, on a blurred fill."""
    silent = ["-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo"]
    if photo:
        frames = max(1, round(seconds * 30))
        zoom = (f"min(1+0.10*on/{frames},1.10)" if index % 2 == 0 else f"max(1.10-0.10*on/{frames},1.0)")
        # blown up first so the zoom steps are sub-pixel and don't shimmer
        graph = (f"[0:v]{_FIT},scale=2160:3840,zoompan=z='{zoom}':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'"
                 f":d={frames}:s=1080x1920:fps=30,format=yuv420p,setsar=1[v]")
        inputs, audio_map = ["-i", str(src), *silent], ["-map", "1:a"]
    else:
        graph = f"[0:v]fps=30,{_FIT},format=yuv420p,setsar=1[v]"
        if has_audio(src):
            graph += ";[0:a]aresample=48000,aformat=channel_layouts=stereo,apad[a]"
            inputs, audio_map = ["-i", str(src)], ["-map", "[a]"]
        else:
            inputs, audio_map = ["-i", str(src), *silent], ["-map", "1:a"]
    _run(["ffmpeg", "-y", "-v", "error", *inputs, "-filter_complex", graph, "-map", "[v]",
          *(audio_map if audio else []), "-t", f"{seconds:.2f}", *_VIDEO_ENCODE, *(_AUDIO_ENCODE if audio else []),
          "-movflags", "+faststart", str(out)])


def assemble(pieces: list[Path], out: Path, work: Path) -> Path:
    """Join videos and photos, in the order given, into one 1080x1920 video of at most 58 seconds.

    Every piece is framed the same way (fitted over a blurred copy of itself), photos slowly zoom,
    and the pieces are cut together back to back. The soundtrack is the videos' own sound, with
    silence under photos; a set of photos alone has no audio track.
    """
    from . import manual
    work.mkdir(parents=True, exist_ok=True)
    kinds = [manual.is_image(p) for p in pieces]
    lengths = [IMAGE_SEC if photo else duration(p) for p, photo in zip(pieces, kinds)]
    plan = plan_pieces(list(zip(kinds, lengths)))
    pieces, kinds = pieces[:len(plan)], kinds[:len(plan)]
    audio = any(not photo and has_audio(p) for p, photo in zip(pieces, kinds))
    segments = []
    for i, (piece, photo, seconds) in enumerate(zip(pieces, kinds, plan)):
        seg = work / f"seg{i:02d}.mp4"
        _segment(piece, seg, seconds, photo, i, audio)
        segments.append(seg)
    listing = work / "list.txt"
    listing.write_text("".join(f"file '{s.name}'\n" for s in segments), encoding="utf-8")
    _run(["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0", "-i", listing.name, "-c", "copy",
          "-movflags", "+faststart", str(out.resolve())], cwd=work)
    return out
