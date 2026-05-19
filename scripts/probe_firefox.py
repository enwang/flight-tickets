#!/usr/bin/env python3
"""Probe UA with Firefox headless. If page loads cleanly, we can switch
the production scrape to firefox and never pop a browser window again.

Tests cash URL first (no login) — quickest validation. If cash works, we
know the firefox fingerprint passes UA's WAF and we can plan miles next.
"""
from __future__ import annotations

import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from daily_flight_report import (  # noqa: E402
    _united_results_url,
    _goto_with_retry,
    dt,
)


def main() -> int:
    from playwright.sync_api import sync_playwright

    out_dir = ROOT / "reports"
    cash_url = _united_results_url(dt.date(2026, 6, 20), mode="cash")
    print(f"target: {cash_url}", flush=True)

    profile_dir = Path(tempfile.mkdtemp(prefix="ua_firefox_"))
    try:
        with sync_playwright() as p:
            ctx = p.firefox.launch_persistent_context(
                user_data_dir=str(profile_dir),
                headless=True,
                viewport={"width": 1440, "height": 1100},
            )
            page = ctx.pages[0] if ctx.pages else ctx.new_page()

            t0 = time.monotonic()
            print(f"[{time.monotonic()-t0:5.1f}s] goto", flush=True)
            try:
                _goto_with_retry(page, cash_url, attempts=2)
            except Exception as e:
                print(f"goto FAILED: {e}", flush=True)
                ctx.close()
                return 2

            print(f"[{time.monotonic()-t0:5.1f}s] navigated, waiting", flush=True)
            page.wait_for_timeout(15000)

            text = page.locator("body").inner_text(timeout=10000)
            (out_dir / "probe_firefox_cash_jun20.txt").write_text(text, encoding="utf-8")
            print(
                f"text_len={len(text)} "
                f"NONSTOP={text.count('NONSTOP')} "
                f"$={text.count('$')} "
                f"loading={'Loading results' in text} "
                f"sorry={'unable to complete' in text}",
                flush=True,
            )
            ctx.close()
    finally:
        import shutil
        shutil.rmtree(profile_dir, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
