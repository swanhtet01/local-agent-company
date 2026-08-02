"""Generated, read-only identity for the local runtime build.

The release digest covers every Python file in this package except this manifest,
plus the fixed local lifecycle scripts used to check, recover, and verify it.
Release validation recomputes it; the running service performs no filesystem or
Git reads to construct its health response.
"""

RUNTIME_BUILD_SCHEMA = "local-company.runtime-build.v2"
BUILD_ID = "local-build-20260802.4"
SOURCE_SHA256 = "d57f359b02ca466c9bc2b26d91446b22796e46a83e94a6a2d763217db867f0ca"
