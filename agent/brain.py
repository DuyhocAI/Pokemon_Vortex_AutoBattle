"""
Brain executor — thu thập game state, gọi LLM, thực thi action.
Vòng lặp chính: observe → decide → act.

Battle dùng DOM thật (agent/battle.py) + phân tích type matchup (agent/pokedex.py).
"""
import asyncio
from collections import deque
from playwright.async_api import Page
from loguru import logger
from config import config
from agent.localization import get_state, is_town, focus_map, hold_key
from agent.memory import get_context, log_encounter, add_lesson
from agent.battle import (
    read_battle_state, select_move_and_attack, click_continue,
    attempt_catch, use_item, close_encounter, reset_battle_tracking,
    select_pokemon_and_start,
)
from agent.pokedex import battle_analysis, rank_team_choices

# Lịch sử tile để LLM nhận biết đang đi đâu
_path_history: deque = deque(maxlen=8)

# Tracking encounter hiện tại
_encounter_moves_used: list = []
_encounter_turns: int = 0
_battle_log: deque = deque(maxlen=12)   # các dòng attackOutput của trận hiện tại
_battle_outcome: str | None = None       # "win" | "lose" khi đã xác định được
_battle_enemy_info: dict = {}            # enemy info cuối cùng đọc được


def get_battle_outcome() -> str | None:
    return _battle_outcome


def get_battle_summary() -> dict:
    """Tóm tắt trận hiện tại — dùng cho learning sau khi thua."""
    return {
        "enemy":       _battle_enemy_info.get("name"),
        "enemy_level": _battle_enemy_info.get("level"),
        "enemy_types": _battle_enemy_info.get("types"),
        "my_pokemon":  _battle_enemy_info.get("my_pokemon"),
        "moves_used":  list(_encounter_moves_used),
        "turns":       _encounter_turns,
        "battle_log":  list(_battle_log)[-6:],
    }


async def collect_game_state(page: Page, stats: dict) -> dict:
    """Thu thập toàn bộ trạng thái game thành 1 dict cho LLM."""
    global _battle_outcome
    loc = await get_state(page)
    zone = loc.get("zone") or "unknown"
    tile = loc.get("tile")

    if tile:
        _path_history.append(list(tile))

    # ── 1. Battle DOM thật? ──
    bstate = await read_battle_state(page)
    encounter_info = None
    analysis = None
    screen = "map"

    if bstate.get("phase") in ("select", "result", "end"):
        screen = "battle"
        encounter_info = _battle_to_encounter(bstate)

        # Track battle log + outcome
        out = bstate.get("output")
        if out and (not _battle_log or _battle_log[-1] != out):
            _battle_log.append(out)
            ev = bstate.get("events", {})
            if ev.get("won") or ev.get("enemy_fainted"):
                _battle_outcome = "win"
            elif ev.get("lost"):
                _battle_outcome = "lose"
        # Lưu enemy info cho learning
        if encounter_info.get("pokemon"):
            _battle_enemy_info.update({
                "name":       encounter_info["pokemon"],
                "level":      encounter_info.get("level"),
                "my_pokemon": encounter_info.get("my_pokemon"),
            })

        # Phân tích matchup (pokedex internet + cache) khi đang chọn move
        if bstate.get("phase") == "select" and bstate.get("moves"):
            try:
                analysis = await battle_analysis(
                    bstate["moves"], encounter_info.get("pokemon") or "",
                    my_name=(encounter_info.get("my_pokemon") or {}).get("name"))
                if analysis.get("enemy_types"):
                    _battle_enemy_info["types"] = analysis["enemy_types"]
            except Exception as e:
                logger.debug(f"[BRAIN] battle_analysis: {e}")

        # Màn chọn Pokemon ra trận → xếp hạng theo tương khắc hệ
        elif bstate.get("phase") == "choose":
            try:
                analysis = await _rank_switch_options(bstate)
            except Exception as e:
                logger.debug(f"[BRAIN] rank_switch: {e}")

    else:
        # ── 2. Encounter popup trên map? ──
        screen = await _detect_screen(page)
        if screen == "encounter":
            encounter_info = await _read_encounter_popup(page)

    # Memory context (bao gồm LESSON từ các trận thua)
    memory = ""
    if encounter_info and encounter_info.get("pokemon"):
        memory = get_context(encounter_info["pokemon"])

    return {
        "screen":      screen,
        "zone":        zone,
        "is_town":     is_town(zone),
        "tile":        list(tile) if tile else None,
        "recent_path": list(_path_history)[-6:],
        "encounter":   encounter_info,
        "analysis":    analysis,
        "memory":      memory,
        "mode":        config.MODE,
        "stats":       stats,
    }


async def _rank_switch_options(bstate: dict) -> dict:
    """Xếp hạng Pokemon nên ra trận (phase=choose) theo type matchup + move types thật."""
    from agent.team import get_cached_team
    choices = bstate.get("choose_options", [])
    enemy_name = _current_enemy_for_choose(bstate)
    team_moves = {pk.get("name"): pk.get("moves", []) for pk in (get_cached_team() or [])}

    ranked = await rank_team_choices(choices, team_moves, enemy_name or "")
    top = ranked[0] if ranked else None
    if top:
        logger.info(f"[SWITCH] Chọn {top['name']} vs {enemy_name} — {top['reason']}")
    return {
        "phase":            "choose",
        "enemy_next":       enemy_name,
        "switch_ranking":   [
            {"value": r["value"], "name": r["name"], "types": r.get("types"),
             "offense": r["offense"], "threat": r["threat"], "score": r["score"],
             "best_move": r.get("best_move")}
            for r in ranked
        ],
        "recommended_switch": {"value": top["value"], "name": top["name"], "reason": top["reason"]} if top else None,
    }


def _current_enemy_for_choose(bstate: dict) -> str | None:
    """Đối thủ sắp đánh: enemy đang hiện trên sân (mid-battle) hoặc con đầu trong đội đối thủ."""
    enemy = (bstate.get("enemy") or {}).get("name")
    if enemy:
        return enemy
    if _battle_enemy_info.get("name"):
        return _battle_enemy_info["name"]
    opponents = bstate.get("opponents", [])
    return opponents[0]["name"] if opponents else None


def _battle_to_encounter(bstate: dict) -> dict:
    """Chuyển battle state → encounter dict cho LLM/UI."""
    enemy = bstate.get("enemy") or {}
    me    = bstate.get("me") or {}
    ev    = bstate.get("events") or {}
    name  = enemy.get("name") or _current_enemy_for_choose(bstate)
    variant = detect_variant(name or "")

    battle_result = None
    if ev.get("won") or ev.get("enemy_fainted"):
        battle_result = "Victory — enemy fainted"
    elif ev.get("lost"):
        battle_result = "Defeat"

    return {
        "pokemon":        name,
        "variant":        variant,
        "is_rare":        variant is not None,
        "level":          enemy.get("level"),
        "hp":             enemy.get("hp"),
        "hp_pct":         enemy.get("hp_pct"),
        "img":            enemy.get("img"),
        "my_pokemon":     {
            "name":   me.get("name"),
            "level":  me.get("level"),
            "hp":     me.get("hp"),
            "hp_pct": me.get("hp_pct"),
            "img":    me.get("img"),
        },
        "phase":          bstate.get("phase"),
        "moves":          [m.get("name") for m in bstate.get("moves", [])],
        "moves_detail":   bstate.get("moves", []),
        "choose_options": bstate.get("choose_options", []),
        "opponents":      bstate.get("opponents", []),
        "items":          [i for i in bstate.get("items", []) if not i.get("disabled")],
        "last_output":    (bstate.get("output") or "")[:400] or None,
        "battle_result":  battle_result,
        "catch_available": any("ball" in (i.get("name") or "").lower()
                               for i in bstate.get("items", []) if not i.get("disabled")),
    }


async def execute_action(page: Page, action: dict) -> dict:
    """Thực thi action từ LLM. Trả về result dict."""
    global _encounter_moves_used, _encounter_turns
    act = action.get("action", "wait")
    result = {"action": act, "success": False}

    # --- Navigation ---
    if act == "go":
        direction = action.get("direction", "east")
        key_map = {"north": "ArrowUp", "south": "ArrowDown", "east": "ArrowRight", "west": "ArrowLeft"}
        key = key_map.get(direction, "ArrowDown")
        await focus_map(page)
        await hold_key(page, key, duration=1.5)
        result["success"] = True
        result["encounter_check"] = await _quick_encounter_check(page)

    # --- Bắt đầu battle từ encounter popup ---
    elif act == "fight":
        clicked = False
        try:
            el = await page.query_selector("a[href*='/battle/']")
            if el and await el.is_visible():
                await el.click()
                await asyncio.sleep(1.0)
                clicked = True
        except Exception:
            pass
        if not clicked:
            for sel in ["a:text('Battle!')", "button:text('Battle!')",
                        "a:text-is('Battle!')", "button:text-is('Battle!')"]:
                try:
                    el = page.locator(sel).first
                    if await el.is_visible(timeout=1000):
                        await el.click()
                        await asyncio.sleep(1.0)
                        clicked = True
                        break
                except Exception:
                    pass
        if clicked:
            result["success"] = True
            reset_battle_tracking()
            _reset_battle_log()
        else:
            logger.debug("[fight] Không tìm thấy Battle button")

    # --- Chọn move + bấm Attack (fix: luôn bấm Attack sau khi chọn) ---
    elif act == "move":
        idx = int(action.get("index", 0))
        r = await select_move_and_attack(page, idx)
        result["success"] = r.get("ok", False)
        if r.get("move"):
            _encounter_moves_used.append(r["move"])
            _encounter_turns += 1

    # --- Chọn Pokemon ra trận (phase=choose) theo tương khắc ---
    elif act == "switch":
        value = action.get("value")
        if value:
            result["success"] = (await select_pokemon_and_start(page, str(value))) == "ok"

    # --- Bắt Pokemon ---
    elif act == "catch":
        result["success"] = await attempt_catch(page)

    # --- Dùng item (heal...) ---
    elif act == "item":
        name = action.get("name", "Potion")
        result["success"] = await use_item(page, name)

    # --- Continue sau mỗi lượt / kết thúc trận ---
    elif act == "next":
        ok = await click_continue(page)
        if not ok:
            # Fallback: nút đóng encounter cũ
            for sel in ["#nextBattle", ".next-battle", ".closeWild", "#closeWild"]:
                try:
                    btn = page.locator(sel).first
                    if await btn.is_visible(timeout=800):
                        await btn.click()
                        await asyncio.sleep(1.0)
                        ok = True
                        break
                except Exception:
                    continue
        result["success"] = ok

    # --- Flee (chỉ ở encounter popup) ---
    elif act == "flee":
        await close_encounter(page)
        result["success"] = True

    elif act == "wait":
        await asyncio.sleep(0.5)
        result["success"] = True

    return result


def _reset_battle_log():
    global _battle_outcome, _encounter_moves_used, _encounter_turns
    _battle_log.clear()
    _battle_enemy_info.clear()
    _battle_outcome = None
    _encounter_moves_used = []
    _encounter_turns = 0


async def _quick_encounter_check(page: Page) -> bool:
    """Check nhanh sau khi di chuyển — có encounter popup không?"""
    try:
        if await page.locator(":text('Battle!')").first.is_visible(timeout=400):
            return True
    except Exception:
        pass
    try:
        el = await page.query_selector("a[href*='/battle/']")
        if el and await el.is_visible():
            return True
    except Exception:
        pass
    try:
        found = await page.evaluate("""() => {
            for (const el of document.querySelectorAll('*')) {
                const t = (el.textContent || '').trim();
                if (t.length < 25 && t.includes('Level:') && /[0-9]/.test(t)) return true;
            }
            return false;
        }""")
        if found:
            return True
    except Exception:
        pass
    return False


async def on_encounter_end(pokemon: str, result: str, mode: str):
    """Gọi khi encounter kết thúc — lưu memory + HỌC TỪ TRẬN THUA."""
    log_encounter(
        pokemon=pokemon,
        mode=mode,
        result=result,
        turns=_encounter_turns,
        moves_used=_encounter_moves_used,
    )

    # Học từ thua: LLM phân tích trận → lưu bài học vào memory
    if result == "lose":
        summary = get_battle_summary()
        summary["enemy"] = summary.get("enemy") or pokemon

        async def _learn():
            try:
                from agent.llm import learn_from_loss
                lesson = await learn_from_loss(summary)
                if lesson:
                    add_lesson(pokemon, lesson)
                    try:
                        import agent.ui as ui
                        ui.add_log(f"[yellow][LEARN] {lesson[:80]}[/yellow]")
                    except Exception:
                        pass
            except Exception as e:
                logger.debug(f"[LEARN] {e}")

        asyncio.create_task(_learn())

    _reset_battle_log()


async def _detect_screen(page: Page) -> str:
    """Xác định màn hình khi KHÔNG ở battle DOM (battle đã check trước đó)."""
    url = page.url.lower()
    if "/battle/" in url or "battle?wild" in url:
        return "battle"

    # "Battle!" button visible → encounter popup
    try:
        if await page.locator(":text('Battle!')").first.is_visible(timeout=300):
            return "encounter"
    except Exception:
        pass

    try:
        el = await page.query_selector("a[href*='/battle/']")
        if el and await el.is_visible():
            return "encounter"
    except Exception:
        pass

    # "Level:" text trong element ngắn → encounter popup
    try:
        found = await page.evaluate("""() => {
            for (const el of document.querySelectorAll('*')) {
                const t = (el.textContent || '').trim();
                if (t.length < 25 && t.includes('Level:') && /[0-9]/.test(t)) return true;
            }
            return false;
        }""")
        if found:
            return "encounter"
    except Exception:
        pass

    return "map"


RARE_VARIANTS = {
    "shiny":    ["shiny",    "✨", "sparkle"],
    "dark":     ["dark",     "🌑", "shadow-dark"],
    "mystic":   ["mystic",   "💫", "mystical"],
    "shadow":   ["shadow",   "👻"],
    "metallic": ["metallic", "🔩", "metal"],
}


def detect_variant(pokemon_name: str, extra_classes: str = "") -> str | None:
    """Trả về tên variant nếu là rare, None nếu normal."""
    combined = (pokemon_name + " " + extra_classes).lower()
    for variant, keywords in RARE_VARIANTS.items():
        if any(kw in combined for kw in keywords):
            return variant
    return None


async def _read_encounter_popup(page: Page) -> dict:
    """Đọc thông tin Pokemon từ encounter popup trên map (trước khi vào battle)."""
    info = {
        "pokemon": None, "variant": None, "is_rare": False,
        "level": None, "moves": [], "catch_available": False,
        "battle_result": None, "phase": "popup",
    }
    try:
        page_data = await page.evaluate("""() => {
            const body_text = document.body?.innerText || '';
            const title = document.title || '';
            const img_srcs = Array.from(document.querySelectorAll('img')).map(i => i.src).join(' ');
            return { body_text, title, img_srcs };
        }""")

        all_text = " ".join([page_data.get("body_text", ""), page_data.get("title", ""),
                             page_data.get("img_srcs", "")])

        raw_name = await page.evaluate(r"""() => {
            const allEls = Array.from(document.querySelectorAll('*'));
            let levelEl = null;
            for (const el of allEls) {
                if (el.childElementCount === 0) {
                    const t = (el.textContent || '').trim();
                    if (/^Level:\s*\d/.test(t)) { levelEl = el; break; }
                }
            }
            if (!levelEl) {
                for (const el of allEls) {
                    const t = (el.textContent || '').trim();
                    if (t.length < 25 && t.includes('Level:') && /[0-9]/.test(t)) {
                        levelEl = el; break;
                    }
                }
            }
            if (!levelEl) return null;
            let node = levelEl.parentElement;
            for (let i = 0; i < 6 && node; i++) {
                const lines = (node.innerText || '').split('\n')
                    .map(s => s.trim()).filter(s => s.length > 1 && s.length < 60);
                for (const line of lines) {
                    if (!/^Level:/i.test(line) && !/^Battle/i.test(line)
                            && !/^\d+$/.test(line) && !/^[♂♀⚥]$/.test(line)) {
                        return line.replace(/[♂♀⚥]/g, '').trim();
                    }
                }
                node = node.parentElement;
            }
            return null;
        }""")

        import re as _re
        lv_match = _re.search(r"Level[:\s]+(\d+)", page_data.get("body_text", ""), _re.I)
        if lv_match:
            info["level"] = int(lv_match.group(1))

        if raw_name:
            variant = detect_variant(raw_name, all_text)
            info["pokemon"] = raw_name
            info["variant"] = variant
            info["is_rare"] = variant is not None
            if info["is_rare"]:
                logger.warning(f"[RARE] {variant.upper()} {raw_name} phát hiện!")

    except Exception as e:
        logger.debug(f"[_read_encounter_popup] {e}")
    return info
