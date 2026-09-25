"""Edits for campaigns whose official footage is shorter than the minimum clip length.

Some brands supply one short asset (e.g. a 6-second product demo) and require 10+ second posts made only
from that footage and its audio. Each variant stretches the asset honestly with its own footage:
a slow-motion replay of the key moment, a double play, or the whole clip slowed down.
"""
import re
import subprocess
from pathlib import Path

from pydantic import BaseModel, Field

from . import llm

VARIANTS = ("replay", "double", "slow")


def approved_lines(checklist: dict | None) -> list[str]:
    """On-screen lines the brand says to use word for word (quoted in the requirements), if any."""
    ck = checklist or {}
    blob = " ".join(ck.get("required_on_screen", []) + ck.get("style_notes", []))
    if not re.search(r"word for word|verbatim|approved|exactly", blob, re.I):
        return []
    lines = re.findall(r'"([^"]{8,140})"', " ".join(ck.get("required_on_screen", [])))
    return list(dict.fromkeys(lines))


def no_own_text(checklist: dict | None) -> bool:
    """True when the brand forbids adding our own on-screen text (captions, hooks)."""
    ck = checklist or {}
    blob = " ".join((ck.get("forbidden") or []) + (ck.get("style_notes") or []))
    return bool(re.search(r"(writ\w* your own|own on-?screen text|add\w* (your own |any )?text)", blob, re.I))


def _dur(p: Path) -> float:
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(p)],
                         capture_output=True, text=True).stdout.strip()
    return float(out or 0)


def make_variant(src: Path, kind: str, min_s: float, out: Path) -> Path:
    """A 16:9 edit at least min_s long, made only from src's own footage and audio."""
    d = _dur(src)
    if kind == "double":
        n = max(2, int(min_s // d) + 1)
        fc = f"[0:v]loop=loop={n - 1}:size=32767:start=0,setpts=N/FRAME_RATE/TB[v];[0:a]aloop=loop={n - 1}:size=2e9[a]"
    elif kind == "slow":
        f = max(min_s / d * 1.05, 1.0)  # slow the whole clip just enough; atempo keeps the pitch natural
        fc = f"[0:v]setpts={f:.3f}*PTS[v];[0:a]atempo={1 / f:.4f}[a]"
    else:  # replay: the clip, then its middle (usually the key moment) again at half speed
        a, b = d * 0.15, d * 0.75
        fc = (f"[0:v]split[v1][v2];[0:a]asplit[a1][a2];"
              f"[v2]trim={a:.2f}:{b:.2f},setpts=2*(PTS-STARTPTS)[vr];[a2]atrim={a:.2f}:{b:.2f},asetpts=PTS-STARTPTS,atempo=0.5[ar];"
              f"[v1][a1][vr][ar]concat=n=2:v=1:a=1[v][a]")
    out.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(src), "-filter_complex", fc, "-map", "[v]", "-map", "[a]",
                    "-c:v", "libx264", "-crf", "16", "-preset", "fast", "-c:a", "aac", "-b:a", "192k", str(out)],
                   check=True)
    if _dur(out) < min_s:  # safety: never hand back something shorter than the rule
        raise RuntimeError(f"{kind} variant came out {_dur(out):.1f}s, under {min_s}s")
    return out


class PostCopy(BaseModel):
    title: str = Field(description="short factual title")
    description: str = Field(description="1-2 sentence caption a genuine fan would write; factual, no hype")
    hashtags: list[str] = Field(description="relevant hashtags, respecting any limit in the rules")
    score: int = Field(description="1-10 how strong this post is")


class CopySet(BaseModel):
    posts: list[PostCopy]


def captions(checklist: dict | None, n: int, brief: str) -> list[PostCopy]:
    """n different caption sets written to the campaign's caption rules."""
    ck = checklist or {}
    return llm.ask(
        f"Write {n} different post captions for short videos of this campaign's official clip.\n\n"
        f"<brief>\n{brief[:5000]}\n</brief>\n<caption_rules>{ck.get('required_in_caption')}; "
        f"{ck.get('style_notes')}</caption_rules>\n<forbidden>{ck.get('forbidden')}</forbidden>\n\n"
        "Each caption: factual, in a real fan's voice, different wording from the others. Don't include the "
        "disclosure hashtag or required @mentions yourself (they're added in code); hashtags only relevant ones.",
        CopySet).posts[:n]
