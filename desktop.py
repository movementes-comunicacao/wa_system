# OBRIGATÓRIO: deve ser as primeiras linhas antes de qualquer outro import
import multiprocessing
multiprocessing.freeze_support()

import os
import sys
import threading
import time
import subprocess
from pathlib import Path


# ── Configuração de ambiente do Playwright ────────────────────────────────────
if hasattr(sys, '_MEIPASS'):
    BASE_DIR = os.path.dirname(sys.executable)
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))

PASTA_NAVEGADORES = os.path.join(BASE_DIR,"_internal" ,"navegadores_playwright")
os.environ["PLAYWRIGHT_BROWSERS_PATH"] = PASTA_NAVEGADORES

# ── Flag de processo-pai: sinaliza que este é o processo principal ────────────
# O PyInstaller re-executa o .exe para subprocessos do multiprocessing/Playwright.
# Subprocessos herdam as variáveis de ambiente — se WA_MAIN_PROCESS já estiver
# definida, este é um subprocesso filho e NÃO deve abrir janela nem servidor.
_IS_MAIN_PROCESS = os.environ.get("WA_MAIN_PROCESS") != "1"
os.environ["WA_MAIN_PROCESS"] = "1"

# ── Tudo dentro do guard — PyInstaller re-executa o módulo como subprocesso ──
if __name__ == "__main__" and _IS_MAIN_PROCESS:

    def _encontrar_executavel_chromium() -> bool:
        """Verifica se o Chromium já foi instalado — apenas leitura de disco."""
        base = Path(PASTA_NAVEGADORES)
        if not base.exists():
            return False
        nomes = {"chrome", "chrome.exe", "chromium", "chromium.exe",
                 "chromium-browser", "Google Chrome"}
        for executavel in base.rglob("*"):
            if executavel.is_file() and executavel.name in nomes:
                return True
        return False

    def instalar_chromium(janela=None):
        if janela:
            try:
                janela.set_title("WA SYSTEM — Instalando Chromium (aguarde)...")
            except Exception:
                pass

        env = os.environ.copy()
        env["PLAYWRIGHT_BROWSERS_PATH"] = PASTA_NAVEGADORES
        env["WA_MAIN_PROCESS"] = "1"

        # Dentro do .exe (PyInstaller), sys.executable é o próprio .exe —
        # não dá para chamar "-m playwright install" nele.
        # O playwright empacotado pelo --collect-all inclui o CLI em:
        #   _MEIPASS/playwright/driver/playwright.cmd  (Windows)
        #   _MEIPASS/playwright/driver/playwright      (Linux/Mac)
        if hasattr(sys, '_MEIPASS'):
            # Usa o driver do Playwright empacotado diretamente
            driver = os.path.join(sys._MEIPASS, "playwright", "driver", "playwright.cmd")
            if not os.path.exists(driver):
                driver = os.path.join(sys._MEIPASS, "playwright", "driver", "playwright")
            cmd = [driver, "install", "chromium"]
        else:
            # Desenvolvimento normal: usa python -m playwright
            cmd = [sys.executable, "-m", "playwright", "install", "chromium"]

        try:
            subprocess.run(
                cmd,
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=300,  # 5 minutos máximo
            )
        except Exception as e:
            # Loga mas não trava — o Chromium pode já estar parcialmente instalado
            print(f"[setup] Erro ao instalar Chromium: {e}")

        if janela:
            try:
                janela.set_title("WA SYSTEM — Automação WhatsApp")
            except Exception:
                pass

    def start_api_server():
        import uvicorn
        from server import app
        uvicorn.run(app, host="localhost", port=8000, reload=False, log_level="info")

    import webview

    webview.settings["ALLOW_DOWNLOADS"] = True

    # Sobe FastAPI em background
    api_thread = threading.Thread(target=start_api_server, daemon=True)
    api_thread.start()
    time.sleep(1.5)

    # Cria janela
    window = webview.create_window(
        title="WA SYSTEM — Automação WhatsApp",
        url="http://localhost:8000",
        width=1200,
        height=850,
        min_size=(900, 600),
        text_select=True,
    )

    # Verifica/instala Chromium — ANTES de liberar o servidor para iniciar sessão
    # Usa um Event para sinalizar ao servidor quando o Chromium estiver pronto
    import threading
    chromium_ready = threading.Event()

    def setup_playwright():
        if not _encontrar_executavel_chromium():
            instalar_chromium(janela=window)
        chromium_ready.set()
        print("[setup] Chromium pronto.")

    setup_thread = threading.Thread(target=setup_playwright, daemon=True)
    setup_thread.start()

    # Aguarda Chromium estar pronto antes de liberar o auto-start da sessão
    # O frontend envia start_session ao receber 'idle' — mas só processamos
    # depois que o Chromium estiver instalado
    def aguardar_chromium_e_iniciar():
        chromium_ready.wait(timeout=300)
        if not chromium_ready.is_set():
            print("[setup] TIMEOUT: Chromium não instalou em 5 minutos.")

    threading.Thread(target=aguardar_chromium_e_iniciar, daemon=True).start()

    webview.start(debug=False)