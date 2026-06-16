"""
Chat interface — nhận lệnh từ user trong khi agent đang chạy.

Trên Windows dùng msvcrt (non-blocking keyboard read) để không xung đột với Rich Live.
Ký tự đang gõ hiển thị trong CHAT panel qua ui._state["input_buf"].
"""
import asyncio
import sys
import platform
from loguru import logger
from config import config

command_queue: asyncio.Queue = asyncio.Queue()
pause_event: asyncio.Event   = asyncio.Event()
pause_event.set()  # Set = running, Clear = paused

agent_state = {
    "running":         True,
    "force_catch":     False,
    "force_skip":      False,
    "rare_pause":      True,
    "rare_action":     None,     # "catch" | "battle" | "skip"
    "pause_reason":    None,
    "fetch_team":      False,
    "llm_instruction": None,
}

COMMANDS = [
    ("status",               "xem trang thai agent"),
    ("team",                 "xem doi hinh Pokemon + level + skill"),
    ("stats",                "thong ke thang/thua/ti le thang (lifetime)"),
    ("optimize",             "LLM phan tich + goi y doi hinh toi uu"),
    ("info <ten pokemon>",   "tra cuu pokemon tu internet (types/stats)"),
    ("stop / quit",          "dung agent"),
    ("pause / resume",       "tam dung / tiep tuc"),
    ("catch",                "bat Pokemon tiep theo"),
    ("skip",                 "bo qua encounter tiep theo"),
    ("catch-rare on/off",    "dung khi gap rare"),
    ("mode battle|catch|both", "doi che do"),
    ("help",                 "xem lai lenh nay"),
    ("<bat ky text>",        "chat tu do voi agent (LLM tra loi)"),
]

# Buffer ký tự đang gõ
_input_buffer: str = ""


def _log(msg: str):
    """Đưa output chat vào CHAT panel."""
    try:
        import agent.ui as ui
        ui.add_chat(msg)
    except Exception:
        pass


def _show_help():
    _log("[bold cyan]─── LỆNH CHAT ──────────────────────────[/bold cyan]")
    for cmd, desc in COMMANDS:
        _log(f"  [bright_blue]{cmd:<26}[/bright_blue] {desc}")
    _log("[bold cyan]────────────────────────────────────────[/bold cyan]")


# ── Chat reader ────────────────────────────────────────────────────────────

async def start_chat_reader():
    """Chạy trong asyncio task riêng."""
    _show_help()
    _log("[green]Chat san sang — go lenh bat ky luc nao[/green]")
    _log("[dim]Ky tu ban go se hien o dong cuoi panel nay[/dim]")

    if platform.system() == "Windows":
        await _windows_chat_reader()
    else:
        await _unix_chat_reader()


async def _windows_chat_reader():
    """Non-blocking keyboard read trên Windows bằng msvcrt."""
    import msvcrt
    global _input_buffer

    while agent_state["running"]:
        try:
            if msvcrt.kbhit():
                ch = msvcrt.getwch()  # không echo, trả về str

                # Arrow keys / special keys gửi 2 byte: \x00 hoặc \xe0 rồi 1 byte nữa
                if ch in ('\x00', '\xe0'):
                    if msvcrt.kbhit():
                        msvcrt.getwch()  # bỏ byte thứ 2
                    continue

                if ch in ('\r', '\n'):          # Enter → submit
                    cmd = _input_buffer.strip()
                    if cmd:
                        _log(f"[dim]>> {cmd}[/dim]")
                        await command_queue.put(cmd.lower())
                    _input_buffer = ""
                    _set_ui_buf("")

                elif ch in ('\x08', '\x7f'):    # Backspace
                    _input_buffer = _input_buffer[:-1]
                    _set_ui_buf(_input_buffer)

                elif ch == '\x03':              # Ctrl+C
                    agent_state["running"] = False
                    break

                elif ch >= ' ':                 # Ký tự in được
                    _input_buffer += ch
                    _set_ui_buf(_input_buffer)

            else:
                await asyncio.sleep(0.05)

        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.debug(f"[CHAT-WIN] {e}")
            await asyncio.sleep(0.1)


async def _unix_chat_reader():
    """Stdin readline trên Linux/Mac."""
    loop = asyncio.get_event_loop()
    while agent_state["running"]:
        try:
            line = await loop.run_in_executor(None, sys.stdin.readline)
            if line is None:
                break
            cmd = line.strip()
            if cmd:
                _log(f"[dim]>> {cmd}[/dim]")
                await command_queue.put(cmd.lower())
        except (EOFError, KeyboardInterrupt):
            break
        except asyncio.CancelledError:
            break


def _set_ui_buf(text: str):
    try:
        import agent.ui as ui
        ui.update(input_buf=text)
    except Exception:
        pass


# ── Command handler ─────────────────────────────────────────────────────────

async def process_commands(stats: dict) -> bool:
    """Gọi mỗi tick — xử lý lệnh pending. Trả về False nếu cần dừng."""
    while not command_queue.empty():
        cmd = await command_queue.get()
        await _handle_command(cmd, stats)

    await pause_event.wait()
    return agent_state["running"]


async def _handle_command(cmd: str, stats: dict):
    parts = cmd.split()
    verb  = parts[0].lower() if parts else ""

    if verb in ("stop", "quit", "exit"):
        logger.info("[CHAT] Dừng agent")
        agent_state["running"] = False
        pause_event.set()

    elif verb == "pause":
        agent_state["pause_reason"] = "user"
        pause_event.clear()
        logger.info("[CHAT] Tạm dừng")
        try:
            import agent.ui as ui
            ui.update(paused=True, pause_reason="user")
        except Exception:
            pass

    elif verb == "resume":
        agent_state["pause_reason"] = None
        pause_event.set()
        logger.info("[CHAT] Tiếp tục")
        try:
            import agent.ui as ui
            ui.update(paused=False, pause_reason=None)
        except Exception:
            pass

    elif verb == "status":
        state_str = "|| PAUSED" if not pause_event.is_set() else "> RUNNING"
        _log("[bold]── STATUS ────────────────────────────[/bold]")
        _log(f"  Trang thai : [cyan]{state_str}[/cyan]")
        _log(f"  Mode       : [yellow]{config.MODE}[/yellow]")
        _log(f"  Battles    : {stats.get('battles', 0)}")
        _log(f"  Wins       : [green]{stats.get('wins', 0)}[/green]")
        _log(f"  Catches    : [cyan]{stats.get('catches', 0)}[/cyan]")
        _log(f"  Rare pause : {'[green]ON[/green]' if agent_state['rare_pause'] else '[red]OFF[/red]'}")
        _log("[bold]──────────────────────────────────────[/bold]")

    elif verb == "catch":
        agent_state["force_catch"] = True
        _log("[cyan]Force catch Pokemon tiep theo[/cyan]")

    elif verb == "skip":
        agent_state["force_skip"] = True
        _log("[yellow]Skip encounter tiep theo[/yellow]")

    elif verb == "mode" and len(parts) > 1:
        new_mode = parts[1].lower()
        if new_mode in ("battle", "catch", "both"):
            config.MODE = new_mode
            _log(f"[green]Mode → {new_mode}[/green]")
        else:
            _log("[red]Mode khong hop le. Dung: battle | catch | both[/red]")

    elif verb in ("catch-rare", "rare"):
        if len(parts) > 1 and parts[1] == "off":
            agent_state["rare_pause"] = False
            _log("[yellow]Rare pause: OFF[/yellow]")
        else:
            agent_state["rare_pause"] = True
            _log("[green]Rare pause: ON[/green]")

    elif verb == "team":
        agent_state["fetch_team"] = True
        _log("[cyan]Dang lay thong tin doi hinh...[/cyan]")

    elif verb == "help":
        _show_help()

    elif verb == "stats":
        _show_stats()

    elif verb == "optimize":
        _log("[cyan]Đang phân tích đội hình (LLM)...[/cyan]")
        asyncio.create_task(_run_optimize())

    elif verb == "info" and len(parts) > 1:
        name = " ".join(parts[1:])
        _log(f"[cyan]Tra cứu {name}...[/cyan]")
        asyncio.create_task(_run_info(name))

    else:
        # Chat tự do → LLM trả lời hội thoại (+ có thể ra lệnh cho agent)
        logger.info(f"[CHAT→LLM] {cmd}")
        asyncio.create_task(_free_chat(cmd, stats))

    # Khi đang pause vì rare, nhận lệnh catch/battle/skip để tiếp tục
    if agent_state["pause_reason"] == "rare" and verb in ("catch", "battle", "skip"):
        agent_state["rare_action"]  = verb
        agent_state["pause_reason"] = None
        logger.info(f"[CHAT] Rare → {verb}")
        pause_event.set()
        try:
            import agent.ui as ui
            ui.update(paused=False, pause_reason=None)
        except Exception:
            pass


def _show_stats():
    """Thống kê lifetime từ memory DB."""
    try:
        from agent.memory import get_summary
        s = get_summary()
        wr = f"{s['winrate']}%" if s["winrate"] is not None else "N/A"
        _log("[bold]── THỐNG KÊ (LIFETIME) ───────────────[/bold]")
        _log(f"  Battles  : {s['battles']}")
        _log(f"  Wins     : [green]{s['wins']}[/green]   Losses: [red]{s['losses']}[/red]")
        _log(f"  Winrate  : [bold cyan]{wr}[/bold cyan]")
        _log(f"  Catches  : [cyan]{s['catches']}[/cyan]   Fled: {s['fled']}")
        if s["hardest"]:
            hard = ", ".join(f"{h['pokemon']}({h['losses']}L)" for h in s["hardest"][:3])
            _log(f"  Khó nhất : [yellow]{hard}[/yellow]")
        _log("[bold]──────────────────────────────────────[/bold]")
    except Exception as e:
        _log(f"[red]Lỗi stats: {e}[/red]")


async def _run_optimize():
    """LLM phân tích đội hình tối ưu — chạy nền, kết quả vào chat + UI panel."""
    try:
        from agent.team import get_cached_team
        from agent.memory import get_summary, get_lessons
        from agent.llm import optimize_team
        from agent.pokedex import lookup
        import agent.ui as ui

        team = get_cached_team()
        if not team:
            _log("[yellow]Chưa có dữ liệu đội hình — gõ 'team' trước.[/yellow]")
            return

        # Tra types cho từng Pokemon trong đội (internet + cache)
        team_types = {}
        for pk in team:
            info = await lookup(pk.get("name") or "")
            if info:
                team_types[pk["name"]] = info.get("types", [])

        rec = await optimize_team(team, get_summary(), get_lessons(limit=8), team_types)
        ui.update(recommendation=rec)
        _log("[bold cyan]── ĐỘI HÌNH TỐI ƯU (LLM) ─────────────[/bold cyan]")
        for line in rec.splitlines():
            if line.strip():
                _log(f"  {line.strip()}")
        _log("[bold cyan]──────────────────────────────────────[/bold cyan]")
    except Exception as e:
        _log(f"[red]Lỗi optimize: {e}[/red]")


async def _run_info(name: str):
    """Tra cứu Pokemon từ internet, lưu memory, hiển thị."""
    try:
        from agent.pokedex import lookup, weaknesses
        info = await lookup(name)
        if not info:
            _log(f"[yellow]Không tìm thấy '{name}' trên PokeAPI[/yellow]")
            return
        types = "/".join(info.get("types", [])) or "?"
        stats = info.get("stats", {})
        weak  = ", ".join(weaknesses(info.get("types", []))) or "?"
        _log(f"[bold]{info['name']}[/bold]  [cyan]{types}[/cyan]")
        _log(f"  HP {stats.get('hp','?')} / ATK {stats.get('attack','?')} / DEF {stats.get('defense','?')} "
             f"/ SpA {stats.get('sp_atk','?')} / SpD {stats.get('sp_def','?')} / SPE {stats.get('speed','?')}")
        _log(f"  Yếu vs: [yellow]{weak}[/yellow]")
        _log("[dim]  (đã lưu vào memory)[/dim]")
    except Exception as e:
        _log(f"[red]Lỗi info: {e}[/red]")


async def _free_chat(text: str, stats: dict):
    """Chat tự do: LLM trả lời hội thoại, kèm instruction cho agent nếu user ra lệnh."""
    try:
        from agent.llm import chat_reply
        from agent.memory import get_summary
        from agent.team import get_cached_team
        import agent.ui as ui

        context = {
            "agent_state": {
                "screen":  ui._state.get("screen"),
                "zone":    ui._state.get("zone"),
                "mode":    config.MODE,
                "paused":  ui._state.get("paused"),
                "current_enemy": ui._state.get("pokemon"),
            },
            "session_stats": stats,
            "lifetime_stats": get_summary(),
            "team": [
                {"name": p.get("name"), "level": p.get("level"), "moves": p.get("moves")}
                for p in get_cached_team()
            ],
        }
        result = await chat_reply(text, context)
        reply = result.get("reply", "...")
        for line in reply.splitlines():
            if line.strip():
                _log(f"[bright_green]Agent:[/bright_green] {line.strip()}")
        if result.get("instruction"):
            agent_state["llm_instruction"] = result["instruction"]
            _log(f"[dim]→ Lệnh cho bot: {result['instruction']}[/dim]")
    except Exception as e:
        _log(f"[red]Lỗi chat: {e}[/red]")


async def notify_rare(pokemon_name: str, variant: str):
    """Gọi khi phát hiện rare Pokemon — pause và thông báo."""
    if not agent_state["rare_pause"]:
        return

    _log(f"[bold yellow]{'!'*40}[/bold yellow]")
    _log(f"[bold yellow]  RARE: {variant.upper()} {pokemon_name}[/bold yellow]")
    _log(f"[bold yellow]{'!'*40}[/bold yellow]")
    _log("[bold]  >> Go: [cyan]catch[/cyan] / [red]battle[/red] / [dim]skip[/dim][/bold]")

    try:
        import agent.ui as ui
        ui.update(paused=True, pause_reason="rare")
    except Exception:
        pass

    agent_state["pause_reason"] = "rare"
    agent_state["rare_action"]  = None
    pause_event.clear()
