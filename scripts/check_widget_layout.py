"""Browser-only layout check; Telegram QuickJS/device acceptance stays separate."""

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from playwright.async_api import async_playwright
from app.widgets import Widgets
from app.widget_template import WIDGET_HTML


async def main():
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        w = Widgets(
            Path(tmp) / "db", SimpleNamespace(get_user_accounts=lambda *args: [])
        )
        players = [
            {
                "tag": f"#P{i}",
                "name": "ОченьДлинныйИгрок👑" + str(i),
                "townHallLevel": 18,
                "selectionScore": 123.4,
                "donations": 123456,
            }
            for i in range(45)
        ]
        w.fetch = AsyncMock(
            return_value={
                "memberList": players,
                "clanRanking": players,
                "standings": [
                    {
                        "name": "ОченьДлинныйКлан👑" + str(i),
                        "place": i + 1,
                        "stars": 270,
                    }
                    for i in range(8)
                ],
            }
        )
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            try:
                for kind in ["players", "rating", "donations", "cwl"]:
                    raw, _ = await w.data({"s": kind, "p": 1}, 7, "a" * 12, 42)
                    for theme in ["light", "dark"]:
                        for width in [320, 390]:
                            page = await browser.new_page(
                                viewport={"width": width, "height": 650}
                            )
                            errors = []
                            page.on("pageerror", lambda e: errors.append(str(e)))
                            palette = (
                                ("#17212b", "#eff4fa", "#253745", "#93a7bc")
                                if theme == "dark"
                                else ("#ffffff", "#17212b", "#edf3f7", "#60778b")
                            )
                            css = (
                                "<style>:root{--tg-theme-bg-color:%s;--tg-theme-text-color:%s;--tg-theme-secondary-bg-color:%s;--tg-theme-hint-color:%s;--tg-theme-button-color:#37a08a;--tg-theme-section-separator-color:#bccbd5}body{background:var(--tg-theme-bg-color)}button{border:0;background:var(--tg-theme-secondary-bg-color);color:var(--tg-theme-text-color);padding:6px;box-sizing:border-box}h2{line-height:24px}</style>"
                                % palette
                            )
                            shim = (
                                "<script>let sent=[];const TelegramWidget={chatData:"
                                + json.dumps(raw)
                                + ",onEvent(){},ready(){},request(s){sent.push(s);return Promise.resolve({result:null})}};</script>"
                            )
                            await page.set_content(
                                '<!doctype html><html lang="ru"><head><meta charset="utf-8">'
                                + css
                                + "</head><body>"
                                + shim
                                + WIDGET_HTML
                                + "</body></html>"
                            )
                            count = await page.locator(".listrow").count()
                            assert count == (8 if kind == "cwl" else 10)
                            height = await page.locator("body").evaluate(
                                "e=>e.getBoundingClientRect().height"
                            )
                            assert height <= 600, (kind, theme, width, height)
                            assert await page.evaluate(
                                "document.documentElement.scrollWidth <= innerWidth"
                            )
                            if kind != "cwl":
                                await page.locator(".listrow").first.click()
                                assert await page.evaluate("sent.length") == 1
                                assert (
                                    len((await page.evaluate("sent[0]")).encode()) <= 64
                                )
                            assert not errors, errors
                            out = Path("/tmp/widget-layout")
                            out.mkdir(exist_ok=True)
                            await page.screenshot(
                                path=str(out / f"{kind}-{theme}-{width}.png")
                            )
                            print(
                                f"{kind} {theme} {width}px: {count} rows, {height}px height, {len(raw.encode())} bytes"
                            )
                            await page.close()
            finally:
                await browser.close()
        await w.close()


asyncio.run(main())
