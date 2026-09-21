#
# Glances - An eye on your system
#
# SPDX-License-Identifier: LGPL-3.0-only
#

"""Unit tests for the sampled DEBUG logging of GlancesExport."""

import logging
import tempfile
from pathlib import Path

from glances.config import Config
from glances.exports.export import GlancesExport


class DummyExport(GlancesExport):
    """Minimal exporter exposing a decorated method."""

    @GlancesExport._log_result_decorator
    def do_export(self, value="ok"):
        return value


def _build_config(content):
    """Instantiate a Glances Config from a temporary config file."""
    with tempfile.TemporaryDirectory() as tmpdir:
        config_file = Path(tmpdir) / "glances.conf"
        config_file.write_text(content, encoding="utf-8")
        return Config(config_dir=str(config_file))


def test_default_sampling_logs_every_ten_calls(caplog):
    """By default only the 10th call should produce a DEBUG line."""
    exporter = DummyExport()
    assert exporter.log_every_n == 10  # nosec B101
    assert exporter.log_every_m_seconds == 0  # nosec B101

    with caplog.at_level(logging.DEBUG):
        for _ in range(9):
            exporter.do_export()

    sampled = [r for r in caplog.records if "sampled export" in r.message]
    assert sampled == []  # nosec B101

    with caplog.at_level(logging.DEBUG):
        exporter.do_export()

    sampled = [r for r in caplog.records if "sampled export" in r.message]
    assert len(sampled) == 1  # nosec B101
    assert "aggregated 10 call(s)" in sampled[0].message  # nosec B101
    assert "do_export" in sampled[0].message  # nosec B101


def test_sampling_state_is_per_method(caplog):
    """The call counters must not be shared between decorated methods."""

    class TwoMethodsExport(GlancesExport):
        @GlancesExport._log_result_decorator
        def export_a(self):
            return "a"

        @GlancesExport._log_result_decorator
        def export_b(self):
            return "b"

    exporter = TwoMethodsExport()
    exporter.log_every_n = 3

    with caplog.at_level(logging.DEBUG):
        exporter.export_a()
        exporter.export_a()
        exporter.export_b()
        exporter.export_b()
        exporter.export_b()

    messages = [r.message for r in caplog.records if "sampled export" in r.message]
    assert len(messages) == 1  # nosec B101
    assert "export_b" in messages[0]  # nosec B101
    assert exporter._log_sample_state["export_a"]["count"] == 2  # nosec B101


def test_log_every_n_one_logs_every_call(caplog):
    """log_every_n=1 restores the legacy one-log-per-call behavior."""
    exporter = DummyExport()
    exporter.log_every_n = 1

    with caplog.at_level(logging.DEBUG):
        for _ in range(5):
            exporter.do_export()

    sampled = [r for r in caplog.records if "sampled export" in r.message]
    assert len(sampled) == 5  # nosec B101


def test_disabled_sampling_logs_nothing(caplog):
    """log_every_n=0 and log_every_m_seconds=0 must silence the decorator."""
    exporter = DummyExport()
    exporter.log_every_n = 0
    exporter.log_every_m_seconds = 0

    with caplog.at_level(logging.DEBUG):
        for _ in range(100):
            exporter.do_export()

    sampled = [r for r in caplog.records if "sampled export" in r.message]
    assert sampled == []  # nosec B101


def test_time_based_sampling(monkeypatch, caplog):
    """With count sampling off, the threshold is driven by elapsed time."""
    exporter = DummyExport()
    exporter.log_every_n = 0
    exporter.log_every_m_seconds = 5

    fake_now = 1000.0
    monkeypatch.setattr("glances.exports.export.time.monotonic", lambda: fake_now)

    with caplog.at_level(logging.DEBUG):
        exporter.do_export()
        # Less than 5 seconds later, even with many calls, nothing is logged
        fake_now = 1003.0
        for _ in range(50):
            exporter.do_export()

    sampled = [r for r in caplog.records if "sampled export" in r.message]
    assert sampled == []  # nosec B101

    with caplog.at_level(logging.DEBUG):
        fake_now = 1005.0
        exporter.do_export()

    sampled = [r for r in caplog.records if "sampled export" in r.message]
    assert len(sampled) == 1  # nosec B101
    assert "aggregated 52 call(s)" in sampled[0].message  # nosec B101

    # The aggregation counter resets after a log line
    assert exporter._log_sample_state["do_export"]["count"] == 0  # nosec B101


def test_sampling_config_loaded_from_common_export_section():
    """The [export] section must configure both sampling parameters."""
    config = _build_config("[export]\nlog_every_n=25\nlog_every_m_seconds=3\n")
    exporter = DummyExport(config=config)

    assert exporter.log_every_n == 25  # nosec B101
    assert exporter.log_every_m_seconds == 3  # nosec B101


def test_sampling_defaults_when_section_missing():
    """Without [export] options the class defaults must be used."""
    config = _build_config("[global]\n")
    exporter = DummyExport(config=config)

    assert exporter.log_every_n == DummyExport.default_log_every_n  # nosec B101
    assert exporter.log_every_m_seconds == DummyExport.default_log_every_m_seconds  # nosec B101


def test_negative_config_value_falls_back_to_default(caplog):
    """A negative value must be rejected in favor of the default."""
    config = _build_config("[export]\nlog_every_n=-1\n")
    with caplog.at_level(logging.WARNING):
        exporter = DummyExport(config=config)

    assert exporter.log_every_n == DummyExport.default_log_every_n  # nosec B101
    assert any("Invalid negative value" in r.message for r in caplog.records)  # nosec B101
