#
# This file is part of Glances.
#
# SPDX-FileCopyrightText: 2022 Nicolas Hennion <nicolas@nicolargo.com>
#
# SPDX-License-Identifier: LGPL-3.0-only
#

"""The stats manager."""

import collections
import sys
import threading

from glances.globals import sys_path, weak_lru_cache
from glances.logger import logger
from glances.stats_loaders import AdditionalPluginLoader, ExportLoader, PluginLoader


class GlancesStats:
    """This class stores, updates and gives stats."""

    # Script header constant
    header = "glances_"

    def __init__(self, config=None, args=None, plugin_loader=None, additional_plugin_loader=None, export_loader=None):
        # Set the config instance
        self.config = config

        # Set the argument instance
        self.args = args

        # Loaders are injected so the loading logic can be tested and
        # replaced independently from the stats holder itself.
        self.plugin_loader = plugin_loader or PluginLoader(self)
        self.additional_plugin_loader = additional_plugin_loader or AdditionalPluginLoader(self)
        self.export_loader = export_loader or ExportLoader(self)

        # Load plugins and exports modules
        self.first_export = True
        self.load_modules(self.args)

    def __getattr__(self, item):
        """Overwrite the getattr method in case of attribute is not found.

        The goal is to dynamically generate the following methods:
        - getPlugname(): return Plugname stat in JSON format
        - getViewsPlugname(): return views of the Plugname stat in JSON format
        """
        # Check if the attribute starts with 'get'
        if item.startswith('getViews'):
            # Get the plugin name
            plugname = item[len('getViews') :].lower()
            # Get the plugin instance
            plugin = self._plugins[plugname]
            if hasattr(plugin, 'get_json_views'):
                # The method get_json_views exist, return it
                return getattr(plugin, 'get_json_views')
            # The method get_views is not found for the plugin
            raise AttributeError(item)
        if item.startswith('get'):
            # Get the plugin name
            plugname = item[len('get') :].lower()
            # Get the plugin instance
            plugin = self._plugins[plugname]
            if hasattr(plugin, 'get_json'):
                # The method get_json exist, return it
                return getattr(plugin, 'get_json')
            # The method get_stats is not found for the plugin
            raise AttributeError(item)
        # Default behavior
        raise AttributeError(item)

    def load_modules(self, args):
        """Wrapper to load: plugins and export modules.

        The actual loading logic lives in the three injected loaders.
        """
        # Init the plugins dict
        # Active plugins dictionary
        self._plugins = collections.defaultdict(dict)
        # Load the plugins
        self.plugin_loader.load(args=args)

        # Load additional plugins (the loader manages sys.path itself)
        self.additional_plugin_loader.load(args=args, config=self.config)

        # Init the export modules dict
        # Active exporters dictionary
        self._exports = collections.defaultdict(dict)
        # All available exporters dictionary
        self._exports_all = collections.defaultdict(dict)
        # Load the export modules
        self.export_loader.load(args=args)

        # Restoring system path
        sys.path = sys_path

    # The following methods are kept as thin backward compatible wrappers
    # around the injected loaders.
    def _load_plugin(self, plugin_path, args=None, config=None):
        """Load one plugin (delegated to the PluginLoader)."""
        self.plugin_loader._load_plugin(plugin_path, args=args, config=config)

    def load_plugins(self, args=None):
        """Load all plugins in the 'plugins' folder (delegated to the PluginLoader)."""
        self.plugin_loader.load(args=args)

    def _contains_plugin_model(self, plugin_file):
        """Return True if the module defines a PluginModel class."""
        return self.additional_plugin_loader._contains_plugin_model(plugin_file)

    def _get_addl_plugins(self, plugin_path):
        """Get list of additional plugins."""
        return self.additional_plugin_loader._get_addl_plugins(plugin_path)

    def load_additional_plugins(self, args=None, config=None):
        """Load additional plugins if defined (delegated to the AdditionalPluginLoader)."""
        self.additional_plugin_loader.load(args=args, config=config)

    def load_exports(self, args=None):
        """Load all exporters in the 'exports' folder (delegated to the ExportLoader)."""
        return self.export_loader.load(args=args)

    def getPluginsList(self, enable=True):
        """Return the plugins list.

        if enable is True, only return the active plugins (default)
        if enable is False, return all the plugins

        Return: list of plugin name
        """
        if enable:
            return [p for p in self._plugins if self._plugins[p].is_enabled()]
        return list(self._plugins)

    def getExportsList(self, enable=True):
        """Return the exports list.

        if enable is True, only return the active exporters (default)
        if enable is False, return all the exporters

        :return: list of export module names
        """
        if enable:
            return list(self._exports)
        return list(self._exports_all)

    def load_limits(self, config=None):
        """Load the stats limits (except the one in the exclude list)."""
        # For each plugins (enable or not), call the load_limits method
        for p in self.getPluginsList(enable=False):
            self._plugins[p].load_limits(config)

    # It's a weak cache to avoid updating the same plugin too often
    # Note: the function always return None
    @weak_lru_cache(ttl=1)
    def update_plugin(self, p):
        """Update stats, history and views for the given plugin name p"""
        self._plugins[p].update()
        self._plugins[p].update_views()
        self._plugins[p].update_stats_history()

    def update(self, plugins_list_to_update=None):
        """Wrapper method to update stats.
        If plugins_list_to_update is provided (list), only update the given plugins.
        """
        if plugins_list_to_update is None:
            plugins_list_to_update = self.getPluginsList(enable=True)

        # Start update of all enable plugins
        for p in plugins_list_to_update:
            self.update_plugin(p)

    def export(self, input_stats=None):
        """Export all the stats.

        Each export module is ran in a dedicated thread.
        """
        if self.first_export:
            # Init fields description
            # Why ? Because some exports modules need to know what is exported (name, type...)
            for e in self.getExportsList():
                logger.debug(f"Init exported stats using the {e} module")
                # @TODO: is it a good idea to thread this call ?
                self._exports[e].init_fields(input_stats)
            # In this first loop, data are not exported because some information are missing (rate for example)
            logger.debug("Do not export stats during the first iteration because some information are missing")
            self.first_export = False
            return False

        input_stats = input_stats or {}

        for e in self.getExportsList():
            logger.debug(f"Export stats using the {e} module")
            thread = threading.Thread(target=self._exports[e].update, args=(input_stats,))
            thread.start()

        return True

    def getAll(self):
        """Return all the stats (list).
        This method is called byt the XML/RPC API.
        It should return all the plugins (enable or not) because filtering can be done by the client.
        """
        return [self._plugins[p].get_raw() for p in self.getPluginsList(enable=False)]

    def getAllAsDict(self, plugin_list=None):
        """Return all the stats (as dict).
        This method is called by the RESTFul API.
        """
        if plugin_list is None:
            # All enabled plugins should be exported
            plugin_list = self.getPluginsList()
        return {p: self._plugins[p].get_raw() for p in plugin_list}

    def getAllFieldsDescription(self):
        """Return all fields description (as list)."""
        return [self._plugins[p].fields_description for p in self.getPluginsList(enable=False)]

    def getAllFieldsDescriptionAsDict(self, plugin_list=None):
        """Return all fields description (as dict)."""
        if plugin_list is None:
            # All enabled plugins should be exported
            plugin_list = self.getPluginsList()
        return {p: self._plugins[p].fields_description for p in plugin_list}

    def getAllExports(self, plugin_list=None):
        """Return all the stats to be exported as a list.

        Default behavior is to export all the stat
        if plugin_list is provided (list), only export stats of given plugins
        """
        if plugin_list is None:
            # All enabled plugins should be exported
            plugin_list = self.getPluginsList()
        return [self._plugins[p].get_export() for p in plugin_list]

    def getAllExportsAsDict(self, plugin_list=None):
        """Return all the stats to be exported as a dict.

        Default behavior is to export all the stat
        if plugin_list is provided (list), only export stats of given plugins
        """
        if plugin_list is None:
            # All enabled plugins should be exported
            plugin_list = self.getPluginsList()
        return {p: self._plugins[p].get_export() for p in plugin_list}

    def getAllLimits(self, plugin_list=None):
        """Return the plugins limits list.

        Default behavior is to export all the limits
        if plugin_list is provided, only export limits of given plugin (list)
        """
        if plugin_list is None:
            # All enabled plugins should be exported
            plugin_list = self.getPluginsList()
        return [self._plugins[p].limits for p in plugin_list]

    def getAllLimitsAsDict(self, plugin_list=None):
        """Return all the stats limits (dict).

        Default behavior is to export all the limits
        if plugin_list is provided, only export limits of given plugin (list)
        """
        if plugin_list is None:
            # All enabled plugins should be exported
            plugin_list = self.getPluginsList()
        return {p: self._plugins[p].limits for p in plugin_list}

    def getAllViews(self, plugin_list=None):
        """Return the plugins views.
        This method is called byt the XML/RPC API.
        It should return all the plugins views (enable or not) because filtering can be done by the client.
        """
        if plugin_list is None:
            plugin_list = self.getPluginsList(enable=False)
        return [self._plugins[p].get_views() for p in plugin_list]

    def getAllViewsAsDict(self, plugin_list=None):
        """Return all the stats views (dict).
        This method is called by the RESTFul API.
        """
        if plugin_list is None:
            # All enabled plugins should be exported
            plugin_list = self.getPluginsList()
        return {p: self._plugins[p].get_views() for p in plugin_list}

    def get_plugin(self, plugin_name):
        """Return the plugin stats."""
        if plugin_name in self._plugins:
            return self._plugins[plugin_name]
        return None

    def get_plugin_view(self, plugin_name):
        """Return the plugin views."""
        if plugin_name in self._plugins:
            return self._plugins[plugin_name].get_views()
        return None

    def end(self):
        """End of the Glances stats."""
        # Close export modules
        for e in self._exports:
            self._exports[e].exit()
        # Close plugins
        for p in self._plugins:
            self._plugins[p].exit()
