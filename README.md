# Pokémon Vortex Agent

A browser-automation agent for [Pokémon Vortex](https://www.pokemon-vortex.com) that auto-grinds
battles, catches Pokémon, and runs Battle Tower / Sidequests — with a type-effectiveness battle
engine, an LLM "brain" for strategy, and a live Pokémon-themed dashboard.

> ⚠️ **Disclaimer — read first.** This project is published for **educational purposes** (browser
> automation, async Python, game-AI experimentation). Automating Pokémon Vortex violates the game's
> Terms of Service and **can get your account banned**. Run it only against environments you are
> allowed to automate (a self-hosted clone, sandbox, or your own engine). The author is not
> responsible for any account action taken against you.

---

## Features

- **Type-aware battle engine** — reads the live battle DOM, ranks moves by effectiveness × power ×
  STAB, and counter-picks team members against the opponent (full type chart + PokéAPI lookups).
- **Multiple modes** — wild-battle grind, catching, Season Battle Tower, and Sidequests.
- **LLM brain (optional)** — uses a local [Ollama](https://ollama.com) model (`qwen3`) for battle
  decisions, post-loss lessons, and team optimization. Falls back to rule-based play.
- **Live dashboards** — a Rich terminal UI and a FastAPI + WebSocket web dashboard
  (`http://127.0.0.1:8770`) with sprites, HP bars, win-rate stats, and a chat box.
- **GPU rest scheduler** — automatically pauses for ~2 hours after a randomized number of battles
  (230–300) to let your hardware cool down.
- **SQLite memory** — tracks encounters, win/loss stats, and learned lessons across runs.

## Requirements

- Python 3.11+ (developed on 3.14)
- [Ollama](https://ollama.com) running locally with a `qwen3` model *(optional — only for LLM features)*
- Windows / macOS / Linux

## Installation

### Windows (PowerShell)

```powershell
git clone https://github.com/DuyhocAI/Pokemon_Vortex_AutoBattle.git
cd Pokemon_Vortex_AutoBattle
./setup.ps1          # creates venv, installs deps + Playwright Chromium, copies .env
```

### Manual (any OS)

```bash
python -m venv venv
# Windows:  venv\Scripts\activate
# Linux/macOS:  source venv/bin/activate

pip install -r requirements.txt
playwright install chromium

cp .env.example .env   # then edit credentials
```

## Configuration

Edit `.env` (see [`.env.example`](.env.example) for all options):

| Variable | Default | Description |
|---|---|---|
| `VORTEX_USERNAME` / `VORTEX_PASSWORD` | — | Your login |
| `MODE` | `battle` | `battle` \| `catch` \| `both` \| `tower` \| `sidequest` |
| `MAX_BATTLES` | `0` | Stop after N battles (0 = unlimited) |
| `PREFERRED_MOVE` | `1` | Default move slot (1–4) |
| `HEADLESS` | `false` | Hide the browser window |
| `REST_AFTER_MIN` / `REST_AFTER_MAX` | `230` / `300` | Battles before a GPU rest (randomized) |
| `REST_HOURS` | `2.0` | Length of each rest |
| `WEB_PORT` | `8770` | Web dashboard port |

## Usage

```bash
python main.py
```

A menu lets you pick the mode at startup (or press Enter to use `MODE` from `.env`). Open the web
dashboard at **http://127.0.0.1:8770** to watch battles and send chat commands
(`status`, `team`, `stats`, `optimize`, `pause`, `resume`, …).

## Project structure

```
agent/
  battle.py         Battle engine (live DOM: move select, attack, HP/level parsing)
  battle_tower.py   Season Battle Tower loop
  sidequest.py      Sidequests loop
  grind_loop.py     Wild-battle / catch grind loop
  pokedex.py        Type chart + PokéAPI lookups + move/team ranking
  brain.py          Game-state collection + action execution
  llm.py            Ollama LLM brain (decisions, lessons, team optimizer)
  memory.py         SQLite (encounters, stats, lessons, pokedex cache)
  team.py           Team fetching
  map_nav.py        Map navigation
  ui.py             Rich terminal dashboard + shared state
  webui.py          FastAPI + WebSocket web dashboard
  utils.py          Helpers + RestScheduler
config.py           Settings (loaded from .env)
main.py             Entry point + mode menu
web/                Web dashboard frontend (HTML/CSS/JS)
```

## License

MIT — see [LICENSE](LICENSE). Provided as-is for educational use.
