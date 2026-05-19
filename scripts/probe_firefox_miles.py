#!/usr/bin/env python3
"""End-to-end Firefox headless test: sign in to MileagePlus, then load one
miles URL (Sep 24) and one cash URL (Jun 20). Reports whether each works.

If both pass, we can confidently switch the daily script to Firefox.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from daily_flight_report import (  # noqa: E402
    UA_FIREFOX_PROFILE_DIR,
    _ensure_united_signed_in,
    _extract_result_cash_by_cabin,
    _extract_result_miles,
    _goto_with_retry,
    _united_results_url,
    dt,
)


def main() -> int:
    from playwright.sync_api import sync_playwright

    UA_FIREFOX_PROFILE_DIR.mkdir(parents=True, exist_ok=True)

    cash_url = _united_results_url(dt.date(2026, 6, 20), mode="cash")
    miles_url = _united_results_url(dt.date(2026, 9, 24), mode="miles")

    with sync_playwright() as p:
        ctx = p.firefox.launch_persistent_context(
            user_data_dir=str(UA_FIREFOX_PROFILE_DIR),
            headless=True,
            viewport={"width": 1440, "height": 1100},
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        try:
            t0 = time.monotonic()

            # 1) Cash — no sign-in needed; smoke test
            print(f"[{time.monotonic()-t0:5.1f}s] testing CASH (Jun 20)", flush=True)
            try:
                _goto_with_retry(page, cash_url, attempts=2)
                page.wait_for_timeout(12000)
                text = page.locator("body").inner_text(timeout=8000)
                cash_extract = _extract_result_cash_by_cabin(text)
                print(
                    f"  text_len={len(text)} NONSTOP={text.count('NONSTOP')} "
                    f"sorry={'unable to complete' in text} "
                    f"extracted={cash_extract}",
                    flush=True,
                )
            except Exception as e:
                print(f"  CASH failed: {e}", flush=True)

            # 2) Sign-in attempt — go to home, see if we're signed in
            print(f"[{time.monotonic()-t0:5.1f}s] checking sign-in", flush=True)
            try:
                _goto_with_retry(page, "https://www.united.com/en/us/", attempts=2)
                page.wait_for_timeout(6000)
                _ensure_united_signed_in(page)
                home_text = page.locator("body").inner_text(timeout=8000)
                signed_in = "Hi, enlin" in home_text or "MILEAGEPLUS" in home_text.upper()
                print(f"  signed_in={signed_in}", flush=True)
            except Exception as e:
                print(f"  sign-in flow raised: {e}", flush=True)
                signed_in = False

            # 3) Miles — only meaningful if signed in
            print(f"[{time.monotonic()-t0:5.1f}s] testing MILES (Sep 24)", flush=True)
            try:
                _goto_with_retry(page, miles_url, attempts=2)
                page.wait_for_timeout(15000)
                text = page.locator("body").inner_text(timeout=8000)
                miles_eco = _extract_result_miles(text, r"United Economy\b", nonstop_only=True)
                print(
                    f"  text_len={len(text)} "
                    f"flight_info={text.count('Flight Information')} "
                    f"miles_word={text.count('miles')} "
                    f"loading={'Loading results' in text} "
                    f"sorry={'unable to complete' in text} "
                    f"extracted_economy={miles_eco}",
                    flush=True,
                )
                (ROOT / "reports" / "probe_firefox_miles.txt").write_text(text, encoding="utf-8")
            except Exception as e:
                print(f"  MILES failed: {e}", flush=True)

            print(f"[{time.monotonic()-t0:5.1f}s] done", flush=True)
        finally:
            ctx.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
