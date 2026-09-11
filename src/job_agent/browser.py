"""Playwright 封装：Chromium 会话、每站点独立登录态（storage_state）、人工登录流程。"""

from __future__ import annotations

import random
import time
from pathlib import Path

from .models import data_dir


def auth_path(config: dict, config_path: Path, site_id: str) -> Path:
    return data_dir(config, config_path) / "auth" / f"{site_id}.json"


def human_delay(config: dict, lo: float | None = None, hi: float | None = None) -> None:
    rng = (config.get("browser") or {}).get("delay") or [2, 6]
    time.sleep(random.uniform(lo if lo is not None else rng[0], hi if hi is not None else rng[1]))


class BrowserSession:
    """context manager。

    with BrowserSession(config, config_path) as bs:
        page = bs.open("boss")     # 每站点独立 context，加载 data/auth/boss.json
        ...
        bs.save_state("boss")
    """

    def __init__(self, config: dict, config_path: Path, headless: bool | None = None):
        self.config = config
        self.config_path = config_path
        bcfg = config.get("browser") or {}
        self.headless = bcfg.get("headless", True) if headless is None else headless
        self._pw = None
        self._browser = None
        self._context = None
        self.page = None
        self._site_id: str | None = None

    def __enter__(self) -> "BrowserSession":
        from playwright.sync_api import sync_playwright

        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(headless=self.headless)
        return self

    def open(self, site_id: str):
        """为站点创建（或复用）带其登录态的 context 与 page。"""
        if self._site_id == site_id and self.page:
            return self.page
        if self._context:
            try:
                self._context.close()
            except Exception:
                pass
        kwargs: dict = {
            "user_agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                           "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"),
            "viewport": {"width": 1440, "height": 900},
            "locale": "zh-CN",
        }
        state = auth_path(self.config, self.config_path, site_id)
        if state.exists():
            kwargs["storage_state"] = str(state)
        self._context = self._browser.new_context(**kwargs)
        self.page = self._context.new_page()
        self._site_id = site_id
        return self.page

    def save_state(self, site_id: str) -> Path:
        path = auth_path(self.config, self.config_path, site_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        if self._context:
            self._context.storage_state(path=str(path))
        return path

    def has_auth(self, site_id: str) -> bool:
        return auth_path(self.config, self.config_path, site_id).exists()

    def __exit__(self, *exc) -> None:
        for closer in (self._context, self._browser):
            try:
                if closer:
                    closer.close()
            except Exception:
                pass
        try:
            if self._pw:
                self._pw.stop()
        except Exception:
            pass


def wait_for_login(site, page, timeout: float = 240.0, interval: float = 3.0) -> bool:
    """轮询站点登录态直到成功或超时；站点检测抛错（如滑块验证）视为未登录继续等。"""
    import time as _time

    deadline = _time.time() + timeout
    while _time.time() < deadline:
        try:
            if site.logged_in(page):
                return True
        except Exception:
            pass
        _time.sleep(interval)
    return False


def login_flow(site_id: str, config: dict, config_path: Path,
               timeout: float = 240.0) -> Path:
    """有头浏览器手动登录，自动检测成功后保存登录态（无需终端确认）。"""
    from .sites import get_site

    site = get_site(site_id, (config.get("home") or {}).get("city"))
    print(f"[login] 打开 {site.name} 登录页，请在浏览器中完成登录（扫码/账密），"
          f"登录成功会自动保存（最长等待 {timeout:.0f} 秒）...")
    with BrowserSession(config, config_path, headless=False) as bs:
        page = bs.open(site_id)
        page.goto(site.login_url, wait_until="domcontentloaded")
        ok = wait_for_login(site, page, timeout=timeout)
        path = bs.save_state(site_id)
        if ok:
            print(f"[login] ✅ {site.name} 登录成功，登录态已保存: {path}")
        else:
            print(f"[login] ⚠️ 等待超时未检测到登录（{timeout:.0f}s）。"
                  f"当前状态已存到 {path}，请重新执行 login 重试。")
        return path
