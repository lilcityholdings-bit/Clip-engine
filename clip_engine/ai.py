"""Claude calls: turn trends into search topics, and pick clip moments."""
import json

import anthropic

MODEL = "claude-opus-5"
# Re-run a declined request on Anthropic's recommended fallback model.
FALLBACK_BETA = "server-side-fallback-2026-07-01"

# Clips Claude scores below this are never posted: weak clips drag down the whole channel.
MIN_CLIP_SCORE = 6

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


def _json_call(client: anthropic.Anthropic, prompt: str, schema: dict, effort: str) -> dict:
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
) -> list[dict]:
    """Choose the most watchable self-contained moments and write metadata for them.

    transcript: [{"start": float, "end": float, "text": str}, ...]
    """
    lines = "\n".join(f"[{s['start']:.1f}-{s['end']:.1f}] {s['text'].strip()}" for s in transcript)
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
                        "score": {"type": "integer"},
                        "why": {"type": "string"},
                    },
                    "required": ["start", "end", "title", "description", "tags", "hook_text", "score", "why"],
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
        "(use the timestamps above). Prefer surprising facts, strong opinions, clear "
        "explanations and emotional moments. Don't overlap clips.\n\n"
        f"Title style: {TITLE_STYLE_GUIDE[title_style]} Keep titles under 70 characters, "
        "accurate to what's said, with no ALL CAPS words or emoji spam. "
        "Description: 1-2 sentences about the clip (attribution is added separately). "
        "Tags: 5-10 search terms. "
        "hook_text: 2-6 words shown on screen for the first 3 seconds to stop the scroll "
        "(a tease of the payoff, not the title repeated). "
        "score: 1-10, how likely a stranger scrolling past watches this to the end and "
        "shares it. Be harsh: 5 is an average clip, 8+ is rare. Only return moments that "
        "are genuinely strong; returning fewer clips is better than weak ones."
    )
    if track_record:
        prompt += (
            "\n\nHow this channel's past clips actually performed (learn from the winners' "
            "choice of moment, hook and title; avoid what the losers did):\n" + track_record
        )
    if rules:
        prompt += f"\n\nThe creator's rules for clips (must follow): {rules}"
    clips = _json_call(client, prompt, schema, effort="high")["clips"]
    valid = []
    for c in clips:
        length = c["end"] - c["start"]
        if min_sec - 3 <= length <= max_sec + 3 and c["start"] >= 0 and c["score"] >= MIN_CLIP_SCORE:
            if fmt == "short":
                c["end"] = min(c["end"], c["start"] + 59)
            valid.append(c)
    valid.sort(key=lambda c: c["score"], reverse=True)
    return valid[:count]
