"""
Navigation không dùng LLM — sweep pattern như robot hút bụi.

Mục tiêu duy nhất: di chuyển NHANH để đạp lên grass tiles.
LLM chỉ được dùng trong battle, không phải navigation.

Phases:
  EXIT_TOWN  → thoát zone "town-*" bằng probe + move
  SWEEP      → quét zigzag Đông↔Tây, dịch Nam dần, bao phủ tối đa diện tích
  IN_BATTLE  → xử lý bởi battle.py
"""
import asyncio
from playwright.async_api import Page
from loguru import logger
from config import config
from agent.utils import random_delay
from agent.localization import (
    get_state, is_town, focus_map,
    exit_town, hold_key, probe_directions,
)

# Hướng sweep hiện tại: True = đang đi Đông, False = đang đi Tây
_sweep_going_east = True
# Số lần đã đi ngang không gặp encounter → dịch Nam
_sweep_steps_without_enc = 0
_STEPS_BEFORE_SHIFT = 3  # sau 3 lượt sweep ngang → dịch Nam 1 bước


async def go_to_map(page: Page):
    logger.info(f"Đến map: {config.MAP_URL}")
    await page.goto(config.MAP_URL, wait_until="domcontentloaded")
    await random_delay(3.0, 4.0)
    await focus_map(page)

    state = await get_state(page)
    logger.info(f"[MAP] zone={state['zone']}  tile={state['tile']}")

    if is_town(state["zone"]):
        logger.info("[PHASE] EXIT_TOWN ...")
        await exit_town(page)
        state = await get_state(page)
        logger.info(f"[MAP] Vào zone={state['zone']}  tile={state['tile']}")


async def walk_pattern(page: Page):
    """
    [PHASE: SWEEP] Quét ngang như robot hút bụi.
    Không probe, không hỏi LLM — chỉ di chuyển nhanh.
    """
    global _sweep_going_east, _sweep_steps_without_enc

    await focus_map(page)
    state = await get_state(page)

    if is_town(state["zone"]):
        logger.warning(f"[EXIT_TOWN] Vào town ({state['zone']}) — thoát...")
        await exit_town(page)
        return

    horiz_key = "ArrowRight" if _sweep_going_east else "ArrowLeft"
    direction_label = "Đông →" if _sweep_going_east else "← Tây"
    tile_before = state["tile"]

    logger.info(f"[SWEEP] {direction_label}  zone={state['zone']}  tile={tile_before}")

    # Đi ngang 6 bước (hold 0.6s mỗi bước)
    hit_wall = False
    for step in range(6):
        await hold_key(page, horiz_key, duration=0.6)
        new_state = await get_state(page)
        if new_state["tile"] == tile_before:
            hit_wall = True
            logger.debug(f"  Tường {direction_label} — đổi chiều")
            break
        tile_before = new_state["tile"]

    # Đảo chiều khi hết lượt hoặc gặp tường
    if hit_wall or True:  # luôn đảo sau mỗi lượt để sweep đều
        _sweep_going_east = not _sweep_going_east

    # Dịch Nam sau N lượt để bao phủ vùng mới
    _sweep_steps_without_enc += 1
    if _sweep_steps_without_enc >= _STEPS_BEFORE_SHIFT:
        _sweep_steps_without_enc = 0
        logger.debug("[SWEEP] Dịch Nam ↓")
        await hold_key(page, "ArrowDown", duration=0.8)


def mark_encounter_found():
    """Gọi sau khi tìm được encounter — reset counter để ở lại vùng này."""
    global _sweep_steps_without_enc
    _sweep_steps_without_enc = 0


async def handle_map_encounter(page: Page) -> bool:
    dom_sel = ", ".join([
        "#pkmnappear", ".wild-pokemon-image", "#encounter",
        ".battle-popup", "[id*='appear']", "[class*='encounter']",
        ".v-dialog--active", "[role='dialog']", ".modal.show",
    ])
    try:
        await page.wait_for_selector(dom_sel, timeout=400)
        return True
    except Exception:
        pass
    if "map" not in page.url and ("battle" in page.url.lower() or "fight" in page.url.lower()):
        return True
    return False


async def return_to_map(page: Page):
    if "map" not in page.url:
        logger.info("[RETURN] Quay về map...")
        await page.goto(config.MAP_URL, wait_until="domcontentloaded")
        await random_delay(2.0, 3.0)
        await focus_map(page)
