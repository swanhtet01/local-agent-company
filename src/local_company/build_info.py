"""Generated, read-only identity for the local runtime build.

The release digest covers every Python file in this package except this manifest,
plus the fixed local lifecycle scripts used to check, recover, and verify it.
Release validation recomputes it; the running service performs no filesystem or
Git reads to construct its health response.
"""

RUNTIME_BUILD_SCHEMA = "local-company.runtime-build.v2"
BUILD_ID = "local-build-20260727.23"
SOURCE_SHA256 = "c90eb2bd1805433ecf7f1dfaa4b3dbfe488730ed4ecbfbb6a9158eaf61c982d3"
