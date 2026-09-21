#
# This file is part of Glances.
#
# SPDX-FileCopyrightText: 2026 Nicolas Hennion <nicolas@nicolargo.com>
#
# SPDX-License-Identifier: LGPL-3.0-only
#

"""Shared AsyncIO event loop manager for the export modules.

Before this module every AsyncIO-based exporter (influxdb, kafka, mqtt, ...)
started its own daemon thread running its own asyncio event loop. With several
AsyncIO exporters configured this resulted in several idle threads and as many
independent event loops without any coordination, in particular during the
shutdown phase.

The :class:`EventLoopManager` is a process wide singleton. All the
:class:`~glances.exports.export_asyncio.GlancesExportAsyncio` exporters share a
single event loop running in a single daemon thread. The manager also
coordinates the graceful shutdown: exporters are stopped in the reverse order
in which they registered (dependents are typically registered after the
exporters they rely on, so they are torn down first).
"""

import asyncio
import threading

from glances.logger import logger


class EventLoopManager:
    """Manage one shared AsyncIO event loop in one background daemon thread.

    The class is a singleton: use :meth:`get_instance` to access it.
    """

    _instance = None
    _instance_lock = threading.Lock()

    def __init__(self):
        # Never call this directly, use :meth:`get_instance`.
        # The event loop is created lazily by the first registering exporter
        # so that configurations without AsyncIO exporters do not spawn any
        # extra thread.
        self._loop = None
        self._loop_thread = None
        self._loop_ready = threading.Event()
        self._loop_exception = None

        # Exporters using the shared loop, in registration order.
        self._exporters = []
        self._state_lock = threading.Lock()

        self._shutdown_started = False
        self._shutdown_completed = threading.Event()
        self._shutdown_errors = []

    @classmethod
    def get_instance(cls):
        """Return the shared :class:`EventLoopManager` singleton."""
        if cls._instance is None:
            with cls._instance_lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

    @classmethod
    def _reset_instance(cls):
        """Reset the singleton.

        This is only meant for the unit tests so each test starts from a
        clean manager. Any exporter tracked by the previous manager must have
        been stopped first.
        """
        with cls._instance_lock:
            cls._instance = None

    @property
    def loop(self):
        """Return the shared event loop (``None`` until an exporter registers)."""
        return self._loop

    def is_running(self):
        """Return True if the shared event loop is up and running."""
        return self._loop is not None and self._loop.is_running()

    def register_exporter(self, exporter):
        """Register an AsyncIO exporter and make sure the shared loop runs.

        :param exporter: a GlancesExportAsyncio instance
        :returns: the shared event loop to use for this exporter
        """
        with self._state_lock:
            if self._shutdown_started:
                raise RuntimeError("EventLoopManager is shutting down, cannot register a new exporter")
            if exporter not in self._exporters:
                self._exporters.append(exporter)
            self._ensure_loop_locked()
            return self._loop

    def unregister_exporter(self, exporter):
        """Unregister an exporter without stopping the shared loop.

        Other exporters may still need the loop, so the loop itself is left
        untouched and will be torn down by the coordinated
        :meth:`shutdown_exporter` call.
        """
        with self._state_lock:
            if exporter in self._exporters:
                self._exporters.remove(exporter)

    def run_coroutine(self, coroutine, timeout=1):
        """Schedule a coroutine on the shared loop and wait for its result.

        :param coroutine: the coroutine to run
        :param timeout: maximum time (seconds) to wait for the result
        :returns: the future result
        :raises: the exception raised by the coroutine, ``asyncio.TimeoutError``
                 on timeout or ``RuntimeError`` if the loop is not running
        """
        if not self.is_running():
            raise RuntimeError("AsyncIO event loop is not running")
        future = asyncio.run_coroutine_threadsafe(coroutine, self._loop)
        return future.result(timeout=timeout)

    def _ensure_loop_locked(self):
        """Start the background event loop thread once (lock held by caller)."""
        if self._loop_thread is not None and self._loop_thread.is_alive():
            return

        self._loop_ready.clear()
        self._loop_exception = None
        self._loop_thread = threading.Thread(
            target=self._run_event_loop,
            name="glances-asyncio-export",
            daemon=True,
        )
        self._loop_thread.start()

        if not self._loop_ready.wait(timeout=10):
            raise RuntimeError("AsyncIO event loop failed to start within timeout")
        if self._loop_exception is not None:
            raise RuntimeError(f"AsyncIO event loop creation failed: {self._loop_exception}")
        if self._loop is None:
            raise RuntimeError("AsyncIO event loop is None after initialization")

    def _run_event_loop(self):
        """Run the shared event loop in the background daemon thread."""
        try:
            self._loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._loop)
            self._loop_ready.set()
            self._loop.run_forever()
        except Exception as e:
            self._loop_exception = e
            self._loop_ready.set()
            logger.error(f"AsyncIO export event loop thread error: {e}")
        finally:
            loop = self._loop
            if loop is not None:
                pending = asyncio.all_tasks(loop)
                for task in pending:
                    task.cancel()
                if pending:
                    loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
                loop.close()

    def shutdown_exporter(self, exporter, exit_timeout=5):
        """Gracefully stop one exporter and coordinate the global shutdown.

        All the registered exporters are torn down in the reverse order of
        their registration (a dependent exporter registered after its
        dependency is therefore stopped first). Once every exporter has run
        its AsyncIO cleanup, the shared loop is stopped and its thread joined.

        Only the first exporter to call this method drives the shutdown; the
        other callers simply wait for the coordinated shutdown to complete.
        This makes the method safe to call once per exporter (as
        ``GlancesStats.end`` does) while guaranteeing the stop order.

        :param exporter: the exporter being stopped
        :param exit_timeout: timeout (seconds) for each ``_async_exit`` call
        :returns: list of exceptions caught during the exporters cleanup
        """
        with self._state_lock:
            if self._shutdown_started:
                leader = False
            else:
                leader = True
                self._shutdown_started = True
                exporters = list(reversed(self._exporters))

        if not leader:
            # Another exporter already drives the shutdown, just wait for it.
            self._shutdown_completed.wait(timeout=max(exit_timeout * 10, 60))
            return list(self._shutdown_errors)

        logger.info(f"Shutting down AsyncIO export modules ({[e.export_name for e in exporters]})")

        for exp in exporters:
            self._stop_one_exporter(exp, exit_timeout)

        self._stop_loop()
        self._shutdown_completed.set()
        return list(self._shutdown_errors)

    def _stop_one_exporter(self, exporter, exit_timeout):
        """Call the exporter AsyncIO cleanup and flag it as shut down."""
        exporter.mark_shutdown()
        if not self.is_running():
            return
        try:
            future = asyncio.run_coroutine_threadsafe(exporter._async_exit(), self._loop)
            try:
                future.result(timeout=exit_timeout)
            except Exception as e:
                logger.error(f"{exporter.export_name} Error in AsyncIO cleanup: {e}")
                self._shutdown_errors.append(e)
        except Exception as e:
            # run_coroutine_threadsafe itself failed (loop closing, ...)
            logger.error(f"{exporter.export_name} unable to schedule AsyncIO cleanup: {e}")
            self._shutdown_errors.append(e)

    def _stop_loop(self):
        """Stop the shared event loop and wait for its thread to terminate."""
        if self._loop is None:
            return
        if self._loop.is_running():
            self._loop.call_soon_threadsafe(self._loop.stop)
        if self._loop_thread is not None and self._loop_thread.is_alive():
            self._loop_thread.join(timeout=10)
            if self._loop_thread.is_alive():
                logger.error("AsyncIO export event loop thread did not stop within timeout")
        logger.debug("AsyncIO export event loop stopped")
