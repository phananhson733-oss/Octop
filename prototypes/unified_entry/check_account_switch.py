"""Verify cross-tab identity switch reloads state without deleting preferences."""

import httpx
from playwright.sync_api import sync_playwright

URL = "http://127.0.0.1:8768"
assert httpx.get(URL + "/__candidate", trust_env=False).json()["synthetic"] is True
with sync_playwright() as playwright:
    browser = playwright.chromium.launch(headless=True)
    try:
        context = browser.new_context(viewport={"width": 1440, "height": 1000})
        first = context.new_page()
        first.goto(URL)
        first.wait_for_load_state("networkidle")
        first.get_by_placeholder("Username or email").fill("customer/demo")
        first.get_by_placeholder("Password", exact=True).fill("TestPass12")
        box = first.get_by_role("slider").bounding_box()
        first.mouse.move(box["x"] + 20, box["y"] + box["height"] / 2)
        first.mouse.down()
        first.mouse.move(box["x"] + box["width"] - 4, box["y"] + box["height"] / 2, steps=20)
        first.mouse.up()
        first.get_by_role("button", name="Sign in", exact=True).click()
        first.get_by_text("选剧专家（候选）", exact=True).first.wait_for()
        before = first.evaluate("performance.timeOrigin")
        first.evaluate("localStorage.setItem('candidate-custom-pref','keep')")
        second = context.new_page()
        second.goto(URL)
        second.wait_for_load_state("networkidle")
        # Use a genuine password login, then the same storage write as the login page.
        identity = second.evaluate("""async () => {
            const r = await fetch('/api/auth/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({username:'internal/demo',password:'TestPass12'})});
            const result = await r.json();
            if(!r.ok) throw new Error('candidate login failed');
            localStorage.setItem('auth_token',result.access_token);
            return result.identity_id;
        }""")
        assert identity == "demo-internal"
        second.goto(URL + "/chat")
        first.get_by_text("内部专家（候选）", exact=True).first.wait_for(timeout=15000)
        first.wait_for_load_state("networkidle")
        assert first.evaluate("performance.timeOrigin") > before
        assert first.get_by_text("选剧专家（候选）", exact=True).count() == 0
        assert first.evaluate("localStorage.getItem('candidate-custom-pref')") == "keep"
        current_identity = first.evaluate("""async () => {
            const r = await fetch('/api/auth/me',{headers:{Authorization:'Bearer '+localStorage.getItem('auth_token')}});
            return (await r.json()).identity_id;
        }""")
        assert current_identity == "demo-internal"
        print(
            {
                "old_customer_ui": False,
                "request_identity": current_identity,
                "full_reload": True,
                "custom_preference_preserved": True,
            }
        )
        first.screenshot(
            path="docs/plans/evidence/browser/account-switch-after.png", full_page=True
        )
    finally:
        browser.close()
