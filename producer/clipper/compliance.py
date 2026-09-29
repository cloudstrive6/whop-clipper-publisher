"""Compliance audit: check a finished clip against every campaign rule before it can be approved.

Claude looks at real frames from the rendered clip, its transcript, and its title/description,
and rules on each requirement from the panel, the reference docs and any PDF rulebook.
"""
import subprocess
from pathlib import Path

from pydantic import BaseModel, Field

from . import db, llm
from .config import campaign_dir
from .render import probe_summary
from .transcribe import as_timestamped_text, transcribe


class Check(BaseModel):
    requirement: str = Field(description="the rule, quoted as closely as possible")
    source: str = Field(description="panel | reference_doc | pdf | platform_policy")
    verdict: str = Field(description="pass | fail | needs_human")
    evidence: str = Field(description="what in the frames, transcript or caption supports this verdict")


class Audit(BaseModel):
    overall_pass: bool = Field(description="False if any requirement is 'fail'")
    checks: list[Check]
    blocking_issues: list[str] = Field(description="Plain-language list of what must change before posting")
    human_todo: list[str] = Field(description="Requirements only the human can satisfy (account/bio/follows/geo)")
    fix_suggestions: list[str] = Field(description="Concrete fixes, e.g. exact caption text to use")
    safe_to_autopost: bool = Field(
        description="True only if nothing failed AND every rule about the clip itself (content, footage source, "
                    "caption, tags, disclosure, length, tone, forbidden topics) passed with evidence. The only "
                    "needs_human items allowed are account-level ones the clip can't affect (bio, follows, audience "
                    "geography, keeping the post up, payout paperwork). If in doubt, False.")


SYSTEM = (
    "You are a strict compliance reviewer for paid clipping campaigns. A clip that breaks any stated rule is "
    "rejected and unpaid, so be conservative: if a requirement cannot be verified from the evidence, mark it "
    "'needs_human' rather than 'pass'. Judge only this clip against the rules given. Do not invent rules."
)


def contact_sheet(video: Path, out: Path, n: int = 4) -> Path:
    """One image with n frames sampled across the clip (cheaper than n separate images)."""
    dur = probe_summary(video)["duration"]
    stamps = [dur * f for f in (0.04, 0.35, 0.62, 0.9)][:n]
    parts = []
    for i, t in enumerate(stamps):
        p = out.parent / f"{out.stem}_f{i}.png"
        subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-ss", f"{t:.2f}",
                        "-i", str(video), "-frames:v", "1", "-vf", "scale=620:-1", str(p)], check=True)
        parts.append(p)
    inputs = [x for p in parts for x in ("-i", str(p))]
    subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", *inputs,
                    "-filter_complex", f"hstack={len(parts)}", str(out)], check=True)
    for p in parts:
        p.unlink(missing_ok=True)
    return out


def audit_clip(clip: dict, campaign: dict) -> Audit:
    ck = campaign.get("checklist") or {}
    refs = campaign_dir(campaign["id"]) / "references.md"
    ref_text = refs.read_text(encoding="utf-8", errors="replace")[:25000] if refs.exists() else "none"
    video = Path(clip["file"])
    sheet = contact_sheet(video, video.with_name(video.stem + "_sheet.png"))
    segs = [s for s in transcribe(Path(clip["source"]))
            if s["end"] > clip["start"] and s["start"] < clip["end"]]
    meta = clip["meta"]
    probe = probe_summary(video)
    from .config import cfg
    from .targets import for_campaign

    destinations = ", ".join(t["label"] for t in for_campaign(campaign)) or "none"
    geo = cfg()["youtube"].get("geo_location", "not set")
    prompt = f"""Audit this finished Short against every rule below. The image is a contact sheet of 4 frames
sampled across the clip (left to right = start to end).

<campaign>{campaign['title']}</campaign>

<panel_content_requirements>
{chr(10).join(ck.get('content_requirements_verbatim', [])) or 'none listed'}
</panel_content_requirements>

<panel_creator_requirements>
{chr(10).join(ck.get('creator_requirements_verbatim', [])) or 'none listed'}
</panel_creator_requirements>

<required_in_caption>{ck.get('required_in_caption')}</required_in_caption>
<required_on_screen>{ck.get('required_on_screen')}</required_on_screen>
<required_in_bio>{ck.get('required_in_bio')}</required_in_bio>
<forbidden>{ck.get('forbidden')}</forbidden>
<handled_by_the_human_at_payout>{ck.get('payout_steps') or 'none'}</handled_by_the_human_at_payout>
(Requirements listed under handled_by_the_human_at_payout - e.g. "include demographic information", audience
screenshots, payout forms - are supplied by the account owner when a clip reaches payout, not by the video or
caption. Do not fail the clip or set safe_to_autopost false because of them; list them in human_todo.)
<style_notes>{ck.get('style_notes')}</style_notes>
<length_rules>min={ck.get('min_seconds')}s max={ck.get('max_seconds')}s</length_rules>

<reference_materials>
{ref_text}
</reference_materials>

<clip>
file: {video.name}
duration: {probe['duration']:.1f}s, {probe['width']}x{probe['height']}, audio={probe['has_audio']}
title: {meta.get('title')}
description: {meta.get('description')}
hashtags: {' '.join(meta.get('hashtags', []))}
on-screen hook added by us: {meta.get('hook_text')}
source file: {Path(clip['source']).name}
KNOWN FACTS you may rely on (verified by the pipeline, treat as established):
- the source file was downloaded directly from this campaign's official asset folder; provenance is confirmed
- no music or audio was added: the audio is exactly the source clip's own audio, untouched
- {meta.get('edit_note') or 'the clip is a straight cut of the source'}
- the only additions are text overlays rendered over the frame: the hook text, word-by-word captions (horizontal sources only){f", and a '{meta['disclosure']}' disclosure badge (top right, whole clip)" if meta.get('disclosure') else ''}.
  {'' if meta.get('disclosure') else 'No on-screen disclosure badge was added, because the campaign rules do not ask for one.'}
- TikTok posts are published with TikTok's branded-content flag switched on
- destinations are exactly: {destinations}. Nothing is posted anywhere else.
- at publish time the post carries the location "{geo}" in the platform's recording-location field
  (YouTube Shorts has no native geo-tag control; this is the closest equivalent the platform offers)
- the post stays live for at least 30 days unless the campaign says otherwise
transcript of this segment:
{as_timestamped_text(segs) or '(no speech)'}
</clip>

{('REVIEWERS ALREADY REJECTED CLIPS FROM THIS CAMPAIGN: ' + str((campaign.get('data') or {}).get('rejections', [])[-5:]) + ' - fail this clip if it repeats anything that could explain those rejections.') if (campaign.get('data') or {}).get('rejections') else ''}
The hook text and title must only state what the featured client actually says in this clip: FAIL them if they
present an interviewer's question or another person's line as the client's claim. The caption must follow the
reference materials' required format and call to action exactly (e.g. END with the booking CTA and tag if asked).
Check every requirement, including: required caption text/tags/disclosure present; length; forbidden words or
claims in the title, description, hook and speech; footage came from the approved source; on-screen elements;
tone (does it read as a real clip rather than an ad?). Mark account-level rules (bio, follows, audience
geography, keeping the post up) as needs_human.
The title and description are published verbatim: FAIL them if they contain brief/instruction wording aimed at
clippers (e.g. "Tag @x in every post") instead of viewer-facing text.
Judge disclosure only against what the campaign's own rules and reference materials require. General
platform-policy questions (e.g. whether YouTube's paid-promotion box should be ticked) go in human_todo and do not
make safe_to_autopost false by themselves."""
    return llm.ask(prompt, Audit, SYSTEM, files=[str(sheet)])


def audit_campaign(campaign_id: str, only_status: tuple[str, ...] = ("rendered", "qa_failed"),
                   only_new: bool = False) -> list[dict]:
    campaign = db.get("campaigns", campaign_id)
    out = []
    for clip in db.rows("clips", "campaign_id=?", campaign_id):
        if clip["status"] not in only_status:
            continue
        if only_new and (clip.get("qa") or {}).get("audit"):
            continue
        a = audit_clip(clip, campaign)
        qa = clip.get("qa") or {}
        qa["audit"] = a.model_dump()
        status = "rendered" if (a.overall_pass and qa.get("ok", True)) else "qa_failed"
        db.upsert("clips", {"id": clip["id"], "qa": qa, "status": status})
        fails = [c for c in a.checks if c.verdict == "fail"]
        print(f"[{'PASS' if a.overall_pass else 'FAIL'}] {clip['id'][-30:]}  "
              f"{len(a.checks)} checks, {len(fails)} failed")
        for c in fails:
            print(f"    x {c.requirement[:90]} -> {c.evidence[:110]}")
        out.append({"id": clip["id"], "pass": a.overall_pass, "audit": a.model_dump()})
    return out
