"""Generated, read-only identity for the local runtime build.

The release digest covers every Python file in this package except this manifest,
plus the fixed local lifecycle scripts used to check, recover, and verify it.
Release validation recomputes it; the running service performs no filesystem or
Git reads to construct its health response.
"""

RUNTIME_BUILD_SCHEMA = "local-company.runtime-build.v2"
BUILD_ID = "local-build-20260727.14"
SOURCE_SHA256 = "9972c6c5c83afc3b035c0fdde462cc0376b8c50133fc9449d50899d07b372991"
