"""Tests for the QGIS version gate.

The plugin targets QGIS 4 but still runs on 3.x, so the gate has to warn on
everything below the target, stay quiet on and above it, and never fire when
it cannot read the version at all.
"""
import os
import sys
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, "qgis_mock"))
sys.path.insert(0, os.path.dirname(_HERE))


class VersionGateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from intermap import version_check
        cls.vc = version_check

    def test_targets_qgis_4(self):
        self.assertEqual(self.vc._MIN_QGIS_VERSION_INT, 40000)
        self.assertEqual(self.vc._MIN_QGIS_VERSION_STR, "4.0")

    def test_qgis_3_is_superseded(self):
        for vint in (30000, 32800, 33400, 34600, 39999):
            self.assertTrue(self.vc.is_superseded(vint), vint)

    def test_qgis_4_and_newer_is_not(self):
        for vint in (40000, 40001, 40200, 50000):
            self.assertFalse(self.vc.is_superseded(vint), vint)

    def test_unknown_version_never_warns(self):
        # A gate that cannot read the version has nothing useful to say.
        self.assertFalse(self.vc.is_superseded(None))

    def test_version_text_formats_the_int(self):
        self.assertEqual(self.vc._version_text(34601), "3.46.1")
        self.assertEqual(self.vc._version_text(40000), "4.0.0")
        self.assertEqual(self.vc._version_text(41203), "4.12.3")

    def test_warning_names_both_versions_and_says_to_update(self):
        msg = self.vc.warning_text(34600)
        self.assertIn("4.0", msg)
        self.assertIn("3.46.0", msg)
        self.assertIn("update", msg.lower())
        self.assertIn("deprecated", msg.lower())

    def test_warning_does_not_block(self):
        # It is advice, not a refusal — the wording has to say so.
        self.assertIn("carry on", self.vc.warning_text(33400).lower())

    def test_warns_once_per_session(self):
        calls = []

        class _Bar:
            def pushMessage(self, *a, **k):
                calls.append(a)

        class _Iface:
            def messageBar(self):
                return _Bar()

            def mainWindow(self):
                return None

        self.vc._warned_this_session = False
        orig = self.vc._qgis_version_int
        self.vc._qgis_version_int = lambda: 33400
        try:
            self.vc.warn_if_unsupported(_Iface())
            self.vc.warn_if_unsupported(_Iface())
            self.vc.warn_if_unsupported(_Iface())
        finally:
            self.vc._qgis_version_int = orig
            self.vc._warned_this_session = False
        self.assertEqual(len(calls), 1, "the warning should appear once a session")

    def test_silent_on_the_target_version(self):
        calls = []

        class _Iface:
            def messageBar(self):
                raise AssertionError("should not reach the message bar")

            def mainWindow(self):
                raise AssertionError("should not reach a dialog")

        self.vc._warned_this_session = False
        orig = self.vc._qgis_version_int
        self.vc._qgis_version_int = lambda: 40000
        try:
            self.vc.warn_if_unsupported(_Iface())
        finally:
            self.vc._qgis_version_int = orig
            self.vc._warned_this_session = False
        self.assertEqual(calls, [])

    def test_never_raises_on_a_broken_iface(self):
        class _Broken:
            def messageBar(self):
                raise RuntimeError("no message bar")

            def mainWindow(self):
                raise RuntimeError("no main window")

        self.vc._warned_this_session = False
        orig = self.vc._qgis_version_int
        self.vc._qgis_version_int = lambda: 33400
        try:
            self.vc.warn_if_unsupported(_Broken())   # must not raise
        finally:
            self.vc._qgis_version_int = orig
            self.vc._warned_this_session = False


if __name__ == "__main__":
    unittest.main()
