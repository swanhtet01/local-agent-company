"""Generated, read-only identity for the local runtime build.

The release digest covers every Python file in this package except this manifest,
plus the fixed local lifecycle and orchestration scripts used to operate it.
Release validation recomputes it; the running service performs no filesystem or
Git reads to construct its health response.
"""

RUNTIME_BUILD_SCHEMA = "local-company.runtime-build.v2"
BUILD_ID = "local-build-20260819.11"
SOURCE_SHA256 = "7bd1de6d52a42e24897c1d81516ecca9342df9f5b3ad3d489a1badf69b2ff8e5"
