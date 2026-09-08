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


_CAPS_CACHE = {}


def _fetch(url: str, timeout_ms: int = 8000) -> str:
    """GET a URL as text, through QGIS's network stack so proxies apply."""
    try:
        from qgis.core import QgsBlockingNetworkRequest
        from qgis.PyQt.QtCore import QUrl
        from qgis.PyQt.QtNetwork import QNetworkRequest
        req = QgsBlockingNetworkRequest()
        qreq = QNetworkRequest(QUrl(url))
        if req.get(qreq) != 0:
            return ""
        return bytes(req.reply().content()).decode("utf-8", "replace")
    except Exception:
        pass
    try:
        import urllib.request
        with urllib.request.urlopen(url, timeout=timeout_ms / 1000.0) as fh:
            return fh.read().decode("utf-8", "replace")
    except Exception:
        return ""


def _wms_advertised_crs(url: str) -> set:
    """Every CRS a WMS advertises, upper-cased, or an empty set on failure.

    Read from GetCapabilities at export time. A server's root CRS list is
    inherited by all its layers, so the union is the right question to ask.
    """
    base = (url or "").split("?")[0]
    if not base:
        return set()
    if base in _CAPS_CACHE:
        return _CAPS_CACHE[base]

    sep = "&" if "?" in url else "?"
    caps = _fetch(url + sep + "SERVICE=WMS&REQUEST=GetCapabilities&VERSION=1.3.0")
    found = set()
    if caps:
        # Tag names carry a namespace in 1.3.0, so match the text directly
        # rather than parsing the whole document.
        for m in re.finditer(r"<(?:\w+:)?(?:CRS|SRS)>([^<]+)</(?:\w+:)?(?:CRS|SRS)>",
                             caps, re.IGNORECASE):
            for token in m.group(1).split():
                found.add(token.strip().upper())
    _CAPS_CACHE[base] = found
    return found


def _best_wms_crs(url: str, declared: str) -> tuple:
    """Pick the CRS to request a WMS in, and say why.

    A web map is Web Mercator, so a layer in anything else has to be
    reprojected by the server. Asking for a CRS the server does not advertise
    is how a layer ends up drawn in the wrong place: some servers ignore the
    request and answer in their own projection instead. Ask for one it
    actually lists, preferring the map's own projection.
    """
    if _is_mercator_matrix_set(declared):
        return declared, ""
    advertised = _wms_advertised_crs(url)
    if not advertised:
        # Offline, or the service did not answer. EPSG:3857 stays the request,
        # which is what it always was — but say that it is unverified.
        return "EPSG:3857", (
            "could not read the service's capabilities to confirm which "
            "projections it supports, so the export asks for EPSG:3857. If "
            "the layer draws shifted, the service is answering in %s instead."
            % declared
        )
    for candidate in ("EPSG:3857", "EPSG:900913", "EPSG:4326", "CRS:84"):
        if candidate in advertised:
            return candidate, ""
    return declared, (
        "the service does not advertise EPSG:3857 or EPSG:4326, only %s, and "
        "a web map cannot reproject those tiles itself. Ask for the layer to "
        "be published in Web Mercator, or bring it in as a vector layer."
        % declared
    )


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

    # Zoom limits QGIS recorded for the service. Without them a web map will
    # happily request tile levels the service does not publish, get nothing
    # back, and show an empty layer at exactly the zoom people work at.
    def _int_param(name):
        try:
            return int((params.get(name) or [""])[0])
        except (TypeError, ValueError):
            return None

    zmin, zmax = _int_param("zmin"), _int_param("zmax")

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
    if zmin is not None:
        out["tileMinZoom"] = zmin
    if zmax is not None:
        out["tileMaxZoom"] = zmax

    if ttype == "xyz":
        # An XYZ layer is only usable if the URL really is a tile template.
        if not _has_xyz_template(url):
            out["exportNote"] = (
                "tile URL has no {z}/{x}/{y} placeholders, so no tiles can be "
                "requested. Re-add the layer using its XYZ tile URL."
            )
        return out

    if ttype == "wms":
        # Ask the service for a projection it actually advertises, rather than
        # assuming Web Mercator and hoping. Requesting an unsupported CRS is
        # how a layer ends up drawn in the wrong place.
        best, note = _best_wms_crs(url, crs)
        out["wmsCrs"] = best
        out["wmsSourceCrs"] = crs
        if note:
            out["exportNote"] = note
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
