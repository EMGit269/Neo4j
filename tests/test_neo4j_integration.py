"""Opt-in real database test, scoped to a fresh random study-area identifier."""

import json
import os
import uuid
from dataclasses import replace

import pytest
from dotenv import load_dotenv
from neo4j import GraphDatabase

from urban_fabric.data import Layers, write_json
from urban_fabric.demo import make_demo
from urban_fabric.graph import export_graph
from urban_fabric.metrics import analyze
from urban_fabric.neo4j_store import import_graph


@pytest.mark.skipif(
    os.getenv("RUN_NEO4J_INTEGRATION") != "1",
    reason="Requires an explicitly configured real Neo4j instance",
)
def test_import_twice_is_idempotent(tmp_path):
    load_dotenv()
    settings = make_demo(tmp_path)
    settings = replace(settings, study_area_id="integration_" + uuid.uuid4().hex)
    raw = Layers.load(tmp_path / "raw")
    raw.area["id"] = settings.study_area_id
    raw.save(tmp_path / "raw")
    provenance = json.loads((tmp_path / "raw/manifest.json").read_text())
    provenance["study_area_id"] = settings.study_area_id
    write_json(tmp_path / "raw/manifest.json", provenance)
    analyze(settings)
    expected = export_graph(settings)
    with GraphDatabase.driver(
        os.getenv("NEO4J_URI", "neo4j://localhost:7687"),
        auth=(os.getenv("NEO4J_USERNAME", "neo4j"), os.environ["NEO4J_PASSWORD"]),
    ) as driver:
        with driver.session(database=os.getenv("NEO4J_DATABASE", "neo4j")) as session:
            try:
                for _ in range(2):
                    import_graph(tmp_path / "graph", batch_size=3)
                    n = session.run(
                        "MATCH (n:UrbanEntity {study_area_id: $id}) RETURN count(n)",
                        id=settings.study_area_id,
                    ).single()[0]
                    r = session.run(
                        "MATCH ()-[r {study_area_id: $id}]->() RETURN count(r)",
                        id=settings.study_area_id,
                    ).single()[0]
                    assert n == expected["nodes"]
                    assert r == expected["relationships"]
            finally:
                session.run(
                    "MATCH (n:UrbanEntity {study_area_id: $id}) DETACH DELETE n",
                    id=settings.study_area_id,
                ).consume()
