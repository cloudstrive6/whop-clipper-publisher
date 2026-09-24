"""Reference materials: fetch the text of every rules/brief doc a campaign links to.

Docs (Google Docs, Notion, web pages) are read as text for the brief analyst.
Media links (Drive folders, S3 files, YouTube channels) are listed as candidate source assets.
"""
import re
import urllib.request

from .config import campaign_dir, pw_channel

MEDIA = re.compile(r"drive\.google\.com/(drive/folders|file)|\.s3\.|youtube\.com|youtu\.be|\.(mp4|mov|mkv|webm|jpg|jpeg|png|gif)(\?|$)", re.I)


def links_from_brief(brief_text: str) -> list[str]:
    if "Links:" not in brief_text:
        return []
    urls = re.findall(r"https?://\S+", brief_text.split("Links:")[-1])
    return [u for u in dict.fromkeys(urls) if "whop.com" not in u]


def _gdoc(url: str) -> str:
    doc_id = re.search(r"/document/d/([\w-]+)", url).group(1)
    req = urllib.request.Request(f"https://docs.google.com/document/d/{doc_id}/export?format=txt",
                                 headers={"User-Agent": "Mozilla/5.0"})
    return urllib.request.urlopen(req, timeout=30).read().decode("utf-8", "replace")


def _rendered_text(urls: list[str]) -> dict[str, str]:
    """Notion and other JS-rendered pages: read via headless Chrome."""
    from playwright.sync_api import sync_playwright

    out = {}
    with sync_playwright() as p:
        b = p.chromium.launch(channel=pw_channel(), headless=True)
        page = b.new_page()
        for u in urls:
            try:
                page.goto(u, wait_until="domcontentloaded", timeout=60000)
                page.wait_for_timeout(6000)  # Notion hydrates slowly
                for _ in range(8):  # load lazy blocks
                    page.mouse.wheel(0, 4000)
                    page.wait_for_timeout(400)
                out[u] = page.inner_text("body")
            except Exception as err:
                out[u] = f"[could not load: {err.__class__.__name__}]"
        b.close()
    return out


def _pdf_text(path) -> str:
    from pypdf import PdfReader

    return "\n".join(p.extract_text() or "" for p in PdfReader(str(path)).pages)


def local_docs(campaign_id: str) -> str:
    """Rulebooks that arrive inside the footage folder (PDF/TXT/MD) are binding too."""
    out = []
    for f in sorted(campaign_dir(campaign_id).rglob("*")):
        if f.suffix.lower() == ".pdf":
            try:
                out.append(f"### Reference file: {f.name}\n{_pdf_text(f)[:30000]}")
            except Exception as err:
                out.append(f"### Reference file: {f.name}\n[could not read: {err.__class__.__name__}]")
        elif f.suffix.lower() in {".txt", ".md"} and f.name != "references.md":
            out.append(f"### Reference file: {f.name}\n{f.read_text(encoding='utf-8', errors='replace')[:30000]}")
    return "\n\n".join(out)


def gather(campaign_id: str, brief_text: str) -> tuple[str, list[str]]:
    """Returns (combined reference text, media/asset links). Also saves references.md."""
    links = links_from_brief(brief_text)
    media = [u for u in links if MEDIA.search(u)]
    docs = [u for u in links if u not in media]
    texts: dict[str, str] = {}
    rendered = []
    for u in docs:
        if "docs.google.com/document" in u:
            try:
                texts[u] = _gdoc(u)
            except Exception as err:
                texts[u] = f"[could not export Google Doc: {err}]"
        else:
            rendered.append(u)
    if rendered:
        texts.update(_rendered_text(rendered))
    combined = "\n\n".join(f"### Reference: {u}\n{t.strip()[:30000]}" for u, t in texts.items())
    if (local := local_docs(campaign_id)):
        combined = f"{combined}\n\n{local}" if combined else local
    (campaign_dir(campaign_id) / "references.md").write_text(
        combined + "\n\n### Media links\n" + "\n".join(media), encoding="utf-8")
    return combined, media
