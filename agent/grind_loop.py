"""
Grind loop chính — observe→decide→act.
Navigation dùng rule-based sweep (nhanh, không cần LLM).
LLM chỉ được gọi khi encounter/battle (hiệu quả hơn nhiều).
"""
import asyncio
from playwright.async_api import Page
from loguru import logger
from config import config
from agent.map_nav import go_to_map, return_to_map
from agent.brain import (
    collect_game_state, execute_action, on_encounter_end, get_battle_outcome,
)
from agent.battle import read_battle_state
from agent.llm import decide
from agent.utils import random_delay, RestScheduler
from agent.chat import process_commands, notify_rare, agent_state
from agent.team import fetch_team, get_cached_team
import agent.ui as ui


# ── Simple sweep navigation (thay LLM cho map) ────────────────────────────
_sweep_east  = True
_sweep_steps = 0
_SWEEP_FLIP  = 7  # Sau N steps đổi chiều


def _simple_nav(recent_path: list) -> dict:
    """Rule-based sweep, không dùng LLM."""
    global _sweep_east, _sweep_steps

    # Detect stuck: 4 tile gần nhất đều giống nhau
    if len(recent_path) >= 4:
        tiles = [tuple(t) for t in recent_path[-4:] if t]
        if len(tiles) >= 4 and len(set(tiles)) == 1:
            _sweep_east  = not _sweep_east
            _sweep_steps = 0
            return {"action": "go", "direction": "south", "reason": "stuck → shift south"}

    _sweep_steps += 1
    if _sweep_steps >= _SWEEP_FLIP:
        _sweep_steps = 0
        _sweep_east  = not _sweep_east
        return {"action": "go", "direction": "south", "reason": "sweep shift ↓"}

    d = "east" if _sweep_east else "west"
    return {"action": "go", "direction": d, "reason": f"sweep {'→' if _sweep_east else '←'}"}


# ── Stats tracker ──────────────────────────────────────────────────────────
class GrindStats:
    def __init__(self):
        self.battles  = 0
        self.wins     = 0
        self.losses   = 0
        self.catches  = 0
        self.errors   = 0
        self.ticks    = 0
        self.llm_calls = 0

    def as_dict(self):
        return {
            "battles": self.battles, "wins": self.wins,
            "losses":  self.losses,  "catches": self.catches,
        }

    def sync_ui(self):
        ui.update(
            battles=self.battles, wins=self.wins, losses=self.losses,
            catches=self.catches, errors=self.errors,
            llm_calls=self.llm_calls,
        )

    def log_summary(self):
        logger.info(
            f"--- THỐNG KÊ ---\n"
            f"  Ticks   : {self.ticks}\n"
            f"  Battles : {self.battles}  (W:{self.wins} L:{self.losses})\n"
            f"  Catches : {self.catches}\n"
            f"  Errors  : {self.errors}\n"
            f"  LLM calls: {self.llm_calls}"
        )


# ── Main grind loop ────────────────────────────────────────────────────────
async def grind(page: Page):
    stats = GrindStats()
    await go_to_map(page)

    logger.info(f"[GRIND] Bắt đầu — mode={config.MODE}  model=qwen3:latest  max={config.MAX_BATTLES or '∞'}")
    ui.add_log(f"[cyan]Grind bắt đầu — zone={ui._state['zone']} mode={config.MODE}[/cyan]")

    # Auto-fetch team khi khởi động
    try:
        team = await fetch_team(page)
        if team:
            ui.set_team(team)
            logger.info(f"[TEAM] Auto-fetch: {len(team)} Pokemon")
        else:
            ui.add_log("[yellow]Team: chua lay duoc — go lenh 'team' de thu lai[/yellow]")
    except Exception as e:
        logger.warning(f"[TEAM] Auto-fetch loi: {e}")

    current_pokemon = None
    in_encounter    = False

    rest = RestScheduler(config.REST_AFTER_MIN, config.REST_AFTER_MAX,
                         config.REST_HOURS, config.REST_ENABLED, tag="GRIND")

    while True:
        # ── Chat commands ──
        if not await process_commands(stats.as_dict()):
            logger.info("[GRIND] Dừng theo lệnh chat")
            break

        # ── Team fetch nếu user yêu cầu ──
        if agent_state.get("fetch_team"):
            agent_state["fetch_team"] = False
            try:
                team = await fetch_team(page)
                ui.set_team(team)
                from agent.chat import _log as chat_log
                if team:
                    chat_log(f"[green]Da lay {len(team)} Pokemon:[/green]")
                    for i, pk in enumerate(team[:6], 1):
                        name  = pk.get("name") or "?"
                        level = pk.get("level") or "?"
                        moves = " / ".join((pk.get("moves") or [])[:4]) or "?"
                        chat_log(f"  [cyan]{i}.[/cyan] [bold]{name}[/bold] Lv.[yellow]{level}[/yellow]  {moves}")
                else:
                    chat_log("[yellow]Khong lay duoc doi hinh. Thu goto pokemon-vortex.com/pokemon/[/yellow]")
            except Exception as e:
                logger.error(f"[TEAM] {e}")

        if config.MAX_BATTLES > 0 and stats.battles >= config.MAX_BATTLES:
            logger.info(f"Đã đạt giới hạn {config.MAX_BATTLES} battles")
            break

        # Nghỉ ngơi cho GPU sau mỗi ~N trận (random) — chỉ nghỉ khi đang ở map, không cắt ngang trận
        if not in_encounter:
            await rest.maybe_rest(stats.battles, on_log=ui.add_log)

        stats.ticks += 1

        try:
            # ── 0. Session hết hạn → tự đăng nhập lại ──
            if "/login" in page.url.lower():
                logger.warning("[GRIND] Session hết hạn — đăng nhập lại...")
                ui.add_log("[yellow]⚠ Session hết hạn — đang đăng nhập lại[/yellow]")
                from agent.login import login as _relogin
                if await _relogin(page):
                    await go_to_map(page)
                else:
                    logger.error("[GRIND] Đăng nhập lại thất bại — thử lại sau 30s")
                    await random_delay(28, 34)
                continue

            # ── 1. Observe ──
            game_state = await collect_game_state(page, stats.as_dict())
            screen = game_state["screen"]
            zone   = game_state["zone"]
            tile   = game_state["tile"]

            # Cập nhật UI cơ bản
            ui.update(zone=zone, tile=tile, screen=screen)

            # Log mỗi 15 ticks
            if stats.ticks % 15 == 0:
                logger.debug(f"[TICK {stats.ticks}] screen={screen} zone={zone} tile={tile}")

            # ── 2. Check rare Pokemon ──
            enc = game_state.get("encounter") or {}
            if enc.get("is_rare") and enc.get("pokemon") and not in_encounter:
                # Cập nhật UI encounter
                _update_encounter_ui(enc)
                await notify_rare(enc["pokemon"], enc.get("variant", "rare"))
                rare_act = agent_state.pop("rare_action", None)
                if   rare_act == "skip":   action = {"action": "flee",  "reason": "user: skip rare"}
                elif rare_act == "catch":  action = {"action": "catch", "reason": "user: catch rare"}
                elif rare_act == "battle": action = {"action": "fight", "reason": "user: battle rare"}
                else:
                    stats.llm_calls += 1
                    action = await decide(game_state)

            # ── 3. Decide ──
            elif screen == "map":
                # Rule-based navigation — không cần LLM
                if agent_state.get("force_catch"):
                    agent_state["force_catch"] = False
                    action = {"action": "catch", "reason": "chat: force catch"}
                elif agent_state.get("force_skip"):
                    agent_state["force_skip"] = False
                    action = {"action": "flee", "reason": "chat: force skip"}
                else:
                    llm_instr = agent_state.pop("llm_instruction", None)
                    if llm_instr:
                        # Có lệnh từ chat → dùng LLM một lần
                        game_state["user_instruction"] = llm_instr
                        stats.llm_calls += 1
                        action = await decide(game_state)
                    else:
                        # Sweep bình thường — không LLM
                        action = _simple_nav(game_state.get("recent_path", []))

            else:
                # Encounter / Battle
                _update_encounter_ui(enc)
                analysis = game_state.get("analysis") or {}
                rec_switch = analysis.get("recommended_switch")

                if enc.get("phase") == "choose" and rec_switch:
                    # Chọn Pokemon ra trận theo tương khắc hệ — rule-based, chính xác
                    action = {
                        "action": "switch",
                        "value":  rec_switch["value"],
                        "reason": f"{rec_switch['name']}: {rec_switch['reason']}",
                    }
                    ui.add_log(
                        f"[cyan]Ra trận [bold]{rec_switch['name']}[/bold] "
                        f"vs {analysis.get('enemy_next','?')} — {rec_switch['reason']}[/cyan]"
                    )
                else:
                    # LLM quyết định
                    llm_instr = agent_state.pop("llm_instruction", None)
                    if llm_instr:
                        game_state["user_instruction"] = llm_instr
                    stats.llm_calls += 1
                    action = await decide(game_state)

            act    = action.get("action", "wait")
            reason = action.get("reason", "")

            # ── 4. Cập nhật UI action ──
            ui.update(action=act, reason=reason)

            # ── 5. Track encounter start ──
            if screen in ("encounter", "battle") and not in_encounter:
                in_encounter    = True
                stats.battles  += 1
                current_pokemon = enc.get("pokemon", "Unknown")
                variant_label   = f" [{enc['variant'].upper()}]" if enc.get("variant") else ""
                logger.info(f"[ENCOUNTER #{stats.battles}] {current_pokemon}{variant_label}")
                ui.add_log(
                    f"[magenta]#{stats.battles} {current_pokemon}{variant_label}[/magenta]"
                    + (f" — [bold]LLM → {act}[/bold]" if stats.llm_calls else "")
                )

            # ── 6. Act ──
            exec_result = await execute_action(page, action)

            # Quick encounter check sau movement
            if act == "go" and exec_result.get("encounter_check"):
                logger.debug("[ENCOUNTER] Popup detected ngay sau movement")
                game_state = await collect_game_state(page, stats.as_dict())
                screen = game_state["screen"]
                enc    = game_state.get("encounter") or {}
                ui.update(screen=screen)

            # ── 7. Encounter end ──
            if in_encounter and act in ("next", "flee", "catch"):
                result = None
                if act == "flee":
                    result = "fled"
                else:
                    # Trận thật sự kết thúc khi battle DOM biến mất / phase end
                    bstate = await read_battle_state(page)
                    if bstate.get("phase") in ("none", "end"):
                        outcome = get_battle_outcome()
                        if act == "catch" and outcome != "lose":
                            result = "caught"
                        else:
                            result = outcome or "win"

                if result:
                    if result == "win":       stats.wins    += 1
                    elif result == "caught":  stats.catches += 1; stats.wins += 1
                    elif result == "lose":    stats.losses  += 1
                    await on_encounter_end(current_pokemon or "Unknown", result, config.MODE)
                    in_encounter    = False
                    current_pokemon = None
                    # Reset encounter UI
                    ui.update(pokemon=None, variant=None, poke_level=None, moves=[],
                              is_rare=False, enemy_hp_pct=None, my_hp_pct=None,
                              my_name=None, enemy_img=None, my_img=None)
                    color = "green" if result in ("win", "caught") else ("red" if result == "lose" else "yellow")
                    ui.add_log(f"[{color}]{result.upper()}[/{color}]")
                    if result == "lose":
                        ui.add_log("[yellow]Đang phân tích trận thua để rút bài học...[/yellow]")
                    await return_to_map(page)

            stats.sync_ui()

            # ── 8. Delay (chỉ khi navigation, không delay khi battle) ──
            if act == "go":
                await random_delay(config.ACTION_DELAY_MIN, config.ACTION_DELAY_MAX)

            # Summary mỗi 10 battles
            if stats.battles > 0 and stats.battles % 10 == 0 and stats.ticks % 5 == 0:
                stats.log_summary()

        except Exception as e:
            stats.errors += 1
            logger.error(f"[ERROR tick {stats.ticks}] {e}")
            ui.add_log(f"[red]ERROR: {str(e)[:60]}[/red]")
            in_encounter = False
            ui.update(pokemon=None, variant=None, is_rare=False)
            try:
                await return_to_map(page)
            except Exception:
                pass
            await random_delay(2.0, 4.0)

    stats.log_summary()
    return stats


def _update_encounter_ui(enc: dict):
    if not enc:
        return
    poke = enc.get("pokemon")
    my   = enc.get("my_pokemon") or {}
    if poke:
        logger.info(f"[UI] Encounter: {enc.get('variant','').upper()+' ' if enc.get('variant') else ''}{poke} Lv.{enc.get('level','?')}")
    ui.update(
        pokemon      = poke,
        variant      = enc.get("variant"),
        is_rare      = enc.get("is_rare", False),
        poke_level   = enc.get("level"),
        moves        = enc.get("moves", []),
        moves_detail = enc.get("moves_detail", []),
        enemy_hp     = enc.get("hp"),
        enemy_hp_pct = enc.get("hp_pct"),
        enemy_img    = enc.get("img"),
        my_name      = my.get("name"),
        my_level     = my.get("level"),
        my_hp        = my.get("hp"),
        my_hp_pct    = my.get("hp_pct"),
        my_img       = my.get("img"),
        battle_phase = enc.get("phase"),
    )
