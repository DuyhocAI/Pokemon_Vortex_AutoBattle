"""
Player localization dùng WebSocket protocol của Pokemon Vortex.

Protocol (reverse-engineered):
  SEND frames: protobuf binary, tag 0x15 = x float32LE, tag 0x1d = y float32LE
  RECV frames: protobuf binary chứa zone name (length-delimited string) + player coords

Zone naming:
  "town-*"  → trong town, không có wild Pokemon
  khác      → route/grass/route → có wild Pokemon
"""
import asyncio
from playwright.async_api import Page
from loguru import logger

TILE_SIZE = 32

_INTERCEPTOR_JS = r"""
(function() {
    if (window.__wsIntercepted) return;
    window.__wsIntercepted = true;

    window._gsState = { x: null, y: null, zone: null, recvMsgs: [] };

    const _Orig = window.WebSocket;
    function PatchedWS(...args) {
        const ws = new _Orig(...args);

        // Capture SEND → extract x,y
        const _origSend = ws.send.bind(ws);
        ws.send = function(data) {
            try {
                const bytes = data instanceof ArrayBuffer ? new Uint8Array(data) : data;
                if (bytes instanceof Uint8Array) _parsePos(bytes);
            } catch(e) {}
            return _origSend(data);
        };

        // Capture RECV → extract zone name + coords
        ws.addEventListener('message', function(evt) {
            try {
                let bytes;
                if (evt.data instanceof ArrayBuffer) bytes = new Uint8Array(evt.data);
                else if (evt.data instanceof Blob) {
                    evt.data.arrayBuffer().then(ab => {
                        const b = new Uint8Array(ab);
                        _parseRecv(b);
                    });
                    return;
                }
                if (bytes) {
                    _parseRecv(bytes, 0);
                    const hex = Array.from(bytes).map(b=>b.toString(16).padStart(2,'0')).join('');
                    window._gsState.recvMsgs.push(hex);
                    if (window._gsState.recvMsgs.length > 20) window._gsState.recvMsgs.shift();
                }
            } catch(e) {}
        });
        return ws;
    }
    PatchedWS.prototype = _Orig.prototype;
    ['CONNECTING','OPEN','CLOSING','CLOSED'].forEach(k => PatchedWS[k] = _Orig[k]);
    window.WebSocket = PatchedWS;

    function _parsePos(bytes) {
        let x = null, y = null;
        for (let i = 0; i < bytes.length - 4; i++) {
            if (bytes[i] === 0x15 && x === null) {
                const v = new DataView(bytes.buffer, bytes.byteOffset + i + 1, 4).getFloat32(0, true);
                if (v > 100 && v < 100000) x = v;
            }
            if (bytes[i] === 0x1d && y === null) {
                const v = new DataView(bytes.buffer, bytes.byteOffset + i + 1, 4).getFloat32(0, true);
                if (v > 100 && v < 100000) y = v;
            }
            if (x && y) break;
        }
        if (x && y) { window._gsState.x = x; window._gsState.y = y; }
    }

    function _tryZoneString(bytes) {
        // Zone names are pure ASCII lowercase with hyphens, e.g. "town-fairy", "route-1"
        let s = '';
        for (const b of bytes) {
            if (b >= 32 && b < 127) s += String.fromCharCode(b);
            else return;
        }
        if (s.length >= 4 && s.length <= 30 && /^[a-z][a-z0-9_-]+$/.test(s)) {
            if (s !== 'zestdapoet' && !s.startsWith('v175') && !s.startsWith('http')) {
                window._gsState.zone = s;
            }
        }
    }

    function _parseRecv(bytes, depth) {
        if (depth > 4) return;
        let i = 0;
        while (i < bytes.length - 1) {
            const tag = bytes[i];
            const wireType = tag & 0x07;
            i++;
            if (wireType === 2) {
                let len = 0, shift = 0;
                while (i < bytes.length) {
                    const b = bytes[i++];
                    len |= (b & 0x7f) << shift;
                    shift += 7;
                    if (!(b & 0x80)) break;
                }
                if (len >= 1 && i + len <= bytes.length) {
                    const slice = bytes.slice(i, i + len);
                    _tryZoneString(slice);          // try as zone string
                    _parseRecv(slice, depth + 1);   // recurse into nested protobuf
                }
                i += len;
            } else if (wireType === 0) {
                while (i < bytes.length && (bytes[i++] & 0x80));
            } else if (wireType === 5) { i += 4; }
            else if (wireType === 1) { i += 8; }
            else { i++; }
        }
    }
})();
"""


async def inject_interceptor(page: Page) -> None:
    await page.add_init_script(_INTERCEPTOR_JS)


async def get_state(page: Page) -> dict:
    """Đọc toàn bộ game state: x, y, zone, tile."""
    try:
        s = await page.evaluate("({x: window._gsState?.x, y: window._gsState?.y, zone: window._gsState?.zone})")
        x, y, zone = s.get("x"), s.get("y"), s.get("zone")
        tile = (int(x // TILE_SIZE), int(y // TILE_SIZE)) if x and y else None
        return {"x": x, "y": y, "zone": zone, "tile": tile}
    except Exception:
        return {"x": None, "y": None, "zone": None, "tile": None}


def is_town(zone: str | None) -> bool:
    if not zone:
        return True  # Unknown → assume town (safe default)
    return zone.startswith("town")


async def focus_map(page: Page) -> None:
    try:
        await page.locator("canvas").first.click(timeout=3000)
    except Exception:
        pass


async def hold_key(page: Page, key: str, duration: float = 0.5) -> None:
    """Hold phím xuống trong duration giây — di chuyển mượt hơn press/release."""
    await page.keyboard.down(key)
    await asyncio.sleep(duration)
    await page.keyboard.up(key)
    await asyncio.sleep(0.08)


async def probe_directions(page: Page) -> dict[str, bool]:
    """
    Test 4 hướng: press key 0.5s, check tile moved, rồi quay lại.
    Trả về {"ArrowDown": True/False, ...}
    """
    opposite = {
        "ArrowDown": "ArrowUp", "ArrowUp": "ArrowDown",
        "ArrowRight": "ArrowLeft", "ArrowLeft": "ArrowRight",
    }
    results = {}
    before = await get_state(page)
    base_tile = before["tile"]

    for key in ["ArrowDown", "ArrowRight", "ArrowUp", "ArrowLeft"]:
        await page.keyboard.down(key)
        await asyncio.sleep(0.5)
        await page.keyboard.up(key)
        await asyncio.sleep(0.2)
        after = await get_state(page)
        moved = after["tile"] != base_tile
        results[key] = moved
        # Quay lại vị trí gốc
        await page.keyboard.down(opposite[key])
        await asyncio.sleep(0.5)
        await page.keyboard.up(opposite[key])
        await asyncio.sleep(0.2)

    return results


async def exit_town(page: Page, max_steps: int = 60) -> bool:
    """
    Thoát town bằng cách probe hướng open, ưu tiên hướng đi xa nhất.
    LLM-friendly: mỗi bước đều probe + chọn hướng tốt nhất.
    """
    state = await get_state(page)
    logger.info(f"Bắt đầu exit town — zone: {state['zone']} tile={state['tile']}")

    no_progress_count = 0
    prev_tile = state["tile"]

    # Thứ tự ưu tiên hướng — thay đổi nếu bị kẹt
    preferred = ["ArrowRight", "ArrowDown", "ArrowLeft", "ArrowUp"]
    pref_idx = 0

    for step in range(max_steps):
        # Probe directions
        open_dirs = await probe_directions(page)
        open_list = [k for k, v in open_dirs.items() if v]

        state = await get_state(page)
        zone = state["zone"]
        tile = state["tile"]

        logger.debug(f"  [{step+1}] tile={tile} zone={zone} open={[d.replace('Arrow','') for d in open_list]}")

        if zone and not is_town(zone):
            logger.success(f"Thoát town! Zone: {zone} tile={tile}")
            return True

        if not open_list:
            logger.warning("Bị kẹt hoàn toàn!")
            break

        # Chọn hướng: ưu tiên theo preferred, nếu bị block thì xoay
        chosen = None
        for i in range(4):
            candidate = preferred[(pref_idx + i) % 4]
            if candidate in open_list:
                chosen = candidate
                break

        if not chosen:
            chosen = open_list[0]

        # Di chuyển
        await hold_key(page, chosen, duration=1.0)

        # Đánh giá tiến độ
        new_state = await get_state(page)
        if new_state["tile"] == prev_tile:
            no_progress_count += 1
            if no_progress_count >= 3:
                pref_idx += 1
                no_progress_count = 0
                logger.debug(f"    Không tiến → đổi ưu tiên sang {preferred[pref_idx % 4]}")
        else:
            no_progress_count = 0

        prev_tile = new_state["tile"]

    logger.warning("Không thoát được town — grind tại chỗ")
    return False


async def walk_for_encounter(page: Page, steps: int = 8) -> None:
    """Zigzag trên grass để trigger encounter. Dùng hold_key cho smooth movement."""
    pattern = (
        ["ArrowRight"] * 3 + ["ArrowDown"] * 2
        + ["ArrowLeft"] * 3 + ["ArrowDown"] * 2
        + ["ArrowRight"] * 3 + ["ArrowUp"]  * 2
        + ["ArrowLeft"] * 3 + ["ArrowUp"]   * 2
    )
    for key in pattern[:steps * 2]:
        await hold_key(page, key, duration=0.3)
