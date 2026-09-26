"""Look-only diagnostic: open each queued campaign's Whop submit dialog in the cloud, record what it
contains (text, inputs, screenshot), then cancel. Submits nothing."""
import json, os, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))
import whop_submit as w
from playwright.sync_api import sync_playwright

titles = sys.argv[1:]
Path("artifacts").mkdir(exist_ok=True)
with sync_playwright() as p:
    b = p.chromium.launch(headless=True)
    ctx = b.new_context(storage_state=json.loads(os.environ["WHOP_SESSION"]), viewport={"width": 1440, "height": 900})
    page = ctx.new_page()
    for n, title in enumerate(titles):
        page.goto(w.MARKETPLACE, wait_until="domcontentloaded", timeout=90000)
        frame = w._app_frame(page); page.wait_for_timeout(3000); w._scroll(frame)
        frame = w._open_campaign(page, frame, title)
        print(f"== {title}: opened={frame is not None}")
        if frame is None:
            continue
        frame.locator("button:has-text('Submit clip')").first.click(timeout=20000)
        for secs in (3, 10, 25):
            page.wait_for_timeout(secs * 1000 - (0 if secs == 3 else 0))
            d = frame.locator("[role=dialog]")
            print(f"  after ~{secs}s: dialogs={d.count()}")
            for i in range(d.count()):
                print(f"    dialog {i}: {d.nth(i).inner_text()[:400]!r}")
                print("    inputs:", d.nth(i).evaluate("e => [...e.querySelectorAll('input,textarea,[contenteditable]')].map(x => [x.tagName, x.getAttribute('type'), x.placeholder])"))
            page.screenshot(path=f"artifacts/diag-{n}-{secs}s.png")
        page.keyboard.press("Escape")
    b.close()
