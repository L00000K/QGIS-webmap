"""Vector layer → GeoJSON conversion and coordinate iteration."""
import json

from qgis.core import QgsCoordinateTransform, QgsFeatureRequest, QgsProject, QgsWkbTypes

from .compat import _WGS84


def _transform_used_a_fallback(transform) -> bool:
    """Did QGIS reproject with a ballpark operation rather than a real one?

    When PROJ has no proper datum transformation available it falls back to a
    "ballpark" one, which for OSGB36 (EPSG:27700) to WGS84 is out by roughly
    100 m across Britain. Every layer sharing that source CRS shifts together,
    so nothing looks wrong until it is compared against a layer in a different
    CRS — at which point the two disagree by that 100 m.
    """
    for name in ("fallbackOperationOccurred", "isShortCircuited"):
        probe = getattr(transform, name, None)
        if probe is None:
            continue
        try:
            if name == "fallbackOperationOccurred" and probe():
                return True
        except Exception:
            continue
    return False


def _layer_to_geojson(layer, notes=None) -> dict:
    """Reproject and convert vector layer to GeoJSON dict.

    `notes` collects (layer name, reason) for anything that could move the
    data, so the export can report it rather than leaving it to be spotted by
    eye against another layer.
    """
    transform = QgsCoordinateTransform(
        layer.crs(), _WGS84, QgsProject.instance()
    )

    features = []
    failed = 0
    for feat in layer.getFeatures(QgsFeatureRequest()):
        geom = feat.geometry()
        if geom is None or geom.isEmpty():
            props = {k: (str(v) if v is not None else None) for k, v in feat.attributeMap().items()}
            features.append({"type": "Feature", "geometry": None, "properties": props})
            continue

        # A failed transform used to be ignored, which shipped the geometry in
        # its source coordinates — projected metres read as degrees, landing
        # the feature off the map entirely.
        try:
            result = geom.transform(transform)
        except Exception:
            result = -1
        if result:                      # 0 / Success is the only good answer
            failed += 1
            continue
        geom_json = json.loads(geom.asJson())

        props = {}
        fields = layer.fields()
        for i, attr in enumerate(feat.attributes()):
            fname = fields[i].name()
            if attr is None:
                props[fname] = None
            elif isinstance(attr, (int, float, bool)):
                props[fname] = attr
            else:
                props[fname] = str(attr)

        features.append({
            "type": "Feature",
            "geometry": geom_json,
            "properties": props,
        })

    if notes is not None:
        if failed:
            notes.append((layer.name(),
                          "%d feature%s could not be reprojected to WGS-84 and "
                          "were left out of the export." %
                          (failed, "" if failed == 1 else "s")))
        if _transform_used_a_fallback(transform):
            notes.append((layer.name(),
                          "reprojected from %s with a fallback (ballpark) datum "
                          "transformation, which can be ~100 m out. Install the "
                          "grid shift files for this CRS, or set a datum "
                          "transform in Project Properties > CRS, then re-export."
                          % (layer.crs().authid() or "its CRS")))
    return {"type": "FeatureCollection", "features": features}


def _geom_type_str(layer) -> str:
    wkb = layer.wkbType()
    flat = QgsWkbTypes.flatType(wkb)
    if flat in (QgsWkbTypes.Point, QgsWkbTypes.MultiPoint):
        return "point"
    if flat in (QgsWkbTypes.LineString, QgsWkbTypes.MultiLineString):
        return "line"
    return "polygon"


def _flatten_coords(geom):
    """Yield all [x, y] coordinate pairs from a GeoJSON geometry dict."""
    gtype = geom.get("type", "")
    coords = geom.get("coordinates", [])
    if gtype == "Point":
        if coords:
            yield coords
    elif gtype in ("MultiPoint", "LineString"):
        for c in coords:
            yield c
    elif gtype in ("MultiLineString", "Polygon"):
        for ring in coords:
            for c in ring:
                yield c
    elif gtype == "MultiPolygon":
        for poly in coords:
            for ring in poly:
                for c in ring:
                    yield c
    elif gtype == "GeometryCollection":
        for g in geom.get("geometries", []):
            yield from _flatten_coords(g)
