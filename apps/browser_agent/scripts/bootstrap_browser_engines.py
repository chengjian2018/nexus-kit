#!/usr/bin/env python3
"""Check or install optional browser automation engine dependencies.

Use lazily. Do not run installation unless the selected engine needs it.
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path


def run(cmd: list[str], timeout: int) -> None:
    print("$ " + " ".join(cmd))
    subprocess.check_call(cmd, timeout=timeout)


def check_import(module: str) -> bool:
    try:
        __import__(module)
        return True
    except ImportError:
        return False


def ensure_cloak(install: bool) -> dict:
    result = {"engine": "cloak", "package": check_import("cloakbrowser"), "binary": False, "installed": False}
    if not result["package"] and install:
        run([sys.executable, "-m", "pip", "install", "cloakbrowser"], timeout=180)
        result["package"] = check_import("cloakbrowser")
        result["installed"] = True
    cloak_dir = Path.home() / ".cloakbrowser"
    result["binary"] = any(p.is_dir() and p.name.startswith("chromium-") for p in cloak_dir.iterdir()) if cloak_dir.exists() else False
    if result["package"] and not result["binary"] and install:
        run([sys.executable, "-m", "cloakbrowser", "install"], timeout=600)
        result["binary"] = any(p.is_dir() and p.name.startswith("chromium-") for p in cloak_dir.iterdir()) if cloak_dir.exists() else False
        result["installed"] = True
    result["available"] = bool(result["package"] and result["binary"])
    return result


def ensure_browser_act(install: bool) -> dict:
    exe = shutil.which("browser-act")
    result = {
        "engine": "browser-act",
        "available": bool(exe),
        "executable": exe,
        "installed": False,
        "lazy_install": "uv tool install browser-act-cli --python 3.12",
    }
    if result["available"] or not install:
        result["setup_deferred"] = not result["available"]
        return result
    uv = shutil.which("uv")
    if not uv:
        result["available"] = False
        result["error"] = "uv missing; install uv first or install browser-act-cli manually"
        return result
    run([uv, "tool", "install", "browser-act-cli", "--python", "3.12"], timeout=600)
    exe = shutil.which("browser-act")
    result["available"] = bool(exe)
    result["executable"] = exe
    result["installed"] = True
    result["setup_deferred"] = not result["available"]
    return result


def ensure_playwright(install: bool) -> dict:
    result = {"engine": "playwright", "package": check_import("playwright"), "browser": None, "installed": False}
    if not result["package"] and install:
        run([sys.executable, "-m", "pip", "install", "playwright"], timeout=180)
        result["package"] = check_import("playwright")
        result["installed"] = True
    if result["package"] and install:
        run([sys.executable, "-m", "playwright", "install", "chromium"], timeout=600)
        result["browser"] = "chromium install command completed"
        result["installed"] = True
    result["available"] = bool(result["package"])
    return result


def check_kimi() -> dict:
    import os

    configured = bool(os.environ.get("KIMI_WEBBRIDGE_URL") or os.environ.get("KIMI_WEBBRIDGE_COMMAND"))
    return {
        "engine": "kimi",
        "available": configured,
        "configured": configured,
        "setup_deferred": not configured,
        "hint": "Set KIMI_WEBBRIDGE_URL or KIMI_WEBBRIDGE_COMMAND only when Kimi fallback is needed.",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Check/install browser engine dependencies lazily")
    parser.add_argument("--engines", default="cloak,browser-act,kimi,playwright", help="Comma-separated: cloak,browser-act,kimi,playwright")
    parser.add_argument("--install", action="store_true", help="Install missing cloak/browser-act/playwright dependencies lazily")
    args = parser.parse_args()

    rows = []
    for engine in [x.strip() for x in args.engines.split(",") if x.strip()]:
        if engine == "cloak":
            rows.append(ensure_cloak(args.install))
        elif engine in {"browser-act", "browser_act"}:
            rows.append(ensure_browser_act(args.install))
        elif engine == "playwright":
            rows.append(ensure_playwright(args.install))
        elif engine == "kimi":
            rows.append(check_kimi())
        else:
            rows.append({"engine": engine, "available": False, "error": "unknown engine"})
    print(json.dumps({"ok": True, "results": rows}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
