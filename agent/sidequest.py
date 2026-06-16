"""
Auto Sidequests loop.
URL: https://www.pokemon-vortex.com/sidequests/

Flow:
  1. Trang sidequests: đọc tiến độ (% / số trận thắng / region / địa điểm / trainer)
  2. Click Battle → /battle-sidequest/<id>/
  3. Battle dùng chung engine với tower (agent/battle.py):
     choose (tương khắc hệ) → select move (eff × power × STAB) → result → end
  4. Thắng → quay lại sidequests → trận tiếp theo

Chống treo + auto re-login giống tower.
"""
import asyncio
import random
from loguru import logger
from playwright.async_api import Page
from agent.chat import process_commands
from agent.team import get_cached_team
from agent.battle import (
    read_battle_state, select_move_and_attack, click_continue,
    select_pokemon_and_start, reset_battle_tracking,
)
from agent.pokedex import rank_team_choices, rank_moves_full
from agent.utils import RestScheduler
from config import config
from agent.battle_tower import (
    _get_types, _ensure_types, _human_delay, _attach_network_logger,
)
import agent.ui as ui

SQ_URL = "https://www.pokemon-vortex.com/sidequests/"


async def read_sidequest_page(page: Page) -> dict:
    """Đọc trang sidequests: tiến độ + trận kế tiếp + nút claim (khi xong region)."""
    return await page.evaluate(r"""() => {
        const body = document.body?.innerText || '';
        const pct  = body.match(/(\d+)%\s*Complete/i);
        const won  = body.match(/([\d,]+)\s*sidequest battles won/i);
        let region = '', location = '', opponent = '';
        const nb = body.indexOf('Next Battle');
        if (nb >= 0) {
            const lines = body.slice(nb).split('\n').map(s => s.trim()).filter(s => s && s !== 'Battle');
            region   = lines[1] || '';
            location = lines[2] || '';
            opponent = lines[3] || '';
        }
        const link = document.querySelector('a[href*="battle-sidequest"]');
        // Nút claim (hoàn thành region): button / submit / link chứa claim|prize|reward|collect
        const txt = e => (e?.innerText || e?.value || '').replace(/\s+/g, ' ').trim();
        const claimEl = Array.from(
            document.querySelectorAll('button, input[type="submit"], input[type="button"], a')
        ).find(el => /claim|collect.*prize|prize.*claim|claim.*reward/i.test(txt(el)));
        return {
            pct:        pct ? parseInt(pct[1]) : null,
            battles_won: won ? won[1] : null,
            region, location, opponent,
            battle_href: link ? link.href : null,
            claim_text:  claimEl ? txt(claimEl).slice(0, 60) : null,
        };
    }""")


async def claim_region_prize(page: Page) -> bool:
    """
    Bấm nút Claim sau khi hoàn thành 1 region. Lưu HTML trước khi claim
    (để tinh chỉnh selector nếu cần) + log phần thưởng nhận được.
    """
    import time as _time
    try:
        html = await page.content()
        fn = f"logs/sq_claim_{int(_time.time())}.html"
        with open(fn, "w", encoding="utf-8") as f:
            f.write(html)
        logger.info(f"[SQ] Đã lưu trang claim: {fn}")
    except Exception:
        pass

    body_before = ""
    try:
        body_before = await page.evaluate("() => document.body?.innerText || ''")
    except Exception:
        pass

    clicked = await page.evaluate(r"""() => {
        const txt = e => (e?.innerText || e?.value || '').replace(/\s+/g, ' ').trim();
        const el = Array.from(
            document.querySelectorAll('button, input[type="submit"], input[type="button"], a')
        ).find(x => /claim|collect.*prize|prize.*claim|claim.*reward/i.test(txt(x)));
        if (!el) return false;
        const f = el.closest('form');
        if (el.tagName === 'A' || el.getAttribute('onclick')) {
            el.click();                       // link / nút có onclick riêng
            return true;
        }
        if (f) {                              // submit trong form → requestSubmit (tin cậy)
            f.querySelectorAll("input[type='submit'], button").forEach(b => { b.disabled = false; });
            if (typeof f.requestSubmit === 'function') {
                try { f.requestSubmit(el); } catch (e) { f.requestSubmit(); }
            } else { el.click(); }
            return true;
        }
        el.click();
        return true;
    }""")
    if not clicked:
        logger.warning("[SQ] Không click được nút claim")
        return False

    await asyncio.sleep(3.0)
    # Log phần thưởng: phần text mới xuất hiện sau khi claim
    try:
        body_after = await page.evaluate("() => document.body?.innerText || ''")
        new_lines = [l.strip() for l in body_after.split("\n")
                     if l.strip() and l.strip() not in body_before][:6]
        if new_lines:
            for l in new_lines:
                ui.add_log(f"[bold yellow]🎁 {l[:90]}[/bold yellow]")
            logger.info(f"[SQ] Phần thưởng: {' | '.join(new_lines)[:200]}")
    except Exception:
        pass
    return True


def _detect_sq_screen(url: str) -> str:
    u = url.lower()
    if "/battle-sidequest/" in u or "/battle/" in u:
        return "battle"
    if "/sidequests" in u:
        return "overview"
    return "other"


async def run_sidequest(page: Page, max_wins: int = 0):
    """Main loop Sidequests. max_wins > 0 → dừng khi thắng đủ (0 = chạy mãi)."""
    wins = losses = total = 0
    battle_started = False
    battle_flags = {"won": False, "lost": False, "last_output": ""}
    stuck_sig, stuck_count = None, 0
    failed_choose: set[str] = set()
    throttle_fails = 0
    cooldown_round = 0
    current_opponent = ""

    def _reset_battle():
        nonlocal battle_started, battle_flags, stuck_sig, stuck_count
        battle_started = False
        battle_flags = {"won": False, "lost": False, "last_output": ""}
        stuck_sig, stuck_count = None, 0
        failed_choose.clear()
        reset_battle_tracking()

    async def _finalize_battle():
        nonlocal wins, losses, total, battle_started
        if not battle_started:
            return
        battle_started = False
        total += 1
        if battle_flags["lost"] and not battle_flags["won"]:
            losses += 1
            ui.add_log(f"[bold red]💀 SQ thua {current_opponent}. Losses: {losses}[/bold red]")
            logger.info(f"[SQ] DEFEAT — {wins}W/{losses}L")
        else:
            wins += 1
            ui.add_log(f"[bold green]🏆 SQ thắng {current_opponent}! Wins: {wins}[/bold green]")
            logger.info(f"[SQ] VICTORY #{wins} — {wins}W/{losses}L")
        ui.update(wins=wins, losses=losses, battles=total)

    async def _throttle_cooldown():
        nonlocal throttle_fails, cooldown_round
        battle_url = page.url
        base = [80, 180, 480][min(cooldown_round, 2)]
        wait_s = base * random.uniform(0.9, 1.2)
        cooldown_round += 1
        logger.warning(f"[SQ] Server không phản hồi — nghỉ {wait_s:.0f}s (lần {cooldown_round})")
        ui.add_log(f"[yellow]⏸ Nghỉ {wait_s:.0f}s (lần {cooldown_round})[/yellow]")
        try:
            await page.goto(SQ_URL, wait_until="domcontentloaded")
        except Exception:
            pass
        await asyncio.sleep(wait_s)
        try:
            await page.goto(battle_url, wait_until="domcontentloaded")
        except Exception:
            pass
        await asyncio.sleep(1.5)
        throttle_fails = 0

    ui.add_log("[bold cyan]===== Auto Sidequests bắt đầu! =====[/bold cyan]")
    logger.info(f"[SQ] Started (max_wins={max_wins or '∞'})")

    _attach_network_logger(page)
    await page.goto(SQ_URL, wait_until="domcontentloaded")
    await asyncio.sleep(1.5)

    rest = RestScheduler(config.REST_AFTER_MIN, config.REST_AFTER_MAX,
                         config.REST_HOURS, config.REST_ENABLED, tag="SQ")

    while True:
        if not await process_commands({"battles": total, "wins": wins, "losses": losses}):
            logger.info("[SQ] Stopped by command")
            break
        if max_wins and wins >= max_wins:
            logger.info(f"[SQ] Đạt mục tiêu {max_wins} trận thắng — dừng")
            break

        # Nghỉ ngơi cho GPU sau mỗi ~N trận (random)
        if await rest.maybe_rest(total, on_log=ui.add_log):
            try:
                await page.goto(SQ_URL, wait_until="domcontentloaded")
                await asyncio.sleep(1.5)
            except Exception:
                pass
            continue

        try:
            # Session hết hạn → tự đăng nhập lại
            if "/login" in page.url.lower():
                logger.warning("[SQ] Session hết hạn — đăng nhập lại...")
                ui.add_log("[yellow]⚠ Session hết hạn — đang đăng nhập lại[/yellow]")
                from agent.login import login as _relogin
                if await _relogin(page):
                    await page.goto(SQ_URL, wait_until="domcontentloaded")
                    await asyncio.sleep(1.5)
                else:
                    await asyncio.sleep(30)
                continue

            screen = _detect_sq_screen(page.url)
            ui.update(screen="sidequest", zone="Sidequests",
                      battles=total, wins=wins, action=screen)

            # ── 1. OVERVIEW: đọc tiến độ, click Battle ──
            if screen == "overview":
                await _finalize_battle()
                _reset_battle()

                sq = await read_sidequest_page(page)
                current_opponent = sq.get("opponent") or "?"
                if not sq.get("battle_href"):
                    # Hoàn thành region → nút Battle biến mất, thay bằng Claim
                    if sq.get("claim_text"):
                        ui.add_log(
                            f"[bold yellow]🏆 HOÀN THÀNH REGION! Nhận quà: "
                            f"'{sq['claim_text']}'[/bold yellow]")
                        logger.info(f"[SQ] Region complete — claiming: {sq['claim_text']}")
                        await _human_delay(1.5, 3.0)
                        await claim_region_prize(page)
                        await page.goto(SQ_URL, wait_until="domcontentloaded")
                        await asyncio.sleep(1.5)
                        continue
                    logger.warning("[SQ] Không thấy nút Battle/Claim — reload")
                    await asyncio.sleep(3)
                    await page.reload(wait_until="domcontentloaded")
                    await asyncio.sleep(1.5)
                    continue

                ui.add_log(
                    f"[bold magenta]═ SQ {sq.get('region','?')} · {sq.get('location','?')} · "
                    f"{current_opponent} ({sq.get('pct','?')}% · {sq.get('battles_won','?')} won) ═[/bold magenta]")
                logger.info(
                    f"[SQ] {sq.get('region')} / {sq.get('location')} vs {current_opponent} "
                    f"— {sq.get('pct')}% complete, {sq.get('battles_won')} won")
                ui.update(pokemon=current_opponent, variant=None)

                # Nghỉ giữa các trận như người chơi
                await _human_delay(4.0, 8.0)
                try:
                    link = page.locator("a[href*='battle-sidequest']").first
                    await link.click(timeout=5000)
                except Exception:
                    await page.goto(sq["battle_href"], wait_until="domcontentloaded")
                battle_started = True
                try:
                    await page.wait_for_selector("#pokeChoose, #attackSelection, #attackForm",
                                                 state="attached", timeout=20000)
                except Exception:
                    logger.warning("[SQ] Battle form không xuất hiện sau 20s")
                await asyncio.sleep(0.5)

            # ── 2. BATTLE (engine chung battle.py) ──
            elif screen == "battle":
                bstate = await read_battle_state(page)
                phase = bstate.get("phase", "none")
                battle_started = True

                # Watchdog
                sig = (phase,
                       (bstate.get("enemy") or {}).get("hp"),
                       (bstate.get("me") or {}).get("hp"),
                       (bstate.get("output") or "")[:60])
                if sig == stuck_sig:
                    stuck_count += 1
                else:
                    stuck_sig, stuck_count = sig, 0
                if stuck_count >= 4:
                    logger.warning(f"[SQ] Stuck tại phase={phase} — reload")
                    ui.add_log("[yellow]⟳ Treo — reload lại trận[/yellow]")
                    await page.reload(wait_until="domcontentloaded")
                    stuck_sig, stuck_count = None, 0
                    await asyncio.sleep(1.5)
                    continue

                # Output mới
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

                # 2a. Chọn Pokemon ra trận theo tương khắc
                if phase == "choose":
                    choices = bstate.get("choose_options", [])
                    if not choices:
                        await asyncio.sleep(1.0)
                        continue
                    usable = [c for c in choices if str(c.get("value")) not in failed_choose]
                    if not usable:
                        failed_choose.clear()
                        usable = choices
                    # Đối thủ: con đang trên sân, hoặc con đầu trong panel đối thủ
                    opps = bstate.get("opponents") or []
                    alive_opps = [o for o in opps if (o.get("hp") or 1) > 0]
                    enemy_next = enemy.get("name") \
                        or (alive_opps[0]["name"] if alive_opps else "") \
                        or (opps[0]["name"] if opps else "")
                    if enemy_next:
                        await _ensure_types([enemy_next])
                    team_moves = {pk.get("name"): pk.get("moves", [])
                                  for pk in (get_cached_team() or [])}
                    ranked = await rank_team_choices(usable, team_moves, enemy_next)
                    top = ranked[0] if ranked else (
                        next((c for c in usable if not c.get("fainted")), None))
                    if top:
                        ui.add_log(
                            f"[cyan]Ra trận [bold]{top['name']}[/bold] vs {enemy_next or '?'} — "
                            f"{top.get('reason', '?')}[/cyan]")
                        logger.info(f"[SQ] Send {top['name']} vs {enemy_next} ({top.get('reason','')})")
                        await _human_delay()
                        status = await select_pokemon_and_start(page, str(top["value"]))
                        if status == "ok":
                            throttle_fails = 0
                            cooldown_round = 0
                        elif status == "rejected":
                            failed_choose.add(str(top["value"]))
                            logger.warning(f"[SQ] Server từ chối {top['name']} — thử con khác")
                        else:
                            throttle_fails += 1
                            if throttle_fails >= 2:
                                await _throttle_cooldown()
                    else:
                        await asyncio.sleep(1.0)

                # 2b. Chọn move tốt nhất + Attack
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

                    ranked_moves = await rank_moves_full(moves, opp_types, my_types)
                    best = ranked_moves[0]
                    idx, eff = best["index"], best["multiplier"]
                    mv = moves[idx]
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
                        f"[SQ] {me.get('name')}({me.get('hp')}HP) → {mv.get('name')} "
                        f"({mv.get('type')}, {eff:.1f}x) vs {enemy_name}({enemy.get('hp')}HP)")

                    await _human_delay()
                    r = await select_move_and_attack(page, idx)
                    if r.get("ok"):
                        throttle_fails = 0
                        cooldown_round = 0
                    else:
                        throttle_fails += 1
                        logger.warning(f"[SQ] Attack không phản hồi ({throttle_fails} lần)")
                        if throttle_fails >= 2:
                            await _throttle_cooldown()

                # 2c. Kết quả lượt → Continue
                elif phase == "result":
                    ui.update(action="Continue...")
                    await _human_delay(0.8, 1.6)
                    await click_continue(page)

                # 2d. Battle DOM biến mất → trận kết thúc
                else:
                    await _finalize_battle()
                    await page.goto(SQ_URL, wait_until="domcontentloaded")
                    await asyncio.sleep(1.0)

            else:
                logger.warning(f"[SQ] Out of sidequests, returning. URL={page.url[:60]}")
                await page.goto(SQ_URL, wait_until="domcontentloaded")
                await asyncio.sleep(1.5)

            await asyncio.sleep(0.3)

        except Exception as e:
            logger.error(f"[SQ] Loop error: {e}")
            ui.add_log(f"[red]SQ ERR: {str(e)[:70]}[/red]")
            await asyncio.sleep(2)
            try:
                if _detect_sq_screen(page.url) == "other":
                    await page.goto(SQ_URL, wait_until="domcontentloaded")
                    await asyncio.sleep(1.5)
            except Exception:
                pass

    logger.info(f"[SQ] Final: {wins}W / {losses}L / {total} battles")
    ui.add_log(f"[cyan]Sidequests done: {wins}W / {losses}L / {total}[/cyan]")
    return {"wins": wins, "losses": losses, "battles": total}
