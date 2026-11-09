"""Browser inspection of the synthetic local entry demo; no production accounts."""

from pathlib import Path

import httpx
from playwright.sync_api import sync_playwright

URL = "http://127.0.0.1:8768"
assert httpx.get(URL + "/__candidate", trust_env=False).json() == {
    "candidate": "pick-test1-unification",
    "synthetic": True,
}
output = Path("docs/plans/evidence/browser")
output.mkdir(parents=True, exist_ok=True)
with sync_playwright() as playwright:
    browser = playwright.chromium.launch(headless=True)
    try:
        page = browser.new_page(viewport={"width": 1440, "height": 1000})
        page.on("pageerror", lambda error: print("PAGE_ERROR", str(error).split("?")[0]))
        failures = []
        page.on(
            "response",
            lambda response: (
                failures.append({"path": response.url.split("?", 1)[0], "status": response.status})
                if response.status >= 400
                else None
            ),
        )
        page.goto(URL)
        page.wait_for_load_state("networkidle")
        page.screenshot(path=str(output / "00-login.png"), full_page=True)
        print("SLIDER", page.locator('[role="slider"]').count())
        print("BUTTONS", page.get_by_role("button").all_text_contents())
        print(
            "INTERACTIVE",
            page.locator("[role],button").evaluate_all(
                '(nodes) => nodes.map(n => ({role:n.getAttribute("role"),tag:n.tagName,text:n.innerText,cls:n.className})).slice(-30)'
            ),
        )
        print(page.locator("body").inner_text())
        print(
            "INPUTS",
            page.locator("input").evaluate_all(
                "(nodes) => nodes.map(n => ({type:n.type,placeholder:n.placeholder,id:n.id}))"
            ),
        )
        page.get_by_placeholder("Username or email").fill("customer/demo")
        page.get_by_placeholder("Password", exact=True).fill("TestPass12")
        slider = page.get_by_role("slider").bounding_box()
        assert slider is not None
        page.mouse.move(slider["x"] + 20, slider["y"] + slider["height"] / 2)
        page.mouse.down()
        page.mouse.move(
            slider["x"] + slider["width"] - 4, slider["y"] + slider["height"] / 2, steps=20
        )
        page.mouse.up()
        page.get_by_role("button", name="Sign in", exact=True).click()
        page.wait_for_url(lambda url: "/login" not in url, timeout=15000)
        page.wait_for_load_state("networkidle")
        page.get_by_text("选剧专家（候选）", exact=True).first.wait_for(timeout=15000)
        page.wait_for_load_state("networkidle")
        page.screenshot(path=str(output / "01-customer.png"), full_page=True)
        print("AFTER_LOGIN", page.url, page.locator("body").inner_text())
        print("AFTER_BUTTONS", page.get_by_role("button").all_text_contents())
        print(
            "EDITABLE",
            page.locator('textarea,[contenteditable="true"]').evaluate_all(
                '(nodes) => nodes.map(n => ({tag:n.tagName,role:n.getAttribute("role"),label:n.getAttribute("aria-label"),placeholder:n.getAttribute("placeholder"),cls:n.className}))'
            ),
        )
        page.get_by_role("button", name="工作台", exact=True).click()
        page.wait_for_load_state("networkidle")
        page.get_by_text("候选演示剧", exact=True).wait_for(timeout=15000)
        page.screenshot(path=str(output / "02-workbench.png"), full_page=True)
        print("WORKBENCH", page.url, page.locator("body").inner_text())
        page.get_by_role("button", name="对话", exact=True).click()
        editor = page.get_by_placeholder("和 AI 小助手说点什么吧～")
        editor.wait_for()
        editor.fill("帮我查看演示剧，验证统一入口。")
        editor.press("Enter")
        page.get_by_text(
            "候选环境回复：这是合成数据验证，没有调用真实模型。", exact=True
        ).first.wait_for(timeout=20000)
        page.wait_for_load_state("networkidle")
        page.screenshot(path=str(output / "03-chat.png"), full_page=True)
        print("CHAT_REPLY_VERIFIED", True)
        with page.expect_response(
            lambda response: response.request.method == "POST" and "/upload" in response.url,
            timeout=15000,
        ) as uploaded:
            page.locator('input[type="file"]').first.set_input_files(
                {
                    "name": "candidate-note.txt",
                    "mimeType": "text/plain",
                    "buffer": b"synthetic attachment only",
                }
            )
        receipt = uploaded.value
        assert receipt.status in (200, 201), receipt.text()
        print("UPLOAD_RECEIPT", {"status": receipt.status, "fields": list(receipt.json())})
        download = receipt.json()["access_url"]
        assert download.startswith("/api/agents/")
        loaded = page.evaluate(
            """async (path) => {
            const token = localStorage.getItem('auth_token') || sessionStorage.getItem('auth_token');
            const response = await fetch(path, {headers:{Authorization:'Bearer '+token}});
            return {status:response.status,body:await response.text()};
        }""",
            download,
        )
        assert loaded == {"status": 200, "body": "synthetic attachment only"}, loaded
        print("UPLOAD_READBACK_VERIFIED", True)

        page.get_by_text("candidate-note.txt", exact=True).first.wait_for()
        page.screenshot(path=str(output / "04-attachment.png"), full_page=True)

        print(
            "FILE_INPUTS",
            page.locator('input[type="file"]').evaluate_all(
                "(nodes) => nodes.map(n => ({accept:n.accept,multiple:n.multiple,id:n.id}))"
            ),
        )

        print("FAILED_HTTP", failures)
    except Exception:
        page.screenshot(path=str(output / "failure.png"), full_page=True)
        print("FAILURE_BODY", page.locator("body").inner_text())
        print("FAILED_HTTP", failures)
        raise
    finally:
        browser.close()
