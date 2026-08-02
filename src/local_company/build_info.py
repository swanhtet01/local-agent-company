"""Generated, read-only identity for the local runtime build.

The release digest covers every Python file in this package except this manifest,
plus the fixed local lifecycle scripts used to check, recover, and verify it.
Release validation recomputes it; the running service performs no filesystem or
Git reads to construct its health response.
"""

RUNTIME_BUILD_SCHEMA = "local-company.runtime-build.v2"
BUILD_ID = "local-build-20260802.8"
SOURCE_SHA256 = "51b28c889c55af870cc1c01b59411e379da48e378cddedd588add17c56f7de42"
