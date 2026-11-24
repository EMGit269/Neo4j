"""Auditable OSM place selection, internal representative points and conservative deduplication."""

import json
import re
import unicodedata
from dataclasses import dataclass

import geopandas as gpd
import numpy as np
import pandas as pd
import shapely

CATEGORY_LABELS = {
    "food": "餐饮",
    "retail": "零售商业",
    "education": "教育科研",
    "health": "医疗健康",
    "culture_tourism": "文化与旅游",
    "leisure_sport": "休闲体育",
    "lodging": "住宿",
    "public_services": "公共与社会服务",
    "finance": "金融服务",
    "transport": "交通服务",
    "life_services": "生活服务",
    "religion": "宗教与殡葬",
    "employment": "办公与生产",
    "municipal": "市政设施",
    "landscape": "自然景观",
    "organisation": "社会组织",
}
AMENITIES = {
    "food": "restaurant cafe fast_food bar pub ice_cream food_court biergarten canteen juice_bar",
    "education": (
        "school kindergarten university college language_school "
        "music_school driving_school training childcare research_institute"
        " prep_school"
    ),
    "health": "hospital clinic doctors dentist pharmacy nursing_home blood_donation veterinary",
    "culture_tourism": (
        "library theatre cinema arts_centre conference_centre music_venue "
        "events_venue exhibition_centre planetarium studio"
    ),
    "public_services": (
        "townhall courthouse police fire_station prison post_office embassy "
        "social_centre social_facility community_centre public_building "
        "shelter_for_homeless"
    ),
    "finance": "bank atm bureau_de_change money_transfer payment_terminal",
    "transport": (
        "bus_station ferry_terminal taxi parking motorcycle_parking "
        "car_rental car_sharing bicycle_rental boat_rental "
        "charging_station fuel car_wash vehicle_inspection compressed_air "
        "driver_training"
    ),
    "life_services": (
        "toilets public_bath internet_cafe animal_shelter animal_boarding "
        "pet_grooming laundry funeral_hall crematorium post_depot parcel_locker"
    ),
    "religion": "place_of_worship monastery grave_yard mortuary",
    "retail": "marketplace market shopping_centre",
    "leisure_sport": "gym dojo dive_centre gambling casino bowling_alley swimming_pool",
    "municipal": "waste_transfer_station recycling_centre wastewater_plant water_works",
}
AMENITY_CATEGORY = {
    value: category for category, values in AMENITIES.items() for value in values.split()
}
INACTIVE = {
    "no",
    "none",
    "vacant",
    "empty",
    "disused",
    "abandoned",
    "construction",
    "proposed",
    "demolished",
    "razed",
}
LODGING = set(
    (
        "hotel motel hostel guest_house apartment chalet camp_site "
        "caravan_site wilderness_hut alpine_hut resort"
    ).split()
)
SPORT = set(
    (
        "sports_centre sports_hall stadium pitch track swimming_pool "
        "fitness_centre fitness_station playground bowling_alley ice_rink "
        "golf_course miniature_golf water_park horse_riding dog_park dance"
        " disc_golf recreation_ground"
    ).split()
)
STATION = {"station", "halt", "tram_stop", "subway_entrance", "platform"}
TOURISM = set(
    "museum gallery attraction aquarium zoo theme_park viewpoint artwork picnic_site yes".split()
)
FIRE = {
    "fire_hydrant",
    "fire_extinguisher",
    "fire_hose",
    "fire_water_pond",
    "fire_service_inlet",
    "fire_alarm_box",
    "fire_suction_point",
    "dry_riser",
    "fire_station",
}
LIFECYCLE = {"yes", "true", "1"}
META_COLUMNS = {
    "osm_uid",
    "osm_type",
    "osm_id",
    "geometry",
    "version",
    "timestamp",
    "changeset",
    "uid",
    "user",
    "lat",
    "lon",
    "nodes",
    "members",
}
REASON_LABELS = {
    "tree_or_tree_row": "树木或树列",
    "street_lamp": "路灯",
    "fire_facility": "消防设施",
    "inactive_or_proposed": "已停用、废弃或规划中",
    "information_board_or_sign": "导览牌或信息标识",
    "information_type_unspecified": "旅游信息设施类型不明",
    "residential_or_roof_garden": "住宅庭院或屋顶花园",
    "transport_group_not_individual_station": "交通分组对象，不能作为单个站点",
    "unnamed_natural_background": "未命名的自然背景",
    "power_or_telecom_equipment": "电力或通信设备",
    "inactive_function_tag": "非现状有效功能标签",
    "building_type_without_place_function": "仅有建筑类型，未确认场所功能",
    "unclassified_function": "功能类别待核",
    "land_cover_or_background_feature": "土地覆盖或普通背景",
    "mapped_function": "有明确场所或服务功能",
    "empty_or_outside_core": "空几何或不与窗口相交",
    "historical_function_only": "只有历史功能，不能作为现状服务",
}


@dataclass(frozen=True)
class Decision:
    decision: str
    category: str = ""
    subcategory: str = ""
    reason: str = ""
    role: str = "place"


def classify_poi(tags, rules):
    tags = {str(k): str(v).strip() for k, v in tags.items() if v is not None and str(v).strip()}
    low = {key: value.lower() for key, value in tags.items()}
    name = tags.get("name:zh") or tags.get("name") or tags.get("official_name") or ""
    excluded = rules["excluded_values"]
    if low.get("natural") in {"tree", "tree_row"} and low["natural"] in excluded.get("natural", []):
        return Decision("excluded", reason="tree_or_tree_row")
    if any(
        low.get(key) == "street_lamp" and "street_lamp" in excluded.get(key, [])
        for key in ["highway", "man_made"]
    ):
        return Decision("excluded", reason="street_lamp")
    if any(
        low.get(key) in FIRE and low.get(key) in excluded.get(key, [])
        for key in ["emergency", "amenity", "building"]
    ):
        return Decision("excluded", reason="fire_facility")
    # Historic ruins remain a mapped heritage site, even though no longer an operating service.
    inactive = any(
        low.get(k) in LIFECYCLE
        for k in ["disused", "abandoned", "demolished", "razed", "construction", "proposed"]
    )
    if inactive and not low.get("historic") and low.get("tourism") not in TOURISM:
        return Decision("excluded", reason="inactive_or_proposed")
    if not low.get("amenity") and not low.get("leisure") and low.get("was:leisure"):
        return Decision("excluded", reason="historical_function_only")

    def include(category, key, role="place"):
        return Decision("included", category, f"{key}={low[key]}", "mapped_function", role)

    amenity = low.get("amenity", "")
    if amenity in AMENITY_CATEGORY:
        role = (
            "service_point"
            if amenity
            in {
                "atm",
                "toilets",
                "charging_station",
                "bicycle_rental",
                "taxi",
                "compressed_air",
                "parcel_locker",
            }
            else "place"
        )
        return include(AMENITY_CATEGORY[amenity], "amenity", role)
    if low.get("healthcare") and low["healthcare"] not in INACTIVE:
        return include("health", "healthcare")
    tourism = low.get("tourism", "")
    if tourism in LODGING:
        return include("lodging", "tourism")
    if tourism in TOURISM:
        return include("culture_tourism", "tourism")
    if tourism == "information":
        if low.get("information") in rules["tourist_information_kept"]:
            return include("culture_tourism", "tourism", "visitor_service")
        if low.get("information"):
            return Decision("excluded", reason="information_board_or_sign")
        return Decision("review", reason="information_type_unspecified")
    for key, category in [
        ("shop", "retail"),
        ("office", "employment"),
        ("craft", "life_services"),
        ("government", "public_services"),
        ("club", "organisation"),
    ]:
        if low.get(key) and low[key] not in INACTIVE:
            return include(category, key)
    if low.get("railway") in STATION:
        role = (
            "station_entrance"
            if low["railway"] == "subway_entrance"
            else "platform"
            if low["railway"] == "platform"
            else "station"
        )
        return include("transport", "railway", role)
    if low.get("highway") in {"bus_stop", "rest_area", "services"}:
        return include("transport", "highway", "stop" if low["highway"] == "bus_stop" else "place")
    if low.get("public_transport") in {"platform", "stop_position", "station"}:
        return include("transport", "public_transport", low["public_transport"])
    if low.get("public_transport") in {"stop_area", "stop_area_group"}:
        return Decision("context", reason="transport_group_not_individual_station", role="group")
    if low.get("aeroway") in {"aerodrome", "terminal", "helipad"}:
        return include("transport", "aeroway")
    if low.get("leisure") in SPORT:
        return include("leisure_sport", "leisure", "sports_facility")
    if low.get("leisure") in {
        "park",
        "garden",
        "nature_reserve",
        "common",
        "beach_resort",
        "marina",
        "amusement_arcade",
        "escape_game",
        "hackerspace",
        "adult_gaming_centre",
    }:
        if low.get("leisure") == "garden" and low.get("garden:type") in {
            "residential",
            "roof_garden",
        }:
            return Decision("context", reason="residential_or_roof_garden")
        return include("leisure_sport", "leisure", "landscape_site")
    if low.get("historic") and low["historic"] not in {"no", "boundary_stone", "milestone"}:
        return include("culture_tourism", "historic", "heritage_site")
    if low.get("man_made") in {
        "works",
        "wastewater_plant",
        "water_works",
        "observatory",
        "lighthouse",
        "watermill",
        "windmill",
    }:
        category = (
            "employment"
            if low["man_made"] == "works"
            else "culture_tourism"
            if low["man_made"] in {"observatory", "lighthouse", "watermill", "windmill"}
            else "municipal"
        )
        return include(category, "man_made")
    if low.get("industrial") and low["industrial"] not in INACTIVE:
        return include("employment", "industrial", "industrial_site")
    if low.get("natural") in rules["named_natural_kept"]:
        return (
            include("landscape", "natural", "natural_landmark")
            if name
            else Decision("context", reason="unnamed_natural_background")
        )
    if low.get("place") == "square":
        return include("leisure_sport", "place", "public_space")
    if low.get("landuse") == "cemetery":
        return include("religion", "landuse", "cemetery")
    if low.get("landuse") in {"recreation_ground", "allotments"}:
        return include("leisure_sport", "landuse", "recreation_site")
    if low.get("boundary") in {"national_park", "protected_area"} and name:
        return include("landscape", "boundary", "protected_site")
    for key, values in rules["excluded_values"].items():
        if low.get(key) in values:
            return Decision("excluded", reason=f"small_equipment_or_non_poi:{key}={low[key]}")
    if any(key in low for key in rules["excluded_keys_without_place_function"]):
        return Decision("excluded", reason="power_or_telecom_equipment")
    if any(
        low.get(k) in INACTIVE for k in ["amenity", "shop", "leisure", "tourism", "office", "craft"]
    ):
        return Decision("excluded", reason="inactive_function_tag")
    if low.get("building") and low["building"] not in {"yes", "no"}:
        return Decision("review", reason="building_type_without_place_function")
    if (
        low.get("amenity")
        or low.get("leisure")
        or low.get("tourism")
        or low.get("emergency")
        or low.get("sport")
    ):
        return (
            Decision("excluded", reason="inactive_function_tag")
            if any(low.get(k) in INACTIVE for k in ["amenity", "shop", "leisure", "tourism"])
            else Decision("review", reason="unclassified_function")
        )
    if any(key.startswith(("disused:", "abandoned:")) for key in low):
        return Decision("excluded", reason="inactive_function_tag")
    return Decision("context", reason="land_cover_or_background_feature")


def normalized_name(value):
    return re.sub(r"[\W_]+", "", unicodedata.normalize("NFKC", str(value or "")).casefold())


def deduplicate_places(features):
    """Merge matching point/area labels and near-identical areas, preserving provenance."""
    frame = features.copy().reset_index(drop=True)
    parents = list(range(len(frame)))
    evidence = []

    def find(i):
        while parents[i] != i:
            parents[i] = parents[parents[i]]
            i = parents[i]
        return i

    def union(i, j, reason):
        a, b = find(i), find(j)
        if a != b:
            parents[b] = a
            evidence.append((i, j, reason))

    def same_identity(a, b):
        if a.category_code != b.category_code:
            return False
        if a.poi_role != b.poi_role:
            return False
        wikidata = bool(a.wikidata and b.wikidata and a.wikidata == b.wikidata)
        same_name = bool(a.normalized_name and a.normalized_name == b.normalized_name)
        return wikidata or (same_name and a.subcategory == b.subcategory)

    frame["normalized_name"] = frame["name"].map(normalized_name)
    areas = frame[frame.geom_type.isin(["Polygon", "MultiPolygon"])]
    for i, row in areas.iterrows():
        for j in areas.sindex.query(row.geometry, predicate="intersects"):
            actual = int(areas.index[j])
            if actual <= i or not same_identity(row, frame.loc[actual]):
                continue
            other = frame.geometry.iloc[actual]
            union_area = row.geometry.union(other).area
            if union_area and row.geometry.intersection(other).area / union_area >= 0.98:
                union(i, actual, "same_identity_area_iou_at_least_0.98")
    frame["ambiguous_duplicate_review"] = False
    for i, row in frame[frame.geom_type == "Point"].iterrows():
        matches = []
        for j in areas.sindex.query(row.geometry, predicate="intersects"):
            actual = int(areas.index[j])
            if same_identity(row, frame.loc[actual]) and frame.geometry.iloc[actual].covers(
                row.geometry
            ):
                matches.append(actual)
        roots = {find(j) for j in matches}
        if len(roots) == 1:
            union(matches[0], i, "same_identity_point_in_area")
        elif len(roots) > 1:
            frame.loc[[i, *matches], "ambiguous_duplicate_review"] = True
    groups = {}
    for i in range(len(frame)):
        groups.setdefault(find(i), []).append(i)
    frame["duplicate_of"] = pd.Series([None] * len(frame), dtype=object)
    frame["member_osm_uids"] = ""
    frame["member_count"] = 1
    frame["deduplication_evidence"] = ""
    for indices in groups.values():
        main = min(
            indices,
            key=lambda i: (
                0 if frame.geom_type.iloc[i] in {"Polygon", "MultiPolygon"} else 1,
                {"relation": 0, "way": 1, "node": 2}.get(frame.osm_type.iloc[i], 3),
                frame.osm_uid.iloc[i],
            ),
        )
        uid = frame.osm_uid.iloc[main]
        ids = sorted(frame.osm_uid.iloc[indices].tolist())
        frame.loc[main, "member_osm_uids"] = json.dumps(ids, ensure_ascii=False)
        frame.loc[main, "member_count"] = len(ids)
        if len(indices) > 1:
            reasons = sorted({reason for i, j, reason in evidence if find(i) == find(main)})
            frame.loc[indices, "deduplication_evidence"] = "|".join(reasons)
        for i in indices:
            if i != main:
                frame.loc[i, "duplicate_of"] = uid
    return frame


def filter_features(source, area_metric, rules):
    frame = source.to_crs(area_metric.crs).copy().reset_index(drop=True)
    if not frame.osm_uid.is_unique:
        raise ValueError("Duplicate OSM source identities.")
    boundary = area_metric.geometry.union_all()
    rows = []
    for row in frame.to_dict("records"):
        tags = {
            key: value
            for key, value in row.items()
            if key not in META_COLUMNS
            and value is not None
            and not (isinstance(value, float) and np.isnan(value))
        }
        result = classify_poi(tags, rules)
        rows.append(
            {
                "osm_uid": row["osm_uid"],
                "osm_type": row["osm_type"],
                "osm_id": row["osm_id"],
                "name": tags.get("name:zh") or tags.get("name") or tags.get("official_name") or "",
                "decision": result.decision,
                "category_code": result.category,
                "category": CATEGORY_LABELS.get(result.category, ""),
                "subcategory": result.subcategory,
                "filter_reason": result.reason,
                "poi_role": result.role,
                "wikidata": tags.get("wikidata") or "",
                "access_raw": tags.get("access") or "",
                "opening_hours": tags.get("opening_hours") or "",
                "tags_json": json.dumps(tags, ensure_ascii=False, sort_keys=True),
                "geometry": row["geometry"],
                "source_geometry_type": row["geometry"].geom_type,
                "osm_edit_timestamp": row.get("timestamp"),
            }
        )
    result = gpd.GeoDataFrame(rows, crs=area_metric.crs)
    result["geometry_repaired_in_filter"] = ~result.is_valid
    result.geometry = shapely.make_valid(result.geometry)
    result["intersects_core"] = result.intersects(boundary)
    for i in result.index[~result.intersects_core | result.is_empty]:
        result.loc[i, ["decision", "filter_reason"]] = ["excluded", "empty_or_outside_core"]
    result["crosses_core_boundary"] = ~result.geometry.covered_by(boundary.buffer(1e-6))
    result["source_area_m2"] = np.where(
        result.geom_type.isin(["Polygon", "MultiPolygon"]), result.area, 0
    )
    result["filter_reason_zh"] = result.filter_reason.map(
        lambda reason: (
            REASON_LABELS.get(reason)
            or (
                "小型设备或非场所：" + reason.split(":", 1)[1]
                if reason.startswith("small_equipment_or_non_poi:")
                else reason
            )
        )
    )
    included = deduplicate_places(result[result.decision == "included"])
    canonical = included[included.duplicate_of.isna()].copy().reset_index(drop=True)
    points = canonical.copy()
    points.geometry = points.geometry.intersection(boundary).representative_point()
    points["point_method"] = np.where(
        points.source_geometry_type == "Point",
        "original_osm_point",
        "representative_point_of_geometry_inside_core",
    )
    geo = points.to_crs(4326)
    points["longitude"] = geo.geometry.x
    points["latitude"] = geo.geometry.y
    if not points.geometry.covered_by(boundary.buffer(1e-6)).all() or not points.is_valid.all():
        raise ValueError("POI representative points failed core/geometry validation.")
    return result, included, canonical, points
