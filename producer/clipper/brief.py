"""Brief analyst: campaign page text + reference docs -> machine-checkable checklist."""
from pydantic import BaseModel, Field

from . import db, llm


class Checklist(BaseModel):
    summary: str = Field(description="One paragraph: what the brand wants and why")
    content_requirements_verbatim: list[str] = Field(
        description="Every bullet under the campaign panel's 'Content requirements' heading, copied word for word. "
                    "Empty list only if that section truly is not present.")
    creator_requirements_verbatim: list[str] = Field(
        description="Every bullet under the panel's 'Creator requirements' heading, word for word")
    eligible_for_youtube: bool = Field(description="True if YouTube Shorts posts are accepted and paid")
    allowed_platforms: list[str] = Field(
        description="Platforms this campaign accepts posts on, from: youtube, instagram, tiktok, x, facebook. "
                    "Use the brief's own wording (e.g. 'TikTok and Instagram ONLY' -> [tiktok, instagram])")
    niche: str = Field(description="one of: gaming, esports, movies_tv, entertainment, music, tech, ai, finance, "
                                   "crypto, gambling, lifestyle, beauty, toys, manifestation, "
                                   "selfimprovement, faith, christian, inspirational, other")
    account_must_be_dedicated: bool = Field(
        description="True if the brief requires a brand-new or niche-dedicated account rather than any account")
    youtube_cpm: float | None = Field(description="$ per 1K views on YouTube, if stated")
    min_seconds: int | None = Field(description="Minimum clip length in seconds, if stated")
    max_seconds: int | None = Field(description="Maximum clip length in seconds, if stated")
    required_on_screen: list[str] = Field(description="Things that must appear in the video (logos, text, CTA, demographic info, etc.)")
    required_in_caption: list[str] = Field(
        description="The literal text that must appear in the title/description, one item each, exactly as it should "
                    "be published: '@handle', '#hashtag', a URL, or a required phrase. Not the instruction around "
                    "it: 'Tag @alichoucair in every post' -> '@alichoucair'")
    required_in_bio: list[str] = Field(description="Things required in the channel description/bio (e.g. 'sponsored by X')")
    forbidden: list[str] = Field(description="Things that get clips rejected (other brands, overly promotional tone, reuploads, etc.)")
    source_assets: list[str] = Field(description="URLs of footage/asset folders we may clip from")
    style_notes: list[str] = Field(description="Tone, format, and editing style guidance")
    recommended_formats: list[str] = Field(description="Which of: reaction, split_screen_gameplay, top_moments, text_on_screen, music_edit fit best")
    needs_manual_action: list[str] = Field(description="Things the pipeline cannot do automatically and the human must do")
    application_answers_needed: list[str] = Field(description="Questions asked by the application/waitlist form, if any")
    red_flags: list[str] = Field(description="Reasons this campaign may not be worth it (low pay, strict rules, near-empty budget)")
    content_language: str = Field(
        description="Main spoken language of the source footage and the campaign's target audience, lowercase "
                    "English name (english, portuguese, spanish, hindi, ...). 'none' if the footage has no speech.")
    production_blockers: list[str] = Field(
        description="Human-only steps needed BEFORE a compliant clip can be made, posted and its link submitted: the "
                    "creator's face or voice, brand approval of each clip before posting, a login- or terms-gated "
                    "download, attaching "
                    "an in-app sound, a brand-new or brand-named account the roster doesn't have, a bio edit, a "
                    "manual form at submission time. Empty if an unattended pipeline can do all of it. Do NOT list "
                    "the campaign's one-time application to join: applications are handled automatically.")
    payout_steps: list[str] = Field(
        description="Human-only steps needed only AFTER a clip performs, to get it paid: audience-demographic "
                    "screenshots or screen recordings, a payout form, messaging analytics. Empty if none.")
    auto_ok: bool = Field(description="True only if production_blockers AND payout_steps are both empty")
    auto_blockers: list[str] = Field(description="production_blockers + payout_steps (kept for older code)")


SYSTEM = (
    "You analyze Whop Content Rewards clipping campaign briefs for a clipper who posts from these accounts:\n"
    "- YouTube @PogingPanda (established gaming / playthrough / trailer channel, ~50% US audience)\n"
    "- YouTube @ericknox2802 (crypto / finance)\n"
    "- YouTube @modernmanifestationsecrets (manifestation / self-improvement)\n"
    "- YouTube @destinedforgreatness777 (faith / Christian / inspirational, 8.4K subscribers)\n"
    "- Instagram @kiwinoygamer (gaming), @iamcryptohustler (crypto), @modernmanifestationsecrets "
    "(manifestation), @destinedforgreatness777 (faith)\n"
    "- TikTok @iamcryptohustler (crypto), @mnfsttnsecrets (manifestation), @iamdestinedforgreatness (faith)\n"
    "- X @MostViewedClips (clips), X @cryptoericknox (crypto) — not connected yet\n"
    "Judge account-eligibility rules against this roster. Extract every rule precisely; never invent rules "
    "that are not in the text. If something is not stated, use null or an empty list."
)


def analyze(campaign_id: str, brief_text: str) -> Checklist:
    from . import references

    c = db.get("campaigns", campaign_id) or {"id": campaign_id}
    refs, media = references.gather(campaign_id, brief_text)
    prompt = (f"Campaign: {c.get('title', campaign_id)}\n\n<campaign_panel>\n{brief_text}\n</campaign_panel>\n\n"
              f"<reference_materials>\n{refs or 'none'}\n</reference_materials>\n\n"
              f"<media_links>\n{chr(10).join(media) or 'none'}\n</media_links>\n\n"
              "Read the panel's 'Content requirements' and 'Creator requirements' sections first and copy their "
              "bullets verbatim; they are binding. Reference materials add detail and override the panel when "
              "more specific, but never drop a panel requirement.")
    result = llm.ask(prompt, Checklist, SYSTEM)
    db.upsert("campaigns", {"id": campaign_id, "checklist": result.model_dump()})  # status (joined etc.) untouched
    return result
