import threading
import time
import uvicorn
import webview

# Importa a instância 'app' do FastAPI diretamente do seu server.py
from server import app 
import subprocess, sys
from playwright.sync_api import sync_playwright

try:
    with sync_playwright() as p:
        p.chromium.launch().close()  # testa se o Chromium está disponível
except Exception:
    print("Instalando Chromium (apenas na primeira vez)...")
    subprocess.run([sys.executable, "-m", "playwright", "install", "chromium"])


def start_api_server():
    """
    Roda o servidor FastAPI. Essa função será executada em uma thread separada
    para não bloquear a interface gráfica do pywebview.
    """
    uvicorn.run(
        app, 
        host="localhost", 
        port=8000, 
        reload=False, 
        log_level="info"
    )

if __name__ == "__main__":
    webview.settings["ALLOW_DOWNLOADS"] = True  # Permite que o usuário baixe arquivos da interface web
    # 1. Inicia o servidor web (FastAPI) em background.
    # O daemon=True garante que o servidor seja desligado automaticamente 
    # quando a janela do aplicativo for fechada.
    api_thread = threading.Thread(target=start_api_server, daemon=True)
    api_thread.start()

    # 2. Aguarda 1.5 segundos para garantir que o Uvicorn subiu e a 
    # rota http://localhost:8000 está pronta para receber conexões.
    time.sleep(1.5)

    # 3. Cria a janela nativa do desktop apontando para o seu próprio servidor.
    window = webview.create_window(
        title="WA SYSTEM - Automação WhatsApp",
        url="http://localhost:8000",
        width=1200,
        height=850,
        min_size=(900, 600),
        text_select=True # Permite que o usuário selecione texto na interface
    )

    # 4. Inicia o loop da interface gráfica
    # Se quiser inspecionar elementos (DevTools), mude para debug=True
    webview.start(debug=False)