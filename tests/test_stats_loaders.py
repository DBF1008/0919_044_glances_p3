#
# Glances - An eye on your system
#
# SPDX-License-Identifier: LGPL-3.0-only
#

"""Unit tests for the PluginLoader/AdditionalPluginLoader/ExportLoader classes."""

import collections
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from glances.stats_loaders import AdditionalPluginLoader, ExportLoader, PluginLoader


class DummyStats:
    """Minimal stats holder used by the loaders."""

    def __init__(self, config=None, args=None):
        self.config = config
        self.args = args
        self._plugins = collections.defaultdict(dict)
        self._exports = collections.defaultdict(dict)
        self._exports_all = collections.defaultdict(dict)

    def getPluginsList(self, enable=True):
        if enable:
            return [p for p in self._plugins if self._plugins[p].is_enabled()]
        return list(self._plugins)

    def getExportsList(self, enable=True):
        return list(self._exports) if enable else list(self._exports_all)


# ---------------------------------------------------------------------------
# AdditionalPluginLoader
# ---------------------------------------------------------------------------
@pytest.fixture
def additional_loader():
    return AdditionalPluginLoader(DummyStats())


class TestAdditionalPluginLoader:
    def test_contains_plugin_model_returns_true(self, additional_loader):
        with tempfile.TemporaryDirectory() as tmpdir:
            plugin_file = Path(tmpdir) / "model.py"
            plugin_file.write_text("class PluginModel:\n    pass\n", encoding="utf-8")
            assert additional_loader._contains_plugin_model(plugin_file) is True  # nosec B101

    def test_contains_plugin_model_returns_false(self, additional_loader):
        with tempfile.TemporaryDirectory() as tmpdir:
            plugin_file = Path(tmpdir) / "model.py"
            plugin_file.write_text("class OtherClass:\n    pass\n", encoding="utf-8")
            assert additional_loader._contains_plugin_model(plugin_file) is False  # nosec B101

    def test_contains_plugin_model_handles_syntax_error(self, additional_loader):
        with tempfile.TemporaryDirectory() as tmpdir:
            plugin_file = Path(tmpdir) / "model.py"
            plugin_file.write_text("class PluginModel(\n", encoding="utf-8")
            assert additional_loader._contains_plugin_model(plugin_file) is False  # nosec B101

    def test_get_addl_plugins_returns_valid_plugins(self, additional_loader):
        with tempfile.TemporaryDirectory() as tmpdir:
            plugin_dir = Path(tmpdir)
            valid_plugin = plugin_dir / "valid_plugin"
            valid_plugin.mkdir()
            (valid_plugin / "model.py").write_text("class PluginModel:\n    pass\n", encoding="utf-8")
            invalid_plugin = plugin_dir / "invalid_plugin"
            invalid_plugin.mkdir()
            (invalid_plugin / "model.py").write_text("class OtherClass:\n    pass\n", encoding="utf-8")

            result = additional_loader._get_addl_plugins(plugin_dir)

            assert result == ["valid_plugin"]  # nosec B101

    def test_resolve_plugin_dir_command_line_takes_precedence(self, additional_loader):
        config = MagicMock()
        config.parser.has_option.return_value = True
        config.parser.__getitem__.return_value = {"plugin_dir": "/from/config"}
        args = MagicMock()
        args.plugin_dir = "/from/args"
        args.__contains__.return_value = True

        assert additional_loader._resolve_plugin_dir(args, config) == "/from/args"  # nosec B101

    def test_resolve_plugin_dir_from_config(self, additional_loader):
        config = MagicMock()
        config.parser.has_option.return_value = True
        config.parser.__getitem__.return_value = {"plugin_dir": "/from/config"}
        args = MagicMock()
        args.plugin_dir = None
        args.__contains__.return_value = True

        assert additional_loader._resolve_plugin_dir(args, config) == "/from/config"  # nosec B101

    def test_resolve_plugin_dir_none_when_unset(self, additional_loader):
        config = MagicMock()
        config.parser.has_option.return_value = False
        args = MagicMock()
        args.plugin_dir = None
        args.__contains__.return_value = False

        assert additional_loader._resolve_plugin_dir(args, config) is None  # nosec B101

    @patch("glances.stats_loaders.import_module")
    def test_load_registers_plugin_and_restores_sys_path(self, mock_import_module, additional_loader):
        mock_args = MagicMock()
        mock_args.plugin_dir = "/fake/plugins"
        mock_args.__contains__.return_value = True

        mock_plugin_model = MagicMock()
        mock_module = MagicMock()
        mock_module.PluginModel.return_value = mock_plugin_model
        mock_import_module.return_value = mock_module

        import sys

        # Make sure a previous test did not leave the fake path behind
        while "/fake/plugins" in sys.path:
            sys.path.remove("/fake/plugins")
        original_sys_path = list(sys.path)
        with patch.object(additional_loader, "_get_addl_plugins", return_value=["test_plugin"]):
            # The plugin dir is inserted while the plugins are imported
            assert "/fake/plugins" not in sys.path  # nosec B101
            additional_loader.load(args=mock_args)

        # sys.path must be restored to its pre-loading value once done
        assert sys.path == original_sys_path  # nosec B101
        assert "/fake/plugins" not in sys.path  # nosec B101
        assert additional_loader.stats._plugins["test_plugin"] is mock_plugin_model  # nosec B101

    def test_load_without_plugin_dir_is_noop(self, additional_loader):
        additional_loader.load()
        assert dict(additional_loader.stats._plugins) == {}  # nosec B101


# ---------------------------------------------------------------------------
# PluginLoader
# ---------------------------------------------------------------------------
class TestPluginLoader:
    def test_load_plugins_with_mocked_modules(self):
        stats = DummyStats()
        loader = PluginLoader(stats)

        plugin_instance = MagicMock()
        plugin_instance.is_enabled.return_value = True
        mock_module = MagicMock()
        # Simulate a new style plugin (CpuPlugin class)
        mock_module.CpuPlugin.return_value = plugin_instance
        del mock_module.PluginModel

        with (
            patch("glances.stats_loaders.os.listdir", return_value=["cpu", "__pycache__", "plugin"]),
            patch("glances.stats_loaders.os.path.isdir", return_value=True),
            patch("glances.stats_loaders.import_module", return_value=mock_module),
        ):
            loader.load(args=MagicMock(disable_all=False))

        assert stats._plugins["cpu"] is plugin_instance  # nosec B101

    def test_load_plugin_disabled_on_init_error(self):
        stats = DummyStats()
        loader = PluginLoader(stats)
        args = MagicMock()
        args.disable_all = False

        with patch("glances.stats_loaders.import_module", side_effect=RuntimeError("boom")):
            loader._load_plugin("broken", args=args, config=None)

        assert "broken" not in stats._plugins  # nosec B101
        assert args.disable_broken is False  # nosec B101


# ---------------------------------------------------------------------------
# ExportLoader
# ---------------------------------------------------------------------------
class TestExportLoader:
    def test_load_returns_false_without_args(self):
        loader = ExportLoader(DummyStats())
        assert loader.load(args=None) is False  # nosec B101

    def test_load_instantiates_enabled_exporters(self):
        args = MagicMock()
        # Explicit flags: nats enabled, csv disabled
        args.export_nats = True
        args.export_csv = False

        stats = DummyStats(args=args)
        loader = ExportLoader(stats)

        export_instance = MagicMock()
        mock_module = MagicMock()
        mock_module.Export.return_value = export_instance

        def fake_import(name):
            assert name == "glances_nats"  # nosec B101
            return mock_module

        with (
            patch("glances.stats_loaders.os.listdir", return_value=["glances_nats", "glances_csv"]),
            patch("glances.stats_loaders.os.path.isdir", return_value=True),
            patch("glances.stats_loaders.import_module", side_effect=fake_import),
        ):
            result = loader.load(args=args)

        assert result is True  # nosec B101
        assert stats._exports["nats"] is export_instance  # nosec B101
        assert stats._exports_all["nats"] is export_instance  # nosec B101
        # Disabled exporter is referenced by name only
        assert stats._exports_all["csv"] == "csv"  # nosec B101
        assert "csv" not in stats._exports  # nosec B101

    def test_exporter_name_strips_glances_prefix(self):
        args = MagicMock()
        args.export_influxdb2 = False
        stats = DummyStats(args=args)
        loader = ExportLoader(stats)

        with (
            patch("glances.stats_loaders.os.listdir", return_value=["glances_influxdb2"]),
            patch("glances.stats_loaders.os.path.isdir", return_value=True),
        ):
            loader.load(args=args)

        # Not enabled => registered by name only
        assert stats._exports_all["influxdb2"] == "influxdb2"  # nosec B101
        assert args.export_influxdb2 is False  # nosec B101
