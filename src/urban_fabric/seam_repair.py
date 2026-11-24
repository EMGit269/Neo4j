"""Conservative, traceable footprint repair along repeated source subdivision seams.

Native XY coordinates supply subdivision evidence, not physical measurements.
Only accepted groups are snapped; metric geometry/measurements are exported separately.
"""

import hashlib
import json
from dataclasses import dataclass

import geopandas as gpd
import networkx as nx
import numpy as np
import pandas as pd
import shapely


@dataclass(frozen=True)
class SeamParameters:
    neighbor_tolerance: float = 0.08
    precision_grid: float = 0.05
    minimum_seam_length: float = 1.5
    maximum_cross_axis_spread: float = 0.20
    grid_cluster_gap: float = 5.0
    grid_minimum_pairs: int = 5
    grid_minimum_spatial_span: float = 80.0
    endpoint_tolerance: float = 0.16
    maximum_outer_edge_turn_degrees: float = 15.0
    maximum_area_change_ratio: float = 0.002
    maximum_boundary_displacement: float = 0.12
    maximum_group_members: int = 16
    maximum_iterations: int = 4

    def __post_init__(self):
        if any(not np.isfinite(v) or v <= 0 for v in vars(self).values()):
            raise ValueError("Seam parameters must be positive and finite.")


PAIR_COLUMNS = [
    "left",
    "right",
    "axis",
    "coordinate",
    "along_coordinate",
    "seam_length",
    "maximum_outer_turn",
    "minimum_outer_turn",
    "union_convexity",
    "seam_wkt",
]


def exterior_vector(polygon, point, axis, tolerance):
    if polygon.geom_type != "Polygon":
        return None
    coords = shapely.get_coordinates(polygon.exterior)[:-1]
    distances = np.linalg.norm(coords - point, axis=1)
    vertex = int(distances.argmin())
    if distances[vertex] > tolerance:
        return None
    vectors = [
        coords[(vertex - 1) % len(coords)] - coords[vertex],
        coords[(vertex + 1) % len(coords)] - coords[vertex],
    ]
    vectors = [v for v in vectors if np.linalg.norm(v) > tolerance / 2]
    if len(vectors) != 2:
        return None
    # The non-seam edge departs across the grid line, the seam edge follows it.
    return max(vectors, key=lambda v: abs(v[0 if axis == "x" else 1]) / np.linalg.norm(v))


def find_seam_candidates(frame, parameters):
    """Temporary precision alignment is evidence only; do not change the source frame."""
    if frame.crs is None or not frame.crs.is_engineering:
        raise ValueError("Grid evidence must come from original engineering XY coordinates.")
    if not frame.index.equals(pd.RangeIndex(len(frame))):
        frame = frame.reset_index(drop=True)
    left, right = frame.sindex.query(
        frame.geometry, predicate="dwithin", distance=parameters.neighbor_tolerance
    )
    keep = left < right
    left, right = left[keep], right[keep]
    aligned = shapely.set_precision(frame.geometry.to_numpy(), parameters.precision_grid)
    common = shapely.intersection(shapely.boundary(aligned[left]), shapely.boundary(aligned[right]))
    lengths = shapely.length(common)
    rows = []
    for a, b, seam, length in zip(left, right, common, lengths, strict=True):
        if length < parameters.minimum_seam_length:
            continue
        coords = shapely.get_coordinates(seam)
        spread = np.ptp(coords, axis=0)
        axis = (
            "x"
            if spread[0] <= parameters.maximum_cross_axis_spread
            else ("y" if spread[1] <= parameters.maximum_cross_axis_spread else "other")
        )
        turns = []
        if axis != "other":
            along_axis = 1 if axis == "x" else 0
            ends = coords[np.argsort(coords[:, along_axis])[[0, -1]]]
            for point in ends:
                v = exterior_vector(aligned[a], point, axis, parameters.endpoint_tolerance)
                w = exterior_vector(aligned[b], point, axis, parameters.endpoint_tolerance)
                if v is None or w is None:
                    turns.append(180.0)
                else:
                    cosine = np.dot(v, w) / np.linalg.norm(v) / np.linalg.norm(w)
                    turns.append(float(180 - np.rad2deg(np.arccos(np.clip(cosine, -1, 1)))))
        else:
            turns = [180.0, 180.0]
        union = shapely.union(aligned[a], aligned[b])
        rows.append(
            {
                "left": int(a),
                "right": int(b),
                "axis": axis,
                "coordinate": float(coords[:, 0 if axis == "x" else 1].mean()),
                "along_coordinate": float(coords[:, 1 if axis == "x" else 0].mean()),
                "seam_length": float(length),
                "maximum_outer_turn": max(turns),
                "minimum_outer_turn": min(turns),
                "union_convexity": float(union.area / union.convex_hull.area),
                "seam_wkt": seam.wkt,
            }
        )
    return pd.DataFrame(rows, columns=PAIR_COLUMNS)


def infer_grid_lines(candidates, parameters):
    """Require repeated spatial support and regular spacing; no grid is assumed a priori."""
    lines = []
    for axis in ["x", "y"]:
        subset = candidates[candidates.axis == axis].sort_values("coordinate")
        if subset.empty:
            continue
        groups = np.split(
            np.arange(len(subset)),
            np.flatnonzero(np.diff(subset.coordinate) > parameters.grid_cluster_gap) + 1,
        )
        supported = []
        for indices in groups:
            group = subset.iloc[indices]
            span = float(group.along_coordinate.max() - group.along_coordinate.min())
            if (
                len(group) < parameters.grid_minimum_pairs
                or span < parameters.grid_minimum_spatial_span
            ):
                continue
            supported.append(
                {
                    "axis": axis,
                    "coordinate": float(group.coordinate.median()),
                    "minimum_coordinate": float(group.coordinate.min()),
                    "maximum_coordinate": float(group.coordinate.max()),
                    "supporting_pairs": len(group),
                    "spatial_span_model_units": span,
                }
            )
        if len(supported) < 3:
            continue
        centers = np.array([row["coordinate"] for row in supported])
        spacing = float(np.median(np.diff(centers)))
        for i, row in enumerate(supported):
            distances = np.abs(centers - centers[i])
            periods = np.maximum(1, np.round(distances / spacing))
            fits = (distances > 0) & (np.abs(distances - periods * spacing) < spacing * 0.06)
            if fits.sum() >= 2:
                row["inferred_spacing_model_units"] = spacing
                lines.append(row)
    return lines


def is_grid_supported(row, grids, parameters):
    return any(
        row.axis == line["axis"]
        and line["minimum_coordinate"] - parameters.grid_cluster_gap
        <= row.coordinate
        <= line["maximum_coordinate"] + parameters.grid_cluster_gap
        for line in grids
    )


def merge_geometry(geometries, parameters):
    original = shapely.union_all(geometries)
    if not original.area:
        return None, "zero_area"
    if original.geom_type == "Polygon":
        # Exact union preserves the footprint perfectly whenever it is already connected.
        return original, "exact_union"

    def acceptable(candidate):
        if candidate.geom_type != "Polygon" or not candidate.is_valid:
            return False
        change = candidate.symmetric_difference(original).area / original.area
        # Internal seam vertices disappearing is not a change in occupied footprint.
        tolerance = parameters.maximum_boundary_displacement
        return (
            change <= parameters.maximum_area_change_ratio
            and original.buffer(tolerance).covers(candidate)
            and candidate.buffer(tolerance).covers(original)
        )

    # Align only near-coincident vertices/edges; preserve distant exterior coordinates.
    snapped = geometries[0]
    try:
        for part in geometries[1:]:
            part = shapely.snap(part, snapped, parameters.neighbor_tolerance)
            snapped = shapely.union(
                shapely.snap(snapped, part, parameters.neighbor_tolerance), part
            )
        if acceptable(snapped):
            return snapped, "mutual_seam_snap_union"
    except shapely.errors.GEOSException:
        pass
    # Bounded fallback when vertex topology cannot be reconciled by snapping.
    merged = shapely.union_all(geometries, grid_size=parameters.precision_grid)
    if merged.geom_type != "Polygon" or not merged.is_valid:
        return None, "remaining_gap_or_multipart"
    if not acceptable(merged):
        return None, "excessive_geometry_change"
    return merged, "local_precision_union"


def repair_seams(source, parameters):
    frame = source.reset_index(drop=True).copy()
    if not frame.id.is_unique or not frame.is_valid.all():
        raise ValueError("Repair needs unique source IDs and valid footprints.")
    originals = frame.geometry.to_numpy().copy()
    source_members = {i: [i] for i in range(len(frame))}
    first_candidates = find_seam_candidates(frame, parameters)
    grids = infer_grid_lines(first_candidates, parameters)
    current = gpd.GeoDataFrame({"work_id": list(source_members)}, geometry=originals, crs=frame.crs)
    events, pair_decisions = [], []
    for iteration in range(parameters.maximum_iterations):
        candidates = (
            first_candidates if iteration == 0 else find_seam_candidates(current, parameters)
        )
        graph = nx.Graph()
        for row in candidates.itertuples():
            left_id = int(current.work_id.iloc[row.left])
            right_id = int(current.work_id.iloc[row.right])
            supported = is_grid_supported(row, grids, parameters)
            continuous = row.maximum_outer_turn <= parameters.maximum_outer_edge_turn_degrees
            decision = "accepted_evidence" if supported and continuous else "review"
            reason = (
                "grid_and_continuous_outline"
                if supported and continuous
                else ("not_repeated_grid_line" if not supported else "outline_discontinuity")
            )
            pair_decisions.append(
                {
                    **row._asdict(),
                    "iteration": iteration,
                    "decision": decision,
                    "reason": reason,
                    "left_source_ids": json.dumps(frame.id.iloc[source_members[left_id]].tolist()),
                    "right_source_ids": json.dumps(
                        frame.id.iloc[source_members[right_id]].tolist()
                    ),
                }
            )
            if supported and continuous:
                graph.add_edge(row.left, row.right)
        successful = []
        for cluster in nx.connected_components(graph):
            positions = sorted(cluster)
            work_ids = current.work_id.iloc[positions].astype(int).tolist()
            members = sorted({j for wid in work_ids for j in source_members[wid]})
            if len(members) > parameters.maximum_group_members:
                events.append(
                    {
                        "iteration": iteration,
                        "decision": "review",
                        "reason": "large_group",
                        "source_indices": members,
                    }
                )
                continue
            geometry, method = merge_geometry(originals[members], parameters)
            if geometry is None:
                events.append(
                    {
                        "iteration": iteration,
                        "decision": "review",
                        "reason": method,
                        "source_indices": members,
                    }
                )
                continue
            wid = min(work_ids)
            source_members[wid] = members
            successful.append((positions, wid, geometry))
            events.append(
                {
                    "iteration": iteration,
                    "decision": "merged",
                    "reason": method,
                    "source_indices": members,
                }
            )
        if not successful:
            break
        remove = {i for positions, _, _ in successful for i in positions}
        records = [
            {"work_id": int(row.work_id), "geometry": row.geometry}
            for i, row in enumerate(current.itertuples())
            if i not in remove
        ]
        records.extend({"work_id": wid, "geometry": geometry} for _, wid, geometry in successful)
        current = (
            gpd.GeoDataFrame(records, crs=frame.crs).sort_values("work_id").reset_index(drop=True)
        )

    membership, rows = [], []
    review_ids = set()
    final_candidates = find_seam_candidates(current, parameters)
    for row in final_candidates.itertuples():
        if is_grid_supported(row, grids, parameters):
            review_ids.update(
                [int(current.work_id.iloc[row.left]), int(current.work_id.iloc[row.right])]
            )
    for row in current.itertuples():
        members = source_members[int(row.work_id)]
        ids = sorted(frame.id.iloc[members].tolist())
        merged = len(members) > 1
        uid = (
            "rhino:seam_merged:" + hashlib.sha256("|".join(ids).encode()).hexdigest()[:20]
            if merged
            else ids[0]
        )
        parts = frame.iloc[members]
        heights = sorted(set(parts.height_extent_m.astype(float)))
        conflict = len(heights) > 1
        record = {
            "id": uid,
            "geometry": row.geometry,
            "source": "Rhino footprints with grid seam repair",
            "source_ids_json": json.dumps(ids),
            "source_component_count": len(members),
            "seam_merged": merged,
            "height_extent_m": heights[0] if not conflict else np.nan,
            "model_height_min_m": min(heights),
            "model_height_max_m": max(heights),
            "model_height_values_json": json.dumps(heights),
            "height_conflict": conflict,
            "height_is_survey_verified": False,
            "is_verified_individual_building": False,
            "remaining_seam_review": int(row.work_id) in review_ids,
            "source_footprint_review": bool(parts.footprint_requires_review.any()),
            "footprint_requires_review": bool(parts.footprint_requires_review.any())
            or merged
            or int(row.work_id) in review_ids,
            "repair_basis": "repeated_grid_and_continuous_outline"
            if merged
            else "original_geometry",
            "repair_entity_verification": "algorithmic_candidate_not_survey_verified",
        }
        rows.append(record)
        for index in members:
            membership.append(
                {
                    "source_id": frame.id.iloc[index],
                    "building_id": uid,
                    "source_component_id": int(frame.component_id.iloc[index]),
                    "source_height_m": float(frame.height_extent_m.iloc[index]),
                    "seam_merged": merged,
                }
            )
    repaired = gpd.GeoDataFrame(rows, crs=frame.crs)
    members = pd.DataFrame(membership).sort_values("source_id").reset_index(drop=True)
    if len(members) != len(frame) or not members.source_id.is_unique:
        raise ValueError("Repair membership did not preserve each source object exactly once.")
    return (
        repaired,
        members,
        pd.DataFrame(pair_decisions),
        {
            "grids": grids,
            "events": events,
            "source_count": len(frame),
            "result_count": len(repaired),
            "merged_groups": int(repaired.seam_merged.sum()),
            "height_conflict_groups": int(repaired.height_conflict.sum()),
        },
    )
