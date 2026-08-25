"""QGIS version gate: warn when the plugin is running on a superseded QGIS.

The plugin targets QGIS 4 (Qt6). It still runs on QGIS 3 — every Qt6 spelling
it uses is valid on Qt5 as well, and intermap/compat.py resolves the enum
aliases QGIS 4 dropped — but 3.x is no longer the version it is developed and
tested against, so anything below the target gets a one-off warning.

The warning never blocks: the plugin opens and exports either way.
"""
from qgis.PyQt.QtCore import QSettings

from .dialog.constants import _SETTINGS_KEY

# ── The flag ────────────────────────────────────────────────────────────────
# QGIS version this plugin targets, in QGIS integer format
# (MAJOR * 10000 + MINOR * 100 + PATCH, so 4.0.0 -> 40000). Anything below it
# is warned about. Raise it when the target moves; set it to 0 to switch the
# warning off entirely.
_MIN_QGIS_VERSION_INT = 40000
_MIN_QGIS_VERSION_STR = "4.0"

# A user or admin can silence the warning permanently by setting this QSettings
# key to true (Settings → Options → Advanced, or QSettings from the console).
_SUPPRESS_KEY = f"{_SETTINGS_KEY}/suppress_version_warning"

# Set once the warning has been shown, so it appears at most once per session.
_warned_this_session = False


def _qgis_version_int():
    """Running QGIS version as an int, or None if it cannot be determined."""
    try:
        from qgis.core import Qgis
    except Exception:
        return None
    try:
        return int(Qgis.QGIS_VERSION_INT)
    except Exception:
        pass
    try:
        return int(Qgis.versionInt())
    except Exception:
        pass
    try:
        # Last resort: parse "4.0.1-Firenze" style strings.
        parts = str(Qgis.QGIS_VERSION).split("-")[0].split(".")
        major, minor = int(parts[0]), int(parts[1])
        patch = int(parts[2]) if len(parts) > 2 else 0
        return major * 10000 + minor * 100 + patch
    except Exception:
        return None


def _version_text(vint):
    return "{}.{}.{}".format(vint // 10000, (vint // 100) % 100, vint % 100)


def is_superseded(vint):
    """Is this QGIS older than the version the plugin targets?

    An unknown version (None) is never warned about — a version check that
    cannot read the version has nothing useful to say.
    """
    if vint is None:
        return False
    return vint < _MIN_QGIS_VERSION_INT


def warning_text(vint):
    """The message shown for a superseded QGIS."""
    return (
        "InterMap targets QGIS {} — you are running {}. You should update: "
        "this plugin may not support deprecated versions of QGIS, and exports "
        "from this version are not tested. You can carry on for now."
    ).format(_MIN_QGIS_VERSION_STR, _version_text(vint))


def warn_if_unsupported(iface):
    """Warn once per session if QGIS is older than the target. Never raises."""
    global _warned_this_session
    try:
        if _warned_this_session:
            return
        if QSettings().value(_SUPPRESS_KEY, False, type=bool):
            return

        vint = _qgis_version_int()
        if not is_superseded(vint):
            return

        _warned_this_session = True
        msg = warning_text(vint)

        try:
            from .compat import MESSAGE_WARNING
            iface.messageBar().pushMessage("InterMap", msg, level=MESSAGE_WARNING, duration=15)
            return
        except Exception:
            pass

        from qgis.PyQt.QtWidgets import QMessageBox
        QMessageBox.warning(iface.mainWindow(), "InterMap — superseded QGIS version", msg)
    except Exception:
        # A version check must never stop the plugin from opening.
        pass
