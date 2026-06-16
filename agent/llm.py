"""
LLM Brain — Ollama qwen3 điều khiển encounter/battle.
Navigation là rule-based, LLM chỉ được gọi cho encounter/battle.
"""
import json
import re
import os
import httpx
from loguru import logger

OLLAMA_BASE = "http://localhost:11434"
MODEL       = "qwen3:latest"

# ── Game knowledge (load từ file nếu có, fallback hardcoded) ─────────────
_KNOWLEDGE_FILE = "game_knowledge.txt"
_KNOWLEDGE_URLS = [
    "https://pokemon-vortex.fandom.com/wiki/Battle",
    "https://pokemon-vortex.fandom.com/wiki/Catching_Pokemon",
    "https://pokemon-vortex.fandom.com/wiki/Unique_Pokemon",
]

_HARDCODED_KNOWLEDGE = """
=== POKEMON VORTEX — GAME MECHANICS ===

WILD ENCOUNTER FLOW:
1. Walking on grass/route tiles randomly triggers encounter popup
2. Popup shows: Pokemon name, level, gender icon, [Battle!] button
3. TWO options when encounter starts:
   - Click "Battle!" → enters turn-by-turn battle
   - (Sometimes) Click catch/pokeball icon → attempt to catch directly

BATTLE MECHANICS:
- Turn-based: each turn pick ONE move (index 0-3)
- Each move has: name, type, power, PP (limited uses)
- Type chart applies: Fire > Grass > Water > Fire, Electric > Water > Ground...
- Standard super-effective = 2x damage, not very effective = 0.5x
- Battle ends when enemy HP = 0 → you win XP
- If your Pokemon faints → next in party continues
- After win: "battle_result" shows "Victory" → action=next to close

CATCHING:
- During encounter (before clicking Battle!): catch button may appear
- Lower enemy HP → better catch rate (in most battles)
- Legendary/rare Pokemon = lower base catch rate
- After successful catch: Pokemon added to your box

RARE/VARIANT POKEMON — HIGHEST PRIORITY:
- Normal: standard colored Pokemon
- Shiny: gold/rainbow sparkle effect — very rare and valuable!
- Dark: dark/shadow colored — rare
- Mystic: glowing blue/purple — rare
- Shadow: dark smoky appearance — rare
- Metallic: shiny silver/metal — rare
All variants are worth FAR more than normal — ALWAYS catch them, never flee!

ZONES:
- town-* / hub-* zones: NO wild Pokemon (safe zones, shops, NPCs)
  → Must navigate OUT of town to find Pokemon
- route-*, cave-*, forest-*, beach-* etc: WILD Pokemon encounters
  → Stay and grind here
- Tile position tracking: if tile doesn't change after move = wall/obstacle
  → Change direction

GRINDING STRATEGY:
- Best zones: routes with common Pokemon (route-1, route-2 etc)
- Walk east/west across the zone to cover ground (sweep pattern)
- When stuck (same tile 4x): change direction south → east → west
- Don't waste time in towns — navigate to nearest route
- battle mode: fight every encounter for XP
- catch mode: catch every encounter for collection
- Both: catch rare variants, battle normal ones

ACTIONS REFERENCE:
- map screen: {"action":"go","direction":"north|south|east|west"}
- encounter screen: {"action":"fight"} or {"action":"flee"}
- battle screen: {"action":"move","index":0-3} or {"action":"catch"} or {"action":"next"}
- after battle_result visible: {"action":"next"} — MUST do this to continue

IMPORTANT RULES:
1. NEVER flee from shiny/dark/mystic/shadow/metallic — ALWAYS catch
2. battle_result visible → action=next (don't re-fight)
3. In town? → Go south/east/west to exit ASAP
4. Stuck? → Try perpendicular direction
5. Encounter screen → fight immediately (don't waste ticks)
"""


async def load_knowledge() -> str:
    """Load game knowledge từ file cache hoặc fetch mới."""
    # Dùng cache nếu có và dưới 7 ngày
    if os.path.exists(_KNOWLEDGE_FILE):
        age_days = (
            __import__("time").time() - os.path.getmtime(_KNOWLEDGE_FILE)
        ) / 86400
        if age_days < 7:
            try:
                with open(_KNOWLEDGE_FILE, encoding="utf-8") as f:
                    content = f.read()
                if len(content) > 200:
                    logger.info(f"[KNOWLEDGE] Loaded from cache ({len(content)} chars)")
                    return content
            except Exception:
                pass

    # Fetch mới từ wiki
    fetched = await _fetch_wiki_knowledge()
    if fetched:
        try:
            with open(_KNOWLEDGE_FILE, "w", encoding="utf-8") as f:
                f.write(fetched)
            logger.info(f"[KNOWLEDGE] Fetched + saved ({len(fetched)} chars)")
        except Exception:
            pass
        return fetched

    return _HARDCODED_KNOWLEDGE


async def _fetch_wiki_knowledge() -> str:
    """Fetch text từ Pokemon Vortex wiki."""
    combined = []
    async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
        for url in _KNOWLEDGE_URLS:
            try:
                resp = await client.get(url, headers={"User-Agent": "Mozilla/5.0"})
                if resp.status_code == 200:
                    # Extract text from HTML (cơ bản)
                    text = re.sub(r"<[^>]+>", " ", resp.text)
                    text = re.sub(r"\s+", " ", text).strip()
                    # Lấy phần relevant (tối đa 800 chars mỗi trang)
                    combined.append(f"[Source: {url}]\n{text[:800]}")
                    logger.info(f"[KNOWLEDGE] Fetched {url}")
            except Exception as e:
                logger.debug(f"[KNOWLEDGE] {url}: {e}")

    if combined:
        return _HARDCODED_KNOWLEDGE + "\n\n=== WIKI EXTRACTS ===\n" + "\n\n".join(combined)
    return ""


# ── System prompt (khởi tạo sau khi load knowledge) ──────────────────────
_extra_knowledge = ""   # Được set bởi main.py sau await load_knowledge()

_BASE_SYSTEM_PROMPT = """You are the AI brain of a Pokemon Vortex grinding bot.
You receive the current game state and return ONE action as JSON.

{KNOWLEDGE}

=== OUTPUT FORMAT ===
Respond with exactly ONE JSON object. No markdown, no explanation outside JSON.
Examples:
  {"action": "go", "direction": "east", "reason": "sweeping east to find Pokemon"}
  {"action": "fight", "reason": "wild Pokemon encountered, starting battle"}
  {"action": "move", "index": 0, "reason": "using first move"}
  {"action": "next", "reason": "battle won, closing result screen"}
  {"action": "catch", "reason": "catching rare variant"}

=== DECISION PRIORITY ===
1. If battle phase=result (attack output shown) → action=next (MANDATORY to continue)
2. If screen=encounter AND is_rare=true → action=catch (NEVER flee rare)
3. If screen=encounter → action=fight (mode=catch? try catch first)
3b. If battle phase=choose (select a Pokemon to send out) →
   {"action":"switch","value":"<pokemon_id>"} — use analysis.switch_ranking (sorted best first,
   score = super-effective moves vs enemy + resistance to enemy STAB). Pick analysis.recommended_switch.value.
4. If battle phase=select → pick the BEST move:
   - state.analysis.move_ranking lists moves sorted by type effectiveness (multiplier 2 = super effective, 0.5 = weak, 0 = immune)
   - Default: use analysis.best_move_index
   - NEVER pick a move with multiplier 0 (no damage!)
   - If my hp_pct < 25 and a Potion item is available → {"action":"item","name":"Potion"}
   - memory LESSON lines contain advice from past LOSSES vs this enemy — follow them
5. If screen=map AND is_town=true → navigate OUT (south/east/west)
6. If screen=map → sweep east/west, shift south to cover ground
7. If stuck (same tile in recent_path 3+ times) → change direction

Extra action available in battle: {"action":"item","name":"Potion"} to heal / use item.
"""

def _build_system_prompt() -> str:
    return _BASE_SYSTEM_PROMPT.replace("{KNOWLEDGE}", _extra_knowledge or _HARDCODED_KNOWLEDGE)


# ── Main decide function ──────────────────────────────────────────────────

async def decide(game_state: dict) -> dict:
    """Nhận game_state, trả về action dict."""
    instruction_note = ""
    if game_state.get("user_instruction"):
        instruction_note = (
            f"\n\n*** USER COMMAND (highest priority): "
            f"{game_state['user_instruction']} ***"
        )

    user_msg = (
        f"Current game state:\n"
        f"{json.dumps(game_state, ensure_ascii=False, indent=2)}"
        f"{instruction_note}\n\n"
        f"What is your next action? Reply with ONE JSON object only."
    )

    payload = {
        "model": MODEL,
        "messages": [
            {"role": "system", "content": _build_system_prompt()},
            {"role": "user",   "content": user_msg},
        ],
        "stream": False,
        # Không dùng format:"json" — xung đột với thinking của qwen3
        "think": False,  # API-level disable thinking (Ollama ≥0.9) — nhanh + không bị content rỗng
        "options": {"temperature": 0.2, "num_predict": 1024},
    }

    raw_content = ""
    try:
        async with httpx.AsyncClient(base_url=OLLAMA_BASE, timeout=60.0) as client:
            resp = await client.post("/api/chat", json=payload)
            resp.raise_for_status()
            raw_content = resp.json()["message"]["content"]

        # Strip <think>...</think> tags
        content = re.sub(r"<think>.*?</think>", "", raw_content, flags=re.DOTALL).strip()

        if not content:
            logger.warning("[LLM] Model chỉ output thinking, không có JSON")
            return _fallback(game_state)

        # Parse JSON
        try:
            action = json.loads(content)
        except json.JSONDecodeError:
            match = re.search(r"\{.*\}", content, re.DOTALL)
            if not match:
                raise ValueError(f"No JSON: {content[:120]}")
            action = json.loads(match.group())

        reason = action.get("reason", "")
        logger.info(
            f"[LLM] {action.get('action','?')} "
            f"{action.get('direction','') or action.get('index','')}"
            f"{'  — ' + reason[:50] if reason else ''}"
        )
        return action

    except Exception as e:
        logger.warning(f"[LLM] Lỗi: {e}")
        logger.debug(f"[LLM] Raw: {raw_content[:200]}")

    return _fallback(game_state)


async def llm_analyze(prompt: str, max_tokens: int = 512) -> str:
    """Goi LLM lay plain text analysis (khong parse JSON)."""
    payload = {
        "model": MODEL,
        "messages": [
            {"role": "system", "content": (
                "You are a Pokemon battle strategist. Analyze the given data and provide "
                "a concise battle strategy in plain text (no JSON, no markdown headers). "
                "Be direct and specific. Max 6 lines."
            )},
            {"role": "user", "content": prompt},
        ],
        "stream": False,
        "think": False,
        "options": {"temperature": 0.3, "num_predict": max_tokens},
    }
    try:
        async with httpx.AsyncClient(base_url=OLLAMA_BASE, timeout=45.0) as client:
            resp = await client.post("/api/chat", json=payload)
            resp.raise_for_status()
            raw = resp.json()["message"]["content"]
        text = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL).strip()
        if text:
            logger.info(f"[LLM] analyze: {text[:80]}")
            return text
        # qwen3 đôi khi chỉ output thinking — lấy nội dung trong <think> làm fallback
        think_m = re.search(r"<think>(.*?)</think>", raw, flags=re.DOTALL)
        if think_m:
            inner = think_m.group(1).strip()
            if inner:
                logger.info(f"[LLM] analyze (from thinking): {inner[:60]}")
                return inner[:300]
        logger.warning("[LLM] analyze: empty")
    except Exception as e:
        logger.warning(f"[LLM] analyze error: {e}")
    return ""


async def _chat_raw(system: str, user: str, max_tokens: int = 700, temperature: float = 0.4) -> str:
    """Gọi Ollama, strip thinking tags, trả plain text."""
    payload = {
        "model": MODEL,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user",   "content": user},
        ],
        "stream": False,
        "options": {"temperature": temperature, "num_predict": max_tokens},
    }
    async with httpx.AsyncClient(base_url=OLLAMA_BASE, timeout=90.0) as client:
        resp = await client.post("/api/chat", json=payload)
        resp.raise_for_status()
        raw = resp.json()["message"]["content"]
    return re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL).strip()


# ── Chat hội thoại người ↔ agent ─────────────────────────────────────────

_CHAT_SYSTEM = """Bạn là trợ lý AI của Pokemon Vortex Agent — một bot tự động grind/bắt Pokemon.
Người dùng chat với bạn bằng tiếng Việt (hoặc tiếng Anh). Bạn trả lời NGẮN GỌN, thân thiện, bằng tiếng Việt.

Bạn được cung cấp CONTEXT về trạng thái agent (đội hình, thống kê, trận hiện tại, memory).
Dựa vào đó trả lời câu hỏi. Nếu người dùng RA LỆNH cho agent (vd: "đi bắt pikachu", "ưu tiên đánh nhanh",
"tránh pokemon mạnh"), hãy đưa lệnh đó vào field "instruction" (tiếng Anh, ngắn gọn cho bot hiểu).

Trả lời bằng JSON DUY NHẤT, không markdown:
{"reply": "câu trả lời tiếng Việt", "instruction": "english command for the bot hoặc null"}
"""


async def chat_reply(user_text: str, context: dict) -> dict:
    """Trả lời chat của user. Returns {reply, instruction|None}."""
    user_msg = (
        f"CONTEXT (trạng thái agent hiện tại):\n{json.dumps(context, ensure_ascii=False, default=str)[:4000]}\n\n"
        f"Tin nhắn của người dùng: {user_text}"
    )
    try:
        text = await _chat_raw(_CHAT_SYSTEM, user_msg, max_tokens=600)
        try:
            obj = json.loads(text)
        except json.JSONDecodeError:
            m = re.search(r"\{.*\}", text, re.DOTALL)
            obj = json.loads(m.group()) if m else {"reply": text, "instruction": None}
        reply = (obj.get("reply") or "").strip() or "..."
        instr = obj.get("instruction")
        if isinstance(instr, str) and instr.strip().lower() in ("null", "none", ""):
            instr = None
        return {"reply": reply, "instruction": instr}
    except Exception as e:
        logger.warning(f"[LLM] chat_reply: {e}")
        return {"reply": f"(Lỗi LLM: {e})", "instruction": None}


# ── Học từ trận thua ──────────────────────────────────────────────────────

async def learn_from_loss(battle_summary: dict) -> str:
    """
    Phân tích trận thua → 1 bài học ngắn (English, cho LLM context các trận sau).
    battle_summary: {enemy, enemy_types, my_pokemon, moves_used, turns, last_output, ...}
    """
    prompt = (
        "We LOST this Pokemon battle. Analyze why and give ONE concise lesson "
        "(max 2 sentences, English) to avoid losing next time vs this enemy. "
        "Focus on: move choice (type effectiveness), when to use items/heal, when to flee.\n\n"
        f"Battle data:\n{json.dumps(battle_summary, ensure_ascii=False, default=str)[:2500]}\n\n"
        "Reply with ONLY the lesson text, no preamble."
    )
    try:
        lesson = await _chat_raw(
            "You are a Pokemon battle coach. Output only the lesson, short and actionable.",
            prompt, max_tokens=200, temperature=0.3,
        )
        return lesson.strip().strip('"')[:400]
    except Exception as e:
        logger.warning(f"[LLM] learn_from_loss: {e}")
        return ""


# ── Tối ưu đội hình ───────────────────────────────────────────────────────

async def optimize_team(team: list[dict], summary: dict, lessons: list[dict],
                        team_types: dict | None = None) -> str:
    """
    Phân tích đội hình + lịch sử thắng thua → khuyến nghị đội hình tối ưu (tiếng Việt).
    """
    prompt = (
        "Phân tích đội hình Pokemon Vortex của tôi và lịch sử thắng/thua, "
        "rồi đưa ra khuyến nghị TỐI ƯU ĐỘI HÌNH bằng tiếng Việt:\n"
        "1. Đánh giá độ phủ type (type coverage) — thiếu/thừa type nào\n"
        "2. Pokemon nào yếu nhất nên thay, thay bằng type gì\n"
        "3. Move nào nên đổi (dựa trên move stats thắng/thua)\n"
        "4. Chiến thuật với các đối thủ hay thua\n"
        "Tối đa 10 dòng, gạch đầu dòng, cụ thể.\n\n"
        f"ĐỘI HÌNH: {json.dumps(team, ensure_ascii=False, default=str)[:1500]}\n"
        f"TYPES CỦA ĐỘI: {json.dumps(team_types or {}, ensure_ascii=False)[:600]}\n"
        f"THỐNG KÊ: {json.dumps(summary, ensure_ascii=False, default=str)[:1500]}\n"
        f"BÀI HỌC TỪ CÁC TRẬN THUA: {json.dumps(lessons, ensure_ascii=False, default=str)[:1000]}"
    )
    try:
        return await _chat_raw(
            "Bạn là chuyên gia chiến thuật Pokemon. Trả lời tiếng Việt, ngắn gọn, gạch đầu dòng.",
            prompt, max_tokens=800, temperature=0.4,
        )
    except Exception as e:
        logger.warning(f"[LLM] optimize_team: {e}")
        return f"(Không phân tích được: {e})"


def _fallback(gs: dict) -> dict:
    """Rule-based fallback khi LLM không respond."""
    screen = gs.get("screen", "map")
    enc    = gs.get("encounter") or {}

    if screen == "battle":
        if enc.get("battle_result"):
            return {"action": "next"}
        moves = enc.get("moves", [])
        if moves:
            return {"action": "move", "index": 0, "reason": "fallback"}
        return {"action": "wait", "reason": "fallback: no moves"}

    if screen == "encounter":
        if enc.get("is_rare"):
            return {"action": "catch", "reason": "fallback: rare → catch"}
        mode = gs.get("mode", "battle")
        if mode == "catch" and enc.get("catch_available"):
            return {"action": "catch", "reason": "fallback: catch mode"}
        return {"action": "fight", "reason": "fallback: fight"}

    for d in ["east", "south", "west", "north"]:
        return {"action": "go", "direction": d, "reason": "fallback: explore"}
    return {"action": "wait", "reason": "fallback"}
