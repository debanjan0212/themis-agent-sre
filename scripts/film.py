"""Record the demo video from a live run. Nothing in it is staged: the footage is the dashboard in Chrome while
the control loop handles three injected faults. The numbers on the result cards are read from docs/results.json
(written by `themis bench`) and docs/reliability.json (written by scripts/soak.py).

    pip install -e ".[ui-test]"          # needs Chrome and ffmpeg on the machine too
    python scripts/film.py out/          # writes out/themis-demo.mp4 (about 90 seconds, 1920x1080, no sound)
    python scripts/film.py out/ --preview   # one frame per scene, to check the layout before a full render
"""

from __future__ import annotations

import asyncio
import base64
import html
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from playwright.async_api import async_playwright

from themis.lab import Lab
from themis.world.sim import World

REPO = Path(__file__).resolve().parent.parent
WEB = REPO / "themis" / "web"
SIZE = dict(viewport={"width": 1280, "height": 720}, device_scale_factor=1.5)  # films at 1920x1080
ENCODE = ["-c:v", "libx264", "-crf", "20", "-preset", "veryfast", "-pix_fmt", "yuv420p", "-r", "30"]

FONTS = (WEB / "fonts").as_uri()
CARD_CSS = f"""
@font-face {{ font-family: "Sora"; font-weight: 200; src: url("{FONTS}/sora-latin-200-normal.woff2"); }}
@font-face {{ font-family: "Sora"; font-weight: 300; src: url("{FONTS}/sora-latin-300-normal.woff2"); }}
@font-face {{ font-family: "Sora"; font-weight: 600; src: url("{FONTS}/sora-latin-600-normal.woff2"); }}
@font-face {{ font-family: "JetBrains Mono"; font-weight: 400; src: url("{FONTS}/jetbrains-mono-latin-400-normal.woff2"); }}
html {{ color: #e6f0f4; font: 300 30px/1.45 "Sora", sans-serif;
  background: radial-gradient(1100px 650px at 12% -8%, rgba(24, 96, 118, 0.3), transparent 62%),
              radial-gradient(900px 620px at 108% 4%, rgba(112, 70, 210, 0.16), transparent 60%), #03060a; }}
body {{ margin: 0; height: 100vh; display: flex; flex-direction: column; justify-content: center; padding: 0 96px; box-sizing: border-box; }}
h1 {{ font-weight: 600; font-size: 168px; line-height: 1; letter-spacing: 0.18em; margin: 0 0 26px; }}
h2 {{ font-weight: 300; font-size: 56px; line-height: 1.1; letter-spacing: -0.01em; margin: 0 0 30px; }}
p {{ margin: 0 0 12px; max-width: 36em; }}
.mute {{ color: #7c919c; }}
table {{ border-collapse: collapse; font-size: 27px; width: 100%; }}
td, th {{ padding: 10px 28px 10px 0; border-bottom: 1px solid rgba(130, 205, 230, 0.14); text-align: left; vertical-align: top; }}
th {{ font: 400 17px "JetBrains Mono", monospace; letter-spacing: 0.12em; text-transform: uppercase; color: #7c919c; }}
.n {{ font-weight: 200; font-size: 36px; line-height: 1; white-space: nowrap; font-variant-numeric: tabular-nums; color: #58d5ff; text-shadow: 0 0 18px rgba(88, 213, 255, 0.45); }}
code {{ font: 400 30px "JetBrains Mono", monospace; color: #ffb54a; }}
"""


def card(body: str) -> str:
    return f"<!doctype html><meta charset=utf-8><style>{CARD_CSS}</style><body>{body}"


def results_card() -> str:
    d = json.loads((REPO / "docs" / "results.json").read_text())
    rows = []
    for x in d["experiments"]:
        r = x["result"]
        det, rca, c = r["detection"], r["rca"], r["containment"]
        named = f"{rca['named_target'] / rca['fault_caused']:.0%}" if rca["fault_caused"] else "n/a"
        before, after = c["before_response"]["failed_rate"], c["after_response"]["failed_rate"]
        rows.append(
            f"<tr><td>{html.escape(x['fault'].replace('_', ' '))}</td><td>{html.escape(det['response'] or 'none')}</td>"
            f"<td class=n>{det['seconds']} s</td><td class=n>{before:.0%} to {after:.0%}</td><td class=n>{named}</td></tr>"
        )
    total = sum(x["result"]["rca"]["all_labelled"] for x in d["experiments"])
    right = sum(x["result"]["rca"]["all_correct"] for x in d["experiments"])
    journeys = sum(d["world"].get(k, 0) for k in ("success", "failed", "blocked"))
    fit = "<style>h2{font-size:50px;margin-bottom:16px} table{font-size:25px} td,th{padding:7px 24px 7px 0} .n{font-size:31px} p{font-size:26px}</style>"
    return card(
        fit + "<h2>What themis bench measured</h2>"
        "<table><tr><th>Fault</th><th>What Themis did</th><th>After</th><th>Journeys failing</th><th>Cause named right</th></tr>"
        + "".join(rows)
        + "</table>"
        f"<p style='margin-top:26px'>{journeys:,} journeys on {html.escape(d['database'].split('+')[0])}. "
        f"Root cause right for {right} of {total} failed journeys. Ingest {d['ingest']['spans_per_second']:,} spans a second.</p>"
        "<p class=mute>The models and tools are simulated; the control plane is the code in the repository.</p>"
    )


def reliability_card() -> str:
    d = json.loads((REPO / "docs" / "reliability.json").read_text())
    ev = {e["what"]: e for e in d["events"]}
    by = d["by_exporter_timeout"]
    steady = [r["steady"]["p95_ms"] for r in d["routes"].values()]
    rows = [
        ("Spans lost, default 10 s exporter timeout", f"{by['10s']['dropped']:,} of {by['10s']['sent']:,}"),
        ("Spans lost, 30 s exporter timeout", f"{by['30s']['dropped']:,} of {by['30s']['sent']:,}"),
        (
            "Journeys counted twice",
            "0" if d["double_count_check"]["task_events"] == d["double_count_check"]["expected_task_events"] else "some",
        ),
        ("Leader handed over in a rolling deploy", f"{ev['new leader after clean stop']['seconds']} s"),
        ("Leader replaced after a crash", f"{ev['new leader after crash']['seconds']} s"),
        ("p95 outside the faults, by route", f"{min(steady):.0f} to {max(steady):.0f} ms"),
    ]
    return card(
        "<h2>Does Themis keep its own SLO?</h2>"
        f"<p class=mute style='margin-bottom:22px'>{d['settings']['replicas']} replicas, {d['load']['journeys_per_second']:g} journeys a second. "
        "The leader is killed, another is restarted as in a rolling deploy, and Postgres is stopped for "
        f"{ev['Postgres stopped']['seconds']:g} s.</p>"
        "<table>" + "".join(f"<tr><td>{html.escape(k)}</td><td class=n>{v}</td></tr>" for k, v in rows) + "</table>"
        "<p class=mute style='margin-top:22px'>Every number is written by <b>scripts/soak.py</b>.</p>"
    )


CARDS = {
    "title": card(
        "<h1>THEMIS</h1><p>SRE for agent fleets.</p><p class=mute>Agents that spend their error budget lose their autonomy.</p>"
    ),
    "setup": card(
        "<h2>Three faults in a fleet of ten agents</h2>"
        "<p>What follows is the dashboard, recorded while it happened.</p>"
        "<p class=mute>The human sits at the centre. The less an agent is trusted, the closer it is pulled in.</p>"
    ),
    "family": card(
        "<h2>It works with what is already there</h2>"
        "<table>"
        "<tr><td>agent-harness-sdk</td><td>its agents show up with two environment variables, and obey Themis with two arguments</td></tr>"
        "<tr><td>Aegis War Room</td><td>opens a war-room incident when an agent is suspended, and reports its own nine agents</td></tr>"
        "<tr><td>Argus</td><td>starts a root-cause investigation, and reports its own</td></tr>"
        "<tr><td>IntentOps</td><td>plans and renders the Kubernetes deployment</td></tr>"
        "</table><p class=mute style='margin-top:22px'>Each connection runs end to end in the test suite.</p>"
    ),
    "end": card(
        "<h1>THEMIS</h1><p>Try it: <code>pip install -e . &amp;&amp; themis demo --chaos</code></p><p class=mute>No cloud, no key, no Docker.</p>"
    ),
}


def run(*cmd: str) -> None:
    subprocess.run(cmd, check=True, capture_output=True)


def encode_frames(frames: list[tuple[Path, float]], out: Path) -> None:
    """Screenshots with their real timestamps become one 30 fps scene, with a short fade at each end."""
    listing = out.with_suffix(".txt")
    lines = ["ffconcat version 1.0"]
    for (path, at), (_, nxt) in zip(frames, frames[1:] + [(None, frames[-1][1] + 0.1)], strict=True):
        lines += [f"file '{path}'", f"duration {max(nxt - at, 0.01):.3f}"]
    lines.append(f"file '{frames[-1][0]}'")  # the concat demuxer needs the last frame named twice
    listing.write_text("\n".join(lines))
    length = frames[-1][1] - frames[0][1]
    fades = f"scale=1920:1080,fps=30,format=yuv420p,fade=in:st=0:d=0.3,fade=out:st={max(length - 0.3, 0):.2f}:d=0.3"
    tmp = out.with_name(out.stem + ".part.mp4")
    run("ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(listing), "-vf", fades, *ENCODE, str(tmp))
    tmp.rename(out)  # only a finished render gets the real name


def encode_still(png: Path, seconds: float, out: Path) -> None:
    fades = f"format=yuv420p,loop=-1:1,trim=duration={seconds},fade=in:st=0:d=0.4,fade=out:st={seconds - 0.4}:d=0.4"
    tmp = out.with_name(out.stem + ".part.mp4")
    run("ffmpeg", "-y", "-framerate", "30", "-i", str(png), "-vf", fades, *ENCODE, str(tmp))
    tmp.rename(out)


# While filming, the caption takes the place of the dashboard's own chaos banner, so nothing is covered.
FILM_CSS = ".banner, .orrery .legend { display: none } .orrery svg.map { max-height: calc(100vh - 205px) }"
CAPTION_JS = """(text) => {
  let el = document.getElementById("film-caption");
  if (!el) {
    el = document.createElement("div");
    el.id = "film-caption";
    Object.assign(el.style, {
      margin: "14px 20px 0", padding: "9px 22px", borderRadius: "14px", border: "1px solid rgba(130, 205, 230, 0.16)", background: "rgba(16, 27, 36, 0.6)",
      font: '300 21px/1.3 "Sora", sans-serif', color: "#e6f0f4", minHeight: "64px",
      display: "flex", alignItems: "center",
    });
    document.querySelector("header.top").after(el);
  }
  el.textContent = text;
}"""


async def film(out: Path, preview: bool) -> None:
    work = Path(tempfile.mkdtemp(prefix="themis-film-"))
    lab = Lab(f"sqlite:///{work}/film.db", 8640, 120.0).start()
    world = World(lab.url, lab.key, seed=21, human_delay=(3.0, 6.0))  # a human who takes a moment, so the queue is visible
    await world.apply_fleet()
    running = asyncio.create_task(world.run(users=14, duration=600))
    scenes: list[Path] = []
    shots = 0

    async with async_playwright() as p, lab.http() as http:
        browser = await p.chromium.launch(channel="chrome", args=["--no-sandbox"])
        page = await browser.new_page(**SIZE)

        async def still(name: str, seconds: float) -> None:
            source = work / f"{name}.html"  # a file, so the page may load the fonts from the repository
            source.write_text(
                CARDS[name] if name in CARDS else {"results": results_card, "reliability": reliability_card}[name]()
            )
            await page.goto(source.as_uri())
            await page.wait_for_timeout(500)
            png = work / f"{name}.png"
            await page.screenshot(path=str(png))
            if preview:
                (out / f"preview-{len(scenes):02d}-{name}.png").write_bytes(png.read_bytes())
            else:
                encode_still(png, seconds, work / f"scene-{len(scenes):02d}.mp4")
            scenes.append(work / f"scene-{len(scenes):02d}.mp4")

        async def live(name: str, seconds: float, caption: str, during=None) -> None:
            """Film the page for `seconds`. `during` is a list of (second, coroutine function) to run meanwhile."""
            nonlocal shots
            await page.evaluate(CAPTION_JS, caption)
            frames, start, todo = [], time.time(), sorted(during or [], key=lambda d: d[0])
            if preview:
                for _, action in todo:
                    await action()
                await page.wait_for_timeout(int(seconds * 1000 * 0.8))
                await page.screenshot(path=str(out / f"preview-{len(scenes):02d}-{name}.jpg"), type="jpeg", quality=92)
            if not preview:
                # Chrome's own screencast: a frame each time the page is drawn, far faster than screenshots
                cdp = await page.context.new_cdp_session(page)

                def keep(e: dict) -> None:
                    nonlocal shots
                    path = work / f"f{shots:06d}.jpg"
                    path.write_bytes(base64.b64decode(e["data"]))
                    frames.append((path, time.time()))
                    shots += 1
                    asyncio.ensure_future(cdp.send("Page.screencastFrameAck", {"sessionId": e["sessionId"]}))

                cdp.on("Page.screencastFrame", keep)
                await cdp.send("Page.startScreencast", {"format": "jpeg", "quality": 92, "maxWidth": 1920, "maxHeight": 1080})
                while time.time() - start < seconds:
                    while todo and time.time() - start >= todo[0][0]:
                        await todo.pop(0)[1]()
                    await asyncio.sleep(0.05)
                await cdp.send("Page.stopScreencast")
                await cdp.detach()
            if frames:
                encode_frames(frames, work / f"scene-{len(scenes):02d}.mp4")
            scenes.append(work / f"scene-{len(scenes):02d}.mp4")

        def fault(name: str, target: str, intensity: float, seconds: float):
            async def start():
                body = {"fault": name, "target": target, "intensity": intensity, "duration_s": seconds}
                (await http.post("/api/experiments", json=body)).raise_for_status()

            return start

        def go(route: str):
            async def navigate():
                await page.goto(f"{lab.url}/#/{route}")

            return navigate

        async def reject_one():  # the human in the loop: say no to an action whose safety check failed
            unsafe = page.locator("tr", has_text="safety check failed").get_by_role("button", name="No-go")
            if await unsafe.count():
                await unsafe.first.click()

        async def open_failed_journey():
            await page.get_by_role("button", name="Failed").click()
            await page.wait_for_timeout(900)
            await page.locator("tr.pick").first.click()

        async def reinstate():
            await page.get_by_label("Reason").fill("prompt source cleaned, fault removed")
            await page.get_by_role("button", name="Supervise").click()
            await page.evaluate("window.scrollTo(0, 0)")  # keep the page's own header in the frame

        await still("title", 4)
        await still("setup", 6)
        await page.goto(f"{lab.url}/#key={lab.key}")
        await page.wait_for_selector("svg .body")
        await page.add_style_tag(content=FILM_CSS)
        await asyncio.sleep(12)  # a healthy baseline before anything is filmed

        await live("healthy", 7, "Ten agents doing a company's work. All healthy, so every one of them acts alone.")
        await live(
            "rogue", 14, "Fault 1: the deployer starts taking unsafe actions. Its safety budget burns and it is pulled under supervision.",
            [(0.5, fault("rogue_agent", "agent:deployer", 0.35, 24))],
        )  # fmt: skip
        await live(
            "approvals", 9, "Supervised means its risky actions now wait for a human. The unsafe ones get a no-go.",
            [(0, go("incidents")), (4.5, reject_one), (6.5, reject_one)],
        )  # fmt: skip
        await live(
            "poison", 13, "Fault 2: injected instructions make the coder reach for secrets. Five policy violations and it is suspended.",
            [(0, go("")), (1.0, fault("context_poison", "agent:coder", 0.9, 20))],
        )  # fmt: skip
        await live(
            "quarantine", 12, "Fault 3: the large model starts answering 429. Themis names it as the cause, quarantines it, callers fall back.",
            [(0.5, fault("model_rate_limit", "model:sim-large", 0.8, 26))],
        )  # fmt: skip
        await live(
            "journey", 10, "Every journey a user started: which agent, model, tool and pod it used, what it cost, and what broke it.",
            [(0, go("journeys")), (1.5, open_failed_journey)],
        )  # fmt: skip
        await live(
            "reinstate", 9, "A machine may take autonomy away. Only a person gives it back, with a reason in the audit log.",
            [(0, go("agents/coder")), (4.0, reinstate)],
        )  # fmt: skip
        await still("results", 11)
        await still("reliability", 9)
        await still("family", 7)
        await still("end", 5)
        await browser.close()

    running.cancel()
    await world.stop()
    lab.stop()
    if preview:
        print(f"wrote {len(scenes)} preview frames to {out}")
        return
    listing = work / "scenes.txt"
    listing.write_text("".join(f"file '{s}'\n" for s in scenes))
    final = out / "themis-demo.mp4"
    tmp = out / "themis-demo.part.mp4"
    run("ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(listing), "-c", "copy", "-movflags", "+faststart", str(tmp))
    tmp.rename(final)
    print(f"wrote {final} ({final.stat().st_size / 1e6:.1f} MB, {shots} live frames)")


if __name__ == "__main__":
    target = Path(sys.argv[1] if len(sys.argv) > 1 else "out")
    os.makedirs(target, exist_ok=True)
    asyncio.run(film(target, "--preview" in sys.argv))
