"""
Web UI server — FastAPI + WebSocket.
Dashboard chủ đề Pokemon tại http://localhost:<WEB_PORT>

Protocol WS (/ws):
  Server → client (mỗi 1s): {"type":"snapshot", "data":{state, team, logs, chats, summary}}
  Client → server:
    {"type":"chat",    "text": "..."}   → đẩy vào command_queue (xử lý như gõ terminal)
    {"type":"command", "cmd":  "..."}   → tương tự (nút bấm nhanh)
"""
import asyncio
import json
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from loguru import logger
import uvicorn

import agent.ui as ui
from agent.chat import command_queue
from agent.memory import get_summary

WEB_DIR = Path(__file__).resolve().parent.parent / "web"

app = FastAPI(title="Pokemon Vortex Agent")
app.mount("/static", StaticFiles(directory=str(WEB_DIR)), name="static")


@app.get("/")
async def index():
    return FileResponse(WEB_DIR / "index.html")


def _build_snapshot() -> dict:
    snap = ui.snapshot()
    try:
        snap["summary"] = get_summary()
    except Exception:
        snap["summary"] = {}
    return snap


@app.websocket("/ws")
async def ws_endpoint(websocket: WebSocket):
    await websocket.accept()
    logger.info("[WEB] Client kết nối dashboard")

    async def _sender():
        while True:
            try:
                await websocket.send_text(json.dumps(
                    {"type": "snapshot", "data": _build_snapshot()},
                    ensure_ascii=False, default=str,
                ))
            except Exception:
                break
            await asyncio.sleep(1.0)

    sender = asyncio.create_task(_sender())
    try:
        while True:
            raw = await websocket.receive_text()
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue

            mtype = msg.get("type")
            if mtype == "chat":
                text = (msg.get("text") or "").strip()
                if text:
                    ui.add_chat(f"[bold white]Bạn:[/bold white] {text}")
                    await command_queue.put(text)
            elif mtype == "command":
                cmd = (msg.get("cmd") or "").strip()
                if cmd:
                    ui.add_chat(f"[dim]>> {cmd}[/dim]")
                    await command_queue.put(cmd)
    except WebSocketDisconnect:
        pass
    except Exception as e:
        logger.debug(f"[WEB] ws: {e}")
    finally:
        sender.cancel()
        logger.info("[WEB] Client ngắt kết nối")


async def run_webui(host: str = "127.0.0.1", port: int = 8770):
    """Chạy uvicorn trong asyncio task của agent (không chiếm signal handlers)."""
    config = uvicorn.Config(app, host=host, port=port, log_level="warning", access_log=False)
    server = uvicorn.Server(config)
    server.install_signal_handlers = lambda: None  # type: ignore[method-assign]
    logger.success(f"[WEB] Dashboard: http://{host}:{port}")
    await server.serve()
