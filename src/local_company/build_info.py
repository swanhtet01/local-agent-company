"""Generated, read-only identity for the local runtime build.

The release digest covers every Python file in this package except this manifest,
plus the fixed local lifecycle scripts used to check, recover, and verify it.
Release validation recomputes it; the running service performs no filesystem or
Git reads to construct its health response.
"""

RUNTIME_BUILD_SCHEMA = "local-company.runtime-build.v2"
BUILD_ID = "local-build-20260731.5"
SOURCE_SHA256 = "9d838b55bc64a1ba4d8770af8fc0e30d8f15f22c2c7d9daf76ce7bbeaea3c0d0"
