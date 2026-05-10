#!/usr/bin/env python3
"""Open/sign in to the persistent United browser profile."""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE_DIR = Path(__file__).resolve().parents[1]
PROFILE_DIR = BASE_DIR / "browser_profiles" / "united_playwright"


def load_env_file() -> None:
    path = BASE_DIR / ".env"
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        key, value = s.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


def keychain_password(account: str) -> str | None:
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


def body_text(page) -> str:
    try:
        return page.locator("body").inner_text(timeout=8000)
    except Exception:
        return ""


def fill_login_if_present(page, account: str, password: str) -> None:
    text = body_text(page)
    if "Email or MileagePlus" in text:
        page.locator("#MPIDEmailField").fill(account)
        page.keyboard.press("Enter")
        page.wait_for_timeout(7000)
    if page.locator('input[type="password"]').count() > 0:
        page.locator('input[type="password"]').first.fill(password)
        page.evaluate(
            """
            () => {
              const buttons = Array.from(document.querySelectorAll('button'));
              const signIn = buttons.find(
                b => b.innerText.trim().toLowerCase() === 'sign in'
              ) || buttons.filter(
                b => b.innerText.toLowerCase().includes('sign in')
              ).at(-1);
              signIn?.click();
            }
            """
        )
        page.wait_for_timeout(12000)


def main() -> int:
    load_env_file()
    account = os.getenv("UA_ACCOUNT", "").strip()
    password = keychain_password(account) if account else None

    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE_DIR),
            headless=False,
            viewport={"width": 1440, "height": 1100},
            args=["--disable-blink-features=AutomationControlled"],
        )
        page = context.pages[0] if context.pages else context.new_page()
        page.goto(
            "https://www.united.com/en/us/fsr/choose-flights"
            "?f=SFO&t=PVG&d=2026-09-28&tt=1&sc=7&px=1&taxng=1"
            "&newHP=True&clm=7&st=bestmatches&at=1&tqp=A",
            wait_until="domcontentloaded",
            timeout=60000,
        )
        page.wait_for_timeout(15000)
        if account and password:
            fill_login_if_present(page, account, password)
        print("United browser profile is open.")
        text = body_text(page)
        if re.search(r"verification code|Enter code|verify", text, re.I):
            print("United needs the text-message verification code.")
            print("Enter the code in the browser window, check 'Remember this browser', then continue.")
        elif account and password:
            print("If United still shows a sign-in prompt, finish it in the browser window.")
        else:
            print("Sign in to United in that browser window.")
        print("When the browser shows mileage results or your signed-in account, press Enter here.")
        input()
        context.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
