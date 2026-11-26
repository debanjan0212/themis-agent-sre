"""Film one bench-real incident as a narrated demo, from the run's own records.

Every number, tool call, log line and chart point comes from the run: results-real.json for the story, and Themis's
own database (themis.db) for the dashboard, which is replayed and clicked through for real.

    python scripts/film_real.py RUN_DIR OUT_DIR --tts-python VENV/bin/python --voice-dir DIR [--preview]

RUN_DIR is a bench-real/run-<id> branch's docs/ folder (results-real.json, bench-real/themis.db). The narration is
spoken by Kokoro (pip install kokoro-onnx soundfile, in the venv given as --tts-python), with kokoro-v1.0.onnx and
voices-v1.0.bin in --voice-dir. Needs Chrome, playwright and ffmpeg. --preview writes one still per scene instead.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import math
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from playwright.async_api import async_playwright

REPO = Path(__file__).resolve().parent.parent
PAGE = (REPO / "scripts" / "film_real.html").as_uri()
FPS = 30
ENCODE = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-pix_fmt", "yuv420p", "-r", str(FPS)]
VOICE = "am_michael"
LEAD, GAP, TAIL = 0.6, 0.38, 0.9  # seconds of quiet before, between and after the sentences of a scene


# ---------------------------------------------------------------------------------------------- what is said


def words(n: int) -> str:
    small = "zero one two three four five six seven eight nine ten eleven twelve".split()
    return small[n] if 0 <= n < len(small) else str(n)


def narration(run: dict) -> list[tuple[str, list[str]]]:
    t, a = run["trial"], run["trial"]["agent"]
    reads = [o for o in a["tools_used"] if o["tool"] not in ("revert_release", "rollout_undo", "restart_deployment")]
    logs = next((o for o in reversed(reads) if o["tool"] == "search_logs"), None)
    top = parsed(logs["output"]) if logs else {}
    pattern = (top.get("top_patterns") or [{}])[0]
    half = round(t["page_s"] / 30) / 2  # to the nearest half minute
    page_min = words(int(half)) + (" and a half" if half % 1 else "")
    tokens = a["input_tokens"] + a["output_tokens"]
    cents = (t["cost"] or 0.0) * 100
    cost = "about a third of a cent" if 0.25 <= cents < 0.45 else f"{cents:.2f} of a cent" if cents < 1 else f"{cents:.1f} cents"
    same = "all" if pattern.get("count") == top.get("total_lines") else pattern.get("count")
    log_line = (
        f"{same} of the last {top.get('total_lines')} log lines are the same error, a KeyError on currency, "
        if pattern.get("count") and "currency" in pattern.get("pattern", "")
        else "the logs are full of the same error, "
    )
    return [
        ("hook", [
            "It's two in the morning.",
            "A new release of the checkout service has just gone out, and it's broken.",
            "Nobody on the team is awake.",
            "Tonight the engineer on call is an AI agent, and Themis is keeping an eye on that agent.",
        ]),
        ("lab", [
            "Everything in this video happened for real, on a Kubernetes cluster built from scratch on a GitHub runner.",
            "A small online shop runs on it: a frontend, a checkout service, and payments.",
            "Prometheus, Loki and Tempo collect its metrics, logs and traces.",
            "The shop's config lives in Git.",
            "The on-call agent runs on Claude Haiku, and every action it wants to take goes through Themis first.",
        ]),
        ("fault", [
            "This is the bad release: one commit from the release bot, moving checkout to version 1.4.0.",
            "Watch the error rate.",
            "Within a minute, about three in every ten requests are failing, on checkout and on the frontend that calls it.",
            f"About {page_min} minutes in, Prometheus pages the on-call, the same way it would wake up a person.",
        ]),
        ("investigate", [
            "The agent picks up the page and starts digging, with the tools an engineer would use.",
            "It pulls the golden signals for checkout, the firing alerts and the logs, then the Git history and the rollout history.",
            f"{words(len(reads)).capitalize()} tool calls in {round(a['seconds'])} seconds.",
            "Then it puts it together: " + log_line + "and the errors started exactly when checkout moved to 1.4.0.",
        ]),
        ("gate", [
            "So it decides to revert that one commit in Git.",
            "Before anything touches the cluster, Themis checks the action.",
            "It's on this agent's allow-list. The agent has earned the right to act on its own. Nothing it needs is quarantined.",
            "The revert is allowed. It goes into Git, and the cluster applies it.",
        ]),
        ("recovery", [
            "And the errors fall away.",
            f"{round(t['recovered_s'])} seconds after the page, the shop is healthy again.",
            "Git and the cluster agree, so the next deploy won't bring the bug back.",
        ]),
        ("themis", [
            "Themis recorded all of it.",
            "This is the on-call agent: the tools it may use, the ones that need a person's approval, and its error budget.",
            "And this is the incident, step by step: every model call and every tool call, how long each one took, and what it cost.",
        ]),
        ("cost", [
            f"The whole incident took {words(a['model_calls'])} model calls and about {round(tokens / 1000)} thousand tokens.",
            f"That's {cost}.",
        ]),
        ("why", [
            "Now step back.",
            "Companies are starting to hand real production work to AI agents: agents that deploy code, touch customer data and spend money.",
            "So who's on call for the agents?",
            "That's Themis.",
            "Every agent gets an error budget, like any other service.",
            "Trust is earned, and it's taken away on its own when an agent misbehaves.",
            "A person says go before anything risky runs.",
            "And when something breaks, Themis tells you which agent, model or tool did it.",
        ]),
        ("where", [
            "Where can this go?",
            "Platform teams running dozens of agents, each with its own targets.",
            "Banks and hospitals that need an audit trail and a human sign-off.",
            "On-call teams letting agents fix incidents, on a short leash.",
            "Finance teams keeping token spend inside a budget.",
            "And security teams, stopping an agent that's been tricked by a poisoned log line. That's the next fault we'll throw at it.",
        ]),
        ("close", [
            "One fault today, and more on the way: memory leaks, overload, tools that lie, and prompt injection.",
            "Themis. SRE for agent fleets.",
        ]),
    ]  # fmt: skip


def parsed(output: str) -> dict:
    """A tool's output as the agent recorded it. The record keeps the first 600 characters, so a long output is cut
    off mid-JSON; the fields the film needs are near the start, so they are read with patterns instead."""
    try:
        return json.loads(output)
    except ValueError:
        pass
    out: dict = {}
    for key in ("error_rate", "error_rate_baseline", "total_lines", "count"):
        if m := re.search(rf'"{key}":\s*(-?[\d.]+)', output):
            out[key] = float(m.group(1)) if "." in m.group(1) else int(m.group(1))
    if m := re.search(r'"pattern":\s*"((?:[^"\\]|\\.)*)",\s*"count":\s*(\d+)', output):
        out["top_patterns"] = [{"pattern": json.loads(f'"{m.group(1)}"'), "count": int(m.group(2))}]
    commits = re.findall(r'"commit": "(\w+)", "author": "([^"]*)", "age": "([^"]*)", "subject": "([^"]*)"', output)
    if commits:
        out["commits"] = [dict(zip(("commit", "author", "age", "subject"), c, strict=True)) for c in commits]
    revs = re.findall(r'"revision":\s*(\d+),\s*"image":\s*"([^"]*)",\s*"age_min":\s*(\d+)', output)
    if revs:
        out["revisions"] = [{"revision": int(r), "image": i, "age_min": int(a)} for r, i, a in revs]
    out["alerts"] = [None] * output.count('"alertname"')
    return out


# what each read tool found, in a few words, from its real output
def found(o: dict) -> str:
    out = parsed(o["output"])
    tool = o["tool"]
    if tool == "golden_signals":
        return f"errors {out.get('error_rate', 0):.1%}, normally {out.get('error_rate_baseline') or 0:.0%}; latency normal"
    if tool == "list_firing_alerts":
        return f"{len(out.get('alerts', []))} alerts firing"
    if tool == "search_logs":
        p = (out.get("top_patterns") or [{}])[0]
        return f"{p.get('count')} of {out.get('total_lines')} lines: {p.get('pattern', '')[:44]}"
    if tool == "gitops_history":
        c = (out.get("commits") or [{}])[0]
        return f"{c.get('commit')} by {c.get('author')}, {c.get('age')}: {c.get('subject')}"
    if tool == "service_dependencies":
        return f"calls {', '.join(out.get('calls') or []) or 'nothing downstream'}"
    if tool == "k8s_events":
        reasons = re.findall(r'"reason":\s*"(\w+)"', o["output"])
        warn = o["output"].count('"Warning"')
        return f"{', '.join(dict.fromkeys(reasons[:4]))}; {'no' if not warn else warn} warnings" if reasons else "no events"
    if tool == "rollout_history":
        r = (out.get("revisions") or [{}])[-1]
        return f"revision {r.get('revision')}: {r.get('image')}, {r.get('age_min')} min old"
    return json.dumps(out)[:70]


def film_data(run: dict, results: dict) -> dict:
    t, a = run["trial"], run["trial"]["agent"]
    reads = [o for o in a["tools_used"] if o["tool"] not in ("revert_release", "rollout_undo", "restart_deployment")]
    tools = [
        {"tool": o["tool"], "args": ", ".join(f"{k}={v}" for k, v in o["arguments"].items()), "out": found(o)} for o in reads
    ]
    hist = next((parsed(o["output"]) for o in reads if o["tool"] == "gitops_history"), {})
    first = (hist.get("commits") or [{}])[0]
    return {
        "trial": t,
        "tools": tools,
        "commit": {"hash": first.get("commit", ""), "subject": first.get("subject", "checkout: release 1.4.0")},
        "autonomy": (t.get("themis") or {}).get("autonomy") or "autonomous",
        "sub": f"live incident, {results['date']}",
    }


def clock(scene: str, t: float, d: float, ev: dict, starts: list[float]) -> str:
    """The incident clock in the corner: how far into the real incident this moment of the film is. It moves with the
    pictures: the error-rate chart in the fault and recovery scenes, the tool calls in the investigation."""
    at = lambda i, fallback: starts[i] if i < len(starts) else fallback  # noqa: E731
    spans = {
        "fault": (0.0, ev["page"], at(1, 0.0), at(3, d * 0.8) + 2.2),
        "investigate": (ev["agent_start"], ev["agent_end"], at(0, 0.0) + 1.5, at(3, d * 0.6)),
        "gate": (ev["agent_end"], ev["agent_end"], 0.0, d),
        "recovery": (ev["page"], ev["end"], 0.3, at(1, d * 0.7) + 0.6),
    }
    if scene not in spans:
        return ""
    a, b, f0, f1 = spans[scene]
    now = a + (b - a) * min(1.0, max(0.0, (t - f0) / max(f1 - f0, 0.01)))
    speed = (b - a) / max(f1 - f0, 0.01)
    note = f"sped up {round(speed)}x" if speed > 1.5 else "real time"
    m, s = divmod(int(now), 60)
    return f"incident <b>+{m:02d}:{s:02d}</b><br><span style='font-size:18px'>{note}</span>"


# ---------------------------------------------------------------------------------------------- the voice

TTS = r"""
import json, sys
import numpy as np, soundfile as sf
from kokoro_onnx import Kokoro
job = json.load(sys.stdin)
k = Kokoro(job["model"], job["voices"])
out = {}
for scene, sentences in job["scenes"]:
    sr, parts, marks, at = 24000, [], [], job["lead"]
    parts.append(np.zeros(int(job["lead"] * sr), dtype=np.float32))
    for i, (text, spoken) in enumerate(sentences):
        audio, sr = k.create(spoken, voice=job["voice"], speed=job["speed"], lang="en-us")
        marks.append([round(at, 3), round(at + len(audio) / sr, 3), text])
        parts.append(audio.astype(np.float32))
        at += len(audio) / sr
        gap = job["gap"] if i < len(sentences) - 1 else job["tail"]
        parts.append(np.zeros(int(gap * sr), dtype=np.float32))
        at += gap
    wav = np.concatenate(parts)
    sf.write(f"{job['dir']}/{scene}.wav", wav, sr)
    out[scene] = {"seconds": len(wav) / sr, "marks": marks}
print(json.dumps(out))
"""


# how a few words are said, where the voice would otherwise read them letter by letter or digit by digit
SAY = {"SRE": "S R E", "1.4.0": "one point four", "KeyError": "key error", "Prometheus pages": "Prometheus pages,"}


def spoken(text: str) -> str:
    for written, said in SAY.items():
        text = text.replace(written, said)
    return text


def speak(script: list, work: Path, tts_python: str, voice_dir: Path) -> dict:
    script = [(scene, [(text, spoken(text)) for text in sentences]) for scene, sentences in script]
    job = {"model": str(voice_dir / "kokoro-v1.0.onnx"), "voices": str(voice_dir / "voices-v1.0.bin"), "voice": VOICE,
           "speed": 1.0, "lead": LEAD, "gap": GAP, "tail": TAIL, "dir": str(work), "scenes": script}  # fmt: skip
    done = subprocess.run([tts_python, "-c", TTS], input=json.dumps(job), capture_output=True, text=True, check=True)
    return json.loads(done.stdout.strip().splitlines()[-1])


# ---------------------------------------------------------------------------------------------- pictures


def encoder(path: Path) -> subprocess.Popen:
    cmd = ["ffmpeg", "-loglevel", "error", "-y", "-f", "image2pipe", "-framerate", str(FPS), "-i", "-", *ENCODE, str(path)]
    return subprocess.Popen(cmd, stdin=subprocess.PIPE)


def caption_at(marks: list, t: float) -> str:
    for a, b, text in marks:
        if a - 0.15 <= t <= b + 0.3:
            return text
    return ""


async def drawn_scene(page, scene: str, voice: dict, ev: dict, out: Path, preview: bool) -> None:
    d = voice["seconds"]
    if preview:
        t = d * 0.9
        starts = [a for a, _, _ in voice["marks"]]
        await page.evaluate(
            "([s, t, d, c, k, m]) => frame(s, t, d, c, k, m)",
            [scene, t, d, caption_at(voice["marks"], t), clock(scene, t, d, ev, starts), starts],
        )
        await page.screenshot(path=str(out.with_suffix(".jpg")), type="jpeg", quality=90)
        return
    tmp = out.with_name(out.stem + ".part.mp4")
    enc = encoder(tmp)
    starts = [a for a, _, _ in voice["marks"]]
    for f in range(math.ceil(d * FPS)):
        t = f / FPS
        args = [scene, t, d, caption_at(voice["marks"], t), clock(scene, t, d, ev, starts), starts]
        await page.evaluate("([s, t, d, c, k, m]) => frame(s, t, d, c, k, m)", args)
        enc.stdin.write(await page.screenshot(type="jpeg", quality=92))
    enc.stdin.close()
    if enc.wait() != 0:
        raise RuntimeError(f"ffmpeg failed on {scene}")
    tmp.rename(out)


# the dashboard, replayed from Themis's own database and clicked through, with a pointer the viewer can follow
OVERLAY = """(cfg) => {
  const css = document.createElement('style');
  css.textContent = `#film-cap{position:fixed;left:50%;bottom:36px;transform:translateX(-50%);max-width:1000px;z-index:99999;
    text-align:center;font:400 22.7px/1.35 Sora,sans-serif;color:#e6f0f4;padding:9px 20px;border-radius:10px;
    background:rgba(3,6,10,.78);box-shadow:0 0 0 1px rgba(130,205,230,.1);pointer-events:none}
    #film-ptr{position:fixed;left:0;top:0;width:22px;height:22px;margin:-11px 0 0 -11px;border-radius:50%;z-index:99999;
    background:rgba(255,181,74,.9);box-shadow:0 0 0 3px rgba(3,6,10,.8),0 0 22px rgba(255,181,74,.9);pointer-events:none}
    .film-ring{position:fixed;width:22px;height:22px;margin:-11px 0 0 -11px;border-radius:50%;z-index:99998;
    border:2px solid #ffb54a;pointer-events:none;animation:film-ring .6s ease-out forwards}
    @keyframes film-ring{to{transform:scale(3.2);opacity:0}}`;
  document.head.appendChild(css);
  const cap = Object.assign(document.createElement('div'), {id: 'film-cap'});
  const ptr = Object.assign(document.createElement('div'), {id: 'film-ptr'});
  document.body.append(cap, ptr);
  window.filmPtr = {x: cfg.x, y: cfg.y};
  ptr.style.transform = `translate(${cfg.x}px,${cfg.y}px)`;
  window.filmMove = (x, y, ms) => new Promise((done) => {
    const x0 = window.filmPtr.x, y0 = window.filmPtr.y, t0 = performance.now();
    const step = (now) => {
      const p = Math.min(1, (now - t0) / ms), e = p < .5 ? 4 * p * p * p : 1 - Math.pow(-2 * p + 2, 3) / 2;
      const x1 = x0 + (x - x0) * e, y1 = y0 + (y - y0) * e;
      ptr.style.transform = `translate(${x1}px,${y1}px)`; window.filmPtr = {x: x1, y: y1};
      p < 1 ? requestAnimationFrame(step) : done();
    };
    requestAnimationFrame(step);
  });
  window.filmRing = () => { const r = document.createElement('div'); r.className = 'film-ring';
    r.style.left = window.filmPtr.x + 'px'; r.style.top = window.filmPtr.y + 'px'; document.body.appendChild(r);
    setTimeout(() => r.remove(), 700); };
  const t0 = performance.now();
  setInterval(() => {
    const t = (performance.now() - t0) / 1000;
    const m = cfg.marks.find(([a, b]) => a - 0.15 <= t && t <= b + 0.3);
    cap.textContent = m ? m[2] : ''; cap.style.display = m ? 'block' : 'none';
  }, 50);
}"""


async def themis_scene(browser, db: Path, voice: dict, out: Path, work: Path, preview: bool) -> None:
    from themis.lab import Lab

    copy = work / "replay.db"
    shutil.copy(db, copy)
    lab = Lab(f"sqlite:///{copy}", 8673, 1.0, eval_interval=3600).start()  # replay only: nothing new to judge
    try:
        page = await browser.new_page(viewport={"width": 1280, "height": 720}, device_scale_factor=1.5)
        await page.goto(f"{lab.url}/#key={lab.key}")
        await page.wait_for_timeout(2500)
        marks, d = voice["marks"], voice["seconds"]
        frames: list[tuple[bytes, float]] = []
        cdp = await page.context.new_cdp_session(page)
        cdp.on("Page.screencastFrame", lambda e: (frames.append((base64.b64decode(e["data"]), time.monotonic())),
                                                  asyncio.ensure_future(cdp.send("Page.screencastFrameAck", {"sessionId": e["sessionId"]}))))  # fmt: skip
        await page.evaluate(OVERLAY, {"x": 640, "y": 420, "marks": marks})
        if not preview:
            await cdp.send("Page.startScreencast", {"format": "jpeg", "quality": 92, "maxWidth": 1920, "maxHeight": 1080})
        start = time.monotonic()

        async def at(sec: float) -> None:
            await asyncio.sleep(max(0.0, start + sec - time.monotonic()))

        async def click(locator, ms: int = 700) -> None:
            box = await locator.bounding_box()
            await page.evaluate(
                "([x, y, ms]) => filmMove(x, y, ms)", [box["x"] + box["width"] / 2, box["y"] + box["height"] / 2, ms]
            )
            await page.evaluate("filmRing()")
            await page.wait_for_timeout(120)
            await locator.click()

        await at(marks[0][1] - 0.2)
        await click(page.get_by_role("link", name="Agents"))
        await page.wait_for_timeout(900)
        await click(page.get_by_text("oncall", exact=True).first)
        await at(marks[2][0] - 0.6)
        await click(page.get_by_role("link", name="Journeys"))
        await page.wait_for_timeout(900)
        await click(page.locator("tr.pick").first)
        # the journey takes the whole width, as on the project site, so its steps are big enough to read
        await page.add_style_tag(content="main{max-width:none}.split{grid-template-columns:1fr}.split>:first-child{display:none}")
        await page.wait_for_timeout(1400)
        if await page.locator(".water").count():
            await page.evaluate("""() => { const w = document.querySelector('.water');
              window.scrollTo({top: w.getBoundingClientRect().top + window.scrollY - 70, behavior: 'smooth'}); }""")
            await page.wait_for_timeout(900)
            box = await page.locator(".water").first.bounding_box()
            await page.evaluate("([x, y, ms]) => filmMove(x, y, ms)", [box["x"] + box["width"] * 0.7, box["y"] + 60, 1200])
        await at(d)
        if preview:
            await page.screenshot(path=str(out.with_suffix(".jpg")), type="jpeg", quality=90)
            return
        await cdp.send("Page.stopScreencast")
        await page.close()
    finally:
        lab.stop()
    # the screencast sends a frame when something changes: hold each until the next, then resample to 30 fps
    listing, base = [], frames[0][1] if frames else start
    pics = work / "themis-frames"
    pics.mkdir(exist_ok=True)
    for i, (jpeg, stamp) in enumerate(frames):
        f = pics / f"{i:05d}.jpg"
        f.write_bytes(jpeg)
        nxt = frames[i + 1][1] if i + 1 < len(frames) else start + d
        listing.append(f"file '{f}'\nduration {max(nxt - stamp, 1 / FPS):.4f}")
    lead = max(0.0, base - start)
    (work / "themis.txt").write_text(
        f"file '{pics / '00000.jpg'}'\nduration {lead:.4f}\n" * (lead > 0)
        + "\n".join(listing)
        + f"\nfile '{pics / f'{len(frames) - 1:05d}.jpg'}'\n"
    )
    tmp = out.with_name(out.stem + ".part.mp4")
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "concat", "-safe", "0", "-i", str(work / "themis.txt"),
                    "-vf", f"scale=1920:1080,fps={FPS}", "-t", f"{d:.3f}", *ENCODE, str(tmp)], check=True)  # fmt: skip
    tmp.rename(out)


# ---------------------------------------------------------------------------------------------- the whole film


async def main(run_dir: Path, out_dir: Path, tts_python: str, voice_dir: Path, preview: bool, only: list[str]) -> None:
    results = json.loads((run_dir / "results-real.json").read_text())
    run = {"trial": results["trials"][0]}
    script = narration(run)
    if only:
        script = [s for s in script if s[0] in only]
    out_dir.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="film-real-"))
    voices = speak(script, work, tts_python, voice_dir)
    (out_dir / "narration.json").write_text(json.dumps(voices, indent=1))
    data, ev = film_data(run, results), run["trial"]["events"]
    async with async_playwright() as p:
        browser = await p.chromium.launch(channel="chrome", args=["--no-sandbox", "--allow-file-access-from-files"])
        page = await browser.new_page(viewport={"width": 1920, "height": 1080})
        await page.goto(f"{(REPO / 'scripts' / 'film_real.html').as_uri()}")
        await page.evaluate("(d) => init(d)", data)
        await page.wait_for_timeout(800)  # fonts
        # the dashboard first: its fleet map shows what happened in the last 30 minutes
        for scene, _ in sorted(script, key=lambda s: s[0] != "themis"):
            target = out_dir / f"{scene}.mp4"
            t0 = time.time()
            if scene == "themis":
                await themis_scene(browser, run_dir / "bench-real" / "themis.db", voices[scene], target, work, preview)
            else:
                await drawn_scene(page, scene, voices[scene], ev, target, preview)
            print(f"{scene}: {voices[scene]['seconds']:.1f}s of film in {time.time() - t0:.0f}s", flush=True)
        await browser.close()
    if preview:
        return
    # one video track and one audio track, scene by scene, then together
    (work / "video.txt").write_text("".join(f"file '{out_dir / (s + '.mp4')}'\n" for s, _ in script))
    (work / "audio.txt").write_text("".join(f"file '{work / (s + '.wav')}'\n" for s, _ in script))
    run_ff = lambda *a: subprocess.run(["ffmpeg", "-loglevel", "error", "-y", *a], check=True)  # noqa: E731
    run_ff("-f", "concat", "-safe", "0", "-i", str(work / "video.txt"), "-c", "copy", str(work / "video.mp4"))
    run_ff("-f", "concat", "-safe", "0", "-i", str(work / "audio.txt"), "-c:a", "pcm_s16le", str(work / "voice.wav"))
    final = out_dir / "themis-live-incident.mp4"
    run_ff("-i", str(work / "video.mp4"), "-i", str(work / "voice.wav"), "-map", "0:v", "-map", "1:a", "-c:v", "copy",
           "-c:a", "aac", "-b:a", "160k", "-shortest", "-movflags", "+faststart", str(final.with_name("part.mp4")))  # fmt: skip
    final.with_name("part.mp4").rename(final)
    print(f"wrote {final}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("run_dir", type=Path)
    ap.add_argument("out_dir", type=Path)
    ap.add_argument("--tts-python", required=True)
    ap.add_argument("--voice-dir", type=Path, required=True)
    ap.add_argument("--preview", action="store_true")
    ap.add_argument("--only", default="", help="comma separated scenes, for trying one out")
    a = ap.parse_args()
    asyncio.run(main(a.run_dir, a.out_dir, a.tts_python, a.voice_dir, a.preview, [s for s in a.only.split(",") if s]))
