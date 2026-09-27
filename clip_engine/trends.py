"""What's trending right now, so titles, hooks and hashtags ride the current wave.

Two inputs, refreshed every TREND_TTL_HOURS per niche and cached in the kv table:
- Google Trends' daily trending searches (free RSS, no key).
- A Claude web-search pass over what's trending on Shorts, TikTok and Reels for the
  niche: hashtags, phrases, title formats and trending sounds.

The brief goes into the clip-picking prompt and onto the dashboard. Trending sounds
are reported, not used: they're copyrighted, and the posting APIs can't attach them.
"""
import json
import logging
import re
import sqlite3
import time
import xml.etree.ElementTree as ET

import anthropic
import requests

from . import db
from .ai import FALLBACK_BETA, MODEL, ClaudeRefused

log = logging.getLogger(__name__)
TREND_TTL_HOURS = 12
GOOGLE_TRENDS_RSS = "https://trends.google.com/trending/rss?geo={geo}"
MAX_CONTINUATIONS = 4

BRIEF_KEYS = ("hashtags", "phrases", "title_patterns", "sounds", "notes")


def google_trends(geo: str = "US", http=None, limit: int = 20) -> list[str]:
    """Today's top trending Google searches for a country."""
    try:
        resp = (http or requests).get(GOOGLE_TRENDS_RSS.format(geo=geo), timeout=20,
                                      headers={"User-Agent": "clip-engine/1.0"})
        resp.raise_for_status()
        root = ET.fromstring(resp.content)
        return [t.text.strip() for t in root.iter("title") if t.text][1:limit + 1]  # first title is the feed's
    except Exception:
        log.exception("could not load Google Trends")
        return []


def _extract_json(text: str) -> dict:
    """The last {...} object in the text (Claude may wrap it in prose or a code fence)."""
    for match in reversed(list(re.finditer(r"\{.*\}", text, re.S))):
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError:
            continue
    return {}


def research(client: anthropic.Anthropic, niche: str, searches: list[str]) -> dict:
    """Claude searches the web for this week's short-form trends in the niche."""
    prompt = (
        f"Research what is trending on YouTube Shorts, TikTok and Instagram Reels this week for "
        f"short-form clips in this niche: {niche}.\n"
        f"Today's top Google searches (use any that genuinely connect to the niche): "
        f"{', '.join(searches) or 'unavailable'}.\n\n"
        "Search the web, then answer with only a JSON object with these keys:\n"
        '- "hashtags": 8-15 hashtags currently performing for this niche (with #)\n'
        '- "phrases": 5-10 words, memes or phrasings people are using right now\n'
        '- "title_patterns": 3-6 caption/title formats that are working now, as templates\n'
        '- "sounds": 3-8 trending sounds or music styles for this niche (for reference only)\n'
        '- "notes": one or two sentences on topics or angles that are hot this week\n'
        "Only include things you found evidence for; don't invent trends."
    )
    messages = [{"role": "user", "content": prompt}]
    tools = [{"type": "web_search_20260209", "name": "web_search", "max_uses": 6}]
    response = None
    for _ in range(MAX_CONTINUATIONS + 1):
        response = client.beta.messages.create(
            model=MODEL, max_tokens=16000, betas=[FALLBACK_BETA], fallbacks="default",
            output_config={"effort": "medium"}, tools=tools, messages=messages,
        )
        if response.stop_reason != "pause_turn":
            break
        # Server-side search loop paused: send the turn back and it resumes.
        messages = [{"role": "user", "content": prompt}, {"role": "assistant", "content": response.content}]
    if response.stop_reason == "refusal":
        raise ClaudeRefused("trend research declined")
    text = "".join(b.text for b in response.content if b.type == "text")
    data = _extract_json(text)
    return {k: data.get(k, [] if k != "notes" else "") for k in BRIEF_KEYS}


def brief(conn: sqlite3.Connection, client: anthropic.Anthropic, niche: str, geo: str = "US",
          http=None, now: float | None = None) -> dict:
    """Cached trend brief for a niche; refreshed every TREND_TTL_HOURS."""
    now = now or time.time()
    key = f"trends:{niche.lower()}"
    cached = db.get(conn, key)
    if cached:
        stored = json.loads(cached)
        if now - stored.get("at", 0) < TREND_TTL_HOURS * 3600:
            return stored["brief"]
    searches = google_trends(geo, http)
    try:
        result = research(client, niche, searches)
    except Exception:
        log.exception("trend research failed for %s", niche)
        result = {k: [] for k in BRIEF_KEYS}
        result["notes"] = ""
    result["google_searches"] = searches[:10]
    db.put(conn, key, json.dumps({"at": now, "brief": result}))
    return result


def as_prompt(trend: dict) -> str:
    """Compact text for the clip-picking prompt; empty when there's nothing useful."""
    parts = []
    if trend.get("hashtags"):
        parts.append("Trending hashtags: " + " ".join(trend["hashtags"][:15]))
    if trend.get("phrases"):
        parts.append("Trending phrases: " + "; ".join(trend["phrases"][:10]))
    if trend.get("title_patterns"):
        parts.append("Title formats working now: " + " | ".join(trend["title_patterns"][:6]))
    if trend.get("notes"):
        parts.append("Hot this week: " + trend["notes"])
    if trend.get("google_searches"):
        parts.append("Top Google searches today: " + ", ".join(trend["google_searches"][:10]))
    return "\n".join(parts)
