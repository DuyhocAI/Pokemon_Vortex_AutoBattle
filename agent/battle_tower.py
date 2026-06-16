"""
Season Battle Tower loop.
URL: https://www.pokemon-vortex.com/season-battle-tower/

Flow (dùng chung battle engine với grind — agent/battle.py):
  1. tower_overview : đọc 6 Pokemon đối thủ, LLM lập chiến thuật (chạy nền), click Battle!
  2. phase=choose   : chọn Pokemon ra trận theo tương khắc hệ (pokedex.rank_team_choices)
  3. phase=select   : chọn move hiệu quả nhất (type thật từ typeImg DOM) + Attack
  4. phase=result   : đọc kết quả lượt, Continue
  5. end            : ghi nhận thắng/thua → quay lại tower → trận tiếp theo

Chống treo: battle.py tự re-enable + resubmit khi AJAX fail (site disable nút submit
và không retry); tower loop có watchdog reload nếu state đứng yên quá lâu.
"""
import asyncio
import random
import re
import httpx
from loguru import logger
from playwright.async_api import Page
from agent.chat import process_commands, agent_state
from agent.team import get_cached_team
from agent.battle import (
    read_battle_state, select_move_and_attack, click_continue,
    select_pokemon_and_start, reset_battle_tracking,
)
from agent.pokedex import rank_team_choices
from agent.utils import RestScheduler
from config import config
import agent.ui as ui
import agent.llm as llm_module

# Runtime type cache — populated via PokeAPI/SQLite on-demand
_TYPE_CACHE: dict[str, list[str]] = {}

TOWER_URL = "https://www.pokemon-vortex.com/season-battle-tower/"

# Type chart: ATTACKER_TYPE -> {DEFENDER_TYPE: multiplier}
TYPE_CHART: dict[str, dict[str, float]] = {
    "Normal":   {"Rock":0.5,"Steel":0.5,"Ghost":0},
    "Fire":     {"Fire":0.5,"Water":0.5,"Rock":0.5,"Dragon":0.5,"Grass":2,"Ice":2,"Bug":2,"Steel":2},
    "Water":    {"Water":0.5,"Grass":0.5,"Dragon":0.5,"Fire":2,"Ground":2,"Rock":2},
    "Grass":    {"Fire":0.5,"Grass":0.5,"Poison":0.5,"Flying":0.5,"Bug":0.5,"Dragon":0.5,"Steel":0.5,"Water":2,"Ground":2,"Rock":2},
    "Electric": {"Grass":0.5,"Electric":0.5,"Dragon":0.5,"Ground":0,"Water":2,"Flying":2},
    "Ice":      {"Fire":0.5,"Water":0.5,"Ice":0.5,"Steel":0.5,"Grass":2,"Ground":2,"Flying":2,"Dragon":2},
    "Fighting": {"Poison":0.5,"Flying":0.5,"Psychic":0.5,"Bug":0.5,"Fairy":0.5,"Ghost":0,"Normal":2,"Ice":2,"Rock":2,"Dark":2,"Steel":2},
    "Poison":   {"Poison":0.5,"Ground":0.5,"Rock":0.5,"Ghost":0.5,"Steel":0,"Grass":2,"Fairy":2},
    "Ground":   {"Grass":0.5,"Bug":0.5,"Flying":0,"Fire":2,"Electric":2,"Poison":2,"Rock":2,"Steel":2},
    "Flying":   {"Electric":0.5,"Rock":0.5,"Steel":0.5,"Grass":2,"Fighting":2,"Bug":2},
    "Psychic":  {"Psychic":0.5,"Steel":0.5,"Dark":0,"Fighting":2,"Poison":2},
    "Bug":      {"Fire":0.5,"Fighting":0.5,"Flying":0.5,"Ghost":0.5,"Steel":0.5,"Fairy":0.5,"Grass":2,"Psychic":2,"Dark":2},
    "Rock":     {"Fighting":0.5,"Ground":0.5,"Steel":0.5,"Fire":2,"Ice":2,"Flying":2,"Bug":2},
    "Ghost":    {"Normal":0,"Dark":0.5,"Ghost":2,"Psychic":2},
    "Dragon":   {"Steel":0.5,"Fairy":0,"Dragon":2},
    "Dark":     {"Fighting":0.5,"Dark":0.5,"Fairy":0.5,"Ghost":2,"Psychic":2},
    "Steel":    {"Fire":0.5,"Water":0.5,"Electric":0.5,"Steel":0.5,"Ice":2,"Rock":2,"Fairy":2},
    "Fairy":    {"Fire":0.5,"Poison":0.5,"Steel":0.5,"Fighting":2,"Dragon":2,"Dark":2},
}

# Base types cho tung loai Pokemon (bo variant prefix) — chi la cache cung pho bien
POKEMON_TYPES: dict[str, list[str]] = {
    "Lunatone": ["Psychic","Rock"],
    "Archaludon": ["Steel","Dragon"],
    "Chimecho": ["Psychic"],
    "Starmie": ["Water","Psychic"],
    "Illumise": ["Bug"],
    "Excadrill": ["Steel","Ground"],
    "Garchomp": ["Dragon","Ground"],
    "Gallade": ["Psychic","Fighting"],
    "Aegislash": ["Steel","Ghost"],
    "Gyarados": ["Water","Flying"],
    "Charizard": ["Fire","Dragon"],  # Mega X = Fire/Dragon
    "Luxray": ["Electric"],
    "Pikachu":["Electric"], "Eevee":["Normal"], "Snorlax":["Normal"],
    "Gengar":["Ghost","Poison"], "Mewtwo":["Psychic"], "Dragonite":["Dragon","Flying"],
    "Tyranitar":["Rock","Dark"], "Salamence":["Dragon","Flying"], "Metagross":["Steel","Psychic"],
    "Rayquaza":["Dragon","Flying"], "Dialga":["Steel","Dragon"], "Palkia":["Water","Dragon"],
    "Giratina":["Ghost","Dragon"], "Reshiram":["Dragon","Fire"], "Zekrom":["Dragon","Electric"],
    "Kyurem":["Dragon","Ice"], "Xerneas":["Fairy"], "Yveltal":["Dark","Flying"],
    "Sylveon":["Fairy"], "Mimikyu":["Ghost","Fairy"], "Toxapex":["Poison","Water"],
    "Ferrothorn":["Grass","Steel"], "Rotom":["Electric","Ghost"], "Togekiss":["Fairy","Flying"],
    "Charizard (Mega X)":["Fire","Dragon"], "Charizard (Mega Y)":["Fire","Flying"],
    "Aegislash (Shield)":["Steel","Ghost"], "Aegislash (Blade)":["Steel","Ghost"],
}

VARIANT_PREFIXES = {"dark","shiny","mystic","shadow","metallic"}


def _base_name(pokemon_name: str) -> str:
    """Strip variant prefix: 'Dark Lunatone' -> 'Lunatone'."""
    parts = pokemon_name.strip().split()
    if parts and parts[0].lower() in VARIANT_PREFIXES:
        return " ".join(parts[1:])
    return pokemon_name.strip()


def _get_types(name: str) -> list[str]:
    """Get Pokemon types: runtime cache → pokedex SQLite → static dict → [] (neutral 1.0x)."""
    clean = _base_name(name)
    for key in [name, clean]:
        if key in _TYPE_CACHE:
            return _TYPE_CACHE[key]
    for key in [name, clean]:
        if key in POKEMON_TYPES:
            return POKEMON_TYPES[key]
    for key, types in POKEMON_TYPES.items():
        if clean.lower().startswith(key.lower()) or key.lower() in clean.lower():
            return types
    # Check pokedex SQLite (populated by pokedex.lookup in grind mode or prior battles)
    try:
        from agent.memory import get_pokedex
        for key in [name, clean]:
            cached = get_pokedex(key)
            if cached and cached.get("types"):
                _TYPE_CACHE[name] = cached["types"]
                _TYPE_CACHE[clean] = cached["types"]
                return cached["types"]
    except Exception:
        pass
    logger.warning(f"[TYPES] Still unknown: {name}")
    return []  # Neutral: all moves = 1.0x (tốt hơn giả "Normal" sai)


def _pokeapi_name(base: str) -> str:
    """Convert Pokemon base name to PokeAPI URL slug."""
    s = base.lower().strip()
    s = re.sub(r"\s*\(([^)]+)\)", lambda m: "-" + m.group(1).lower().replace(" ", "-"), s)
    s = s.replace(" ", "-").replace("'", "").replace(".", "").replace(":", "")
    return s


async def _fetch_pokemon_types(name: str) -> list[str]:
    """Fetch types from PokeAPI. name = base name (no variant prefix)."""
    slug = _pokeapi_name(name)
    attempts = [slug, slug.split("-")[0]]  # full form, then base species
    for attempt in attempts:
        try:
            async with httpx.AsyncClient(timeout=8.0, follow_redirects=True) as client:
                resp = await client.get(
                    f"https://pokeapi.co/api/v2/pokemon/{attempt}",
                    headers={"User-Agent": "Mozilla/5.0"},
                )
                if resp.status_code == 200:
                    data = resp.json()
                    types = [t["type"]["name"].capitalize() for t in data["types"]]
                    logger.info(f"[TYPES] PokeAPI: {name} ({attempt}) → {types}")
                    return types
        except Exception as e:
            logger.debug(f"[TYPES] PokeAPI {attempt}: {e}")
    logger.warning(f"[TYPES] Could not fetch types for: {name}")
    return []


async def _ensure_types(pokemon_names: list[str]):
    """Pre-fetch types — dùng pokedex.lookup() (SQLite cache) trước, fallback PokeAPI."""
    from agent.pokedex import lookup as pokedex_lookup
    unknown = []
    for name in pokemon_names:
        if not name:
            continue
        clean = _base_name(name)
        if any(k in _TYPE_CACHE or k in POKEMON_TYPES for k in [name, clean]):
            continue
        if any(
            clean.lower().startswith(k.lower()) or k.lower() in clean.lower()
            for k in POKEMON_TYPES
        ):
            continue
        unknown.append(name)

    if not unknown:
        return

    for name in unknown:
        clean = _base_name(name)
        try:
            info = await pokedex_lookup(clean)
            if info and info.get("types"):
                _TYPE_CACHE[name] = info["types"]
                _TYPE_CACHE[clean] = info["types"]
                logger.info(f"[TYPES] {name} → {info['types']}")
                continue
        except Exception as e:
            logger.debug(f"[TYPES] pokedex_lookup {clean}: {e}")
        types = await _fetch_pokemon_types(clean)
        if types:
            _TYPE_CACHE[name] = types
            _TYPE_CACHE[clean] = types
        else:
            logger.warning(f"[TYPES] Unknown: {name}")


def _move_effectiveness(move_type: str | None, defender_types: list[str]) -> float:
    """Calculate total effectiveness multiplier."""
    chart = TYPE_CHART.get(move_type or "", {})
    mult = 1.0
    for dt in defender_types:
        mult *= chart.get(dt, 1.0)
    return mult


def _best_move(moves_with_types: list[tuple[str, str | None]], defender_types: list[str]) -> tuple[int, float]:
    """Returns (best_move_index, effectiveness) given list of (moveName, moveType)."""
    if not moves_with_types:
        return 0, 1.0
    best_idx = 0
    best_eff = _move_effectiveness(moves_with_types[0][1], defender_types)
    for i, (_, mtype) in enumerate(moves_with_types[1:], 1):
        eff = _move_effectiveness(mtype, defender_types)
        if eff > best_eff:
            best_eff, best_idx = eff, i
    return best_idx, best_eff


# ── Read Tower Page ─────────────────────────────────────────────────────────

async def read_tower_page(page: Page) -> dict:
    """Doc thong tin trang Season Battle Tower."""
    data = await page.evaluate(r"""() => {
        let opponentName = '';
        const allSpans = Array.from(document.querySelectorAll('span.color-maroon, span.text-bold'));
        for (const sp of allSpans) {
            const t = (sp.textContent || '').trim();
            if (t.length > 2 && t.length < 40 && !t.includes(':') && !t.includes('Points')) {
                opponentName = t;
                break;
            }
        }

        const slots = Array.from(document.querySelectorAll('div.battleTowerSlot'));
        const opponentPokemon = slots.map(slot => {
            const nameEl = slot.querySelector('h4');
            const name   = (nameEl?.innerText || nameEl?.textContent || '').trim();
            const text   = slot.innerText || '';
            const lvMatch  = text.match(/Level:\s*(\d+)/i);
            const img = slot.querySelector('img');
            return {
                name,
                level: lvMatch ? parseInt(lvMatch[1]) : 100,
                imgSrc: img?.src || '',
            };
        }).filter(p => p.name);

        opponentName = opponentName.replace(/next\s+opponent\s*[-–]\s*/i, '').trim();
        return { opponentName, opponentPokemon };
    }""")
    return data


async def _detect_battle_screen(page: Page) -> str:
    """'tower_overview' | 'battle' | 'map'"""
    url = page.url.lower()
    if "season-battle-tower" in url:
        if await page.locator("button:text('Battle!')").count() > 0:
            return "tower_overview"
        if await page.locator("#attackForm, #attackSelection, #pokeChoose").count() > 0:
            return "battle"
        return "tower_overview"
    if "/battle" in url:
        return "battle"
    return "map"


# ── LLM Strategy (chạy nền, không block battle) ─────────────────────────────

# Move types thật (tra PokeAPI qua agent.pokedex, fill bởi _ensure_move_types)
_MOVE_TYPE_LOOKUP: dict[str, str] = {}


async def _ensure_move_types(team: list[dict]):
    """Tra type THẬT của mọi move trong đội từ PokeAPI (cache SQLite) — thay vì đoán tên."""
    from agent.pokedex import moves_types
    all_moves = []
    for pk in team or []:
        for mv in pk.get("moves", []) or []:
            if mv and mv.lower() not in (k.lower() for k in _MOVE_TYPE_LOOKUP):
                all_moves.append(mv)
    if not all_moves:
        return
    try:
        found = await moves_types(all_moves)
        _MOVE_TYPE_LOOKUP.update(found)
    except Exception as e:
        logger.debug(f"[TOWER] _ensure_move_types: {e}")


def _infer_move_type(move_name: str) -> str:
    """Type của move: ưu tiên dữ liệu thật từ PokeAPI, fallback heuristic theo tên."""
    for key, mtype in _MOVE_TYPE_LOOKUP.items():
        if key.lower() == move_name.lower():
            return mtype
    name = move_name.lower()
    if any(w in name for w in ["flare","fire","flame","ember","overheat","heat"]):
        return "Fire"
    if any(w in name for w in ["hydro","aqua","water","surf","rain","torrent"]):
        return "Water"
    if any(w in name for w in ["thunder","discharge","wild charge","volt","electric","spark","zap"]):
        return "Electric"
    if any(w in name for w in ["psycho","psychic","psych","psybeam","extrasensory"]):
        return "Psychic"
    if any(w in name for w in ["shadow","phantom","ghost","hex","will-o","ominous"]):
        return "Ghost"
    if any(w in name for w in ["dragon","outrage","draco","spacial","twister"]):
        return "Dragon"
    if any(w in name for w in ["dark","crunch","night","pursuit","payback","bite","thief","snatch"]):
        return "Dark"
    if any(w in name for w in ["close combat","aura sphere","fighting","brick","low kick","mach","cross chop","sacred sword"]):
        return "Fighting"
    if any(w in name for w in ["ice","blizzard","freeze","icicle","frost","aurora","powder snow"]):
        return "Ice"
    if any(w in name for w in ["earth","quake","dig","mud","ground","sand tomb","fissure"]):
        return "Ground"
    if any(w in name for w in ["iron","steel","meteor","flash cannon","gyro","metal","mirror shot"]):
        return "Steel"
    if any(w in name for w in ["aerial","wing","fly","feather","air","gust","hurricane","brave bird"]):
        return "Flying"
    if any(w in name for w in ["stone","rock","rollout","avalanche","ancient","smash","power gem"]):
        return "Rock"
    if any(w in name for w in ["poison","toxic","sludge","acid","venoshock"]):
        return "Poison"
    if any(w in name for w in ["grass","leaf","petal","solar","energy ball","giga drain"]):
        return "Grass"
    return "Normal"


async def _plan_strategy(opponent_team: list[dict]) -> str:
    """Goi LLM phan tich type matchup va de xuat chien thuat."""
    our_team = get_cached_team()
    if not our_team:
        return "No team data. Use default battle order."

    our_info = [
        {"name": pk["name"], "types": _get_types(pk["name"]),
         "moves": pk.get("moves", []), "level": pk.get("level")}
        for pk in our_team
    ]
    opp_info = [
        {"name": pk["name"], "types": _get_types(pk["name"]), "level": pk.get("level", 100)}
        for pk in opponent_team
    ]

    matchups = []
    for opp in opp_info:
        best_attacker, best_eff, best_move_name = None, 0.0, ""
        for our in our_info:
            for move_name in our.get("moves", []):
                mtype = _infer_move_type(move_name)
                eff = _move_effectiveness(mtype, opp["types"])
                if eff > best_eff:
                    best_eff, best_attacker = eff, our["name"]
                    best_move_name = f"{move_name} ({mtype})"
        matchups.append(
            f"  {opp['name']} ({'/'.join(opp['types']) or '?'}): "
            f"best counter={best_attacker or 'any'} move={best_move_name} {best_eff:.1f}x"
        )

    our_lines = [f"- {p['name']} ({'/'.join(p['types']) or '?'}) moves: {', '.join(p['moves'][:4])}"
                 for p in our_info]
    opp_lines = [f"- {p['name']} ({'/'.join(p['types']) or '?'}) Lv{p['level']}" for p in opp_info]

    prompt = (
        "OUR TEAM:\n" + "\n".join(our_lines) +
        "\n\nOPPONENT TEAM:\n" + "\n".join(opp_lines) +
        "\n\nTYPE MATCHUPS:\n" + "\n".join(matchups) +
        "\n\nGive a 4-6 line battle strategy: which of our Pokemon to lead with, "
        "key type advantages to exploit, and any threats to watch."
    )
    result = await llm_module.llm_analyze(prompt)
    return result or "Use type advantages. Lead with best type matchup."


def _start_strategy_task(opponent_team: list[dict]) -> None:
    """Chạy LLM strategy ở background — không block việc bắt đầu trận."""
    async def _run():
        try:
            strategy = await _plan_strategy(opponent_team)
            for line in strategy.split("\n")[:6]:
                line = line.strip()
                if line:
                    ui.add_log(f"[green dim]{line[:80]}[/green dim]")
        except Exception as e:
            logger.debug(f"[TOWER] strategy bg: {e}")
    asyncio.create_task(_run())


# ── Network evidence: log battle POST responses (chẩn đoán rate-limit) ──────

def _attach_network_logger(page: Page):
    if getattr(page, "_tower_net_logged", False):
        return
    page._tower_net_logged = True

    async def _log_resp(resp):
        try:
            body = await resp.body()
            n = len(body)
            if resp.status != 200 or n < 50:
                logger.warning(f"[NET] POST battle → {resp.status} ({n}B) {resp.url[-40:]}")
            else:
                logger.debug(f"[NET] POST battle → {resp.status} ({n}B)")
        except Exception:
            pass

    def _on_response(resp):
        try:
            if resp.request.method == "POST" and "/battle" in resp.url:
                asyncio.create_task(_log_resp(resp))
        except Exception:
            pass

    def _on_failed(req):
        try:
            if req.method == "POST" and "/battle" in req.url:
                logger.warning(f"[NET] POST battle FAILED: {req.failure} {req.url[-40:]}")
        except Exception:
            pass

    def _on_request(req):
        # Phân định: request có được BẮN đi không (pending vô hạn vẫn hiện ở đây)
        try:
            if req.method == "POST" and "/battle" in req.url:
                logger.info(f"[NET] → POST battle gửi đi {req.url[-40:]}")
        except Exception:
            pass

    page.on("request", _on_request)
    page.on("response", _on_response)
    page.on("requestfailed", _on_failed)


async def _human_delay(lo: float = 1.0, hi: float = 2.2):
    """Delay ngẫu nhiên giống người chơi — tránh server rate-limit."""
    await asyncio.sleep(random.uniform(lo, hi))


# ── Main Tower Loop ──────────────────────────────────────────────────────────

async def run_tower(page: Page, max_wins: int = 0):
    """Main loop cho Season Battle Tower. max_wins > 0 → dừng khi đủ số trận thắng."""
    wins = losses = total = 0
    current_opp_team: list[dict] = []
    opp_defeated_count = 0
    battle_started = False
    battle_flags = {"won": False, "lost": False, "last_output": ""}
    stuck_sig, stuck_count = None, 0
    failed_choose: set[str] = set()   # các pokemon_id chọn bị server từ chối trong trận này
    throttle_fails = 0                # số lần liên tiếp server không phản hồi POST

    def _reset_battle():
        nonlocal opp_defeated_count, battle_started, battle_flags, stuck_sig, stuck_count
        opp_defeated_count = 0
        battle_started = False
        battle_flags = {"won": False, "lost": False, "last_output": ""}
        stuck_sig, stuck_count = None, 0
        failed_choose.clear()
        reset_battle_tracking()

    cooldown_round = 0  # tăng dần nếu cooldown trước không giúp được

    async def _throttle_cooldown():
        """Server ngừng trả lời battle POST (rate-limit) → nghỉ lũy tiến rồi quay lại trận."""
        nonlocal throttle_fails, cooldown_round
        battle_url = page.url
        # Backoff lũy tiến: ~90s → ~3min → ~8min (cap)
        base = [80, 180, 480][min(cooldown_round, 2)]
        wait_s = base * random.uniform(0.9, 1.2)
        cooldown_round += 1
        logger.warning(f"[TOWER] Nghi bị rate-limit — nghỉ {wait_s:.0f}s (lần {cooldown_round}) rồi quay lại")
        ui.add_log(f"[yellow]⏸ Server không phản hồi — nghỉ {wait_s:.0f}s (lần {cooldown_round})[/yellow]")
        try:
            await page.goto(TOWER_URL, wait_until="domcontentloaded")
        except Exception:
            pass
        await asyncio.sleep(wait_s)
        try:
            await page.goto(battle_url, wait_until="domcontentloaded")
        except Exception:
            pass
        await asyncio.sleep(1.5)
        throttle_fails = 0

    async def _finalize_battle():
        """Ghi nhận kết quả trận (gọi khi battle DOM biến mất hoặc thấy win/lose text)."""
        nonlocal wins, losses, total, battle_started
        if not battle_started:
            return
        battle_started = False
        total += 1
        n_opp = len(current_opp_team) or 6
        if battle_flags["lost"]:
            losses += 1
            ui.add_log(f"[bold red]💀 THUA. Losses: {losses}[/bold red]")
            logger.info(f"[TOWER] DEFEAT — total {wins}W/{losses}L")
        elif battle_flags["won"] or opp_defeated_count >= n_opp:
            wins += 1
            ui.add_log(f"[bold green]🏆 THẮNG #{wins}! ({opp_defeated_count}/{n_opp} đối thủ bị hạ)[/bold green]")
            logger.info(f"[TOWER] VICTORY #{wins} — total {wins}W/{losses}L")
        else:
            logger.warning(f"[TOWER] Trận kết thúc không rõ kết quả (defeated {opp_defeated_count}/{n_opp})")
        ui.update(wins=wins, losses=losses, battles=total)

    ui.add_log("[bold cyan]===== Season Battle Tower bắt đầu! =====[/bold cyan]")
    logger.info(f"[TOWER] Started (max_wins={max_wins or '∞'})")

    _attach_network_logger(page)
    await page.goto(TOWER_URL, wait_until="domcontentloaded")
    await asyncio.sleep(1.5)

    rest = RestScheduler(config.REST_AFTER_MIN, config.REST_AFTER_MAX,
                         config.REST_HOURS, config.REST_ENABLED, tag="TOWER")

    while True:
        if not await process_commands({"battles": total, "wins": wins, "losses": losses}):
            logger.info("[TOWER] Stopped by command")
            break
        if max_wins and wins >= max_wins:
            logger.info(f"[TOWER] Đạt mục tiêu {max_wins} trận thắng — dừng")
            break

        # Nghỉ ngơi cho GPU sau mỗi ~N trận (random)
        if await rest.maybe_rest(total, on_log=ui.add_log):
            try:
                await page.goto(TOWER_URL, wait_until="domcontentloaded")
                await asyncio.sleep(1.5)
            except Exception:
                pass
            continue

        try:
            # Session hết hạn (vd 2 phiên cùng account đá nhau) → tự đăng nhập lại
            if "/login" in page.url.lower():
                logger.warning("[TOWER] Session hết hạn — đăng nhập lại...")
                ui.add_log("[yellow]⚠ Session hết hạn — đang đăng nhập lại[/yellow]")
                from agent.login import login as _relogin
                if await _relogin(page):
                    await page.goto(TOWER_URL, wait_until="domcontentloaded")
                    await asyncio.sleep(1.5)
                else:
                    logger.error("[TOWER] Đăng nhập lại thất bại — thử lại sau 30s")
                    await asyncio.sleep(30)
                continue

            screen = await _detect_battle_screen(page)
            ui.update(screen="tower", zone="Season Battle Tower",
                      battles=total, wins=wins, action=screen)

            # ── 1. TOWER OVERVIEW ──
            if screen == "tower_overview":
                # Trận trước vừa xong mà chưa ghi nhận? (Continue đưa thẳng về overview)
                await _finalize_battle()
                _reset_battle()

                tower_data = await read_tower_page(page)
                opp_name = tower_data.get("opponentName", "Unknown")
                current_opp_team = tower_data.get("opponentPokemon", [])

                if not current_opp_team:
                    logger.warning("[TOWER] Khong doc duoc opponent team, reload...")
                    await asyncio.sleep(2)
                    await page.reload(wait_until="domcontentloaded")
                    await asyncio.sleep(1.5)
                    continue

                # Pre-fetch types (SQLite cache → nhanh từ lần 2)
                await _ensure_types([pk["name"] for pk in current_opp_team])
                await _ensure_move_types(get_cached_team())

                ui.add_log(f"[bold magenta]═══ Đối thủ: {opp_name} ═══[/bold magenta]")
                for pk in current_opp_team:
                    t = "/".join(_get_types(pk["name"])) or "?"
                    ui.add_log(f"  [yellow]{pk['name']}[/yellow] [{t}]")
                ui.update(pokemon=opp_name, variant=None, poke_level=100)

                # LLM strategy chạy NỀN — không chặn trận đấu
                _start_strategy_task(list(current_opp_team))

                # Click Battle! — nghỉ giữa các trận như người chơi (tránh rate-limit)
                ui.update(action="starting battle")
                await _human_delay(5.0, 10.0)
                try:
                    btn = page.locator("button:text('Battle!')")
                    if await btn.is_visible(timeout=3000):
                        await btn.click()
                        logger.info(f"[TOWER] Battle! clicked vs {opp_name}")
                    else:
                        await page.evaluate(
                            "document.querySelector('form[action*=\"battle\"]')?.submit()")
                except Exception as e:
                    logger.warning(f"[TOWER] Battle start: {e}")
                battle_started = True
                # Đợi màn chọn Pokemon / battle form xuất hiện (thay vì sleep mù)
                try:
                    await page.wait_for_selector("#pokeChoose, #attackSelection, #attackForm",
                                                 state="attached", timeout=20000)
                except Exception:
                    logger.warning("[TOWER] Battle form không xuất hiện sau 20s")
                await asyncio.sleep(0.5)

            # ── 2. BATTLE (engine chung với grind: battle.py) ──
            elif screen == "battle":
                bstate = await read_battle_state(page)
                phase = bstate.get("phase", "none")
                battle_started = True  # đang trong battle DOM

                # Watchdog: state đứng yên quá lâu → reload (battle resume server-side)
                sig = (phase,
                       (bstate.get("enemy") or {}).get("hp"),
                       (bstate.get("me") or {}).get("hp"),
                       (bstate.get("output") or "")[:60])
                if sig == stuck_sig:
                    stuck_count += 1
                else:
                    stuck_sig, stuck_count = sig, 0
                if stuck_count >= 4:
                    logger.warning(f"[TOWER] Stuck tại phase={phase} — reload trang")
                    ui.add_log("[yellow]⟳ Treo — reload lại trận[/yellow]")
                    await page.reload(wait_until="domcontentloaded")
                    stuck_sig, stuck_count = None, 0
                    await asyncio.sleep(1.5)
                    continue

                # Xử lý output mới (1 lần mỗi output)
                out = bstate.get("output") or ""
                ev = bstate.get("events") or {}
                if out and out != battle_flags["last_output"]:
                    battle_flags["last_output"] = out
                    for sent in out.split("."):
                        sent = sent.strip()
                        if sent:
                            color = "green" if "super effective" in sent.lower() else \
                                    "red"   if "fainted" in sent.lower()         else \
                                    "yellow" if "critical" in sent.lower()       else "white"
                            ui.add_log(f"  [{color}]{sent}.[/{color}]")
                    if ev.get("won"):
                        battle_flags["won"] = True
                    if ev.get("lost"):
                        battle_flags["lost"] = True
                    if ev.get("enemy_fainted"):
                        opp_defeated_count += 1
                        ui.add_log(f"  [bold green]Đối thủ bị hạ #{opp_defeated_count}[/bold green]")

                enemy = bstate.get("enemy") or {}
                me = bstate.get("me") or {}
                ui.update(
                    pokemon=enemy.get("name"), poke_level=enemy.get("level"),
                    enemy_hp=enemy.get("hp"), enemy_hp_pct=enemy.get("hp_pct"),
                    enemy_img=enemy.get("img"),
                    my_name=me.get("name"), my_level=me.get("level"),
                    my_hp=me.get("hp"), my_hp_pct=me.get("hp_pct"), my_img=me.get("img"),
                    battle_phase=phase,
                )

                # ── 2a. Chọn Pokemon ra trận theo tương khắc hệ ──
                if phase == "choose":
                    choices = bstate.get("choose_options", [])
                    if not choices:
                        await asyncio.sleep(1.0)
                        continue
                    # Loại các con server đã từ chối (fainted không bị đánh dấu...)
                    usable = [c for c in choices if str(c.get("value")) not in failed_choose]
                    if not usable:
                        failed_choose.clear()
                        usable = choices
                    idx = min(opp_defeated_count, len(current_opp_team) - 1) if current_opp_team else 0
                    enemy_next = enemy.get("name") \
                        or (current_opp_team[idx]["name"] if current_opp_team else None) \
                        or ((bstate.get("opponents") or [{}])[0].get("name")) or ""
                    await _ensure_types([enemy_next])
                    team_moves = {pk.get("name"): pk.get("moves", [])
                                  for pk in (get_cached_team() or [])}
                    ranked = await rank_team_choices(usable, team_moves, enemy_next)
                    if ranked:
                        top = ranked[0]
                    else:
                        alive = [c for c in usable if not c.get("fainted")]
                        top = alive[0] if alive else None
                    if top:
                        ui.add_log(
                            f"[cyan]Ra trận [bold]{top['name']}[/bold] vs {enemy_next} — "
                            f"{top.get('reason', '?')}[/cyan]")
                        logger.info(f"[TOWER] Send {top['name']} vs {enemy_next} ({top.get('reason','')})")
                        await _human_delay()
                        status = await select_pokemon_and_start(page, str(top["value"]))
                        if status == "ok":
                            throttle_fails = 0
                            cooldown_round = 0
                        elif status == "rejected":
                            failed_choose.add(str(top["value"]))
                            logger.warning(
                                f"[TOWER] Server từ chối {top['name']} — blacklist, thử con khác")
                        else:  # no_response → rate-limit, KHÔNG phải lỗi Pokemon
                            throttle_fails += 1
                            if throttle_fails >= 2:
                                await _throttle_cooldown()
                    else:
                        logger.warning("[TOWER] Không còn Pokemon nào chọn được")
                        await asyncio.sleep(1.0)

                # ── 2b. Chọn move + Attack ──
                elif phase == "select":
                    moves = bstate.get("moves", [])
                    if not moves:
                        await asyncio.sleep(1.0)
                        continue
                    enemy_name = enemy.get("name") or "?"
                    if not _get_types(enemy_name):
                        await _ensure_types([enemy_name])
                    opp_types = _get_types(enemy_name)
                    my_types = _get_types(me.get("name") or "")

                    # Xếp hạng theo DAMAGE thật: effectiveness × power × STAB
                    from agent.pokedex import rank_moves_full
                    ranked_moves = await rank_moves_full(moves, opp_types, my_types)
                    best = ranked_moves[0]
                    idx, eff = best["index"], best["multiplier"]
                    mv = moves[idx]
                    eff_label = ("SUPER EFFECTIVE" if eff >= 2 else
                                 "neutral" if eff == 1 else "not effective")
                    stab_label = " STAB" if best.get("stab") else ""
                    ui.update(action=f"{mv.get('name')} ({mv.get('type')})",
                              reason=f"{eff:.1f}x pow{best.get('power')}{stab_label}")
                    ui.add_log(
                        f"[cyan]{me.get('name')}[/cyan] HP:{me.get('hp')} vs "
                        f"[yellow]{enemy_name}[/yellow] HP:{enemy.get('hp')} "
                        f"[{'/'.join(opp_types) or '?'}] → "
                        f"[bold]{mv.get('name')}[/bold] ({mv.get('type')}, {eff:.1f}x, "
                        f"pow {best.get('power')}{stab_label})")
                    logger.info(
                        f"[TOWER] {me.get('name')}({me.get('hp')}HP) → {mv.get('name')} "
                        f"({mv.get('type')}, {eff:.1f}x, pow {best.get('power')}{stab_label}) "
                        f"vs {enemy_name}({enemy.get('hp')}HP)")

                    await _human_delay()
                    r = await select_move_and_attack(page, idx)
                    if r.get("ok"):
                        throttle_fails = 0
                        cooldown_round = 0
                    else:
                        throttle_fails += 1
                        logger.warning(f"[TOWER] Attack không phản hồi ({throttle_fails} lần)")
                        if throttle_fails >= 2:
                            await _throttle_cooldown()

                # ── 2c. Kết quả lượt → Continue ──
                elif phase == "result":
                    if battle_flags["won"] or battle_flags["lost"]:
                        ui.update(action="finishing battle")
                    else:
                        ui.update(action="Continue...")
                    await _human_delay(0.8, 1.6)
                    await click_continue(page)

                # ── 2d. Battle DOM biến mất → trận kết thúc ──
                else:  # end | none
                    await _finalize_battle()
                    await page.goto(TOWER_URL, wait_until="domcontentloaded")
                    await asyncio.sleep(1.0)

            else:
                logger.warning(f"[TOWER] Out of tower, returning. URL={page.url[:60]}")
                await page.goto(TOWER_URL, wait_until="domcontentloaded")
                await asyncio.sleep(1.5)

            await asyncio.sleep(0.3)  # breathing room giữa các vòng

        except Exception as e:
            logger.error(f"[TOWER] Loop error: {e}")
            ui.add_log(f"[red]TOWER ERR: {str(e)[:70]}[/red]")
            await asyncio.sleep(2)
            try:
                if await _detect_battle_screen(page) == "map":
                    await page.goto(TOWER_URL, wait_until="domcontentloaded")
                    await asyncio.sleep(1.5)
            except Exception:
                pass

    logger.info(f"[TOWER] Final: {wins}W / {losses}L / {total} battles")
    ui.add_log(f"[cyan]Tower done: {wins}W / {losses}L / {total} battles[/cyan]")
    return {"wins": wins, "losses": losses, "battles": total}
