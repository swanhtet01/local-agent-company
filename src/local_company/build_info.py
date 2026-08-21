"""Generated, read-only identity for the local runtime build.

The release digest covers every Python file in this package except this manifest,
plus the fixed local lifecycle and orchestration scripts used to operate it.
Release validation recomputes it; the running service performs no filesystem or
Git reads to construct its health response.
"""

RUNTIME_BUILD_SCHEMA = "local-company.runtime-build.v2"
BUILD_ID = "local-build-20260821.3"
SOURCE_SHA256 = "69d2689168ecf5210755b3f964eeaa4bd2ceb37f0e15226dbd5173df53f7a0f7"
