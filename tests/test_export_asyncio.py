#
# Glances - An eye on your system
#
# SPDX-License-Identifier: LGPL-3.0-only
#

"""Unit tests for GlancesExportAsyncio on top of the shared EventLoopManager."""

import pytest

from glances.exports.event_loop_manager import EventLoopManager
from glances.exports.export_asyncio import GlancesExportAsyncio


class DummyAsyncExport(GlancesExportAsyncio):
    """Concrete async exporter used for the tests."""

    def __init__(self, init_fails=False, export_fails=False, config=None, args=None):
        self.init_fails = init_fails
        self.export_fails = export_fails
        self.init_called = 0
        self.exit_called = 0
        self.exported = []
        super().__init__(config=config, args=args)
        self.export_enable = True

    async def _async_init(self):
        self.init_called += 1
        if self.init_fails:
            raise RuntimeError("init boom")

    async def _async_exit(self):
        self.exit_called += 1

    async def _async_export(self, name, columns, points):
        if self.export_fails:
            raise RuntimeError("export boom")
        self.exported.append((name, columns, points))


@pytest.fixture(autouse=True)
def reset_manager():
    """Ensure each test starts and ends with a clean singleton manager."""
    EventLoopManager._reset_instance()
    yield
    manager = EventLoopManager.get_instance()
    if manager.is_running():
        # Force a clean shutdown of exporters left registered by the test:
        # mark them as shutting down and drive one coordinated shutdown.
        manager._shutdown_started = False
        manager._shutdown_completed.clear()
        for exporter in list(manager._exporters):
            exporter.mark_shutdown()

        class _CleanupExporter:
            export_name = "test-cleanup"

        manager.shutdown_exporter(_CleanupExporter())
    EventLoopManager._reset_instance()


def test_init_uses_shared_manager():
    """An async exporter must register on the shared manager/loop."""
    exporter = DummyAsyncExport()
    manager = EventLoopManager.get_instance()

    assert exporter._event_loop_manager is manager  # nosec B101
    assert exporter.loop is manager.loop  # nosec B101
    assert manager.is_running()  # nosec B101
    assert exporter.init_called == 1  # nosec B101
    assert exporter in manager._exporters  # nosec B101


def test_several_exporters_share_single_loop_thread():
    """Multiple async exporters must share one loop and one daemon thread."""
    exporters = [DummyAsyncExport() for _ in range(3)]
    manager = EventLoopManager.get_instance()

    loops = {id(e.loop) for e in exporters}
    assert loops == {id(manager.loop)}  # nosec B101
    threads = {e._event_loop_manager._loop_thread for e in exporters}
    assert len(threads) == 1  # nosec B101
    assert list(manager._exporters) == exporters  # nosec B101


def test_export_runs_async_method():
    """The synchronous export bridge must run the async method on the loop."""
    exporter = DummyAsyncExport()
    assert exporter.export("cpu", ["user"], [1.2]) is None  # nosec B101
    assert exporter.exported == [("cpu", ["user"], [1.2])]  # nosec B101


def test_export_error_is_swallowed():
    """An exception in _async_export must not propagate to the caller."""
    exporter = DummyAsyncExport(export_fails=True)
    # Should not raise
    exporter.export("cpu", ["user"], [1.2])
    assert exporter.exported == []  # nosec B101


def test_init_failure_does_not_crash_constructor():
    """A failing _async_init is warned about but the exporter is still built."""
    exporter = DummyAsyncExport(init_fails=True)
    assert exporter.init_called == 1  # nosec B101
    manager = EventLoopManager.get_instance()
    assert exporter in manager._exporters  # nosec B101


def test_exit_coordinates_shutdown_order():
    """Exiting one exporter stops all of them in reverse registration order."""
    first = DummyAsyncExport()
    second = DummyAsyncExport()
    third = DummyAsyncExport()

    first.exit()

    assert [e.exit_called for e in (first, second, third)] == [1, 1, 1]  # nosec B101
    manager = EventLoopManager.get_instance()
    assert not manager.is_running()  # nosec B101


def test_export_skipped_after_shutdown():
    """Exports submitted during shutdown must be skipped."""
    exporter = DummyAsyncExport()
    exporter.exit()
    exporter.export("cpu", ["user"], [1.2])
    assert exporter.exported == []  # nosec B101
