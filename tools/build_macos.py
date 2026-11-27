"""在原生 macOS 將 uv 管理的 Python 與鎖定依賴封裝成 .app 和 .dmg。"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import platform
import plistlib
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import tomllib
import urllib.error
import urllib.request
from pathlib import Path

from build_bundle import PYTHON_VERSION_PIN

ROOT = Path(__file__).resolve().parent.parent
APP_NAME = "Shioaji Wizard"
APP_EXECUTABLE = "ShioajiWizard"
ARCHES = {"arm64": "arm64", "x86_64": "x64"}


def run(*args: str | Path, env: dict[str, str] | None = None, cwd: Path = ROOT) -> str:
    result = subprocess.run(
        [str(arg) for arg in args], check=True, cwd=cwd, env=env, stdout=subprocess.PIPE, text=True
    )
    return result.stdout.strip()


def stage_python(runtime: Path, temp: Path) -> None:
    run("uv", "python", "install", PYTHON_VERSION_PIN, "--no-bin")
    python = run("uv", "python", "find", "--managed-python", "--system", PYTHON_VERSION_PIN)
    prefix = Path(run(python, "-c", "import sys; print(sys.base_prefix)")).resolve()
    venv = temp / "vendored"
    env = {**os.environ, "UV_PROJECT_ENVIRONMENT": str(venv), "UV_LINK_MODE": "copy"}
    run("uv", "sync", "--locked", "--no-dev", "--no-editable", "--python", python, env=env)
    libraries = Path(run(venv / "bin/python", "-c", "import sysconfig; print(sysconfig.get_path('purelib'))"))
    base_libraries = Path(run(python, "-c", "import sysconfig; print(sysconfig.get_path('purelib'))"))
    ignored = shutil.ignore_patterns("__pycache__", "*.pyc", "*.[eE][nN][vV]", ".env.*", "*.pfx", "*.p12")
    shutil.copytree(prefix, runtime, symlinks=True, ignore=ignored)
    shutil.copytree(
        libraries, runtime / base_libraries.relative_to(prefix), dirs_exist_ok=True, ignore=ignored
    )


def stage_app(app: Path, version: str, temp: Path) -> None:
    contents = app / "Contents"
    resources = contents / "Resources"
    launcher = contents / "MacOS" / APP_EXECUTABLE
    launcher.parent.mkdir(parents=True)
    resources.mkdir()
    run(
        "/usr/bin/xcrun",
        "swiftc",
        "-O",
        "-target",
        f"{platform.machine()}-apple-macosx13.0",
        ROOT / "tools/macos_launcher.swift",
        "-o",
        launcher,
    )
    info = {
        "CFBundleDisplayName": "Shioaji 測試精靈",
        "CFBundleExecutable": APP_EXECUTABLE,
        "CFBundleIdentifier": "tw.github.benjamin-teng.shioaji-wizard",
        "CFBundleInfoDictionaryVersion": "6.0",
        "CFBundleName": APP_NAME,
        "CFBundlePackageType": "APPL",
        "CFBundleShortVersionString": version,
        "CFBundleVersion": version,
        "LSMinimumSystemVersion": "13.0",
        "LSUIElement": True,
    }
    (contents / "Info.plist").write_bytes(plistlib.dumps(info))
    runtime = resources / "python"
    stage_python(runtime, temp)
    # 逐個簽署 Mach-O 後再封裝 .app，避免 --deep 批次簽署漏掉放在 Resources 的
    # 內嵌 Python 擴充模組。這是 ad-hoc 簽署，發布時仍須說明 Gatekeeper。
    executable = (runtime / "bin/python3.12").resolve(strict=True)
    libraries = sorted(
        file for file in runtime.rglob("*") if not file.is_symlink() and file.suffix in (".so", ".dylib")
    )
    for path in [executable, *libraries]:
        run("/usr/bin/codesign", "--force", "--sign", "-", path)
    run("/usr/bin/codesign", "--force", "--sign", "-", app)


def verify_app(app: Path, version: str, temp: Path) -> None:
    run("/usr/bin/codesign", "--verify", "--strict", app)
    # 從 DMG 複製到另一個絕對位置再啟動，檢查沒有依賴建置環境的 Python 或 repo。
    installed = temp / "Installed applications" / app.name
    installed.parent.mkdir()
    run("/usr/bin/ditto", app, installed)
    for pattern in ("*.[eE][nN][vV]", ".env.*", "*.pfx", "*.p12"):
        if next(installed.rglob(pattern), None) is not None:
            raise RuntimeError("安裝檔混入使用者設定")

    data = temp / "User files"
    data.mkdir()
    env = {
        **{name: value for name, value in os.environ.items() if name not in ("PYTHONHOME", "PYTHONPATH")},
        "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONNOUSERSITE": "1",
        "SJ_EKEY_BASE": str(temp / "fixture-ekey"),
    }
    python = installed / "Contents/Resources/python/bin/python3.12"
    sdk = run(
        python,
        "-c",
        "import shioaji._core; from importlib.metadata import version; print(version('shioaji'))",
        cwd=data,
        env=env,
    )
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = int(sock.getsockname()[1])
    log = temp / "launcher.log"
    with (
        log.open("wb") as output,
        subprocess.Popen(
            [
                str(installed / "Contents/MacOS" / APP_EXECUTABLE),
                "--port",
                str(port),
                "--root",
                str(data),
                "--idle-timeout",
                "20",
            ],
            env=env,
            cwd=data,
            stdout=output,
            stderr=subprocess.STDOUT,
        ) as proc,
    ):
        try:
            for _ in range(100):
                if proc.poll() is not None:
                    break
                try:
                    with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/state", timeout=1) as response:
                        state = json.load(response)
                    with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=1) as response:
                        page = response.read().decode("utf-8")
                except (OSError, urllib.error.URLError):
                    time.sleep(0.2)
                    continue
                browser_args = run("/bin/ps", "-ax", "-o", "command=", cwd=data)
                if (
                    state["platform"] == "darwin"
                    and state["version"] == version
                    and "設定金鑰" in page
                    and f"--app=http://127.0.0.1:{port}/" in browser_args
                ):
                    verify_picker(python, data, env)
                    print(
                        f"macOS verify PASS：{platform.machine()}、DMG、桌面視窗啟動、原生選檔、Shioaji {sdk}"
                    )
                    return
                time.sleep(0.2)
            raise RuntimeError(
                "內嵌 App 啟動驗證失敗：\n" + log.read_text(encoding="utf-8", errors="replace")
            )
        finally:
            if proc.poll() is None:
                proc.terminate()
                with contextlib.suppress(subprocess.TimeoutExpired):
                    proc.wait(timeout=5)
                if proc.poll() is None:
                    proc.kill()


def verify_picker(python: Path, data: Path, env: dict[str, str]) -> None:
    """實際進入原生選檔對話框。CI 結束對話框的行程，避免等候人工選檔。"""
    command = [
        str(python),
        "-c",
        "from pathlib import Path; from shioaji_wizard.server import _show_native_picker; "
        "_show_native_picker(Path.cwd(), 'settings')",
    ]
    with subprocess.Popen(
        command, env=env, cwd=data, start_new_session=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE
    ) as picker:
        try:
            _, stderr = picker.communicate(timeout=4)
        except subprocess.TimeoutExpired:
            # JXA 的 runModal 阻塞表示已進入對話框；一併收掉 Python 和 osascript。
            stop_group = os.killpg  # ty: ignore[unresolved-attribute] — 僅在 Darwin 驗收。
            with contextlib.suppress(ProcessLookupError):
                stop_group(picker.pid, signal.SIGTERM)
            try:
                picker.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                with contextlib.suppress(ProcessLookupError):
                    stop_group(picker.pid, signal.SIGKILL)  # ty: ignore[unresolved-attribute]
                picker.communicate()
        else:
            raise RuntimeError(f"macOS 選檔未停留在選擇視窗：{stderr.decode(errors='replace')}")


def build(dist: Path, *, verify: bool) -> Path:
    if sys.platform != "darwin" or platform.machine() not in ARCHES:
        raise RuntimeError("請在對應架構的 macOS 執行打包")
    version = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
    name = f"shioaji-wizard-macos-{ARCHES[platform.machine()]}.dmg"
    dist.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="shioaji-macos-", dir=dist) as temporary:
        temp = Path(temporary).resolve()
        image_dir = temp / "image"
        app = image_dir / f"{APP_NAME}.app"
        stage_app(app, version, temp)
        (image_dir / "Applications").symlink_to("/Applications", target_is_directory=True)
        image = temp / name
        run(
            "/usr/bin/hdiutil",
            "create",
            "-fs",
            "HFS+",
            "-format",
            "UDZO",
            "-volname",
            APP_NAME,
            "-srcfolder",
            image_dir,
            image,
        )
        if verify:
            mount = temp / "mounted"
            mount.mkdir()
            run("/usr/bin/hdiutil", "verify", image)
            run("/usr/bin/hdiutil", "attach", "-readonly", "-nobrowse", "-mountpoint", mount, image)
            try:
                verify_app(mount / app.name, version, temp)
            finally:
                run("/usr/bin/hdiutil", "detach", mount)
        return image.replace(dist / name)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dist-dir", type=Path, default=ROOT / "dist")
    parser.add_argument("--verify", action="store_true")
    args = parser.parse_args()
    print(f"macOS 安裝檔：{build(args.dist_dir.resolve(), verify=args.verify)}")


if __name__ == "__main__":
    main()
