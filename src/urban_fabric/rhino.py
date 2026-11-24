"""Read-only Rhino inspection and conservative local-coordinate mesh extraction.

Mesh components are geometric candidates, not verified individual buildings.
No geographic coordinates, road access, source dates or measured heights are invented.
"""

import hashlib
from collections import Counter
from pathlib import Path

import geopandas as gpd
import momepy
import numpy as np
import shapely
from pyproj import CRS
from scipy.sparse import coo_matrix
from scipy.sparse.csgraph import connected_components
from shapely.geometry import LineString

from .data import utc_now, write_json
from .metrics import _polygon

LOCAL_METRES = CRS.from_wkt(
    'ENGCRS["Rhino local metres",EDATUM["Unreferenced model"],CS[Cartesian,2],'
    'AXIS["x",east,ORDER[1],LENGTHUNIT["metre",1]],'
    'AXIS["y",north,ORDER[2],LENGTHUNIT["metre",1]]]'
)


def mesh_arrays(mesh):
    """Extract exact float coordinates and triangle/quad vertex indices once."""
    vertices = np.fromiter(
        (value for p in mesh.Vertices for value in (p.X, p.Y, p.Z)),
        dtype=np.float64,
        count=len(mesh.Vertices) * 3,
    ).reshape(-1, 3)
    faces = np.fromiter(
        (value for face in mesh.Faces for value in face),
        dtype=np.int32,
        count=len(mesh.Faces) * 4,
    ).reshape(-1, 4)
    return vertices, faces


def face_components(vertices, faces):
    """Join faces only along exact shared 3-D edges, including unwelded vertices.

    XY proximity, shared projected points and mere corner contacts never join solids.
    Full coincident edges between touching solids can still merge candidates.
    """
    if not len(faces) or not len(vertices):
        return np.zeros(0, dtype=np.int32), {"components": 0, "unique_vertices": 0}
    if not np.isfinite(vertices).all() or faces.min() < 0 or faces.max() >= len(vertices):
        raise ValueError("Invalid mesh coordinates or vertex indices.")
    unique, inverse = np.unique(vertices, axis=0, return_inverse=True)
    welded = inverse[faces]
    first = np.concatenate([welded[:, i] for i in range(4)])
    second = np.concatenate([welded[:, (i + 1) % 4] for i in range(4)])
    owner = np.tile(np.arange(len(faces), dtype=np.int32), 4)
    good = first != second
    first, second, owner = first[good], second[good], owner[good]
    low, high = np.minimum(first, second), np.maximum(first, second)
    keys = low.astype(np.uint64) * np.uint64(len(unique)) + high.astype(np.uint64)
    order = np.argsort(keys, kind="stable")
    ordered_keys, ordered_owner = keys[order], owner[order]
    same = ordered_keys[1:] == ordered_keys[:-1]
    rows, columns = ordered_owner[:-1][same], ordered_owner[1:][same]
    adjacency = coo_matrix(
        (np.ones(len(rows), dtype=np.uint8), (rows, columns)), shape=(len(faces), len(faces))
    ).tocsr()
    count, labels = connected_components(adjacency, directed=False)
    _, incidence = np.unique(ordered_keys, return_counts=True)
    nonmanifold_ids = np.unique(labels[ordered_owner[np.repeat(incidence > 2, incidence)]])
    return labels, {
        "components": int(count),
        "unique_vertices": len(unique),
        "edge_incidence_one": int((incidence == 1).sum()),
        "edge_incidence_over_two": int((incidence > 2).sum()),
        "components_with_nonmanifold_edges": len(nonmanifold_ids),
        "_nonmanifold_component_ids": nonmanifold_ids.tolist(),
        "component_basis": "Exact XYZ shared full edge, no proximity snapping",
    }


def component_footprints(vertices, faces, labels, mesh_id, tolerance=0.0001):
    """Union lowest horizontal faces; keep true outlines/holes, never bounding rectangles."""
    if not len(labels):
        return [], [{"mesh_id": mesh_id, "reason": "empty_mesh"}]
    count = int(labels.max()) + 1
    elevations = vertices[:, 2][faces]
    face_min, face_max = elevations.min(axis=1), elevations.max(axis=1)
    base = np.full(count, np.inf)
    top = np.full(count, -np.inf)
    np.minimum.at(base, labels, face_min)
    np.maximum.at(top, labels, face_max)
    face_counts = np.bincount(labels, minlength=count)
    at_base = (face_max - face_min <= tolerance) & (np.abs(face_min - base[labels]) <= tolerance)
    horizontal = face_max - face_min <= tolerance
    levels = np.unique(np.column_stack([labels[horizontal], face_min[horizontal]]), axis=0)
    level_counts = np.bincount(levels[:, 0].astype(int), minlength=count)
    indices = np.flatnonzero(at_base)
    polygons = shapely.polygons(vertices[faces[indices]][:, :, :2])
    nonempty = ~shapely.is_empty(polygons) & (shapely.area(polygons) > 0)
    indices, polygons = indices[nonempty], polygons[nonempty]
    order = np.argsort(labels[indices], kind="stable")
    grouped, polygons = labels[indices][order], polygons[order]
    starts = np.flatnonzero(np.r_[True, grouped[1:] != grouped[:-1]]) if len(grouped) else []
    ends = np.r_[starts[1:], len(grouped)] if len(grouped) else []
    found, rows, reviews = set(), [], []
    for start, end in zip(starts, ends, strict=True):
        component = int(grouped[start])
        found.add(component)
        parts = polygons[start:end]
        repaired = bool((~shapely.is_valid(parts)).any())
        if repaired:
            parts = shapely.make_valid(parts)
        geom = _polygon(shapely.union_all(parts))
        if geom is None or geom.is_empty or geom.area <= 0:
            reviews.append(
                {"mesh_id": mesh_id, "component": component, "reason": "no_positive_polygon_base"}
            )
            continue
        height = float(top[component] - base[component])
        if height <= tolerance:
            reviews.append(
                {
                    "mesh_id": mesh_id,
                    "component": component,
                    "reason": "no_positive_vertical_extent",
                }
            )
            continue
        rows.append(
            {
                "id": f"rhino:{mesh_id}:component:{component}",
                "source_mesh_id": mesh_id,
                "component_id": component,
                "source": "Rhino local mesh",
                "geometry": geom,
                "base_z_m": float(base[component]),
                "top_z_m": float(top[component]),
                "height_extent_m": height,
                "height_source": "Model XYZ vertical extent; source accuracy unknown",
                "height_is_survey_verified": False,
                "is_verified_individual_building": False,
                "mesh_face_count": int(face_counts[component]),
                "base_face_count": int(end - start),
                "horizontal_level_count": int(level_counts[component]),
                "multiple_horizontal_levels": bool(level_counts[component] > 2),
                "multipart_base": geom.geom_type == "MultiPolygon",
                "geometry_repaired": repaired,
            }
        )
    for component in set(range(count)) - found:
        reviews.append(
            {
                "mesh_id": mesh_id,
                "component": component,
                "reason": "no_horizontal_base; projection_requires_review",
            }
        )
    return rows, reviews


def audit_footprint_overlaps(buildings):
    """Flag substantive overlap; preserve narrow/small forms and all original candidates."""
    left, right = buildings.sindex.query(buildings.geometry, predicate="intersects")
    left, right = left[left < right], right[left < right]
    areas = shapely.area(
        shapely.intersection(buildings.geometry.array[left], buildings.geometry.array[right])
    )
    minimum = np.minimum(buildings.area_m2.to_numpy()[left], buildings.area_m2.to_numpy()[right])
    selected = areas > np.maximum(1.0, 0.02 * minimum)
    left, right, areas, minimum = (
        left[selected],
        right[selected],
        areas[selected],
        minimum[selected],
    )
    marked = np.unique(np.r_[left, right])
    buildings["footprint_overlap_review"] = False
    buildings.loc[buildings.index[marked], "footprint_overlap_review"] = True
    return [
        {
            "a": buildings.id.iloc[i],
            "b": buildings.id.iloc[j],
            "overlap_m2": float(area),
            "fraction_of_smaller": float(area / smaller),
        }
        for i, j, area, smaller in zip(left, right, areas, minimum, strict=True)
    ]


def inspect_model(path, output, *, extract=True, progress=print):
    import rhino3dm as rhino

    path, output = Path(path).resolve(), Path(output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    model = rhino.File3dm.Read(str(path))
    if model is None:
        raise ValueError("Unable to read the Rhino model.")
    if model.Settings.ModelUnitSystem != rhino.UnitSystem.Meters:
        raise ValueError("Current extractor requires declared metre units; review scale first.")
    definition_ids = {str(i) for d in model.InstanceDefinitions for i in d.GetObjectIds()}
    layer_counts = Counter(obj.Attributes.LayerIndex for obj in model.Objects)
    main_counts = Counter(
        obj.Attributes.LayerIndex
        for obj in model.Objects
        if str(obj.Attributes.Id) not in definition_ids
    )
    layer_rows, catalogue = [], []
    for layer in model.Layers:
        layer_rows.append(
            {
                "index": layer.Index,
                "name": layer.FullPath,
                "visible_own_setting": layer.Visible,
                "object_count": layer_counts[layer.Index],
                "nondefinition_object_count": main_counts[layer.Index],
            }
        )
    bounds_by_layer = {}
    meshes = []
    curve_rows = []
    roles = {
        "raod": "road_linework",
        "water": "water_linework",
        "natrue": "green_area_linework",
        "railway": "railway_linework",
    }
    for obj in model.Objects:
        geometry, attributes = obj.Geometry, obj.Attributes
        identifier = str(attributes.Id)
        layer = model.Layers[attributes.LayerIndex]
        in_definition = identifier in definition_ids
        bounds = geometry.GetBoundingBox()
        values = [
            bounds.Min.X,
            bounds.Min.Y,
            bounds.Min.Z,
            bounds.Max.X,
            bounds.Max.Y,
            bounds.Max.Z,
        ]
        if np.isfinite(values).all():
            previous = bounds_by_layer.setdefault(str(layer.Index), values.copy())
            for i in range(6):
                previous[i] = min(previous[i], values[i]) if i < 3 else max(previous[i], values[i])
        catalogue.append(
            {
                "id": identifier,
                "geometry_type": type(geometry).__name__,
                "layer_index": attributes.LayerIndex,
                "layer_name": layer.FullPath,
                "is_definition_geometry": in_definition,
                "bounds_xyz": values,
            }
        )
        if in_definition:
            continue
        if isinstance(geometry, rhino.Mesh) and "建筑" in layer.FullPath:
            meshes.append((identifier, geometry))
        leaf = layer.FullPath.split("::")[-1]
        if extract and leaf in roles and isinstance(geometry, rhino.PolylineCurve):
            points = geometry.TryGetPolyline()
            coordinates = [(p.X, p.Y, p.Z) for p in points] if points is not None else []
            if len(coordinates) >= 2:
                curve_rows.append(
                    {
                        "id": f"rhino:{identifier}",
                        "source_object_id": identifier,
                        "layer_name": layer.FullPath,
                        "role": roles[leaf],
                        "is_closed": bool(geometry.IsClosed),
                        "geometry": LineString(coordinates),
                    }
                )
    anchor = model.Settings.EarthAnchorPoint
    report = {
        "inspected_at_utc": utc_now(),
        "source_file": str(path),
        "source_bytes": path.stat().st_size,
        "source_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "rhino3dm_version": rhino.__version__,
        "source_archive_version": model.ArchiveVersion,
        "declared_units": "metres",
        "absolute_tolerance_model_units": model.Settings.ModelAbsoluteTolerance,
        "file_created": str(model.Created),
        "file_last_edited": str(model.LastEdited),
        "data_capture_date_known": False,
        "source_application": model.ApplicationName,
        "model_objects": len(model.Objects),
        "layers": layer_rows,
        "layer_bounds_xyz": bounds_by_layer,
        "geometry_types": dict(Counter(type(obj.Geometry).__name__ for obj in model.Objects)),
        "definition_geometry_objects": len(definition_ids),
        "instance_definitions": len(model.InstanceDefinitions),
        "earth_anchor": {
            "latitude": anchor.EarthBasepointLatitude,
            "longitude": anchor.EarthBasepointLongitude,
            "elevation": anchor.EarthBasepointElevation,
        },
        "georeferencing_verified": False,
        "coordinate_semantics": "Unreferenced local engineering coordinates; no EPSG inferred",
        "individual_building_ids_present": False,
        "mesh_candidates": [],
    }
    write_json(output / "object_catalogue.json", catalogue)
    candidate_rows, review_rows = [], []
    if extract:
        for identifier, mesh in meshes:
            progress(
                f"Extracting mesh {identifier}: {len(mesh.Vertices):,} vertices, "
                f"{len(mesh.Faces):,} faces"
            )
            vertices, faces = mesh_arrays(mesh)
            labels, connectivity = face_components(vertices, faces)
            nonmanifold_components = set(connectivity.pop("_nonmanifold_component_ids", []))
            progress(
                f"Shared-edge components: {connectivity['components']:,}; extracting lowest faces"
            )
            rows, reviews = component_footprints(
                vertices, faces, labels, identifier, tolerance=model.Settings.ModelAbsoluteTolerance
            )
            for row in rows:
                row["mesh_nonmanifold_edge_review"] = row["component_id"] in nonmanifold_components
                row["segmentation_requires_review"] = (
                    row["multipart_base"]
                    or row["multiple_horizontal_levels"]
                    or row["mesh_nonmanifold_edge_review"]
                )
            candidate_rows.extend(rows)
            review_rows.extend(reviews)
            report["mesh_candidates"].append(
                {
                    "object_id": identifier,
                    "vertices": len(vertices),
                    "faces": len(faces),
                    "source_mesh_is_valid": mesh.IsValid,
                    "source_mesh_is_closed": mesh.IsClosed,
                    "source_bounds_xyz": [float(vertices[:, i].min()) for i in range(3)]
                    + [float(vertices[:, i].max()) for i in range(3)],
                    "connectivity": connectivity,
                    "extracted_candidates": len(rows),
                    "excluded_components": len(reviews),
                }
            )
            del vertices, faces, labels
        if candidate_rows:
            buildings = gpd.GeoDataFrame(candidate_rows, crs=LOCAL_METRES)
            buildings["area_m2"] = buildings.area
            buildings["perimeter_m"] = buildings.length
            buildings["circular_compactness"] = momepy.circular_compactness(buildings.geometry)
            buildings["elongation"] = momepy.elongation(buildings.geometry)
            buildings["rectangularity"] = momepy.rectangularity(buildings.geometry)
            buildings["estimated_prism_volume_m3"] = buildings.area_m2 * buildings.height_extent_m
            buildings["exact_duplicate_footprint"] = buildings.geometry.map(
                lambda geom: shapely.to_wkb(shapely.normalize(geom), hex=True)
            ).duplicated(keep=False)
            overlaps = audit_footprint_overlaps(buildings)
            buildings["footprint_requires_review"] = (
                buildings.segmentation_requires_review
                | buildings.footprint_overlap_review
                | buildings.geometry_repaired
                | buildings.exact_duplicate_footprint
            )
            write_json(output / "footprint_overlap_reviews.json", overlaps)
            buildings.to_parquet(output / "building_candidates.parquet", index=False)
            buildings.drop(columns="geometry").to_csv(
                output / "building_candidate_metrics.csv", index=False
            )
            height = buildings.height_extent_m
            report["extracted"] = {
                "building_candidates": len(buildings),
                "valid_polygon_geometries": bool(buildings.is_valid.all()),
                "positive_footprint_areas": bool((buildings.area_m2 > 0).all()),
                "positive_vertical_extents": bool((height > 0).all()),
                "unique_ids": bool(buildings.id.is_unique),
                "height_extent_quantiles_m": {
                    str(p): float(height.quantile(p)) for p in [0, 0.25, 0.5, 0.75, 0.95, 1]
                },
                "top_height_extent_frequencies_m": {
                    str(k): int(v) for k, v in height.value_counts().head(15).items()
                },
                "multiple_horizontal_level_candidates": int(
                    buildings.multiple_horizontal_levels.sum()
                ),
                "mesh_nonmanifold_edge_review_candidates": int(
                    buildings.mesh_nonmanifold_edge_review.sum()
                ),
                "segmentation_review_candidates": int(buildings.segmentation_requires_review.sum()),
                "all_bases_at_model_zero": bool(np.isclose(buildings.base_z_m, 0).all()),
                "all_heights_multiples_of_3_m": bool(
                    np.isclose(height / 3, np.round(height / 3)).all()
                ),
                "multipart_base_candidates": int(buildings.multipart_base.sum()),
                "repaired_base_candidates": int(buildings.geometry_repaired.sum()),
                "exact_duplicate_footprint_candidates": int(
                    buildings.exact_duplicate_footprint.sum()
                ),
                "excluded_mesh_components": len(review_rows),
                "substantial_overlap_pairs": len(overlaps),
                "overlap_review_candidates": int(buildings.footprint_overlap_review.sum()),
                "footprint_review_candidates": int(buildings.footprint_requires_review.sum()),
                "unit_scale_verified_against_real_world": False,
            }
        if curve_rows:
            curves = gpd.GeoDataFrame(curve_rows, crs=LOCAL_METRES)
            curves["length_model_m"] = curves.length
            curves.to_parquet(output / "context_linework.parquet", index=False)
            report["linework_by_role"] = dict(Counter(curves.role))
            report["closed_linework_by_role"] = dict(Counter(curves.loc[curves.is_closed, "role"]))
            report["road_network_topology_verified"] = False
    write_json(output / "mesh_component_reviews.json", review_rows)
    report["limitations"] = [
        "File name suggests Suzhou; actual location and source require verification.",
        "Model save dates are not capture dates; Earth anchor 0/0 does not locate the city.",
        "Shared-edge components can combine touching buildings or split their parts.",
        "Lowest horizontal surface is a base hypothesis, not a surveyed ground-contact footprint.",
        "Vertical extents and metre scale are not surveyed or independently validated.",
        "Road lines may be outlines, medians or CAD details; routable topology is unverified.",
        "Prism volume = base area times vertical extent; it is not measured closed-mesh volume.",
    ]
    write_json(output / "report.json", report)
    return report


def export_local_geopackage(output, *, name="suzhou_model_local.gpkg"):
    """Portable GIS layers with explicit engineering CRS, without invented lat/lon."""
    output = Path(output)
    destination = output / name
    import uuid

    temporary = output / f".{destination.stem}-{uuid.uuid4().hex}.gpkg"
    buildings = gpd.read_parquet(output / "building_candidates.parquet")
    lines = gpd.read_parquet(output / "context_linework.parquet")
    buildings.to_file(temporary, layer="building_candidates", driver="GPKG")
    for role in sorted(lines.role.unique()):
        lines[lines.role == role].to_file(temporary, layer=role, driver="GPKG", mode="a")
    recovered = gpd.read_file(temporary, layer="building_candidates")
    if not (
        len(recovered) == len(buildings)
        and recovered.crs.is_engineering
        and recovered.id.tolist() == buildings.id.tolist()
        and np.allclose(recovered.area, buildings.area, rtol=1e-10, atol=1e-8)
    ):
        raise ValueError("GeoPackage local-coordinate roundtrip failed.")
    temporary.replace(destination)
    return {
        "path": str(destination),
        "roundtrip_passed": True,
        "building_candidates": len(buildings),
        "layers": 1 + lines.role.nunique(),
    }
