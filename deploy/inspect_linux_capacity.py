"""Read-only Linux capacity preflight; never grants deployment acceptance."""
from __future__ import annotations

import json
import platform
import shutil
from pathlib import Path

GIB = 1024 ** 3


def assess(meminfo: str, disk_free: int) -> dict:
    fields = {}
    for line in meminfo.splitlines():
        parts = line.split()
        if parts and parts[0] in {"MemTotal:", "MemAvailable:"}:
            if len(parts) != 3 or parts[2] != "kB" or parts[0] in fields:
                raise ValueError("invalid memory observation")
            fields[parts[0]] = int(parts[1]) * 1024
    total, available = fields["MemTotal:"], fields["MemAvailable:"]
    if not 0 <= available <= total or total <= 0 or disk_free < 0:
        raise ValueError("invalid capacity observation")
    # Seven GiB container limits plus one GiB host reserve. Existing workloads
    # must fit outside this reservation; total installed RAM alone is not enough.
    checks = {"available_memory": available >= 8 * GIB,
              "free_disk": disk_free >= 20 * GIB}
    return {"schema": "supermega.linux-capacity.v1", "status": "observed",
            "memory_total_bytes": total, "memory_available_bytes": available,
            "disk_free_bytes": disk_free, "checks": checks,
            "capacity_pass": all(checks.values()), "deployment_accepted": False,
            "workload_isolation_verified": False}


def main() -> int:
    try:
        if platform.system() != "Linux":
            raise ValueError("unsupported platform")
        result = assess(Path("/proc/meminfo").read_text(encoding="ascii"),
                        shutil.disk_usage(Path.cwd()).free)
    except (OSError, ValueError, KeyError):
        # Do not leak host paths or exception details into a portable receipt.
        print(json.dumps({"schema": "supermega.linux-capacity.v1",
                          "status": "inspection_unavailable",
                          "capacity_pass": False, "deployment_accepted": False}))
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0 if result["capacity_pass"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
