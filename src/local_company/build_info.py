"""Generated, read-only identity for the local runtime build.

The release digest covers every Python file in this package except this manifest,
plus the fixed local lifecycle scripts used to check, recover, and verify it.
Release validation recomputes it; the running service performs no filesystem or
Git reads to construct its health response.
"""

RUNTIME_BUILD_SCHEMA = "local-company.runtime-build.v2"
BUILD_ID = "local-build-20260730.81"
SOURCE_SHA256 = "8fa9e9f8c9fc9d4e1b4f3e7f77f59d6d374155c32c6a5e47a6fa84475fa18cd2"
