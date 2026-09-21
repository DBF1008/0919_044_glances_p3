#
# This file is part of Glances.
#
# SPDX-FileCopyrightText: 2026 Nicolas Hennion <nicolas@nicolargo.com>
#
# SPDX-License-Identifier: LGPL-3.0-only
#

"""Loaders for the Glances stats manager.

The historical ``GlancesStats.load_modules`` method mixed four
responsibilities: core plugin loading, additional plugin directory scanning
with ``sys.path`` management and export module loading. They are now split
into three independent, injectable loader classes:

- :class:`PluginLoader`: loads the plugins shipped in the ``glances/plugins``
  folder.
- :class:`AdditionalPluginLoader`: scans and loads plugins from extra plugin
  directories (``plugin_dir`` option) and manages ``sys.path`` for them.
- :class:`ExportLoader`: loads the exporters shipped in the
  ``glances/exports`` folder.

The loaders operate on a stats holder object (typically a
:class:`~glances.stats.GlancesStats` instance) which must expose the
``_plugins``, ``_exports`` and ``_exports_all`` dictionaries and a ``config``
attribute. This keeps the loaders decoupled from the stats class itself and
allows them to be unit tested independently.
"""

import ast
import os
import sys
import traceback
from importlib import import_module
from pathlib import Path

from glances.globals import exports_path, plugins_path
from glances.logger import logger
from glances.timer import Counter


class PluginLoader:
    """Load the plugins shipped in the Glances ``plugins`` folder."""

    def __init__(self, stats):
        # The stats holder the loaded plugins are registered into
        self.stats = stats

    def _load_plugin(self, plugin_path, args=None, config=None):
        """Load the plugin, init it and add it to the stats _plugins dict."""
        plugins = self.stats._plugins
        # Load the plugin class
        try:
            # Import the plugin
            plugin = import_module('glances.plugins.' + plugin_path)
            # Init and add the plugin to the dictionary
            if hasattr(plugin, 'PluginModel'):
                # Old fashion way to load the plugin (before Glances 5.0)
                # Should be removed in Glances 5.0 - see #3170
                plugins[plugin_path] = getattr(plugin, 'PluginModel')(args=args, config=config)
                logger.warning(
                    f'The {plugin_path} plugin class name is "PluginModel" and it is deprecated, \
please rename it to "{plugin_path.capitalize()}Plugin"'
                )
            elif hasattr(plugin, plugin_path.capitalize() + 'Plugin'):
                # New fashion way to load the plugin (after Glances 5.0)
                plugins[plugin_path] = getattr(plugin, plugin_path.capitalize() + 'Plugin')(args=args, config=config)
        except Exception as e:
            # If a plugin can not be loaded, display a critical message
            # on the console but do not crash
            logger.critical(f"Error while initializing the {plugin_path} plugin ({e})")
            logger.error(traceback.format_exc())
            # An error occurred, disable the plugin
            if args is not None:
                setattr(args, 'disable_' + plugin_path, False)
        else:
            # Manage the default status of the plugin (enable or disable)
            if args is not None:
                # If the all keys are set in the disable_plugin option then look in the enable_plugin option
                if getattr(args, 'disable_all', False):
                    logger.debug('%s => %s', plugin_path, getattr(args, 'enable_' + plugin_path, False))
                    setattr(args, 'disable_' + plugin_path, not getattr(args, 'enable_' + plugin_path, False))
                else:
                    setattr(args, 'disable_' + plugin_path, getattr(args, 'disable_' + plugin_path, False))

    def load(self, args=None):
        """Load all plugins in the 'plugins' folder."""
        start_duration = Counter()

        for item in os.listdir(plugins_path):
            if os.path.isdir(os.path.join(plugins_path, item)) and not item.startswith('__') and item != 'plugin':
                # Load the plugin
                start_duration.reset()
                self._load_plugin(os.path.basename(item), args=args, config=self.stats.config)
                logger.debug(f"Plugin {item} started in {start_duration.get()} seconds")

        # Log plugins list
        logger.debug(f"Active plugins list: {self.stats.getPluginsList()}")


class AdditionalPluginLoader:
    """Load plugins from additional plugin directories.

    This loader also owns the ``sys.path`` management needed to import these
    out-of-tree plugins: the configured directory is inserted into
    ``sys.path`` while the plugins are imported and the original path is
    restored afterwards.
    """

    def __init__(self, stats):
        # The stats holder the loaded plugins are registered into
        self.stats = stats

    def _contains_plugin_model(self, plugin_file):
        """Return True if the module defines a PluginModel class."""
        try:
            # Read and parse the Python source file into an AST
            # so plugin detection is based on actual class definitions
            # rather than fragile string matching.
            content = plugin_file.read_text(encoding="utf-8")
            tree = ast.parse(content)

            # Walk through all nodes in the AST and look for
            # a class named PluginModel.
            for node in ast.walk(tree):
                if isinstance(node, ast.ClassDef):
                    if node.name == "PluginModel":
                        return True

        except (SyntaxError, OSError):
            # Invalid Python file or unreadable file
            return False

        return False

    def _get_addl_plugins(self, plugin_path):
        """Get list of additional plugins."""
        plugin_list = []

        # Iterate through each directory in the configured plugin path.
        for plugin in os.listdir(plugin_path):
            path = Path(plugin_path) / plugin

            # Ignore non-directories and internal Python directories.
            if not path.is_dir() or path.name.startswith('__'):
                continue

            # Search Python files within the plugin directory.
            for fil in path.glob('*.py'):
                # Skip invalid filesystem entries.
                if not fil.is_file():
                    continue

                # Register the plugin if a PluginModel class exists.
                if self._contains_plugin_model(fil):
                    plugin_list.append(plugin)
                    break

        return plugin_list

    def _resolve_plugin_dir(self, args=None, config=None):
        """Return the configured additional plugin directory or None."""
        path = None
        # Skip section check as implied by has_option
        if config and config.parser.has_option('global', 'plugin_dir'):
            path = config.parser['global']['plugin_dir']

        # Command-line argument takes precedence over config value.
        if args and 'plugin_dir' in args and args.plugin_dir:
            path = args.plugin_dir

        return path

    def load(self, args=None, config=None):
        """Load additional plugins if defined."""
        path = self._resolve_plugin_dir(args=args, config=config)

        # No additional plugin directory configured.
        if not path:
            return

        # Store a copy of the original sys.path so it can be restored
        # after loading (a plain reference would keep the inserted entry).
        _sys_path = list(sys.path)

        # Used to measure plugin startup duration.
        start_duration = Counter()

        # Ensure plugins can be imported from the configured directory.
        sys.path.insert(0, path)

        try:
            for plugin in self._get_addl_plugins(path):
                # Prevent duplicate imports for plugins already loaded.
                if plugin in sys.modules:
                    logger.warn(f"Plugin {plugin} already in sys.modules, skipping (workaround: rename plugin)")
                    continue

                start_duration.reset()
                try:
                    # Dynamically import the plugin model module.
                    _mod_loaded = import_module(plugin + '.model')

                    # Create and register the plugin instance.
                    self.stats._plugins[plugin] = _mod_loaded.PluginModel(args=args, config=config)
                    logger.debug(f"Plugin {plugin} started in {start_duration.get()} seconds")
                except Exception as e:
                    # If a plugin can not be loaded, display a critical message
                    # on the console but do not crash
                    logger.critical(f"Error while initializing the {plugin} plugin ({e})")
                    logger.error(traceback.format_exc())
                    # An error occurred, disable the plugin
                    if args:
                        setattr(args, 'disable_' + plugin, False)
        finally:
            # Restore original Python import path.
            sys.path = _sys_path

        # Log active additional plugins list
        logger.debug(f"Active additional plugins list: {self.stats.getPluginsList()}")


class ExportLoader:
    """Load the exporters shipped in the Glances ``exports`` folder."""

    def __init__(self, stats):
        # The stats holder the loaded exporters are registered into
        self.stats = stats

    def load(self, args=None):
        """Load all exporters in the 'exports' folder."""
        start_duration = Counter()

        if args is None:
            return False

        for item in os.listdir(exports_path):
            if os.path.isdir(os.path.join(exports_path, item)) and not item.startswith('__'):
                # Load the exporter
                start_duration.reset()
                if item.startswith('glances_'):
                    # Avoid circular loop when Glances exporter uses lib with same name
                    # Example: influxdb should be named to glances_influxdb
                    exporter_name = os.path.basename(item).split('glances_')[1]
                else:
                    exporter_name = os.path.basename(item)
                # Set the disable_<name> to False by default
                setattr(
                    self.stats.args,
                    'export_' + exporter_name,
                    getattr(self.stats.args, 'export_' + exporter_name, False),
                )
                # We should import the module
                if getattr(self.stats.args, 'export_' + exporter_name, False):
                    # Import the export module
                    export_module = import_module(item)
                    # Add the exporter instance to the active exporters dictionary
                    self.stats._exports[exporter_name] = export_module.Export(args=args, config=self.stats.config)
                    # Add the exporter instance to the available exporters dictionary
                    self.stats._exports_all[exporter_name] = self.stats._exports[exporter_name]
                else:
                    # Add the exporter name to the available exporters dictionary
                    self.stats._exports_all[exporter_name] = exporter_name
                logger.debug(f"Exporter {exporter_name} started in {start_duration.get()} seconds")

        # Log plugins list
        logger.debug(f"Active exports modules list: {self.stats.getExportsList()}")
        return True
