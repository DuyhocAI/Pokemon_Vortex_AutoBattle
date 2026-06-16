"""
Battle engine — dựa trên DOM THẬT của Pokemon Vortex v6 (xác nhận từ battle_active.html).

Cấu trúc battle page:
  <form id="attackForm" action="/battle/" onsubmit="get('/battle/',...); return false;">  ← AJAX
    <div id="battleView">
      <h3>Select an Attack | Attack Results</h3>
      <div id="battleScene">
        <div id="battleTopStats">    <h4>Dark Lunatone - Lvl: 100</h4> <strong>HP: <img> 400</strong>
        <div id="battleBottomStats"> <h4>Metallic Garchomp - Lvl: 100</h4> <strong>HP: <img> 400</strong>
      <div id="attackSelection">
        <div class="attack attack-select">
          <label class="attackLabel"><input type="radio" name="attack" value="1-4">MoveName
            <img class="typeImg" src=".../types/Dragon.png">
      <input type="submit" value=" Attack ! ">
  Sau khi attack (AJAX thay nội dung #ajax):
      <div class="attackOutput"> ...attacked... did N HP damage... has fainted... </div>
      <input type="submit" value="Continue..">

Flow chuẩn: select radio (click label) → click " Attack ! " → đợi .attackOutput → Continue.. → lặp.
"""
import asyncio
import re
from playwright.async_api import Page
from loguru import logger

# Max HP theo dõi trong 1 trận (HP đầu tiên nhìn thấy = max)
_hp_max: dict = {"me": None, "enemy": None}


def reset_battle_tracking():
    _hp_max["me"] = None
    _hp_max["enemy"] = None


# ── Đọc toàn bộ battle state trong 1 lần evaluate ──────────────────────────
_READ_STATE_JS = r"""() => {
    const q   = s  => document.querySelector(s);
    const txt = el => el ? (el.innerText || '').trim() : '';
    const vis = el => !!(el && el.offsetParent !== null);

    const heading = txt(q('#battleView h3'));

    // Moves (radio + type từ typeImg)
    const moves = Array.from(document.querySelectorAll('#attackSelection .attack')).map(a => {
        const input = a.querySelector('input[name="attack"]');
        const img   = a.querySelector('img.typeImg');
        let type = null;
        if (img && img.src) {
            const m = img.src.match(/types\/([A-Za-z]+)\.png/);
            if (m) type = m[1];
        }
        return {
            name:    (a.innerText || '').trim(),
            value:   input ? input.value   : null,
            checked: input ? input.checked : false,
            type:    type,
        };
    });

    // Items (itemForm) — gồm cả pokeball nếu có
    const items = Array.from(document.querySelectorAll('#itemView .item')).map(it => {
        const input = it.querySelector('input[name="item"]');
        const qty   = txt(it.querySelector('.itemQuantity'));
        return {
            name:     input ? input.value : txt(it.querySelector('.itemName')),
            qty:      parseInt(qty) || 0,
            disabled: input ? input.disabled : true,
        };
    }).filter(it => it.name);

    // Nút submit visible
    const submits = Array.from(document.querySelectorAll("input[type='submit']")).filter(vis);
    const attack_btn   = submits.some(b => /attack/i.test(b.value || ''));
    const continue_btn = submits.some(b => /continue/i.test(b.value || ''));

    // Sprite images (cho UI)
    const topImg = q('#battleTopPkmn img');
    const botImg = q('#battleBottomPkmn img');

    // ── Màn hình chọn Pokemon ra trận (#pokeChoose) ──
    const chooseEl = q('#pokeChoose');
    const choose_visible = vis(chooseEl);
    const choose_options = chooseEl ? Array.from(chooseEl.querySelectorAll('[id^="slot"]')).map(sl => {
        const input  = sl.querySelector('input[name="active_pokemon"]');
        const nameEl = sl.querySelector('strong a, a[id^="pokeName"]');
        const text   = sl.innerText || '';
        const lv = text.match(/Level:\s*(\d+)/i);
        const hp = text.match(/HP:\s*([\d,]+)/i);
        const hpVal = hp ? parseInt(hp[1].replace(/,/g, '')) : null;
        const fainted = !!(input && input.disabled)
            || sl.classList.contains('battle-poke-fainted')
            || !!sl.querySelector('[style*="line-through"], .fainted')
            || hpVal === 0;
        return {
            value:   input ? input.value : null,
            checked: input ? input.checked : false,
            name:    nameEl ? (nameEl.textContent || '').trim() : null,
            level:   lv ? parseInt(lv[1]) : null,
            hp:      hpVal,
            fainted: fainted,
        };
    }).filter(c => c.value && c.name) : [];

    // ── Đội của đối thủ (trainer/tower battle) ──
    const oppEl = q('#opponentPoke');
    const opponents = oppEl ? Array.from(oppEl.querySelectorAll('.battle-poke-select')).map(d => {
        const nameEl = d.querySelector('strong a');
        const text   = d.innerText || '';
        const lv = text.match(/Level:\s*(\d+)/i);
        const hp = text.match(/HP:\s*([\d,]+)/i);
        return {
            name:  nameEl ? (nameEl.textContent || '').trim() : null,
            level: lv ? parseInt(lv[1]) : null,
            hp:    hp ? parseInt(hp[1].replace(/,/g, '')) : null,
        };
    }).filter(o => o.name) : [];

    return {
        choose_visible: choose_visible,
        choose_options: choose_options,
        opponents:      opponents,
        heading:       heading,
        has_form:      !!q('#attackForm'),
        has_selection: vis(q('#attackSelection')),
        enemy_raw:     txt(q('#battleTopStats h4')),
        enemy_hp_raw:  txt(q('#battleTopStats strong')),
        me_raw:        txt(q('#battleBottomStats h4')),
        me_hp_raw:     txt(q('#battleBottomStats strong')),
        enemy_img:     topImg ? topImg.src : null,
        me_img:        botImg ? botImg.src : null,
        moves:         moves,
        items:         items,
        output:        txt(q('.attackOutput')),
        attack_btn:    attack_btn,
        continue_btn:  continue_btn,
        body_excerpt:  (document.body ? (document.body.innerText || '').slice(0, 3000) : ''),
    };
}"""


def _parse_name_level(raw: str) -> tuple[str | None, int | None]:
    """'Dark Lunatone  - Lvl: 100' → ('Dark Lunatone', 100)"""
    if not raw:
        return None, None
    m = re.match(r"(.+?)\s*-\s*Lvl:\s*(\d+)", raw)
    if m:
        return m.group(1).strip(), int(m.group(2))
    return raw.strip() or None, None


def _parse_hp(raw: str) -> int | None:
    """'HP:  400' → 400"""
    if not raw:
        return None
    m = re.search(r"([\d,]+)\s*$", raw)
    return int(m.group(1).replace(",", "")) if m else None


async def read_battle_state(page: Page) -> dict:
    """
    Đọc trạng thái battle hiện tại. Trả về dict:
      phase: 'select' | 'result' | 'end' | 'none'
      me/enemy: {name, level, hp, hp_max, hp_pct, img}
      moves: [{name, type, value, checked}]
      items: [{name, qty, disabled}]
      output: text kết quả lượt trước
      events: {enemy_fainted, me_fainted, won, lost, critical, super_effective, damage_dealt, damage_taken}
    """
    try:
        raw = await page.evaluate(_READ_STATE_JS)
    except Exception as e:
        logger.debug(f"[BATTLE] read_battle_state: {e}")
        return {"phase": "none"}

    if not raw.get("has_form") and not raw.get("output") and not raw.get("choose_visible"):
        # Không còn battle form — có thể đã kết thúc trận
        body = (raw.get("body_excerpt") or "").lower()
        if any(k in body for k in ("you won", "you have won", "exp gained", "experience gained", "you lost", "you have lost")):
            return {"phase": "end", "output": raw.get("body_excerpt", "")[:500],
                    "events": _parse_events(raw.get("body_excerpt", ""), None, None)}
        return {"phase": "none"}

    enemy_name, enemy_lv = _parse_name_level(raw.get("enemy_raw", ""))
    me_name,    me_lv    = _parse_name_level(raw.get("me_raw", ""))
    enemy_hp = _parse_hp(raw.get("enemy_hp_raw", ""))
    me_hp    = _parse_hp(raw.get("me_hp_raw", ""))

    # Track max HP (giá trị đầu tiên thấy trong trận)
    if enemy_hp is not None and (_hp_max["enemy"] is None or enemy_hp > _hp_max["enemy"]):
        _hp_max["enemy"] = enemy_hp
    if me_hp is not None and (_hp_max["me"] is None or me_hp > _hp_max["me"]):
        _hp_max["me"] = me_hp

    output = raw.get("output", "")
    events = _parse_events(output, me_name, enemy_name)

    if raw.get("choose_visible") and raw.get("choose_options"):
        phase = "choose"
    elif raw.get("has_selection") and raw.get("attack_btn"):
        phase = "select"
    elif output or raw.get("continue_btn"):
        phase = "result"
    else:
        phase = "end"

    def _side(name, lv, hp, key, img):
        hp_max = _hp_max[key]
        return {
            "name": name, "level": lv, "hp": hp, "hp_max": hp_max,
            "hp_pct": round(hp / hp_max * 100, 1) if (hp is not None and hp_max) else None,
            "img": img,
        }

    return {
        "phase":   phase,
        "heading": raw.get("heading"),
        "me":      _side(me_name, me_lv, me_hp, "me", raw.get("me_img")),
        "enemy":   _side(enemy_name, enemy_lv, enemy_hp, "enemy", raw.get("enemy_img")),
        "moves":   [m for m in raw.get("moves", []) if m.get("name")],
        "items":   raw.get("items", []),
        "output":  output,
        "events":  events,
        "continue_btn":   raw.get("continue_btn", False),
        "choose_options": raw.get("choose_options", []),
        "opponents":      raw.get("opponents", []),
    }


def _parse_events(output: str, me_name: str | None, enemy_name: str | None) -> dict:
    """Phân tích text .attackOutput → sự kiện trong lượt."""
    ev = {
        "enemy_fainted": False, "me_fainted": False,
        "won": False, "lost": False,
        "critical": False, "super_effective": False, "not_effective": False,
        "damage_dealt": None, "damage_taken": None,
    }
    if not output:
        return ev
    low = output.lower()

    ev["critical"]        = "critical hit" in low
    ev["super_effective"] = "super effective" in low
    ev["not_effective"]   = "not very effective" in low
    ev["won"]  = any(k in low for k in ("you won", "you have won", "you defeated", "exp gained", "experience gained"))
    ev["lost"] = any(k in low for k in ("you lost", "you have lost", "you were defeated", "no more pokemon", "all of your pokemon have fainted"))

    # "X has fainted" — phân biệt phe ta / địch
    for m in re.finditer(r"([^.\n]*?)\s+has fainted", output, re.I):
        chunk = m.group(1).strip()
        if re.search(r"\byour\b", chunk, re.I) or (me_name and me_name.lower() in chunk.lower()):
            ev["me_fainted"] = True
        elif enemy_name and enemy_name.lower() in chunk.lower():
            ev["enemy_fainted"] = True
        else:
            # Không rõ — đoán theo enemy (dòng faint của địch thường đi sau dòng ta attack)
            ev["enemy_fainted"] = True

    # Damage: "Your X attacked ... did N HP damage" = dealt; "Y attacked your X ... did N" = taken
    for m in re.finditer(r"(.{0,80}?)did\s+([\d,]+)\s+HP damage", output, re.I):
        prefix, dmg = m.group(1), int(m.group(2).replace(",", ""))
        if re.search(r"\byour\b.{0,40}attacked", prefix, re.I):
            ev["damage_dealt"] = dmg
        elif re.search(r"attacked your", prefix, re.I):
            ev["damage_taken"] = dmg
    return ev


# ── Hành động ───────────────────────────────────────────────────────────────

async def select_move_and_attack(page: Page, idx: int) -> dict:
    """
    Chọn move idx (0-3) RỒI bấm Attack — fix lỗi 'chọn xong không tấn công'.
    Trả về {ok, move} — ok=True nếu attack đã gửi và có kết quả.
    """
    # 1. Chọn radio qua label click + verify, force-check bằng JS nếu cần
    sel = await page.evaluate(r"""(idx) => {
        const attacks = Array.from(document.querySelectorAll('#attackSelection .attack'));
        if (!attacks.length) return {ok: false, err: 'no_attack_options'};
        const i = Math.max(0, Math.min(idx, attacks.length - 1));
        const label = attacks[i].querySelector('label.attackLabel');
        const input = attacks[i].querySelector('input[name="attack"]');
        if (label) label.click();
        if (input && !input.checked) {
            input.checked = true;
            input.dispatchEvent(new Event('change', {bubbles: true}));
            input.dispatchEvent(new Event('click',  {bubbles: true}));
        }
        // Cập nhật highlight như JS của site
        attacks.forEach(a => { a.classList.remove('attack-selected'); a.classList.add('attack-select'); });
        attacks[i].classList.add('attack-selected');
        attacks[i].classList.remove('attack-select');
        return {
            ok:   !!(input && input.checked),
            move: (attacks[i].innerText || '').trim(),
        };
    }""", idx)

    if not sel.get("ok"):
        logger.warning(f"[BATTLE] Không chọn được move idx={idx}: {sel.get('err','radio not checked')}")
        return {"ok": False, "move": None}

    move_name = sel.get("move")
    await asyncio.sleep(0.3)

    # 2. Submit #attackForm qua requestSubmit() — button.click() đôi khi không fire
    #    submit trên site này (kể cả click thật), gây timeout 12s vô ích.
    clicked = await _resubmit_form(page, "#attackForm")
    if not clicked:
        # Fallback: click thật qua Playwright
        try:
            btn = page.locator("#attackForm input[type='submit']").first
            if await btn.is_visible(timeout=2000):
                await btn.click(timeout=4000)
                clicked = True
        except Exception:
            pass
    if not clicked:
        logger.warning("[BATTLE] Không tìm thấy nút Attack!")
        return {"ok": False, "move": move_name}

    # 3. Đợi AJAX trả kết quả: .attackOutput xuất hiện HOẶC selection biến mất.
    #    Site disable nút submit khi gửi AJAX; nếu response fail thì nút kẹt disabled
    #    vĩnh viễn (game không retry) → phải tự re-enable + resubmit.
    if await _wait_battle_response(page):
        logger.info(f"[BATTLE] Attack: {move_name}")
        return {"ok": True, "move": move_name}

    logger.warning("[BATTLE] Timeout chờ kết quả attack — resubmit (AJAX có thể đã fail)")
    if await _resubmit_form(page, "#attackForm") and await _wait_battle_response(page):
        logger.info(f"[BATTLE] Attack (retry OK): {move_name}")
        return {"ok": True, "move": move_name}

    logger.warning("[BATTLE] Attack vẫn không có kết quả sau retry")
    return {"ok": False, "move": move_name}


async def _wait_battle_response(page: Page, timeout: int = 12000) -> bool:
    """Đợi AJAX battle trả kết quả: có .attackOutput / selection ẩn / sang màn khác."""
    try:
        await page.wait_for_function(
            """() => {
                if (document.querySelector('.attackOutput')) return true;
                const sel = document.querySelector('#attackSelection');
                if (!sel || sel.offsetParent === null) return true;
                // Nút Attack được enable lại = AJAX đã trả selection mới
                const btn = sel.closest('form')?.querySelector("input[type='submit']");
                return false;
            }""",
            timeout=timeout,
        )
        return True
    except Exception:
        return False


async def _resubmit_form(page: Page, form_sel: str) -> bool:
    """
    Submit form qua requestSubmit() — cách DUY NHẤT tin cậy trên site này.
    (button.click() — kể cả click thật — đôi khi không fire submit ở trạng thái
    full-page render; requestSubmit() chạy validation + onsubmit y như click.)
    """
    try:
        return await page.evaluate(r"""(sel) => {
            const f = document.querySelector(sel);
            if (!f) return false;
            f.querySelectorAll("input[type='submit']").forEach(b => {
                b.disabled = false; b.style.color = '';
            });
            if (typeof f.requestSubmit === 'function') { f.requestSubmit(); return true; }
            const b = f.querySelector("input[type='submit']");
            if (b) { b.click(); return true; }
            return false;
        }""", form_sel)
    except Exception as e:
        logger.debug(f"[BATTLE] _resubmit_form {form_sel}: {e}")
        return False


_CONTINUE_RESUBMIT_JS = r"""() => {
    const btns = Array.from(document.querySelectorAll("input[type='submit']"))
        .filter(b => /continue/i.test(b.value || '') && b.offsetParent !== null);
    if (!btns.length) return false;
    btns.forEach(b => { b.disabled = false; b.style.color = ''; });
    const f = btns[0].form;
    if (f && typeof f.requestSubmit === 'function') { f.requestSubmit(btns[0]); return true; }
    btns[0].click();
    return true;
}"""


async def _wait_after_continue(page: Page, timeout: int = 12000) -> bool:
    """Đợi lượt mới / màn chọn Pokemon / trận kết thúc sau khi bấm Continue."""
    try:
        await page.wait_for_function(
            """() => {
                const sel = document.querySelector('#attackSelection');
                if (sel && sel.offsetParent !== null) return true;       // lượt mới
                const ch = document.querySelector('#pokeChoose');
                if (ch && ch.offsetParent !== null) return true;         // chọn Pokemon mới
                if (!document.querySelector('#attackForm')) return true; // trận kết thúc
                const out = document.querySelector('.attackOutput');
                return !out;                                             // output cũ đã bị thay
            }""",
            timeout=timeout,
        )
        return True
    except Exception:
        return False


async def click_continue(page: Page) -> bool:
    """Bấm Continue.. sau khi xem kết quả lượt — kèm recovery khi AJAX fail."""
    try:
        btn = page.locator("input[type='submit'][value*='Continue']:visible").first
        if await btn.count() == 0:
            return False
        await btn.click(timeout=3000)
    except Exception:
        # Fallback JS: re-enable (nếu bị disable) rồi click
        try:
            if not await page.evaluate(_CONTINUE_RESUBMIT_JS):
                return False
        except Exception:
            return False

    if not await _wait_after_continue(page):
        # AJAX có thể fail → nút bị disable kẹt — re-enable + resubmit 1 lần
        logger.warning("[BATTLE] Timeout sau Continue — resubmit")
        try:
            await page.evaluate(_CONTINUE_RESUBMIT_JS)
        except Exception:
            pass
        if not await _wait_after_continue(page):
            logger.warning("[BATTLE] Continue vẫn kẹt sau retry")
            return False
    await asyncio.sleep(0.4)
    return True


async def attempt_catch(page: Page) -> bool:
    """
    Thử bắt Pokemon: tìm Pokeball trong itemForm (battle) hoặc nút catch (encounter popup).
    """
    # 1. Pokeball trong item form (wild battle)
    try:
        threw = await page.evaluate("""() => {
            const items = Array.from(document.querySelectorAll('#itemView .item'));
            for (const it of items) {
                const input = it.querySelector('input[name="item"]');
                if (!input || input.disabled) continue;
                if (/ball/i.test(input.value || '')) {
                    const label = it.querySelector('label');
                    if (label) label.click();
                    input.checked = true;
                    input.dispatchEvent(new Event('change', {bubbles: true}));
                    const form = input.closest('form');
                    const submit = form ? form.querySelector("input[type='submit']") : null;
                    if (submit) { submit.click(); return input.value; }
                }
            }
            return null;
        }""")
        if threw:
            logger.info(f"[BATTLE] Ném {threw}!")
            await asyncio.sleep(2.5)
            return True
    except Exception as e:
        logger.debug(f"[BATTLE] attempt_catch item: {e}")

    # 2. Fallback: nút catch cũ (encounter popup)
    for sel in ["#catchPokemon", ".catch-btn", "button[value='catch']"]:
        try:
            btn = page.locator(sel).first
            if await btn.is_visible(timeout=800):
                await btn.click()
                await asyncio.sleep(1.5)
                ball = await page.query_selector(".pokeball-item, #useBall")
                if ball:
                    await ball.click()
                    await asyncio.sleep(2.0)
                return True
        except Exception:
            continue
    logger.debug("[BATTLE] Không có Pokeball / nút catch")
    return False


async def use_item(page: Page, item_name: str) -> bool:
    """Dùng item (Potion/Full Heal...) trong battle."""
    try:
        ok = await page.evaluate("""(wanted) => {
            const items = Array.from(document.querySelectorAll('#itemView .item'));
            for (const it of items) {
                const input = it.querySelector('input[name="item"]');
                if (!input || input.disabled) continue;
                if ((input.value || '').toLowerCase().includes(wanted.toLowerCase())) {
                    const label = it.querySelector('label');
                    if (label) label.click();
                    input.checked = true;
                    input.dispatchEvent(new Event('change', {bubbles: true}));
                    const form = input.closest('form');
                    const submit = form ? form.querySelector("input[type='submit']") : null;
                    if (submit) { submit.click(); return true; }
                }
            }
            return false;
        }""", item_name)
        if ok:
            logger.info(f"[BATTLE] Dùng item: {item_name}")
            await asyncio.sleep(2.0)
        return ok
    except Exception as e:
        logger.debug(f"[BATTLE] use_item: {e}")
        return False


async def select_pokemon_and_start(page: Page, radio_value: str) -> str:
    """
    Màn hình 'Select a Pokémon to battle' (#pokeChoose):
    chọn radio active_pokemon=radio_value rồi bấm Continue (form tự giải JS captcha).

    Trả về:
      "ok"          — server chấp nhận, đã sang màn tiếp theo
      "rejected"    — server trả lại màn choose mới (vd Pokemon fainted)
      "no_response" — server không phản hồi (nghi rate-limit) — KHÔNG phải lỗi Pokemon
    """
    sel = await page.evaluate(r"""(val) => {
        const chooseEl = document.querySelector('#pokeChoose');
        if (!chooseEl) return {ok: false, err: 'no_pokeChoose'};
        const input = chooseEl.querySelector(`input[name="active_pokemon"][value="${val}"]`);
        if (!input) return {ok: false, err: 'no_radio_' + val};
        const label = input.closest('label') || chooseEl.querySelector(`label[for="${input.id}"]`);
        if (label) label.click();
        if (!input.checked) {
            input.checked = true;
            input.dispatchEvent(new Event('change', {bubbles: true}));
        }
        // Cập nhật highlight slot như site
        chooseEl.querySelectorAll('[id^="slot"]').forEach(s => {
            s.classList.remove('battle-poke-selected');
            s.classList.add('battle-poke-select');
        });
        const slot = input.closest('[id^="slot"]');
        if (slot) { slot.classList.add('battle-poke-selected'); slot.classList.remove('battle-poke-select'); }
        // Đánh dấu DOM hiện tại — nếu server trả lại màn choose MỚI (từ chối lựa chọn)
        // thì marker biến mất → phát hiện ngay, không chờ timeout
        chooseEl.setAttribute('data-bot-sel', '1');
        return {ok: input.checked};
    }""", str(radio_value))

    if not sel.get("ok"):
        logger.warning(f"[BATTLE] Không chọn được Pokemon {radio_value}: {sel.get('err')}")
        return "rejected"

    await asyncio.sleep(0.3)

    # Submit form chọn Pokemon qua requestSubmit() — button.click() (kể cả click
    # thật của Playwright) KHÔNG fire submit ở trạng thái full-page render.
    clicked = False
    try:
        clicked = await page.evaluate("""() => {
            const f = document.querySelector('#pokeChoose')?.closest('form');
            if (!f) return false;
            f.querySelectorAll("input[type='submit']").forEach(b => {
                b.disabled = false; b.style.color = '';
            });
            if (typeof f.requestSubmit === 'function') { f.requestSubmit(); return true; }
            const b = f.querySelector("input[type='submit']");
            if (b) { b.click(); return true; }
            return false;
        }""")
    except Exception:
        pass
    if not clicked:
        # Fallback: nút Continue visible bất kỳ (qua Playwright)
        try:
            btn = page.locator("input[type='submit'][value*='Continue']:visible").first
            if await btn.count() > 0:
                await btn.click(timeout=3000)
                clicked = True
        except Exception:
            pass
    if not clicked:
        logger.warning("[BATTLE] Không tìm thấy nút Continue ở màn chọn Pokemon")
        return "rejected"

    # Đợi server phản hồi: sang màn move / pokeChoose biến mất / pokeChoose bị THAY MỚI
    async def _wait_choose_response(timeout=12000) -> bool:
        try:
            await page.wait_for_function(
                """() => {
                    const ch = document.querySelector('#pokeChoose');
                    if (!ch || ch.offsetParent === null) return true;
                    if (!ch.hasAttribute('data-bot-sel')) return true;  // DOM mới = server đã trả lời
                    const sel = document.querySelector('#attackSelection');
                    return sel && sel.offsetParent !== null;
                }""",
                timeout=timeout,
            )
            return True
        except Exception:
            return False

    async def _choose_accepted() -> bool:
        """True nếu đã rời màn choose (server chấp nhận lựa chọn)."""
        try:
            return await page.evaluate(
                """() => {
                    const ch = document.querySelector('#pokeChoose');
                    if (!ch || ch.offsetParent === null) return true;
                    const sel = document.querySelector('#attackSelection');
                    return !!(sel && sel.offsetParent !== null);
                }""")
        except Exception:
            return False

    if not await _wait_choose_response():
        # Không có phản hồi nào → AJAX fail, nút bị disable kẹt — resubmit
        logger.warning("[BATTLE] Timeout sau khi chọn Pokemon — resubmit")
        try:
            await page.evaluate(r"""() => {
                const f = document.querySelector('#pokeChoose')?.closest('form');
                if (!f) return false;
                f.querySelectorAll("input[type='submit']").forEach(b => {
                    b.disabled = false; b.style.color = '';
                });
                if (typeof f.requestSubmit === 'function') { f.requestSubmit(); return true; }
                const b = f.querySelector("input[type='submit']");
                if (b) { b.click(); return true; }
                return false;
            }""")
        except Exception:
            pass
        if not await _wait_choose_response():
            # Marker còn nguyên = server chưa từng trả lời → nghi rate-limit
            still_mine = False
            try:
                still_mine = await page.evaluate(
                    "() => !!document.querySelector('#pokeChoose[data-bot-sel]')")
            except Exception:
                pass
            if still_mine:
                logger.warning("[BATTLE] Server không phản hồi choose (nghi rate-limit)")
                await _dump_choose_diag(page)
                return "no_response"
            logger.warning("[BATTLE] Chọn Pokemon kẹt sau retry")
            await _dump_choose_diag(page)
            return "rejected"

    await asyncio.sleep(0.4)
    if await _choose_accepted():
        return "ok"
    # Server trả lại màn choose mới = TỪ CHỐI lựa chọn (vd Pokemon đã fainted)
    logger.warning(f"[BATTLE] Server từ chối lựa chọn {radio_value} (trả lại màn choose)")
    await _dump_choose_diag(page)
    return "rejected"


async def _dump_choose_diag(page: Page):
    """Chẩn đoán khi chọn Pokemon kẹt: log radio states + lưu HTML."""
    import json as _json
    import time as _time
    try:
        diag = await page.evaluate(r"""() => {
            const radios = Array.from(document.querySelectorAll('input[name="active_pokemon"]')).map(r => {
                const slot = r.closest('[id^="slot"]');
                return {
                    value: r.value, checked: r.checked, disabled: r.disabled,
                    slotClass: slot ? slot.className : '',
                    slotText: slot ? (slot.innerText || '').replace(/\s+/g, ' ').slice(0, 90) : '',
                };
            });
            const a = document.getElementById('nojs-solve-a');
            const v = document.getElementById('nojs-solve-v');
            const subs = Array.from(document.querySelectorAll("input[type='submit']"))
                .map(b => ({val: b.value, dis: b.disabled, vis: b.offsetParent !== null}));
            return {
                url: location.href,
                radios, submits: subs,
                cap: a ? {a: a.value, b: document.getElementById('nojs-solve-b')?.value, v: v ? v.value : null} : null,
            };
        }""")
        logger.warning(f"[BATTLE] choose-diag: {_json.dumps(diag, ensure_ascii=False)[:900]}")
        html = await page.content()
        fn = f"logs/stuck_choose_{int(_time.time())}.html"
        with open(fn, "w", encoding="utf-8") as f:
            f.write(html)
        logger.warning(f"[BATTLE] Đã lưu HTML kẹt: {fn}")
    except Exception as e:
        logger.debug(f"[BATTLE] dump diag: {e}")


async def close_encounter(page: Page):
    """Đóng encounter popup (flee trước khi vào battle)."""
    for sel in [".closeWild", "#closeWild", ".close-encounter"]:
        try:
            btn = page.locator(sel).first
            if await btn.is_visible(timeout=800):
                await btn.click()
                await asyncio.sleep(0.6)
                return
        except Exception:
            continue
