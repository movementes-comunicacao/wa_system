# WA_SYSTEM

Sistema de automação WhatsApp Web via Playwright + Python.

## Estrutura do Projeto

```
wa_system/
├── server.py              ← Servidor FastAPI (ponto de entrada)
├── requirements.txt       ← Dependências Python
├── setup.sh               ← Script de instalação automática
├── debug_browser.py       ← Teste isolado do Playwright
│
├── core/
│   ├── __init__.py
│   └── wa_session.py      ← Sessão Playwright (QR, auth, exportação, disparo)
│
├── templates/
│   └── index.html         ← Interface web completa (auth + dashboard)
│
├── static/                ← Arquivos estáticos futuros (CSS/JS extras)
│
└── data/
    ├── session/           ← Perfil Chromium persistente (login salvo)
    ├── exports/           ← CSVs e JSONs exportados
    └── logs/              ← Logs de envio de mensagens
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

## Arquitetura

```
Browser (HTML/JS)
      │  WebSocket ws://localhost:8000/ws  (estado em tempo real)
      │  REST      http://localhost:8000/api/*  (exportação + envio)
      ▼
  server.py  (FastAPI + uvicorn)
      │  asyncio
      ▼
  core/wa_session.py
      │  Playwright (Chromium headless)
      ▼
  web.whatsapp.com
```

## Funcionalidades

### 1. Autenticação via QR Code
- Chromium headless abre o WhatsApp Web automaticamente
- QR Code capturado do `<canvas>` e enviado via WebSocket como PNG base64
- Interface exibe o QR em tempo real — basta escanear com o celular
- **Sessão persistida** em `data/session/` — na próxima vez não precisa escanear

### 2. Exportação de Contatos e Grupos
```
GET /api/export?filter=all|contacts|groups
```
- Varre a lista de chats do WhatsApp Web conectado
- Detecta automaticamente contatos vs grupos
- Salva JSON + CSV em `data/exports/`
- Interface permite baixar CSV ou JSON

### 3. Disparo de Mensagens via CSV

```
POST /api/messages/validate   ← prévia sem enviar
POST /api/messages/send       ← envia de verdade
```

**Estrutura do CSV:**

| Coluna 1 (destinatário) | Colunas intermediárias (variáveis) | Última coluna (mensagem) |
|-------------------------|------------------------------------|--------------------------|
| +5511999990000          | Ana, Centro                        | Olá {Nome} do {Bairro}!  |
| 120363000@g.us          | Produto X, 10%                     | Promo de {Produto}: {Desconto} off! |

- **Coluna 0**: número do contato (`+55...`) ou ID do grupo (`...@g.us`)
- **Colunas do meio**: valores de variáveis, acessíveis como `{NomeDaColuna}` na mensagem
- **Última coluna**: template da mensagem com placeholders
- Variáveis vazias → substituídas por string vazia (sem quebrar a mensagem)
- Progresso linha a linha via WebSocket

### Validação de CSV
O endpoint `/api/messages/validate` retorna antes de enviar:
```json
{
  "valid_count": 10,
  "error_count": 1,
  "var_columns": ["Nome", "Bairro"],
  "preview": [...],
  "errors": [{"line": 3, "msg": "Destinatário vazio"}]
}
```

## Rotas da API

| Método | Rota                              | Descrição                          |
|--------|-----------------------------------|------------------------------------|
| GET    | `/`                               | Interface web                      |
| GET    | `/health`                         | Status da sessão                   |
| GET    | `/api/export?filter=all`          | Exporta contatos/grupos            |
| GET    | `/api/export/files`               | Lista exportações salvas           |
| GET    | `/api/export/download/{filename}` | Baixa arquivo de exportação        |
| POST   | `/api/messages/validate`          | Valida CSV sem enviar              |
| POST   | `/api/messages/send`              | Envia mensagens via CSV            |
| WS     | `/ws`                             | WebSocket (QR, progresso, estado)  |

## Debug (browser visível)

Para ver o Chromium na tela durante o desenvolvimento, edite `core/wa_session.py`:

```python
self._context = await self._playwright.chromium.launch_persistent_context(
    ...
    headless=False,   # ← mude para False
    ...
)
```

Execute o teste isolado do browser:

```bash
python debug_browser.py
```
