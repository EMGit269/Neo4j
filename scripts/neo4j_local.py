"""Start/stop the project-local Neo4j without changing the system Java configuration."""

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command", choices=["start", "stop", "restart", "status", "console", "version"]
    )
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    manifest = root / ".local/services/installation.json"
    if not manifest.exists():
        parser.error("Run scripts/install_neo4j_local.py first.")
    installation = json.loads(manifest.read_text(encoding="utf-8"))
    neo_home = Path(installation["neo4j_home"])
    env = {
        **os.environ,
        "JAVA_HOME": installation["java_home"],
        "NEO4J_HOME": str(neo_home),
        "NEO4J_CONF": installation["neo4j_conf"],
    }
    command = "--version" if args.command == "version" else args.command
    return subprocess.call(
        [str(neo_home / "bin/neo4j"), command],
        env=env,
        cwd=root,
        start_new_session=args.command in {"start", "restart"},
    )


if __name__ == "__main__":
    sys.exit(main())
