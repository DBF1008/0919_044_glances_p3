#
# This file is part of Glances.
#
# SPDX-FileCopyrightText: 2026 Nicolas Hennion <nicolas@nicolargo.com>
#
# SPDX-License-Identifier: LGPL-3.0-only
#

"""
I am your son...
...abstract class for AsyncIO-based Glances exports.
"""

import asyncio
from abc import abstractmethod

from glances.exports.event_loop_manager import EventLoopManager
from glances.exports.export import GlancesExport
from glances.logger import logger


class GlancesExportAsyncio(GlancesExport):
    """Abstract class for AsyncIO-based export modules.

    All the AsyncIO exporters share a single event loop running in a single
    background thread. The loop life cycle and the coordinated shutdown are
    managed by the :class:`~glances.exports.event_loop_manager.EventLoopManager`
    singleton, so configuring several AsyncIO exporters (influxdb, kafka,
    mqtt, ...) no longer spawns one event loop thread per exporter.

    Child classes must implement:
    - async _async_init(): AsyncIO initialization (e.g., connection setup)
    - async _async_exit(): AsyncIO cleanup (e.g., disconnection)
    - async _async_export(name, columns, points): AsyncIO export operation
    """

    def __init__(self, config=None, args=None):
        """Init the AsyncIO export interface."""
        super().__init__(config=config, args=args)

        # Shared event loop manager (process wide singleton)
        self._event_loop_manager = EventLoopManager.get_instance()
        self._shutdown = False

        # Register the exporter and get the shared, already running, loop
        self.loop = self._event_loop_manager.register_exporter(self)

        # Call child class AsyncIO initialization
        try:
            self._event_loop_manager.run_coroutine(self._async_init(), timeout=10)
            logger.debug(f"{self.export_name} AsyncIO export initialized successfully")
        except Exception as e:
            logger.warning(f"{self.export_name} AsyncIO initialization failed: {e}. Will retry in background.")

    @abstractmethod
    async def _async_init(self):
        """AsyncIO initialization method.

        Child classes should implement this method to perform AsyncIO-based
        initialization such as connecting to servers, setting up clients, etc.

        This method is called once during __init__ after the event loop is ready.
        """
        pass

    @abstractmethod
    async def _async_exit(self):
        """AsyncIO cleanup method.

        Child classes should implement this method to perform AsyncIO-based
        cleanup such as disconnecting from servers, closing clients, etc.

        This method is called during exit() before stopping the event loop.
        """
        pass

    @abstractmethod
    async def _async_export(self, name, columns, points):
        """AsyncIO export method.

        Child classes must implement this method to perform the actual
        export operation using AsyncIO.

        :param name: plugin name
        :param columns: list of column names
        :param points: list of values corresponding to columns
        """
        pass

    def mark_shutdown(self):
        """Flag the exporter as shutting down (called by the manager)."""
        self._shutdown = True

    def exit(self):
        """Close the AsyncIO export module.

        The first exporter exiting drives the coordinated shutdown: every
        AsyncIO exporter runs its ``_async_exit`` cleanup in the reverse
        registration order and, finally, the shared event loop is stopped.
        """
        super().exit()
        logger.info(f"{self.export_name} AsyncIO export shutting down")
        self._event_loop_manager.shutdown_exporter(self)
        logger.debug(f"{self.export_name} AsyncIO export shutdown complete")

    def export(self, name, columns, points):
        """Export data using AsyncIO.

        This method bridges the synchronous export() interface with
        the AsyncIO _async_export() implementation.
        """
        if self._shutdown:
            logger.debug(f"{self.export_name} Export called during shutdown, skipping")
            return

        if not self._event_loop_manager.is_running():
            logger.error(f"{self.export_name} AsyncIO event loop is not running")
            return

        # Submit the export operation to the shared background event loop
        try:
            self._event_loop_manager.run_coroutine(self._async_export(name, columns, points), timeout=1)
        except asyncio.TimeoutError:
            logger.warning(f"{self.export_name} AsyncIO export timeout for {name}")
        except Exception as e:
            logger.error(f"{self.export_name} AsyncIO export error for {name}: {e}", exc_info=True)
