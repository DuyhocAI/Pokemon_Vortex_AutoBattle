import os
from dotenv import load_dotenv

load_dotenv()


class Config:
    USERNAME = os.getenv("VORTEX_USERNAME", "")
    PASSWORD = os.getenv("VORTEX_PASSWORD", "")
    MODE = os.getenv("MODE", "battle")  # battle | catch | both
    MAP_URL = os.getenv("MAP_URL", "https://www.pokemon-vortex.com/map/1/")
    MAX_BATTLES = int(os.getenv("MAX_BATTLES", "0"))
    ACTION_DELAY_MIN = float(os.getenv("ACTION_DELAY_MIN", "1.0"))
    ACTION_DELAY_MAX = float(os.getenv("ACTION_DELAY_MAX", "3.0"))
    PREFERRED_MOVE = int(os.getenv("PREFERRED_MOVE", "1"))
    HEADLESS = os.getenv("HEADLESS", "false").lower() == "true"

    # Nghỉ ngơi cho GPU: sau mỗi N trận (random trong [MIN, MAX]) thì ngủ REST_HOURS giờ
    REST_ENABLED   = os.getenv("REST_ENABLED", "true").lower() == "true"
    REST_AFTER_MIN = int(os.getenv("REST_AFTER_MIN", "230"))
    REST_AFTER_MAX = int(os.getenv("REST_AFTER_MAX", "300"))
    REST_HOURS     = float(os.getenv("REST_HOURS", "2.0"))

    # Web dashboard
    WEB_UI   = os.getenv("WEB_UI", "true").lower() == "true"
    WEB_HOST = os.getenv("WEB_HOST", "127.0.0.1")
    WEB_PORT = int(os.getenv("WEB_PORT", "8770"))

    BASE_URL = "https://www.pokemon-vortex.com"
    LOGIN_URL = f"{BASE_URL}/login/"

    def validate(self):
        if not self.USERNAME or not self.PASSWORD:
            raise ValueError("Chưa điền VORTEX_USERNAME và VORTEX_PASSWORD trong file .env")
        if self.MODE not in ("battle", "catch", "both", "tower", "sidequest"):
            raise ValueError("MODE phải là: battle, catch, both, tower, hoặc sidequest")
        if self.PREFERRED_MOVE not in range(1, 5):
            raise ValueError("PREFERRED_MOVE phải từ 1 đến 4")


config = Config()
