"""QA: check a rendered clip against technical specs and the campaign checklist."""
import re
from pathlib import Path

from .config import cfg
from .render import probe_summary


def check(file: Path, meta: dict, checklist: dict | None) -> dict:
    e = cfg()["editing"]
    ck = checklist or {}
    p = probe_summary(file)
    issues, manual = [], []
    lo = ck.get("min_seconds") or e["min_seconds"]  # the campaign's own minimum wins when stated
    hi = min(60, ck.get("max_seconds") or 60)
    if p["duration"] < lo - 0.5:
        issues.append(f"too short: {p['duration']:.1f}s < {lo}s")
    if p["duration"] > hi:
        issues.append(f"too long: {p['duration']:.1f}s > {hi}s")
    if (p["width"], p["height"]) != (e["width"], e["height"]):
        issues.append(f"wrong resolution {p['width']}x{p['height']}")
    if not p["has_audio"]:
        issues.append("no audio track")
    text = " ".join([meta.get("title", ""), meta.get("description", ""), " ".join(meta.get("hashtags", []))]).lower()
    import re

    for req in ck.get("required_in_caption", []):
        # same reading as _enforce_caption: "Tag @x in every post" requires "@x", not the sentence
        tokens = re.findall(r"https?://\S+|[@#][\w.]+", req) if " " in req.strip() else [req.strip()]
        for tok in (t.rstrip(".,;:)") for t in tokens):
            if tok and tok.lower() not in text:
                issues.append(f"caption missing required text: {tok!r}")
    # the campaign panel's own bullets are binding: enforce what is measurable, surface the rest
    for req in ck.get("content_requirements_verbatim", []) + ck.get("creator_requirements_verbatim", []):
        m = re.search(r"minimum (?:video )?length[^\d]*(\d+)\s*s", req, re.I)
        if m and p["duration"] < int(m.group(1)) - 0.5:
            issues.append(f"panel rule: {req} (clip is {p['duration']:.1f}s)")
            continue
        m = re.search(r'caption:\s*"([^"]+)"', req, re.I)
        if m and m.group(1).lower() not in text:
            issues.append(f"panel rule: {req}")
            continue
        if not m:
            manual.append(f"panel requirement: {req}")
    manual += [f"on-screen requirement: {r}" for r in ck.get("required_on_screen", [])]
    manual += [f"channel bio must contain: {r}" for r in ck.get("required_in_bio", [])]
    if ck and not ck.get("eligible_for_youtube", True):
        issues.append("campaign does not pay for YouTube")
    return {"ok": not issues, "issues": issues, "manual_checks": manual, "probe": p}
