#
# This file is part of Glances.
#
# SPDX-FileCopyrightText: 2026 Nicolas Hennion <nicolas@nicolargo.com>
#
# SPDX-License-Identifier: LGPL-3.0-only
#

"""
Shared AsyncIO event loop manager for Glances exports.

Instead of spawning one event loop thread per export module, all
AsyncIO-based exporters share a common pool of event loop threads
managed by the EventLoopManager singleton.
"""

import asyncio
import atexit
import os
import threading

from glances.logger import logger


class _EventLoopSlot:
    """A single event loop running in a dedicated daemon thread."""

    def __init__(self, name):
        self.name = name
        self.loop = None
        self.refcount = 0
        self._ready = threading.Event()
        self._exception = None
        self.thread = threading.Thread(target=self._run, name=name, daemon=True)
        self.thread.start()
        if not self._ready.wait(timeout=10):
            raise RuntimeError(f"Event loop thread {name} failed to start within timeout")
        if self._exception is not None:
            raise RuntimeError(f"Event loop thread {name} failed to start: {self._exception}")

    def _run(self):
        """Run the event loop in the background thread."""
        try:
            self.loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self.loop)
            self._ready.set()
            self.loop.run_forever()
        except Exception as e:
            self._exception = e
            self._ready.set()
            logger.error(f"Event loop thread {self.name} error: {e}")
        finally:
            if self.loop is not None:
                # Clean up pending tasks
                pending = asyncio.all_tasks(self.loop)
                for task in pending:
                    task.cancel()
                if pending:
                    self.loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
                self.loop.close()

    def stop(self, timeout=5):
        """Stop the event loop and wait for the thread to finish."""
        if self.loop is not None and self.loop.is_running():
            self.loop.call_soon_threadsafe(self.loop.stop)
        if self.thread.is_alive():
            self.thread.join(timeout=timeout)


class EventLoopManager:
    """Singleton managing a shared pool of AsyncIO event loop threads.

    All AsyncIO-based export modules acquire their event loop from this
    manager instead of creating a dedicated thread per exporter.

    Exporters registered with register_exporter() are stopped in dependency
    order during shutdown(): higher shutdown_priority first, then LIFO for
    equal priorities (dependents stop before the exporters they depend on).
    """

    _instance = None
    _instance_lock = threading.Lock()

    DEFAULT_POOL_SIZE = 1

    def __init__(self, pool_size=None):
        if pool_size is None:
            pool_size = int(os.environ.get('GLANCES_EXPORT_EVENT_LOOP_POOL_SIZE', self.DEFAULT_POOL_SIZE))
        self._pool_size = max(1, pool_size)
        self._pool = []
        # Registered exporters: list of (shutdown_priority, sequence, exporter)
        self._exporters = []
        self._sequence = 0
        self._lock = threading.Lock()
        self._shutdown = False

    @classmethod
    def get_instance(cls, pool_size=None):
        """Return the shared EventLoopManager instance (create it if needed)."""
        with cls._instance_lock:
            if cls._instance is None or cls._instance._shutdown:
                cls._instance = cls(pool_size=pool_size)
                atexit.register(cls._instance._atexit_shutdown)
            return cls._instance

    @classmethod
    def shutdown_instance(cls):
        """Shutdown the shared instance if it exists (no-op otherwise)."""
        with cls._instance_lock:
            instance = cls._instance
        if instance is not None:
            instance.shutdown()

    @classmethod
    def reset_instance(cls):
        """Shutdown and drop the shared instance (mainly for tests)."""
        with cls._instance_lock:
            instance, cls._instance = cls._instance, None
        if instance is not None:
            instance.shutdown()

    @property
    def pool_size(self):
        """Return the configured event loop pool size."""
        return self._pool_size

    @property
    def loop_count(self):
        """Return the number of event loop threads currently started."""
        with self._lock:
            return len(self._pool)

    def _ensure_pool(self):
        while len(self._pool) < self._pool_size:
            self._pool.append(_EventLoopSlot(f"glances-export-loop-{len(self._pool)}"))

    def acquire(self):
        """Acquire a shared event loop from the pool (least-loaded first)."""
        with self._lock:
            if self._shutdown:
                raise RuntimeError("EventLoopManager has been shut down")
            self._ensure_pool()
            slot = min(self._pool, key=lambda s: s.refcount)
            slot.refcount += 1
            logger.debug(f"Acquired event loop {slot.name} (refcount={slot.refcount})")
            return slot.loop

    def release(self, loop):
        """Release a previously acquired event loop."""
        with self._lock:
            for slot in self._pool:
                if slot.loop is loop and slot.refcount > 0:
                    slot.refcount -= 1
                    logger.debug(f"Released event loop {slot.name} (refcount={slot.refcount})")
                    break

    def register_exporter(self, exporter, shutdown_priority=0):
        """Register an exporter for ordered graceful shutdown.

        :param exporter: the exporter instance (must provide an exit() method)
        :param shutdown_priority: exporters with a higher priority value are
            stopped first; exporters with the same priority are stopped in
            reverse registration order (LIFO)
        """
        with self._lock:
            self._sequence += 1
            self._exporters.append((shutdown_priority, self._sequence, exporter))

    def unregister_exporter(self, exporter):
        """Remove an exporter from the graceful shutdown registry."""
        with self._lock:
            self._exporters = [entry for entry in self._exporters if entry[2] is not exporter]

    def shutdown(self):
        """Gracefully stop registered exporters (dependency order) then the loops."""
        with self._lock:
            if self._shutdown:
                return
            self._shutdown = True
            # Higher priority first, then LIFO for equal priorities
            exporters = sorted(self._exporters, key=lambda entry: (-entry[0], -entry[1]))
            self._exporters = []
        for _, _, exporter in exporters:
            try:
                exporter.exit()
            except Exception as e:
                logger.error(f"Error while shutting down exporter {exporter}: {e}")
        with self._lock:
            pool, self._pool = self._pool, []
        for slot in pool:
            slot.stop()
        logger.debug("EventLoopManager shutdown complete")

    def _atexit_shutdown(self):
        try:
            self.shutdown()
        except Exception as e:
            logger.error(f"Error during EventLoopManager atexit shutdown: {e}")
