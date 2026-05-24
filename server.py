"""
server.py
Servidor FastAPI que:
  - Serve a interface HTML em http://localhost:8000
  - Expõe WebSocket em ws://localhost:8000/ws para comunicação em tempo real
  - Controla o ciclo de vida da WASession
"""

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Set

import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from core.wa_session import WASession

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# App & estado global
# ---------------------------------------------------------------------------

app = FastAPI(title="WA_SYSTEM")
session = WASession()
connected_clients: Set[WebSocket] = set()

STATIC_DIR = Path(__file__).parent / "static"
TEMPLATES_DIR = Path(__file__).parent / "templates"


# ---------------------------------------------------------------------------
# Broadcast para todos os clientes WebSocket
# ---------------------------------------------------------------------------

async def broadcast(payload: dict):
    """Envia mensagem JSON para todos os browsers conectados."""
    msg = json.dumps(payload)
    dead = set()
    for ws in connected_clients:
        try:
            await ws.send_text(msg)
        except Exception:
            dead.add(ws)
    connected_clients.difference_update(dead)


# ---------------------------------------------------------------------------
# Callbacks da WASession → notificam os browsers via WebSocket
# ---------------------------------------------------------------------------

async def on_state_change(state: str):
    payload = {"type": "state", "state": state}
    if state == "connected" and session.phone_number:
        payload["phone"] = session.phone_number
    await broadcast(payload)


async def on_qr(qr_base64: str):
    await broadcast({"type": "qr", "data": qr_base64})


session.on_state_change(on_state_change)
session.on_qr(on_qr)


# ---------------------------------------------------------------------------
# Rotas HTTP
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
async def index():
    """Serve a interface principal."""
    html_path = TEMPLATES_DIR / "index.html"
    return HTMLResponse(content=html_path.read_text(encoding="utf-8"))


@app.get("/health")
async def health():
    return {"status": "ok", "session_state": session.state}


# ---------------------------------------------------------------------------
# WebSocket — comunicação em tempo real com o browser
# ---------------------------------------------------------------------------

@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    connected_clients.add(ws)
    logger.info(f"Cliente WebSocket conectado. Total: {len(connected_clients)}")

    # Envia estado atual imediatamente ao conectar
    await ws.send_text(json.dumps({
        "type": "state",
        "state": session.state,
    }))
    if session.qr_base64:
        await ws.send_text(json.dumps({
            "type": "qr",
            "data": session.qr_base64,
        }))

    try:
        while True:
            raw = await ws.receive_text()
            await handle_client_message(json.loads(raw))
    except WebSocketDisconnect:
        connected_clients.discard(ws)
        logger.info(f"Cliente desconectado. Total: {len(connected_clients)}")
    except Exception as e:
        logger.error(f"Erro WebSocket: {e}")
        connected_clients.discard(ws)


async def handle_client_message(msg: dict):
    """
    Processa comandos enviados pelo browser.

    Comandos suportados:
        { "action": "start_session" }   → inicia sessão (se parada)
        { "action": "stop_session" }    → para sessão
        { "action": "clear_session" }   → apaga perfil salvo e reinicia
        { "action": "restart_session" } → para + reinicia sem apagar perfil
    """
    action = msg.get("action")
    logger.info(f"Comando recebido: {action}")

    if action == "start_session":
        # start() já cancela task anterior se existir
        await session.start()

    elif action == "stop_session":
        await session.stop()

    elif action == "clear_session":
        # Para a sessão atual (aguarda cancelamento completo)
        await session.stop()
        # Apaga o perfil salvo do Chromium
        await session.clear_session()
        # Reseta estado visível para o browser
        session.state = "idle"
        session.qr_base64 = None
        await broadcast({"type": "state", "state": "idle"})

    elif action == "restart_session":
        await session.stop()
        await session.start()

    else:
        logger.warning(f"Ação desconhecida: {action}")


# ---------------------------------------------------------------------------
# Startup / Shutdown
# ---------------------------------------------------------------------------

@app.on_event("startup")
async def startup():
    logger.info("Servidor iniciado. Acesse http://localhost:8000")
    # Inicia a sessão automaticamente ao ligar o servidor
    await session.start()


@app.on_event("shutdown")
async def shutdown():
    await session.stop()


# ---------------------------------------------------------------------------
# Ponto de entrada
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    uvicorn.run(
        "server:app",
        host="localhost",
        port=8000,
        reload=False,
        log_level="info",
    )
