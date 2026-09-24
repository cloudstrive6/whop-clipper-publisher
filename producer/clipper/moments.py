"""Moment picker: timestamped transcript + campaign checklist -> clip plan."""
from pydantic import BaseModel, Field

from . import llm
from .config import cfg
from .transcribe import as_timestamped_text


class Moment(BaseModel):
    start: float = Field(description="Clip start in seconds. Start ON the hook, no warm-up")
    end: float = Field(description="Clip end in seconds, right after the payoff")
    hook_text: str = Field(description="Short curiosity hook shown at the top of the screen, max 8 words, no emojis")
    title: str = Field(description="YouTube Shorts title, max 90 chars, curiosity-driven, not clickbait lies")
    description: str = Field(description="YouTube description, 1-2 sentences; include every required caption item")
    hashtags: list[str] = Field(description="3-6 hashtags incl. #shorts and any required ones")
    hook_type: str = Field(description="wild_statement | chaos_start | visual_action | peak_reaction | trend | question")
    why_viral: str = Field(description="One sentence: the emotion/intrigue that keeps people watching")
    score: int = Field(description="1-10 predicted virality")


class ClipPlan(BaseModel):
    moments: list[Moment]


SYSTEM = """You are an elite short-form clipper for YouTube Shorts. Rules from the Whop Clips course:
- The hook decides everything: start at a wild statement, chaos, visual action, peak reaction, trend, or a question.
- Pick moments that spark emotion (laughter, outrage, controversy, relatability) so people comment and share.
- Cut everything not essential; shorter beats padded. A clip must be self-contained and end on a payoff.
- "If you wouldn't watch it yourself, don't post it."
- Follow the campaign checklist exactly (lengths, required caption text, forbidden things)."""


class MultiMoment(Moment):
    source: str = Field(description="exact file name (from the <clip name=...> tag) this moment comes from")


class MultiPlan(BaseModel):
    moments: list[MultiMoment]


def pick_multi(transcripts: dict[str, list[dict]], durations: dict[str, float], checklist: dict | None,
               n: int) -> list[MultiMoment]:
    """One call for a batch of short pre-cut clips: choose the best n moments across all of them."""
    e = cfg()["editing"]
    lo = max((checklist or {}).get("min_seconds") or 0, 7)
    hi = min(e["max_seconds"], (checklist or {}).get("max_seconds") or 999)
    body = "\n\n".join(
        f'<clip name="{name}" duration="{durations[name]:.1f}">\n{as_timestamped_text(segs) or "(no speech)"}\n</clip>'
        for name, segs in transcripts.items())
    prompt = (
        f"These are short pre-cut clips from the campaign's approved folder. Choose the {n} strongest moments, "
        f"at most one per clip, each between {lo} and {hi} seconds, staying inside that clip's duration. "
        "A moment can be the whole clip if it is already tight. Prefer clips with a clear reaction, trash talk, "
        "whiff/pop-off, or caster hype. Hooks and titles must obey the checklist's forbidden words. "
        "The channel's audience is mostly US/English-speaking: skip clips whose speech is not English.\n\n"
        f"<campaign_checklist>\n{checklist or 'none provided'}\n</campaign_checklist>\n\n{body}"
    )
    plan = llm.ask(prompt, MultiPlan, SYSTEM)
    out, used = [], set()
    for m in sorted(plan.moments, key=lambda m: -m.score):
        if m.source not in durations or m.source in used:
            continue
        m.start = max(0.0, m.start)
        m.end = min(durations[m.source], max(m.end, m.start + lo))
        if m.end - m.start < lo - 0.5:
            continue
        m.end = min(m.end, m.start + hi)
        used.add(m.source)
        out.append(m)
    return out[:n]


def pick(segments: list[dict], checklist: dict | None, n: int | None = None) -> list[Moment]:
    e = cfg()["editing"]
    n = n or e["clips_per_source"]
    lo = max(e["min_seconds"], (checklist or {}).get("min_seconds") or 0)
    hi = min(e["max_seconds"], (checklist or {}).get("max_seconds") or 999)
    prompt = (
        f"Pick the {n} best non-overlapping clips, each between {lo} and {hi} seconds long.\n"
        f"Use only timestamps that exist in the transcript.\n\n"
        f"<campaign_checklist>\n{checklist or 'none provided'}\n</campaign_checklist>\n\n"
        f"<transcript>\n{as_timestamped_text(segments)}\n</transcript>"
    )
    plan = llm.ask(prompt, ClipPlan, SYSTEM)
    good = []
    for m in sorted(plan.moments, key=lambda m: -m.score):
        dur = m.end - m.start
        if dur < lo:
            m.end = m.start + lo
        elif dur > hi:
            m.end = m.start + hi
        if not any(m.start < g.end and g.start < m.end for g in good):
            good.append(m)
    return good[:n]
