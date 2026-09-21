#
# Glances - An eye on your system
#
# SPDX-License-Identifier: LGPL-3.0-only
#

"""Unit tests for the shared AsyncIO EventLoopManager."""

import asyncio
import threading

import pytest

from glances.exports.event_loop_manager import EventLoopManager


class DummyExporter:
    """Minimal exporter used to exercise the manager coordination logic."""

    def __init__(self, name, fail_exit=False):
        self.export_name = name
        self.fail_exit = fail_exit
        self.shutdown_marked = False
        self.exit_order = None

    def mark_shutdown(self):
        self.shutdown_marked = True

    async def _async_exit(self):
        if self.fail_exit:
            raise RuntimeError(f"{self.export_name} boom")
        await asyncio.sleep(0)


@pytest.fixture
def manager():
    """Return a fresh singleton manager and reset it after the test."""
    mgr = EventLoopManager.get_instance()
    yield mgr
    # Ensure the loop is stopped so the next test gets a clean state
    try:
        if mgr.is_running():
            for exporter in list(mgr._exporters):
                exporter.mark_shutdown()
            mgr._shutdown_started = False
            mgr._shutdown_completed.clear()

            class _CleanupExporter:
                export_name = "test-cleanup"

                def mark_shutdown(self):
                    pass

            mgr.shutdown_exporter(_CleanupExporter())
    except Exception:
        pass
    EventLoopManager._reset_instance()


def test_singleton_get_instance(manager):
    """get_instance must always return the same object."""
    assert EventLoopManager.get_instance() is manager  # nosec B101
    assert EventLoopManager.get_instance() is EventLoopManager.get_instance()  # nosec B101


def test_loop_created_lazily(manager):
    """No event loop thread should exist before the first registration."""
    assert manager.loop is None  # nosec B101
    assert not manager.is_running()  # nosec B101

    exporter = DummyExporter("first")
    loop = manager.register_exporter(exporter)

    assert loop is manager.loop  # nosec B101
    assert manager.is_running()  # nosec B101
    assert isinstance(manager._loop_thread, threading.Thread)  # nosec B101
    assert manager._loop_thread.daemon is True  # nosec B101


def test_exporters_share_one_loop(manager):
    """Several exporters must be served by the very same loop/thread."""
    exporter_a = DummyExporter("influxdb")
    exporter_b = DummyExporter("kafka")
    exporter_c = DummyExporter("mqtt")

    loop_a = manager.register_exporter(exporter_a)
    thread_a = manager._loop_thread
    loop_b = manager.register_exporter(exporter_b)
    loop_c = manager.register_exporter(exporter_c)

    assert loop_a is loop_b is loop_c is manager.loop  # nosec B101
    assert manager._loop_thread is thread_a  # nosec B101
    assert manager._exporters == [exporter_a, exporter_b, exporter_c]  # nosec B101


def test_run_coroutine_executes_on_shared_loop(manager):
    """Coroutines submitted through the manager run on the shared loop."""
    exporter = DummyExporter("nats")
    manager.register_exporter(exporter)

    async def echo(value):
        await asyncio.sleep(0)
        return value

    assert manager.run_coroutine(echo("ok")) == "ok"  # nosec B101

    async def current_loop():
        return asyncio.get_running_loop()

    assert manager.run_coroutine(current_loop()) is manager.loop  # nosec B101


def test_run_coroutine_timeout(manager):
    """A long running coroutine must raise asyncio.TimeoutError."""
    exporter = DummyExporter("slow")
    manager.register_exporter(exporter)

    async def slow():
        await asyncio.sleep(5)

    with pytest.raises(asyncio.TimeoutError):
        manager.run_coroutine(slow(), timeout=0.1)


def test_shutdown_reverse_registration_order(manager):
    """Exporters must be stopped in the reverse registration order."""
    order = []

    class OrderedExporter(DummyExporter):
        async def _async_exit(self):
            order.append(self.export_name)
            await asyncio.sleep(0)

    exporter_a = OrderedExporter("a")
    exporter_b = OrderedExporter("b")
    exporter_c = OrderedExporter("c")
    manager.register_exporter(exporter_a)
    manager.register_exporter(exporter_b)
    manager.register_exporter(exporter_c)

    # The first exporter to exit drives the coordinated shutdown
    errors = manager.shutdown_exporter(exporter_a, exit_timeout=2)

    assert order == ["c", "b", "a"]  # nosec B101
    assert errors == []  # nosec B101
    assert all(e.shutdown_marked for e in (exporter_a, exporter_b, exporter_c))  # nosec B101
    assert not manager.is_running()  # nosec B101
    assert not manager._loop_thread.is_alive()  # nosec B101


def test_shutdown_is_coordinated(manager):
    """Every exporter calling shutdown must wait for the single coordinated one."""
    exporter_a = DummyExporter("a")
    exporter_b = DummyExporter("b")
    manager.register_exporter(exporter_a)
    manager.register_exporter(exporter_b)

    errors_a = manager.shutdown_exporter(exporter_a, exit_timeout=2)
    # The loop is already stopped; this call must only wait, not crash
    errors_b = manager.shutdown_exporter(exporter_b, exit_timeout=2)

    assert errors_a == errors_b == []  # nosec B101
    assert manager._shutdown_completed.is_set()  # nosec B101


def test_shutdown_collects_cleanup_errors(manager):
    """An exception in _async_exit must be collected, not stop the shutdown."""
    good = DummyExporter("good")
    bad = DummyExporter("bad", fail_exit=True)
    manager.register_exporter(good)
    manager.register_exporter(bad)

    errors = manager.shutdown_exporter(good, exit_timeout=2)

    assert len(errors) == 1  # nosec B101
    assert "bad boom" in str(errors[0])  # nosec B101
    assert not manager.is_running()  # nosec B101


def test_register_after_shutdown_rejected(manager):
    """A new exporter cannot register once the shutdown has started."""
    exporter = DummyExporter("only")
    manager.register_exporter(exporter)
    manager.shutdown_exporter(exporter, exit_timeout=2)

    with pytest.raises(RuntimeError):
        manager.register_exporter(DummyExporter("late"))


def test_unregister_exporter(manager):
    """unregister_exporter removes the exporter but keeps the shared loop alive."""
    exporter_a = DummyExporter("a")
    exporter_b = DummyExporter("b")
    manager.register_exporter(exporter_a)
    manager.register_exporter(exporter_b)

    manager.unregister_exporter(exporter_a)

    assert exporter_a not in manager._exporters  # nosec B101
    assert exporter_b in manager._exporters  # nosec B101
    assert manager.is_running()  # nosec B101
