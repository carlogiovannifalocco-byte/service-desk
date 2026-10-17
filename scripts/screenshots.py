"""Take the README screenshots from a running demo instance.

    SERVICE_DESK_DEMO=1 uvicorn service_desk.main:app --port 8000
    python scripts/screenshots.py            # writes docs/*.png

Set BASE_URL to point elsewhere and CHROMIUM to use a specific Chromium binary.
Needs `pip install playwright pillow`.
"""

import os
from pathlib import Path

from PIL import Image
from playwright.sync_api import sync_playwright

BASE = os.getenv("BASE_URL", "http://127.0.0.1:8000/")
OUT = Path(__file__).resolve().parent.parent / "docs"


def settle(page):
    page.wait_for_function("!document.querySelector('#toast').classList.contains('show')", timeout=6000)
    page.evaluate("window.scrollTo(0, 0)")
    page.wait_for_timeout(300)


def home(page):
    page.goto(BASE)
    page.wait_for_selector(".chip")


def plan(page):
    page.goto(BASE + "#plan")
    page.wait_for_selector(".task")
    page.check("input[name=task][value=residency]")
    page.fill("#city", "Milano")
    page.fill("#country", "Brazil")
    page.fill("#situation", "Moved into a rented flat last week")
    page.click("#plan-go")
    page.wait_for_selector(".timeline")
    for tick in ("s0", "d0", "d2"):
        page.check(f"[data-tick={tick}]")
    settle(page)


def letter(page):
    page.goto(BASE + "#letter")
    page.click("#sample")
    page.click("#letter-go")
    page.wait_for_selector(".deadline")
    settle(page)


def scroll_to(selector):
    def run(page):
        page.evaluate(f"document.querySelector('{selector}').scrollIntoView()")
        page.evaluate("window.scrollTo(0, window.scrollY - 16)")
    return run


SHOTS = [  # name, steps, viewport, color scheme, full page
    ("home", [home], (1280, 900), "light", True),
    ("plan", [plan], (1280, 900), "light", True),
    ("letter", [letter], (1280, 900), "light", True),
    ("m-home", [home], (390, 844), "light", False),
    ("m-plan", [plan, scroll_to(".plan-head")], (390, 844), "light", False),
    ("m-letter", [letter, scroll_to(".lt-head")], (390, 844), "dark", False),
]

with sync_playwright() as p:
    browser = p.chromium.launch(executable_path=os.getenv("CHROMIUM") or None)
    for name, steps, (w, h), scheme, full in SHOTS:
        ctx = browser.new_context(viewport={"width": w, "height": h}, device_scale_factor=2 if w < 500 else 1,
                                  color_scheme=scheme)
        page = ctx.new_page()
        for step in steps:
            step(page)
        page.wait_for_timeout(300)
        page.screenshot(path=str(OUT / f"{name}.png"), full_page=full)
        ctx.close()
    browser.close()

# The three phone screens side by side.
phones = [Image.open(OUT / f"m-{n}.png") for n in ("home", "plan", "letter")]
gap = 48
canvas = Image.new("RGB", (sum(i.width for i in phones) + gap * 4, max(i.height for i in phones) + gap * 2), (232, 228, 219))
x = gap
for im in phones:
    canvas.paste(im, (x, gap))
    x += im.width + gap
canvas.save(OUT / "mobile.png", optimize=True)
for n in ("home", "plan", "letter"):
    (OUT / f"m-{n}.png").unlink()
print("wrote", sorted(p.name for p in OUT.glob("*.png")))
