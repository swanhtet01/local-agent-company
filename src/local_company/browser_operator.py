from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable
from urllib.parse import SplitResult, urlsplit, urlunsplit


RECEIPT_SCHEMA = "supermega.browser-proof.v1"
DOCTOR_SCHEMA = "supermega.browser-doctor.v1"
SUITE_SCHEMA = "supermega.browser-suite.v1"
PINNED_AGENT_BROWSER_VERSION = "0.33.2"
NAMESPACE = "supermega-local-workcell"
MAX_URL_LENGTH = 4096
MAX_PAGE_TEXT_BYTES = 512 * 1024
Runner = Callable[..., subprocess.CompletedProcess[str]]

SUPERMEGA_RELEASE_PAGES = (
    {"id": "shop", "product": "Shop"},
    {"id": "plant", "product": "Plant"},
    {"id": "website", "product": "Website"},
    {"id": "ecommerce", "product": "Ecommerce"},
)


class BrowserToolError(RuntimeError):
    pass


def _repository_root() -> Path:
    return Path(__file__).resolve().parents[2]


def discover_agent_browser(repository_root: Path | None = None) -> Path | None:
    explicit = os.getenv("LOCAL_COMPANY_AGENT_BROWSER")
    if explicit:
        path = Path(explicit).expanduser()
        return path.resolve() if path.is_file() else None

    root = (repository_root or _repository_root()).resolve()
    candidates = [
        root / ".local-company-tools" / "node_modules" / "agent-browser" / "bin"
        / "agent-browser-win32-x64.exe",
        root / ".local-company-tools" / "node_modules" / ".bin" / "agent-browser.cmd",
    ]
    bundled = next((path for path in candidates if path.is_file()), None)
    if bundled is not None:
        return bundled
    on_path = shutil.which("agent-browser")
    return Path(on_path).resolve() if on_path else None


def discover_browser_executable() -> Path | None:
    explicit = os.getenv("LOCAL_COMPANY_BROWSER_EXECUTABLE")
    if explicit:
        path = Path(explicit).expanduser()
        return path.resolve() if path.is_file() else None

    program_files = Path(os.getenv("ProgramFiles", ""))
    program_files_x86 = Path(os.getenv("ProgramFiles(x86)", ""))
    local_app_data = Path(os.getenv("LOCALAPPDATA", ""))
    candidates = [
        program_files_x86 / "Microsoft" / "Edge" / "Application" / "msedge.exe",
        program_files / "Microsoft" / "Edge" / "Application" / "msedge.exe",
        local_app_data / "Microsoft" / "Edge" / "Application" / "msedge.exe",
        program_files / "Google" / "Chrome" / "Application" / "chrome.exe",
        program_files_x86 / "Google" / "Chrome" / "Application" / "chrome.exe",
        local_app_data / "Google" / "Chrome" / "Application" / "chrome.exe",
    ]
    return next((path.resolve() for path in candidates if path.is_file()), None)


def _validate_target_url(value: str) -> str:
    target = value.strip()
    if not target or len(target) > MAX_URL_LENGTH:
        raise ValueError("browser_url_invalid")
    parsed = urlsplit(target)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise ValueError("browser_url_must_be_http_or_https")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("browser_url_credentials_forbidden")
    return target


def _safe_url(value: str) -> str:
    try:
        parsed = urlsplit(value)
    except ValueError:
        return "<invalid-url>"
    query = "<redacted>" if parsed.query else ""
    fragment = "<redacted>" if parsed.fragment else ""
    return urlunsplit(SplitResult(parsed.scheme, parsed.netloc, parsed.path, query, fragment))


def _safe_name(value: str) -> str:
    cleaned = re.sub(r"[^a-z0-9.-]+", "-", value.lower()).strip("-.")
    return cleaned[:60] or "site"


def _url_identity(value: str) -> tuple[str, str, int | None, str] | None:
    try:
        parsed = urlsplit(value)
        return (
            parsed.scheme.lower(), (parsed.hostname or "").lower(),
            parsed.port, parsed.path or "/",
        )
    except ValueError:
        return None


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _evidence(path: Path) -> dict[str, object]:
    return {
        "file": path.name,
        "bytes": path.stat().st_size,
        "sha256": _sha256(path),
    }


def _short_error(value: object, limit: int = 800) -> str:
    if isinstance(value, str):
        text = value
    else:
        text = json.dumps(value, sort_keys=True, default=str)
    return text.replace("\r", " ").replace("\n", " ")[:limit]


def _sanitize_url_fields(value: object) -> object:
    if isinstance(value, list):
        return [_sanitize_url_fields(item) for item in value]
    if not isinstance(value, dict):
        return value
    sanitized: dict[str, object] = {}
    for key, item in value.items():
        if key == "lifecycle":
            continue
        if key.lower().endswith("url") and item is not None:
            sanitized[key] = _safe_url(str(item))
        else:
            sanitized[key] = _sanitize_url_fields(item)
    return sanitized


class AgentBrowserClient:
    def __init__(
        self,
        tool: Path,
        browser: Path,
        session: str,
        *,
        timeout_seconds: int = 30,
        runner: Runner = subprocess.run,
    ) -> None:
        self.tool = tool
        self.browser = browser
        self.timeout_seconds = timeout_seconds
        self.runner = runner
        self.environment = os.environ.copy()
        for key in (
            "AGENT_BROWSER_AUTO_CONNECT", "AGENT_BROWSER_EXTENSIONS",
            "AGENT_BROWSER_HEADERS", "AGENT_BROWSER_PLUGINS", "AGENT_BROWSER_PROFILE",
            "AGENT_BROWSER_RESTORE", "AGENT_BROWSER_STATE", "AI_GATEWAY_API_KEY",
            "ANTHROPIC_API_KEY", "OPENAI_API_KEY",
        ):
            self.environment.pop(key, None)
        config = tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", prefix="supermega-browser-",
            suffix=".json", delete=False,
        )
        try:
            json.dump({"headed": False, "hideScrollbars": True}, config)
        finally:
            config.close()
        self.config_path = Path(config.name)
        self.environment.update({
            "AGENT_BROWSER_CONFIG": str(self.config_path),
            "AGENT_BROWSER_EXECUTABLE_PATH": str(browser),
            "AGENT_BROWSER_SESSION": session,
            "AGENT_BROWSER_NAMESPACE": NAMESPACE,
            "AGENT_BROWSER_IDLE_TIMEOUT_MS": "10000",
            "AGENT_BROWSER_DEFAULT_TIMEOUT": str(min(timeout_seconds * 1000, 10000)),
            "AGENT_BROWSER_MAX_OUTPUT": str(MAX_PAGE_TEXT_BYTES * 2),
            "AGENT_BROWSER_RESTORE_SAVE": "never",
        })

    def version(self) -> str:
        try:
            completed = self.runner(
                [str(self.tool), "--version"], capture_output=True, text=True,
                encoding="utf-8", errors="strict",
                timeout=10, check=False, env=self.environment,
            )
        except (OSError, subprocess.SubprocessError, UnicodeError) as error:
            raise BrowserToolError("agent_browser_version_failed") from error
        if completed.returncode != 0:
            raise BrowserToolError("agent_browser_version_failed")
        observed = completed.stdout.strip()
        if observed != f"agent-browser {PINNED_AGENT_BROWSER_VERSION}":
            raise BrowserToolError("agent_browser_version_mismatch")
        return observed

    def run_batch(self, commands: list[list[str]]) -> list[dict[str, object]]:
        try:
            completed = self.runner(
                [str(self.tool), "batch", "--bail", "--json"],
                input=json.dumps(commands),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="strict",
                timeout=self.timeout_seconds,
                check=False,
                env=self.environment,
            )
        except subprocess.TimeoutExpired as error:
            raise BrowserToolError("agent_browser_batch_timeout") from error
        except UnicodeError as error:
            raise BrowserToolError("agent_browser_batch_encoding_invalid") from error
        raw = completed.stdout.strip()
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as error:
            detail = _short_error(completed.stderr or raw or "no_output")
            raise BrowserToolError(f"agent_browser_batch_invalid_json:{detail}") from error
        if not isinstance(payload, list):
            detail = _short_error(payload)
            raise BrowserToolError(f"agent_browser_batch_result_invalid:{detail}")
        failed = next(
            (item for item in payload if not isinstance(item, dict) or not item.get("success")),
            None,
        )
        if completed.returncode != 0 or failed is not None:
            detail = _short_error({
                "command": failed.get("command") if isinstance(failed, dict) else None,
                "error": failed.get("error") if isinstance(failed, dict)
                else completed.stderr or failed,
            })
            raise BrowserToolError(f"agent_browser_batch_failed:{detail}")
        results: list[dict[str, object]] = []
        for index, item in enumerate(payload):
            result = item.get("result") if isinstance(item, dict) else None
            if not isinstance(result, dict):
                raise BrowserToolError(f"agent_browser_batch_data_invalid:{index}")
            results.append(result)
        if len(results) != len(commands):
            raise BrowserToolError("agent_browser_batch_count_mismatch")
        return results

    def close(self) -> None:
        try:
            self.runner(
                [str(self.tool), "--json", "close"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="strict",
                timeout=5,
                check=False,
                env=self.environment,
            )
        except (OSError, subprocess.SubprocessError, UnicodeError):
            pass

    def dispose(self) -> None:
        try:
            self.config_path.unlink(missing_ok=True)
        except OSError:
            pass


def browser_doctor(
    *,
    repository_root: Path | None = None,
    runner: Runner = subprocess.run,
) -> dict[str, object]:
    tool = discover_agent_browser(repository_root)
    browser = discover_browser_executable()
    result: dict[str, object] = {
        "schema": DOCTOR_SCHEMA,
        "status": "blocked",
        "agentBrowserVersionPin": PINNED_AGENT_BROWSER_VERSION,
        "agentBrowserPath": str(tool) if tool else None,
        "browserPath": str(browser) if browser else None,
        "liveLaunchPassed": False,
        "modelRequired": False,
        "paidApiRequired": False,
        "externalWritePerformed": False,
    }
    if tool is None:
        result["reason"] = "agent_browser_missing"
        result["nextAction"] = (
            "npm.cmd install --prefix .local-company-tools "
            f"agent-browser@{PINNED_AGENT_BROWSER_VERSION} "
            "--ignore-scripts --no-audit --no-fund"
        )
        return result
    if browser is None:
        result["reason"] = "compatible_browser_missing"
        result["nextAction"] = "Install Microsoft Edge or Google Chrome locally."
        return result

    client = AgentBrowserClient(
        tool, browser, f"doctor-{uuid.uuid4().hex[:10]}", timeout_seconds=20, runner=runner,
    )
    try:
        result["agentBrowserVersion"] = client.version()
        opened, observed, _closed = client.run_batch([
            ["open", "about:blank"], ["get", "url"], ["close"],
        ])
        result["liveLaunchPassed"] = (
            opened.get("url") == "about:blank" and observed.get("url") == "about:blank"
        )
        result["status"] = "ready" if result["liveLaunchPassed"] else "blocked"
        result["reason"] = "ready" if result["liveLaunchPassed"] else "live_launch_mismatch"
        result["nextAction"] = "none" if result["liveLaunchPassed"] else "repair_browser_runtime"
    except (BrowserToolError, OSError) as error:
        result["reason"] = "live_launch_failed"
        result["detail"] = _short_error(error)
        result["nextAction"] = "repair_browser_runtime"
    finally:
        if not result["liveLaunchPassed"]:
            client.close()
        client.dispose()
    return result


def _normalized_network_requests(data: dict[str, object]) -> list[dict[str, object]]:
    requests = data.get("requests")
    if not isinstance(requests, list):
        return []
    normalized: list[dict[str, object]] = []
    for item in requests[:1000]:
        if not isinstance(item, dict):
            continue
        normalized.append({
            "method": str(item.get("method", ""))[:16],
            "resourceType": str(item.get("resourceType", ""))[:40],
            "status": item.get("status") if isinstance(item.get("status"), int) else None,
            "mimeType": str(item.get("mimeType", ""))[:120],
            "url": _safe_url(str(item.get("url", ""))),
        })
    return normalized


def _normalized_a11y(data: dict[str, object]) -> dict[str, object]:
    counts = data.get("counts") if isinstance(data.get("counts"), dict) else {}
    violations = data.get("violations") if isinstance(data.get("violations"), list) else []
    concise: list[dict[str, object]] = []
    for item in violations[:200]:
        if not isinstance(item, dict):
            continue
        concise.append({
            "id": str(item.get("id", ""))[:120],
            "impact": str(item.get("impact", ""))[:40],
            "help": str(item.get("help", ""))[:500],
            "helpUrl": _safe_url(str(item.get("helpUrl", ""))),
            "nodeCount": item.get("nodeCount") if isinstance(item.get("nodeCount"), int) else 0,
        })
    return {
        "axeVersion": data.get("axeVersion"),
        "url": _safe_url(str(data.get("url", ""))),
        "counts": counts,
        "violations": concise,
    }


def _normalized_messages(data: dict[str, object], key: str) -> list[dict[str, str]]:
    values = data.get(key)
    if not isinstance(values, list):
        return []
    normalized: list[dict[str, str]] = []
    for item in values[:500]:
        if isinstance(item, dict):
            level = str(item.get("type") or item.get("level") or "unknown")[:40]
            message = item.get("text") or item.get("message") or item
        else:
            level = "error" if key == "errors" else "unknown"
            message = item
        normalized.append({"level": level, "message": _short_error(message, 1000)})
    return normalized


def run_browser_check(
    company_home: Path,
    url: str,
    *,
    expected_title: str | None = None,
    expected_text: list[str] | None = None,
    fail_on_console_errors: bool = False,
    max_a11y_violations: int | None = None,
    timeout_seconds: int = 30,
    repository_root: Path | None = None,
    runner: Runner = subprocess.run,
) -> dict[str, object]:
    target = _validate_target_url(url)
    if max_a11y_violations is not None and max_a11y_violations < 0:
        raise ValueError("max_a11y_violations_must_be_nonnegative")
    if timeout_seconds < 5 or timeout_seconds > 120:
        raise ValueError("browser_timeout_must_be_between_5_and_120")
    if expected_title is not None and (not expected_title.strip() or len(expected_title) > 500):
        raise ValueError("expected_title_invalid")
    expected_text = list(expected_text or [])
    if len(expected_text) > 10 or any(
        not isinstance(value, str) or not value.strip() or len(value) > 500
        for value in expected_text
    ):
        raise ValueError("expected_text_invalid")

    parsed = urlsplit(target)
    proof_id = (
        datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-"
        + _safe_name(parsed.hostname or "site") + "-" + uuid.uuid4().hex[:8]
    )
    output = company_home.resolve() / "browser-proofs" / proof_id
    output.mkdir(parents=True, exist_ok=False)
    started_at = datetime.now(timezone.utc).isoformat()
    started = time.perf_counter()
    tool = discover_agent_browser(repository_root)
    browser = discover_browser_executable()
    checks: list[dict[str, object]] = []
    evidence_paths: list[Path] = []
    stage = "dependency_check"
    receipt: dict[str, object] = {
        "schema": RECEIPT_SCHEMA,
        "proofId": proof_id,
        "status": "failed",
        "startedAt": started_at,
        "targetUrl": _safe_url(target),
        "expectedTitleContains": expected_title,
        "expectedTextContains": expected_text,
        "agentBrowserVersionPin": PINNED_AGENT_BROWSER_VERSION,
        "agentBrowserPath": str(tool) if tool else None,
        "browserPath": str(browser) if browser else None,
        "modelCalled": False,
        "paidApiUsed": False,
        "externalReadAttempted": False,
        "externalReadPerformed": False,
        "externalWritePerformed": False,
        "authenticatedProfileUsed": False,
        "isolatedBrowserConfigUsed": False,
    }

    client: AgentBrowserClient | None = None
    closed_in_batch = False
    try:
        if tool is None:
            raise BrowserToolError("agent_browser_missing_run_web_doctor")
        if browser is None:
            raise BrowserToolError("compatible_browser_missing_run_web_doctor")
        client = AgentBrowserClient(
            tool, browser, f"proof-{uuid.uuid4().hex[:12]}",
            timeout_seconds=timeout_seconds, runner=runner,
        )
        receipt["isolatedBrowserConfigUsed"] = True
        receipt["agentBrowserVersion"] = client.version()

        stage = "browser_batch"
        receipt["externalReadAttempted"] = True
        screenshot_path = output / "page.png"
        (
            opened,
            _loaded,
            _settled,
            title_data,
            url_data,
            text_data,
            snapshot_data,
            network_data,
            error_data,
            console_data,
            a11y_data,
            performance,
            screenshot,
            _closed,
        ) = client.run_batch([
            ["open", target],
            ["wait", "--load", "domcontentloaded"],
            ["wait", "--load", "networkidle"],
            ["get", "title"],
            ["get", "url"],
            ["get", "text", "body"],
            ["snapshot", "-c"],
            ["network", "requests"],
            ["errors"],
            ["console"],
            ["a11y"],
            ["vitals"],
            ["screenshot", str(screenshot_path), "--full"],
            ["close"],
        ])
        closed_in_batch = True
        receipt["externalReadPerformed"] = True

        stage = "evidence_write"
        title = str(title_data.get("title", ""))
        final_url = str(url_data.get("url", opened.get("url", "")))
        page_text = str(text_data.get("text", ""))
        snapshot = str(snapshot_data.get("snapshot", ""))
        network = _normalized_network_requests(network_data)
        page_errors = _normalized_messages(error_data, "errors")
        console = _normalized_messages(console_data, "messages")
        a11y = _normalized_a11y(a11y_data)

        page_text_path = output / "page-text.txt"
        page_text_path.write_text(page_text[:MAX_PAGE_TEXT_BYTES], encoding="utf-8")
        snapshot_path = output / "accessibility-snapshot.txt"
        snapshot_path.write_text(snapshot[:MAX_PAGE_TEXT_BYTES], encoding="utf-8")
        network_path = output / "network-requests.json"
        _write_json(network_path, network)
        error_path = output / "page-errors.json"
        _write_json(error_path, page_errors)
        console_path = output / "console-messages.json"
        _write_json(console_path, console)
        a11y_path = output / "accessibility-audit.json"
        _write_json(a11y_path, a11y)
        performance = _sanitize_url_fields(performance)
        if not isinstance(performance, dict):
            raise BrowserToolError("agent_browser_performance_invalid")
        performance_path = output / "performance.json"
        _write_json(performance_path, performance)
        evidence_paths.extend([
            page_text_path, snapshot_path, network_path, error_path,
            console_path, a11y_path, performance_path,
        ])

        actual_screenshot = Path(str(screenshot.get("path", screenshot_path)))
        if actual_screenshot.resolve() != screenshot_path.resolve() or not screenshot_path.is_file():
            raise BrowserToolError("agent_browser_screenshot_path_mismatch")
        evidence_paths.append(screenshot_path)

        document_requests = [item for item in network if item.get("resourceType") == "Document"]
        matching_documents = [
            item for item in document_requests
            if _url_identity(str(item.get("url", ""))) == _url_identity(final_url)
        ]
        status_candidates = matching_documents or document_requests
        document_status = status_candidates[-1].get("status") if status_candidates else None
        console_errors = [
            item for item in console if item.get("level", "").lower() in {"error", "assert"}
        ]
        a11y_count = int(a11y.get("counts", {}).get("violations", 0))
        checks.extend([
            {"name": "document_http_status", "passed": isinstance(document_status, int) and 200 <= document_status < 400, "observed": document_status},
            {"name": "nonempty_title", "passed": bool(title.strip()), "observed": title[:200]},
            {"name": "visible_page_text", "passed": bool(page_text.strip()), "observedCharacters": len(page_text)},
            {"name": "accessibility_snapshot", "passed": bool(snapshot.strip()), "observedCharacters": len(snapshot)},
            {"name": "no_page_errors", "passed": not page_errors, "observedCount": len(page_errors)},
            {"name": "screenshot_captured", "passed": screenshot_path.stat().st_size > 0, "observedBytes": screenshot_path.stat().st_size},
        ])
        if expected_title:
            checks.append({
                "name": "expected_title",
                "passed": expected_title.casefold() in title.casefold(),
                "expected": expected_title,
            })
        for index, value in enumerate(expected_text, start=1):
            checks.append({
                "name": f"expected_text_{index}",
                "passed": value.casefold() in page_text.casefold(),
                "expected": value,
            })
        if fail_on_console_errors:
            checks.append({
                "name": "no_console_errors",
                "passed": not console_errors,
                "observedCount": len(console_errors),
            })
        if max_a11y_violations is not None:
            checks.append({
                "name": "accessibility_violation_limit",
                "passed": a11y_count <= max_a11y_violations,
                "maximum": max_a11y_violations,
                "observedCount": a11y_count,
            })

        receipt.update({
            "title": title,
            "finalUrl": _safe_url(final_url),
            "documentStatus": document_status,
            "pageErrorCount": len(page_errors),
            "consoleErrorCount": len(console_errors),
            "accessibilityViolationCount": a11y_count,
            "performance": {
                "ttfbMs": performance.get("ttfb"),
                "fcpMs": performance.get("fcp"),
                "lcpMs": performance.get("lcp", {}).get("startTime")
                if isinstance(performance.get("lcp"), dict) else None,
                "cls": performance.get("cls", {}).get("score")
                if isinstance(performance.get("cls"), dict) else None,
                "inpMs": performance.get("inp"),
            },
            "checks": checks,
            "status": "passed" if all(bool(item["passed"]) for item in checks) else "failed",
            "haltStage": None,
        })
    except (
        BrowserToolError, OSError, subprocess.SubprocessError,
        TypeError, ValueError, UnicodeError,
    ) as error:
        error_detail = _short_error(error).replace(target, _safe_url(target))
        checks.append({"name": "browser_execution", "passed": False, "detail": error_detail})
        receipt.update({
            "checks": checks,
            "status": "failed",
            "haltStage": stage,
            "error": error_detail,
        })
    finally:
        if client is not None and not closed_in_batch:
            client.close()
        if client is not None:
            client.dispose()

    receipt["finishedAt"] = datetime.now(timezone.utc).isoformat()
    receipt["wallSeconds"] = round(time.perf_counter() - started, 3)
    receipt["evidence"] = [_evidence(path) for path in evidence_paths if path.is_file()]
    receipt_path = output / "receipt.json"
    receipt["receiptPath"] = str(receipt_path)
    receipt["proofDirectory"] = str(output)
    _write_json(receipt_path, receipt)
    return receipt


def run_supermega_release_suite(
    company_home: Path,
    *,
    runs: int = 1,
    timeout_seconds: int = 45,
    checker: Callable[..., dict[str, object]] = run_browser_check,
) -> dict[str, object]:
    if runs < 1 or runs > 10:
        raise ValueError("browser_suite_runs_must_be_between_1_and_10")
    if timeout_seconds < 5 or timeout_seconds > 120:
        raise ValueError("browser_timeout_must_be_between_5_and_120")

    suite_id = (
        datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        + "-supermega-release-" + uuid.uuid4().hex[:8]
    )
    output = company_home.resolve() / "browser-suites" / suite_id
    output.mkdir(parents=True, exist_ok=False)
    started_at = datetime.now(timezone.utc).isoformat()
    started = time.perf_counter()
    run_summaries: list[dict[str, object]] = []
    page_check_count = 0
    passed_page_checks = 0

    for run_number in range(1, runs + 1):
        page_summaries: list[dict[str, object]] = []
        for page in SUPERMEGA_RELEASE_PAGES:
            product = str(page["product"])
            result = checker(
                company_home,
                f"https://app.supermega.dev/settings/?product={page['id']}",
                expected_title="Setup | SuperMega",
                expected_text=[f"Set up {product}", "Open working sample"],
                fail_on_console_errors=True,
                max_a11y_violations=0,
                timeout_seconds=timeout_seconds,
            )
            page_check_count += 1
            passed = result.get("status") == "passed"
            passed_page_checks += int(passed)
            child_receipt = Path(str(result.get("receiptPath", "")))
            page_summaries.append({
                "id": page["id"],
                "product": product,
                "status": result.get("status"),
                "passed": passed,
                "documentStatus": result.get("documentStatus"),
                "title": result.get("title"),
                "pageErrorCount": result.get("pageErrorCount"),
                "consoleErrorCount": result.get("consoleErrorCount"),
                "accessibilityViolationCount": result.get("accessibilityViolationCount"),
                "wallSeconds": result.get("wallSeconds"),
                "receiptPath": str(child_receipt) if child_receipt.is_file() else None,
                "receiptSha256": _sha256(child_receipt) if child_receipt.is_file() else None,
            })
        run_passed = all(bool(item["passed"]) for item in page_summaries)
        run_summaries.append({
            "run": run_number,
            "status": "passed" if run_passed else "failed",
            "pages": page_summaries,
        })

    passed = passed_page_checks == page_check_count
    release_gate_passed = passed and runs == 10
    receipt: dict[str, object] = {
        "schema": SUITE_SCHEMA,
        "suiteId": suite_id,
        "suite": "supermega-four-product-release",
        "status": "passed" if passed else "failed",
        "promotionStatus": "release_check_ready" if release_gate_passed else "baseline_only",
        "releaseGatePassed": release_gate_passed,
        "requestedRuns": runs,
        "consecutivePassingRuns": runs if passed else 0,
        "requiredConsecutiveRuns": 10,
        "pageChecks": page_check_count,
        "passedPageChecks": passed_page_checks,
        "failedPageChecks": page_check_count - passed_page_checks,
        "startedAt": started_at,
        "finishedAt": datetime.now(timezone.utc).isoformat(),
        "wallSeconds": round(time.perf_counter() - started, 3),
        "modelCalled": False,
        "paidApiUsed": False,
        "externalReadPerformed": page_check_count > 0,
        "externalWritePerformed": False,
        "authenticatedProfileUsed": False,
        "runs": run_summaries,
        "nextAction": (
            "none" if release_gate_passed
            else "Run `local-ai.cmd web supermega --runs 10` for the promotion gate."
            if passed else "Inspect the failed child receipt before changing the site or checks."
        ),
    }
    receipt_path = output / "suite-receipt.json"
    receipt["receiptPath"] = str(receipt_path)
    receipt["suiteDirectory"] = str(output)
    _write_json(receipt_path, receipt)
    return receipt
