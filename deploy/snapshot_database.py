"""Create a new SQLite snapshot; never overwrite or restore the live store."""
from contextlib import closing
import argparse
import json
import os
from pathlib import Path
import sqlite3


def snapshot(source: Path, destination: Path) -> dict:
    source = source.resolve(strict=True)
    destination = destination.absolute()
    if source == destination.resolve():
        raise ValueError("Source and destination must differ")
    # Exclusive creation also refuses existing files and symlinks.
    fd = os.open(destination, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(fd)
    try:
        with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)) as reader:
            with closing(sqlite3.connect(destination)) as writer:
                reader.backup(writer)
        with closing(sqlite3.connect(destination.as_uri() + "?mode=ro", uri=True)) as restored:
            if restored.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
                raise ValueError("Snapshot integrity check failed")
        return {"schema": "local-company.database-snapshot.v1", "status": "PASS",
                "integrity": "ok", "bytes": destination.stat().st_size,
                "live_store_replaced": False, "full_state_backup": False}
    except Exception:
        # Leave the failed file for inspection; never report it as accepted.
        raise


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    print(json.dumps(snapshot(args.source, args.destination)))
