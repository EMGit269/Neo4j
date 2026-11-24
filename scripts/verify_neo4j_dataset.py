"""Read back the scoped Neo4j dataset and compare every exported entity/property."""

import argparse
import math
import os
from collections import Counter
from pathlib import Path

from dotenv import load_dotenv
from neo4j import GraphDatabase

from urban_fabric.data import utc_now, write_json
from urban_fabric.graph import validate_graph


def verify(directory):
    metadata, nodes, relations = validate_graph(directory)
    identifier = metadata["study_area_id"]
    load_dotenv()
    with GraphDatabase.driver(
        os.environ["NEO4J_URI"],
        auth=(os.getenv("NEO4J_USERNAME", "neo4j"), os.environ["NEO4J_PASSWORD"]),
        connection_timeout=15,
    ) as driver:
        with driver.session(database=os.getenv("NEO4J_DATABASE", "neo4j")) as session:
            actual_nodes = list(
                session.run(
                    "MATCH (n:UrbanEntity {study_area_id:$id}) "
                    "RETURN n.uid AS uid, labels(n) AS labels, properties(n) AS properties",
                    id=identifier,
                )
            )
            actual_relations = list(
                session.run(
                    "MATCH (a:UrbanEntity {study_area_id:$id})-[r]->(b) "
                    "RETURN r.uid AS uid, type(r) AS type, a.uid AS source, "
                    "b.uid AS target, properties(r) AS properties",
                    id=identifier,
                )
            )
    expected_node_ids = {row["uid"] for row in nodes}
    expected_relation_ids = {row["uid"] for row in relations}
    checks = {
        "node_count": len(actual_nodes) == len(nodes),
        "relation_count": len(actual_relations) == len(relations),
        "node_ids": {row["uid"] for row in actual_nodes} == expected_node_ids,
        "relation_ids": {row["uid"] for row in actual_relations} == expected_relation_ids,
    }
    by_node = {row["uid"]: row for row in actual_nodes}
    by_relation = {row["uid"]: row for row in actual_relations}

    def same_properties(expected, actual):
        for key, value in expected.items():
            found = actual.get(key)
            if isinstance(value, float):
                if not isinstance(found, (float, int)) or not math.isclose(
                    value, found, rel_tol=1e-12, abs_tol=1e-9
                ):
                    return False
            elif value != found:
                return False
        return True

    checks["node_labels_properties_and_locations"] = all(
        row["uid"] in by_node
        and set(by_node[row["uid"]]["labels"]) == {"UrbanEntity", row["label"]}
        and same_properties(row["properties"], by_node[row["uid"]]["properties"])
        and math.isclose(
            by_node[row["uid"]]["properties"]["location"].longitude,
            row["properties"]["longitude"],
            abs_tol=1e-10,
        )
        and math.isclose(
            by_node[row["uid"]]["properties"]["location"].latitude,
            row["properties"]["latitude"],
            abs_tol=1e-10,
        )
        for row in nodes
    )
    checks["relation_types_endpoints_and_properties"] = all(
        row["uid"] in by_relation
        and all(row[key] == by_relation[row["uid"]][key] for key in ["type", "source", "target"])
        and same_properties(row["properties"], by_relation[row["uid"]]["properties"])
        for row in relations
    )
    report = {
        "checked_at_utc": utc_now(),
        "study_area_id": identifier,
        "all_checks_passed": all(checks.values()),
        "checks": checks,
        "nodes": len(actual_nodes),
        "relationships": len(actual_relations),
        "node_counts_by_label": dict(Counter(row["label"] for row in nodes)),
        "relationship_counts_by_type": dict(Counter(row["type"] for row in relations)),
        "connects_to_length_sum_m": sum(
            row["properties"]["length_m"]
            for row in actual_relations
            if row["type"] == "CONNECTS_TO"
        ),
    }
    write_json(directory / "neo4j-verification.json", report)
    if not report["all_checks_passed"]:
        raise ValueError("Neo4j readback differs from the exported graph; see verification report.")
    return report


if __name__ == "__main__":
    import json

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("graph_dir", type=Path)
    print(json.dumps(verify(parser.parse_args().graph_dir.resolve()), ensure_ascii=False, indent=2))
