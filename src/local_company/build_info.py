"""Generated, read-only identity for the local runtime build.

The release digest covers every Python file in this package except this manifest,
plus the fixed local lifecycle and orchestration scripts used to operate it.
Release validation recomputes it; the running service performs no filesystem or
Git reads to construct its health response.
"""

RUNTIME_BUILD_SCHEMA = "local-company.runtime-build.v2"
BUILD_ID = "local-build-20260821.4"
SOURCE_SHA256 = "7f63f0a487ba852b942b5bb9581fab59c4af7e33d96dc225b8de26c9be6196c8"
