"""
Lay thong tin doi hinh Pokemon tu trang /team/ va /pokehub/ID/.

HTML da xac nhan tu team_page.html:
  <div class="front face">
    <h4 class="color-maroon textTrim">
      <a onclick="pokedexTab(...)">Metallic Garchomp</a>
      <i class="fa-solid fa-mars male"></i>
    </h4>
    <div class="pokeball-small"><img src="..."></div>
    <p><span>Level:</span> 100<br><span>Experience:</span> 63,710</p>
    <p class="teamOptions font-20">
      <a href="/evolve/ID/">...</a>
      <a href="/change-attacks/ID/">...</a>
      <a href="/pokehub/ID/">...</a>   <- detail page co 4 moves
      <select name="slot_change">...</select>
    </p>
  </div>
"""
import asyncio
import time
from loguru import logger
from playwright.async_api import Page

_cache: list[dict] = []
_last_fetch: float = 0.0
CACHE_TTL = 120   # 2 phut

TEAM_URL  = "https://www.pokemon-vortex.com/team/"
POKE_BASE = "https://www.pokemon-vortex.com"


async def fetch_team(page: Page) -> list[dict]:
    """Lay du lieu 6 Pokemon trong doi hinh (ten, level, exp, 4 moves)."""
    global _cache, _last_fetch
    if time.time() - _last_fetch < CACHE_TTL and _cache:
        return _cache

    new_page = None
    try:
        context  = page.context
        new_page = await context.new_page()

        # --- Buoc 1: Lay team page ---
        await new_page.goto(TEAM_URL, wait_until="domcontentloaded", timeout=20000)
        await asyncio.sleep(3.0)

        cards = await _extract_cards(new_page)
        if not cards:
            logger.warning(f"[TEAM] Khong tim thay card nao tren {TEAM_URL}")
            return _cache or []

        logger.info(f"[TEAM] Tim thay {len(cards)} card(s)")

        # --- Buoc 2: Vao pokehub page lay 4 moves cho tung Pokemon ---
        for card in cards:
            detail = card.get("detail_url", "")
            if detail:
                card["moves"] = await _fetch_moves(new_page, detail)
                logger.debug(f"[TEAM] {card['name']} moves: {card['moves']}")
            else:
                card["moves"] = []

        _cache      = cards
        _last_fetch = time.time()
        logger.info(f"[TEAM] Done: {[c['name'] for c in cards]}")
        return cards

    except Exception as e:
        logger.warning(f"[TEAM] fetch_team error: {e}")
        return _cache or []
    finally:
        if new_page:
            try:
                await new_page.close()
            except Exception:
                pass


# ── Scrape 6 cards tu team page ─────────────────────────────────────────────

async def _extract_cards(page: Page) -> list[dict]:
    """
    Selector chinh xac da xac nhan tu HTML:
    Container = div.front.face
    Ten = h4 > a (text content, khong phai onclick attr)
    Level/Exp = regex tu innerText cua container
    Detail URL = a[href*='/pokehub/'] trong container
    """
    return await page.evaluate(r"""() => {
        const results = [];

        // Selector chinh xac: div.front.face la container cua moi card
        let cards = Array.from(document.querySelectorAll('div.front.face'));

        // Fallback: div.front neu khong co "face" class
        if (cards.length === 0) {
            cards = Array.from(document.querySelectorAll('div.front'));
        }

        // Filter: phai co h4 va img (la Pokemon card that su)
        const validCards = cards.filter(c =>
            c.querySelector('h4') && c.querySelector('img')
        );

        validCards.slice(0, 6).forEach(card => {
            // Ten Pokemon: lay text cua <a> ben trong <h4>
            const h4 = card.querySelector('h4');
            let name = '';
            if (h4) {
                const nameLink = h4.querySelector('a');
                if (nameLink) {
                    name = (nameLink.textContent || nameLink.innerText || '').trim();
                } else {
                    // Fallback: lay h4 text, bo gender icon
                    name = (h4.innerText || '').replace(/[♂♀⚥]/g, '').trim();
                }
            }

            // Level va Experience tu innerText
            const text = card.innerText || '';
            const lvMatch  = text.match(/Level:\s*(\d+)/i);
            const expMatch = text.match(/Experience:\s*([\d,]+)/i);
            const level = lvMatch  ? parseInt(lvMatch[1]) : null;
            const exp   = expMatch ? parseInt(expMatch[1].replace(/,/g, '')) : null;

            // Detail URL: dung /change-attacks/ID/ de lay current attacks
            // (pokehub = trang quan ly, change-attacks = trang co 4 moves hien tai)
            const changeAttacksEl = card.querySelector('a[href*="/change-attacks/"]');
            const detail_url = changeAttacksEl
                ? (changeAttacksEl.href || '').split('?')[0]
                : null;

            // Image
            const img    = card.querySelector('img');
            const imgSrc = img ? img.src : '';

            if (name) {
                results.push({ name, level, exp, detail_url, imgSrc, moves: [] });
            }
        });

        return results;
    }""")


# ── Scrape 4 moves tu pokehub page ──────────────────────────────────────────

async def _fetch_moves(page: Page, url: str) -> list[str]:
    """
    pokehub page co section 'Attacks' voi 4 current moves.
    Cau truc can xac nhan; thu nhieu approach de robust.
    """
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=15000)
        await asyncio.sleep(1.5)

        # Save pokehub HTML lan dau de debug (chi ghi neu chua co file)
        try:
            from pathlib import Path
            fname = "pokehub_debug.html"
            if not Path(fname).exists():
                html = await page.content()
                Path(fname).write_text(html, encoding="utf-8")
                logger.debug(f"[TEAM] Saved {fname} ({len(html)} chars)")
        except Exception:
            pass

        moves = await page.evaluate(r"""() => {
            // Selector chinh xac da xac nhan: input[name="original-attack"] trong ul#yourAttacks
            // value attribute = ten move (Dragon Claw, Dig, Crunch, Fire Fang...)
            const radios = Array.from(document.querySelectorAll(
                '#yourAttacks input[name="original-attack"], input[name="original-attack"]'
            ));
            const moves = radios
                .map(r => (r.value || '').trim())
                .filter(v => v.length > 1)
                .slice(0, 4);
            return moves;
        }""")

        return moves

    except Exception as e:
        logger.debug(f"[TEAM] _fetch_moves {url}: {e}")
        return []


def get_cached_team() -> list[dict]:
    return _cache
