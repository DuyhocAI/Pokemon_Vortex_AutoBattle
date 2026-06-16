"""
Rich terminal dashboard — realtime UI cho Pokemon Vortex Agent.
Layout:
  Row 1: AGENT | ENCOUNTER | STATS | SYSTEM
  Row 2: TEAM INFO
  Row 3: LOG (game events) | CHAT (commands)
  Row 4: footer commands
"""
import asyncio, io, sys, time
from collections import deque
from datetime    import datetime
from rich.console import Console
from rich.layout  import Layout
from rich.live    import Live
from rich.panel   import Panel
from rich.table   import Table
from rich.text    import Text

from agent.monitor import get_stats, get_gpu_name, NVML_OK

# ── UTF-8 console ──────────────────────────────────────────────────────────
_utf8 = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace", line_buffering=True)
console = Console(file=_utf8, force_terminal=True, legacy_windows=False)

# ── Shared state ───────────────────────────────────────────────────────────
_state = {
    "zone": "?", "tile": None, "screen": "starting",
    "action": "...", "reason": "",
    # Encounter / battle
    "pokemon": None, "variant": None, "poke_level": None,
    "moves": [], "moves_detail": [], "is_rare": False,
    "enemy_hp": None, "enemy_hp_pct": None, "enemy_img": None,
    "my_name": None, "my_level": None, "my_hp": None, "my_hp_pct": None, "my_img": None,
    "battle_phase": None,
    # Stats (session)
    "battles": 0, "wins": 0, "losses": 0, "catches": 0, "errors": 0, "llm_calls": 0,
    "start_time": time.time(),
    # Status
    "paused": False, "pause_reason": None,
    # LLM team recommendation
    "recommendation": None,
    # Chat input buffer (hiển thị ký tự đang gõ)
    "input_buf": "",
}

_log_buf:  deque = deque(maxlen=30)   # game events
_chat_buf: deque = deque(maxlen=40)   # chat input/output
_team_info: list = []                  # team Pokemon data


def update(**kw):           _state.update(kw)
def set_team(data: list):
    global _team_info
    _team_info = data


def add_log(msg: str):
    ts = datetime.now().strftime("%H:%M:%S")
    _log_buf.append(f"[dim]{ts}[/dim] {msg}")


def add_chat(msg: str):
    """Riêng cho chat input/output — không lẫn với game log."""
    ts = datetime.now().strftime("%H:%M:%S")
    _chat_buf.append(f"[dim]{ts}[/dim] {msg}")


# ── Snapshot cho Web UI ─────────────────────────────────────────────────────
import re as _re
_MARKUP_RE = _re.compile(r"\[/?[a-zA-Z_][^\[\]]*\]")


def _strip_markup(s: str) -> str:
    return _MARKUP_RE.sub("", s)


def snapshot() -> dict:
    """Trạng thái đầy đủ, JSON-serializable — Web UI dùng."""
    keys = [
        "zone", "tile", "screen", "action", "reason",
        "pokemon", "variant", "poke_level", "moves", "moves_detail", "is_rare",
        "enemy_hp", "enemy_hp_pct", "enemy_img",
        "my_name", "my_level", "my_hp", "my_hp_pct", "my_img", "battle_phase",
        "battles", "wins", "losses", "catches", "errors", "llm_calls",
        "paused", "pause_reason", "recommendation",
    ]
    st = {k: _state.get(k) for k in keys}
    st["uptime"] = int(time.time() - _state["start_time"])
    try:
        from config import config as _cfg
        st["mode"] = _cfg.MODE
    except Exception:
        st["mode"] = None
    return {
        "state": st,
        "team":  _team_info,
        "logs":  [_strip_markup(x) for x in list(_log_buf)[-30:]],
        "chats": [_strip_markup(x) for x in list(_chat_buf)[-40:]],
    }


# ── Bar helper ──────────────────────────────────────────────────────────────
def _bar(pct: float, w: int = 10) -> str:
    color  = "red" if pct > 85 else "yellow" if pct > 60 else "green"
    filled = int(pct / 100 * w)
    return f"[{color}]{'#'*filled}{'-'*(w-filled)}[/{color}] {pct:.0f}%"


def _tc(t: int) -> str:
    return "red" if t >= 80 else "yellow" if t >= 70 else "green"


# ── Panels ─────────────────────────────────────────────────────────────────
def _agent_panel() -> Panel:
    sc = {"map": "cyan", "encounter": "yellow", "battle": "red", "starting": "dim"}
    t = Table.grid(padding=(0, 1))
    t.add_column(style="bright_blue", min_width=9)
    t.add_column(min_width=16)
    t.add_row("Zone",   _state["zone"] or "?")
    tile = _state["tile"]
    t.add_row("Tile",   f"{tile[0]},{tile[1]}" if tile else "?")
    t.add_row("Screen", f"[{sc.get(_state['screen'],'white')}]{_state['screen']}[/{sc.get(_state['screen'],'white')}]")
    t.add_row("Action", f"[bold]{_state['action']}[/bold]")
    r = (_state.get("reason") or "")[:26]
    if r: t.add_row("", f"[dim italic]{r}[/dim italic]")
    el = int(time.time() - _state["start_time"])
    t.add_row("Uptime", f"{el//3600:02d}:{(el%3600)//60:02d}:{el%60:02d}")
    st = "[bold red]|| PAUSED[/bold red]" if _state["paused"] else "[bold green]> RUNNING[/bold green]"
    t.add_row("Status", st)
    return Panel(t, title="[bold cyan]AGENT[/bold cyan]", border_style="cyan")


def _encounter_panel() -> Panel:
    poke    = _state["pokemon"]
    variant = _state["variant"]
    level   = _state["poke_level"]
    moves   = _state["moves"] or []
    is_rare = _state["is_rare"]

    t = Table.grid(padding=(0, 1))
    t.add_column(style="bright_magenta", min_width=9)
    t.add_column(min_width=18)

    if poke:
        v_str = f"[bold yellow]{variant.upper()}[/bold yellow] " if variant else ""
        name_display = f"{v_str}[bold white]{poke}[/bold white]"
        if is_rare:
            t.add_row("", "[blink bold yellow]!! RARE POKEMON !![/blink bold yellow]")
        t.add_row("Pokemon", name_display)
        t.add_row("Level",   str(level) if level else "?")
        if moves:
            for i, m in enumerate(moves[:4]):
                t.add_row("Move" if i == 0 else "", f"[cyan]{m}[/cyan]")
    else:
        t.add_row("", "[dim italic]Chua gap Pokemon[/dim italic]")

    return Panel(t, title="[bold magenta]ENCOUNTER[/bold magenta]", border_style="magenta")


def _stats_panel() -> Panel:
    b   = _state["battles"]
    w   = _state["wins"]
    win = f"{w/b*100:.0f}%" if b > 0 else "N/A"
    t = Table.grid(padding=(0, 1))
    t.add_column(style="green", min_width=9)
    t.add_column(min_width=10)
    t.add_row("Battles", str(b))
    t.add_row("Wins",    f"[green]{w}[/green] ({win})")
    t.add_row("Catches", f"[cyan]{_state['catches']}[/cyan]")
    t.add_row("Errors",  f"[red]{_state['errors']}[/red]" if _state["errors"] else "0")
    t.add_row("LLM",     f"[dim]{_state['llm_calls']} calls[/dim]")
    return Panel(t, title="[bold green]STATS[/bold green]", border_style="green")


def _system_panel(hw: dict) -> Panel:
    t = Table.grid(padding=(0, 1))
    t.add_column(style="bright_yellow", min_width=5)
    t.add_column(min_width=24)
    t.add_row("CPU",  f"{_bar(hw['cpu_pct'])}  [dim]{hw['cpu_pct']}%[/dim]")
    t.add_row("RAM",  f"{_bar(hw['ram_pct'], 8)}  {hw['ram_used_gb']:.1f}/{hw['ram_total_gb']:.0f}GB")
    if NVML_OK:
        tc = _tc(hw["gpu_temp_c"])
        t.add_row("GPU",  f"{_bar(hw['gpu_pct'])}  [{tc}]{hw['gpu_temp_c']}C[/{tc}]")
        vp = hw["vram_used_gb"] / hw["vram_total_gb"] * 100 if hw["vram_total_gb"] else 0
        t.add_row("VRAM", f"{_bar(vp, 8)}  {hw['vram_used_gb']:.1f}/{hw['vram_total_gb']:.0f}GB")
        t.add_row("", f"[dim]{get_gpu_name()[:28]}[/dim]")
    else:
        t.add_row("", "[dim]GPU: N/A[/dim]")
    return Panel(t, title="[bold yellow]SYSTEM[/bold yellow]", border_style="yellow")


def _team_panel() -> Panel:
    t = Table(show_header=True, header_style="bold bright_blue", box=None, padding=(0, 1))
    t.add_column("#",       style="dim",          width=2)
    t.add_column("Pokemon", style="bold white",   min_width=22)
    t.add_column("Lv",      style="cyan",         width=4)
    t.add_column("Attacks",  style="green",       min_width=40)

    if not _team_info:
        t.add_row("", "[dim italic]Loading... (tu dong lay khi khoi dong)[/dim italic]", "", "")
    else:
        for i, pk in enumerate(_team_info[:6], 1):
            name    = pk.get("name") or "?"
            level   = str(pk.get("level") or "?")
            moves   = pk.get("moves") or []
            move_str = " / ".join(moves[:4]) if moves else "[dim]?[/dim]"
            t.add_row(str(i), name, level, move_str)

    return Panel(t, title="[bold bright_blue]DOI HINH POKEMON[/bold bright_blue]", border_style="bright_blue")


def _log_panel() -> Panel:
    txt = Text()
    for line in list(_log_buf)[-14:]:
        try:    txt.append_text(Text.from_markup(line))
        except: txt.append(line)
        txt.append("\n")
    return Panel(txt, title="[bold]GAME LOG[/bold]", border_style="dim")


def _chat_panel() -> Panel:
    txt = Text()
    lines = list(_chat_buf)[-15:]
    if not lines:
        txt.append("[dim]Chat chua co gi...[/dim]\n")
    else:
        for line in lines:
            try:    txt.append_text(Text.from_markup(line))
            except: txt.append(line)
            txt.append("\n")
    # Hiển thị input buffer — user gõ gì thì thấy ở đây
    buf = _state.get("input_buf", "")
    txt.append("\n")
    if buf:
        txt.append_text(Text.from_markup(f"[bold bright_green]> {buf}[bold blink]_[/bold blink][/bold bright_green]"))
    else:
        txt.append_text(Text.from_markup("[dim]> [blink]_[/blink][/dim]"))
    return Panel(txt, title="[bold bright_green]CHAT[/bold bright_green]", border_style="bright_green")


def _footer_panel() -> Panel:
    t = Text(justify="center")
    paused = _state.get("paused")
    reason = _state.get("pause_reason")
    if paused and reason == "rare":
        t.append("  !! RARE PAUSE !!  gõ: ", style="bold yellow")
        t.append("catch", style="bold cyan"); t.append(" / ")
        t.append("battle", style="bold red"); t.append(" / ")
        t.append("skip", style="dim"); t.append("   |   ")
    elif paused:
        t.append("  || PAUSED   gõ: ", style="bold red")
        t.append("resume", style="bold green"); t.append("   |   ")
    else:
        t.append("  > ", style="dim")
    cmds = ["status","pause","resume","stop","catch","skip","team","mode battle|catch|both","help"]
    t.append("  ".join(cmds), style="dim")
    return Panel(t, style="dim", height=3)


# ── Layout ─────────────────────────────────────────────────────────────────
def _build(hw: dict) -> Layout:
    lo = Layout()
    lo.split_column(
        Layout(name="top",    size=10),
        Layout(name="team",   size=10),
        Layout(name="middle", size=16),
        Layout(name="footer", size=3),
    )
    lo["top"].split_row(
        Layout(name="agent",     ratio=2),
        Layout(name="encounter", ratio=2),
        Layout(name="stats",     ratio=2),
        Layout(name="system",    ratio=3),
    )
    lo["middle"].split_row(
        Layout(name="log",  ratio=3),
        Layout(name="chat", ratio=2),
    )
    lo["top"]["agent"].update(_agent_panel())
    lo["top"]["encounter"].update(_encounter_panel())
    lo["top"]["stats"].update(_stats_panel())
    lo["top"]["system"].update(_system_panel(hw))
    lo["team"].update(_team_panel())
    lo["middle"]["log"].update(_log_panel())
    lo["middle"]["chat"].update(_chat_panel())
    lo["footer"].update(_footer_panel())
    return lo


# ── Dashboard task ─────────────────────────────────────────────────────────
async def run_dashboard():
    hw   = get_stats()
    tick = 0
    with Live(_build(hw), refresh_per_second=2, console=console,
              screen=False, vertical_overflow="visible") as live:
        while True:
            tick += 1
            if tick % 4 == 0:
                hw = get_stats()
            live.update(_build(hw))
            await asyncio.sleep(0.5)
