"""Claude calls: turn trends into search topics, and pick clip moments."""
from __future__ import annotations

import json

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # the Anthropic library takes ~45 MB; only the clip-making process needs it
    import anthropic

from . import moments

MODEL = "claude-opus-5-5"
# Re-run a declined request on Anthropic's recommended fallback model.
FALLBACK_BETA = "server-side-fallback-2026-07-01"

# Clips Claude scores below this are never posted: weak clips drag down the whole channel.
MIN_CLIP_SCORE = 6
# When the owner needs a minimum number of clips from one video, the next best moments can fill in down to this score.
MIN_SPARE_SCORE = 4

EVERGREEN_TOPICS = [
    "space", "history", "science", "technology", "psychology",
    "nature documentary", "economics", "lecture", "interview", "health",
]

TITLE_STYLE_GUIDE = {
    "question": "Ask a question the clip answers.",
    "number": "Lead with a specific number or statistic from the clip.",
    "bold_claim": "State the clip's most surprising claim directly.",
    "curiosity_gap": "Hint at a payoff without giving it away (no clickbait lies).",
    "how_to": "Frame it as how to do or understand something.",
}


class ClaudeRefused(RuntimeError):
    pass


def _json_call(client: anthropic.Anthropic, prompt: str | list, schema: dict, effort: str) -> dict:
    """prompt: plain text, or a list of content blocks (text and images)."""
    response = client.beta.messages.create(
        model=MODEL,
        max_tokens=16000,
        betas=[FALLBACK_BETA],
        fallbacks="default",
        output_config={"effort": effort, "format": {"type": "json_schema", "schema": schema}},
        messages=[{"role": "user", "content": prompt}],
    )
    if response.stop_reason == "refusal":
        raise ClaudeRefused(getattr(response.stop_details, "explanation", None) or "request declined")
    text = next(b.text for b in response.content if b.type == "text")
    return json.loads(text)


def trend_topics(client: anthropic.Anthropic, trending_titles: list[str], past_topics: list[str]) -> list[str]:
    """Map what's trending on YouTube to search terms for licensed archive footage."""
    if not trending_titles:
        return EVERGREEN_TOPICS
    schema = {
        "type": "object",
        "properties": {"topics": {"type": "array", "items": {"type": "string"}}},
        "required": ["topics"],
        "additionalProperties": False,
    }
    prompt = (
        "These videos are trending on YouTube right now:\n"
        + "\n".join(f"- {t}" for t in trending_titles[:50])
        + "\n\nWe clip openly licensed long-form videos (lectures, talks, documentaries, "
        "interviews, public-domain films) from the Internet Archive. Give 8 short search "
        "phrases (1-3 words each) for subjects that ride these trends and that such "
        "videos are likely to cover. Skip celebrity names, brands, sports matches and "
        "music, which won't have licensed footage.\n"
        f"Topics that have worked before, for reference: {', '.join(past_topics[:15]) or 'none yet'}."
    )
    topics = _json_call(client, prompt, schema, effort="low")["topics"]
    cleaned = [t.strip().lower() for t in topics if t.strip()]
    return cleaned or EVERGREEN_TOPICS


def _line(seg: dict, prev: dict | None) -> str:
    """One transcript line with markers for signals Claude can't hear or see."""
    gap = "\n[... skipped part of the video ...]\n" if prev and prev.get("window", 0) != seg.get("window", 0) else ""
    tags = []
    if seg.get("replayed"):
        tags.append("MOST REPLAYED")
    if seg.get("energy", 1.0) >= 1.6:
        tags.append("LOUD")
    tag = f" <{', '.join(tags)}>" if tags else ""
    return f"{gap}[{seg['start']:.1f}-{seg['end']:.1f}]{tag} {seg['text'].strip()}"


def pick_clips(
    client: anthropic.Anthropic,
    transcript: list[dict],
    *,
    source_title: str,
    fmt: str,
    min_sec: int,
    max_sec: int,
    title_style: str,
    count: int,
    rules: str = "",
    track_record: str = "",
    trends: str = "",
    recent_titles: list[str] | None = None,
    want: int = 0,
) -> list[dict]:
    """Choose the most watchable self-contained moments and write metadata for them.

    want: when the owner needs at least this many clips from the video (to post through the day), Claude is told
    to return that many even if the extra ones are weaker, down to MIN_SPARE_SCORE.

    transcript: [{"start": float, "end": float, "text": str}, ...]
    """
    lines = "\n".join(_line(s, transcript[i - 1] if i else None) for i, s in enumerate(transcript))
    schema = {
        "type": "object",
        "properties": {
            "clips": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "start": {"type": "number"},
                        "end": {"type": "number"},
                        "title": {"type": "string"},
                        "description": {"type": "string"},
                        "tags": {"type": "array", "items": {"type": "string"}},
                        "hook_text": {"type": "string"},
                        "hashtags": {"type": "array", "items": {"type": "string"}},
                        "score": {"type": "integer"},
                        "why": {"type": "string"},
                    },
                    "required": ["start", "end", "title", "description", "tags", "hook_text", "hashtags", "score", "why"],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["clips"],
        "additionalProperties": False,
    }
    kind = "YouTube Short (vertical, under 60 seconds)" if fmt == "short" else "regular YouTube video"
    prompt = (
        f"Transcript of \"{source_title}\" with timestamps in seconds:\n\n{lines}\n\n"
        f"Pick up to {count} moments to post as a {kind}. Each must be {min_sec}-{max_sec} "
        "seconds long, make sense on its own without the rest of the video, open with a "
        "strong hook in the first 3 seconds, and start and end on sentence boundaries "
        "(use the timestamps above). Start right on the peak line or action: no greeting, "
        "intro, setup or throat-clearing before it, and end as soon as the payoff lands "
        "(shorter is better than padded). Prefer moments that trigger a reaction: a laugh, "
        "shock, hype, a surprising reveal or curiosity (never rage bait or exaggeration "
        "that the footage doesn't support). Also prefer surprising facts, strong opinions, "
        "clear explanations and emotional moments. Don't overlap clips.\n"
        "Markers: <MOST REPLAYED> means YouTube viewers rewatch that part most, a strong "
        "sign it's the best material. <LOUD> means the speaker got louder than usual "
        "(laughing, shouting, excitement). Never make a clip that crosses a "
        "'skipped part of the video' line.\n\n"
        f"Title style: {TITLE_STYLE_GUIDE[title_style]} Keep titles under 70 characters, "
        "accurate to what's said, with no ALL CAPS words or emoji spam. "
        "Description: 1-2 sentences in the channel's own voice that add something: why this "
        "moment matters, the context a stranger needs, or a question for viewers. Not just a "
        "summary of what's said (platforms reward an original angle; attribution is added "
        "separately). "
        "Tags: 5-10 search terms. "
        "hook_text: 2-6 words shown on screen for the first 3 seconds to stop the scroll "
        "(a tease of the payoff, not the title repeated). "
        "score: 1-10, how likely a stranger scrolling past watches this to the end and "
        "shares it. Be harsh: 5 is an average clip, 8+ is rare. Only return moments that "
        "are genuinely strong; returning fewer clips is better than weak ones."
    )
    want = min(want, count)
    if want > 1:
        prompt += (
            f"\n\nThe owner will post these through the day, so return at least {want} different moments (up to "
            f"{count}): after the strongest, take the next best ones even if they score lower, as long as they are "
            "still worth posting and don't overlap. This overrides the earlier line about returning fewer clips."
        )
    if trends:
        prompt += (
            "\n\nWhat's trending right now:\n" + trends + "\n"
            "Use this to write the title, hook and hashtags in today's language: borrow a "
            "trending phrase or title format when it honestly fits what's said in the clip, "
            "and prefer moments that connect to a hot topic. Never misrepresent the clip to "
            "chase a trend."
        )
    prompt += (
        "\n\nhashtags: 3-5 hashtags for the caption, mixing 1-2 trending ones that genuinely "
        "fit with specific ones for the topic (include the # sign). Every hashtag must be "
        "about this clip; never use generic ones like #fyp, #viral, #reels or #explore."
    )
    if recent_titles:
        prompt += (
            "\n\nTitles this channel already used recently. Don't repeat their wording or "
            "structure; near-identical uploads are treated as mass-produced spam:\n"
            + "\n".join(f"- {t}" for t in recent_titles[:40])
        )
    if track_record:
        prompt += (
            "\n\nHow this channel's past clips actually performed (learn from the winners' "
            "choice of moment, hook and title; avoid what the losers did):\n" + track_record
        )
    if rules:
        prompt += f"\n\nThe creator's rules for clips (must follow): {rules}"
    clips = _json_call(client, prompt, schema, effort="high")["clips"]
    valid, spare = [], []
    for c in clips:
        length = c["end"] - c["start"]
        if min_sec - 3 <= length <= max_sec + 3 and c["start"] >= 0:
            if fmt == "short":
                c["end"] = min(c["end"], c["start"] + 59)
            if c["score"] >= MIN_CLIP_SCORE:
                valid.append(c)
            elif c["score"] >= MIN_SPARE_SCORE:
                spare.append(c)
    # Every clip must be different footage: take the best first and skip any that overlaps one already taken.
    valid.sort(key=lambda c: c["score"], reverse=True)
    kept: list[dict] = []
    for c in valid:
        if moments.is_distinct(c, kept):
            kept.append(c)
    spare.sort(key=lambda c: c["score"], reverse=True)
    for c in spare:
        if len(kept) >= want:
            break
        if moments.is_distinct(c, kept):
            kept.append(c)
    return kept[:count]


ARRANGE_SCHEMA = {
    "type": "object",
    "properties": {
        "order": {"type": "array", "items": {"type": "integer"}},
        "dropped": {"type": "array", "items": {
            "type": "object",
            "properties": {"piece": {"type": "integer"}, "why": {"type": "string"}},
            "required": ["piece", "why"], "additionalProperties": False}},
    },
    "required": ["order", "dropped"],
    "additionalProperties": False,
}


def arrange_pieces(client: anthropic.Anthropic, pieces: list[tuple[str, bytes]], brief: str) -> dict:
    """Look at one frame of each photo or video and decide the order for a short vertical video.

    pieces: (label, JPEG bytes) in upload order. Returns {"order": [piece numbers to keep, in showing
    order], "dropped": [{"piece", "why"}]}. Piece numbers start at 0.
    """
    import base64
    content: list[dict] = [{"type": "text", "text": (
        "You are editing one short vertical social video (YouTube Shorts / TikTok) from the pieces below, "
        f"for this paid campaign: {brief}\n"
        "Each piece is a photo or a video clip; you see one frame of each. Decide the order that tells the clearest, "
        "most watchable story: open on the strongest, most eye-catching shot (the viewer decides in the first second), "
        "group related shots, build to the product or payoff, and end on a clean shot. "
        "Drop a piece only if it is clearly unusable: blurry, nearly black, an accidental shot, a near-duplicate of "
        "another piece (keep the better one), or showing another app's watermark. Keep everything else.")}]
    for i, (label, jpeg) in enumerate(pieces):
        content.append({"type": "text", "text": f"Piece {i}: {label}"})
        content.append({"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                                     "data": base64.b64encode(jpeg).decode()}})
    content.append({"type": "text", "text": (
        "Reply with `order`: the piece numbers to keep, in the order to show them, each at most once; and "
        "`dropped`: each piece left out with a short reason.")})
    return _json_call(client, content, ARRANGE_SCHEMA, effort="low")


CAPTIONS_SCHEMA = {
    "type": "object",
    "properties": {"captions": {"type": "array", "items": {"type": "string"}}},
    "required": ["captions"],
    "additionalProperties": False,
}


def caption_from_frames(client: anthropic.Anthropic, frames: list[bytes], brief: str, notes: str = "") -> list[str]:
    """Write one caption per frame for clips of footage with nothing to read (silent or music only).

    Claude only sees pictures, so the captions describe what is visible, plainly and truthfully. Returns exactly
    len(frames) captions, in order; the caller falls back to something safe when this raises.
    """
    import base64
    content: list[dict] = [{"type": "text", "text": (
        f"Write a caption for each of {len(frames)} different short social clips cut from one video with no speech, "
        f"for this paid campaign: {brief or 'none'}\n"
        "You see one frame from each clip. Each caption is at most 80 characters and says plainly and truthfully what "
        "the viewer sees, in a way that makes someone curious to watch. Describe only what is visible: no claims, "
        "facts, stats, dates, names you can't read on screen, no hashtags, no mentions, no emojis, nothing exaggerated "
        "or clickbait. The captions must differ from each other."
        + (f"\nThe owner's instructions (follow them): {notes}" if notes else ""))}]
    for i, jpeg in enumerate(frames):
        content.append({"type": "text", "text": f"Clip {i}:"})
        content.append({"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                                     "data": base64.b64encode(jpeg).decode()}})
    content.append({"type": "text", "text": f"Reply with `captions`: exactly {len(frames)} captions, in clip order."})
    captions = [str(c).strip() for c in _json_call(client, content, CAPTIONS_SCHEMA, effort="low")["captions"]]
    if len(captions) != len(frames) or not all(captions):
        raise ValueError(f"expected {len(frames)} captions, got {captions!r}")
    return captions


VIDEOS_SCHEMA = {
    "type": "object",
    "properties": {
        "videos": {"type": "array", "items": {
            "type": "object",
            "properties": {"pieces": {"type": "array", "items": {"type": "integer"}}, "title": {"type": "string"}},
            "required": ["pieces", "title"], "additionalProperties": False}},
        "dropped": {"type": "array", "items": {
            "type": "object",
            "properties": {"piece": {"type": "integer"}, "why": {"type": "string"}},
            "required": ["piece", "why"], "additionalProperties": False}},
    },
    "required": ["videos", "dropped"],
    "additionalProperties": False,
}


def plan_videos(client: anthropic.Anthropic, pieces: list[tuple[str, bytes]], brief: str, count: int) -> dict:
    """Look at one frame of each photo or video and split them into `count` DIFFERENT short vertical videos.

    pieces: (label, JPEG bytes) in upload order; the label says how long a video piece is. Returns
    {"videos": [{"pieces": [numbers in showing order], "title": caption}], "dropped": [{"piece", "why"}]}.
    """
    import base64
    content: list[dict] = [{"type": "text", "text": (
        f"You are editing {count} different short vertical social videos (YouTube Shorts / TikTok / Reels) from the "
        f"pieces below, for this paid campaign: {brief}\n"
        "Each piece is a photo or a video clip; you see one frame of each. A photo counts as about 3 seconds of "
        "screen time, a video piece as its length. Make exactly "
        f"{count} videos, each about 15 to 30 seconds of material, each telling its own clear story. Use each piece "
        "in at most one video, so the videos look different from one another, and give each a similar amount of "
        "material. Within a video put the strongest, most eye-catching shot first, group related shots, and end "
        "on a clean shot. Drop a piece only if it is clearly unusable: blurry, nearly black, an accidental shot, a "
        "near-duplicate of another piece (keep the better one), or showing another app's watermark. "
        "Give each video a short caption of at most 80 characters that says plainly and truthfully what the "
        "viewer sees. Describe only what is visible: no claims, facts, stats or dates, no hashtags, no mentions, no "
        "emojis, and nothing exaggerated or clickbait. The captions must differ from each other.")}]
    for i, (label, jpeg) in enumerate(pieces):
        content.append({"type": "text", "text": f"Piece {i}: {label}"})
        content.append({"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                                     "data": base64.b64encode(jpeg).decode()}})
    content.append({"type": "text", "text": (
        f"Reply with `videos`: exactly {count} entries, each with `pieces` (the piece numbers in showing order) "
        "and `title` (the caption); and `dropped`: each piece left out, with a short reason.")})
    return _json_call(client, content, VIDEOS_SCHEMA, effort="low")
