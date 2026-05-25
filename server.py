"""
server.py
Servidor FastAPI que:
  - Serve a interface HTML em http://localhost:8000
  - Expõe WebSocket em ws://localhost:8000/ws para comunicação em tempo real
  - Controla o ciclo de vida da WASession
  - Rotas REST para exportação e disparo de mensagens
"""

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Set

import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, UploadFile, File, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles

from core.wa_session import WASession, EXPORTS_DIR

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

if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


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
# ROTAS: Exportação de contatos e grupos
# ---------------------------------------------------------------------------

@app.get("/api/export")
async def export_contacts(filter: str = "all"):
    """
    Exporta contatos e/ou grupos.

    Query param:
        filter: "all" | "contacts" | "groups"  (default: "all")

    Retorna JSON com { contacts, groups, total, filter }
    """
    if session.state != "connected":
        raise HTTPException(
            status_code=400,
            detail=f"Sessão não conectada. Estado atual: {session.state}"
        )

    if filter not in ("all", "contacts", "groups"):
        raise HTTPException(status_code=422, detail="filter deve ser 'all', 'contacts' ou 'groups'")

    try:
        result = await session.export_contacts_and_groups(filter_type=filter)
        return JSONResponse(result)
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.exception(f"Erro na exportação: {e}")
        raise HTTPException(status_code=500, detail=f"Erro interno: {e}")


@app.get("/api/export/files")
async def list_export_files():
    """Lista todos os arquivos de exportação gerados."""
    files = []
    for f in sorted(EXPORTS_DIR.iterdir(), reverse=True):
        if f.is_file():
            files.append({
                "name": f.name,
                "size": f.stat().st_size,
                "url": f"/api/export/download/{f.name}",
            })
    return {"files": files}


@app.get("/api/export/download/{filename}")
async def download_export(filename: str):
    """Baixa um arquivo de exportação gerado."""
    # Sanitiza o nome do arquivo
    safe_name = Path(filename).name
    file_path = EXPORTS_DIR / safe_name
    if not file_path.exists() or not file_path.is_file():
        raise HTTPException(status_code=404, detail="Arquivo não encontrado.")
    return FileResponse(path=str(file_path), filename=safe_name)


# ---------------------------------------------------------------------------
# ROTAS: Disparo de mensagens via CSV
# ---------------------------------------------------------------------------

@app.post("/api/messages/send")
async def send_messages(file: UploadFile = File(...)):
    """
    Recebe um CSV e dispara mensagens personalizadas.

    Estrutura do CSV:
        Coluna 0      → destinatário (+55... ou ID@g.us)
        Colunas 1..N-2 → variáveis para {NomeDaColuna}
        Coluna N-1    → template da mensagem

    Retorna JSON com { total, sent, failed, skipped, results }
    """
    if session.state != "connected":
        raise HTTPException(
            status_code=400,
            detail=f"Sessão não conectada. Estado atual: {session.state}"
        )

    if not file.filename.endswith(".csv"):
        raise HTTPException(status_code=422, detail="Apenas arquivos .csv são aceitos.")

    try:
        content_bytes = await file.read()
        content = content_bytes.decode("utf-8")
    except UnicodeDecodeError:
        # Tenta latin-1 como fallback
        content = content_bytes.decode("latin-1")
    except Exception as e:
        raise HTTPException(status_code=422, detail=f"Erro ao ler o arquivo: {e}")

    # Progresso via WebSocket
    async def progress_cb(sent, total, row_result):
        await broadcast({
            "type": "send_progress",
            "sent": sent,
            "total": total,
            "row": row_result,
        })

    try:
        result = await session.send_messages_from_csv(content, progress_cb=progress_cb)
        # Notifica conclusão
        await broadcast({"type": "send_done", **result})
        return JSONResponse(result)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.exception(f"Erro no disparo: {e}")
        raise HTTPException(status_code=500, detail=f"Erro interno: {e}")


@app.post("/api/messages/validate")
async def validate_csv(file: UploadFile = File(...)):
    """
    Valida a estrutura do CSV sem enviar mensagens.
    Retorna preview das mensagens que seriam enviadas.
    """
    if not file.filename.endswith(".csv"):
        raise HTTPException(status_code=422, detail="Apenas arquivos .csv são aceitos.")

    try:
        content = (await file.read()).decode("utf-8")
    except UnicodeDecodeError:
        content = (await file.read()).decode("latin-1")
    except Exception as e:
        raise HTTPException(status_code=422, detail=f"Erro ao ler o arquivo: {e}")

    valid, errors = session._parse_csv(content)

    preview = [
        {
            "line": r["line"],
            "dest": r["dest"],
            "vars": r["vars"],
            "message": r["message"],
        }
        for r in valid[:20]  # Mostra até 20 no preview
    ]

    return JSONResponse({
        "valid_count": len(valid),
        "error_count": len(errors),
        "var_columns": list(valid[0]["vars"].keys()) if valid else [],
        "preview": preview,
        "errors": errors,
    })


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
    Processa comandos enviados pelo browser via WebSocket.

    Comandos:
        { "action": "start_session" }
        { "action": "stop_session" }
        { "action": "clear_session" }
        { "action": "restart_session" }
    """
    action = msg.get("action")
    logger.info(f"Comando recebido: {action}")

    if action == "start_session":
        await session.start()

    elif action == "stop_session":
        await session.stop()

    elif action == "clear_session":
        await session.stop()
        await session.clear_session()
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
