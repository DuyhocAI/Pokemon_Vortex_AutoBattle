"""
Pokedex — tra cứu thông tin Pokemon từ internet (PokeAPI) + type chart.

- lookup(name): types + base stats + sprite, cache vào SQLite (data/memory.db) 30 ngày
- effectiveness(move_type, enemy_types): hệ số sát thương
- analyze_matchup(moves, enemy_types): xếp hạng move tốt nhất
- battle_analysis(): tổng hợp phân tích cho LLM (types, điểm yếu, move khuyên dùng)

Variant của Vortex (Shiny/Dark/Mystic/Shadow/Metallic) chỉ đổi màu — base stats/types
giống bản gốc, nên strip prefix trước khi tra.
"""
import re
import httpx
from loguru import logger
from agent.memory import get_pokedex, save_pokedex

POKEAPI       = "https://pokeapi.co/api/v2/pokemon/"
POKEAPI_MOVE  = "https://pokeapi.co/api/v2/move/"
_MOVE_PREFIX  = "move::"   # key prefix trong bảng pokedex cache

VARIANT_PREFIXES = ("shiny", "dark", "mystic", "shadow", "metallic")

# ATTACKER_TYPE -> {DEFENDER_TYPE: multiplier} (khác 1.0)
TYPE_CHART: dict[str, dict[str, float]] = {
    "Normal":   {"Rock": 0.5, "Steel": 0.5, "Ghost": 0},
    "Fire":     {"Fire": 0.5, "Water": 0.5, "Rock": 0.5, "Dragon": 0.5, "Grass": 2, "Ice": 2, "Bug": 2, "Steel": 2},
    "Water":    {"Water": 0.5, "Grass": 0.5, "Dragon": 0.5, "Fire": 2, "Ground": 2, "Rock": 2},
    "Grass":    {"Fire": 0.5, "Grass": 0.5, "Poison": 0.5, "Flying": 0.5, "Bug": 0.5, "Dragon": 0.5, "Steel": 0.5, "Water": 2, "Ground": 2, "Rock": 2},
    "Electric": {"Grass": 0.5, "Electric": 0.5, "Dragon": 0.5, "Ground": 0, "Water": 2, "Flying": 2},
    "Ice":      {"Fire": 0.5, "Water": 0.5, "Ice": 0.5, "Steel": 0.5, "Grass": 2, "Ground": 2, "Flying": 2, "Dragon": 2},
    "Fighting": {"Poison": 0.5, "Flying": 0.5, "Psychic": 0.5, "Bug": 0.5, "Fairy": 0.5, "Ghost": 0, "Normal": 2, "Ice": 2, "Rock": 2, "Dark": 2, "Steel": 2},
    "Poison":   {"Poison": 0.5, "Ground": 0.5, "Rock": 0.5, "Ghost": 0.5, "Steel": 0, "Grass": 2, "Fairy": 2},
    "Ground":   {"Grass": 0.5, "Bug": 0.5, "Flying": 0, "Fire": 2, "Electric": 2, "Poison": 2, "Rock": 2, "Steel": 2},
    "Flying":   {"Electric": 0.5, "Rock": 0.5, "Steel": 0.5, "Grass": 2, "Fighting": 2, "Bug": 2},
    "Psychic":  {"Psychic": 0.5, "Steel": 0.5, "Dark": 0, "Fighting": 2, "Poison": 2},
    "Bug":      {"Fire": 0.5, "Fighting": 0.5, "Flying": 0.5, "Ghost": 0.5, "Steel": 0.5, "Fairy": 0.5, "Poison": 0.5, "Grass": 2, "Psychic": 2, "Dark": 2},
    "Rock":     {"Fighting": 0.5, "Ground": 0.5, "Steel": 0.5, "Fire": 2, "Ice": 2, "Flying": 2, "Bug": 2},
    "Ghost":    {"Normal": 0, "Dark": 0.5, "Ghost": 2, "Psychic": 2},
    "Dragon":   {"Steel": 0.5, "Fairy": 0, "Dragon": 2},
    "Dark":     {"Fighting": 0.5, "Dark": 0.5, "Fairy": 0.5, "Ghost": 2, "Psychic": 2},
    "Steel":    {"Fire": 0.5, "Water": 0.5, "Electric": 0.5, "Steel": 0.5, "Ice": 2, "Rock": 2, "Fairy": 2},
    "Fairy":    {"Fire": 0.5, "Poison": 0.5, "Steel": 0.5, "Fighting": 2, "Dragon": 2, "Dark": 2},
}

ALL_TYPES = list(TYPE_CHART.keys())


def strip_variant(name: str) -> tuple[str, str | None]:
    """'Dark Lunatone' → ('Lunatone', 'dark'). 'Pikachu' → ('Pikachu', None)."""
    if not name:
        return "", None
    parts = name.strip().split()
    if len(parts) >= 2 and parts[0].lower() in VARIANT_PREFIXES:
        return " ".join(parts[1:]), parts[0].lower()
    return name.strip(), None


def _api_name(base: str) -> str:
    """Chuẩn hóa tên cho PokeAPI: lowercase, bỏ form trong ngoặc, space → '-'."""
    n = base.lower().strip()
    # Form đặc biệt: "Charizard (Mega X)" → charizard-mega-x
    m = re.match(r"(.+?)\s*\((.+?)\)", n)
    if m:
        n = f"{m.group(1).strip()}-{m.group(2).strip()}"
    n = n.replace(". ", "-").replace(" ", "-").replace("'", "").replace(".", "")
    n = n.replace("♀", "-f").replace("♂", "-m").replace(":", "")
    return n


async def _lookup_via_species(client: httpx.AsyncClient, species_slug: str):
    """PokeAPI: loài chỉ có form variety (jellicent-male...) — tra qua /pokemon-species/."""
    try:
        sp = await client.get("https://pokeapi.co/api/v2/pokemon-species/" + species_slug)
        if sp.status_code != 200:
            return None
        varieties = sp.json().get("varieties") or []
        default = next((v for v in varieties if v.get("is_default")), varieties[0] if varieties else None)
        if not default:
            return None
        variety_name = default["pokemon"]["name"]
        logger.debug(f"[POKEDEX] species {species_slug} → variety {variety_name}")
        return await client.get(POKEAPI + variety_name)
    except Exception as e:
        logger.debug(f"[POKEDEX] species lookup {species_slug}: {e}")
        return None


async def lookup(name: str) -> dict | None:
    """
    Tra cứu Pokemon (tự strip variant). Trả về:
      {name, variant, types: [..], stats: {hp, attack, defense, sp_atk, sp_def, speed}, sprite}
    Cache SQLite — chỉ gọi internet lần đầu.
    """
    base, variant = strip_variant(name)
    if not base:
        return None

    cached = get_pokedex(base)
    if cached:
        return {**cached, "variant": variant}

    api_name = _api_name(base)
    try:
        async with httpx.AsyncClient(timeout=12.0, follow_redirects=True) as client:
            resp = await client.get(POKEAPI + api_name)
            if resp.status_code != 200:
                # Thử lần nữa với tên đơn giản hơn (bỏ hậu tố sau '-')
                simple = api_name.split("-")[0]
                if simple != api_name:
                    resp = await client.get(POKEAPI + simple)
            if resp.status_code != 200:
                # Một số loài chỉ có variety form (vd Jellicent → jellicent-male):
                # tra species → lấy default variety
                resp = await _lookup_via_species(client, api_name.split("-")[0])
            if resp is None or resp.status_code != 200:
                logger.debug(f"[POKEDEX] PokeAPI 404: {api_name}")
                return None
            data = resp.json()

        stat_map = {"hp": "hp", "attack": "attack", "defense": "defense",
                    "special-attack": "sp_atk", "special-defense": "sp_def", "speed": "speed"}
        stats = {}
        for s in data.get("stats", []):
            key = stat_map.get(s["stat"]["name"])
            if key:
                stats[key] = s["base_stat"]

        entry = {
            "name":   base,
            "types":  [t["type"]["name"].capitalize() for t in data.get("types", [])],
            "stats":  stats,
            "sprite": (data.get("sprites") or {}).get("front_default"),
        }
        save_pokedex(base, entry)
        logger.info(f"[POKEDEX] Tra cứu internet: {base} → {'/'.join(entry['types'])} (đã lưu memory)")
        return {**entry, "variant": variant}

    except Exception as e:
        logger.debug(f"[POKEDEX] lookup {name}: {e}")
        return None


def effectiveness(move_type: str | None, enemy_types: list[str]) -> float:
    """Hệ số sát thương của move_type lên enemy_types (0, 0.25, 0.5, 1, 2, 4)."""
    if not move_type or not enemy_types:
        return 1.0
    chart = TYPE_CHART.get(move_type.capitalize(), {})
    mult = 1.0
    for t in enemy_types:
        mult *= chart.get(t.capitalize(), 1.0)
    return mult


def weaknesses(enemy_types: list[str]) -> list[str]:
    """Các type đánh hiệu quả (>1x) lên Pokemon có enemy_types."""
    out = []
    for atk in ALL_TYPES:
        if effectiveness(atk, enemy_types) > 1.0:
            out.append(atk)
    return out


def analyze_matchup(moves: list[dict], enemy_types: list[str]) -> list[dict]:
    """
    moves: [{name, type}] (type lấy từ typeImg trong battle DOM)
    Trả về moves kèm multiplier, sắp xếp giảm dần theo hiệu quả.
    """
    ranked = []
    for i, m in enumerate(moves):
        mult = effectiveness(m.get("type"), enemy_types)
        ranked.append({**m, "index": i, "multiplier": mult})
    ranked.sort(key=lambda x: x["multiplier"], reverse=True)
    return ranked


# ── Move lookup (type thật từ PokeAPI, không đoán theo tên) ─────────────────

def _move_slug(name: str) -> str:
    n = name.lower().strip()
    n = re.sub(r"\s*\(.*?\)", "", n)          # bỏ chú thích trong ngoặc
    n = n.replace("'", "").replace(".", "").replace(",", "")
    n = re.sub(r"\s+", "-", n)
    return n


async def move_lookup(name: str) -> dict | None:
    """Tra cứu move: {name, type, power, damage_class}. Cache vĩnh viễn (move không đổi)."""
    if not name:
        return None
    key = _MOVE_PREFIX + _move_slug(name)
    cached = get_pokedex(key, max_age_days=3650)
    if cached:
        return cached
    try:
        async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
            resp = await client.get(POKEAPI_MOVE + _move_slug(name))
        if resp.status_code != 200:
            logger.debug(f"[POKEDEX] move 404: {name}")
            return None
        data = resp.json()
        entry = {
            "name":         name,
            "type":         (data.get("type") or {}).get("name", "").capitalize() or None,
            "power":        data.get("power"),
            "damage_class": (data.get("damage_class") or {}).get("name"),
        }
        save_pokedex(key, entry)
        logger.info(f"[POKEDEX] Move: {name} → {entry['type']} (power {entry['power']})")
        return entry
    except Exception as e:
        logger.debug(f"[POKEDEX] move_lookup {name}: {e}")
        return None


async def moves_types(move_names: list[str]) -> dict[str, str]:
    """Map tên move → type. Chỉ trả move tra được."""
    out = {}
    for n in move_names or []:
        info = await move_lookup(n)
        if info and info.get("type"):
            out[n] = info["type"]
    return out


# ── Chọn Pokemon ra trận theo tương khắc ────────────────────────────────────

async def rank_team_choices(candidates: list[dict], team_moves: dict[str, list[str]],
                            enemy_name: str) -> list[dict]:
    """
    Xếp hạng Pokemon nên ra trận vs enemy.
    candidates: [{value, name, level, hp, fainted}] từ màn hình #pokeChoose
    team_moves: {pokemon_name: [4 move names]} từ team cache
    Trả về list sắp xếp giảm dần theo score, mỗi phần tử kèm:
      offense (hệ số move tốt nhất), best_move, threat (enemy STAB vs mình), score, reason
    """
    enemy_info  = await lookup(enemy_name) if enemy_name else None
    enemy_types = (enemy_info or {}).get("types", [])

    ranked = []
    for c in candidates:
        if c.get("fainted") or not c.get("name"):
            continue
        name = c["name"]

        # Offense: move tốt nhất của Pokemon này vs enemy (type move thật từ PokeAPI)
        offense, best_move = 1.0, None
        mtypes = await moves_types(team_moves.get(name, []))
        for mv, mtype in mtypes.items():
            eff = effectiveness(mtype, enemy_types)
            if best_move is None or eff > offense:
                offense, best_move = eff, f"{mv} ({mtype})"

        # STAB fallback nếu không có move data: dùng type của chính Pokemon
        my_info  = await lookup(name)
        my_types = (my_info or {}).get("types", [])
        if best_move is None and my_types:
            for t in my_types:
                eff = effectiveness(t, enemy_types)
                if eff > offense:
                    offense, best_move = eff, f"STAB {t}"

        # Defense: enemy STAB đánh vào mình mạnh cỡ nào (thấp = tốt)
        threat = 1.0
        if enemy_types and my_types:
            threat = max(effectiveness(et, my_types) for et in enemy_types)

        score = offense * 2.0 - threat
        ranked.append({
            **c,
            "types":     my_types,
            "offense":   offense,
            "best_move": best_move,
            "threat":    threat,
            "score":     round(score, 2),
            "reason":    f"{best_move or '?'} {offense:.1f}x vs {'/'.join(enemy_types) or '?'}"
                         f", chịu {threat:.1f}x",
        })

    ranked.sort(key=lambda x: (x["score"], x["offense"], -(x["threat"])), reverse=True)
    return ranked


async def rank_moves_full(moves: list[dict], enemy_types: list[str],
                          my_types: list[str] | None = None) -> list[dict]:
    """
    Xếp hạng move theo DAMAGE THẬT SỰ: effectiveness × power × STAB.
    moves: [{name, type}] — type từ DOM (chính xác theo game), power tra PokeAPI (cache).
    Status move (power=null, damage_class=status) bị xếp cuối (score 0).
    """
    ranked = []
    for i, m in enumerate(moves):
        mtype = m.get("type")
        eff = effectiveness(mtype, enemy_types) if enemy_types else 1.0
        info = await move_lookup(m.get("name") or "")
        if info and info.get("damage_class") == "status":
            power = 0          # move trạng thái không gây damage
        else:
            power = (info or {}).get("power") or 60  # không rõ → trung bình
        stab = bool(my_types and mtype and
                    mtype.capitalize() in [t.capitalize() for t in my_types])
        score = eff * power * (1.5 if stab else 1.0)
        ranked.append({**m, "index": i, "multiplier": eff, "power": power,
                       "stab": stab, "score": round(score, 1)})
    ranked.sort(key=lambda x: (x["score"], x["multiplier"]), reverse=True)
    return ranked


async def battle_analysis(moves: list[dict], enemy_name: str,
                          my_name: str | None = None) -> dict:
    """
    Phân tích đầy đủ cho LLM: enemy types/stats (tra internet nếu chưa có),
    moves xếp hạng theo effectiveness × power × STAB, gợi ý move tốt nhất.
    """
    info = await lookup(enemy_name) if enemy_name else None
    enemy_types = (info or {}).get("types", [])
    my_types = []
    if my_name:
        my_info = await lookup(my_name)
        my_types = (my_info or {}).get("types", [])
    ranked = await rank_moves_full(moves, enemy_types, my_types) if enemy_types else []

    analysis = {
        "enemy_types":      enemy_types or None,
        "enemy_base_stats": (info or {}).get("stats") or None,
        "enemy_weak_to":    weaknesses(enemy_types) if enemy_types else None,
        "move_ranking":     [
            {"index": r["index"], "name": r["name"], "type": r.get("type"),
             "multiplier": r["multiplier"], "power": r.get("power"),
             "stab": r.get("stab"), "score": r.get("score")}
            for r in ranked
        ] or None,
        "best_move_index":  ranked[0]["index"] if ranked else None,
    }
    return analysis
