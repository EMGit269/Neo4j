"""Install Neo4j with Homebrew and keep database/configuration inside this project."""

import argparse
import json
import os
import secrets
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[1]
BASE = ROOT / ".local/services"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--skip-install", action="store_true", help="Configure an existing Homebrew installation"
    )
    args = parser.parse_args()
    if sys.platform != "darwin":
        raise RuntimeError("This helper is for macOS. Use Compose or an existing Neo4j elsewhere.")
    brew = shutil.which("brew") or "/opt/homebrew/bin/brew"
    environment = {**os.environ, "HOMEBREW_NO_AUTO_UPDATE": "1", "HOMEBREW_NO_INSTALL_CLEANUP": "1"}
    if not args.skip_install:
        result = subprocess.run([brew, "install", "neo4j"], env=environment)
        if result.returncode:
            raise RuntimeError("Homebrew installation did not finish.")
    prefix = Path(
        subprocess.check_output([brew, "--prefix", "neo4j"], env=environment, text=True).strip()
    )
    java_prefix = Path(
        subprocess.check_output(
            [brew, "--prefix", "openjdk@21"], env=environment, text=True
        ).strip()
    )
    neo_home = prefix / "libexec"
    java_home = java_prefix / "libexec/openjdk.jdk/Contents/Home"
    if not (neo_home / "bin/neo4j").exists() or not (java_home / "bin/java").exists():
        raise RuntimeError("Neo4j or Java 21 is not installed yet.")
    instance = BASE / "neo4j"
    conf = instance / "conf"
    for directory in [
        conf,
        instance / "data",
        instance / "logs",
        instance / "run",
        instance / "import",
        instance / "plugins",
        instance / "transactions",
    ]:
        directory.mkdir(parents=True, exist_ok=True)
    for source in (neo_home / "conf").glob("*.xml"):
        if not (conf / source.name).exists():
            shutil.copy2(source, conf / source.name)
    lines = [
        "# Project-local development instance.",
        "server.default_listen_address=127.0.0.1",
        "server.default_advertised_address=127.0.0.1",
        "server.http.enabled=true",
        "server.http.listen_address=127.0.0.1:7474",
        "server.http.advertised_address=127.0.0.1:7474",
        "server.https.enabled=false",
        "server.bolt.enabled=true",
        "server.bolt.listen_address=127.0.0.1:7687",
        "server.bolt.advertised_address=127.0.0.1:7687",
        "server.memory.heap.initial_size=256m",
        "server.memory.heap.max_size=1g",
        "server.memory.pagecache.size=512m",
        "dbms.security.auth_enabled=true",
    ]
    for key, directory in [
        ("data", "data"),
        ("logs", "logs"),
        ("run", "run"),
        ("import", "import"),
        ("plugins", "plugins"),
        ("transaction.logs.root", "transactions"),
    ]:
        lines.append(f"server.directories.{key}={instance / directory}")
    if not (conf / "neo4j.conf").exists():
        (conf / "neo4j.conf").write_text("\n".join(lines) + "\n", encoding="utf-8")
    env_path = ROOT / ".env"
    if not env_path.exists():
        password = secrets.token_urlsafe(24)
        fd = os.open(env_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as file:
            file.write(
                "NEO4J_URI=neo4j://127.0.0.1:7687\nNEO4J_USERNAME=neo4j\n"
                f"NEO4J_PASSWORD={password}\nNEO4J_DATABASE=neo4j\n"
            )
        print("Created private .env; password is not printed.", flush=True)
    else:
        values = dotenv_values(env_path)
        if values.get("NEO4J_URI") not in {
            "neo4j://127.0.0.1:7687",
            "neo4j://localhost:7687",
            "bolt://127.0.0.1:7687",
            "bolt://localhost:7687",
        }:
            raise RuntimeError("Existing .env points elsewhere and was preserved.")
        password = values.get("NEO4J_PASSWORD")
        if not password:
            raise RuntimeError("Existing .env has no password and was preserved.")
    environment.update(
        {"JAVA_HOME": str(java_home), "NEO4J_HOME": str(neo_home), "NEO4J_CONF": str(conf)}
    )
    version = subprocess.check_output(
        [str(neo_home / "bin/neo4j"), "--version"], env=environment, text=True
    ).strip()
    if not (instance / "data/databases/system").exists():
        result = subprocess.run(
            [str(neo_home / "bin/neo4j-admin"), "dbms", "set-initial-password", password],
            env=environment,
            capture_output=True,
            text=True,
        )
        if result.returncode:
            raise RuntimeError((result.stdout + result.stderr).replace(password, "[redacted]"))
        print(result.stdout.replace(password, "[redacted]").strip(), flush=True)
    metadata = {
        "installed_at_utc": datetime.now(timezone.utc).isoformat(),
        "distribution": "Homebrew bottle",
        "neo4j_version": version,
        "neo4j_home": str(neo_home),
        "neo4j_conf": str(conf),
        "instance_dir": str(instance),
        "java_home": str(java_home),
        "http_url": "http://127.0.0.1:7474",
        "bolt_uri": "neo4j://127.0.0.1:7687",
    }
    (BASE / "installation.json").write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {"neo4j_version": version, "instance_dir": str(instance), "configuration": str(conf)}
        ),
        flush=True,
    )


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(f"Setup error: {error}", file=sys.stderr)
        raise SystemExit(1) from None
