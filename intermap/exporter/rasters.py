"""Raster layer handling: legend extraction and PNG snapshot embedding."""
import os
import base64
import tempfile

from qgis.core import (
    QgsCoordinateTransform, QgsMapSettings, QgsProject,
)
from qgis.PyQt.QtCore import QSize
from qgis.PyQt.QtGui import QColor

from .compat import _WGS84
from .utils import _color_to_hex


def _raster_legend_data(layer) -> dict:
    """Extract legend symbology from a raster layer renderer for use in the web legend."""
    try:
        from qgis.core import (
            QgsSingleBandPseudoColorRenderer, QgsPalettedRasterRenderer,
            QgsSingleBandGrayRenderer, QgsMultiBandColorRenderer,
        )
        renderer = layer.renderer()
        if renderer is None:
            return {"type": "unknown"}

        if isinstance(renderer, QgsPalettedRasterRenderer):
            classes = []
            for cls in renderer.classes():
                classes.append({
                    "value": cls.value,
                    "label": cls.label or str(cls.value),
                    "color": _color_to_hex(cls.color),
                    "alpha": round(cls.color.alphaF(), 3),
                })
            return {"type": "paletted", "classes": classes}

        if isinstance(renderer, QgsSingleBandPseudoColorRenderer):
            shader = renderer.shader()
            if shader:
                fn = shader.rasterShaderFunction()
                if fn and hasattr(fn, "colorRampItemList"):
                    raw = fn.colorRampItemList()
                    # Thin to ≤30 stops to keep JSON compact
                    if len(raw) > 30:
                        step = len(raw) / 28
                        raw = [raw[int(i * step)] for i in range(28)] + [raw[-1]]
                    stops = [
                        {"value": round(it.value, 6),
                         "label": it.label or "",
                         "color": _color_to_hex(it.color)}
                        for it in raw
                    ]
                    return {
                        "type": "pseudocolor",
                        "stops": stops,
                        "min": stops[0]["value"] if stops else 0,
                        "max": stops[-1]["value"] if stops else 1,
                    }

        if isinstance(renderer, QgsSingleBandGrayRenderer):
            ce = renderer.contrastEnhancement()
            mn = ce.minimumValue() if ce else 0
            mx = ce.maximumValue() if ce else 255
            try:
                from qgis.core import QgsSingleBandGrayRenderer as _GR
                black_first = renderer.gradient() == _GR.BlackToWhite
            except Exception:
                black_first = True
            return {
                "type": "gray",
                "min": round(mn, 4),
                "max": round(mx, 4),
                "blackFirst": black_first,
            }

        if isinstance(renderer, QgsMultiBandColorRenderer):
            return {
                "type": "multiband",
                "redBand":   renderer.redBand(),
                "greenBand": renderer.greenBand(),
                "blueBand":  renderer.blueBand(),
            }

    except Exception:
        pass
    return {"type": "unknown"}


def _raster_to_base64(layer) -> tuple:
    """Render raster layer to PNG in WGS-84, return (base64_str, bounds_list [[s,w],[n,e]])."""
    transform = QgsCoordinateTransform(layer.crs(), _WGS84, QgsProject.instance())
    wgs_extent = transform.transformBoundingBox(layer.extent())

    width = 1024
    ratio = wgs_extent.height() / wgs_extent.width() if wgs_extent.width() > 0 else 1
    height = max(1, int(width * ratio))

    settings = QgsMapSettings()
    settings.setLayers([layer])
    settings.setOutputSize(QSize(width, height))
    settings.setExtent(wgs_extent)       # render in WGS-84 so image aligns with bounds
    settings.setDestinationCrs(_WGS84)
    settings.setBackgroundColor(QColor(0, 0, 0, 0))

    from qgis.core import QgsMapRendererParallelJob
    job = QgsMapRendererParallelJob(settings)
    job.start()
    job.waitForFinished()
    img = job.renderedImage()

    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
        tmp_path = tmp.name
    try:
        img.save(tmp_path, "PNG")
        with open(tmp_path, "rb") as f:
            b64 = base64.b64encode(f.read()).decode("ascii")
    finally:
        os.unlink(tmp_path)

    bounds = [
        [wgs_extent.yMinimum(), wgs_extent.xMinimum()],
        [wgs_extent.yMaximum(), wgs_extent.xMaximum()],
    ]
    return b64, bounds


# ── Report / story-mode helpers ──────────────────────────────────────────────
