#!/usr/bin/env python3
"""Diagnose why UA results stall on automated runs.

Opens one Sep 24 miles URL with our persistent profile, then snapshots key
text markers + a screenshot every 5s for ~60s. Prints a timeline so we can
see what UA's serving us.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from daily_flight_report import (  # noqa: E402
    UA_BROWSER_PROFILE_DIR,
    _EXTRA_STEALTH_INIT_JS,
    _purge_recaptcha_cookies,
    _united_results_url,
    _ensure_united_signed_in,
    _goto_with_retry,
    dt,
)

CHECK_MARKERS = [
    "Loading results",
    "Flight Information",
    "NONSTOP",
    "1 STOP",
    "Sign in",
    "Verify",
    "verify",
    "challenge",
    "robot",
    "captcha",
    "Press & Hold",
    "No flights",
    "Hi, enlin",
    "miles",
    "$",
]


def snapshot(page, label: str, out_dir: Path) -> dict:
    try:
        text = page.locator("body").inner_text(timeout=5000)
    except Exception as e:
        return {"label": label, "error": f"inner_text failed: {e}"}

    counts = {m: text.count(m) for m in CHECK_MARKERS}
    url = page.url
    # Save snapshot artifacts so we can compare what UA served
    (out_dir / f"flaky_{label}.txt").write_text(text, encoding="utf-8")
    try:
        page.screenshot(path=str(out_dir / f"flaky_{label}.png"), full_page=False)
    except Exception:
        pass
    return {"label": label, "url": url, "text_len": len(text), "counts": counts}


def main() -> int:
    from playwright.sync_api import sync_playwright
    from playwright_stealth import Stealth

    UA_BROWSER_PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    out_dir = ROOT / "reports"

    # Pick a date that has consistently failed at night: Sep 24 miles
    url = _united_results_url(dt.date(2026, 9, 24), mode="miles")

    print(f"target url: {url}", flush=True)

    stealth = Stealth(navigator_platform_override="MacIntel")
    with stealth.use_sync(sync_playwright()) as p:
        ctx = p.chromium.launch_persistent_context(
            user_data_dir=str(UA_BROWSER_PROFILE_DIR),
            channel="chrome",
            headless=True,
            viewport={"width": 1440, "height": 1100},
            args=["--disable-blink-features=AutomationControlled"],
        )
        ctx.add_init_script(_EXTRA_STEALTH_INIT_JS)
        removed = _purge_recaptcha_cookies(ctx)
        print(f"purged {removed} reputation cookies", flush=True)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        try:
            t0 = time.monotonic()
            _goto_with_retry(page, url, attempts=2)
            print(f"[{time.monotonic()-t0:5.1f}s] navigated, url={page.url}", flush=True)

            page.wait_for_timeout(3000)
            try:
                _ensure_united_signed_in(page)
                print(
                    f"[{time.monotonic()-t0:5.1f}s] signed-in check passed",
                    flush=True,
                )
            except Exception as e:
                print(
                    f"[{time.monotonic()-t0:5.1f}s] signed-in check raised: {e}",
                    flush=True,
                )

            for i, wait_s in enumerate([5, 5, 5, 10, 10, 10, 15]):
                page.wait_for_timeout(wait_s * 1000)
                snap = snapshot(page, f"sep24_t{int(time.monotonic()-t0)}s", out_dir)
                print(
                    f"[{time.monotonic()-t0:5.1f}s] url={snap.get('url')[:80]} "
                    f"len={snap.get('text_len')} "
                    f"counts={ {k:v for k,v in snap.get('counts',{}).items() if v} }",
                    flush=True,
                )
                # Once we see flight cards, we're done
                if snap.get("counts", {}).get("Flight Information", 0) > 0:
                    print("results loaded — stopping early", flush=True)
                    break

            # Try also detecting iframe / modal overlay
            frame_count = len(page.frames)
            print(f"frame count: {frame_count}", flush=True)
            for f in page.frames:
                if f != page.main_frame:
                    print(f"  iframe url: {f.url}", flush=True)
        finally:
            ctx.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
