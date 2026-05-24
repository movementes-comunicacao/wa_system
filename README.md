# WA_SYSTEM

Sistema de automação WhatsApp Web via Playwright + Python.

## Estrutura do Projeto

```
wa_system/
├── server.py              ← Servidor FastAPI (ponto de entrada)
├── requirements.txt       ← Dependências Python
├── setup.sh               ← Script de instalação
│
├── core/
│   ├── __init__.py
│   └── wa_session.py      ← Sessão Playwright (QR, auth, estado)
│
├── templates/
│   └── index.html         ← Interface web (auth + dashboard)
│
├── static/                ← Arquivos estáticos (CSS/JS extras, futuro)
│
└── data/
    ├── session/           ← Perfil Chromium persistente (login salvo)
    ├── exports/           ← CSVs e JSONs exportados
    └── logs/              ← Logs da aplicação
```

## Instalação

```bash
cd wa_system
chmod +x setup.sh
./setup.sh
```

## Rodando

```bash
source .venv/bin/activate
python server.py
```

Acesse **http://localhost:8000** no navegador.

## Como funciona

```
Browser (HTML/JS)
      │  WebSocket ws://localhost:8000/ws
      ▼
  server.py  (FastAPI + uvicorn)
      │  asyncio
      ▼
  core/wa_session.py
      │  Playwright (Chromium headless)
      ▼
  web.whatsapp.com
```

1. O servidor abre o **Chromium headless** apontando para WhatsApp Web
2. Captura o **QR Code** do `<canvas>` como imagem PNG em base64
3. Envia o QR via **WebSocket** para o browser exibir na tela
4. Quando o celular escaneia, detecta o seletor de "usuário autenticado"
5. Envia evento `connected` → interface muda para o dashboard
6. O **perfil do Chromium é persistido** em `data/session/`, então na próxima vez não precisa escanear de novo

## Roadmap dos próximos passos

| Passo | Módulo              | Status     |
|-------|---------------------|------------|
| 1     | Login QR Code real  | ✅ Pronto  |
| 2     | Exportar contatos   | 🔜 próximo |
| 3     | Disparar mensagens  | 🔜 futuro  |
| 4     | Responder auto      | 🔜 futuro  |
| 5     | Monitorar conversas | 🔜 futuro  |

## Debug (modo visível)

Para ver o Chromium abrindo na tela, edite `core/wa_session.py`:

```python
self._context = await self._playwright.chromium.launch_persistent_context(
    ...
    headless=False,   # ← mude para False
    ...
)
```
