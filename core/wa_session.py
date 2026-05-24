"""
core/wa_session.py  (v1.3 — seletores múltiplos + screenshot debug)
"""

import asyncio
import base64
import logging
import shutil
from pathlib import Path
from typing import Callable, Optional

from playwright.async_api import (
    async_playwright,
    BrowserContext,
    Page,
    Error as PlaywrightError,
)

logger = logging.getLogger(__name__)

SESSION_DIR  = Path("data/session")
DEBUG_DIR    = Path("data/debug")
SESSION_DIR.mkdir(parents=True, exist_ok=True)
DEBUG_DIR.mkdir(parents=True, exist_ok=True)

LAUNCH_TIMEOUT_S = 30
NAV_TIMEOUT_S    = 30
AUTH_TIMEOUT_S   = 120

# WhatsApp Web muda esses seletores com frequência — tentamos todos em ordem
QR_SELECTORS = [
    'canvas[aria-label="Scan this QR code to link a device"]',   # versão antiga
    'canvas[aria-label="Scan me!"]',                              # versão alternativa
    'div[data-ref] canvas',                                       # pelo container do QR
    'canvas',                                                     # qualquer canvas (fallback)
]

CONNECTED_SELECTORS = [
    'div[data-testid="default-user"]',
    'div[data-testid="conversation-panel-wrapper"]',
    'div[aria-label="Chat list"]',
    '#side',                                                      # sidebar principal do WA
]


class WASession:
    """
    Estados: idle · loading · qr_ready · connecting · connected · error · closed
    """

    WA_URL = "https://web.whatsapp.com"

    def __init__(self):
        self.state       : str           = "idle"
        self.qr_base64   : Optional[str] = None
        self.phone_number: Optional[str] = None

        self._on_state_change: Optional[Callable] = None
        self._on_qr          : Optional[Callable] = None

        self._playwright = None
        self._context   : Optional[BrowserContext] = None
        self._page      : Optional[Page]           = None
        self._task      : Optional[asyncio.Task]   = None

    def on_state_change(self, fn: Callable):
        self._on_state_change = fn

    def on_qr(self, fn: Callable):
        self._on_qr = fn

    # ------------------------------------------------------------------ #
    # API pública                                                          #
    # ------------------------------------------------------------------ #

    async def start(self):
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await asyncio.wait_for(asyncio.shield(self._task), timeout=8)
            except Exception:
                pass

        self._reset_handles()
        self.qr_base64    = None
        self.phone_number = None
        self._task = asyncio.create_task(self._run(), name="wa_session_run")

    async def stop(self):
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await asyncio.wait_for(asyncio.shield(self._task), timeout=8)
            except Exception:
                pass
        await self._cleanup()
        await self._set_state("closed")

    async def clear_session(self):
        if SESSION_DIR.exists():
            shutil.rmtree(SESSION_DIR, ignore_errors=True)
        SESSION_DIR.mkdir(parents=True, exist_ok=True)
        logger.info("Sessão local apagada.")

    # ------------------------------------------------------------------ #
    # Loop principal                                                       #
    # ------------------------------------------------------------------ #

    async def _run(self):
        try:
            await self._set_state("loading")

            try:
                await asyncio.wait_for(self._launch_browser(), timeout=LAUNCH_TIMEOUT_S)
            except asyncio.TimeoutError:
                logger.error(f"Timeout abrindo browser ({LAUNCH_TIMEOUT_S}s).")
                await self._set_state("error")
                return

            try:
                await asyncio.wait_for(self._navigate(), timeout=NAV_TIMEOUT_S)
            except asyncio.TimeoutError:
                logger.error(f"Timeout carregando WhatsApp Web ({NAV_TIMEOUT_S}s).")
                await self._set_state("error")
                return

            await self._wait_for_auth()

        except asyncio.CancelledError:
            logger.info("Sessão cancelada.")
        except Exception as e:
            logger.exception(f"Erro inesperado: {e}")
            if self.state != "closed":
                await self._set_state("error")
        finally:
            await self._cleanup()

    async def _launch_browser(self):
        self._playwright = await async_playwright().start()
        logger.info("Playwright iniciado. Abrindo Chromium...")
        self._context = await self._playwright.chromium.launch_persistent_context(
            user_data_dir=str(SESSION_DIR),
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--disable-extensions",
                "--no-first-run",
                "--no-default-browser-check",
            ],
            viewport={"width": 1280, "height": 900},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            timeout=LAUNCH_TIMEOUT_S * 1000,
        )
        pages = self._context.pages
        self._page = pages[0] if pages else await self._context.new_page()
        logger.info("Chromium aberto.")

    async def _navigate(self):
        await self._page.goto(
            self.WA_URL,
            wait_until="domcontentloaded",
            timeout=NAV_TIMEOUT_S * 1000,
        )
        logger.info("WhatsApp Web carregado.")

    async def _wait_for_auth(self):
        # Verifica sessão salva
        logger.info("Verificando sessão salva...")
        connected = await self._find_element(CONNECTED_SELECTORS, timeout=8_000)
        if connected:
            logger.info("Sessão salva detectada!")
            await self._on_connected()
            return

        logger.info("Sem sessão salva. Aguardando QR Code...")
        await self._set_state("loading")

        deadline    = asyncio.get_event_loop().time() + AUTH_TIMEOUT_S
        qr_count    = 0
        debug_shots = 0

        while asyncio.get_event_loop().time() < deadline:
            remaining = int(deadline - asyncio.get_event_loop().time())

            # ── Tenta capturar QR ──────────────────────────────────────
            qr, selector_used = await self._capture_qr()

            if qr:
                if qr != self.qr_base64:
                    qr_count += 1
                    self.qr_base64 = qr
                    logger.info(f"QR #{qr_count} capturado via '{selector_used}'. [{remaining}s]")
                    await self._set_state("qr_ready")
                    if self._on_qr:
                        await self._on_qr(qr)
            else:
                # Tira screenshot de debug a cada 10s para inspecionar a página
                if debug_shots < 3 and remaining % 10 < 2:
                    await self._save_debug_screenshot(f"waiting_{debug_shots}.png")
                    await self._log_page_info()
                    debug_shots += 1
                logger.debug(f"QR não encontrado ainda. [{remaining}s restantes]")

            # ── Verifica autenticação ──────────────────────────────────
            connected = await self._find_element(CONNECTED_SELECTORS, timeout=2_000)
            if connected:
                logger.info("Login detectado!")
                await self._set_state("connecting")
                await self._on_connected()
                return

            await asyncio.sleep(2)

        logger.error("Timeout: QR não escaneado a tempo.")
        await self._save_debug_screenshot("timeout.png")
        await self._set_state("error")

    # ------------------------------------------------------------------ #
    # Captura do QR — tenta cada seletor em ordem                         #
    # ------------------------------------------------------------------ #

    async def _capture_qr(self):
        """Retorna (base64_str, selector_usado) ou (None, None)."""
        for sel in QR_SELECTORS:
            try:
                el = await self._page.query_selector(sel)
                if not el:
                    continue
                # Verifica se o elemento tem tamanho razoável (não é um canvas oculto)
                box = await el.bounding_box()
                if not box or box["width"] < 50 or box["height"] < 50:
                    continue
                data = await el.screenshot(type="png")
                b64  = "data:image/png;base64," + base64.b64encode(data).decode()
                return b64, sel
            except PlaywrightError as e:
                if "closed" in str(e).lower():
                    raise asyncio.CancelledError("Página fechada")
                continue
            except Exception:
                continue
        return None, None

    # ------------------------------------------------------------------ #
    # Helpers de diagnóstico                                              #
    # ------------------------------------------------------------------ #

    async def _find_element(self, selectors: list, timeout: int = 3000):
        """Tenta cada seletor e retorna o primeiro encontrado."""
        for sel in selectors:
            try:
                el = await self._page.wait_for_selector(sel, timeout=timeout // len(selectors))
                if el:
                    return el
            except Exception:
                continue
        return None

    async def _save_debug_screenshot(self, filename: str):
        """Salva screenshot da página inteira em data/debug/."""
        try:
            path = DEBUG_DIR / filename
            await self._page.screenshot(path=str(path), full_page=True)
            logger.info(f"Screenshot de debug salvo: {path}")
        except Exception as e:
            logger.warning(f"Não foi possível salvar screenshot: {e}")

    async def _log_page_info(self):
        """Loga título da página e todos os canvas/elementos relevantes encontrados."""
        try:
            title = await self._page.title()
            url   = self._page.url
            logger.info(f"[DEBUG] URL: {url} | Title: '{title}'")

            # Lista todos os canvas na página
            canvases = await self._page.query_selector_all("canvas")
            logger.info(f"[DEBUG] Canvas encontrados na página: {len(canvases)}")
            for i, c in enumerate(canvases):
                try:
                    box   = await c.bounding_box()
                    label = await c.get_attribute("aria-label") or ""
                    logger.info(f"  canvas[{i}] aria-label='{label}' box={box}")
                except Exception:
                    pass

            # Testa os seletores de "conectado"
            for sel in CONNECTED_SELECTORS:
                el = await self._page.query_selector(sel)
                logger.info(f"[DEBUG] Seletor '{sel}': {'ENCONTRADO' if el else 'não encontrado'}")

        except Exception as e:
            logger.warning(f"Erro ao logar info da página: {e}")

    # ------------------------------------------------------------------ #
    # Auth + estado                                                        #
    # ------------------------------------------------------------------ #

    async def _on_connected(self):
        try:
            for sel in CONNECTED_SELECTORS:
                el = await self._page.query_selector(sel)
                if el:
                    self.phone_number = (await el.get_attribute("title") or "").strip()
                    break
        except Exception:
            pass
        logger.info(f"Autenticado! Conta: {self.phone_number or 'desconhecida'}")
        await self._set_state("connected")

    def _reset_handles(self):
        self._playwright = None
        self._context    = None
        self._page       = None

    async def _set_state(self, state: str):
        self.state = state
        logger.info(f"[WASession] estado → {state}")
        if self._on_state_change:
            try:
                await self._on_state_change(state)
            except Exception as e:
                logger.warning(f"Callback falhou: {e}")

    async def _cleanup(self):
        ctx, pw = self._context, self._playwright
        self._reset_handles()
        if ctx:
            try:
                await ctx.close()
            except Exception:
                pass
        if pw:
            try:
                await pw.stop()
            except Exception:
                pass
        logger.info("Cleanup concluído.")
