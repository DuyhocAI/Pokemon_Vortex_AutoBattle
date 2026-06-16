import asyncio
import random
from loguru import logger
import sys

logger.remove()
logger.add(
    sys.stdout,
    format="<green>{time:HH:mm:ss}</green> | <level>{level: <8}</level> | {message}",
    colorize=True,
)
logger.add(
    "logs/agent.log",
    rotation="10 MB",
    retention="7 days",
    format="{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {message}",
)


async def random_delay(min_s: float = 1.0, max_s: float = 3.0):
    delay = random.uniform(min_s, max_s)
    await asyncio.sleep(delay)


class RestScheduler:
    """Sau mỗi ~N trận (N random trong [after_min, after_max]) thì ngủ `hours`
    giờ để GPU được nghỉ. Ngưỡng random lại sau mỗi lần nghỉ nên không cố định 250.

    Dùng: gọi `await rest.maybe_rest(total_battles)` mỗi vòng lặp sau khi 1 trận
    kết thúc. Trả về True nếu vừa nghỉ xong.
    """

    def __init__(self, after_min: int = 230, after_max: int = 300,
                 hours: float = 2.0, enabled: bool = True, tag: str = "REST"):
        self.after_min = after_min
        self.after_max = max(after_max, after_min)
        self.hours     = hours
        self.enabled   = enabled
        self.tag       = tag
        self._last_count = 0          # số trận tại lần nghỉ gần nhất
        self._threshold  = self._roll()

    def _roll(self) -> int:
        return random.randint(self.after_min, self.after_max)

    async def maybe_rest(self, total_battles: int, on_log=None) -> bool:
        if not self.enabled:
            return False
        if total_battles - self._last_count < self._threshold:
            return False

        secs = self.hours * 3600 * random.uniform(0.95, 1.05)  # ±5% cho tự nhiên
        mins = secs / 60
        msg = (f"[{self.tag}] Đã đánh {self._threshold} trận (tổng {total_battles}) — "
               f"nghỉ {mins:.0f} phút (~{self.hours:.1f}h) cho GPU mát máy...")
        logger.info(msg)
        if on_log:
            try:
                on_log(f"😴 Nghỉ {mins:.0f} phút cho GPU mát máy (sau {self._threshold} trận)")
            except Exception:
                pass

        await asyncio.sleep(secs)

        self._last_count = total_battles
        self._threshold  = self._roll()   # random lại ngưỡng kế tiếp
        logger.info(f"[{self.tag}] Hết giờ nghỉ — chạy tiếp. Ngưỡng nghỉ kế: {self._threshold} trận.")
        if on_log:
            try:
                on_log(f"☀️ Hết giờ nghỉ — chạy tiếp (nghỉ kế sau ~{self._threshold} trận)")
            except Exception:
                pass
        return True


async def safe_click(page, selector: str, timeout: int = 5000) -> bool:
    try:
        await page.wait_for_selector(selector, timeout=timeout, state="visible")
        await page.click(selector)
        return True
    except Exception:
        return False


async def element_exists(page, selector: str, timeout: int = 3000) -> bool:
    try:
        await page.wait_for_selector(selector, timeout=timeout, state="visible")
        return True
    except Exception:
        return False
