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
import threading
from abc import abstractmethod

from glances.exports.event_loop_manager import EventLoopManager
from glances.exports.export import GlancesExport
from glances.logger import logger


class GlancesExportAsyncio(GlancesExport):
    """Abstract class for AsyncIO-based export modules.

    This class uses a shared event loop (managed by the EventLoopManager
    singleton) running in a background thread pool, allowing child classes
    to use AsyncIO operations for exporting data. All AsyncIO exporters
    share the same event loop(s) instead of spawning one thread each.

    Child classes must implement:
    - async _async_init(): AsyncIO initialization (e.g., connection setup)
    - async _async_exit(): AsyncIO cleanup (e.g., disconnection)
    - async _async_export(name, columns, points): AsyncIO export operation
    """

    # Shutdown priority used when the shared EventLoopManager stops the
    # exporters in dependency order (higher value = stopped earlier).
    # Child classes may override this class attribute.
    shutdown_priority = 0

    def __init__(self, config=None, args=None):
        """Init the AsyncIO export interface."""
        super().__init__(config=config, args=args)

        self._shutdown = False
        self._exit_lock = threading.Lock()

        # Acquire a shared event loop from the manager (no dedicated thread
        # per exporter) and register for ordered graceful shutdown
        self._loop_manager = EventLoopManager.get_instance()
        self.loop = self._loop_manager.acquire()
        self._loop_manager.register_exporter(self, shutdown_priority=self.shutdown_priority)

        # Call child class AsyncIO initialization
        future = asyncio.run_coroutine_threadsafe(self._async_init(), self.loop)
        try:
            future.result(timeout=10)
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

        This method is called during exit() before releasing the event loop.
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

    def exit(self):
        """Close the AsyncIO export module."""
        with self._exit_lock:
            if self._shutdown:
                return
            self._shutdown = True

        super().exit()
        logger.info(f"{self.export_name} AsyncIO export shutting down")

        # Call child class cleanup on the shared event loop
        if self.loop and self.loop.is_running():
            future = asyncio.run_coroutine_threadsafe(self._async_exit(), self.loop)
            try:
                future.result(timeout=5)
            except Exception as e:
                logger.error(f"{self.export_name} Error in AsyncIO cleanup: {e}")

        # Unregister from the graceful shutdown registry and release the
        # shared event loop (the loop itself is stopped by the manager)
        self._loop_manager.unregister_exporter(self)
        self._loop_manager.release(self.loop)

        logger.debug(f"{self.export_name} AsyncIO export shutdown complete")

    def export(self, name, columns, points):
        """Export data using AsyncIO.

        This method bridges the synchronous export() interface with
        the AsyncIO _async_export() implementation.
        """
        if self._shutdown:
            logger.debug(f"{self.export_name} Export called during shutdown, skipping")
            return

        if not self.loop or not self.loop.is_running():
            logger.error(f"{self.export_name} AsyncIO event loop is not running")
            return

        # Submit the export operation to the shared event loop
        try:
            future = asyncio.run_coroutine_threadsafe(self._async_export(name, columns, points), self.loop)
            # Don't block forever - use a short timeout
            future.result(timeout=1)
        except asyncio.TimeoutError:
            logger.warning(f"{self.export_name} AsyncIO export timeout for {name}")
        except Exception as e:
            logger.error(f"{self.export_name} AsyncIO export error for {name}: {e}", exc_info=True)
