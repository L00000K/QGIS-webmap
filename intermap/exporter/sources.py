"""Parsing QGIS raster data sources that stay remote: WMS/WMTS/XYZ and COGs."""
import re
from urllib.parse import parse_qs
from typing import Optional

from qgis.core import QgsCoordinateTransform, QgsProject

from .compat import _WGS84


# Tile matrix sets a Leaflet map in EPSG:3857 can consume directly. A set in
# any other projection (British National Grid, say) tiles a different grid
# entirely, and no amount of URL building makes those tiles line up.
_MERCATOR_MATRIX_SETS = (
    "googlemapscompatible", "epsg:3857", "epsg:900913", "epsg:102100",
    "webmercatorquad", "web_mercator", "webmercator", "gooogle",  # seen in the wild
)

_XYZ_PLACEHOLDERS = ("{z}", "{x}", "{y}")


def _is_mercator_matrix_set(name: str) -> bool:
    """Can a Web-Mercator Leaflet map use this WMTS tile matrix set?"""
    low = (name or "").strip().lower()
    if not low:
        return False
    return any(token in low for token in _MERCATOR_MATRIX_SETS)


def _wmts_kvp_base(url: str) -> str:
    """Turn a WMTS capabilities URL into the endpoint a KVP GetTile goes to.

    Handles the two shapes QGIS records:
        .../service/wmts?SERVICE=WMTS&REQUEST=GetCapabilities  → .../service/wmts
        .../MapServer/WMTS/1.0.0/WMTSCapabilities.xml          → .../MapServer/WMTS
    """
    base = (url or "").split("?")[0].rstrip("/")
    low = base.lower()
    if low.endswith("/wmtscapabilities.xml"):
        base = base[: -len("/WMTSCapabilities.xml")]
        if base.lower().endswith("/1.0.0"):
            base = base[: -len("/1.0.0")]
    return base


def _has_xyz_template(url: str) -> bool:
    low = (url or "").lower()
    return all(ph in low for ph in _XYZ_PLACEHOLDERS)


def _parse_wms_source(layer) -> Optional[dict]:
    """
    If layer is a WMS/WMTS/XYZ raster layer, return a dict describing how to
    add it in Leaflet. Returns None for plain file-based rasters.

    ``exportNote`` is set when the service cannot be reproduced faithfully in
    the export, so the plugin can say so at export time instead of the reader
    finding an empty map.
    """
    provider = layer.dataProvider()
    if provider is None or provider.name() != "wms":
        return None

    uri_str = provider.dataSourceUri()
    params  = parse_qs(uri_str, keep_blank_values=True)

    url = (params.get("url") or params.get("URL") or [None])[0]
    if not url:
        return None

    layers  = (params.get("layers")  or [""])[0]
    format_ = (params.get("format")  or ["image/png"])[0]
    styles  = (params.get("styles")  or [""])[0]
    crs     = (params.get("crs") or params.get("CRS") or
               params.get("srs") or params.get("SRS") or ["EPSG:3857"])[0]
    version = (params.get("version") or ["1.1.1"])[0]
    matrix_set = (params.get("tileMatrixSet") or params.get("tilematrixset")
                  or [""])[0]

    ttype = (params.get("type") or [""])[0].lower()
    # QGIS records a WMTS connection by its capabilities URL plus a
    # tileMatrixSet, and sets no type= at all — so a WMTS layer used to fall
    # through to the WMS branch and have GetMap requests fired at a
    # capabilities document, which returns XML and draws nothing.
    if not ttype:
        ttype = "wmts" if matrix_set else "wms"

    out = {
        "wmsUrl":     url,
        "wmsLayers":  layers,
        "wmsFormat":  format_,
        "wmsStyles":  styles,
        "wmsCrs":     crs,
        "wmsVersion": version,
        "tileType":   ttype,
    }

    if ttype == "xyz":
        # An XYZ layer is only usable if the URL really is a tile template.
        if not _has_xyz_template(url):
            out["exportNote"] = (
                "tile URL has no {z}/{x}/{y} placeholders, so no tiles can be "
                "requested. Re-add the layer using its XYZ tile URL."
            )
        return out

    if ttype == "wms" and not _is_mercator_matrix_set(crs):
        # A web map is Web Mercator. A WMS in any other projection is fetched
        # by asking the server for EPSG:3857 and letting it reproject — which
        # works, until a server quietly ignores the requested CRS and returns
        # its native one. The image then lands in the right place on the page
        # and the wrong place on the ground, which is a slight, easily-missed
        # shift rather than an obvious failure. Nothing client-side can detect
        # that, so say up front which layers depend on it.
        out["exportNote"] = (
            "layer is in %s, so the web map asks the service to reproject it "
            "to EPSG:3857. If it draws shifted, the service is not honouring "
            "that — check it advertises EPSG:3857, or re-publish the layer in "
            "Web Mercator." % crs
        )
        return out

    if ttype == "wmts":
        out["wmtsMatrixSet"] = matrix_set
        if not _is_mercator_matrix_set(matrix_set):
            out["exportNote"] = (
                "WMTS tile matrix set '%s' is not Web Mercator, and a web map "
                "cannot line those tiles up. Add the service as WMS instead, "
                "or pick its Web Mercator / GoogleMapsCompatible matrix set."
                % (matrix_set or "unknown")
            )
            return out
        # KVP GetTile, which is what a Mercator matrix set lets us build. The
        # matrix identifier is the zoom level for these sets.
        base = _wmts_kvp_base(url)
        sep = "&" if "?" in base else "?"
        out["wmsUrl"] = (
            base + sep
            + "SERVICE=WMTS&REQUEST=GetTile&VERSION=1.0.0"
            + "&LAYER=" + layers
            + "&STYLE=" + (styles or "default")
            + "&FORMAT=" + format_
            + "&TILEMATRIXSET=" + matrix_set
            + "&TILEMATRIX={z}&TILEROW={y}&TILECOL={x}"
        )
        return out

    return out


def _parse_cog_source(layer) -> Optional[dict]:
    """
    If a GDAL raster layer's source is a remote HTTP(S) Cloud Optimized
    GeoTIFF (e.g. a topography raster on public Azure blob storage), return a
    dict describing how to load it client-side rather than embedding it as
    base64. Returns None for local files and non-COG sources.

    QGIS represents a remote COG through GDAL's virtual filesystem, e.g.
        /vsicurl/https://acct.blob.core.windows.net/container/dem.tif
    or occasionally as a bare https URL. We pull the first http(s) URL out of
    the source URI and keep it only when it points at a .tif/.tiff.
    """
    provider = layer.dataProvider()
    if provider is None or provider.name() != "gdal":
        return None

    uri = provider.dataSourceUri() or ""
    m = re.search(r"https?://\S+", uri)
    if not m:
        return None
    url = m.group(0)
    # Trim trailing GDAL open-option delimiters (|) or whitespace-separated args
    for sep in ("|", " ", "\t"):
        if sep in url:
            url = url.split(sep)[0]

    low = url.lower()
    if not (low.endswith(".tif") or low.endswith(".tiff")
            or ".tif?" in low or ".tiff?" in low):
        return None

    ext = layer.extent()
    tr  = QgsCoordinateTransform(layer.crs(), _WGS84, QgsProject.instance())
    wgs = tr.transformBoundingBox(ext)
    return {
        "kind":    "cog",
        "name":    layer.name(),
        "url":     url,
        "opacity": round(layer.opacity(), 3),
        "bands":   layer.bandCount() if hasattr(layer, "bandCount") else 0,
        "bounds": [
            [wgs.yMinimum(), wgs.xMinimum()],
            [wgs.yMaximum(), wgs.xMaximum()],
        ],
    }


def _wms_legend_url(wms: dict) -> str:
    """Build a GetLegendGraphic URL from a WMS params dict, or '' if not possible."""
    base = wms.get("wmsUrl", "")
    if not base or wms.get("tileType", "wms") != "wms":
        return ""
    from urllib.parse import urlencode
    params = {
        "SERVICE": "WMS",
        "REQUEST": "GetLegendGraphic",
        "VERSION": wms.get("wmsVersion", "1.1.1"),
        "LAYER": wms.get("wmsLayers", ""),
        "FORMAT": "image/png",
    }
    style = wms.get("wmsStyles", "")
    if style:
        params["STYLE"] = style
    sep = "&" if "?" in base else "?"
    return base + sep + urlencode(params)
