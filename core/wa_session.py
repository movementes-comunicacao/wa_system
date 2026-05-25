"""
core/wa_session.py

Gerencia o ciclo de vida completo da sessão WhatsApp Web via Playwright:
  - Abertura do Chromium com perfil persistente (não precisa escanear todo dia)
  - Captura e transmissão do QR Code em base64
  - Detecção de autenticação bem-sucedida
  - Exportação de contatos e grupos
  - Disparo de mensagens via CSV com substituição de variáveis dinâmicas
"""

import asyncio
import base64
import csv
import io
import json
import logging
import re
import time
from pathlib import Path
from typing import Callable, Optional, List, Dict, Any

from playwright.async_api import async_playwright, BrowserContext, Page, Playwright

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Caminhos de dados
# ---------------------------------------------------------------------------

BASE_DIR = Path(__file__).parent.parent
SESSION_DIR = BASE_DIR / "data" / "session"
EXPORTS_DIR = BASE_DIR / "data" / "exports"
LOGS_DIR = BASE_DIR / "data" / "logs"

for _d in (SESSION_DIR, EXPORTS_DIR, LOGS_DIR):
    _d.mkdir(parents=True, exist_ok=True)

# ---------------------------------------------------------------------------
# Seletores WhatsApp Web (podem precisar de ajuste conforme versão do WA)
# ---------------------------------------------------------------------------

SEL_QR_CANVAS   = 'canvas[aria-label="Scan this QR code to link a device"]'
SEL_QR_CANVAS_2 = 'canvas[aria-label="Scan me!"]'          # fallback alternativo
SEL_AUTHENTICATED = '[data-testid="chatlist-header"]'        # header da lista de chats
SEL_SEARCH_BOX  = '[data-testid="chat-list-search-container"]'
SEL_CONTACT_ROW = '[data-testid="cell-frame-container"]'
SEL_MSG_INPUT   = '[data-testid="conversation-compose-box-input"]'
SEL_SEND_BTN    = '[data-testid="send"]'

WA_URL = "https://web.whatsapp.com"
QR_POLL_INTERVAL   = 3    # segundos entre capturas do QR
AUTH_CHECK_INTERVAL = 2   # segundos entre verificações de autenticação
QR_TIMEOUT         = 120  # segundos máximos aguardando o scan

# ---------------------------------------------------------------------------
# WASession
# ---------------------------------------------------------------------------

class WASession:
    """
    Gerencia uma sessão Playwright conectada ao WhatsApp Web.

    Estados possíveis:
        idle       → sessão não iniciada
        starting   → abrindo Chromium / carregando WA
        qr_ready   → QR exibido, aguardando scan
        connecting → QR escaneado, finalizando autenticação
        connected  → autenticado e pronto
        error      → falha (mensagem em self.error_msg)
        stopped    → sessão encerrada manualmente
    """

    def __init__(self):
        self.state: str = "idle"
        self.qr_base64: Optional[str] = None
        self.phone_number: Optional[str] = None
        self.error_msg: Optional[str] = None

        self._playwright: Optional[Playwright] = None
        self._context: Optional[BrowserContext] = None
        self._page: Optional[Page] = None
        self._task: Optional[asyncio.Task] = None

        self._on_state_change: Optional[Callable] = None
        self._on_qr: Optional[Callable] = None

    # ------------------------------------------------------------------
    # Registro de callbacks
    # ------------------------------------------------------------------

    def on_state_change(self, cb: Callable):
        self._on_state_change = cb

    def on_qr(self, cb: Callable):
        self._on_qr = cb

    async def _emit_state(self, state: str):
        self.state = state
        logger.info(f"[WASession] state → {state}")
        if self._on_state_change:
            await self._on_state_change(state)

    async def _emit_qr(self, qr_b64: str):
        self.qr_base64 = qr_b64
        if self._on_qr:
            await self._on_qr(qr_b64)

    # ------------------------------------------------------------------
    # Ciclo de vida da sessão
    # ------------------------------------------------------------------

    async def start(self):
        """Inicia a sessão em background (cancela anterior se existir)."""
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

        self._task = asyncio.create_task(self._run())

    async def stop(self):
        """Para a sessão e fecha o browser."""
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        await self._close_browser()
        await self._emit_state("stopped")

    async def clear_session(self):
        """Remove o perfil Chromium persistido (força novo QR na próxima vez)."""
        import shutil
        if SESSION_DIR.exists():
            shutil.rmtree(SESSION_DIR)
            SESSION_DIR.mkdir(parents=True, exist_ok=True)
            logger.info("[WASession] Perfil de sessão apagado.")

    # ------------------------------------------------------------------
    # Loop principal
    # ------------------------------------------------------------------

    async def _run(self):
        try:
            await self._emit_state("starting")
            await self._open_browser()
            await self._load_whatsapp()
            await self._auth_loop()
        except asyncio.CancelledError:
            logger.info("[WASession] Task cancelada.")
            raise
        except Exception as e:
            self.error_msg = str(e)
            logger.exception(f"[WASession] Erro fatal: {e}")
            await self._emit_state("error")
        finally:
            # Não fecha o browser aqui — mantém aberto para comandos
            pass

    async def _open_browser(self):
        logger.info("[WASession] Abrindo Playwright + Chromium persistente...")
        self._playwright = await async_playwright().start()

        self._context = await self._playwright.chromium.launch_persistent_context(
            str(SESSION_DIR),
            headless=False,          # Windows: headless=True quebra WebGL/canvas do WA
            args=[
                "--no-sandbox",
                "--disable-blink-features=AutomationControlled",
                "--window-position=-32000,-32000",  # janela fora da tela (invisível)
                "--window-size=1280,800",
                "--disable-extensions",
                "--mute-audio",
            ],
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 800},
            timeout=30_000,
        )
        logger.info("[WASession] Chromium aberto.")

    async def _load_whatsapp(self):
        pages = self._context.pages
        self._page = pages[0] if pages else await self._context.new_page()
        logger.info(f"[WASession] Navegando para {WA_URL}...")
        await self._page.goto(WA_URL, wait_until="domcontentloaded", timeout=30_000)
        # Aguarda o React/WA terminar de montar a UI (QR ou chat)
        logger.info("[WASession] Aguardando UI do WhatsApp Web renderizar...")
        try:
            await self._page.wait_for_function(
                """() => {
                    const qr = document.querySelector('canvas[aria-label]');
                    const chat = document.querySelector('[data-testid=\"chatlist-header\"]');
                    const landing = document.querySelector('[data-testid=\"intro-title\"]');
                    const logoutBtn = [...document.querySelectorAll('button,div[role=\"button\"]')]
                        .find(b => b.innerText && (b.innerText.includes('Desconectar') || b.innerText.includes('Log out')));
                    return !!(qr || chat || landing || logoutBtn);
                }""",
                timeout=60_000,
            )
            logger.info("[WASession] WhatsApp Web carregado e UI pronta.")
            # Verifica e clica em Desconectar imediatamente se aparecer
            await self._force_logout_if_stuck()
        except Exception:
            # Diagnostica o que o browser realmente carregou
            try:
                url = self._page.url
                title = await self._page.title()
                html = await self._page.content()
                logger.warning(f"[WASession] URL atual: {url}")
                logger.warning(f"[WASession] Título da página: {title}")
                logger.warning(f"[WASession] HTML parcial (primeiros 800 chars): {html[:800]}")
                # Salva screenshot
                ss = await self._page.screenshot()
                ss_path = LOGS_DIR / "debug_load.png"
                ss_path.write_bytes(ss)
                logger.warning(f"[WASession] Screenshot salvo em {ss_path}")
            except Exception as dbg_e:
                logger.warning(f"[WASession] Erro no diagnóstico: {dbg_e}")
            logger.warning("[WASession] Continuando mesmo sem UI detectada...")

    async def _force_logout_if_stuck(self) -> bool:
        """
        Detecta tela de loading travada ("Carregando suas conversas") e
        clica em Desconectar para forçar o aparecimento do QR Code.
        Retorna True se desconectou.
        """
        try:
            # Lista todos os botões visíveis para diagnóstico
            btns_info = await self._page.evaluate("""() => {
                const all = [...document.querySelectorAll('button, div[role="button"], span[role="button"]')];
                return all.map(b => b.innerText ? b.innerText.trim().substring(0, 40) : '(sem texto)').filter(t => t);
            }""")
            logger.info(f"[WASession] Botões na página: {btns_info}")

            # Tenta clicar em Desconectar / Log out
            clicked = await self._page.evaluate("""() => {
                const keywords = ['Desconectar', 'Log out', 'Logout', 'Sair'];
                const all = [...document.querySelectorAll('button, div[role="button"], span[role="button"]')];
                const b = all.find(b => b.innerText && keywords.some(k => b.innerText.includes(k)));
                if (b) { b.click(); return b.innerText.trim(); }
                return null;
            }""")

            if clicked:
                logger.info(f"[WASession] Clicou em '{clicked}'. Aguardando QR...")
                await asyncio.sleep(5)
                return True
            else:
                logger.info("[WASession] Botão Desconectar não encontrado ainda.")
                return False
        except Exception as e:
            logger.debug(f"[WASession] _force_logout_if_stuck: {e}")
            return False

    async def _auth_loop(self):
        """
        Aguarda autenticação:
          1. Se já autenticado (sessão persistida) → vai direto para connected
          2. Senão → captura QR e aguarda scan
        """
        deadline = time.time() + QR_TIMEOUT
        qr_sent = False
        attempt = 0
        stuck_checked = False

        while True:
            attempt += 1
            # ── Verificar se já está autenticado ──────────────────────
            if await self._is_authenticated():
                logger.info("[WASession] Autenticado!")
                await self._on_authenticated()
                return

            # ── Detectar sessão travada no loading e forçar logout ────
            if not stuck_checked and attempt >= 5:
                stuck_checked = True
                if await self._force_logout_if_stuck():
                    # Reseta deadline após desconectar
                    deadline = time.time() + QR_TIMEOUT
                    continue

            # ── Capturar QR ───────────────────────────────────────────
            qr_b64 = await self._capture_qr()
            if qr_b64:
                if not qr_sent:
                    await self._emit_state("qr_ready")
                    qr_sent = True
                if qr_b64 != self.qr_base64:
                    await self._emit_qr(qr_b64)
                    logger.info("[WASession] QR Code atualizado.")
            else:
                logger.info(f"[WASession] Tentativa {attempt}: QR não encontrado ainda — aguardando...")

            # ── Timeout ───────────────────────────────────────────────
            if time.time() > deadline:
                try:
                    ss = await self._page.screenshot()
                    ss_path = LOGS_DIR / "debug_timeout.png"
                    ss_path.write_bytes(ss)
                    logger.error(f"[WASession] Screenshot de debug salvo em {ss_path}")
                    logger.error(f"[WASession] URL atual: {self._page.url}")
                except Exception as dbg_e:
                    logger.error(f"[WASession] Erro ao capturar debug: {dbg_e}")
                raise TimeoutError(f"QR não escaneado em {QR_TIMEOUT}s.")

            await asyncio.sleep(QR_POLL_INTERVAL)

    async def _is_authenticated(self) -> bool:
        try:
            el = await self._page.query_selector(SEL_AUTHENTICATED)
            return el is not None
        except Exception:
            return False

    async def _capture_qr(self) -> Optional[str]:
        """Captura o canvas do QR Code e retorna como PNG base64."""
        try:
            # Loga todos os canvas na página para diagnóstico
            canvases_info = await self._page.evaluate("""() => {
                return [...document.querySelectorAll('canvas')].map(c => ({
                    aria: c.getAttribute('aria-label'),
                    w: c.width, h: c.height,
                    role: c.getAttribute('role')
                }));
            }""")
            if canvases_info:
                logger.info(f"[WASession] Canvas encontrados: {canvases_info}")

            # Tenta seletores específicos primeiro
            for sel in (SEL_QR_CANVAS, SEL_QR_CANVAS_2):
                canvas = await self._page.query_selector(sel)
                if canvas:
                    qr_b64 = await self._page.evaluate(
                        """(s) => { const c = document.querySelector(s);
                           if(!c) return null;
                           try { return c.toDataURL('image/png').split(',')[1]; } catch(e) { return null; }
                        }""", sel)
                    if qr_b64 and len(qr_b64) > 500:
                        return qr_b64

            # Fallback: pega qualquer canvas quadrado com tamanho de QR (>=200px)
            qr_b64 = await self._page.evaluate("""() => {
                const canvases = [...document.querySelectorAll('canvas')];
                // Prefere canvas com aria-label de QR
                const qrCanvas = canvases.find(c => {
                    const a = (c.getAttribute('aria-label') || '').toLowerCase();
                    return a.includes('qr') || a.includes('scan') || a.includes('code');
                }) || canvases.find(c => c.width >= 200 && c.height >= 200 && Math.abs(c.width - c.height) < 20);
                if (!qrCanvas) return null;
                try { return qrCanvas.toDataURL('image/png').split(',')[1]; } catch(e) { return null; }
            }""")
            if qr_b64 and len(qr_b64) > 500:
                logger.info("[WASession] QR capturado via fallback (canvas genérico).")
                return qr_b64

        except Exception as e:
            logger.debug(f"[WASession] _capture_qr erro: {e}")
        return None

    async def _on_authenticated(self):
        await self._emit_state("connecting")
        # Aguarda a interface carregar completamente
        try:
            await self._page.wait_for_selector(SEL_SEARCH_BOX, timeout=15_000)
        except Exception:
            pass
        # Tenta extrair o número de telefone (opcional)
        try:
            self.phone_number = await self._extract_phone()
        except Exception:
            self.phone_number = None
        await self._emit_state("connected")

    async def _extract_phone(self) -> Optional[str]:
        """Tenta extrair o número do perfil. Retorna None se não encontrar."""
        try:
            # Abre menu de perfil
            await self._page.click('[data-testid="menu"]', timeout=3000)
            await asyncio.sleep(0.8)
            await self._page.click('[data-testid="mi-profile"]', timeout=3000)
            await asyncio.sleep(1.2)
            el = await self._page.query_selector('[data-testid="profile-phone"]')
            if el:
                txt = await el.inner_text()
                return txt.strip()
        except Exception:
            pass
        return None

    # ------------------------------------------------------------------
    # Fechar browser
    # ------------------------------------------------------------------

    async def _close_browser(self):
        try:
            if self._context:
                await self._context.close()
        except Exception:
            pass
        try:
            if self._playwright:
                await self._playwright.stop()
        except Exception:
            pass
        self._context = None
        self._page = None
        self._playwright = None

    # ------------------------------------------------------------------
    # FEATURE: Exportar contatos e grupos
    # ------------------------------------------------------------------

    async def export_contacts_and_groups(
        self, filter_type: str = "all"
    ) -> Dict[str, Any]:
        """
        Exporta contatos e/ou grupos do WhatsApp Web aberto.

        Args:
            filter_type: "all" | "contacts" | "groups"

        Returns:
            {
              "contacts": [...],
              "groups":   [...],
              "total":    int,
              "filter":   str,
            }

        Cada contato: { "name": str, "phone": str, "type": "contact" }
        Cada grupo:   { "name": str, "id": str,    "type": "group"   }
        """
        if self.state != "connected" or not self._page:
            raise RuntimeError("Sessão não está conectada.")

        page = self._page
        contacts: List[Dict] = []
        groups: List[Dict] = []

        logger.info(f"[WASession] Iniciando exportação (filter={filter_type})...")

        # Diagnóstico: loga data-testid disponíveis na página
        try:
            testids = await page.evaluate("""() => {
                const els = [...document.querySelectorAll('[data-testid]')];
                return [...new Set(els.map(e => e.getAttribute('data-testid')))].slice(0, 40);
            }""")
            logger.info(f"[WASession] data-testid disponíveis: {testids}")
        except Exception:
            pass

        # Garante que a lista de chats está visível — tenta múltiplos seletores
        SEARCH_SELECTORS = [
            '[data-testid="chat-list-search-container"]',
            '[data-testid="chat-list-search"]',
            '[data-testid="search-input"]',
            'div[contenteditable="true"][data-tab="3"]',
            'div[contenteditable="true"]',
        ]
        search_found = False
        for sel in SEARCH_SELECTORS:
            try:
                await page.wait_for_selector(sel, timeout=5_000)
                logger.info(f"[WASession] Barra de busca encontrada com: {sel}")
                search_found = True
                break
            except Exception:
                continue
        if not search_found:
            raise RuntimeError("Lista de chats não encontrada. Verifique a sessão.")

        # Scroll para carregar todos os chats visíveis
        await self._scroll_chat_list(page)

        # Coleta as linhas de chat
        rows = await page.query_selector_all(SEL_CONTACT_ROW)
        logger.info(f"[WASession] {len(rows)} linhas de chat encontradas.")

        for row in rows:
            try:
                # Extrai dados via JS para evitar texto extra (unread count etc.)
                row_data = await row.evaluate("""el => {
                    // Nome: só o span do título, sem contar badges de mensagem
                    const titleEl = el.querySelector('[data-testid="cell-frame-title"] span[title]')
                                 || el.querySelector('[data-testid="cell-frame-title"] span')
                                 || el.querySelector('[data-testid="cell-frame-title"]');
                    const name = titleEl ? (titleEl.getAttribute('title') || titleEl.innerText || '').trim() : '';

                    // Subtítulo
                    const subEl = el.querySelector('[data-testid="cell-frame-secondary"]');
                    const subtitle = subEl ? subEl.innerText.trim() : '';

                    // Detecta grupo pelo ícone SVG com data-testid de grupo
                    const groupIcon = el.querySelector('[data-testid="default-group-refreshed"]')
                                   || el.querySelector('[data-testid="subgroup-identity"]');
                    const isGroup = !!groupIcon;

                    // Número de não lidos (para ignorar do nome)
                    const unreadEl = el.querySelector('[data-testid="icon-unread-count"]');
                    const unread = unreadEl ? unreadEl.innerText.trim() : '';

                    return { name, subtitle, isGroup, unread };
                }""")

                name = row_data.get("name", "").strip()
                subtitle = row_data.get("subtitle", "").strip()
                is_group = row_data.get("isGroup", False)

                # Remove prefixo de unread count do nome se vier junto
                if name:
                    name = re.sub(r'^\d+\s+mensagens?\s+não\s+lida[s]?\s*', '', name, flags=re.IGNORECASE).strip()
                    name = re.sub(r'^\d+\s*$', '', name).strip()

                if not name:
                    continue

                if is_group:
                    groups.append({
                        "name": name,
                        "phone": "",
                        "type": "group",
                    })
                else:
                    phone = self._extract_phone_from_text(subtitle) or ""
                    contacts.append({
                        "name": name,
                        "phone": phone,
                        "type": "contact",
                    })
            except Exception as e:
                logger.debug(f"[WASession] Erro ao processar linha: {e}")
                continue

        # Aplica filtro
        if filter_type == "contacts":
            groups = []
        elif filter_type == "groups":
            contacts = []

        result = {
            "contacts": contacts,
            "groups": groups,
            "total": len(contacts) + len(groups),
            "filter": filter_type,
        }

        # Persiste em arquivo
        await self._save_export(result, filter_type)
        logger.info(f"[WASession] Exportação concluída: {len(contacts)} contatos, {len(groups)} grupos.")
        return result

    async def _scroll_chat_list(self, page: Page, scrolls: int = 15):
        """Faz scroll na lista de chats para carregar mais itens."""
        try:
            # Tenta o painel correto (data-testid="chat-list" existe conforme diagnóstico)
            pane = await page.query_selector('[data-testid="chat-list"]')
            if not pane:
                # Fallback: drawer esquerdo
                pane = await page.query_selector('[data-testid="drawer-left"]')
            if not pane:
                logger.warning("[WASession] Painel de chat não encontrado para scroll.")
                return
            for _ in range(scrolls):
                await pane.evaluate("el => el.scrollBy(0, 800)")
                await asyncio.sleep(0.4)
            await asyncio.sleep(0.5)
            logger.info(f"[WASession] Scroll concluído ({scrolls} vezes).")
        except Exception as e:
            logger.debug(f"[WASession] _scroll_chat_list: {e}")

    async def _is_group_row(self, row, subtitle: str) -> bool:
        """Heurística para detectar se a linha é um grupo."""
        try:
            # Ícone de grupo
            group_icon = await row.query_selector('[data-testid="group"]')
            if group_icon:
                return True
            # Subtítulo típico de grupo: começa com nome seguido de ":"
            # e não parece número de telefone
            if subtitle and ":" in subtitle and not re.search(r'^\+?\d', subtitle):
                return True
            # Se não tem telefone no subtítulo → provavelmente grupo
            if subtitle and not self._extract_phone_from_text(subtitle) and len(subtitle) > 5:
                return True
        except Exception:
            pass
        return False

    def _extract_phone_from_text(self, text: str) -> Optional[str]:
        """Extrai número de telefone de uma string."""
        match = re.search(r'\+?\d[\d\s\-()]{7,}', text)
        return match.group(0).strip() if match else None

    def _slugify(self, name: str) -> str:
        slug = re.sub(r'[^a-z0-9]', '_', name.lower())
        return slug[:40]

    async def _save_export(self, data: Dict, filter_type: str):
        """Salva exportação em JSON e CSV."""
        ts = int(time.time())
        prefix = EXPORTS_DIR / f"export_{filter_type}_{ts}"

        # JSON
        json_path = prefix.with_suffix(".json")
        json_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

        # CSV — achata contatos + grupos numa única lista
        csv_path = prefix.with_suffix(".csv")
        rows = data["contacts"] + data["groups"]
        if rows:
            fieldnames = ["name", "phone", "type"]
            # utf-8-sig = BOM para Excel abrir sem encoding quebrado
            with csv_path.open("w", newline="", encoding="utf-8-sig") as f:
                writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
                writer.writeheader()
                writer.writerows({f: row.get(f, "") for f in fieldnames} for row in rows)

        logger.info(f"[WASession] Exportação salva em {prefix}.*")

    # ------------------------------------------------------------------
    # FEATURE: Enviar mensagens via CSV
    # ------------------------------------------------------------------

    async def send_messages_from_csv(
        self,
        csv_content: str,
        progress_cb: Optional[Callable] = None,
    ) -> Dict[str, Any]:
        """
        Processa um CSV e envia mensagens personalizadas.

        Estrutura do CSV:
            - Coluna 0  → destinatário (número +55... ou ID de grupo ...@g.us)
            - Colunas 1..N-2 → variáveis para substituição em {NomeDaColuna}
            - Coluna N-1 (última) → template da mensagem

        Args:
            csv_content: conteúdo bruto do arquivo CSV (string UTF-8)
            progress_cb: async callable(sent, total, row_result) chamado a cada envio

        Returns:
            {
                "total":   int,
                "sent":    int,
                "failed":  int,
                "skipped": int,
                "results": [ { "dest", "status", "message", "error" }, ... ]
            }
        """
        if self.state != "connected" or not self._page:
            raise RuntimeError("Sessão não está conectada.")

        # ── Parse do CSV ──────────────────────────────────────────────
        parsed, parse_errors = self._parse_csv(csv_content)
        if not parsed and parse_errors:
            raise ValueError(f"CSV inválido: {parse_errors[0]}")

        results = []
        sent = 0
        failed = 0
        skipped = len(parse_errors)

        # Registra erros de parse como skipped
        for err in parse_errors:
            results.append({
                "dest": err.get("dest", "?"),
                "line": err.get("line"),
                "status": "skipped",
                "message": "",
                "error": err["msg"],
            })

        total = len(parsed) + skipped
        logger.info(f"[WASession] CSV: {len(parsed)} linhas válidas, {skipped} com erro.")

        # ── Envio linha a linha ───────────────────────────────────────
        for i, row in enumerate(parsed):
            dest = row["dest"]
            msg  = row["message"]
            line = row["line"]

            row_result = {
                "dest": dest,
                "line": line,
                "status": "pending",
                "message": msg,
                "error": None,
            }

            try:
                await self._send_single_message(dest, msg)
                row_result["status"] = "sent"
                sent += 1
                logger.info(f"[WASession] ✓ [{line}] → {dest}")
            except asyncio.CancelledError:
                raise
            except Exception as e:
                row_result["status"] = "failed"
                row_result["error"] = str(e)
                failed += 1
                logger.warning(f"[WASession] ✗ [{line}] → {dest}: {e}")

            results.append(row_result)

            if progress_cb:
                await progress_cb(sent + failed, total, row_result)

            # Delay entre envios (evita ban)
            await asyncio.sleep(1.5)

        summary = {
            "total": total,
            "sent": sent,
            "failed": failed,
            "skipped": skipped,
            "results": results,
        }

        # Salva log do envio
        await self._save_send_log(summary)
        return summary

    def _parse_csv(self, content: str):
        """
        Parseia o CSV e retorna (linhas_válidas, linhas_com_erro).

        Cada linha válida: { "line": int, "dest": str, "vars": dict, "message": str }
        Cada erro:         { "line": int, "dest": str, "msg": str }
        """
        valid = []
        errors = []

        reader = csv.reader(io.StringIO(content))
        rows = list(reader)

        if not rows:
            errors.append({"line": 0, "dest": "", "msg": "Arquivo CSV vazio."})
            return valid, errors

        headers = [h.strip() for h in rows[0]]
        if len(headers) < 2:
            errors.append({"line": 1, "dest": "", "msg": "CSV precisa ter ao menos 2 colunas (destinatário + mensagem)."})
            return valid, errors

        # Colunas intermediárias são variáveis
        var_cols = headers[1:-1]

        for i, row in enumerate(rows[1:], start=2):
            if not any(cell.strip() for cell in row):
                continue  # linha em branco

            if len(row) < 2:
                errors.append({"line": i, "dest": row[0] if row else "", "msg": "Colunas insuficientes."})
                continue

            dest = row[0].strip()
            if not dest:
                errors.append({"line": i, "dest": "", "msg": "Destinatário vazio na primeira coluna."})
                continue

            raw_msg = row[-1].strip()
            if not raw_msg:
                errors.append({"line": i, "dest": dest, "msg": "Mensagem (última coluna) está vazia."})
                continue

            # Monta dict de variáveis
            vars_dict: Dict[str, str] = {}
            for j, col in enumerate(var_cols):
                cell_idx = j + 1
                vars_dict[col] = row[cell_idx].strip() if cell_idx < len(row) else ""

            # Substitui placeholders {NomeDaColuna}
            final_msg = self._interpolate(raw_msg, vars_dict)

            valid.append({
                "line": i,
                "dest": dest,
                "vars": vars_dict,
                "raw_message": raw_msg,
                "message": final_msg,
            })

        return valid, errors

    def _interpolate(self, template: str, variables: Dict[str, str]) -> str:
        """
        Substitui {NomeDaColuna} pelos valores do dicionário.
        Variáveis não encontradas → string vazia.
        """
        def replacer(match):
            key = match.group(1)
            return variables.get(key, "")

        return re.sub(r'\{([^}]+)\}', replacer, template)

    async def _send_single_message(self, dest: str, message: str):
        """
        Abre a conversa com `dest` e envia `message`.

        `dest` pode ser:
            +5511999990000       → número de contato (formato internacional)
            120363xxxxxxxx@g.us  → ID de grupo
        """
        page = self._page

        # Monta URL direta (funciona para contatos e grupos via ID)
        if dest.endswith("@g.us"):
            # Grupos: usa a API de URL do WA
            group_id = dest.replace("@g.us", "")
            url = f"{WA_URL}/accept?code={group_id}"
            # Fallback: navega pela busca
            await self._open_chat_by_search(dest)
        else:
            # Contato: normaliza número
            phone = re.sub(r'[^\d+]', '', dest)
            if not phone.startswith('+'):
                phone = '+' + phone
            url = f"{WA_URL}/send?phone={phone}"
            await page.goto(url, wait_until="domcontentloaded", timeout=15_000)
            await asyncio.sleep(2)

        # Aguarda campo de texto da conversa
        try:
            await page.wait_for_selector(SEL_MSG_INPUT, timeout=10_000)
        except Exception:
            raise RuntimeError(f"Campo de mensagem não encontrado para {dest}. Contato pode não existir.")

        # Digita a mensagem
        msg_box = await page.query_selector(SEL_MSG_INPUT)
        await msg_box.click()
        await asyncio.sleep(0.3)

        # Usa clipboard para suportar emojis e caracteres especiais
        await page.evaluate(
            """([el, txt]) => {
                el.focus();
                document.execCommand('insertText', false, txt);
            }""",
            [msg_box, message],
        )
        await asyncio.sleep(0.5)

        # Envia com Enter
        await msg_box.press("Enter")
        await asyncio.sleep(1.0)

    async def _open_chat_by_search(self, dest: str):
        """Abre um chat pesquisando pelo nome/número na barra de busca."""
        page = self._page
        try:
            # Navega para a raiz antes de buscar
            await page.goto(WA_URL, wait_until="domcontentloaded", timeout=15_000)
            await asyncio.sleep(1.5)

            # Clica no container de busca e depois no input editável
            await page.click('[data-testid="chat-list-search-container"]')
            await asyncio.sleep(0.5)
            search_input = await page.query_selector('div[contenteditable="true"][data-tab]') or                            await page.query_selector('[data-testid="chat-list-search-container"] div[contenteditable]')
            if search_input:
                await search_input.fill(dest)
            else:
                await page.keyboard.type(dest)
            await asyncio.sleep(1.5)

            # Clica no primeiro resultado
            first = await page.wait_for_selector(SEL_CONTACT_ROW, timeout=5_000)
            await first.click()
            await asyncio.sleep(1.0)
        except Exception as e:
            raise RuntimeError(f"Não foi possível abrir chat para '{dest}': {e}")

    async def _save_send_log(self, summary: Dict):
        """Salva o relatório de envio em JSON."""
        ts = int(time.time())
        log_path = LOGS_DIR / f"send_log_{ts}.json"
        log_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        logger.info(f"[WASession] Log de envio salvo em {log_path}")
