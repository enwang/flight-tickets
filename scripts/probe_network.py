#!/usr/bin/env python3
"""Watch the network requests UA's awards page makes (or fails to make).

If the search results never load on the page, we want to know:
  - Is the underlying search API even being called?
  - If yes, what's the response status / body?
  - If no, what's preventing the call (reCAPTCHA, missing token, etc.)?

Logs every united.com response with status + content-length + content-type
to reports/probe_network.log.
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
    _ensure_united_signed_in,
    _goto_with_retry,
    _minimize_chrome_for_profile,
    _purge_recaptcha_cookies,
    _united_results_url,
    dt,
)


def main() -> int:
    from playwright.sync_api import sync_playwright
    from playwright_stealth import Stealth

    out_dir = ROOT / "reports"
    log_path = out_dir / "probe_network.log"
    log = log_path.open("w", encoding="utf-8")

    url = _united_results_url(dt.date(2026, 9, 24), mode="miles")
    print(f"target: {url}", flush=True)
    log.write(f"target: {url}\n")

    seen_search_responses: list[str] = []

    stealth = Stealth(navigator_platform_override="MacIntel")
    with stealth.use_sync(sync_playwright()) as p:
        ctx = p.chromium.launch_persistent_context(
            user_data_dir=str(UA_BROWSER_PROFILE_DIR),
            channel="chrome",
            headless=False,
            viewport={"width": 1440, "height": 1100},
            args=["--disable-blink-features=AutomationControlled"],
        )
        ctx.add_init_script(_EXTRA_STEALTH_INIT_JS)
        time.sleep(0.8)
        minimized = _minimize_chrome_for_profile(str(UA_BROWSER_PROFILE_DIR))
        print(f"minimized {minimized} window(s)", flush=True)
        _purge_recaptcha_cookies(ctx)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()

        def on_response(resp):
            try:
                resp_url = resp.url
                if "united.com" not in resp_url:
                    return
                # Anything that looks like a search/booking API call
                interesting = any(
                    kw in resp_url.lower()
                    for kw in (
                        "search", "flight", "fare", "shopping", "trip",
                        "award", "fsr/api", "/api/",
                    )
                )
                line = (
                    f"{time.strftime('%H:%M:%S')} "
                    f"{resp.status:>3} "
                    f"{resp.request.method:>4} "
                    f"len={resp.headers.get('content-length','?'):>7} "
                    f"ct={resp.headers.get('content-type','?').split(';')[0]:<32} "
                    f"{resp_url[:200]}"
                )
                log.write(line + "\n")
                log.flush()
                if interesting and resp.status >= 400:
                    print(f"!! {line}", flush=True)
                if interesting:
                    seen_search_responses.append(resp_url)
                    # Try to capture body for search API calls
                    if any(kw in resp_url.lower() for kw in ("search", "shopping", "fsr/api", "/api/")):
                        try:
                            body = resp.body()
                            preview = body[:500].decode("utf-8", "ignore")
                            log.write(f"  body[:500]: {preview}\n\n")
                            log.flush()
                        except Exception as e:
                            log.write(f"  body err: {e}\n")
            except Exception:
                pass

        page.on("response", on_response)

        print(f"[{time.strftime('%H:%M:%S')}] goto", flush=True)
        _goto_with_retry(page, url, attempts=2)
        page.wait_for_timeout(3000)
        try:
            _ensure_united_signed_in(page)
            print("signed-in OK", flush=True)
        except Exception as e:
            print(f"sign-in raised: {e}", flush=True)

        # Wait for results or give up after 30s
        page.wait_for_timeout(30000)

        text = page.locator("body").inner_text(timeout=10000)
        print(
            f"final: text_len={len(text)} "
            f"loading={'Loading results' in text} "
            f"flight_info={'Flight Information' in text} "
            f"sorry={'unable to complete' in text}",
            flush=True,
        )
        log.write(f"\nfinal_text_len={len(text)}\n")
        log.write(f"loading={'Loading results' in text} ")
        log.write(f"flight_info={'Flight Information' in text} ")
        log.write(f"sorry={'unable to complete' in text}\n")
        log.write(f"\nseen search/api responses ({len(seen_search_responses)}):\n")
        for u in seen_search_responses:
            log.write(f"  {u}\n")

        log.close()
        ctx.close()
    print(f"log written to {log_path}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
