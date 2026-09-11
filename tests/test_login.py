"""wait_for_login 轮询逻辑测试（fake site/page，不起浏览器）。"""

from job_agent.browser import wait_for_login


class FlakySite:
    """第 N 次 logged_in 才返回 True；可选抛错模拟滑块验证。"""

    base_url = "http://mock.local"

    def __init__(self, succeed_on: int, raise_times: int = 0):
        self.calls = 0
        self.succeed_on = succeed_on
        self.raise_times = raise_times

    def logged_in(self, page) -> bool:
        self.calls += 1
        if self.calls <= self.raise_times:
            raise RuntimeError("安全验证")
        return self.calls >= self.succeed_on


class FakePage:
    def wait_for_timeout(self, ms):
        pass

    def goto(self, url, wait_until=None):
        pass

    @property
    def context(self):
        return self

    def new_page(self):
        return self


def test_wait_for_login_success():
    site = FlakySite(succeed_on=3)
    assert wait_for_login(site, FakePage(), timeout=5, interval=0.01)


def test_wait_for_login_timeout():
    site = FlakySite(succeed_on=10**9)
    assert not wait_for_login(site, FakePage(), timeout=0.2, interval=0.05)


def test_wait_for_login_survives_exceptions():
    site = FlakySite(succeed_on=4, raise_times=2)
    assert wait_for_login(site, FakePage(), timeout=5, interval=0.01)
    assert site.calls >= 4
