"""Generated, read-only identity for the local runtime build.

The release digest covers every Python file in this package except this manifest,
plus the fixed local lifecycle and orchestration scripts used to operate it.
Release validation recomputes it; the running service performs no filesystem or
Git reads to construct its health response.
"""

RUNTIME_BUILD_SCHEMA = "local-company.runtime-build.v2"
BUILD_ID = "local-build-20260802.42"
SOURCE_SHA256 = "c8e450a827514eb60510262ea2f751ce6a53344a23f17622aef7781e77765961"
