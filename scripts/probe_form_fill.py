#!/usr/bin/env python3
"""Probe: can we get UA miles results by filling the form on the homepage
(instead of direct URL navigation, which has been consistently bot-blocked)?

Strategy:
  1. Open united.com homepage
  2. Make sure we're signed in (needed for awards)
  3. Click "One-way" trip type
  4. Click "Show price in: Miles" toggle
  5. Fill From=SFO, To=PVG, Date=2026-09-24
  6. Click Find Flights
  7. Wait for results, dump page text to compare with the broken direct-URL path

Logs to reports/probe_form_fill.txt for inspection.
"""
from __future__ import annotations

import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from daily_flight_report import (  # noqa: E402
    UA_BROWSER_PROFILE_DIR,
    _EXTRA_STEALTH_INIT_JS,
    _ensure_united_signed_in,
    _goto_with_retry,
    _purge_recaptcha_cookies,
    dt,
)


def _try(label: str, fn) -> bool:
    try:
        fn()
        print(f"  ✓ {label}", flush=True)
        return True
    except Exception as e:
        print(f"  ✗ {label}: {e}", flush=True)
        return False


def main() -> int:
    from playwright.sync_api import sync_playwright
    from playwright_stealth import Stealth

    out_dir = ROOT / "reports"
    target_date = dt.date(2026, 9, 24)

    stealth = Stealth(navigator_platform_override="MacIntel")
    t0 = time.monotonic()
    with stealth.use_sync(sync_playwright()) as p:
        ctx = p.chromium.launch_persistent_context(
            user_data_dir=str(UA_BROWSER_PROFILE_DIR),
            channel="chrome",
            headless=True,
            viewport={"width": 1440, "height": 1100},
            args=["--disable-blink-features=AutomationControlled"],
        )
        ctx.add_init_script(_EXTRA_STEALTH_INIT_JS)
        _purge_recaptcha_cookies(ctx)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        try:
            print(f"[{time.monotonic()-t0:5.1f}s] goto home", flush=True)
            _goto_with_retry(page, "https://www.united.com/en/us/", attempts=2)
            page.wait_for_timeout(5000)
            try:
                page.get_by_role("button", name=re.compile(r"Accept cookies", re.I)).click(timeout=3000)
            except Exception:
                pass

            print(f"[{time.monotonic()-t0:5.1f}s] sign-in check", flush=True)
            _ensure_united_signed_in(page)

            # Trip type: One-way
            _try(
                "trip type → One-way",
                lambda: page.get_by_role("radio", name=re.compile(r"^One-way$", re.I)).check(timeout=5000),
            )

            # Award toggle on home form
            _try(
                "click 'Book with miles'",
                lambda: page.get_by_role("checkbox", name=re.compile(r"book with miles", re.I)).check(timeout=5000),
            )

            # From
            _try(
                "fill origin=SFO",
                lambda: page.locator("#bookFlightOriginInput").fill("SFO", timeout=8000),
            )
            page.wait_for_timeout(1500)
            _try(
                "pick SFO option",
                lambda: page.get_by_role("option", name=re.compile(r"SFO|San Francisco", re.I)).first.click(timeout=8000),
            )

            # To
            _try(
                "fill destination=PVG",
                lambda: page.locator("#bookFlightDestinationInput").fill("PVG", timeout=8000),
            )
            page.wait_for_timeout(1500)
            _try(
                "pick PVG option",
                lambda: page.get_by_role("option", name=re.compile(r"PVG|Shanghai", re.I)).first.click(timeout=8000),
            )

            # Date
            _try(
                "open date picker",
                lambda: page.locator("#DepartDate").click(timeout=8000),
            )
            page.wait_for_timeout(1500)
            # Navigate calendar forward to target month
            target_month_name = target_date.strftime("%B %Y")  # e.g. "September 2026"
            for _ in range(20):
                try:
                    if page.locator(f"text={target_month_name}").count() > 0:
                        break
                except Exception:
                    pass
                try:
                    page.get_by_role("button", name=re.compile(r"next month|forward", re.I)).first.click(timeout=3000)
                    page.wait_for_timeout(400)
                except Exception:
                    break
            _try(
                f"click day {target_date.day}",
                lambda: page.get_by_role(
                    "button",
                    name=re.compile(rf"{target_date.strftime('%B')} {target_date.day},? {target_date.year}", re.I),
                ).first.click(timeout=8000),
            )
            page.wait_for_timeout(500)
            try:
                page.get_by_role("button", name=re.compile(r"^done$", re.I)).first.click(timeout=2000)
            except Exception:
                pass

            # Submit
            _try(
                "click Find flights",
                lambda: page.get_by_role("button", name=re.compile(r"find flights|^search$", re.I)).first.click(timeout=8000),
            )

            print(f"[{time.monotonic()-t0:5.1f}s] waiting for results", flush=True)
            page.wait_for_timeout(15000)

            text = page.locator("body").inner_text(timeout=10000)
            (out_dir / "probe_form_fill.txt").write_text(text, encoding="utf-8")
            print(
                f"[{time.monotonic()-t0:5.1f}s] "
                f"text_len={len(text)} "
                f"flight_info={text.count('Flight Information')} "
                f"NONSTOP={text.count('NONSTOP')} "
                f"miles={text.count('miles')} "
                f"loading={'Loading results' in text} "
                f"sorry={'unable to complete' in text}",
                flush=True,
            )
            print(f"final url: {page.url}", flush=True)
        finally:
            ctx.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
