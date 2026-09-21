#
# This file is part of Glances.
#
# SPDX-FileCopyrightText: 2026 Nicolas Hennion <nicolas@nicolargo.com>
#
# SPDX-License-Identifier: LGPL-3.0-only
#

"""
I am your father...
...for all Glances exports IF.
"""

import re
import time

from glances.globals import NoOptionError, NoSectionError, json_dumps
from glances.logger import logger
from glances.timer import Counter


class GlancesExport:
    """Main class for Glances export IF."""

    # List of non exportable internal plugins
    non_exportable_plugins = [
        "alert",
        "help",
        "plugin",
        "psutilversion",
        "quicklook",
        "version",
    ]

    def __init__(self, config=None, args=None):
        """Init the export class."""
        # Export name
        self.export_name = self.__class__.__module__
        logger.debug(f"Init export module {self.export_name}")

        # Init the config & args
        self.config = config
        self.args = args

        # By default export is disabled
        # Needs to be set to True in the __init__ class of child
        self.export_enable = False

        # Mandatory for (most of) the export module
        self.host = None
        self.port = None

        # Save last export list
        self._last_exported_list = None

        # Fields description
        self._fields_description = None

        # Sampled DEBUG log state, keyed by decorated method name
        self._log_sample_state = {}
        self.log_every_n = self.default_log_every_n
        self.log_every_m_seconds = self.default_log_every_m_seconds

        # Load the default common export configuration
        if self.config is not None:
            self.load_common_conf()

    def _log_result_decorator(fct):
        """Log (DEBUG) the result of the function fct with sampling.

        Export methods are called at a very high frequency and logging a DEBUG
        line on every single call quickly floods the log files. The logging is
        therefore sampled and can be tuned through the common [export] section
        of the Glances configuration file:

        - log_every_n: only log one call every N calls (0 = disabled, 1 = log
          every call). Default: 10.
        - log_every_m_seconds: at most one log line every M seconds for a
          given exporter/method (0 = disabled). Default: 0 (time based
          sampling disabled).

        When both are enabled, a line is emitted as soon as one of the two
        thresholds is reached. The sampled line also reports the number of
        calls aggregated since the previous line and their average duration.
        """

        def wrapper(*args, **kw):
            exporter = args[0]
            counter = Counter()
            ret = fct(*args, **kw)
            duration = counter.get()
            exporter._sample_log(fct.__name__, ret, duration)
            return ret

        return wrapper

    # Default sampling values, can be overridden in the [export] config section
    default_log_every_n = 10
    default_log_every_m_seconds = 0

    def _sample_log(self, method_name, result, duration):
        """Record one decorated export call and emit a sampled DEBUG line.

        :param method_name: name of the decorated export method
        :param result: value returned by the export method
        :param duration: duration in seconds of the export method
        """
        every_n = getattr(self, "log_every_n", None)
        if every_n is None:
            every_n = self.default_log_every_n
        every_m = getattr(self, "log_every_m_seconds", None)
        if every_m is None:
            every_m = self.default_log_every_m_seconds

        # Sampling disabled on both axes: nothing to log
        if every_n <= 0 and every_m <= 0:
            return

        state = self._log_sample_state.setdefault(method_name, {})
        now = time.monotonic()
        if not state:
            state["count"] = 0
            state["total_duration"] = 0.0
            state["last_log"] = now

        state["count"] += 1
        state["total_duration"] += duration

        by_count = every_n > 0 and state["count"] >= every_n
        by_time = every_m > 0 and (now - state["last_log"]) >= every_m
        if not (by_count or by_time):
            return

        aggregated = state["count"]
        total_duration = state["total_duration"]
        avg_duration = total_duration / aggregated if aggregated else duration
        state["count"] = 0
        state["total_duration"] = 0.0
        state["last_log"] = now

        logger.debug(
            f"{self.__class__.__name__} {self.export_name} sampled export: "
            f"{method_name} last return {result} in {duration} seconds "
            f"(aggregated {aggregated} call(s), average {avg_duration:.6f} seconds)"
        )

    def exit(self):
        """Close the export module."""
        logger.debug(f"Finalise export interface {self.export_name}")

    def load_common_conf(self):
        """Load the common export configuration in the Glances configuration file.

        :returns: Boolean -- True if section is found
        """
        # Read the common [export] section
        section = "export"

        opt = "exclude_fields"
        try:
            setattr(self, opt, self.config.get_list_value(section, opt))
        except NoOptionError:
            logger.debug(f"{opt} option not found in the {section} configuration section")

        # Sampled export logging: log every N calls and/or every M seconds
        for opt, default in (
            ("log_every_n", self.default_log_every_n),
            ("log_every_m_seconds", self.default_log_every_m_seconds),
        ):
            value = self.config.get_int_value(section, opt, default=default)
            if value < 0:
                logger.warning(
                    f"Invalid negative value ({value}) for {opt} in the {section} "
                    f"configuration section, fallback to {default}"
                )
                value = default
            setattr(self, opt, value)

        logger.debug(f"Load common {section} from the Glances configuration file")

        return True

    def load_conf(self, section, mandatories=["host", "port"], options=None):
        """Load the export <section> configuration in the Glances configuration file.

        :param section: name of the export section to load
        :param mandatories: a list of mandatory parameters to load
        :param options: a list of optional parameters to load

        :returns: Boolean -- True if section is found
        """
        options = options or []

        if self.config is None:
            return False

        # By default read the mandatory host:port items
        try:
            for opt in mandatories:
                setattr(self, opt, self.config.get_value(section, opt))
        except NoSectionError:
            logger.error(f"No {section} configuration found")
            return False
        except NoOptionError as e:
            logger.error(f"Error in the {section} configuration ({e})")
            return False

        # Load options
        for opt in options:
            try:
                setattr(self, opt, self.config.get_value(section, opt))
            except NoOptionError:
                logger.debug(f"{opt} option not found in the {section} configuration section")

        logger.debug(f"Load {section} from the Glances configuration file")
        logger.debug(f"{section} parameters: { ({opt: getattr(self, opt) for opt in mandatories + options}) }")

        return True

    def get_item_key(self, item):
        """Return the value of the item 'key'."""
        ret = None
        try:
            ret = item[item["key"]]
        except KeyError:
            logger.error(f"No 'key' available in {item}")
        if isinstance(ret, list):
            return ret[0]
        return ret

    def parse_tags(self, tags):
        """Parse tags into a dict.

        :param tags: a comma-separated list of 'key:value' pairs. Example: foo:bar,spam:eggs
        :return: a dict of tags. Example: {'foo': 'bar', 'spam': 'eggs'}
        """
        d_tags = {}
        if tags:
            try:
                d_tags = dict(x.split(":", 1) for x in tags.split(","))
            except ValueError:
                # one of the 'key:value' pairs was missing
                logger.info("Invalid tags passed: %s", tags)
                d_tags = {}

        return d_tags

    def normalize_for_influxdb(self, name, columns, points):
        """Normalize data for the InfluxDB's data model.

        :return: a list of measurements.
        """
        ret = []

        # Some fields should be converted to tags in order to avoid type mismatch in InfluxDB
        # and to be able to use them as filters in queries.
        FIELD_TO_TAG = ["name", "cmdline", "type"]
        # Some fields should be converted to string in order to avoid type mismatch in InfluxDB
        # Example: the 'result' field of the AMP plugin can be a string or a number depending on the AMP implementation.
        # In this case, we convert it to a string and create another field with the same name but with a suffix (_float)
        # to keep the original value.
        # See #3419 for more details.
        FIELD_TO_STRING = ["result"]

        # Build initial dict by crossing columns and point
        data_dict = dict(zip(columns, points))

        # issue1871 - Check if a key exist. If a key exist, the value of
        # the key should be used as a tag to identify the measurement.
        keys_list = [k.split(".")[0] for k in columns if k.endswith(".key")]
        if not keys_list:
            keys_list = [None]

        for measurement in keys_list:
            # Manage field
            if measurement is not None:
                fields = {
                    k.replace(f"{measurement}.", ""): data_dict[k] for k in data_dict if k.startswith(f"{measurement}.")
                }
            else:
                fields = data_dict
            # Transform to InfluxDB data model
            # https://docs.influxdata.com/influxdb/v1.8/write_protocols/line_protocol_reference/
            for k in fields:
                #  Do not export empty (None) value
                if fields[k] is None:
                    continue
                # Convert numerical to float
                try:
                    fields[k] = float(fields[k])
                except (TypeError, ValueError):
                    # Convert others to string
                    try:
                        fields[k] = str(fields[k])
                    except (TypeError, ValueError):
                        pass
                # Convert some fields to string to avoid type mismatch in InfluxDB
                if k in FIELD_TO_STRING:
                    fields[k] = str(fields[k])
            # Manage tags
            tags = self.parse_tags(self.tags)
            # Add the hostname as a tag
            tags["hostname"] = self.hostname
            if "hostname" in fields:
                fields.pop("hostname")
            # Others tags...
            if "key" in fields and fields["key"] in fields:
                # Create a tag from the key
                # Tag should be an string (see InfluxDB data model)
                tags[fields["key"]] = str(fields[fields["key"]])
                # Remove it from the field list (can not be a field and a tag)
                fields.pop(fields["key"])
            # Add name as a tag (example for the process list)
            for k in FIELD_TO_TAG:
                if k in fields:
                    tags[k] = str(fields[k])
                    # Remove it from the field list (can not be a field and a tag)
                    fields.pop(k)
            # Add the measurement to the list
            ret.append({"measurement": name, "tags": tags, "fields": fields})
        return ret

    def is_excluded(self, field):
        """Return true if the field is excluded."""
        return any(re.fullmatch(i, field, re.I) for i in (getattr(self, 'exclude_fields') or ()))

    def plugins_to_export(self, stats):
        """Return the list of plugins to export.

        :param stats: the stats object
        :return: a list of plugins to export
        """
        return [p for p in stats.getPluginsList() if p not in self.non_exportable_plugins]

    def last_exported_list(self):
        """Return the list of plugins last exported."""
        return self._last_exported_list

    def init_fields(self, stats):
        """Return fields description in order to init stats in a server."""
        if not self.export_enable:
            return False

        self._last_exported_list = self.plugins_to_export(stats)
        self._fields_description = stats.getAllFieldsDescriptionAsDict(plugin_list=self.last_exported_list())
        return self._fields_description

    def update(self, stats):
        """Update stats to a server.

        The method builds two lists: names and values and calls the export method to export the stats.

        Note: if needed this class can be overwritten.
        """
        if not self.export_enable:
            return False

        # Get all the stats & limits
        self._last_exported_list = self.plugins_to_export(stats)
        all_stats = stats.getAllExportsAsDict(plugin_list=self.last_exported_list())
        all_limits = stats.getAllLimitsAsDict(plugin_list=self.last_exported_list())

        # Loop over plugins to export
        for plugin in self.last_exported_list():
            if isinstance(all_stats[plugin], dict):
                all_stats[plugin].update(all_limits[plugin])
                # Remove the <plugin>_disable field
                all_stats[plugin].pop(f"{plugin}_disable", None)
            elif isinstance(all_stats[plugin], list):
                # TypeError: string indices must be integers (Network plugin) #1054
                for i in all_stats[plugin]:
                    i.update(all_limits[plugin])
                    # Remove the <plugin>_disable field
                    i.pop(f"{plugin}_disable", None)
            else:
                continue
            export_names, export_values = self.build_export(all_stats[plugin])
            self.export(plugin, export_names, export_values)

        return True

    def build_export(self, stats):
        """Build the export lists.
        This method builds two lists: names and values.
        """

        # Initialize export lists
        export_names = []
        export_values = []

        if isinstance(stats, dict):
            # Stats is a dict
            # Is there a key ?
            if "key" in stats and stats["key"] in stats:
                pre_key = "{}.".format(stats[stats["key"]])
            else:
                pre_key = ""
            # Walk through the dict
            # Priviously, we sort the dict but it breaks export for some plugins (see #3449)
            for key, value in stats.items():
                # Convert the key to a string and lower case it
                key = str(key).lower()
                if isinstance(value, bool):
                    value = json_dumps(value).decode()

                if isinstance(value, list):
                    value = " ".join([str(v) for v in value])

                if isinstance(value, dict):
                    item_names, item_values = self.build_export(value)
                    item_names = [pre_key + key + str(i) for i in item_names]
                    export_names += item_names
                    export_values += item_values
                else:
                    # We are on a simple value
                    if self.is_excluded(pre_key + key):
                        continue
                    export_names.append(pre_key + key)
                    export_values.append(value)
        elif isinstance(stats, list):
            # Stats is a list (of dict)
            # Recursive loop through the list
            for item in stats:
                item_names, item_values = self.build_export(item)
                export_names += item_names
                export_values += item_values
        return export_names, export_values

    def export(self, name, columns, points):
        # This method should be implemented by each exporter
        pass
