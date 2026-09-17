#!/usr/bin/env python3
"""Priority-based browser automation orchestrator.

Engines: cloak > browser-act > kimi > playwright by default.

This script intentionally keeps setup lazy: it checks or installs dependencies only for
an engine that is actually selected.

Embedded copy for the browser_agent app (integration mode 4 of the source
skill's integration guide). Provenance & deviations:

- Source: skill_kb/skill_knowledge/zh-skills/browser-automation-toolbox
  (v2.0.11, MIT) scripts/browser_orchestrator.py — copied verbatim EXCEPT:
- Deviation 1 (attribution header, this block).
- Deviation 2: PLATFORM_PRIORITY extended with the AI-platform rows the
  source SKILL.md documents in its platform table (cloak > playwright >
  browser-act > kimi — cloud browser pools lose local login cookies on
  strictly-gated AI platforms) but the source script never encoded. B站 /
  抖音 / 微博 stay on DEFAULT_ORDER per the same table, so no rows needed.
Upstream updates: re-copy from the skill and re-apply these two deviations.
"""
from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import subprocess
import sys
import time
import traceback
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

DEFAULT_ORDER = ["cloak", "browser-act", "kimi", "playwright"]
BROWSER_ACT_SKILL_VERSION = "2.0.2"

# Platform-aware priority overrides: empirically higher success rates on certain platforms.
# Explicit --engine-order always takes highest precedence; then --platform override; then DEFAULT_ORDER.
PLATFORM_PRIORITY: Dict[str, List[str]] = {
    "xhs": ["browser-act", "cloak", "kimi", "playwright"],
    "xiaohongshu": ["browser-act", "cloak", "kimi", "playwright"],
    "小红书": ["browser-act", "cloak", "kimi", "playwright"],
    # App-side extension (source SKILL.md's platform table, see header):
    # AI platforms require login + strict bot detection — cloak's
    # anti-fingerprinting + persistent profile is the only stable path, and
    # cloud pools (browser-act) drop local login cookies.
    "ai": ["cloak", "playwright", "browser-act", "kimi"],
    "ai_platform": ["cloak", "playwright", "browser-act", "kimi"],
    "gemini": ["cloak", "playwright", "browser-act", "kimi"],
    "doubao": ["cloak", "playwright", "browser-act", "kimi"],
    "chatgpt": ["cloak", "playwright", "browser-act", "kimi"],
}


@dataclass
class EngineResult:
    ok: bool
    engine: str
    attempt: int
    url: Optional[str] = None
    current_url: Optional[str] = None
    outputs: Dict[str, Any] = field(default_factory=dict)
    artifacts: List[str] = field(default_factory=list)
    error: Optional[str] = None
    failure_kind: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ok": self.ok,
            "engine": self.engine,
            "attempt": self.attempt,
            "url": self.url,
            "current_url": self.current_url,
            "outputs": self.outputs,
            "artifacts": self.artifacts,
            "error": self.error,
            "failure_kind": self.failure_kind,
        }


def expand_path(value: Optional[str], base_dir: Path) -> Optional[Path]:
    if not value:
        return None
    p = Path(os.path.expanduser(value))
    if not p.is_absolute():
        p = base_dir / p
    return p


def classify_error(error: BaseException | str) -> str:
    msg = str(error).lower()
    if "no module named" in msg or "not installed" in msg or "executable doesn't exist" in msg or "not found" in msg:
        return "dependency_missing"
    if "login" in msg or "signin" in msg or "passport" in msg:
        return "login_required"
    if "captcha" in msg or "verify" in msg or "验证" in msg:
        return "captcha_required"
    if "selector" in msg or "strict mode violation" in msg:
        return "selector_drift"
    if "timeout" in msg or "timed out" in msg or "net::" in msg or "dns" in msg:
        return "network_or_timeout"
    if "bridge" in msg or "extension" in msg or "kimi" in msg or "browser-act" in msg or "adapter" in msg:
        return "setup_required"
    return "unknown"


def pip_install(package: str, timeout: int = 180) -> None:
    subprocess.check_call([sys.executable, "-m", "pip", "install", package], timeout=timeout)


def load_plan(args: argparse.Namespace) -> Dict[str, Any]:
    if args.command == "smoke":
        return {
            "url": args.url,
            "headless": args.headless,
            "actions": [
                {"type": "wait", "seconds": 1},
                {"type": "evaluate", "name": "title", "script": "document.title"},
                {"type": "extract_text", "name": "body", "selector": "body"},
                {"type": "screenshot", "path": "smoke.png"},
            ],
        }
    with open(args.plan, "r", encoding="utf-8") as f:
        return json.load(f)


def run_action_chain(page: Any, plan: Dict[str, Any], output_dir: Path) -> tuple[Dict[str, Any], List[str]]:
    outputs: Dict[str, Any] = {}
    artifacts: List[str] = []
    for idx, action in enumerate(plan.get("actions", []), start=1):
        action_type = action.get("type")
        if action_type == "goto":
            page.goto(action["url"], timeout=action.get("timeout", 30000))
        elif action_type == "wait":
            time.sleep(float(action.get("seconds", 1)))
        elif action_type == "click":
            page.click(action["selector"], timeout=action.get("timeout", 10000))
        elif action_type == "fill":
            page.fill(action["selector"], action.get("text", ""), timeout=action.get("timeout", 10000))
        elif action_type == "press":
            target = action.get("selector")
            if target:
                page.press(target, action["key"], timeout=action.get("timeout", 10000))
            else:
                page.keyboard.press(action["key"])
        elif action_type == "scroll":
            x = int(action.get("x", 0))
            y = int(action.get("y", 1000))
            page.evaluate(f"window.scrollBy({x}, {y})")
        elif action_type == "evaluate":
            name = action.get("name", f"evaluate_{idx}")
            outputs[name] = page.evaluate(action["script"])
        elif action_type == "extract_text":
            name = action.get("name", f"text_{idx}")
            selector = action.get("selector", "body")
            outputs[name] = page.locator(selector).inner_text(timeout=action.get("timeout", 10000))
        elif action_type == "screenshot":
            shot_path = expand_path(action.get("path", f"screenshot_{idx}.png"), output_dir)
            assert shot_path is not None
            shot_path.parent.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(shot_path), full_page=bool(action.get("full_page", True)))
            artifacts.append(str(shot_path))
        else:
            raise ValueError(f"Unsupported action type: {action_type}")
    return outputs, artifacts


class BaseEngine:
    name = "base"

    def __init__(self, install_missing: bool = False) -> None:
        self.install_missing = install_missing

    def check(self) -> Dict[str, Any]:
        raise NotImplementedError

    def run_once(self, plan: Dict[str, Any], output_dir: Path, attempt: int) -> EngineResult:
        raise NotImplementedError


class CloakEngine(BaseEngine):
    name = "cloak"

    def check(self) -> Dict[str, Any]:
        try:
            import cloakbrowser  # noqa: F401
        except ImportError:
            if not self.install_missing:
                return {"available": False, "reason": "cloakbrowser package missing", "failure_kind": "dependency_missing"}
            pip_install("cloakbrowser")
        cloak_bin = Path.home() / ".cloakbrowser"
        has_binary = any(p.is_dir() and p.name.startswith("chromium-") for p in cloak_bin.iterdir()) if cloak_bin.exists() else False
        if not has_binary:
            if not self.install_missing:
                return {"available": False, "reason": "CloakBrowser Chromium binary missing", "failure_kind": "dependency_missing"}
            subprocess.check_call([sys.executable, "-m", "cloakbrowser", "install"], timeout=600)
        return {"available": True}

    def run_once(self, plan: Dict[str, Any], output_dir: Path, attempt: int) -> EngineResult:
        check = self.check()
        if not check.get("available"):
            return EngineResult(False, self.name, attempt, plan.get("url"), error=check.get("reason"), failure_kind=check.get("failure_kind"))
        try:
            from cloakbrowser import launch_persistent_context
            profile_dir = os.path.expanduser(plan.get("profile_dir", "~/.cloakbrowser-profile"))
            Path(profile_dir).mkdir(parents=True, exist_ok=True)
            context = launch_persistent_context(
                profile_dir,
                headless=bool(plan.get("headless", False)),
                viewport=plan.get("viewport", {"width": 1366, "height": 768}),
                humanize=bool(plan.get("humanize", True)),
            )
            try:
                page = context.new_page()
                if plan.get("url"):
                    page.goto(plan["url"], timeout=plan.get("timeout", 30000))
                outputs, artifacts = run_action_chain(page, plan, output_dir)
                return EngineResult(True, self.name, attempt, plan.get("url"), getattr(page, "url", None), outputs, artifacts)
            finally:
                context.close()
        except Exception as exc:
            artifacts = self._screenshot_on_error(locals().get("page"), output_dir, attempt)
            return EngineResult(False, self.name, attempt, plan.get("url"), getattr(locals().get("page"), "url", None), artifacts=artifacts, error=f"{exc}\n{traceback.format_exc(limit=3)}", failure_kind=classify_error(exc))

    def _screenshot_on_error(self, page: Any, output_dir: Path, attempt: int) -> List[str]:
        if page is None:
            return []
        path = output_dir / f"{self.name}_attempt{attempt}_error.png"
        try:
            page.screenshot(path=str(path), full_page=True)
            return [str(path)]
        except Exception:
            return []


class BrowserActEngine(BaseEngine):
    name = "browser-act"

    def _exe(self) -> Optional[str]:
        configured = os.environ.get("BROWSER_ACT_BIN")
        if configured:
            return configured
        return shutil.which("browser-act")

    def _install(self) -> None:
        uv = shutil.which("uv")
        if not uv:
            raise RuntimeError("uv is required to install browser-act-cli lazily. Install uv first or set BROWSER_ACT_BIN.")
        subprocess.check_call([uv, "tool", "install", "browser-act-cli", "--python", "3.12"], timeout=600)

    def check(self) -> Dict[str, Any]:
        exe = self._exe()
        if not exe and self.install_missing:
            try:
                self._install()
            except Exception as exc:
                return {
                    "available": False,
                    "reason": str(exc),
                    "failure_kind": "dependency_missing",
                    "lazy_install": "uv tool install browser-act-cli --python 3.12",
                }
            exe = self._exe()
        if not exe:
            return {
                "available": False,
                "reason": "browser-act CLI missing. Default behavior is not to install it; rerun with --install-missing or install with: uv tool install browser-act-cli --python 3.12",
                "failure_kind": "dependency_missing",
                "lazy_install": "uv tool install browser-act-cli --python 3.12",
            }
        return {"available": True, "executable": exe}

    def _load_core_guide(self, exe: str, output_dir: Path, attempt: int) -> str:
        path = output_dir / f"browser_act_core_attempt{attempt}.txt"
        proc = subprocess.run(
            [exe, "get-skills", "core", "--skill-version", BROWSER_ACT_SKILL_VERSION],
            text=True,
            capture_output=True,
            timeout=180,
            shell=False,
        )
        path.write_text(
            "STDOUT:\n" + proc.stdout + "\nSTDERR:\n" + proc.stderr,
            encoding="utf-8",
        )
        if proc.returncode != 0:
            raise RuntimeError(f"browser-act get-skills core failed; see {path}")
        return str(path)

    def _resolve_command(self, plan: Dict[str, Any], exe: str) -> Optional[List[str]]:
        raw = plan.get("browser_act_command") or plan.get("browser_act_args") or os.environ.get("BROWSER_ACT_RUN_COMMAND")
        if not raw:
            return None
        if isinstance(raw, list):
            args = [str(x) for x in raw]
        else:
            args = shlex.split(str(raw), posix=(os.name != "nt"))
        if args and Path(args[0]).name.lower().startswith("browser-act"):
            return args
        return [exe, *args]

    def run_once(self, plan: Dict[str, Any], output_dir: Path, attempt: int) -> EngineResult:
        check = self.check()
        if not check.get("available"):
            return EngineResult(False, self.name, attempt, plan.get("url"), error=check.get("reason"), failure_kind=check.get("failure_kind"))
        exe = check["executable"]
        artifacts: List[str] = []
        try:
            core_path = self._load_core_guide(exe, output_dir, attempt)
            artifacts.append(core_path)
            cmd = self._resolve_command(plan, exe)
            if not cmd:
                return EngineResult(
                    False,
                    self.name,
                    attempt,
                    plan.get("url"),
                    artifacts=artifacts,
                    error=(
                        "browser-act is installed and its core guide was captured, but no execution adapter was provided. "
                        "Set plan.browser_act_command / plan.browser_act_args or BROWSER_ACT_RUN_COMMAND. "
                        "Do not guess browser-act subcommands; inspect the captured core guide first."
                    ),
                    failure_kind="setup_required",
                )
            payload = dict(plan)
            payload["output_dir"] = str(output_dir)
            proc = subprocess.run(
                cmd,
                input=json.dumps(payload, ensure_ascii=False),
                text=True,
                capture_output=True,
                timeout=int(plan.get("browser_act_timeout", 240)),
                shell=False,
            )
            out_path = output_dir / f"browser_act_attempt{attempt}_run.txt"
            out_path.write_text("COMMAND:\n" + json.dumps(cmd, ensure_ascii=False) + "\nSTDOUT:\n" + proc.stdout + "\nSTDERR:\n" + proc.stderr, encoding="utf-8")
            artifacts.append(str(out_path))
            if proc.returncode != 0:
                raise RuntimeError(proc.stderr.strip() or proc.stdout.strip() or f"browser-act command failed with code {proc.returncode}")
            data: Dict[str, Any]
            try:
                data = json.loads(proc.stdout)
            except json.JSONDecodeError:
                data = {"ok": True, "outputs": {"stdout": proc.stdout}, "artifacts": artifacts}
            return EngineResult(
                ok=bool(data.get("ok", True)),
                engine=self.name,
                attempt=attempt,
                url=plan.get("url"),
                current_url=data.get("current_url"),
                outputs=data.get("outputs", {}),
                artifacts=list(dict.fromkeys([*artifacts, *data.get("artifacts", [])])),
                error=data.get("error"),
                failure_kind=data.get("failure_kind"),
            )
        except Exception as exc:
            return EngineResult(False, self.name, attempt, plan.get("url"), artifacts=artifacts, error=f"{exc}\n{traceback.format_exc(limit=3)}", failure_kind=classify_error(exc))


class PlaywrightEngine(BaseEngine):
    name = "playwright"

    def check(self) -> Dict[str, Any]:
        try:
            import playwright.sync_api  # noqa: F401
        except ImportError:
            if not self.install_missing:
                return {"available": False, "reason": "playwright package missing", "failure_kind": "dependency_missing"}
            pip_install("playwright")
            subprocess.check_call([sys.executable, "-m", "playwright", "install", "chromium"], timeout=600)
        return {"available": True}

    def run_once(self, plan: Dict[str, Any], output_dir: Path, attempt: int) -> EngineResult:
        check = self.check()
        if not check.get("available"):
            return EngineResult(False, self.name, attempt, plan.get("url"), error=check.get("reason"), failure_kind=check.get("failure_kind"))
        try:
            from playwright.sync_api import sync_playwright
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=bool(plan.get("headless", False)))
                context = browser.new_context(viewport=plan.get("viewport", {"width": 1366, "height": 768}))
                page = context.new_page()
                if plan.get("url"):
                    page.goto(plan["url"], timeout=plan.get("timeout", 30000))
                outputs, artifacts = run_action_chain(page, plan, output_dir)
                current_url = page.url
                browser.close()
                return EngineResult(True, self.name, attempt, plan.get("url"), current_url, outputs, artifacts)
        except Exception as exc:
            artifacts = self._screenshot_on_error(locals().get("page"), output_dir, attempt)
            return EngineResult(False, self.name, attempt, plan.get("url"), getattr(locals().get("page"), "url", None), artifacts=artifacts, error=f"{exc}\n{traceback.format_exc(limit=3)}", failure_kind=classify_error(exc))

    def _screenshot_on_error(self, page: Any, output_dir: Path, attempt: int) -> List[str]:
        if page is None:
            return []
        path = output_dir / f"{self.name}_attempt{attempt}_error.png"
        try:
            page.screenshot(path=str(path), full_page=True)
            return [str(path)]
        except Exception:
            return []


class KimiWebBridgeEngine(BaseEngine):
    name = "kimi"

    def check(self) -> Dict[str, Any]:
        if os.environ.get("KIMI_WEBBRIDGE_URL") or os.environ.get("KIMI_WEBBRIDGE_COMMAND"):
            return {"available": True}
        return {
            "available": False,
            "reason": "Kimi WebBridge not configured. Set KIMI_WEBBRIDGE_URL or KIMI_WEBBRIDGE_COMMAND when this fallback is needed.",
            "failure_kind": "setup_required",
        }

    def run_once(self, plan: Dict[str, Any], output_dir: Path, attempt: int) -> EngineResult:
        check = self.check()
        if not check.get("available"):
            return EngineResult(False, self.name, attempt, plan.get("url"), error=check.get("reason"), failure_kind=check.get("failure_kind"))
        payload = dict(plan)
        payload["output_dir"] = str(output_dir)
        try:
            if os.environ.get("KIMI_WEBBRIDGE_URL"):
                req = urllib.request.Request(
                    os.environ["KIMI_WEBBRIDGE_URL"],
                    data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(req, timeout=120) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
            else:
                cmd = shlex.split(os.environ["KIMI_WEBBRIDGE_COMMAND"], posix=(os.name != "nt"))
                proc = subprocess.run(
                    cmd,
                    input=json.dumps(payload, ensure_ascii=False),
                    text=True,
                    capture_output=True,
                    shell=False,
                    timeout=180,
                )
                if proc.returncode != 0:
                    raise RuntimeError(proc.stderr.strip() or proc.stdout.strip())
                data = json.loads(proc.stdout)
            return EngineResult(
                ok=bool(data.get("ok")),
                engine=self.name,
                attempt=attempt,
                url=plan.get("url"),
                current_url=data.get("current_url"),
                outputs=data.get("outputs", {}),
                artifacts=data.get("artifacts", []),
                error=data.get("error"),
                failure_kind=data.get("failure_kind"),
            )
        except Exception as exc:
            return EngineResult(False, self.name, attempt, plan.get("url"), error=f"{exc}\n{traceback.format_exc(limit=3)}", failure_kind=classify_error(exc))


ENGINE_REGISTRY = {
    "cloak": CloakEngine,
    "browser-act": BrowserActEngine,
    "browser_act": BrowserActEngine,
    "kimi": KimiWebBridgeEngine,
    "playwright": PlaywrightEngine,
}


def parse_order(value: str) -> List[str]:
    order = [x.strip() for x in value.split(",") if x.strip()]
    unknown = [x for x in order if x not in ENGINE_REGISTRY]
    if unknown:
        raise SystemExit(f"Unknown engines: {', '.join(unknown)}. Known: {', '.join(ENGINE_REGISTRY)}")
    return order


def run_with_fallback(plan: Dict[str, Any], output_dir: Path, order: List[str], max_attempts: int, install_missing: bool) -> Dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    attempts: List[Dict[str, Any]] = []
    for engine_name in order:
        engine = ENGINE_REGISTRY[engine_name](install_missing=install_missing)
        for attempt in range(1, max_attempts + 1):
            result = engine.run_once(plan, output_dir, attempt)
            result_dict = result.to_dict()
            attempts.append(result_dict)
            print(json.dumps(result_dict, ensure_ascii=False), flush=True)
            if result.ok:
                final = {"ok": True, "selected_engine": engine_name, "attempts": attempts, "result": result_dict}
                write_report(output_dir, final)
                return final
        print(f"[fallback] {engine_name} failed {max_attempts} time(s); switching engine.", file=sys.stderr)
    final = {"ok": False, "selected_engine": None, "attempts": attempts, "result": None}
    write_report(output_dir, final)
    return final


def write_report(output_dir: Path, report: Dict[str, Any]) -> None:
    path = output_dir / "browser_orchestrator_report.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)


def resolve_order(engine_order_str: str, platform: Optional[str]) -> List[str]:
    """Resolve final engine priority: explicit order > platform override > default."""
    if engine_order_str:
        return parse_order(engine_order_str)
    if platform and platform.lower() in PLATFORM_PRIORITY:
        resolved = PLATFORM_PRIORITY[platform.lower()]
        print(f"[platform] Detected platform '{platform}', using overridden order: {','.join(resolved)}", file=sys.stderr)
        return resolved
    return list(DEFAULT_ORDER)


def main() -> int:
    parser = argparse.ArgumentParser(description="Browser automation orchestrator: cloak > browser-act > kimi > playwright (with platform-aware overrides)")
    sub = parser.add_subparsers(dest="command", required=True)

    check = sub.add_parser("check", help="Check lazy availability of engines")
    check.add_argument("--engine-order", default="")
    check.add_argument("--platform", default=None, choices=["xhs", "xiaohongshu", "小红书"], help="Apply platform-specific engine priority override")
    check.add_argument("--install-missing", action="store_true")

    smoke = sub.add_parser("smoke", help="Run a built-in smoke action chain")
    smoke.add_argument("--url", required=True)
    smoke.add_argument("--output-dir", required=True)
    smoke.add_argument("--engine-order", default="")
    smoke.add_argument("--platform", default=None, help="Target platform name (e.g. xhs) to apply priority override")
    smoke.add_argument("--max-attempts-per-engine", type=int, default=2)
    smoke.add_argument("--headless", action="store_true")
    smoke.add_argument("--install-missing", action="store_true")

    run = sub.add_parser("run", help="Run a JSON action plan")
    run.add_argument("--plan", required=True)
    run.add_argument("--output-dir", required=True)
    run.add_argument("--engine-order", default="")
    run.add_argument("--platform", default=None, help="Target platform name (e.g. xhs) to apply priority override")
    run.add_argument("--max-attempts-per-engine", type=int, default=2)
    run.add_argument("--install-missing", action="store_true")

    args = parser.parse_args()
    order = resolve_order(args.engine_order, args.platform)

    if args.command == "check":
        rows = []
        for engine_name in order:
            rows.append({"engine": engine_name, **ENGINE_REGISTRY[engine_name](install_missing=args.install_missing).check()})
        print(json.dumps({"ok": True, "engines": rows, "resolved_order": order}, ensure_ascii=False, indent=2))
        return 0

    output_dir = Path(args.output_dir).resolve()
    plan = load_plan(args)
    final = run_with_fallback(plan, output_dir, order, args.max_attempts_per_engine, args.install_missing)
    return 0 if final["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
