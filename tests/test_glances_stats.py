import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from glances.stats import AdditionalPluginLoader, GlancesStats


@pytest.fixture
def additional_plugin_loader():
    """Create an AdditionalPluginLoader instance."""
    return AdditionalPluginLoader()


class Testload_additional_plugins:  # noqa: D203
    """Tests for helper methods and main method of load_additional_plugins."""

    def test_contains_plugin_model_returns_true(self, additional_plugin_loader):
        """Should return True when PluginModel class exists."""
        with tempfile.TemporaryDirectory() as tmpdir:
            plugin_file = Path(tmpdir) / "model.py"

            plugin_file.write_text(
                "class PluginModel:\n    pass\n",
                encoding="utf-8",
            )

            result = additional_plugin_loader._contains_plugin_model(plugin_file)

            assert result is True  # nosec B101

    def test_contains_plugin_model_returns_false(self, additional_plugin_loader):
        """Should return False when PluginModel class does not exist."""
        with tempfile.TemporaryDirectory() as tmpdir:
            plugin_file = Path(tmpdir) / "model.py"

            plugin_file.write_text(
                "class OtherClass:\n    pass\n",
                encoding="utf-8",
            )

            result = additional_plugin_loader._contains_plugin_model(plugin_file)

            assert result is False  # nosec B101

    def test_contains_plugin_model_handles_syntax_error(self, additional_plugin_loader):
        """Should return False for invalid Python syntax."""
        with tempfile.TemporaryDirectory() as tmpdir:
            plugin_file = Path(tmpdir) / "model.py"

            plugin_file.write_text(
                "class PluginModel(\n",
                encoding="utf-8",
            )

            result = additional_plugin_loader._contains_plugin_model(plugin_file)

            assert result is False  # nosec B101

    def test_get_addl_plugins_returns_valid_plugins(self, additional_plugin_loader):
        """Should return only valid plugin directories."""
        with tempfile.TemporaryDirectory() as tmpdir:
            plugin_dir = Path(tmpdir)

            valid_plugin = plugin_dir / "valid_plugin"
            valid_plugin.mkdir()

            (valid_plugin / "model.py").write_text(
                "class PluginModel:\n    pass\n",
                encoding="utf-8",
            )

            invalid_plugin = plugin_dir / "invalid_plugin"
            invalid_plugin.mkdir()

            (invalid_plugin / "model.py").write_text(
                "class OtherClass:\n    pass\n",
                encoding="utf-8",
            )

            result = additional_plugin_loader._get_addl_plugins(plugin_dir)

            assert result == ["valid_plugin"]  # nosec B101

    @patch("glances.stats.import_module")
    @patch("glances.stats.Counter")
    def test_load_additional_plugins_loads_plugin(
        self,
        mock_counter,
        mock_import_module,
        additional_plugin_loader,
    ):
        """Should load and register additional plugins."""
        mock_args = MagicMock()
        mock_args.plugin_dir = "/fake/plugins"
        mock_args.__contains__.return_value = True
        mock_plugin_model = MagicMock()

        mock_module = MagicMock()
        mock_module.PluginModel.return_value = mock_plugin_model

        mock_import_module.return_value = mock_module

        with patch.object(
            additional_plugin_loader,
            "_get_addl_plugins",
            return_value=["test_plugin"],
        ):
            plugins = {}
            additional_plugin_loader.load(plugins, args=mock_args)

        assert "test_plugin" in plugins  # nosec B101


class TestLoaderDependencyInjection:
    """Tests for the loader dependency injection in GlancesStats."""

    def test_default_loaders_are_created(self):
        """GlancesStats should create default loaders when none are injected."""
        stats = GlancesStats()

        assert stats.plugin_loader is not None  # nosec B101
        assert stats.export_loader is not None  # nosec B101
        assert stats.additional_plugin_loader is not None  # nosec B101

    def test_injected_loaders_are_used(self):
        """GlancesStats should delegate module loading to the injected loaders."""
        plugin_loader = MagicMock()
        export_loader = MagicMock()
        additional_plugin_loader = MagicMock()

        stats = GlancesStats(
            plugin_loader=plugin_loader,
            export_loader=export_loader,
            additional_plugin_loader=additional_plugin_loader,
        )

        plugin_loader.load.assert_called_once_with(stats._plugins, args=None)
        additional_plugin_loader.load.assert_called_once_with(stats._plugins, args=None)
        export_loader.load.assert_called_once_with(stats._exports, stats._exports_all, args=None)
