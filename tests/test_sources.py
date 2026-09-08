"""Tests for remote raster sources: WMS projections and WMTS tile templates.

Both bugs these cover came from the field: a BGS WMS in British National Grid
drew in the wrong place, and a Mining Remediation Authority WMTS listed in the
legend but drew nothing at all.
"""
import os
import sys
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, "qgis_mock"))
sys.path.insert(0, os.path.dirname(_HERE))

from intermap.exporter.sources import (            # noqa: E402
    _has_xyz_template, _is_mercator_matrix_set, _parse_wms_source,
    _wms_legend_url, _wmts_kvp_base,
)


class _FakeProvider:
    def __init__(self, uri, name="wms"):
        self._uri, self._name = uri, name

    def name(self):
        return self._name

    def dataSourceUri(self):
        return self._uri


class _FakeLayer:
    def __init__(self, uri, name="wms"):
        self._p = _FakeProvider(uri, name)

    def dataProvider(self):
        return self._p


class MatrixSetTests(unittest.TestCase):
    def test_web_mercator_sets_are_usable(self):
        for name in ("EPSG:3857", "epsg:900913", "GoogleMapsCompatible",
                     "WebMercatorQuad", "EPSG:102100"):
            self.assertTrue(_is_mercator_matrix_set(name), name)

    def test_british_national_grid_is_not(self):
        # The heart of the WMTS bug: these tiles cannot line up on a
        # Web Mercator map however the URL is built.
        for name in ("EPSG:27700", "BNG", "", None):
            self.assertFalse(_is_mercator_matrix_set(name), name)


class KvpBaseTests(unittest.TestCase):
    def test_strips_an_arcgis_rest_capabilities_path(self):
        self.assertEqual(
            _wmts_kvp_base("https://h/arcgis/rest/services/X/MapServer/WMTS/1.0.0/WMTSCapabilities.xml"),
            "https://h/arcgis/rest/services/X/MapServer/WMTS")

    def test_strips_a_capabilities_query(self):
        self.assertEqual(
            _wmts_kvp_base("https://h/geoserver/gwc/service/wmts?SERVICE=WMTS&REQUEST=GetCapabilities"),
            "https://h/geoserver/gwc/service/wmts")

    def test_leaves_a_plain_endpoint_alone(self):
        self.assertEqual(_wmts_kvp_base("https://h/wmts"), "https://h/wmts")


class XyzTemplateTests(unittest.TestCase):
    def test_detects_a_tile_template(self):
        self.assertTrue(_has_xyz_template("https://h/tiles/{z}/{x}/{y}.png"))

    def test_rejects_a_capabilities_url(self):
        self.assertFalse(_has_xyz_template("https://h/WMTSCapabilities.xml"))


class WmsParseTests(unittest.TestCase):
    def test_keeps_the_layers_own_crs(self):
        # A BGS service in British National Grid: the CRS has to survive to
        # the browser, which is what makes the request unambiguous.
        ld = _parse_wms_source(_FakeLayer(
            "crs=EPSG:27700&format=image/png&layers=GeoSure&styles=&"
            "url=https://map.bgs.ac.uk/arcgis/services/GeoSure/MapServer/WMSServer"))
        self.assertEqual(ld["wmsCrs"], "EPSG:27700")
        self.assertEqual(ld["tileType"], "wms")

    def test_a_non_mercator_wms_says_it_depends_on_the_server(self):
        # It still draws — the server is asked to reproject — but a server that
        # ignores the request shifts the layer silently, so flag it.
        ld = _parse_wms_source(_FakeLayer(
            "crs=EPSG:27700&layers=GeoSure&url=https://h/WMSServer"))
        self.assertIn("exportNote", ld)
        self.assertIn("EPSG:27700", ld["exportNote"])
        self.assertIn("3857", ld["exportNote"])

    def test_a_mercator_wms_needs_no_note(self):
        ld = _parse_wms_source(_FakeLayer(
            "crs=EPSG:3857&layers=X&url=https://h/WMSServer"))
        self.assertNotIn("exportNote", ld)

    def test_non_wms_provider_is_not_a_remote_service(self):
        self.assertIsNone(_parse_wms_source(_FakeLayer("/data/dem.tif", name="gdal")))


class WmtsParseTests(unittest.TestCase):
    _MRA = ("crs=EPSG:3857&format=image/png&layers=CoalMining&styles=default&"
            "tileMatrixSet=%s&url=https://services.example/MapServer/WMTS/1.0.0/"
            "WMTSCapabilities.xml")

    def test_a_wmts_uri_without_type_is_still_wmts(self):
        # QGIS records no type= for WMTS, so this used to be treated as WMS
        # and have GetMap fired at a capabilities document.
        ld = _parse_wms_source(_FakeLayer(self._MRA % "EPSG:3857"))
        self.assertEqual(ld["tileType"], "wmts")

    def test_mercator_wmts_becomes_a_tile_template(self):
        ld = _parse_wms_source(_FakeLayer(self._MRA % "EPSG:3857"))
        url = ld["wmsUrl"]
        for token in ("{z}", "{x}", "{y}"):
            self.assertIn(token, url)
        self.assertIn("REQUEST=GetTile", url)
        self.assertIn("TILEMATRIXSET=EPSG:3857", url)
        self.assertIn("LAYER=CoalMining", url)
        self.assertNotIn("WMTSCapabilities.xml", url)
        self.assertNotIn("exportNote", ld)

    def test_non_mercator_wmts_is_reported_not_silently_broken(self):
        ld = _parse_wms_source(_FakeLayer(self._MRA % "EPSG:27700"))
        self.assertIn("exportNote", ld)
        self.assertIn("27700", ld["exportNote"])
        self.assertIn("WMS", ld["exportNote"])
        # It must not pretend to be a drawable tile template.
        self.assertNotIn("{z}", ld["wmsUrl"])

    def test_xyz_without_placeholders_is_reported(self):
        ld = _parse_wms_source(_FakeLayer(
            "type=xyz&url=https://h/service/WMTSCapabilities.xml"))
        self.assertIn("exportNote", ld)
        self.assertIn("{z}", ld["exportNote"])

    def test_legend_url_is_only_built_for_real_wms(self):
        self.assertEqual(_wms_legend_url({"wmsUrl": "https://h/wms",
                                          "tileType": "wmts"}), "")
        self.assertIn("GetLegendGraphic",
                      _wms_legend_url({"wmsUrl": "https://h/wms",
                                       "tileType": "wms", "wmsLayers": "L"}))


if __name__ == "__main__":
    unittest.main()
