"""The dashboard and the product page against the 20 signs of an AI-built page (zerowing.in/premium).

Each test is one sign that a machine can check. The signs that need an eye (everything in a card, three icon
cards in a row, pills above the headline) were checked by looking at screenshots; they are listed at the end so
nobody thinks they were forgotten.
"""

from __future__ import annotations

import importlib.util
import re
from html.parser import HTMLParser
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
WEB = REPO / "themis" / "web"
SITE = REPO / "site"

DASHBOARD = [WEB / "index.html", WEB / "app.js", WEB / "styles.css"]
PAGE = [SITE / "page.html"]
CSS = {"dashboard": (WEB / "styles.css").read_text(), "product page": (SITE / "page.html").read_text()}
COPY = DASHBOARD + PAGE + [REPO / "README.md"]


def _site_build():
    spec = importlib.util.spec_from_file_location("site_build", SITE / "build.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _Text(HTMLParser):
    """Visible text of a page, one string per block, script and style left out."""

    BLOCKS = {"p", "h1", "h2", "h3", "h4", "li", "dt", "dd", "td", "th", "figcaption", "button", "a", "blockquote"}

    def __init__(self) -> None:
        super().__init__()
        self.blocks: list[str] = []
        self._skip = 0
        self._buf: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "svg"):
            self._skip += 1
        elif tag in self.BLOCKS:
            self._flush()

    def handle_endtag(self, tag):
        if tag in ("script", "style", "svg"):
            self._skip -= 1
        elif tag in self.BLOCKS:
            self._flush()

    def handle_data(self, data):
        if not self._skip:
            self._buf.append(data)

    def _flush(self):
        text = " ".join("".join(self._buf).split())
        if text:
            self.blocks.append(text)
        self._buf = []


def _visible_text(html: str) -> list[str]:
    p = _Text()
    p.feed(html)
    p._flush()
    return p.blocks


def _js_strings(js: str) -> list[str]:
    """Literal text in the dashboard script: quoted strings and the text between tags in templates."""
    out = re.findall(r'"([^"\\\n]*)"|\'([^\'\\\n]*)\'', js)
    strings = [a or b for a, b in out]
    for tpl in re.findall(r"`([^`]*)`", js):
        strings += [t for t in re.split(r"<[^>]+>|\$\{[^}]*\}", tpl) if t.strip()]
    return strings


@pytest.fixture(scope="module")
def built_page() -> str:
    return _site_build().render()


# 1 and 2: arrows inside buttons and links, and dots between words
@pytest.mark.parametrize("path", DASHBOARD + PAGE, ids=lambda p: p.name)
def test_no_arrows_or_interpuncts_in_the_interface(path):
    text = path.read_text()
    for mark in ("→", "↗", "›", "»", "·", "&middot;", "&rarr;"):
        assert mark not in text, f"{path.name} uses {mark!r}"


def test_readme_links_have_no_arrows():
    links = re.findall(r"\[([^\]]+)\]\(", (REPO / "README.md").read_text())
    assert not [t for t in links if re.search(r"[→↗›»]", t)]


# 3: small all-caps eyebrows
@pytest.mark.parametrize("where", CSS)
def test_no_uppercase_labels(where):
    assert not re.search(r"text-transform:\s*uppercase", CSS[where])
    assert not re.search(r"font-variant(-caps)?:\s*(all-)?small-caps", CSS[where])


# 4: cream backgrounds
def _colours(value: str):
    for h in re.findall(r"#([0-9a-fA-F]{6}|[0-9a-fA-F]{3})\b", value):
        h = "".join(c * 2 for c in h) if len(h) == 3 else h
        yield tuple(int(h[i : i + 2], 16) for i in (0, 2, 4)), 1.0
    for r, g, b, a in re.findall(r"rgba?\((\d+),\s*(\d+),\s*(\d+)(?:,\s*([\d.]+))?\)", value):
        yield (int(r), int(g), int(b)), float(a or 1)


@pytest.mark.parametrize("where", CSS)
def test_no_cream_or_beige_backgrounds(where):
    for value in re.findall(r"background(?:-color)?:\s*([^;]+);", CSS[where]):
        for (r, g, b), alpha in _colours(value):
            warm_light = min(r, g, b) > 200 and r - b > 12
            assert not (warm_light and alpha > 0.5), f"{where} has a cream background: {value.strip()}"


# 5: one italic word in a headline
@pytest.mark.parametrize("where", CSS)
def test_no_italics(where):
    assert not re.search(r"font-style:\s*italic", CSS[where])


def test_no_emphasis_inside_headings(built_page):
    sources = built_page + (WEB / "app.js").read_text() + (WEB / "index.html").read_text()
    assert not re.search(r"<h[1-4][^>]*>[^<]*(<[^/h][^>]*>[^<]*)*<(em|i|strong|b)\b", sources)


# 6: sparkles
@pytest.mark.parametrize("path", COPY, ids=lambda p: p.name)
def test_no_sparkle_icons(path):
    assert not re.search("[✨✦✧❇\U0001f31f⭐]", path.read_text())


# 8 and 16: words doing no work
FILLER = ("quiet", "quietly", "calm", "calmly", "effortless", "effortlessly", "seamless", "seamlessly", "real", "really")


@pytest.mark.parametrize("path", COPY, ids=lambda p: p.name)
def test_no_filler_words_in_copy(path):
    text = path.read_text()
    if path.suffix == ".js":
        text = " ".join(_js_strings(text))
    elif path.suffix == ".css":
        text = ""  # selectors and properties are not copy
    found = sorted({w.lower() for w in re.findall(r"[A-Za-z]+", text) if w.lower() in FILLER})
    assert not found, f"{path.name} says {found}"


# 9: numbered sections
def test_no_numbered_sections(built_page):
    for source in (built_page, (WEB / "app.js").read_text(), (WEB / "index.html").read_text()):
        assert not re.search(r">\s*0[1-9]\s*[<./]", source)


# 10 and 19: motion. The dashboard has none; the product page has one moment, on the product only
def test_the_dashboard_does_not_animate():
    assert "@keyframes" not in CSS["dashboard"]
    assert not re.search(r"animation:\s*(?!\s|none)", CSS["dashboard"])


def test_the_product_page_moves_only_the_product():
    css = CSS["product page"]
    assert set(re.findall(r"@keyframes\s+([\w-]+)", css)) == {"float", "shadow", "sweep"}
    animated = re.findall(r"([^{}]+)\{[^{}]*animation:\s*(?!\s|none)", css)
    assert sorted(s.strip() for s in animated) == [".float", ".shadow", ".sweep"]
    assert "prefers-reduced-motion" in css
    # scrolling changes one thing: which part of the pinned fleet map is lit. No section fades or slides in
    transitions = re.findall(r"([^{}]+)\{[^{}]*transition:", css)
    assert all(sel.strip().startswith(".pin") for sel in transitions), transitions
    script = re.search(r"<script>(.*?)</script>", css, re.S).group(1)
    assert "classList" not in script and ".style." not in script
    assert re.findall(r"(\w+)\.dataset\.\w+\s*=", script) == ["pin"]


# 11: blurred glows. The one gradient allowed is the contact shadow under the product
@pytest.mark.parametrize("where", CSS)
def test_no_glow_blobs(where):
    css = CSS[where]
    assert not re.search(r"(?<!backdrop-)filter:\s*blur", css)
    rules = re.findall(r"([^{}]+)\{([^{}]*radial-gradient[^{}]*)\}", css)
    assert [sel.strip() for sel, _ in rules] in ([], [".shadow"])
    for _, body in rules:
        for (r, g, b), _ in _colours(body):
            assert max(r, g, b) < 40, "the shadow is a shadow, not a coloured light"


# 12: one type family
@pytest.mark.parametrize("where", CSS)
def test_one_type_family(where):
    css = CSS[where]
    assert "Instrument Serif" not in css
    faces = set(re.findall(r'@font-face\s*\{[^}]*font-family:\s*"([^"]+)"', css))
    assert faces <= {"IBM Plex Sans", "IBM Plex Mono"}
    stacks = re.findall(r"--(?:sans|mono):\s*([^;]+);", css)
    assert stacks and all("IBM Plex" in s for s in stacks)
    for value in re.findall(r"(?<!@font-face \{ )font-family:\s*([^;]+);", css):
        assert "IBM Plex" in value or value.startswith("var(--"), value


# 13: coloured bars on the side of quotes and callouts
@pytest.mark.parametrize("where", CSS)
def test_no_thick_side_borders(where):
    for side in re.findall(r"border-(?:left|right|inline-start|inline-end)(?:-width)?:\s*([^;]+);", CSS[where]):
        width = re.match(r"\s*([\d.]+)px", side)
        assert side.strip() in ("0", "none") or (width and float(width.group(1)) <= 1), side


# 17: every number on the product page comes from a measurement, and the page is not stale
def test_the_committed_page_is_built_from_the_committed_measurements(built_page):
    assert (SITE / "index.html").read_text() == built_page, "run python site/build.py"


def test_every_footnote_mark_has_a_footnote(built_page):
    marks = set(re.findall(r"<sup>(\d+)</sup>", built_page))
    notes = re.search(r'<ol class="notes">(.*?)</ol>', built_page, re.S).group(1)
    for m in marks:
        assert f"<sup>{m}</sup>" in notes
        body = re.search(rf"<sup>{m}</sup>(.*?)</li>", notes, re.S).group(1)
        assert len(body.split()) > 12, f"footnote {m} must say how the number was measured"


def test_every_figure_in_the_hero_carries_a_footnote(built_page):
    hero = re.search(r'<header class="hero".*?</header>', built_page, re.S).group(0)
    for dl in re.findall(r'<dl class="corner[^"]*">.*?</dl>', hero, re.S):
        if re.search(r"\d", dl):
            assert "<sup>" in dl, dl


# 18: strings of two-word sentences
def _short_sentence_runs(blocks):
    for block in blocks:
        sentences = [s for s in re.split(r"(?<=[.!?])\s+", block) if s]
        short = [len(s.split()) <= 2 and s.endswith((".", "!")) for s in sentences]
        if len(sentences) > 1 and any(a and b for a, b in zip(short, short[1:], strict=False)):
            yield block


def test_no_two_word_sentence_runs(built_page):
    blocks = _visible_text(built_page) + _visible_text((WEB / "index.html").read_text())
    blocks += _js_strings((WEB / "app.js").read_text())
    assert not list(_short_sentence_runs(blocks))


# 20: dithered images
@pytest.mark.parametrize("where", CSS)
def test_no_dithering(where):
    assert not re.search(r"image-rendering:\s*(pixelated|crisp-edges)", CSS[where])
    assert "halftone" not in CSS[where].lower()


# the premium hero: the name edge to edge, the product in front of it, controls in one dock
def test_hero_layers_the_product_over_the_name(built_page):
    css = CSS["product page"]
    title = re.search(r"\.wordmark\s*\{([^}]*)\}", css).group(1)
    product = re.search(r"\.object\s*\{([^}]*)\}", css).group(1)
    assert re.search(r"z-index:\s*1\b", title) and re.search(r"z-index:\s*2\b", product)
    hero = re.search(r'<header class="hero".*?</header>', built_page, re.S).group(0)
    dock = re.search(r'<div class="dock".*?</div>', hero, re.S).group(0)
    outside = hero.replace(dock, "")
    assert "<button" not in outside and "<a " not in outside, "every control in the hero lives in the dock"
    assert dock.count("<button") == 1, "one button"


# Checked by eye on screenshots, not here: 7 everything in a card, 14 three icon cards in a row,
# 15 pills above the headline.
