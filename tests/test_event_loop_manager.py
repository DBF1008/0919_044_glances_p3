#
# This file is part of Glances.
#
# SPDX-License-Identifier: LGPL-3.0-only
#

"""Unit tests for the shared EventLoopManager and GlancesExportAsyncio."""

import asyncio
import threading

import pytest

from glances.exports.event_loop_manager import EventLoopManager
from glances.exports.export_asyncio import GlancesExportAsyncio


@pytest.fixture(autouse=True)
def reset_manager():
    """Ensure each test starts and ends with a fresh manager instance."""
    EventLoopManager.reset_instance()
    yield
    EventLoopManager.reset_instance()


class DummyAsyncExport(GlancesExportAsyncio):
    """Minimal AsyncIO exporter for testing purposes."""

    def __init__(self, *args, **kwargs):
        self.init_called = 0
        self.exit_called = 0
        self.exported = []
        super().__init__(*args, **kwargs)

    async def _async_init(self):
        self.init_called += 1

    async def _async_exit(self):
        self.exit_called += 1

    async def _async_export(self, name, columns, points):
        self.exported.append((name, columns, points))


class TestEventLoopManager:
    """Tests for the EventLoopManager singleton."""

    def test_singleton_instance(self):
        """get_instance should always return the same instance."""
        manager1 = EventLoopManager.get_instance()
        manager2 = EventLoopManager.get_instance()

        assert manager1 is manager2  # nosec B101

    def test_default_pool_size_is_one(self):
        """By default, a single shared event loop thread should be used."""
        manager = EventLoopManager.get_instance()

        assert manager.pool_size == 1  # nosec B101

    def test_acquire_starts_loop_thread(self):
        """Acquiring a loop should start the shared daemon thread."""
        manager = EventLoopManager.get_instance()
        loop = manager.acquire()

        assert loop is not None  # nosec B101
        assert loop.is_running()  # nosec B101
        assert manager.loop_count == 1  # nosec B101

    def test_shared_loop_between_exporters(self):
        """Multiple exporters should share the same event loop (pool size 1)."""
        manager = EventLoopManager.get_instance()
        loop1 = manager.acquire()
        loop2 = manager.acquire()

        assert loop1 is loop2  # nosec B101
        assert manager.loop_count == 1  # nosec B101

    def test_pool_distributes_loops(self):
        """With a pool size > 1, acquisitions should be distributed (least-loaded first)."""
        manager = EventLoopManager.get_instance(pool_size=2)
        loop1 = manager.acquire()
        loop2 = manager.acquire()

        assert loop1 is not loop2  # nosec B101
        assert manager.loop_count == 2  # nosec B101
        # Third acquisition reuses the least-loaded loop
        loop3 = manager.acquire()
        assert loop3 in (loop1, loop2)  # nosec B101
        assert manager.loop_count == 2  # nosec B101

    def test_acquire_after_shutdown_raises(self):
        """Acquiring a loop after shutdown should raise an error."""
        manager = EventLoopManager.get_instance()
        manager.shutdown()

        with pytest.raises(RuntimeError):
            manager.acquire()

    def test_shutdown_is_idempotent(self):
        """Calling shutdown twice should not fail."""
        manager = EventLoopManager.get_instance()
        manager.acquire()
        manager.shutdown()
        manager.shutdown()

    def test_shutdown_stops_exporters_in_dependency_order(self):
        """Exporters should be stopped by priority (higher first), then LIFO."""
        manager = EventLoopManager.get_instance()
        stopped = []

        class FakeExporter:
            def __init__(self, name):
                self.name = name

            def exit(self):
                stopped.append(self.name)

        first = FakeExporter('first')
        second = FakeExporter('second')
        high_priority = FakeExporter('high_priority')

        manager.register_exporter(first)
        manager.register_exporter(second)
        manager.register_exporter(high_priority, shutdown_priority=10)

        manager.shutdown()

        # Higher priority stopped first, then LIFO for equal priorities
        assert stopped == ['high_priority', 'second', 'first']  # nosec B101

    def test_unregister_exporter(self):
        """Unregistered exporters should not be stopped by shutdown."""
        manager = EventLoopManager.get_instance()
        stopped = []

        class FakeExporter:
            def exit(self):
                stopped.append(True)

        exporter = FakeExporter()
        manager.register_exporter(exporter)
        manager.unregister_exporter(exporter)
        manager.shutdown()

        assert stopped == []  # nosec B101

    def test_shutdown_stops_loop_threads(self):
        """All event loop threads should be stopped after shutdown."""
        manager = EventLoopManager.get_instance()
        manager.acquire()
        threads_before = [t for t in threading.enumerate() if t.name.startswith('glances-export-loop-')]
        assert len(threads_before) == 1  # nosec B101

        manager.shutdown()

        for thread in threads_before:
            thread.join(timeout=5)
            assert not thread.is_alive()  # nosec B101


class TestGlancesExportAsyncio:
    """Tests for the GlancesExportAsyncio class using the shared manager."""

    def test_exporters_share_the_same_loop(self):
        """Two AsyncIO exporters should share one event loop thread."""
        export1 = DummyAsyncExport()
        export2 = DummyAsyncExport()

        assert export1.loop is export2.loop  # nosec B101
        assert EventLoopManager.get_instance().loop_count == 1  # nosec B101

        export1.exit()
        export2.exit()

    def test_async_init_is_called(self):
        """The child _async_init should be executed on the shared loop."""
        export = DummyAsyncExport()

        assert export.init_called == 1  # nosec B101
        export.exit()

    def test_export_submits_coroutine_to_shared_loop(self):
        """export() should run _async_export on the shared event loop."""
        export = DummyAsyncExport()
        export.export('cpu', ['user'], [1.0])

        assert export.exported == [('cpu', ['user'], [1.0])]  # nosec B101
        export.exit()

    def test_exit_calls_async_exit_once(self):
        """exit() should be idempotent and call _async_exit only once."""
        export = DummyAsyncExport()
        export.exit()
        export.exit()

        assert export.exit_called == 1  # nosec B101

    def test_export_after_exit_is_skipped(self):
        """export() after exit() should be a no-op."""
        export = DummyAsyncExport()
        export.exit()
        export.export('cpu', ['user'], [1.0])

        assert export.exported == []  # nosec B101

    def test_manager_shutdown_stops_registered_exporters(self):
        """Manager shutdown should stop registered exporters in order."""
        manager = EventLoopManager.get_instance()
        export1 = DummyAsyncExport()
        export2 = DummyAsyncExport()

        manager.shutdown()

        assert export1.exit_called == 1  # nosec B101
        assert export2.exit_called == 1  # nosec B101

    def test_exported_coroutine_really_runs_on_shared_loop(self):
        """Sanity check: coroutines run on the manager's loop thread."""
        export = DummyAsyncExport()
        thread_names = []

        async def record_thread():
            thread_names.append(threading.current_thread().name)

        future = asyncio.run_coroutine_threadsafe(record_thread(), export.loop)
        future.result(timeout=5)

        assert thread_names == ['glances-export-loop-0']  # nosec B101
        export.exit()
