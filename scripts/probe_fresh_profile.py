#!/usr/bin/env python3
"""Test UA with a totally fresh profile dir.

If this loads results where the regular profile can't, the issue is profile
reputation (cookies/localStorage/CDP fingerprint history).
We test the cash URL since it needs no sign-in.
"""
from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from daily_flight_report import (  # noqa: E402
    UA_BROWSER_PROFILE_DIR,
    _EXTRA_STEALTH_INIT_JS,
    _united_results_url,
    _goto_with_retry,
    dt,
)


def main() -> int:
    from playwright.sync_api import sync_playwright
    from playwright_stealth import Stealth

    use_fresh = "--fresh" in sys.argv
    if use_fresh:
        fresh = Path(tempfile.mkdtemp(prefix="ua_fresh_"))
        profile_dir = fresh
    else:
        fresh = None
        profile_dir = UA_BROWSER_PROFILE_DIR
    print(f"profile: {profile_dir}", flush=True)
    url = _united_results_url(dt.date(2026, 6, 20), mode="cash")
    print(f"target url: {url}", flush=True)

    stealth = Stealth(navigator_platform_override="MacIntel")
    try:
        with stealth.use_sync(sync_playwright()) as p:
            ctx = p.chromium.launch_persistent_context(
                user_data_dir=str(profile_dir),
                channel="chrome",
                headless=True,
                viewport={"width": 1440, "height": 1100},
                args=["--disable-blink-features=AutomationControlled"],
            )
            ctx.add_init_script(_EXTRA_STEALTH_INIT_JS)
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            _goto_with_retry(page, url, attempts=2)
            page.wait_for_timeout(15000)
            text = page.locator("body").inner_text(timeout=5000)
            sorry_marker = "unable to complete your request"
            print(
                f"text_len={len(text)} "
                f"NONSTOP={text.count('NONSTOP')} "
                f"$={text.count('$')} "
                f"loading={'Loading results' in text} "
                f"sorry={sorry_marker in text}",
                flush=True,
            )
            (ROOT / "reports" / "probe_fresh_cash_sep24.txt").write_text(
                text, encoding="utf-8"
            )
            ctx.close()
    finally:
        if fresh:
            shutil.rmtree(fresh, ignore_errors=True)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
