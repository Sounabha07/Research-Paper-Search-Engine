"""Scripted demo recording (Playwright, 1920x1080). Not part of the app.

  demo/.venv/Scripts/python demo/record.py          (app running on localhost:3000 / :8000)

Writes raw/<name>.webm and raw/marks.json (the spans spent waiting, cut by make_video.py). The questions are the ones
run beforehand, so their LLM responses come from the response cache and the answers are the reviewed ones.
"""

from __future__ import annotations

import json
import os
import time
import urllib.request
from pathlib import Path

from playwright.sync_api import Locator, Page, sync_playwright

APP = "http://127.0.0.1:3000"
API = "http://127.0.0.1:8000"
HERE = Path(__file__).parent
RAW = HERE / "raw"
HOLD = 1.5  # caption reading time multiplier (pacing of the final video)

Q_QUICK = (
    "How does speculative decoding speed up large language model inference without changing the output distribution?"
)
Q_DEEP = "How do selective state space models like Mamba compare to Transformers on long sequences?"
Q_MATH = "How was the sensitivity conjecture proved, and what does it say about Boolean functions?"
FETCH_PAPER = os.environ.get("DEMO_FETCH_PAPER", "2609.09338")  # abstract-only until the demo fetches it

# Fake cursor, click ripple and caption banner, injected into every page.
OVERLAY = r"""
(() => {
  if (window.__demoOverlay) return;
  window.__demoOverlay = true;
  const css = `
    #demo-cursor { position: fixed; z-index: 2147483647; width: 22px; height: 22px; margin: -3px 0 0 -3px;
      pointer-events: none; transition: transform .08s; }
    #demo-cursor svg { filter: drop-shadow(0 1px 2px rgba(0,0,0,.45)); }
    .demo-ripple { position: fixed; z-index: 2147483646; width: 44px; height: 44px; margin: -22px 0 0 -22px;
      border-radius: 50%; border: 3px solid rgba(234,179,8,.95); background: rgba(234,179,8,.25);
      pointer-events: none; animation: demo-ripple .55s ease-out forwards; }
    @keyframes demo-ripple { from { transform: scale(.3); opacity: 1 } to { transform: scale(1.6); opacity: 0 } }
    #demo-caption { position: fixed; left: 50%; bottom: 48px; transform: translateX(-50%); z-index: 2147483645;
      max-width: 1500px; padding: 16px 30px; border-radius: 14px; background: rgba(17,24,39,.88); color: #fff;
      font: 600 30px/1.3 ui-sans-serif, system-ui, -apple-system, "Segoe UI", sans-serif; text-align: center;
      box-shadow: 0 10px 30px rgba(0,0,0,.35); pointer-events: none; transition: opacity .25s; }
    #demo-bibtex { position: fixed; right: 60px; top: 120px; z-index: 2147483644; width: 760px; max-height: 640px;
      overflow: hidden; padding: 20px 24px; border-radius: 12px; background: #0b1020; color: #d1fae5;
      font: 15px/1.45 ui-monospace, SFMono-Regular, Consolas, monospace; white-space: pre-wrap;
      box-shadow: 0 20px 50px rgba(0,0,0,.45); pointer-events: none; }`;
  const add = () => {
    const style = document.createElement("style"); style.textContent = css; document.head.appendChild(style);
    const c = document.createElement("div"); c.id = "demo-cursor";
    c.innerHTML = '<svg width="22" height="22" viewBox="0 0 24 24"><path d="M3 2l7.5 19 2.6-7.9L21 10.5z" fill="#fff" stroke="#111" stroke-width="1.6" stroke-linejoin="round"/></svg>';
    c.style.left = (window.__cx || 960) + "px"; c.style.top = (window.__cy || 540) + "px";
    document.body.appendChild(c);
    window.addEventListener("mousemove", (e) => { window.__cx = e.clientX; window.__cy = e.clientY;
      c.style.left = e.clientX + "px"; c.style.top = e.clientY + "px"; }, true);
    window.addEventListener("mousedown", (e) => { c.style.transform = "scale(.85)";
      const r = document.createElement("div"); r.className = "demo-ripple";
      r.style.left = e.clientX + "px"; r.style.top = e.clientY + "px"; document.body.appendChild(r);
      setTimeout(() => r.remove(), 600); }, true);
    window.addEventListener("mouseup", () => { c.style.transform = ""; }, true);
    window.__caption = (text) => {
      let el = document.getElementById("demo-caption");
      if (!text) { if (el) el.remove(); return; }
      if (!el) { el = document.createElement("div"); el.id = "demo-caption"; document.body.appendChild(el); }
      el.textContent = text;
    };
    window.__bibtex = (text) => {
      let el = document.getElementById("demo-bibtex");
      if (!text) { if (el) el.remove(); return; }
      if (!el) { el = document.createElement("div"); el.id = "demo-bibtex"; document.body.appendChild(el); }
      el.textContent = text;
    };
  };
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", add); else add();
})();
"""


class Demo:
    def __init__(self, page: Page) -> None:
        self.page = page
        self.t0 = time.monotonic()
        self.waits: list[list[float]] = []
        self.events: dict[str, float] = {}  # step name -> time in the raw video
        self.pos = (960.0, 540.0)

    def now(self) -> float:
        return time.monotonic() - self.t0

    def mark(self, name: str) -> None:
        self.events[name] = self.now()

    def caption(self, text: str | None, hold: float = 0.0) -> None:
        self.page.evaluate("t => window.__caption && window.__caption(t)", text)
        if hold:
            self.page.wait_for_timeout(int(hold * HOLD * 1000))

    def move_to(self, loc: Locator, steps: int = 28) -> tuple[float, float]:
        loc.scroll_into_view_if_needed()
        box = loc.bounding_box()
        assert box, "element not visible"
        x, y = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
        self.page.mouse.move(x, y, steps=steps)
        self.pos = (x, y)
        return x, y

    def click(self, loc: Locator) -> None:
        self.move_to(loc)
        self.page.wait_for_timeout(250)
        self.page.mouse.down()
        self.page.wait_for_timeout(90)
        self.page.mouse.up()

    def wait(self, fn, timeout: float = 300.0) -> None:
        """Wait for a condition; the span is cut from the final video."""
        start = self.now()
        deadline = time.monotonic() + timeout
        while not fn():
            if time.monotonic() > deadline:
                raise TimeoutError("demo wait timed out")
            self.page.wait_for_timeout(500)
        self.waits.append([start, self.now()])

    def goto(self, url: str) -> None:
        self.page.goto(url, wait_until="networkidle")
        self.page.mouse.move(*self.pos)

    def ask(self, question: str, mode: str) -> None:
        self.goto(APP + "/")
        box = self.page.get_by_placeholder("Ask a research question")
        self.click(box)
        if mode == "deep":
            self.click(self.page.get_by_role("group", name="Mode").get_by_role("button", name="Deep"))
        box.press_sequentially(question, delay=40)
        self.page.wait_for_timeout(400)
        box.press("Enter")
        self.page.wait_for_url("**/threads/**", timeout=30000)
        self.page.mouse.move(*self.pos)

    def answer_done(self) -> bool:
        return self.page.get_by_text("working…").count() == 0 and self.page.get_by_text("Sources").count() > 0


def api_json(path: str) -> dict:
    with urllib.request.urlopen(API + path, timeout=30) as r:
        return json.load(r)


def main() -> None:
    RAW.mkdir(exist_ok=True)
    for old in RAW.glob("*.webm"):
        old.unlink()
    with sync_playwright() as p:
        browser = p.chromium.launch(slow_mo=60)
        context = browser.new_context(
            viewport={"width": 1920, "height": 1080}, device_scale_factor=1, color_scheme="light",
            record_video_dir=str(RAW), record_video_size={"width": 1920, "height": 1080},
        )  # fmt: skip
        context.add_init_script(OVERLAY)
        page = context.new_page()
        d = Demo(page)

        # 1. Quick question: live trace and answer
        d.goto(APP + "/")
        d.caption("Ask a research question about arXiv CS and math papers", hold=2.2)
        d.ask(Q_QUICK, "quick")
        d.caption("A live trace: rewrite, hybrid search, rerank, read, write, verify")
        d.wait(d.answer_done)
        d.mark("first_answer")
        d.caption("Replayed from cache for the video. Live answers take about 20 to 40 s and cost about $0.015.")
        d.page.wait_for_timeout(1800)
        answer = d.page.locator("article").first
        for _ in range(4):  # read through the answer
            d.page.mouse.wheel(0, 220)
            d.page.wait_for_timeout(900)
        d.page.mouse.wheel(0, -880)
        d.page.wait_for_timeout(1200)
        d.mark("checked")
        d.caption("Every sentence is checked against the passage it cites", hold=3.0)

        # 2. Hover a citation chip, then open the reader at the passage
        chip = answer.get_by_role("link", name="[1]").first
        d.move_to(chip)
        d.caption("Hover a citation to see the exact passage", hold=3.6)
        d.click(chip)
        d.page.wait_for_url("**/papers/**")
        d.page.wait_for_load_state("networkidle")
        d.page.mouse.move(*d.pos)
        d.page.wait_for_timeout(1500)
        d.caption("Click through: the reader opens at the highlighted passage", hold=3.5)

        # 3. Citation graph of the paper
        d.mark("graph")
        tab = d.page.get_by_role("tab", name="Citation graph")
        d.click(tab)
        d.page.wait_for_timeout(2500)
        d.caption("Citation graph: references and citing papers in the corpus", hold=1.0)
        canvas = d.page.locator("canvas").first
        canvas.evaluate("e => e.scrollIntoView({block: 'center', behavior: 'smooth'})")
        d.page.wait_for_timeout(1200)
        box = canvas.bounding_box()
        assert box
        for fx, fy in ((0.5, 0.5), (0.35, 0.4), (0.62, 0.58)):
            d.page.mouse.move(box["x"] + box["width"] * fx, box["y"] + box["height"] * fy, steps=25)
            d.page.wait_for_timeout(700)
        d.page.wait_for_timeout(1200)

        # 4. Get full text for an abstract-only paper
        d.goto(f"{APP}/papers/{FETCH_PAPER}")
        d.caption("Abstract-only papers can be fetched on demand", hold=2.0)
        d.click(d.page.get_by_role("button", name="Get full text"))
        d.caption("Fetched politely from arXiv, parsed with GROBID, chunked and embedded", hold=2.5)
        d.wait(lambda: bool(api_json(f"/papers/{FETCH_PAPER}").get("full_text")), timeout=600)
        d.goto(f"{APP}/papers/{FETCH_PAPER}")
        d.page.mouse.wheel(0, 900)
        d.caption("Full text ready to read and cite", hold=3.0)

        # 5. Deep question
        d.ask(Q_DEEP, "deep")
        d.caption("Deep mode fetches full text for the top papers and follows their references")
        d.wait(d.answer_done)
        d.page.wait_for_timeout(1500)
        trace = d.page.get_by_text("Followed references").first
        if trace.count():
            d.move_to(trace, steps=20)
        d.caption("Papers found through references are added to the evidence", hold=3.2)
        d.move_to(d.page.get_by_role("heading", name="Sources").first, steps=20)
        d.page.mouse.wheel(0, -500)
        d.caption("Preprint claims are attributed, never stated as settled fact", hold=3.5)

        # 6. Math question, sources and BibTeX
        d.ask(Q_MATH, "quick")
        d.caption("Mathematics too")
        d.wait(d.answer_done)
        d.page.wait_for_timeout(1500)
        d.caption("Mathematics too: answers built from the papers, with the proof cited", hold=2.5)
        d.move_to(d.page.get_by_role("heading", name="Sources").first, steps=25)
        d.caption("Sources: authors, year, full text or abstract, sections used", hold=3.5)
        export = d.page.get_by_role("link", name="Export BibTeX")
        d.move_to(export)
        with d.page.expect_download() as dl:
            d.click(export)
        text = Path(dl.value.path()).read_text(encoding="utf-8")
        d.page.evaluate("t => window.__bibtex(t)", "\n".join(text.splitlines()[:24]))
        d.caption("One click: BibTeX for every cited paper", hold=4.0)
        d.page.evaluate("() => window.__bibtex(null)")
        d.caption("arXiv Research Engine", hold=2.5)

        video = page.video
        context.close()
        browser.close()
        path = Path(video.path()) if video else None
    marks = {"video": path.name if path else None, "waits": d.waits, "end": d.now(), "events": d.events}
    (RAW / "marks.json").write_text(json.dumps(marks, indent=1), encoding="utf-8")
    print(json.dumps(marks, indent=1))


if __name__ == "__main__":
    main()
