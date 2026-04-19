"""
BarracudaWeb — FastAPI + WebSocket сервер
Подключается к C++ боту (TCP localhost:9999),
читает JSON снапшоты и раздаёт браузерным клиентам.

Запуск:
    source .venv/bin/activate
    uvicorn main:app --host 0.0.0.0 --port 8080 --reload
"""

import asyncio
import json
import socket
import logging
import os
from typing import Set
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from contextlib import asynccontextmanager

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("barracuda_web")

# ─────────────────────────────────────────
# Конфиг
# ─────────────────────────────────────────
BOT_HOST = "127.0.0.1"
BOT_PORT = int(os.environ.get("BOT_PORT", 9999))
RECONNECT_DELAY = 2.0   # секунд между попытками переподключения к боту

# ─────────────────────────────────────────
# FastAPI app
# ─────────────────────────────────────────
app = FastAPI(title="BarracudaBot Web Panel")
app.mount("/static", StaticFiles(directory="static"), name="static")

# ─────────────────────────────────────────
# Хранилище активных WebSocket соединений
# ─────────────────────────────────────────
class ConnectionManager:
    def __init__(self):
        self.active: Set[WebSocket] = set()

    async def connect(self, ws: WebSocket):
        await ws.accept()
        self.active.add(ws)
        log.info(f"Browser connected. Total: {len(self.active)}")

    def disconnect(self, ws: WebSocket):
        self.active.discard(ws)
        log.info(f"Browser disconnected. Total: {len(self.active)}")

    async def broadcast(self, data: str):
        dead = set()
        for ws in self.active:
            try:
                await ws.send_text(data)
            except Exception:
                dead.add(ws)
        for ws in dead:
            self.active.discard(ws)

manager = ConnectionManager()

# ─────────────────────────────────────────
# Последний снапшот от бота (кэш)
# ─────────────────────────────────────────
last_snapshot: dict = {}

# ─────────────────────────────────────────
# TCP клиент — подключается к C++ боту
# Читает JSON строки (разделитель '\n') и
# рассылает всем браузерным клиентам.
# ─────────────────────────────────────────
async def bot_reader_loop():
    global last_snapshot

    while True:
        try:
            log.info(f"Connecting to bot at {BOT_HOST}:{BOT_PORT}...")
            reader, writer = await asyncio.open_connection(BOT_HOST, BOT_PORT)
            log.info("Connected to bot")

            buf = ""
            while True:
                chunk = await reader.read(65536)
                if not chunk:
                    log.warning("Bot disconnected")
                    break

                buf += chunk.decode("utf-8", errors="replace")

                # Разбиваем по '\n' — каждая строка = один JSON снапшот
                while "\n" in buf:
                    line, buf = buf.split("\n", 1)
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        snapshot = json.loads(line)
                        last_snapshot = snapshot
                        # Рассылаем браузерам
                        await manager.broadcast(line)
                    except json.JSONDecodeError as e:
                        log.warning(f"JSON parse error: {e}")

            writer.close()
            await writer.wait_closed()

        except (ConnectionRefusedError, OSError) as e:
            log.warning(f"Cannot connect to bot: {e}. Retry in {RECONNECT_DELAY}s")
        except Exception as e:
            import traceback
            log.error(f"Bot reader error: {e}")
            log.error(traceback.format_exc())

        await asyncio.sleep(RECONNECT_DELAY)


# ─────────────────────────────────────────
# Старт фонового цикла при запуске приложения
# ─────────────────────────────────────────
_bot_task = None

@asynccontextmanager
async def lifespan(app: FastAPI):
    global _bot_task
    _bot_task = asyncio.create_task(bot_reader_loop())
    yield
    if _bot_task:
        _bot_task.cancel()

app = FastAPI(title="BarracudaBot Web Panel", lifespan=lifespan)

# ─────────────────────────────────────────
# WebSocket эндпоинт для браузера
# ─────────────────────────────────────────
@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await manager.connect(ws)

    # Отдаём последний снапшот сразу при подключении
    if last_snapshot:
        await ws.send_text(json.dumps(last_snapshot))

    try:
        while True:
            # Читаем команды от браузера
            data = await ws.receive_text()
            await handle_browser_command(data)
    except WebSocketDisconnect:
        manager.disconnect(ws)


# ─────────────────────────────────────────
# Обработка команд от браузера → бот
# ─────────────────────────────────────────
async def handle_browser_command(data: str):
    """
    Принимает команду от браузера и пересылает боту через TCP.
    Поддерживаемые команды:
      {"cmd": "emergency_stop"}
      {"cmd": "shutdown"}
    """
    try:
        cmd = json.loads(data)
        log.info(f"Browser command: {cmd}")

        # Пересылаем боту
        try:
            reader, writer = await asyncio.open_connection(BOT_HOST, BOT_PORT)
            writer.write((json.dumps(cmd) + "\n").encode())
            await writer.drain()
            writer.close()
            await writer.wait_closed()
        except Exception as e:
            log.error(f"Cannot forward command to bot: {e}")

    except json.JSONDecodeError:
        log.warning(f"Invalid command from browser: {data}")


# ─────────────────────────────────────────
# HTTP эндпоинты
# ─────────────────────────────────────────
@app.get("/")
async def root():
    return FileResponse("static/index.html")

@app.get("/api/status")
async def status():
    """REST эндпоинт для проверки статуса (healthcheck)"""
    return {
        "bot_connected": bool(last_snapshot),
        "browser_clients": len(manager.active),
        "last_ts": last_snapshot.get("ts", 0)
    }
