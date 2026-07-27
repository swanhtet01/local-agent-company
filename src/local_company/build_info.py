"""Generated, read-only identity for the local runtime build.

The release digest covers every Python file in this package except this manifest,
plus the fixed local lifecycle scripts used to check, recover, and verify it.
Release validation recomputes it; the running service performs no filesystem or
Git reads to construct its health response.
"""

RUNTIME_BUILD_SCHEMA = "local-company.runtime-build.v2"
BUILD_ID = "local-build-20260727.18"
SOURCE_SHA256 = "06fcf06aba73fd1ee225c1b316df83f36edb430cf0937d4bda3af3c7932c7005"
