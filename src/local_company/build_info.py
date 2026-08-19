"""Generated, read-only identity for the local runtime build.

The release digest covers every Python file in this package except this manifest,
plus the fixed local lifecycle and orchestration scripts used to operate it.
Release validation recomputes it; the running service performs no filesystem or
Git reads to construct its health response.
"""

RUNTIME_BUILD_SCHEMA = "local-company.runtime-build.v2"
BUILD_ID = "local-build-20260819.7"
SOURCE_SHA256 = "b4d3a88986792acd9752e3b96e97a8a71038fcc3d6b526f72a3e1b44ff55dc68"
