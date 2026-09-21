#
# Glances - An eye on your system
#
# SPDX-License-Identifier: LGPL-3.0-only
#

"""Unit tests for GlancesStats and the backward compatible loader wrappers."""

import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from glances.stats import GlancesStats
from glances.stats_loaders import AdditionalPluginLoader, ExportLoader, PluginLoader


@pytest.fixture
def plugin_manager():
    """Create plugin manager instance."""
    return GlancesStats()


class Testload_additional_plugins:  # noqa: D203
    """Tests for helper methods and main method of load_additional_plugins.

    The loading logic now lives in AdditionalPluginLoader; GlancesStats keeps
    thin wrappers that should behave exactly the same way.
    """

    def test_contains_plugin_model_returns_true(self, plugin_manager):
        """Should return True when PluginModel class exists."""
        with tempfile.TemporaryDirectory() as tmpdir:
            plugin_file = Path(tmpdir) / "model.py"
            plugin_file.write_text("class PluginModel:\n    pass\n", encoding="utf-8")
            result = plugin_manager._contains_plugin_model(plugin_file)
            assert result is True  # nosec B101

    def test_contains_plugin_model_returns_false(self, plugin_manager):
        """Should return False when PluginModel class does not exist."""
        with tempfile.TemporaryDirectory() as tmpdir:
            plugin_file = Path(tmpdir) / "model.py"
            plugin_file.write_text("class OtherClass:\n    pass\n", encoding="utf-8")
            result = plugin_manager._contains_plugin_model(plugin_file)
            assert result is False  # nosec B101

    def test_contains_plugin_model_handles_syntax_error(self, plugin_manager):
        """Should return False for invalid Python syntax."""
        with tempfile.TemporaryDirectory() as tmpdir:
            plugin_file = Path(tmpdir) / "model.py"
            plugin_file.write_text("class PluginModel(\n", encoding="utf-8")
            result = plugin_manager._contains_plugin_model(plugin_file)
            assert result is False  # nosec B101

    def test_get_addl_plugins_returns_valid_plugins(self, plugin_manager):
        """Should return only valid plugin directories."""
        with tempfile.TemporaryDirectory() as tmpdir:
            plugin_dir = Path(tmpdir)
            valid_plugin = plugin_dir / "valid_plugin"
            valid_plugin.mkdir()
            (valid_plugin / "model.py").write_text("class PluginModel:\n    pass\n", encoding="utf-8")
            invalid_plugin = plugin_dir / "invalid_plugin"
            invalid_plugin.mkdir()
            (invalid_plugin / "model.py").write_text("class OtherClass:\n    pass\n", encoding="utf-8")

            result = plugin_manager._get_addl_plugins(plugin_dir)
            assert result == ["valid_plugin"]  # nosec B101

    @patch("glances.stats_loaders.import_module")
    def test_load_additional_plugins_loads_plugin(self, mock_import_module, plugin_manager):
        """Should load and register additional plugins via the wrapper."""
        mock_args = MagicMock()
        mock_args.plugin_dir = "/fake/plugins"
        mock_args.__contains__.return_value = True
        mock_plugin_model = MagicMock()
        mock_module = MagicMock()
        mock_module.PluginModel.return_value = mock_plugin_model
        mock_import_module.return_value = mock_module

        with patch.object(
            plugin_manager.additional_plugin_loader,
            "_get_addl_plugins",
            return_value=["test_plugin"],
        ):
            plugin_manager.load_additional_plugins(args=mock_args)

        assert "test_plugin" in plugin_manager._plugins  # nosec B101


class TestLoaderDependencyInjection:
    """The three loaders must be injectable into GlancesStats."""

    def test_default_loaders_are_instantiated(self):
        stats = GlancesStats()
        assert isinstance(stats.plugin_loader, PluginLoader)  # nosec B101
        assert isinstance(stats.additional_plugin_loader, AdditionalPluginLoader)  # nosec B101
        assert isinstance(stats.export_loader, ExportLoader)  # nosec B101
        # The loaders operate on the stats holder itself
        assert stats.plugin_loader.stats is stats  # nosec B101
        assert stats.additional_plugin_loader.stats is stats  # nosec B101
        assert stats.export_loader.stats is stats  # nosec B101

    def test_load_modules_uses_injected_loaders(self):
        plugin_loader = MagicMock()
        additional_loader = MagicMock()
        export_loader = MagicMock()
        export_loader.load.return_value = True

        stats = GlancesStats(
            config=None,
            args=MagicMock(),
            plugin_loader=plugin_loader,
            additional_plugin_loader=additional_loader,
            export_loader=export_loader,
        )

        plugin_loader.load.assert_called_once_with(args=stats.args)  # nosec B101
        additional_loader.load.assert_called_once_with(args=stats.args, config=None)  # nosec B101
        export_loader.load.assert_called_once_with(args=stats.args)  # nosec B101

    def test_wrapper_methods_delegate_to_injected_loaders(self):
        plugin_loader = MagicMock()
        additional_loader = MagicMock()
        export_loader = MagicMock()

        stats = GlancesStats(
            args=MagicMock(),
            plugin_loader=plugin_loader,
            additional_plugin_loader=additional_loader,
            export_loader=export_loader,
        )

        stats.load_plugins(args="a")
        plugin_loader.load.assert_called_with(args="a")  # nosec B101

        stats._load_plugin("cpu", args="b", config="c")
        plugin_loader._load_plugin.assert_called_with("cpu", args="b", config="c")  # nosec B101

        stats.load_additional_plugins(args="d", config="e")
        additional_loader.load.assert_called_with(args="d", config="e")  # nosec B101

        stats._get_addl_plugins("/some/path")
        additional_loader._get_addl_plugins.assert_called_with("/some/path")  # nosec B101

        stats._contains_plugin_model("model.py")
        additional_loader._contains_plugin_model.assert_called_with("model.py")  # nosec B101

        stats.load_exports(args="f")
        export_loader.load.assert_called_with(args="f")  # nosec B101
