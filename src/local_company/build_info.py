"""Generated, read-only identity for the local runtime build.

The release digest covers every Python file in this package except this manifest,
plus the fixed local lifecycle and orchestration scripts used to operate it.
Release validation recomputes it; the running service performs no filesystem or
Git reads to construct its health response.
"""

RUNTIME_BUILD_SCHEMA = "local-company.runtime-build.v2"
BUILD_ID = "local-build-20260914.4"
SOURCE_SHA256 = "5c6f9a10c2f06369f02aa86d3ef291d90c291f3ba6555bf14299bc094bb3aaf2"
