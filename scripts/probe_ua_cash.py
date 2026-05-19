#!/usr/bin/env python3
"""One-shot probe: open one UA cash URL and one UA miles URL, dump DOM + text."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from daily_flight_report import (  # noqa: E402
    UA_BROWSER_PROFILE_DIR,
    _united_results_url,
    _ensure_united_signed_in,
    _goto_with_retry,
    dt,
)


def main() -> int:
    from playwright.sync_api import sync_playwright

    UA_BROWSER_PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    out_dir = ROOT / "reports"

    cash_url = _united_results_url(dt.date(2026, 6, 20), mode="cash")
    miles_url = _united_results_url(dt.date(2026, 9, 24), mode="miles")

    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            user_data_dir=str(UA_BROWSER_PROFILE_DIR),
            headless=True,
            viewport={"width": 1440, "height": 1100},
            args=["--disable-blink-features=AutomationControlled"],
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        try:
            for label, url, want_login in (
                ("cash_jun20", cash_url, False),
                ("miles_sep24", miles_url, True),
            ):
                print(f"--- {label}: {url}", flush=True)
                _goto_with_retry(page, url, attempts=2)
                page.wait_for_timeout(8000)
                if want_login:
                    try:
                        _ensure_united_signed_in(page)
                    except Exception as e:
                        print(f"login check raised: {e}", flush=True)
                page.wait_for_timeout(15000)

                text = page.locator("body").inner_text(timeout=10000)
                (out_dir / f"probe_{label}.txt").write_text(text, encoding="utf-8")

                # Save a lightweight HTML snapshot of fare-card areas.
                html = page.content()
                (out_dir / f"probe_{label}.html").write_text(html, encoding="utf-8")

                # Quick stats
                print(
                    f"  text_len={len(text)}  $count={text.count('$')}  "
                    f"miles_count={text.count('miles')}  flight_info={text.count('Flight Information')}",
                    flush=True,
                )
        finally:
            ctx.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
