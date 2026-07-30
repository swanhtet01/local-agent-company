from __future__ import annotations

import ctypes
import json
import os
import subprocess
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable

from .core import Company, ROLES
from .focus import read_execution_focus
from .service import service_status


MACHINE_CAPACITY_SCHEMA = "local-company.machine-capacity.v1"
LISTENER_PORTS = (5173, 8765, 8788, 11434)
MIN_AVAILABLE_MEMORY_BYTES = 1024 * 1024 * 1024
_MAX_NETSTAT_BYTES = 256 * 1024
_MAX_OLLAMA_RESPONSE_BYTES = 32 * 1024


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        return None


def parse_windows_listeners(output: str) -> dict[int, int]:
    if type(output) is not str or len(output.encode("utf-8")) > _MAX_NETSTAT_BYTES:
        raise RuntimeError("Listener inventory is invalid")
    owners: dict[int, set[int]] = {port: set() for port in LISTENER_PORTS}
    for line in output.splitlines():
        fields = line.split()
        if len(fields) != 5 or fields[0].upper() != "TCP" or fields[3].upper() != "LISTENING":
            continue
        try:
            port = int(fields[1].rsplit(":", 1)[1])
            pid = int(fields[4])
        except (IndexError, ValueError):
            continue
        if port in owners and pid > 0:
            owners[port].add(pid)
    return {port: len(owners[port]) for port in LISTENER_PORTS}


def observe_listeners() -> dict[str, object]:
    if os.name != "nt":
        return {"status": "unavailable", "counts": {str(port): None for port in LISTENER_PORTS}}
    try:
        completed = subprocess.run(
            ["netstat", "-ano", "-p", "tcp"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=3,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return {"status": "unavailable", "counts": {str(port): None for port in LISTENER_PORTS}}
    if completed.returncode != 0 or len(completed.stdout) > _MAX_NETSTAT_BYTES:
        return {"status": "unavailable", "counts": {str(port): None for port in LISTENER_PORTS}}
    try:
        parsed = parse_windows_listeners(completed.stdout.decode("utf-8", errors="strict"))
    except (UnicodeDecodeError, RuntimeError):
        return {"status": "unavailable", "counts": {str(port): None for port in LISTENER_PORTS}}
    return {"status": "ready", "counts": {str(port): parsed[port] for port in LISTENER_PORTS}}


def observe_loaded_models(ollama_listener_count: int | None) -> dict[str, object]:
    if ollama_listener_count == 0:
        return {"status": "absent", "loaded_count": 0}
    if type(ollama_listener_count) is not int or ollama_listener_count < 0:
        return {"status": "unavailable", "loaded_count": None}
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
    request = urllib.request.Request(
        "http://127.0.0.1:11434/api/ps",
        headers={"Accept": "application/json"},
        method="GET",
    )
    try:
        with opener.open(request, timeout=2) as response:
            if response.status != 200:
                return {"status": "unavailable", "loaded_count": None}
            raw = response.read(_MAX_OLLAMA_RESPONSE_BYTES + 1)
    except (OSError, urllib.error.URLError, urllib.error.HTTPError, TimeoutError):
        return {"status": "unavailable", "loaded_count": None}
    if len(raw) > _MAX_OLLAMA_RESPONSE_BYTES:
        return {"status": "unavailable", "loaded_count": None}
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {"status": "unavailable", "loaded_count": None}
    models = payload.get("models") if type(payload) is dict else None
    if type(models) is not list or len(models) > 8:
        return {"status": "unavailable", "loaded_count": None}
    for model in models:
        if type(model) is not dict or type(model.get("name")) is not str or not 1 <= len(model["name"]) <= 256:
            return {"status": "unavailable", "loaded_count": None}
    return {"status": "ready", "loaded_count": len(models)}


def observe_memory() -> dict[str, object]:
    if os.name != "nt":
        return {"status": "unavailable", "total_bytes": None, "available_bytes": None}

    class MemoryStatusEx(ctypes.Structure):
        _fields_ = [
            ("dwLength", ctypes.c_ulong),
            ("dwMemoryLoad", ctypes.c_ulong),
            ("ullTotalPhys", ctypes.c_ulonglong),
            ("ullAvailPhys", ctypes.c_ulonglong),
            ("ullTotalPageFile", ctypes.c_ulonglong),
            ("ullAvailPageFile", ctypes.c_ulonglong),
            ("ullTotalVirtual", ctypes.c_ulonglong),
            ("ullAvailVirtual", ctypes.c_ulonglong),
            ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
        ]

    state = MemoryStatusEx()
    state.dwLength = ctypes.sizeof(MemoryStatusEx)
    try:
        succeeded = ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(state))  # type: ignore[attr-defined]
    except (AttributeError, OSError):
        return {"status": "unavailable", "total_bytes": None, "available_bytes": None}
    if not succeeded or state.ullTotalPhys <= 0 or state.ullAvailPhys > state.ullTotalPhys:
        return {"status": "unavailable", "total_bytes": None, "available_bytes": None}
    return {
        "status": "ready",
        "total_bytes": int(state.ullTotalPhys),
        "available_bytes": int(state.ullAvailPhys),
    }


def build_capacity_snapshot(
    *,
    brief: dict[str, object],
    focus: dict[str, object],
    health: dict[str, object],
    listeners: dict[str, object],
    loaded_models: dict[str, object],
    memory: dict[str, object],
    service: dict[str, object],
) -> dict[str, object]:
    counts = listeners.get("counts") if type(listeners) is dict else None
    if type(counts) is not dict:
        counts = {str(port): None for port in LISTENER_PORTS}
    blockers: list[str] = []
    indeterminate: list[str] = []
    advisories: list[str] = []

    if not focus.get("enabled"):
        blockers.append("execution_focus_disabled")
    elif brief.get("project_id") != focus.get("projectId"):
        blockers.append("execution_focus_project_mismatch")

    active_jobs = health.get("active_jobs")
    running_missions = health.get("running_missions")
    if type(active_jobs) is not int or type(running_missions) is not int:
        indeterminate.append("company_work_state_unavailable")
    else:
        if active_jobs > 1:
            blockers.append("multiple_active_jobs")
        if running_missions > 1:
            blockers.append("multiple_running_missions")

    if listeners.get("status") != "ready":
        indeterminate.append("listener_inventory_unavailable")
    else:
        for port in LISTENER_PORTS:
            value = counts.get(str(port))
            if type(value) is not int or value < 0:
                indeterminate.append("listener_inventory_invalid")
                break
            if value > 1:
                blockers.append(f"duplicate_listener_{port}")

    if service.get("status") != "running" or service.get("live") is not True:
        blockers.append("company_service_not_verified")
    if service.get("process_identity_status") != "match":
        blockers.append("company_service_identity_not_verified")

    loaded_count = loaded_models.get("loaded_count")
    if loaded_models.get("status") not in {"ready", "absent"} or type(loaded_count) is not int:
        indeterminate.append("loaded_model_inventory_unavailable")
    elif active_jobs == 0 and loaded_count > 0:
        blockers.append("idle_model_loaded")
    elif loaded_count > 1:
        blockers.append("multiple_models_loaded")

    available_memory = memory.get("available_bytes")
    if memory.get("status") != "ready" or type(available_memory) is not int:
        indeterminate.append("memory_inventory_unavailable")
    elif available_memory < MIN_AVAILABLE_MEMORY_BYTES:
        blockers.append("memory_headroom_below_1gib")

    if brief.get("status") == "attention_required":
        if (
            brief.get("next_action") == "review_quality_failures_before_retry"
            and active_jobs == 0
            and running_missions == 0
        ):
            advisories.append("quality_failures_retained_for_review")
        else:
            blockers.append("company_attention_required")
    elif brief.get("status") not in {"ready", "work_pending"}:
        indeterminate.append("operator_brief_unavailable")

    blockers = list(dict.fromkeys(blockers))
    indeterminate = list(dict.fromkeys(indeterminate))
    advisories = list(dict.fromkeys(advisories))
    if indeterminate:
        status = "indeterminate"
        next_action = "inspect_machine_capacity_observations"
    elif any(item.startswith("duplicate_listener_") for item in blockers):
        status = "attention_required"
        next_action = "review_duplicate_local_runtime"
    elif "idle_model_loaded" in blockers:
        status = "attention_required"
        next_action = "release_idle_model_memory"
    elif "company_attention_required" in blockers:
        status = "attention_required"
        next_action = str(brief.get("next_action", "review_company_attention"))
    elif blockers:
        status = "attention_required"
        next_action = "review_capacity_blockers"
    else:
        status = "ready"
        next_action = str(brief.get("next_action", "queue_or_schedule_reviewed_mission"))

    return {
        "schema": MACHINE_CAPACITY_SCHEMA,
        "status": status,
        "focus": {
            "enabled": focus.get("enabled"),
            "project_id": focus.get("projectId"),
            "max_roles": focus.get("maxRoles"),
        },
        "company": {
            "registered_roles": len(ROLES),
            "resident_role_processes": 0,
            "execution_parallelism": 1,
            "active_jobs": active_jobs,
            "running_missions": running_missions,
        },
        "runtime": {
            "service_live": service.get("live") is True,
            "service_identity": service.get("process_identity_status"),
            "listener_counts": {str(port): counts.get(str(port)) for port in LISTENER_PORTS},
            "loaded_models": loaded_count,
            "available_memory_bytes": available_memory,
            "minimum_available_memory_bytes": MIN_AVAILABLE_MEMORY_BYTES,
        },
        "company_attention": {
            "status": brief.get("status"),
            "next_action": brief.get("next_action"),
        },
        "blockers": blockers,
        "indeterminate": indeterminate,
        "advisories": advisories,
        "next_action": next_action,
        "effects": {
            "database_mutated": False,
            "model_called": False,
            "process_started": False,
            "process_stopped": False,
            "queue_changed": False,
        },
    }


def machine_capacity_snapshot(
    company: Company,
    project: str,
    *,
    listener_observer: Callable[[], dict[str, object]] = observe_listeners,
    memory_observer: Callable[[], dict[str, object]] = observe_memory,
    model_observer: Callable[[int | None], dict[str, object]] = observe_loaded_models,
) -> dict[str, object]:
    listeners = listener_observer()
    counts = listeners.get("counts") if type(listeners) is dict else None
    ollama_listener_count = counts.get("11434") if type(counts) is dict else None
    return build_capacity_snapshot(
        brief=company.operator_brief(project),
        focus=read_execution_focus(company.home),
        health=company.health_snapshot(),
        listeners=listeners,
        loaded_models=model_observer(ollama_listener_count),
        memory=memory_observer(),
        service=service_status(Path(company.home)),
    )
