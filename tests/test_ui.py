"""The dashboard in a real browser. Skipped when Chrome or playwright is not installed (`pip install -e ".[ui-test]"`)."""

import httpx
import pytest

from tests.conftest import free_port, journey, wait_for
from themis.lab import Lab

pw = pytest.importorskip("playwright.sync_api")
pytestmark = pytest.mark.ui

# Agent names arrive in telemetry, so anyone who can send a span chooses them. This one tries to run script.
HOSTILE = '<img src=x onerror="window.pwned=1">'


@pytest.fixture(scope="module")
def browser():
    with pw.sync_playwright() as p:
        try:
            chrome = p.chromium.launch(channel="chrome", args=["--no-sandbox"])
        except pw.Error as e:
            pytest.skip(f"Chrome is not available: {e}")
        yield chrome
        chrome.close()


@pytest.fixture
def lab(tmp_path):
    lab = Lab(f"sqlite:///{tmp_path}/ui.db", free_port(), 120.0, eval_interval=0.3, settle_seconds=0.2).start()
    auth = {"Authorization": f"Bearer {lab.key}"}
    fleet = {"agents": {"deployer": {"model": "m1", "tools": ["deploy.apply"], "high_risk": ["deploy.apply"]}}}
    httpx.put(f"{lab.url}/api/fleet", json=fleet, headers=auth).raise_for_status()
    for agent in ("deployer", HOSTILE):
        httpx.post(f"{lab.url}/v1/traces", content=journey(agent, error=agent == HOSTILE)[1], headers=auth).raise_for_status()
    wait_for(lambda: len(httpx.get(f"{lab.url}/api/overview", headers=auth).json()["agents"]) == 2, 10, "both agents to appear")
    lab.auth = auth
    yield lab
    lab.stop()


def test_dashboard_views_work_and_telemetry_is_never_run_as_code(browser, lab):
    page = browser.new_page(viewport={"width": 1280, "height": 900})
    problems = []
    page.on("pageerror", lambda e: problems.append(str(e)))
    page.on("console", lambda m: problems.append(m.text) if m.type == "error" else None)

    page.goto(f"{lab.url}/#key={lab.key}")
    page.wait_for_selector("svg .body")
    assert "key=" not in page.url  # the key is taken out of the address bar as soon as it is read
    assert page.locator("svg .body").count() == 2  # both agents are on the fleet map
    assert page.get_by_text(HOSTILE).count() >= 1  # the hostile name is shown as plain text...
    assert page.evaluate("window.pwned") is None  # ...and was not run
    assert page.locator("img[src='x']").count() == 0

    page.goto(f"{lab.url}/#/journeys")
    page.locator("tr.pick").first.click()
    page.wait_for_selector(".water")
    assert page.get_by_text("What it used").is_visible() and page.locator(".used > div").count() >= 1
    assert page.evaluate("window.pwned") is None

    # an operator suspends an agent from its page, with a reason
    page.goto(f"{lab.url}/#/agents/deployer")
    page.get_by_label("Reason").fill("testing the kill switch")
    page.get_by_role("button", name="Suspend").click()
    page.get_by_text("Set to suspended.").wait_for()
    detail = httpx.get(f"{lab.url}/api/agents/deployer", headers=lab.auth).json()
    assert detail["agent"]["autonomy"] == "suspended" and detail["events"][0]["reason"] == "testing the kill switch"

    # a supervised agent asks for go / no-go; the operator answers from the Incidents page
    asked = httpx.post(
        f"{lab.url}/v1/approvals", json={"agent": "deployer", "action": "tool:deploy.apply"}, headers=lab.auth
    ).json()
    page.goto(f"{lab.url}/#/incidents")
    page.get_by_role("button", name="No-go").click()
    wait_for(
        lambda: httpx.get(f"{lab.url}/v1/approvals/{asked['id']}", headers=lab.auth).json()["status"] == "denied", 5, "the no-go"
    )
    assert page.get_by_text("deployer is now suspended").count() >= 1  # the incident the suspension opened

    # the chaos lab starts an experiment
    page.goto(f"{lab.url}/#/chaos")
    page.get_by_label("Fault").select_option("rogue_agent")
    page.get_by_label("Target").fill("agent:deployer")
    page.get_by_role("button", name="Start experiment").click()
    page.get_by_text("rogue_agent on agent:deployer").wait_for()

    page.goto(f"{lab.url}/#/agents")
    page.wait_for_selector(".cards a")
    assert problems == []
    page.close()


def test_without_a_key_the_dashboard_asks_for_one_and_shows_nothing(browser, lab):
    page = browser.new_page()
    page.goto(lab.url)
    page.get_by_role("heading", name="Open the dashboard").wait_for()
    assert page.locator("svg").count() == 0
    page.get_by_label("API key").fill("thm_000000000000_notakey")
    page.get_by_role("button", name="Open dashboard").click()
    page.get_by_text("That key was not accepted").wait_for()
    page.close()
