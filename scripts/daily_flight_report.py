#!/usr/bin/env python3
"""Daily award + revenue tracker for United SFO -> Shanghai.

Tracks per window:
- Cheapest one-way nonstop Economy mileage award
- Cheapest one-way nonstop Premium economy mileage award
- Cheapest one-way nonstop Economy cash fare
- Cheapest one-way nonstop Premium economy cash fare
- Deal signals based on this tracker's own historical low and average

Date windows:
- 2026-06-20 .. 2026-06-27
- 2026-09-24 .. 2026-09-30
"""

from __future__ import annotations

import datetime as dt
import base64
import json
import os
import re
import smtplib
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from email.message import EmailMessage
from pathlib import Path
from typing import Any

BASE_DIR = Path(__file__).resolve().parents[1]
REPORT_DIR = BASE_DIR / "reports"
REPORT_DIR.mkdir(parents=True, exist_ok=True)
PRICE_HISTORY_FILE = REPORT_DIR / "price_history.jsonl"
EMAIL_LOG_FILE = REPORT_DIR / "email_log.jsonl"
SCRAPE_DEBUG_FILE = REPORT_DIR / "scrape_debug.jsonl"
UA_BROWSER_PROFILE_DIR = BASE_DIR / "browser_profiles" / "united_playwright"
UA_FIREFOX_PROFILE_DIR = BASE_DIR / "browser_profiles" / "united_firefox"
REPORT_SEND_NOT_BEFORE_HOUR = 8
REPORT_SEND_NOT_BEFORE_MINUTE = 0

# Each entry declares the date window plus, per fare mode, the cabins we want
# scraped. Omit a mode entirely to skip it for that window.
TRACKED_WINDOWS: list[dict[str, Any]] = [
    {
        "label": "Jun 20-27",
        "start": dt.date(2026, 6, 20),
        "end": dt.date(2026, 6, 27),
        "modes": {
            "miles": ("economy",),
            "cash": ("economy",),
        },
    },
    {
        "label": "Sep 24-30",
        "start": dt.date(2026, 9, 24),
        "end": dt.date(2026, 9, 30),
        "modes": {
            "miles": ("economy", "premium_economy"),
        },
    },
]

# Legacy single-window aliases retained for unused SerpAPI/Kiwi helpers below.
OUTBOUND_START = TRACKED_WINDOWS[-1]["start"]
OUTBOUND_END = TRACKED_WINDOWS[-1]["end"]
RETURN_START = TRACKED_WINDOWS[-1]["start"]
RETURN_END = TRACKED_WINDOWS[-1]["end"]

ORIGIN = "SFO"
DEST = "PVG"
CURRENCY = "USD"
TARGET_AIRLINE_CODE = "UA"
TARGET_AIRLINE_NAME = "United"
TRACKED_CLASSES = (
    ("Economy", 1, "economy"),
    ("Premium economy", 2, "premium_economy"),
)
FARE_MODES: tuple[str, ...] = ("miles", "cash")


@dataclass
class FareResult:
    price: float | None
    airline: str | None
    depart_date: str | None
    return_date: str | None
    url: str | None
    samples_checked: int = 0
    api_source: str | None = None
    # Populated by main() when price is None — last known value from history
    # so the email always carries some signal even on a scrape failure.
    fallback_price: float | None = None
    fallback_depart_date: str | None = None
    fallback_scraped_on: str | None = None


def daterange(start: dt.date, end: dt.date) -> list[dt.date]:
    days = (end - start).days
    return [start + dt.timedelta(days=i) for i in range(days + 1)]


def build_all_pairs() -> list[tuple[dt.date, dt.date]]:
    pairs: list[tuple[dt.date, dt.date]] = []
    for out_date in daterange(OUTBOUND_START, OUTBOUND_END):
        for ret_date in daterange(RETURN_START, RETURN_END):
            if ret_date > out_date:
                pairs.append((out_date, ret_date))
    return pairs


def _latest_nonstop_pair(rows: list[dict[str, Any]]) -> tuple[dt.date, dt.date] | None:
    for row in reversed(rows):
        out_raw = row.get("nonstop_depart")
        ret_raw = row.get("nonstop_return")
        if not isinstance(out_raw, str) or not isinstance(ret_raw, str):
            continue
        try:
            out_date = dt.date.fromisoformat(out_raw)
            ret_date = dt.date.fromisoformat(ret_raw)
        except ValueError:
            continue
        if OUTBOUND_START <= out_date <= OUTBOUND_END and RETURN_START <= ret_date <= RETURN_END:
            if ret_date > out_date:
                return out_date, ret_date
    return None


def _neighbor_pairs(center: tuple[dt.date, dt.date]) -> list[tuple[dt.date, dt.date]]:
    out_date, ret_date = center
    offsets = [
        (0, 0),
        (1, 0),
        (-1, 0),
        (0, -1),
        (0, 1),
        (1, -1),
        (-1, 1),
    ]
    pairs: list[tuple[dt.date, dt.date]] = []
    seen: set[tuple[dt.date, dt.date]] = set()
    for out_offset, ret_offset in offsets:
        candidate = (
            out_date + dt.timedelta(days=out_offset),
            ret_date + dt.timedelta(days=ret_offset),
        )
        cand_out, cand_ret = candidate
        if not (OUTBOUND_START <= cand_out <= OUTBOUND_END):
            continue
        if not (RETURN_START <= cand_ret <= RETURN_END):
            continue
        if cand_ret <= cand_out or candidate in seen:
            continue
        seen.add(candidate)
        pairs.append(candidate)
    return pairs


def http_json(url: str, timeout: int = 45) -> dict[str, Any]:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0",
            "Accept": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def serpapi_google_flights(
    api_key: str,
    outbound: dt.date,
    inbound: dt.date | None,
    stops_param: int,
    travel_class: int = 1,
) -> dict[str, Any]:
    # SerpAPI Google Flights parameters.
    # Docs evolve; these params are widely used and safe fallbacks.
    params = {
        "engine": "google_flights",
        "departure_id": ORIGIN,
        "arrival_id": DEST,
        "outbound_date": outbound.isoformat(),
        "currency": CURRENCY,
        "hl": "en",
        "gl": "us",
        "stops": str(stops_param),
        "include_airlines": TARGET_AIRLINE_CODE,
        "travel_class": str(travel_class),
        "sort_by": "2",
        "type": "1" if inbound else "2",
        "api_key": api_key,
    }
    if inbound:
        params["return_date"] = inbound.isoformat()
    url = "https://serpapi.com/search.json?" + urllib.parse.urlencode(params)
    return http_json(url)


def extract_best_price(
    data: dict[str, Any],
    max_layovers_total: int,
) -> tuple[float | None, str | None, str | None]:
    candidates: list[tuple[float, str | None, str | None]] = []

    for bucket in ("best_flights", "other_flights"):
        for option in data.get(bucket, []) or []:
            layovers = option.get("layovers") or []
            if len(layovers) > max_layovers_total:
                continue
            price = option.get("price")
            if isinstance(price, (int, float)):
                legs = option.get("flights", []) or []
                if legs and not all(_is_target_airline_leg(leg) for leg in legs):
                    continue
                airline = legs[0].get("airline") if legs else None
                token = option.get("booking_token")
                candidates.append((float(price), airline, token))

    if not candidates:
        price = data.get("price")
        if isinstance(price, (int, float)):
            return float(price), None, None
        return None, None, None

    best = min(candidates, key=lambda x: x[0])
    return best[0], best[1], best[2]


def _is_target_airline_leg(leg: dict[str, Any]) -> bool:
    airline = str(leg.get("airline") or "")
    flight_number = str(leg.get("flight_number") or "")
    airline_code = str(leg.get("airline_code") or leg.get("airline_id") or "")
    return (
        airline_code.upper() == TARGET_AIRLINE_CODE
        or flight_number.upper().startswith(TARGET_AIRLINE_CODE)
        or TARGET_AIRLINE_NAME.lower() in airline.lower()
    )


def choose_daily_pairs(limit: int, history_rows: list[dict[str, Any]] | None = None) -> list[tuple[dt.date, dt.date]]:
    # Keep API usage predictable on the free tier while spending some of the budget
    # near the last best nonstop fare and the rest across the full date matrix.
    if limit <= 0:
        return []
    all_pairs = build_all_pairs()
    if not all_pairs:
        return []

    selected: list[tuple[dt.date, dt.date]] = []
    seen: set[tuple[dt.date, dt.date]] = set()

    def add_pair(pair: tuple[dt.date, dt.date]) -> None:
        if pair in seen or len(selected) >= limit:
            return
        seen.add(pair)
        selected.append(pair)

    focus_budget = 0
    if history_rows:
        latest_pair = _latest_nonstop_pair(history_rows)
        if latest_pair:
            focus_budget = max(1, min(limit // 2, 3))
            for pair in _neighbor_pairs(latest_pair):
                add_pair(pair)
                if len(selected) >= focus_budget:
                    break

    remaining = limit - len(selected)
    if remaining <= 0:
        return selected

    start = dt.date.today().toordinal() % len(all_pairs)
    stride = max(1, len(all_pairs) // remaining)
    idx = start
    for _ in range(len(all_pairs)):
        add_pair(all_pairs[idx % len(all_pairs)])
        if len(selected) >= limit:
            return selected
        idx += stride

    for offset in range(len(all_pairs)):
        add_pair(all_pairs[(start + offset) % len(all_pairs)])
        if len(selected) >= limit:
            break

    return selected


def find_cheapest_fare(
    api_key: str,
    stops_param: int,
    max_layovers_total: int,
    pairs: list[tuple[dt.date, dt.date]],
) -> FareResult:
    best = FareResult(None, None, None, None, None, 0)

    for out_date, ret_date in pairs:
        best.samples_checked += 1
        try:
            data = serpapi_google_flights(api_key, out_date, ret_date, stops_param)
        except Exception:
            continue

        price, airline, booking_token = extract_best_price(data, max_layovers_total)
        if price is None:
            continue

        if best.price is None or price < best.price:
            query = (
                f"Flights from {ORIGIN} to {DEST} on {out_date.isoformat()} "
                f"return {ret_date.isoformat()}"
            )
            deep_link = (
                "https://www.google.com/travel/flights?q="
                + urllib.parse.quote(query)
            )
            if booking_token:
                # Keep a tokenized link when available; it may open closer to checkout.
                deep_link = (
                    "https://www.google.com/travel/flights/booking?"
                    + urllib.parse.urlencode({"token": booking_token})
                )
            best = FareResult(
                price=price,
                airline=airline,
                depart_date=out_date.isoformat(),
                return_date=ret_date.isoformat(),
                url=deep_link,
                samples_checked=best.samples_checked,
            )

    return best


def find_cheapest_one_way_window(
    api_key: str,
    travel_class: int,
    max_layovers_total: int = 1,
) -> FareResult:
    best = FareResult(None, None, None, None, None, 0, "serpapi")

    for out_date in daterange(OUTBOUND_START, OUTBOUND_END):
        best.samples_checked += 1
        try:
            data = serpapi_google_flights(
                api_key,
                outbound=out_date,
                inbound=None,
                stops_param=2,
                travel_class=travel_class,
            )
        except Exception:
            continue

        price, airline, booking_token = extract_best_price(data, max_layovers_total)
        if price is None:
            continue

        if best.price is None or price < best.price:
            query = f"Flights from {ORIGIN} to {DEST} on {out_date.isoformat()}"
            deep_link = (
                "https://www.google.com/travel/flights?q="
                + urllib.parse.quote(query)
            )
            if booking_token:
                deep_link = (
                    "https://www.google.com/travel/flights/booking?"
                    + urllib.parse.urlencode({"token": booking_token})
                )
            best = FareResult(
                price=price,
                airline=airline,
                depart_date=out_date.isoformat(),
                return_date=None,
                url=deep_link,
                samples_checked=best.samples_checked,
                api_source="serpapi",
            )

    return best


def _price_from_calendar_text(text: str) -> tuple[int, float] | None:
    normalized = " ".join(text.split())
    m = re.match(r"^(\d{1,2})\s+\$([\d,]+)", normalized)
    if not m:
        return None
    return int(m.group(1)), float(m.group(2).replace(",", ""))


def _goto_with_retry(page: Any, url: str, attempts: int = 3) -> None:
    last_error: Exception | None = None
    for _ in range(attempts):
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=60000)
            return
        except Exception as e:
            last_error = e
            page.wait_for_timeout(3000)
    if last_error:
        raise last_error


def _choose_airport(page: Any, field_id: str, option_text: str, value: str) -> None:
    field = page.locator(f"#{field_id}")
    field.fill(value)
    page.get_by_role("option", name=re.compile(option_text, re.I)).first.click(timeout=15000)


def _open_calendar_on_month(page: Any, month_name: str) -> None:
    page.get_by_role("button", name=re.compile(r"(Choose|Change) date", re.I)).first.click(timeout=15000)
    for _ in range(12):
        if page.get_by_role("grid", name=month_name).count() > 0:
            return
        page.get_by_role("button", name="Next month").click(timeout=10000)
        page.wait_for_timeout(1000)
    raise RuntimeError(f"Unable to find {month_name} calendar")


def _scrape_month_prices(page: Any, month_name: str, start_day: int, end_day: int) -> dict[int, float]:
    grid = page.get_by_role("grid", name=month_name)
    grid.locator("button").first.wait_for(timeout=10000)
    raw_buttons = grid.locator("button").all_inner_texts()
    prices: dict[int, float] = {}
    for raw in raw_buttons:
        parsed = _price_from_calendar_text(raw)
        if not parsed:
            continue
        day, price = parsed
        if start_day <= day <= end_day:
            prices[day] = price
    return prices


def _best_from_day_values(
    values: dict[dt.date, float],
    *,
    mode: str,
    fallback_date: dt.date,
    samples_checked: int = 0,
) -> FareResult:
    if not values:
        return FareResult(
            None,
            None,
            None,
            None,
            _united_results_url(fallback_date, mode=mode),
            samples_checked,
            "united.com browser",
        )
    best_date = min(values, key=lambda d: values[d])
    return FareResult(
        price=values[best_date],
        airline=TARGET_AIRLINE_NAME,
        depart_date=best_date.isoformat(),
        return_date=None,
        url=_united_results_url(best_date, mode=mode),
        samples_checked=samples_checked or len(values),
        api_source="united.com browser",
    )


def _united_results_url(out_date: dt.date, *, mode: str = "miles") -> str:
    params = {
        "f": ORIGIN,
        "t": DEST,
        "d": out_date.isoformat(),
        "tt": "1",
        "sc": "7",
        "px": "1",
        "taxng": "1",
        "newHP": "True",
        "clm": "7",
        "st": "bestmatches",
        "tqp": "A" if mode == "miles" else "R",
    }
    if mode == "miles":
        params["at"] = "1"
    return "https://www.united.com/en/us/fsr/choose-flights?" + urllib.parse.urlencode(params)


def _load_env_file() -> None:
    env_path = BASE_DIR / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


def _keychain_password(account: str) -> str | None:
    try:
        return subprocess.check_output(
            [
                "security",
                "find-generic-password",
                "-s",
                "flight_tickets_united",
                "-a",
                account,
                "-w",
            ],
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
    except Exception:
        return None


def _fill_first_visible(page: Any, selectors: list[str], value: str) -> bool:
    for selector in selectors:
        locator = page.locator(selector)
        try:
            count = locator.count()
        except Exception:
            count = 0
        for idx in range(count):
            item = locator.nth(idx)
            try:
                if item.is_visible(timeout=500):
                    item.fill(value, timeout=3000)
                    return True
            except Exception:
                continue
    return False


def _click_visible_button(page: Any, pattern: re.Pattern[str]) -> bool:
    try:
        page.get_by_role("button", name=pattern).last.click(timeout=5000)
        return True
    except Exception:
        pass
    try:
        page.locator("button").filter(has_text=pattern).last.click(timeout=5000)
        return True
    except Exception:
        return False


def _login_prompt_visible(page: Any, text: str) -> bool:
    if "You must be signed-in" in text:
        return True
    for selector in ("#MPIDEmailField", 'input[type="password"]', 'input[type="email"]'):
        locator = page.locator(selector)
        try:
            for idx in range(locator.count()):
                if locator.nth(idx).is_visible(timeout=500):
                    return True
        except Exception:
            continue
    return False


def _ensure_united_signed_in(page: Any) -> None:
    text = page.locator("body").inner_text(timeout=10000)
    if not _login_prompt_visible(page, text):
        return

    _load_env_file()
    account = os.getenv("UA_ACCOUNT", "").strip()
    password = _keychain_password(account) if account else None
    if not account or not password:
        raise RuntimeError(
            "United MileagePlus session is not signed in and UA credentials "
            "were not found in .env/macOS Keychain."
        )

    _fill_first_visible(
        page,
        ["#MPIDEmailField", 'input[name="MPIDEmail"]', 'input[type="email"]'],
        account,
    )
    _click_visible_button(page, re.compile(r"Continue|Next", re.I))
    page.wait_for_timeout(7000)

    if _fill_first_visible(page, ['#password', 'input[type="password"]'], password):
        page.keyboard.press("Enter")
        page.wait_for_timeout(3000)
        _click_visible_button(page, re.compile(r"^Sign in$", re.I))
        page.wait_for_timeout(15000)

    text = page.locator("body").inner_text(timeout=10000)
    if re.search(r"verification code|enter code|verify|one-time|security code", text, re.I):
        raise RuntimeError(
            "United needs a fresh verification code. Run: "
            ".venv/bin/python scripts/setup_united_browser_login.py"
        )
    if _login_prompt_visible(page, text):
        raise RuntimeError(
            "United MileagePlus session is not signed in. Run: "
            ".venv/bin/python scripts/setup_united_browser_login.py"
        )


def _extract_result_miles(text: str, cabin_pattern: str, *, nonstop_only: bool = True) -> float | None:
    calendar_pattern = re.compile(
        r"From\s+([\d,]+(?:\.\d+)?)\s*(K|k)?\s*miles?\s+" + cabin_pattern,
        re.I,
    )
    values: list[float] = []
    fare_card_pattern = re.compile(
        r"Now\s+([\d,]+(?:\.\d+)?)\s*(K|k)?\s*miles?\b"
        r"(?:(?!Select fare|Flight Information).){0,300}?"
        + cabin_pattern,
        re.I | re.S,
    )
    patterns = (fare_card_pattern,) if nonstop_only else (calendar_pattern, fare_card_pattern)
    search_texts = _nonstop_fare_texts(text) if nonstop_only else [text]
    for search_text in search_texts:
        for pattern in patterns:
            for m in pattern.finditer(search_text):
                val = float(m.group(1).replace(",", ""))
                if m.group(2):
                    val *= 1000
                values.append(val)
    return min(values) if values else None


def _nonstop_fare_texts(text: str) -> list[str]:
    chunks = text.split("Flight Information")
    if len(chunks) < 2:
        return []

    fare_texts: list[str] = []
    for idx in range(1, len(chunks)):
        flight_info = chunks[idx]
        stop_header = "\n".join(
            line.strip() for line in flight_info.splitlines()[:4] if line.strip()
        )
        if not re.search(r"\bNON\s*STOP\b|\bNONSTOP\b|0\s+stops?", stop_header, re.I):
            continue

        # Fare card data ("Now X miles … United Economy") is in this same chunk,
        # after the flight details block. Find the first fare marker.
        for marker in ("CARDMEMBERS SAVE", "Saver Award", "Now"):
            pos = flight_info.find(marker)
            if pos >= 0:
                fare_texts.append(flight_info[pos:])
                break
        else:
            # Fallback: also check the preceding chunk (old page layout)
            previous = chunks[idx - 1]
            start_markers = [
                m for m in (
                    previous.find("CARDMEMBERS SAVE"),
                    previous.find("Saver Award"),
                    previous.find("Now"),
                )
                if m >= 0
            ]
            if start_markers:
                fare_texts.append(previous[min(start_markers):])
    return fare_texts


def _extract_calendar_day_miles(text: str, day: int, *, nonstop_only: bool = True) -> float | None:
    if nonstop_only:
        # United's date strip can show the cheapest award across all stops. Use it
        # only when callers explicitly allow connecting itineraries.
        return None
    day_pattern = re.compile(
        rf"(?:Choose|Now Showing)\s+[A-Za-z]+,\s+[A-Za-z]+\s+{day},\s+\d{{4}}"
        r".{0,180}?([\d,]+(?:\.\d+)?)\s*(K|k)?\s*miles?\b",
        re.I | re.S,
    )
    values: list[float] = []
    for m in day_pattern.finditer(text):
        val = float(m.group(1).replace(",", ""))
        if m.group(2):
            val *= 1000
        values.append(val)
    return min(values) if values else None


def _wait_for_results(page: Any, *, mode: str = "miles", timeout_ms: int = 25000) -> None:
    # Exit fast when UA shows its backend error, so we don't burn the whole
    # timeout per dead page. Success markers differ by mode.
    error_check = (
        '|| body.includes("unable to complete your request") '
        '|| body.includes("try again later")'
    )
    if mode == "miles":
        js = """() => {
            const body = document.body.innerText || "";
            return (
                body.includes("miles") && body.includes("Flight Information")
            ) || body.includes("No flights found") || body.includes("no results")""" + error_check + ";\n        }"
    else:
        js = """() => {
            const body = document.body.innerText || "";
            return (
                body.includes("$") && /\\bNON\\s*STOP\\b|\\b\\d+\\s+STOPS?\\b/.test(body)
            ) || body.includes("No flights found") || body.includes("no results")""" + error_check + ";\n        }"
    try:
        page.wait_for_function(js, timeout=timeout_ms)
    except Exception:
        pass


def _set_date_via_calendar(page: Any, out_date: dt.date) -> None:
    """Click the DepartDate field, navigate the calendar to out_date, and click the day."""
    page.locator("#DepartDate").click(timeout=5000)
    page.wait_for_timeout(1500)

    target_month = out_date.strftime("%B %Y")  # e.g. "September 2026"
    for _ in range(20):
        # Check if the target month is visible in the open calendar
        header = page.locator('[class*="datepicker"] [class*="month"], [class*="calendar"] [class*="month"]')
        visible_text = " ".join(h.inner_text() for h in header.all()) if header.count() else ""
        if out_date.strftime("%B") in visible_text and str(out_date.year) in visible_text:
            break
        try:
            page.get_by_role("button", name=re.compile(r"next month|forward", re.I)).first.click(timeout=5000)
            page.wait_for_timeout(500)
        except Exception:
            break

    # Click the specific day button (aria-label often includes full date string)
    full_date_label = out_date.strftime("%-d").lstrip("0")  # day without leading zero
    try:
        # Try aria-label like "September 24, 2026"
        page.get_by_role(
            "button",
            name=re.compile(rf"{out_date.strftime('%B')} {out_date.day},? {out_date.year}", re.I),
        ).first.click(timeout=5000)
    except Exception:
        # Fallback: click the numbered day button
        try:
            page.get_by_role("button", name=re.compile(rf"^{full_date_label}$")).first.click(timeout=5000)
        except Exception:
            pass

    page.wait_for_timeout(500)
    # Dismiss calendar if a Done/Close button appeared
    try:
        page.get_by_role("button", name=re.compile(r"^done$|^close$", re.I)).first.click(timeout=2000)
    except Exception:
        pass


_UA_BACKEND_ERROR_MARKERS = (
    "unable to complete your request",
    "try again later",
)


def _search_united_results(page: Any, out_date: dt.date, *, mode: str) -> None:
    """Navigate directly to United's results URL for one-way SFO->PVG."""
    url = _united_results_url(out_date, mode=mode)
    success_markers = ("Flight Information",) if mode == "miles" else ("NONSTOP", "1 STOP", "2 STOPS")
    for attempt in range(2):
        _goto_with_retry(page, url, attempts=2)
        page.wait_for_timeout(3000)
        if mode == "miles":
            _ensure_united_signed_in(page)

        _wait_for_results(page, mode=mode)

        text = page.locator("body").inner_text(timeout=10000)
        if any(marker in text for marker in _UA_BACKEND_ERROR_MARKERS):
            raise TimeoutError(
                f"UA backend error for {out_date.isoformat()} ({mode}): "
                "site returned 'unable to complete your request'"
            )
        if "Loading results" not in text or any(marker in text for marker in success_markers):
            return
        if attempt == 0:
            page.wait_for_timeout(5000)

    raise TimeoutError(f"United results did not finish loading for {out_date.isoformat()}")


def _cabin_patterns(mode: str) -> dict[str, str]:
    if mode == "miles":
        return {
            "economy": r"United Economy\b",
            "premium_economy": r"(?:United )?Premium Plus\b",
        }
    # Cash result cards
    return {
        "economy": r"\bEconomy\b(?!\s*Plus)",
        "premium_economy": r"\bPremium\s*Plus\b",
    }


_STOP_MARKER_RE = re.compile(r"\b(NON\s*STOP|NONSTOP|\d+\s+STOPS?)\b", re.I)
_CASH_FARE_TILE_RE = re.compile(
    r"From\s*\n\s*\$([\d,]+)(?:\.\d{2})?\s*\n\s*([^\n]+)",
    re.I,
)


def _nonstop_flight_chunks(text: str) -> list[str]:
    """Split UA results text into per-flight chunks, keeping only nonstop ones."""
    markers = list(_STOP_MARKER_RE.finditer(text))
    chunks: list[str] = []
    for idx, m in enumerate(markers):
        if "NON" not in m.group(0).upper().replace(" ", ""):
            continue
        start = m.end()
        end = markers[idx + 1].start() if idx + 1 < len(markers) else len(text)
        chunks.append(text[start:end])
    return chunks


def _cash_cabin_key(label: str) -> str | None:
    """Map a UA cabin label string to our cabin keys; ignore Polaris/First."""
    low = label.lower()
    if "polaris" in low or "first" in low or "business" in low:
        return None
    if "premium plus" in low:
        return "premium_economy"
    if "economy" in low:
        return "economy"
    return None


def _extract_result_cash_by_cabin(text: str) -> dict[str, float]:
    """Return {cabin_key: cheapest USD} parsed from UA cash results text."""
    by_cabin: dict[str, list[float]] = {key: [] for _, _, key in TRACKED_CLASSES}
    for chunk in _nonstop_flight_chunks(text):
        for m in _CASH_FARE_TILE_RE.finditer(chunk):
            try:
                price = float(m.group(1).replace(",", ""))
            except ValueError:
                continue
            if price < 100:
                continue
            key = _cash_cabin_key(m.group(2))
            if key is None:
                continue
            by_cabin[key].append(price)
    return {key: min(vals) for key, vals in by_cabin.items() if vals}


def _scrape_all_cabins_window(
    page: Any,
    window_start: dt.date,
    window_end: dt.date,
    mode: str,
    cabin_keys: tuple[str, ...],
    *,
    window_label: str = "",
) -> dict[str, FareResult]:
    cabin_patterns = _cabin_patterns(mode)
    by_key: dict[str, dict[dt.date, float]] = {key: {} for key in cabin_keys}
    samples_checked = 0

    for out_date in daterange(window_start, window_end):
        samples_checked += 1
        url = _united_results_url(out_date, mode=mode)
        text: str | None = None
        error: str | None = None
        extracted: dict[str, float | None] = {key: None for key in cabin_keys}
        t0 = time.monotonic()
        try:
            _search_united_results(page, out_date, mode=mode)
            text = page.locator("body").inner_text(timeout=10000)
        except RuntimeError:
            raise
        except TimeoutError as e:
            error = f"timeout: {e}"
        except Exception as e:
            error = f"exception: {e}"

        if text and not error:
            if mode == "miles":
                for key in cabin_keys:
                    price = _extract_result_miles(
                        text, cabin_patterns[key], nonstop_only=True
                    )
                    if price is None and key == "economy":
                        price = _extract_calendar_day_miles(
                            text, out_date.day, nonstop_only=True
                        )
                    if price is not None:
                        by_key[key][out_date] = price
                        extracted[key] = price
            else:
                cash_by_cabin = _extract_result_cash_by_cabin(text)
                for key in cabin_keys:
                    if key in cash_by_cabin:
                        by_key[key][out_date] = cash_by_cabin[key]
                        extracted[key] = cash_by_cabin[key]

        log_scrape_attempt(
            window_label=window_label,
            out_date=out_date,
            mode=mode,
            url=url,
            text=text,
            extracted=extracted,
            error=error,
            elapsed_ms=int((time.monotonic() - t0) * 1000),
        )

    return {
        key: _best_from_day_values(
            by_key[key],
            mode=mode,
            fallback_date=window_start,
            samples_checked=samples_checked,
        )
        for key in cabin_keys
    }


_EXTRA_STEALTH_INIT_JS = r"""
// Belt-and-suspenders fingerprint patches on top of playwright-stealth.
// UA's results page loads invisible reCAPTCHA which silently blocks the
// search submission when the score is low — these patches help the score.

Object.defineProperty(navigator, 'webdriver', { get: () => undefined });

if (!navigator.plugins || navigator.plugins.length === 0) {
    Object.defineProperty(navigator, 'plugins', {
        get: () => ([
            { name: 'PDF Viewer', filename: 'internal-pdf-viewer', description: 'Portable Document Format' },
            { name: 'Chrome PDF Plugin', filename: 'internal-pdf-viewer', description: 'Portable Document Format' },
            { name: 'Chromium PDF Plugin', filename: 'internal-pdf-viewer', description: 'Portable Document Format' },
        ]),
    });
}

Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });

const _originalQuery = window.navigator.permissions && window.navigator.permissions.query;
if (_originalQuery) {
    window.navigator.permissions.query = (params) =>
        params && params.name === 'notifications'
            ? Promise.resolve({ state: Notification.permission })
            : _originalQuery(params);
}

window.chrome = window.chrome || {};
window.chrome.runtime = window.chrome.runtime || {};
window.chrome.app = window.chrome.app || { isInstalled: false };

// Spoof hairline detection (reCAPTCHA sometimes probes this)
const _getParameter = WebGLRenderingContext.prototype.getParameter;
WebGLRenderingContext.prototype.getParameter = function(parameter) {
    if (parameter === 37445) return 'Apple Inc.';
    if (parameter === 37446) return 'Apple M2';
    return _getParameter.apply(this, arguments);
};
"""


_RECAPTCHA_COOKIE_PREFIXES = (
    "_GRECAPTCHA",
    "__gads",
    "__gpi",
    "__eoi",
    "_ga",
    "_gid",
    "IDE",
    "DSID",
    "test_cookie",
)


def _minimize_chrome_for_profile(profile_path: str) -> int:
    """Minimize Chrome windows belonging to the Playwright-launched instance.

    Targets only processes whose argv contains ``user-data-dir=<profile_path>``
    so the user's regular Chrome session is untouched. Returns the number of
    AppleScript invocations that succeeded; 0 if the helper is unavailable
    (e.g., accessibility permission not granted to Terminal/this app).
    """
    try:
        out = subprocess.run(
            ["pgrep", "-f", f"user-data-dir={profile_path}"],
            capture_output=True,
            text=True,
            check=False,
        )
    except Exception:
        return 0
    pids = [p.strip() for p in out.stdout.split() if p.strip()]
    minimized = 0
    for pid in pids:
        script = (
            'tell application "System Events" to '
            f'tell (first application process whose unix id is {pid}) '
            'to set value of attribute "AXMinimized" of every window to true'
        )
        try:
            result = subprocess.run(
                ["osascript", "-e", script],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode == 0:
                minimized += 1
        except Exception:
            continue
    return minimized


def _purge_recaptcha_cookies(context: Any) -> int:
    """Remove third-party reputation cookies (reCAPTCHA, ad networks) while
    leaving united.com session cookies alone. Returns number removed."""
    try:
        cookies = context.cookies()
    except Exception:
        return 0
    keep: list[dict[str, Any]] = []
    removed = 0
    for c in cookies:
        name = str(c.get("name", ""))
        domain = str(c.get("domain", ""))
        if any(name.startswith(p) for p in _RECAPTCHA_COOKIE_PREFIXES):
            removed += 1
            continue
        # Drop cookies set by Google / ad networks even on first-party domains
        if any(d in domain for d in ("google.com", "doubleclick", "gstatic")):
            removed += 1
            continue
        keep.append(c)
    try:
        context.clear_cookies()
        if keep:
            context.add_cookies(keep)
    except Exception:
        pass
    return removed


def find_united_browser_calendar_fares(
    headless: bool = False,
) -> dict[str, dict[str, dict[str, FareResult]]]:
    """Scrape United for every configured (window, mode, cabin) combination.

    Returns: { window_label: { mode: { cabin_key: FareResult } } }
    Only modes/cabins listed in each window's "modes" entry are scraped.
    """
    try:
        from playwright.sync_api import sync_playwright
    except Exception as e:
        raise RuntimeError(
            "Playwright is not installed. Run: .venv/bin/python -m pip install playwright"
        ) from e

    try:
        from playwright_stealth import Stealth
    except Exception as e:
        raise RuntimeError(
            "playwright-stealth missing. Run: "
            ".venv/bin/python -m pip install playwright-stealth"
        ) from e

    UA_BROWSER_PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    results: dict[str, dict[str, dict[str, FareResult]]] = {}

    stealth = Stealth(navigator_platform_override="MacIntel")

    with stealth.use_sync(sync_playwright()) as p:
        context = p.chromium.launch_persistent_context(
            user_data_dir=str(UA_BROWSER_PROFILE_DIR),
            channel="chrome",  # use installed Google Chrome, not bundled Chromium
            # Headed mode (real Chrome) gives the best fingerprint for UA's
            # bot detection. The window is hidden via AppleScript minimize
            # right after launch — see _minimize_chrome_for_profile.
            headless=False,
            viewport={"width": 1440, "height": 1100},
            args=["--disable-blink-features=AutomationControlled"],
        )
        context.add_init_script(_EXTRA_STEALTH_INIT_JS)
        # Hide the Chrome window from the user's screen. Has to come AFTER
        # the context exists (so the window is open) but ideally before any
        # heavy work. AppleScript needs accessibility permission.
        time.sleep(0.8)
        minimized = _minimize_chrome_for_profile(str(UA_BROWSER_PROFILE_DIR))
        print(f"minimized {minimized} chrome window(s)", flush=True)
        removed = _purge_recaptcha_cookies(context)
        if removed:
            print(f"purged {removed} reputation cookies", flush=True)
        page = context.pages[0] if context.pages else context.new_page()
        try:
            _goto_with_retry(page, "https://www.united.com/en/us/")
            page.wait_for_timeout(8000)
            try:
                page.get_by_role("button", name="Accept cookies").click(timeout=3000)
            except Exception:
                pass

            for window in TRACKED_WINDOWS:
                label = window["label"]
                results[label] = {}
                for mode, cabin_keys in window["modes"].items():
                    results[label][mode] = _scrape_all_cabins_window(
                        page,
                        window["start"],
                        window["end"],
                        mode,
                        cabin_keys,
                        window_label=label,
                    )
        finally:
            context.close()

    return results


def find_cheapest_nonstop_flexible(api_key: str) -> FareResult:
    """Two-pass scan to find cheapest nonstop across the full date window.

    Pass 1: scan all outbound dates with the mid-point return date.
    Pass 2: scan all return dates with the best outbound date from pass 1.
    """
    outbound_dates = daterange(OUTBOUND_START, OUTBOUND_END)
    return_dates = daterange(RETURN_START, RETURN_END)

    # Pick a stable mid-point return date for pass 1.
    mid_idx = len(return_dates) // 2
    anchor_return = return_dates[mid_idx]

    best_outbound: dt.date | None = None
    best_price_pass1: float | None = None
    samples = 0

    # Pass 1: find cheapest outbound date.
    for out_date in outbound_dates:
        samples += 1
        try:
            data = serpapi_google_flights(api_key, out_date, anchor_return, stops_param=1)
        except Exception:
            continue
        price, _, _ = extract_best_price(data, max_layovers_total=0)
        if price is not None:
            if best_price_pass1 is None or price < best_price_pass1:
                best_price_pass1 = price
                best_outbound = out_date

    if best_outbound is None:
        return FareResult(None, None, None, None, None, samples_checked=samples)

    # Pass 2: find cheapest return date for the best outbound.
    best = FareResult(None, None, None, None, None, samples_checked=samples)
    for ret_date in return_dates:
        samples += 1
        best.samples_checked = samples
        try:
            data = serpapi_google_flights(api_key, best_outbound, ret_date, stops_param=1)
        except Exception:
            continue
        price, airline, booking_token = extract_best_price(data, max_layovers_total=0)
        if price is None:
            continue
        if best.price is None or price < best.price:
            query = (
                f"Flights from {ORIGIN} to {DEST} on {best_outbound.isoformat()} "
                f"return {ret_date.isoformat()}"
            )
            deep_link = (
                "https://www.google.com/travel/flights?q="
                + urllib.parse.quote(query)
            )
            if booking_token:
                deep_link = (
                    "https://www.google.com/travel/flights/booking?"
                    + urllib.parse.urlencode({"token": booking_token})
                )
            best = FareResult(
                price=price,
                airline=airline,
                depart_date=best_outbound.isoformat(),
                return_date=ret_date.isoformat(),
                url=deep_link,
                samples_checked=samples,
            )

    return best


def kiwi_search_nonstop_flexible(api_key: str) -> FareResult:
    """Kiwi/Tequila: single call covering the full date window for nonstop round trips.

    Free tier: ~500 calls/month.
    Date format for Kiwi: dd/mm/YYYY.
    """
    params = {
        "fly_from": ORIGIN,
        "fly_to": DEST,
        "date_from": OUTBOUND_START.strftime("%d/%m/%Y"),
        "date_to": OUTBOUND_END.strftime("%d/%m/%Y"),
        "return_from": RETURN_START.strftime("%d/%m/%Y"),
        "return_to": RETURN_END.strftime("%d/%m/%Y"),
        "max_stopovers": "0",
        "flight_type": "round",
        "curr": CURRENCY,
        "sort": "price",
        "limit": "20",
    }
    url = "https://api.tequila.kiwi.com/v2/search?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(
        url,
        headers={"apikey": api_key, "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=45) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception:
        return FareResult(None, None, None, None, None, 0, "kiwi")

    itineraries = data.get("data") or []
    best = FareResult(None, None, None, None, None, len(itineraries), "kiwi")

    for itin in itineraries:
        price = itin.get("price")
        if not isinstance(price, (int, float)):
            continue
        # Extract outbound and return leg dates from the route array.
        dep_date: str | None = None
        ret_date: str | None = None
        for leg in (itin.get("route") or []):
            if leg.get("flyFrom") == ORIGIN and dep_date is None:
                dep_date = (leg.get("local_departure") or "")[:10]
            elif leg.get("flyFrom") == DEST and ret_date is None:
                ret_date = (leg.get("local_departure") or "")[:10]
        if best.price is None or float(price) < best.price:
            airlines = itin.get("airlines") or []
            best = FareResult(
                price=float(price),
                airline=airlines[0] if airlines else None,
                depart_date=dep_date,
                return_date=ret_date,
                url=itin.get("deep_link") or None,
                samples_checked=len(itineraries),
                api_source="kiwi",
            )

    return best


def _travelpayouts_calendar(token: str, origin: str, dest: str, year: int, month: int) -> dict[str, float]:
    """Travelpayouts /v1/prices/calendar: nonstop one-way prices per day.

    Returns {date_str: price} for nonstop flights only (transfers==0).
    Free tier: no hard call limit on cached data.
    """
    params = {
        "origin": origin,
        "destination": dest,
        "month": f"{year}-{month:02d}-01",
        "currency": CURRENCY.lower(),
        "token": token,
    }
    url = "https://api.travelpayouts.com/v1/prices/calendar?" + urllib.parse.urlencode(params)
    try:
        data = http_json(url)
    except Exception:
        return {}
    prices: dict[str, float] = {}
    for date_str, info in (data.get("data") or {}).items():
        if not isinstance(info, dict):
            continue
        if info.get("transfers", 1) != 0:
            continue
        p = info.get("price")
        if isinstance(p, (int, float)):
            prices[date_str] = float(p)
    return prices


def travelpayouts_best_nonstop(token: str) -> FareResult:
    """Find cheapest nonstop round trip via Travelpayouts price calendars.

    Fetches one-way price calendars for the outbound window and return window,
    then combines the cheapest outbound day + cheapest return day.
    Note: the total is an estimate (sum of two one-way cached fares).
    """
    # Outbound: SFO -> PVG (may span two calendar months)
    out_prices: dict[str, float] = {}
    for month in sorted(set([OUTBOUND_START.month, OUTBOUND_END.month])):
        out_prices.update(
            _travelpayouts_calendar(token, ORIGIN, DEST, OUTBOUND_START.year, month)
        )
    # Return: PVG -> SFO
    ret_prices: dict[str, float] = {}
    for month in sorted(set([RETURN_START.month, RETURN_END.month])):
        ret_prices.update(
            _travelpayouts_calendar(token, DEST, ORIGIN, RETURN_START.year, month)
        )

    # Filter to our date windows.
    valid_out = {
        d: p for d, p in out_prices.items()
        if OUTBOUND_START <= dt.date.fromisoformat(d) <= OUTBOUND_END
    }
    valid_ret = {
        d: p for d, p in ret_prices.items()
        if RETURN_START <= dt.date.fromisoformat(d) <= RETURN_END
    }

    samples = len(valid_out) + len(valid_ret)
    if not valid_out or not valid_ret:
        return FareResult(None, None, None, None, None, samples, "travelpayouts")

    best_dep = min(valid_out, key=lambda d: valid_out[d])
    best_ret = min(valid_ret, key=lambda d: valid_ret[d])
    total = valid_out[best_dep] + valid_ret[best_ret]

    return FareResult(
        price=total,
        airline=None,
        depart_date=best_dep,
        return_date=best_ret,
        url=None,
        samples_checked=samples,
        api_source="travelpayouts (est)",
    )


def merge_fare_results(results: list[FareResult]) -> FareResult:
    """Return the lowest-price result; accumulate total samples_checked."""
    total_samples = sum(r.samples_checked for r in results)
    valid = [r for r in results if r.price is not None]
    if not valid:
        base = results[0] if results else FareResult(None, None, None, None, None)
        base.samples_checked = total_samples
        return base
    best = min(valid, key=lambda r: r.price)  # type: ignore[arg-type]
    best.samples_checked = total_samples
    return best


def google_search_text(api_key: str, query: str) -> list[dict[str, Any]]:
    params = {
        "engine": "google",
        "q": query,
        "hl": "en",
        "gl": "us",
        "api_key": api_key,
    }
    url = "https://serpapi.com/search.json?" + urllib.parse.urlencode(params)
    data = http_json(url)
    return data.get("organic_results", []) or []


def extract_miles_from_text(text: str) -> list[int]:
    values: list[int] = []
    for m in re.finditer(r"\b(\d{1,3}(?:,\d{3})?)\s*(?:miles|mile|k)\b", text, re.I):
        raw = m.group(1).replace(",", "")
        val = int(raw)
        if "k" in m.group(0).lower() and val < 1000:
            val *= 1000
        if 5000 <= val <= 500000:
            values.append(val)
    return values


def _mentions_route(text: str) -> bool:
    normalized = text.lower()
    origin_terms = ("sfo", "san francisco")
    dest_terms = ("pvg", "shanghai")
    return any(term in normalized for term in origin_terms) and any(
        term in normalized for term in dest_terms
    )


def find_united_mileage_signal(api_key: str) -> tuple[str, int | None, str | None]:
    # United award pricing is frequently behind login and personalized.
    # We use a best-effort public signal, but only trust route-specific snippets.
    q = (
        'site:united.com (SFO OR "San Francisco") (PVG OR Shanghai) '
        '"award" "miles" "MileagePlus" "United"'
    )
    try:
        results = google_search_text(api_key, q)
    except Exception:
        return "Unavailable (query failed)", None, None

    best: int | None = None
    src: str | None = None

    for item in results[:10]:
        blob = " ".join(
            [
                str(item.get("title", "")),
                str(item.get("snippet", "")),
            ]
        )
        if not _mentions_route(blob):
            continue
        for val in extract_miles_from_text(blob):
            if best is None or val < best:
                best = val
                src = item.get("link")

    if best is None:
        return "No public UA mileage deal detected", None, None

    return f"Potential UA deal around {best:,} miles", best, src


def _format_amount(value: float, mode: str) -> str:
    if mode == "miles":
        return f"{value:,.0f} miles"
    return f"${value:,.0f}"


def format_fare(label: str, fare: FareResult, *, mode: str) -> str:
    if fare.price is None:
        if fare.fallback_price is not None:
            return (
                f"{label}: current scrape failed; "
                f"last known {_format_amount(fare.fallback_price, mode)} "
                f"for {fare.fallback_depart_date} "
                f"(scraped {fare.fallback_scraped_on})"
            )
        return f"{label}: unavailable (no current data, no history)"
    trip_text = "round trip" if fare.return_date else "one way"
    dates = (
        f"{fare.depart_date} -> {fare.return_date}"
        if fare.return_date
        else str(fare.depart_date)
    )
    line = (
        f"{label}: {_format_amount(fare.price, mode)} {trip_text} "
        f"({dates})"
    )
    if fare.airline:
        line += f" via {fare.airline}"
    if fare.api_source:
        line += f" [{fare.api_source}]"
    if fare.url:
        line += f"\n{label} link: {fare.url}"
    return line


def load_price_history(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        s = raw.strip()
        if not s:
            continue
        try:
            obj = json.loads(s)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            rows.append(obj)
    return rows


def append_price_history(path: Path, row: dict[str, Any]) -> None:
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=True) + "\n")


def prune_old_daily_reports(directory: Path, keep_days: int = 7) -> int:
    """Delete daily_YYYY-MM-DD.txt files older than `keep_days` (by date stamp,
    not by mtime). Returns count removed. Cron logs and JSONL stores are never
    touched here — those are the durable artifacts."""
    if not directory.exists():
        return 0
    files = sorted(directory.glob("daily_*.txt"))
    if len(files) <= keep_days:
        return 0
    removed = 0
    for f in files[:-keep_days]:
        try:
            f.unlink()
            removed += 1
        except OSError:
            continue
    return removed


def _append_jsonl(path: Path, row: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=True) + "\n")


def log_email(
    *,
    recipients: list[str],
    subject: str,
    body: str,
    html_body: str | None,
    ok: bool,
    detail: str,
    transport: str,
) -> None:
    _append_jsonl(
        EMAIL_LOG_FILE,
        {
            "timestamp": dt.datetime.now().isoformat(timespec="seconds"),
            "transport": transport,
            "recipients": recipients,
            "subject": subject,
            "body": body,
            "html_body": html_body,
            "ok": ok,
            "detail": detail,
        },
    )


def log_scrape_attempt(
    *,
    window_label: str,
    out_date: dt.date,
    mode: str,
    url: str,
    text: str | None,
    extracted: dict[str, float | None],
    error: str | None,
    elapsed_ms: int,
) -> None:
    """Record what UA actually served us so we can debug bot-detection patterns."""
    text = text or ""
    body_excerpt = text[:1200]
    _append_jsonl(
        SCRAPE_DEBUG_FILE,
        {
            "timestamp": dt.datetime.now().isoformat(timespec="seconds"),
            "window_label": window_label,
            "out_date": out_date.isoformat(),
            "mode": mode,
            "url": url,
            "elapsed_ms": elapsed_ms,
            "error": error,
            "extracted": extracted,
            "text_len": len(text),
            "markers": {
                "loading_results": "Loading results" in text,
                "flight_information": "Flight Information" in text,
                "nonstop": "NONSTOP" in text,
                "unable_to_complete": "unable to complete" in text,
                "try_again_later": "try again later" in text,
                "signed_in": "Hi, enlin" in text,
                "no_flights_found": "No flights found" in text,
                "choose_date": "Choose a date" in text,
            },
            "body_excerpt": body_excerpt,
        },
    )


def _find_last_price(rows: list[dict[str, Any]], key: str) -> tuple[float | None, str | None]:
    for row in reversed(rows):
        val = row.get(key)
        if isinstance(val, (int, float)):
            return float(val), str(row.get("date")) if row.get("date") else None
    return None, None


def _find_last_known_fare(
    rows: list[dict[str, Any]],
    key: str,
) -> tuple[float | None, str | None, str | None]:
    """Walk history backwards; return (price, depart_date, scraped_on) of the
    most recent row where `key` is non-null. Used to fill the email when the
    current scrape failed for this (window, mode, cabin)."""
    depart_key = f"{key}_depart"
    for row in reversed(rows):
        val = row.get(key)
        if isinstance(val, (int, float)):
            depart = row.get(depart_key)
            return (
                float(val),
                str(depart) if depart else None,
                str(row.get("date")) if row.get("date") else None,
            )
    return None, None, None


def _collect_prices(rows: list[dict[str, Any]], key: str) -> list[float]:
    out: list[float] = []
    for row in rows:
        val = row.get(key)
        if isinstance(val, (int, float)):
            out.append(float(val))
    return out


def _delta_text(current: float, previous: float, mode: str) -> str:
    diff = current - previous
    pct = (diff / previous) * 100 if previous else 0.0
    unit = "miles" if mode == "miles" else "USD"
    noun = "mileage" if mode == "miles" else "price"
    if abs(diff) < 0.5:
        return f"unchanged vs last tracked {noun}"
    direction = "higher" if diff > 0 else "lower"
    return f"{direction} by {abs(diff):,.0f} {unit} ({abs(pct):.1f}%) vs last tracked {noun}"


def _buy_signal(current: float, history: list[float], mode: str) -> str:
    if not history:
        return "No historical baseline yet."
    min_hist = min(history)
    avg_hist = sum(history) / len(history)
    noun = "mileage" if mode == "miles" else "price"
    cap = noun.capitalize()
    if current <= min_hist * 1.02:
        return f"Very good {noun} deal, please consider booking."
    if current <= avg_hist * 0.93:
        return f"Good {noun} deal, booking is reasonable."
    if current >= avg_hist * 1.10:
        return f"{cap} is elevated vs past levels; waiting may be better."
    return f"{cap} is in the normal range."


def _is_deal_signal(signal: str) -> bool:
    return signal.startswith(("Very good mileage", "Good mileage", "Very good price", "Good price"))


def _filter_history(
    rows: list[dict[str, Any]],
    window_label: str,
    window_start: dt.date,
    mode: str,
) -> list[dict[str, Any]]:
    """Return rows for the (window, mode) pair, translating legacy rows on the fly."""
    out: list[dict[str, Any]] = []
    for row in rows:
        row_mode = row.get("fare_mode")
        row_label = row.get("window_label")
        if row_mode is not None or row_label is not None:
            if row_label == window_label and row_mode == mode:
                out.append(row)
            continue
        # Legacy schema (pre-multi-window): single Sep window in miles only.
        if mode != "miles":
            continue
        if row.get("window_start") != window_start.isoformat():
            continue
        out.append(
            {
                "date": row.get("date"),
                "economy": row.get("economy_miles"),
                "premium_economy": row.get("premium_economy_miles"),
                "economy_depart": row.get("economy_depart"),
                "premium_economy_depart": row.get("premium_economy_depart"),
            }
        )
    return out


def build_price_context(
    label: str,
    fare: FareResult,
    history_rows: list[dict[str, Any]],
    cabin_key: str,
    *,
    mode: str,
) -> tuple[list[str], list[str], bool]:
    trend_lines: list[str] = []
    prediction_lines: list[str] = []
    noun = "mileage" if mode == "miles" else "price"
    unit = "miles" if mode == "miles" else "USD"
    if fare.price is None:
        trend_lines.append(f"{label}: no current {noun}, unable to compare.")
        prediction_lines.append(f"{label}: no deal signal.")
        return trend_lines, prediction_lines, False

    last_price, last_date = _find_last_price(history_rows, cabin_key)
    hist = _collect_prices(history_rows, cabin_key)

    if last_price is not None:
        trend_lines.append(
            f"{label}: {_delta_text(fare.price, last_price, mode)} (last date: {last_date})"
        )
    else:
        trend_lines.append(f"{label}: no past tracked {noun} yet; baseline created today.")

    if hist:
        signal = _buy_signal(fare.price, hist, mode)
        prediction_lines.append(
            f"{label}: historical low {_format_amount(min(hist), mode)}; {signal}"
        )
        return trend_lines, prediction_lines, _is_deal_signal(signal)

    prediction_lines.append(f"{label}: historical low N/A; No historical baseline yet.")
    return trend_lines, prediction_lines, False


def notify_macos(message: str) -> None:
    script = (
        'display notification '
        + json.dumps(message)
        + ' with title "Daily Flight Update"'
    )
    try:
        subprocess.run(
            ["osascript", "-e", script],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception:
        pass


def send_mail_local(subject: str, body: str, to_addr: str) -> tuple[bool, str]:
    try:
        p = subprocess.run(
            ["/usr/bin/mail", "-s", subject, to_addr],
            input=body.encode("utf-8"),
            check=False,
            capture_output=True,
        )
        if p.returncode == 0:
            return True, "local mail accepted"
        err = (p.stderr or b"").decode("utf-8", "ignore").strip()
        return False, f"local mail failed: {err or f'rc={p.returncode}'}"
    except Exception:
        return False, "local mail exception"


def send_mail_smtp(
    subject: str,
    body: str,
    html_body: str | None,
    recipients: list[str],
    host: str,
    port: int,
    username: str | None,
    password: str | None,
    from_addr: str,
    use_tls: bool,
    use_ssl: bool,
) -> tuple[bool, str]:
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = from_addr
    msg["To"] = ", ".join(recipients)
    msg.set_content(body)
    if html_body:
        msg.add_alternative(html_body, subtype="html")
    try:
        if use_ssl:
            with smtplib.SMTP_SSL(host, port, timeout=30) as server:
                if username and password:
                    server.login(username, password)
                server.send_message(msg)
        else:
            with smtplib.SMTP(host, port, timeout=30) as server:
                if use_tls:
                    server.starttls()
                if username and password:
                    server.login(username, password)
                server.send_message(msg)
        return True, "smtp sent"
    except Exception as e:
        return False, f"smtp failed: {e}"


def parse_recipients(raw: str) -> list[str]:
    # Accept comma, semicolon, or whitespace separated email list.
    parts = re.split(r"[,\s;]+", raw.strip())
    return [p for p in parts if p]


def _fare_summary_line(label: str, fare: FareResult, mode: str) -> str:
    if fare.price is None:
        if fare.fallback_price is not None:
            return (
                f"{label}: current N/A — last known "
                f"{_format_amount(fare.fallback_price, mode)} "
                f"for {fare.fallback_depart_date} "
                f"(scraped {fare.fallback_scraped_on})"
            )
        return f"{label}: unavailable"
    return f"{label}: {_format_amount(fare.price, mode)} one way ({fare.depart_date})"


def build_concise_email(
    scrape: dict[str, dict[str, dict[str, FareResult]]],
    trend_lines: list[str],
    prediction_lines: list[str],
    deal_lines: list[str],
) -> tuple[str, str]:
    cabin_labels = {key: label for label, _, key in TRACKED_CLASSES}
    text_lines: list[str] = ["UA SFO -> PVG non-stop one-way"]
    html_parts: list[str] = [
        "<html><body>",
        "<p><b>UA SFO -&gt; PVG non-stop one-way</b></p>",
    ]

    for window in TRACKED_WINDOWS:
        window_label = window["label"]
        text_lines.append("")
        text_lines.append(f"=== {window_label} ===")
        html_parts.append(f"<p><b>{window_label}</b><br/>")
        for mode, cabin_keys in window["modes"].items():
            mode_title = "Miles" if mode == "miles" else "Cash"
            text_lines.append(f"[{mode_title}]")
            html_parts.append(f"<i>{mode_title}</i><br/>")
            for key in cabin_keys:
                fare = scrape[window_label][mode][key]
                text_lines.append(_fare_summary_line(cabin_labels[key], fare, mode))
                if fare.url:
                    text_lines.append(f"  link: {fare.url}")
                amount_text = _fare_summary_line(cabin_labels[key], fare, mode)
                if fare.url:
                    html_parts.append(
                        f'{amount_text} — <a href="{fare.url}">link</a><br/>'
                    )
                else:
                    html_parts.append(f"{amount_text}<br/>")
        html_parts.append("</p>")

    text_lines.append("")
    text_lines.append("Deal:")
    text_lines.extend(deal_lines or ["No deal signal yet."])
    text_lines.append("")
    text_lines.append("Trend:")
    text_lines.extend(trend_lines or ["No trend data yet."])
    text_lines.append("")
    text_lines.append("Prediction:")
    text_lines.extend(prediction_lines or ["No prediction yet."])

    html_parts.append(
        "<p><b>Deal</b><br/>" + "<br/>".join(deal_lines or ["No deal signal yet."]) + "</p>"
    )
    html_parts.append(
        "<p><b>Trend</b><br/>" + "<br/>".join(trend_lines or ["No trend data yet."]) + "</p>"
    )
    html_parts.append(
        "<p><b>Prediction</b><br/>" + "<br/>".join(prediction_lines or ["No prediction yet."])
        + "</p>"
    )
    html_parts.append("</body></html>")
    return "\n".join(text_lines), "".join(html_parts)


def twilio_request(
    account_sid: str,
    auth_token: str,
    url: str,
    data: bytes | None = None,
) -> dict[str, Any]:
    creds = f"{account_sid}:{auth_token}".encode("utf-8")
    auth = base64.b64encode(creds).decode("ascii")
    req = urllib.request.Request(
        url,
        data=data,
        method="POST" if data is not None else "GET",
        headers={
            "Authorization": f"Basic {auth}",
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": "flight-tracker/1.0",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        raw = resp.read().decode("utf-8")
        return json.loads(raw) if raw else {}


def send_twilio_sms(
    account_sid: str,
    auth_token: str,
    from_number: str,
    to_number: str,
    body: str,
) -> tuple[bool, str, str | None]:
    url = f"https://api.twilio.com/2010-04-01/Accounts/{account_sid}/Messages.json"
    data = urllib.parse.urlencode(
        {
            "From": from_number,
            "To": to_number,
            "Body": body,
        }
    ).encode("utf-8")
    try:
        payload = twilio_request(account_sid, auth_token, url, data=data)
        sid = payload.get("sid")
        if sid:
            return True, f"twilio accepted ({sid})", sid
        return True, "twilio accepted", None
    except urllib.error.HTTPError as e:
        msg = e.read().decode("utf-8", "ignore")
        return False, f"twilio http {e.code}: {msg[:180]}", None
    except Exception as e:
        return False, f"twilio failed: {e}", None


def fetch_twilio_message_status(
    account_sid: str,
    auth_token: str,
    message_sid: str,
) -> tuple[str | None, str | None]:
    try:
        url = (
            f"https://api.twilio.com/2010-04-01/Accounts/{account_sid}/Messages/{message_sid}.json"
        )
        payload = twilio_request(account_sid, auth_token, url, data=None)
        return (
            str(payload.get("status")) if payload.get("status") else None,
            str(payload.get("error_code")) if payload.get("error_code") else None,
        )
    except Exception:
        return None, None


def env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


def load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        k, v = s.split("=", 1)
        if k and k not in os.environ:
            os.environ[k] = v


def wait_until_report_send_window() -> None:
    now = dt.datetime.now()
    target = now.replace(
        hour=REPORT_SEND_NOT_BEFORE_HOUR,
        minute=REPORT_SEND_NOT_BEFORE_MINUTE,
        second=0,
        microsecond=0,
    )
    if now >= target:
        return
    time.sleep((target - now).total_seconds())


def main() -> int:
    load_env_file(BASE_DIR / ".env")

    report_email_raw = os.getenv("REPORT_EMAIL", "").strip()
    smtp_host = os.getenv("SMTP_HOST", "").strip()
    smtp_port = int(os.getenv("SMTP_PORT", "587").strip() or "587")
    smtp_user = os.getenv("SMTP_USER", "").strip() or None
    smtp_pass = os.getenv("SMTP_PASS", "").strip() or None
    smtp_from = os.getenv("SMTP_FROM", "").strip() or (smtp_user or "")
    smtp_tls = env_bool("SMTP_TLS", True)
    smtp_ssl = env_bool("SMTP_SSL", False)
    twilio_sid = os.getenv("TWILIO_ACCOUNT_SID", "").strip()
    twilio_token = os.getenv("TWILIO_AUTH_TOKEN", "").strip()
    twilio_from = os.getenv("TWILIO_FROM", "").strip()
    sms_to_raw = os.getenv("SMS_TO", "").strip()

    today = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    cabin_labels = {key: label for label, _, key in TRACKED_CLASSES}

    def _window_summary_line(w: dict[str, Any]) -> str:
        parts = []
        for mode, cabin_keys in w["modes"].items():
            cabins = "/".join(cabin_labels[k] for k in cabin_keys)
            parts.append(f"{mode}:{cabins}")
        return (
            f"{w['label']} ({w['start'].isoformat()}..{w['end'].isoformat()}) "
            f"[{', '.join(parts)}]"
        )

    lines = [
        f"Date: {today}",
        "Route: SFO -> PVG | Airline: United only | Non-stop only",
        "Windows:",
        *(f"  - {_window_summary_line(w)}" for w in TRACKED_WINDOWS),
        "",
    ]

    browser_headless = env_bool("UA_BROWSER_HEADLESS", False)

    history_rows = load_price_history(PRICE_HISTORY_FILE)

    def _empty_scrape() -> dict[str, dict[str, dict[str, FareResult]]]:
        return {
            w["label"]: {
                mode: {
                    key: FareResult(None, None, None, None, None, 0, "united.com browser")
                    for key in cabin_keys
                }
                for mode, cabin_keys in w["modes"].items()
            }
            for w in TRACKED_WINDOWS
        }

    try:
        scrape = find_united_browser_calendar_fares(headless=browser_headless)
    except Exception as e:
        scrape = _empty_scrape()
        lines.append(f"United browser search failed: {e}")
        output = "\n".join(lines)
        recipients = parse_recipients(report_email_raw)
        wait_until_report_send_window()
        if recipients:
            subject = f"Flight report failed {dt.date.today().isoformat()}"
            if smtp_host and smtp_from:
                ok, detail = send_mail_smtp(
                    subject=subject,
                    body=output,
                    html_body=None,
                    recipients=recipients,
                    host=smtp_host,
                    port=smtp_port,
                    username=smtp_user,
                    password=smtp_pass,
                    from_addr=smtp_from,
                    use_tls=smtp_tls,
                    use_ssl=smtp_ssl,
                )
                log_email(
                    recipients=recipients,
                    subject=subject,
                    body=output,
                    html_body=None,
                    ok=ok,
                    detail=detail,
                    transport="smtp",
                )
            else:
                local_results = [send_mail_local(subject, output, addr) for addr in recipients]
                all_ok = all(r[0] for r in local_results)
                detail = next((r[1] for r in local_results if not r[0]), "local mail accepted")
                log_email(
                    recipients=recipients,
                    subject=subject,
                    body=output,
                    html_body=None,
                    ok=all_ok,
                    detail=detail,
                    transport="local",
                )
        out_file = REPORT_DIR / f"daily_{dt.date.today().isoformat()}.txt"
        out_file.write_text(output + "\n", encoding="utf-8")
        notify_macos("Flight report failed: United browser search failed")
        return 1

    trend_lines: list[str] = []
    prediction_lines: list[str] = []
    deal_lines: list[str] = []
    any_deal = False

    # Populate last-known fallbacks for any cabin where today's scrape failed.
    for window in TRACKED_WINDOWS:
        window_label = window["label"]
        w_start = window["start"]
        for mode, cabin_keys in window["modes"].items():
            filtered_history = _filter_history(history_rows, window_label, w_start, mode)
            for key in cabin_keys:
                fare = scrape[window_label][mode][key]
                if fare.price is None:
                    fb_price, fb_depart, fb_scraped = _find_last_known_fare(
                        filtered_history, key
                    )
                    fare.fallback_price = fb_price
                    fare.fallback_depart_date = fb_depart
                    fare.fallback_scraped_on = fb_scraped

    for window in TRACKED_WINDOWS:
        window_label = window["label"]
        w_start = window["start"]
        lines.append(f"=== {window_label} ===")
        for mode, cabin_keys in window["modes"].items():
            mode_title = "Miles" if mode == "miles" else "Cash"
            lines.append(f"-- {mode_title} --")
            filtered_history = _filter_history(history_rows, window_label, w_start, mode)
            for key in cabin_keys:
                fare = scrape[window_label][mode][key]
                cabin_label = cabin_labels[key]
                lines.append(format_fare(f"{cabin_label} best non-stop", fare, mode=mode))
                trend, prediction, is_deal = build_price_context(
                    f"{window_label} {mode_title} {cabin_label}",
                    fare,
                    filtered_history,
                    key,
                    mode=mode,
                )
                trend_lines.extend(trend)
                prediction_lines.extend(prediction)
                if is_deal and fare.price is not None:
                    any_deal = True
                    deal_lines.append(
                        f"{window_label} {mode_title} {cabin_label}: DEAL at "
                        f"{_format_amount(fare.price, mode)} on {fare.depart_date}"
                    )
        lines.append("")

    lines.append("Trend:")
    lines.extend(trend_lines)
    lines.append("")
    lines.append("Deal signal:")
    lines.extend(deal_lines if deal_lines else ["No deal detected yet."])
    lines.append("")
    lines.append("Prediction:")
    lines.extend(prediction_lines)

    subject_prefix = "Flight DEAL" if any_deal else "Flight update"
    subject = f"{subject_prefix} {dt.date.today().isoformat()}"
    recipients = parse_recipients(report_email_raw)
    mail_ok = False
    mail_detail = "disabled (REPORT_EMAIL empty)"
    email_text, email_html = build_concise_email(
        scrape=scrape,
        trend_lines=trend_lines,
        prediction_lines=prediction_lines,
        deal_lines=deal_lines,
    )
    wait_until_report_send_window()
    if recipients:
        if smtp_host and smtp_from:
            mail_ok, mail_detail = send_mail_smtp(
                subject=subject,
                body=email_text,
                html_body=email_html,
                recipients=recipients,
                host=smtp_host,
                port=smtp_port,
                username=smtp_user,
                password=smtp_pass,
                from_addr=smtp_from,
                use_tls=smtp_tls,
                use_ssl=smtp_ssl,
            )
            log_email(
                recipients=recipients,
                subject=subject,
                body=email_text,
                html_body=email_html,
                ok=mail_ok,
                detail=mail_detail,
                transport="smtp",
            )
        else:
            results = [send_mail_local(subject, email_text, addr) for addr in recipients]
            mail_ok = all(ok for ok, _ in results)
            if mail_ok:
                mail_detail = "local mail accepted (delivery not guaranteed)"
            else:
                first_err = next((msg for ok, msg in results if not ok), "local mail failed")
                mail_detail = first_err
            log_email(
                recipients=recipients,
                subject=subject,
                body=email_text,
                html_body=email_html,
                ok=mail_ok,
                detail=mail_detail,
                transport="local",
            )
    lines.append("")
    lines.append(
        "Email delivery: "
        + (
            f"sent to {len(recipients)} recipient(s) [{mail_detail}]" if (recipients and mail_ok)
            else f"failed for {len(recipients)} recipient(s) [{mail_detail}]" if recipients
            else mail_detail
        )
    )

    sms_targets = parse_recipients(sms_to_raw)
    sms_ok = False
    sms_detail = "disabled (SMS_TO empty)"
    if sms_targets:
        if twilio_sid and twilio_token and twilio_from:
            sms_lines = [f"UA SFO->PVG {dt.date.today().isoformat()}"]
            for window in TRACKED_WINDOWS:
                window_label = window["label"]
                parts: list[str] = []
                for mode, cabin_keys in window["modes"].items():
                    for key in cabin_keys:
                        fare = scrape[window_label][mode][key]
                        prefix = "Eco" if key == "economy" else "Prem"
                        if fare.price is None:
                            parts.append(f"{prefix} {mode}:-")
                        elif mode == "miles":
                            parts.append(f"{prefix} {fare.price:,.0f}mi")
                        else:
                            parts.append(f"{prefix} ${fare.price:,.0f}")
                sms_lines.append(f"{window_label}: " + "; ".join(parts))
            sms_lines.append("Deal: " + ("; ".join(deal_lines) if deal_lines else "none"))
            sms_body = "\n".join(sms_lines)
            results = [
                send_twilio_sms(
                    account_sid=twilio_sid,
                    auth_token=twilio_token,
                    from_number=twilio_from,
                    to_number=to_num,
                    body=sms_body,
                )
                for to_num in sms_targets
            ]
            sms_ok = all(ok for ok, _, _ in results)
            if sms_ok:
                # Twilio accepted the request; check message status for delivery signal.
                status_parts: list[str] = []
                terminal_fail = False
                for _, _, sid in results:
                    if not sid:
                        continue
                    status, error_code = fetch_twilio_message_status(
                        twilio_sid, twilio_token, sid
                    )
                    if status:
                        if status in {"failed", "undelivered"}:
                            terminal_fail = True
                        if error_code:
                            status_parts.append(f"{sid}:{status}/error={error_code}")
                        else:
                            status_parts.append(f"{sid}:{status}")
                if terminal_fail:
                    sms_ok = False
                sms_detail = (
                    "twilio accepted; " + ", ".join(status_parts)
                    if status_parts
                    else "twilio accepted"
                )
            else:
                sms_detail = next((msg for ok, msg, _ in results if not ok), "twilio failed")
        else:
            sms_detail = "missing TWILIO_ACCOUNT_SID/TWILIO_AUTH_TOKEN/TWILIO_FROM"
    lines.append(
        "SMS delivery: "
        + (
            f"sent to {len(sms_targets)} number(s) [{sms_detail}]"
            if (sms_targets and sms_ok)
            else f"failed for {len(sms_targets)} number(s) [{sms_detail}]"
            if sms_targets
            else sms_detail
        )
    )

    now_iso = dt.datetime.now().isoformat(timespec="seconds")
    today_iso = dt.date.today().isoformat()
    for window in TRACKED_WINDOWS:
        for mode, cabin_keys in window["modes"].items():
            row: dict[str, Any] = {
                "timestamp": now_iso,
                "date": today_iso,
                "route": f"{ORIGIN}-{DEST}",
                "trip_type": "one_way",
                "window_label": window["label"],
                "window_start": window["start"].isoformat(),
                "window_end": window["end"].isoformat(),
                "fare_mode": mode,
            }
            for key in cabin_keys:
                fare = scrape[window["label"]][mode][key]
                row[key] = fare.price
                row[f"{key}_depart"] = fare.depart_date
            append_price_history(PRICE_HISTORY_FILE, row)
    output = "\n".join(lines)

    out_file = REPORT_DIR / f"daily_{dt.date.today().isoformat()}.txt"
    out_file.write_text(output + "\n", encoding="utf-8")
    prune_old_daily_reports(REPORT_DIR, keep_days=7)

    summary: list[str] = []
    for window in TRACKED_WINDOWS:
        window_label = window["label"]
        parts: list[str] = []
        for mode, cabin_keys in window["modes"].items():
            for key in cabin_keys:
                fare = scrape[window_label][mode][key]
                prefix = "Eco" if key == "economy" else "Prem"
                if fare.price is None:
                    parts.append(f"{prefix} {mode}:-")
                elif mode == "miles":
                    parts.append(f"{prefix} {fare.price:,.0f}mi")
                else:
                    parts.append(f"{prefix} ${fare.price:,.0f}")
        summary.append(f"{window_label}: " + ", ".join(parts))
    if deal_lines:
        summary.append("DEAL")

    notify_text = " | ".join(summary) if summary else "Daily report ready"
    if recipients:
        notify_text += " (email sent)" if mail_ok else " (email failed)"
    if sms_targets:
        notify_text += " (sms sent)" if sms_ok else " (sms failed)"
    notify_macos(notify_text)

    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
