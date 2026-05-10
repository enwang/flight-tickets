#!/usr/bin/env python3
"""Daily award tracker for United SFO -> Shanghai with flexible date scanning.

Tracks:
- Cheapest one-way nonstop Economy mileage award for the last week of September
- Cheapest one-way nonstop Premium Economy mileage award for the last week of September
- Deal signals based on this tracker's own historical low and average

Date windows:
- Outbound: 2026-09-24 .. 2026-09-30
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
UA_BROWSER_PROFILE_DIR = BASE_DIR / "browser_profiles" / "united_playwright"
REPORT_SEND_NOT_BEFORE_HOUR = 8
REPORT_SEND_NOT_BEFORE_MINUTE = 0

OUTBOUND_START = dt.date(2026, 9, 24)
OUTBOUND_END = dt.date(2026, 9, 30)
RETURN_START = dt.date(2026, 9, 24)
RETURN_END = dt.date(2026, 9, 30)

ORIGIN = "SFO"
DEST = "PVG"
CURRENCY = "USD"
TARGET_AIRLINE_CODE = "UA"
TARGET_AIRLINE_NAME = "United"
TRACKED_CLASSES = (
    ("Economy", 1, "economy"),
    ("Premium economy", 2, "premium_economy"),
)


@dataclass
class FareResult:
    price: float | None
    airline: str | None
    depart_date: str | None
    return_date: str | None
    url: str | None
    samples_checked: int = 0
    api_source: str | None = None


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


def _best_from_day_values(label: str, values: dict[int, float]) -> FareResult:
    if not values:
        return FareResult(None, None, None, None, _united_results_url(OUTBOUND_START), 0, "united.com browser")
    best_day = min(values, key=lambda day: values[day])
    depart = dt.date(OUTBOUND_START.year, OUTBOUND_START.month, best_day)
    return FareResult(
        price=values[best_day],
        airline=TARGET_AIRLINE_NAME,
        depart_date=depart.isoformat(),
        return_date=None,
        url=_united_results_url(depart),
        samples_checked=len(values),
        api_source="united.com browser",
    )


def _united_results_url(out_date: dt.date) -> str:
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
        "at": "1",
        "tqp": "A",
    }
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
    for selector in ("#MPIDEmailField", 'input[type="password"]'):
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


def _wait_for_results(page: Any, timeout_ms: int = 90000) -> None:
    try:
        page.wait_for_function(
            """() => {
                const body = document.body.innerText || "";
                return (
                    body.includes("miles") && body.includes("Flight Information")
                ) || body.includes("No flights found") || body.includes("no results");
            }""",
            timeout=timeout_ms,
        )
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


def _search_united_award(page: Any, out_date: dt.date) -> None:
    """Fill in and submit United's award search form for one-way SFO->PVG."""
    _goto_with_retry(page, "https://www.united.com/en/us/", attempts=2)
    page.wait_for_timeout(5000)
    _ensure_united_signed_in(page)

    # Enable "Book with miles" (award) checkbox — id="award"
    award_cb = page.locator("#award")
    if award_cb.count() and not award_cb.is_checked():
        award_cb.click(timeout=5000)
        page.wait_for_timeout(1000)

    # Set one-way — id="radiofield-item-id-flightType-1"
    oneway = page.locator("#radiofield-item-id-flightType-1")
    if oneway.count() and not oneway.is_checked():
        oneway.click(timeout=5000)
        page.wait_for_timeout(500)

    # Fill origin (only if not already set correctly)
    origin_val = page.locator("#bookFlightOriginInput").input_value() or ""
    if ORIGIN not in origin_val:
        _choose_airport(page, "bookFlightOriginInput", r"San Francisco.*SFO", ORIGIN)

    # Fill destination
    dest_val = page.locator("#bookFlightDestinationInput").input_value() or ""
    if DEST not in dest_val:
        _choose_airport(page, "bookFlightDestinationInput", r"Shanghai.*PVG", DEST)

    # Set departure date via calendar
    _set_date_via_calendar(page, out_date)

    # Submit
    try:
        page.get_by_role("button", name=re.compile(r"^Find flights?$|^Search$", re.I)).first.click(timeout=10000)
    except Exception:
        page.locator('button[type="submit"]').first.click(timeout=5000)


def _scrape_results_window(page: Any, label: str, key: str) -> FareResult:
    cabin_patterns = {
        "economy": r"United Economy\b",
        "premium_economy": r"(?:United )?Premium Plus\b",
    }
    miles_by_day: dict[int, float] = {}
    for out_date in daterange(OUTBOUND_START, OUTBOUND_END):
        try:
            _search_united_award(page, out_date)
            _wait_for_results(page)
            text = page.locator("body").inner_text(timeout=10000)
        except RuntimeError:
            raise
        except Exception:
            continue
        miles = _extract_result_miles(text, cabin_patterns[key], nonstop_only=True)
        if miles is None and key == "economy":
            miles = _extract_calendar_day_miles(text, out_date.day, nonstop_only=True)
        if miles is not None:
            miles_by_day[out_date.day] = miles
    return _best_from_day_values(label, miles_by_day)


def find_united_browser_calendar_fares(headless: bool = False) -> dict[str, FareResult]:
    try:
        from playwright.sync_api import sync_playwright
    except Exception as e:
        raise RuntimeError(
            "Playwright is not installed. Run: .venv/bin/python -m pip install playwright"
        ) from e

    UA_BROWSER_PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    month_name = OUTBOUND_START.strftime("%B %Y")
    results: dict[str, FareResult] = {}

    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            user_data_dir=str(UA_BROWSER_PROFILE_DIR),
            headless=headless,
            viewport={"width": 1440, "height": 1100},
            args=["--disable-blink-features=AutomationControlled"],
        )
        page = context.pages[0] if context.pages else context.new_page()
        try:
            _goto_with_retry(page, "https://www.united.com/en/us/")
            page.wait_for_timeout(8000)
            try:
                page.get_by_role("button", name="Accept cookies").click(timeout=3000)
            except Exception:
                pass

            for idx, (label, _, key) in enumerate(TRACKED_CLASSES):
                scan_page = page if idx == 0 else context.new_page()
                results[key] = _scrape_results_window(scan_page, label, key)
                if scan_page is not page:
                    scan_page.close()
        finally:
            context.close()

    for _, _, key in TRACKED_CLASSES:
        results.setdefault(
            key,
            FareResult(None, None, None, None, None, 0, "united.com browser"),
        )
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


def format_fare(label: str, fare: FareResult) -> str:
    if fare.price is None:
        return f"{label}: unavailable"
    trip_text = "round trip" if fare.return_date else "one way"
    dates = (
        f"{fare.depart_date} -> {fare.return_date}"
        if fare.return_date
        else str(fare.depart_date)
    )
    line = (
        f"{label}: {fare.price:,.0f} miles {trip_text} "
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


def _find_last_price(rows: list[dict[str, Any]], key: str) -> tuple[float | None, str | None]:
    for row in reversed(rows):
        val = row.get(key)
        if isinstance(val, (int, float)):
            return float(val), str(row.get("date")) if row.get("date") else None
    return None, None


def _collect_prices(rows: list[dict[str, Any]], key: str) -> list[float]:
    out: list[float] = []
    for row in rows:
        val = row.get(key)
        if isinstance(val, (int, float)):
            out.append(float(val))
    return out


def _delta_text(current: float, previous: float) -> str:
    diff = current - previous
    pct = (diff / previous) * 100 if previous else 0.0
    if abs(diff) < 0.5:
        return "unchanged vs last tracked price"
    direction = "higher" if diff > 0 else "lower"
    return f"{direction} by {abs(diff):,.0f} miles ({abs(pct):.1f}%) vs last tracked mileage"


def _buy_signal(current: float, history: list[float]) -> str:
    if not history:
        return "No historical baseline yet."
    min_hist = min(history)
    avg_hist = sum(history) / len(history)
    if current <= min_hist * 1.02:
        return "Very good mileage deal, please consider booking."
    if current <= avg_hist * 0.93:
        return "Good mileage deal, booking is reasonable."
    if current >= avg_hist * 1.10:
        return "Mileage is elevated vs past levels; waiting may be better."
    return "Mileage is in the normal range."


def _is_deal_signal(signal: str) -> bool:
    return signal.startswith("Very good price") or signal.startswith("Good price")


def build_price_context(
    label: str,
    fare: FareResult,
    history_rows: list[dict[str, Any]],
    price_key: str,
) -> tuple[list[str], list[str], bool]:
    trend_lines: list[str] = []
    prediction_lines: list[str] = []
    if fare.price is None:
        trend_lines.append(f"{label}: no current mileage, unable to compare.")
        prediction_lines.append(f"{label}: no deal signal.")
        return trend_lines, prediction_lines, False

    last_price, last_date = _find_last_price(history_rows, price_key)
    hist = _collect_prices(history_rows, price_key)

    if last_price is not None:
        trend_lines.append(
            f"{label}: {_delta_text(fare.price, last_price)} (last date: {last_date})"
        )
    else:
        trend_lines.append(f"{label}: no past tracked mileage yet; baseline created today.")

    if hist:
        signal = _buy_signal(fare.price, hist)
        prediction_lines.append(
            f"{label}: historical low {min(hist):,.0f} miles; {signal}"
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


def build_concise_email(
    economy: FareResult,
    premium_economy: FareResult,
    trend_lines: list[str],
    prediction_lines: list[str],
    deal_lines: list[str],
) -> tuple[str, str]:
    def fare_line(label: str, fare: FareResult) -> str:
        if fare.price is None:
            return f"{label}: unavailable"
        trip_text = "round trip" if fare.return_date else "one way"
        dates = (
            f"{fare.depart_date} -> {fare.return_date}"
            if fare.return_date
            else str(fare.depart_date)
        )
        return (
            f"{label}: {fare.price:,.0f} miles {trip_text} "
            f"({dates})"
        )

    text_lines = [
        "UA SFO -> PVG, Sep 24-30, 2026, non-stop awards only",
        fare_line("Economy best non-stop", economy),
        fare_line("Premium economy best non-stop", premium_economy),
    ]
    if economy.url:
        text_lines.append(f"Economy link: {economy.url}")
    if premium_economy.url:
        text_lines.append(f"Premium economy link: {premium_economy.url}")
    text_lines.append("")
    text_lines.append("Deal:")
    text_lines.extend(deal_lines or ["No deal signal yet."])
    text_lines.append("Trend:")
    text_lines.extend(trend_lines or ["No trend data yet."])
    text_lines.append("Prediction:")
    text_lines.extend(prediction_lines or ["No prediction yet."])
    text_body = "\n".join(text_lines)

    html = [
        "<html><body>",
        "<p><b>UA SFO -&gt; PVG, Sep 24-30, 2026, non-stop awards only</b></p>",
        f"<p><b>{fare_line('Economy best non-stop', economy)}</b><br/>",
        (f"<a href=\"{economy.url}\">View economy award</a>" if economy.url else "No link"),
        "</p>",
        f"<p><b>{fare_line('Premium economy best non-stop', premium_economy)}</b><br/>",
        (f"<a href=\"{premium_economy.url}\">View premium economy award</a>" if premium_economy.url else "No link"),
        "</p>",
        "<p><b>Deal</b><br/>" + "<br/>".join(deal_lines or ["No deal signal yet."]) + "</p>",
        "<p><b>Trend</b><br/>" + "<br/>".join(trend_lines or ["No trend data yet."]) + "</p>",
        "<p><b>Prediction</b><br/>" + "<br/>".join(prediction_lines or ["No prediction yet."]) + "</p>",
        "</body></html>",
    ]
    return text_body, "".join(html)


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
    lines = [
        f"Date: {today}",
        "Route: SFO -> PVG",
        "Window: depart 2026-09-24..2026-09-30 (one-way non-stop mileage scan)",
        "Airline: United only | Awards: Economy, Premium economy",
        "",
    ]

    browser_headless = env_bool("UA_BROWSER_HEADLESS", False)

    history_rows = load_price_history(PRICE_HISTORY_FILE)

    try:
        class_results = find_united_browser_calendar_fares(headless=browser_headless)
    except Exception as e:
        class_results = {
            key: FareResult(None, None, None, None, None, 0, "united.com browser")
            for _, _, key in TRACKED_CLASSES
        }
        lines.append(f"United browser search failed: {e}")
        output = "\n".join(lines)
        recipients = parse_recipients(report_email_raw)
        wait_until_report_send_window()
        if recipients:
            subject = f"Flight report failed {dt.date.today().isoformat()}"
            if smtp_host and smtp_from:
                send_mail_smtp(
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
            else:
                for addr in recipients:
                    send_mail_local(subject, output, addr)
        out_file = REPORT_DIR / f"daily_{dt.date.today().isoformat()}.txt"
        out_file.write_text(output + "\n", encoding="utf-8")
        notify_macos("Flight report failed: United browser search failed")
        return 1

    economy = class_results["economy"]
    premium_economy = class_results["premium_economy"]

    lines.append(format_fare("Economy best non-stop", economy))
    lines.append(format_fare("Premium economy best non-stop", premium_economy))
    lines.append(
        "Samples checked: "
        + f"economy={economy.samples_checked}, "
        + f"premium_economy={premium_economy.samples_checked}"
    )
    lines.append("")

    lines.append("Mileage trend:")
    trend_lines: list[str] = []
    prediction_lines: list[str] = []
    deal_lines: list[str] = []
    any_deal = False
    for label, _, key in TRACKED_CLASSES:
        trend, prediction, is_deal = build_price_context(
            label,
            class_results[key],
            history_rows,
            f"{key}_miles",
        )
        trend_lines.extend(trend)
        prediction_lines.extend(prediction)
        if is_deal and class_results[key].price is not None:
            any_deal = True
            deal_lines.append(
                f"{label}: DEAL at {class_results[key].price:,.0f} miles on {class_results[key].depart_date}"
            )

    lines.extend(trend_lines)
    lines.append("")
    lines.append("Deal signal:")
    if deal_lines:
        lines.extend(deal_lines)
    else:
        lines.append("No deal detected yet.")
    lines.append("")
    lines.append("Prediction:")
    lines.extend(prediction_lines)

    subject_prefix = "Flight DEAL" if any_deal else "Flight update"
    subject = f"{subject_prefix} {dt.date.today().isoformat()}"
    recipients = parse_recipients(report_email_raw)
    mail_ok = False
    mail_detail = "disabled (REPORT_EMAIL empty)"
    email_text, email_html = build_concise_email(
        economy=economy,
        premium_economy=premium_economy,
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
        else:
            results = [send_mail_local(subject, email_text, addr) for addr in recipients]
            mail_ok = all(ok for ok, _ in results)
            if mail_ok:
                mail_detail = "local mail accepted (delivery not guaranteed)"
            else:
                first_err = next((msg for ok, msg in results if not ok), "local mail failed")
                mail_detail = first_err
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
            sms_body = "\n".join(
                [
                    f"UA SFO->PVG non-stop Sep 24-30 {dt.date.today().isoformat()}",
                    format_fare("Economy non-stop", economy),
                    format_fare("Premium economy non-stop", premium_economy),
                    "Deal: " + ("; ".join(deal_lines) if deal_lines else "none"),
                ]
            )
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

    append_price_history(
        PRICE_HISTORY_FILE,
        {
            "timestamp": dt.datetime.now().isoformat(timespec="seconds"),
            "date": dt.date.today().isoformat(),
            "route": f"{ORIGIN}-{DEST}",
            "trip_type": "one_way_award",
            "window_start": OUTBOUND_START.isoformat(),
            "window_end": OUTBOUND_END.isoformat(),
            "economy_miles": economy.price,
            "premium_economy_miles": premium_economy.price,
            "economy_depart": economy.depart_date,
            "premium_economy_depart": premium_economy.depart_date,
        },
    )
    output = "\n".join(lines)

    out_file = REPORT_DIR / f"daily_{dt.date.today().isoformat()}.txt"
    out_file.write_text(output + "\n", encoding="utf-8")

    summary = []
    if economy.price is not None:
        summary.append(f"Eco {economy.price:,.0f} mi")
    if premium_economy.price is not None:
        summary.append(f"PremEco {premium_economy.price:,.0f} mi")
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
