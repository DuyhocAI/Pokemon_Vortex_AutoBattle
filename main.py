import asyncio
import sys
import os

# Windows console mặc định cp1252 — ép UTF-8 để in được tiếng Việt
for _s in (sys.stdout, sys.stderr):
    if _s and hasattr(_s, "reconfigure"):
        _s.reconfigure(encoding="utf-8", errors="replace")

from loguru import logger

from config import config
from agent.browser import BrowserManager
from agent.login import login
from agent.grind_loop import grind
from agent.battle_tower import run_tower
from agent.sidequest import run_sidequest
from agent.memory import init_db, get_all_stats
from agent.localization import inject_interceptor
from agent.chat import start_chat_reader
import agent.ui as ui
import agent.llm as llm_module


# ── Menu chọn chế độ lúc khởi động ──────────────────────────────────────────
_MODE_MENU = {
    "1": ("battle",    "Grind map — đánh Pokemon hoang dã (EXP)"),
    "2": ("catch",     "Grind map — bắt Pokemon"),
    "3": ("tower",     "Season Battle Tower (auto)"),
    "4": ("sidequest", "Sidequests (auto)"),
}


def choose_mode_menu() -> str:
    """Bảng chọn chế độ khi khởi động. Enter = dùng MODE trong .env."""
    if not sys.stdin.isatty():
        return config.MODE  # chạy nền/script → không hỏi
    print()
    print("=" * 46)
    print("        POKEMON VORTEX AGENT")
    print("=" * 46)
    for key, (mode, desc) in _MODE_MENU.items():
        mark = " ←" if mode == config.MODE else ""
        print(f"  {key}. {desc}{mark}")
    print("-" * 46)
    try:
        choice = input(f"Chọn chế độ [1-4] (Enter = {config.MODE}): ").strip()
    except (EOFError, KeyboardInterrupt):
        return config.MODE
    mode = _MODE_MENU.get(choice, (config.MODE, ""))[0]
    print(f"→ Chế độ: {mode}\n")
    return mode


def _setup_logging():
    """Chuyển loguru sang UI log buffer thay vì stderr."""
    logger.remove()

    # Level → màu Rich
    COLORS = {
        "DEBUG":   "dim",
        "INFO":    "cyan",
        "SUCCESS": "bold green",
        "WARNING": "yellow",
        "ERROR":   "bold red",
        "CRITICAL":"bold red on white",
    }

    def _sink(message):
        record = message.record
        level  = record["level"].name
        color  = COLORS.get(level, "white")
        ts     = record["time"].strftime("%H:%M:%S")
        text   = record["message"].strip()
        # Ghi thẳng vào buffer — KHÔNG dùng add_log() để tránh timestamp kép
        ui._log_buf.append(f"[dim]{ts}[/dim] [{color}]{level:7}[/{color}] {text}")

    logger.add(_sink, level="DEBUG", format="{message}")
    # Giữ file log đầy đủ
    logger.add("logs/agent.log", level="DEBUG", rotation="10 MB", retention=3)


async def main():
    os.makedirs("logs", exist_ok=True)
    init_db()

    try:
        config.validate()
    except ValueError as e:
        print(f"Lỗi cấu hình: {e}")
        sys.exit(1)

    # Bảng chọn chế độ (trước khi dashboard chiếm terminal)
    config.MODE = choose_mode_menu()

    _setup_logging()

    # Khởi động dashboard trước khi làm bất cứ gì
    dashboard_task = asyncio.create_task(ui.run_dashboard())

    # Web UI (Pokemon-themed dashboard + chat)
    web_task = None
    if config.WEB_UI:
        from agent.webui import run_webui
        web_task = asyncio.create_task(run_webui(config.WEB_HOST, config.WEB_PORT))
        logger.info(f"Web dashboard: http://{config.WEB_HOST}:{config.WEB_PORT}")

    # Load game knowledge (cache 7 ngày, fetch wiki nếu cần)
    logger.info("Đang tải game knowledge...")
    llm_module._extra_knowledge = await llm_module.load_knowledge()

    browser = BrowserManager()
    try:
        page = await browser.start()
        await inject_interceptor(page)

        logger.info("Đăng nhập...")
        if not await login(page):
            logger.error("Không thể đăng nhập. Kiểm tra lại username/password.")
            sys.exit(1)

        # Fetch team at startup for both modes
        from agent.team import fetch_team
        try:
            team = await fetch_team(page)
            if team:
                ui.set_team(team)
                logger.info(f"[TEAM] Loaded {len(team)} Pokemon")
        except Exception as e:
            logger.warning(f"[TEAM] Startup fetch error: {e}")

        # Chat reader
        chat_task = asyncio.create_task(start_chat_reader())

        # Route by mode
        mode = config.MODE
        logger.info(f"[MAIN] Mode: {mode}")
        if mode == "tower":
            await run_tower(page)
        elif mode == "sidequest":
            await run_sidequest(page)
        else:
            await grind(page)

        chat_task.cancel()
        try:
            await chat_task
        except asyncio.CancelledError:
            pass

    except KeyboardInterrupt:
        logger.info("Dừng agent (Ctrl+C)")
        stats = get_all_stats()
        if stats:
            logger.info("=== TOP POKEMON ===")
            for s in stats[:10]:
                logger.info(f"  {s['pokemon']:20} total={s['total']:4}  wins={s['wins']:4}")
    except Exception as e:
        logger.exception(f"Lỗi không mong muốn: {e}")
    finally:
        dashboard_task.cancel()
        if web_task:
            web_task.cancel()
        try:
            await browser.close()
        except Exception:
            pass


if __name__ == "__main__":
    asyncio.run(main())
