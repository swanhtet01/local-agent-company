"""Generated, read-only identity for the local runtime build.

The release digest covers every Python file in this package except this manifest,
plus the fixed local lifecycle and orchestration scripts used to operate it.
Release validation recomputes it; the running service performs no filesystem or
Git reads to construct its health response.
"""

RUNTIME_BUILD_SCHEMA = "local-company.runtime-build.v2"
BUILD_ID = "local-build-20260802.39"
SOURCE_SHA256 = "1b58abbdc45a2f6a17ff9c580c5e943d1e3dff2bcbdc9ddc97273d7474ecd1f1"
