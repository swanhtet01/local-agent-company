"""Generated, read-only identity for the local runtime build.

The release digest covers every Python file in this package except this manifest,
plus the fixed local lifecycle and orchestration scripts used to operate it.
Release validation recomputes it; the running service performs no filesystem or
Git reads to construct its health response.
"""

RUNTIME_BUILD_SCHEMA = "local-company.runtime-build.v2"
BUILD_ID = "local-build-20260809.2"
SOURCE_SHA256 = "fa0ea94bea65681d791a4ec1c836b2d60f85da04330b53a7dc9b6e6517c7b4cc"
