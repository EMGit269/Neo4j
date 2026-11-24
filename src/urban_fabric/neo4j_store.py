"""Parameterized, batched Neo4j imports; APOC and GDS are not required."""

import os
from collections import defaultdict
from pathlib import Path

from neo4j import GraphDatabase

from .graph import validate_graph


def _batches(rows, size):
    for start in range(0, len(rows), size):
        yield rows[start : start + size]


def _execute(tx, query, rows):
    tx.run(query, rows=rows).consume()


def import_graph(
    directory: Path,
    *,
    database: str | None = None,
    batch_size: int = 500,
    dry_run: bool = False,
    replace_study_area: bool = False,
) -> dict:
    if batch_size <= 0:
        raise ValueError("batch_size 必须大于零。")
    metadata, nodes, relationships = validate_graph(directory)
    result = {
        "study_area_id": metadata["study_area_id"],
        "nodes": len(nodes),
        "relationships": len(relationships),
        "dry_run": dry_run,
    }
    if dry_run:
        return result
    uri = os.getenv("NEO4J_URI", "neo4j://localhost:7687")
    username = os.getenv("NEO4J_USERNAME", "neo4j")
    password = os.getenv("NEO4J_PASSWORD")
    if not password:
        raise ValueError("请在 .env 或环境变量中设置 NEO4J_PASSWORD。")
    database = database or os.getenv("NEO4J_DATABASE", "neo4j")
    with GraphDatabase.driver(uri, auth=(username, password), connection_timeout=15) as driver:
        driver.verify_connectivity()
        with driver.session(database=database) as session:
            session.run(
                "CREATE CONSTRAINT urban_entity_uid IF NOT EXISTS "
                "FOR (n:UrbanEntity) REQUIRE n.uid IS UNIQUE"
            ).consume()
            session.run(
                "CREATE INDEX urban_study_area IF NOT EXISTS "
                "FOR (n:UrbanEntity) ON (n.study_area_id)"
            ).consume()
            if replace_study_area:
                session.run(
                    "MATCH (n:UrbanEntity {study_area_id: $study_area_id}) DETACH DELETE n",
                    study_area_id=metadata["study_area_id"],
                ).consume()
            groups = defaultdict(list)
            for row in nodes:
                groups[row["label"]].append(row)
            for label, rows in groups.items():
                # Labels/types passed validation against fixed allowlists, not user Cypher.
                query = (
                    "UNWIND $rows AS row MERGE (n:UrbanEntity {uid: row.uid}) "
                    f"SET n:{label} SET n += row.properties "
                    "SET n.location = point({longitude: row.properties.longitude, "
                    "latitude: row.properties.latitude})"
                )
                for batch in _batches(rows, batch_size):
                    session.execute_write(_execute, query, batch)
            groups.clear()
            for row in relationships:
                groups[row["type"]].append(row)
            for kind, rows in groups.items():
                query = (
                    "UNWIND $rows AS row MATCH (a:UrbanEntity {uid: row.source}) "
                    "MATCH (b:UrbanEntity {uid: row.target}) "
                    f"MERGE (a)-[r:{kind} {{uid: row.uid}}]->(b) SET r += row.properties"
                )
                for batch in _batches(rows, batch_size):
                    session.execute_write(_execute, query, batch)
    result["mode"] = "replace-study-area" if replace_study_area else "upsert"
    return result
