import os
import sys

sys.path.insert(0, os.path.abspath("."))
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright

load_dotenv()
from apps.api.auth import create_access_token

SCREENSHOT_DIR = r"C:\Users\HASNI\.gemini\antigravity\brain\47be30ce-1860-4ecc-8796-323fab9f7605\screenshots\render_check"
os.makedirs(SCREENSHOT_DIR, exist_ok=True)

routes = [
    ("/", "home"),
    ("/dashboard", "dashboard"),
    ("/dispatches", "dispatches"),
    ("/performance", "performance"),
    ("/platforms", "platforms"),
    ("/violations", "violations"),
    ("/remediation", "remediation"),
    ("/alerts", "alerts"),
]

def main():
    token = create_access_token(user_id="demo_buyer", role="buyer")
    print(f"Generated test token for demo_buyer: {token[:16]}...")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(viewport={"width": 1280, "height": 800})
        # Inject auth token into localStorage on all pages
        context.add_init_script(f"""
            window.localStorage.setItem("launch_gate_token", "{token}");
        """)

        results = []

        for route, name in routes:
            page = context.new_page()
            console_errors = []
            page_errors = []

            page.on("console", lambda msg: console_errors.append(msg.text) if msg.type in ("error", "warning") else None)
            page.on("pageerror", lambda err: page_errors.append(str(err)))

            url = f"http://localhost:3000{route}"
            print(f"\n==================================================")
            print(f"Checking {url} ({name})")
            print(f"==================================================")
            try:
                page.goto(url, wait_until="load", timeout=30000)
                page.wait_for_timeout(3000) # allow background fetches to complete

                screenshot_path = os.path.join(SCREENSHOT_DIR, f"{name}.png")
                page.screenshot(path=screenshot_path, full_page=True)
                print(f"Screenshot saved: {screenshot_path}")

                body_text = page.inner_text("body")
                has_crash = "Unhandled Runtime Error" in body_text or "Application error" in body_text
                
                # Check for fake metrics
                fake_strings = ["4.6x", "18,450", "18450", "LIVE_ACTIVE"]
                found_fakes = [f for f in fake_strings if f in body_text]

                status_str = "CRASHED" if has_crash else ("FAKE_DATA" if found_fakes else "OK")
                print(f"Status: {status_str}")
                print(f"Page errors: {page_errors}")
                print(f"Console errors: {console_errors[:5]}")
                if found_fakes:
                    print(f"Found fake strings: {found_fakes}")

                results.append({
                    "route": route,
                    "name": name,
                    "status": status_str,
                    "screenshot": screenshot_path,
                    "page_errors": page_errors,
                    "console_errors": console_errors,
                    "fakes": found_fakes,
                })
            except Exception as e:
                print(f"Failed navigating to {url}: {e}")
                results.append({
                    "route": route,
                    "name": name,
                    "status": "TIMEOUT_OR_FAILED",
                    "error": str(e),
                })
            finally:
                page.close()

        browser.close()

        print("\n\n================ SUMMARY REPORT ================")
        for r in results:
            print(f"Route: {r['route']} | Status: {r['status']} | Screenshot: {r.get('screenshot', 'None')}")

if __name__ == "__main__":
    main()
