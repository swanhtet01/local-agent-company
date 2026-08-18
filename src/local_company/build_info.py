"""Generated, read-only identity for the local runtime build.

The release digest covers every Python file in this package except this manifest,
plus the fixed local lifecycle and orchestration scripts used to operate it.
Release validation recomputes it; the running service performs no filesystem or
Git reads to construct its health response.
"""

RUNTIME_BUILD_SCHEMA = "local-company.runtime-build.v2"
BUILD_ID = "local-build-20260819.2"
SOURCE_SHA256 = "01d4179e5a2a21b6415403ca3f658361a6272a94acf7913b33b63e265a53d6d9"
