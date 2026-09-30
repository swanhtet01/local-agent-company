"""Generated, read-only identity for the local runtime build.

The release digest covers every Python file in this package except this manifest,
plus the fixed local lifecycle and orchestration scripts used to operate it.
Release validation recomputes it; the running service performs no filesystem or
Git reads to construct its health response.
"""

RUNTIME_BUILD_SCHEMA = "local-company.runtime-build.v2"
BUILD_ID = "local-build-20260930.3"
SOURCE_SHA256 = "33dbb1d0700c0e97ad3ff5a3c7c5745ca47d1d4bcea032fb208ba902933f27cf"
