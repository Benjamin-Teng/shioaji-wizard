from ctypes import POINTER, cast, wintypes
from types import SimpleNamespace

import pytest

from shioaji_wizard import shell


def test_startup_fit_targets_app_window_instead_of_taskbar_proxy(monkeypatch):
    proxy, app, pid = 31, 32, 42

    def enum_windows(callback, lparam):
        for hwnd in (proxy, app):
            if not callback(hwnd, lparam):
                break

    def get_pid(_hwnd, owner):
        cast(owner, POINTER(wintypes.DWORD)).contents.value = pid

    def get_class(_hwnd, buffer, _length):
        buffer.value = "Chrome_WidgetWin_1"

    def get_bounds(hwnd, bounds):
        rect = cast(bounds, POINTER(wintypes.RECT)).contents
        rect.right, rect.bottom = (158, 26) if hwnd.value == proxy else (1248, 680)
        return True

    win32 = SimpleNamespace(
        EnumWindows=enum_windows,
        GetWindowThreadProcessId=get_pid,
        GetClassNameW=get_class,
        GetWindowRect=get_bounds,
        IsWindowVisible=lambda _: True,
    )
    monkeypatch.setattr(shell, "_user32", lambda: win32)
    monkeypatch.setattr(shell, "_WNDENUMPROC", lambda callback: callback)
    fitted, branded = [], []
    monkeypatch.setattr(shell, "_fit_window_to_work_area", fitted.append)

    assert shell.brand_window(pid, fit_to_screen=True, set_props=lambda hwnd, _: branded.append(hwnd))
    assert fitted == branded == [app]


@pytest.mark.parametrize("has_dpi_support", [False, True])
def test_branding_still_runs_if_startup_fit_is_unavailable(monkeypatch, has_dpi_support):
    def fail_fit(_hwnd):
        raise OSError("window no longer accepts positioning")

    monkeypatch.setattr(shell, "_user32", SimpleNamespace)
    if has_dpi_support:
        monkeypatch.setattr(shell, "_fit_window_to_work_area", fail_fit)
    branded = []

    assert shell.brand_window(
        42, fit_to_screen=True, find_hwnd=lambda *_, **__: 32, set_props=lambda hwnd, _: branded.append(hwnd)
    )
    assert branded == [32]
