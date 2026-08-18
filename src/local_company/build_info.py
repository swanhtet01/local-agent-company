"""Generated, read-only identity for the local runtime build.

The release digest covers every Python file in this package except this manifest,
plus the fixed local lifecycle and orchestration scripts used to operate it.
Release validation recomputes it; the running service performs no filesystem or
Git reads to construct its health response.
"""

RUNTIME_BUILD_SCHEMA = "local-company.runtime-build.v2"
BUILD_ID = "local-build-20260818.2"
SOURCE_SHA256 = "b0b82dc51e1d2b698bdac64d89e8ba8c19a28b4f96dfa20c47e66356d5c51038"
