"""Generated, read-only identity for the local runtime build.

The release digest covers every Python file in this package except this manifest,
plus the fixed local lifecycle scripts used to check, recover, and verify it.
Release validation recomputes it; the running service performs no filesystem or
Git reads to construct its health response.
"""

RUNTIME_BUILD_SCHEMA = "local-company.runtime-build.v2"
BUILD_ID = "local-build-20260801.3"
SOURCE_SHA256 = "e1971fe2365527a8cae1757a340d488e623ad45491418724c9615b94aaa2a32c"
