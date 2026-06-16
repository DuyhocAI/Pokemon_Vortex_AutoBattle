from playwright.async_api import Page
from loguru import logger
from config import config
from agent.utils import random_delay, element_exists

_CONSENT_SELECTORS = [
    "button#ez-accept-all",
    "button[data-ez-action='accept']",
    ".ez-cmp-accept-all",
    "button.ez-accept-all",
    "#gdpr-cookie-accept",
    ".cookie-consent-accept",
    "button[data-accept='all']",
    "#cookie-notice-accept",
    ".cn-accept-cookie",
]


async def _dismiss_consent(page: Page) -> None:
    for sel in _CONSENT_SELECTORS:
        try:
            btn = page.locator(sel).first
            if await btn.is_visible(timeout=2000):
                await btn.click()
                await random_delay(0.8, 1.5)
                logger.debug(f"Đã đóng cookie consent: {sel}")
                return
        except Exception:
            continue


async def login(page: Page) -> bool:
    logger.info("Đang đăng nhập...")
    await page.goto(config.LOGIN_URL, wait_until="domcontentloaded")
    await random_delay(2.0, 3.0)

    await _dismiss_consent(page)
    await random_delay(0.5, 1.0)

    # Form login thực sự dùng name="myusername" / name="mypassword"
    try:
        await page.locator('#myusername').fill(config.USERNAME, timeout=8000)
        logger.debug("Đã nhập username")
    except Exception as e:
        logger.error(f"Không fill được username: {e}")
        return False

    await random_delay(0.4, 0.8)

    try:
        await page.locator('#mypassword').fill(config.PASSWORD, timeout=8000)
        logger.debug("Đã nhập password")
    except Exception as e:
        logger.error(f"Không fill được password: {e}")
        return False

    await random_delay(0.4, 0.8)

    try:
        await page.locator('input#submit').click(timeout=5000)
    except Exception:
        await page.keyboard.press("Enter")

    await page.wait_for_load_state("networkidle", timeout=20000)

    current_url = page.url
    logger.debug(f"URL sau login: {current_url}")

    # Thành công nếu không còn ở trang login
    if "login" not in current_url:
        logger.success(f"Đăng nhập thành công: {config.USERNAME}")
        return True

    # Vẫn ở trang login — kiểm tra thông báo lỗi
    error_el = await page.query_selector(".alert-danger, .error-message, #error, .login-error")
    if error_el:
        logger.error(f"Lỗi đăng nhập: {(await error_el.inner_text()).strip()}")
    else:
        logger.error("Đăng nhập thất bại — không rõ nguyên nhân")
    return False
