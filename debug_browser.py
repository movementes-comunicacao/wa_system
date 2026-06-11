"""
debug_browser.py
Roda isolado para testar se o Playwright abre o browser corretamente.
Execute: python debug_browser.py

Se travar aqui → problema no Playwright/Chromium, não no código do servidor.
"""

import asyncio
import time
from playwright.async_api import async_playwright

async def main():
    print("[1] Iniciando Playwright...")
    t0 = time.time()

    async with async_playwright() as p:
        print(f"[2] Playwright OK ({time.time()-t0:.1f}s). Abrindo Chromium...")
        t1 = time.time()

        # Testa primeiro SEM persistent context (mais simples)
        browser = await p.chromium.launch(
            headless=False,
            args=["--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage"],
            timeout=20_000,
        )
        print(f"[3] Browser aberto ({time.time()-t1:.1f}s). Abrindo página...")
        t2 = time.time()

        page = await browser.new_page()
        await page.goto("https://web.whatsapp.com", wait_until="domcontentloaded", timeout=20_000)
        print(f"[4] Página carregada ({time.time()-t2:.1f}s).")
        print(f"    Title: {await page.title()}")

        print("[5] Aguardando 5s para ver se QR aparece...")
        await asyncio.sleep(5)

        canvas = await page.query_selector('canvas[aria-label="Scan this QR code to link a device"]')
        print(f"[6] QR Canvas encontrado: {canvas is not None}")

        await browser.close()
        print("[7] Browser fechado. Tudo OK!")
        print(f"\nTempo total: {time.time()-t0:.1f}s")

asyncio.run(main())
