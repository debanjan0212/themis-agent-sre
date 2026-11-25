"""Record the product page's imagery from a live run of the dashboard, then rebuild the page.

    python scripts/site_assets.py        # needs Chrome and playwright (pip install -e ".[ui-test]")

It runs the ten-agent world, injects three faults, and waits until they show. Then it saves the fleet map as
SVG (the page's centrepiece) and takes close-ups of a failed journey, an agent's budgets and the cause table
at twice the screen resolution. Nothing is drawn by hand: every picture is the dashboard itself.
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from pathlib import Path

from PIL import Image
from playwright.async_api import async_playwright

from themis.lab import Lab
from themis.world.sim import World

REPO = Path(__file__).resolve().parent.parent
ASSETS = REPO / "site" / "assets"
FAULTS = (
    ("rogue_agent", "agent:deployer", 0.35),
    ("context_poison", "agent:coder", 0.9),
    ("model_rate_limit", "model:sim-large", 0.8),
)


async def capture(raw: Path) -> None:
    lab = Lab(f"sqlite:///{raw}/assets.db", 8655, 120.0).start()
    world = World(lab.url, lab.key, seed=5, human_delay=(3.0, 6.0))
    await world.apply_fleet()
    running = asyncio.create_task(world.run(users=14, duration=240))
    await asyncio.sleep(14)
    try:
        async with async_playwright() as p, lab.http() as http:
            browser = await p.chromium.launch(channel="chrome", args=["--no-sandbox"])
            page = await browser.new_page(viewport={"width": 1440, "height": 900}, device_scale_factor=2)
            await page.goto(f"{lab.url}/#key={lab.key}")
            await page.wait_for_selector("svg .body")
            for fault, target, intensity in FAULTS:
                body = {"fault": fault, "target": target, "intensity": intensity, "duration_s": 60}
                (await http.post("/api/experiments", json=body)).raise_for_status()
            # wait until the map shows a supervised and a suspended agent
            for _ in range(40):
                await page.wait_for_timeout(1000)
                if await page.locator(".body.suspended").count() and await page.locator(".body.supervised").count():
                    break
            await page.wait_for_timeout(1500)  # let the agents finish sliding to their new orbits
            (ASSETS / "orrery.svg").write_text(await page.evaluate("document.querySelector('.orrery svg').outerHTML"))
            # a failed journey that crossed several agents, wide enough to be cropped past the frame
            wide = await browser.new_page(viewport={"width": 1800, "height": 1100}, device_scale_factor=2)
            await wide.goto(f"{lab.url}/#key={lab.key}")
            await wide.goto(f"{lab.url}/#/journeys")
            await wide.get_by_role("button", name="Failed").click()
            await wide.wait_for_timeout(1500)
            rows = wide.locator("tr.pick")
            for i in range(min(await rows.count(), 30)):
                await rows.nth(i).click()
                await wide.wait_for_selector(".water")
                if await wide.locator(".used > div").count() >= 4 and await wide.locator(".water .bad").count() >= 2:
                    break
            await wide.wait_for_timeout(600)
            await wide.locator(".used").screenshot(path=str(raw / "used.png"))
            # the close-up: give the journey's steps the whole width, so the picture stays sharp edge to edge
            await wide.add_style_tag(
                content="main{max-width:none}.split{grid-template-columns:1fr}.split>:first-child{display:none}"
            )
            await wide.wait_for_timeout(400)
            await wide.locator(".water").screenshot(path=str(raw / "waterfall.png"))
            kv = await wide.evaluate("[...document.querySelectorAll('.kv dd')].map(x => x.textContent)")
            title = await wide.evaluate("document.querySelector('#view h2').textContent")
            (ASSETS / "journey.json").write_text(json.dumps({"title": title, "kv": kv}, indent=2) + "\n")
            # close-ups of the part of each page that proves a number, not whole pages shrunk down
            for route, heading, name in (
                ("agents/deployer", "Targets and error budget", "agent"),
                ("incidents", "What is breaking journeys", "incidents"),
            ):
                await page.goto(f"{lab.url}/#/{route}")
                await page.wait_for_timeout(2500)
                await page.locator(".panel", has=page.get_by_role("heading", name=heading)).screenshot(
                    path=str(raw / f"{name}.png")
                )
            await browser.close()
    finally:
        running.cancel()
        await world.stop()
        lab.stop()


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        raw = Path(tmp)
        asyncio.run(capture(raw))
        for name, keep_height in (("waterfall", None), ("used", None), ("agent", None), ("incidents", None)):
            im = Image.open(raw / f"{name}.png").convert("RGB")
            if keep_height and im.height > keep_height:
                im = im.crop((0, 0, im.width, keep_height))
            im.save(ASSETS / f"{name}.jpg", quality=90, optimize=True, progressive=True)  # crisp, full colour
    sys.path.insert(0, str(REPO / "site"))
    import build

    print(f"wrote {build.build()}")


if __name__ == "__main__":
    main()
