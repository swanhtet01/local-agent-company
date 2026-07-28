"""Generated, read-only identity for the local runtime build.

The release digest covers every Python file in this package except this manifest,
plus the fixed local lifecycle scripts used to check, recover, and verify it.
Release validation recomputes it; the running service performs no filesystem or
Git reads to construct its health response.
"""

RUNTIME_BUILD_SCHEMA = "local-company.runtime-build.v2"
BUILD_ID = "local-build-20260729.50"
SOURCE_SHA256 = "dea64b96bcb7d4dc9699d62eaa7d5f2e5aa35a1c426fdf33d91e058acd3d9a5d"
