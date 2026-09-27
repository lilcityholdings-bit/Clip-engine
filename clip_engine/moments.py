"""Where to look for clips in a long video.

YouTube publishes a "most replayed" graph for popular videos (yt-dlp exposes it as
`heatmap`: [{"start_time", "end_time", "value"}] with value 0-1). The peaks are
the moments viewers rewatch, which is exactly what clips should be made of. We
transcribe a few minutes around the top peaks instead of just the start of the
video, and tell Claude which lines sit on a peak.
"""

WINDOW_PAD_SEC = 150       # transcribe 2.5 minutes either side of each peak
MAX_PEAKS = 4
REPLAYED_THRESHOLD = 0.7   # share of the top peak's value that counts as "most replayed"


def peaks(heatmap: list[dict], count: int = MAX_PEAKS, min_gap: float = 2 * WINDOW_PAD_SEC) -> list[float]:
    """Centers of the highest heatmap points, at least min_gap seconds apart."""
    chosen: list[float] = []
    for point in sorted(heatmap, key=lambda p: p.get("value", 0), reverse=True):
        center = (point["start_time"] + point["end_time"]) / 2
        if all(abs(center - c) >= min_gap for c in chosen):
            chosen.append(center)
        if len(chosen) == count:
            break
    return chosen


def windows(heatmap: list[dict], duration: float) -> list[tuple[float, float]]:
    """Sorted, non-overlapping (start, end) windows around the top peaks. [] without a heatmap."""
    spans = sorted((max(0.0, c - WINDOW_PAD_SEC), min(duration or c + WINDOW_PAD_SEC, c + WINDOW_PAD_SEC))
                   for c in peaks(heatmap or []))
    merged: list[tuple[float, float]] = []
    for start, end in spans:
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def mark_replayed(segments: list[dict], heatmap: list[dict]) -> None:
    """Set segment["replayed"] = True where the segment overlaps a heatmap peak."""
    if not heatmap:
        return
    top = max(p.get("value", 0) for p in heatmap) or 1
    hot = [(p["start_time"], p["end_time"]) for p in heatmap if p.get("value", 0) >= REPLAYED_THRESHOLD * top]
    for s in segments:
        s["replayed"] = any(s["start"] < e and s["end"] > b for b, e in hot)


def same_window(clip: dict, segments: list[dict]) -> bool:
    """A clip must not jump across the gap between two transcribed windows."""
    inside = [s for s in segments if s["end"] > clip["start"] and s["start"] < clip["end"]]
    return bool(inside) and len({s.get("window", 0) for s in inside}) == 1
