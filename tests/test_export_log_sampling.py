#
# This file is part of Glances.
#
# SPDX-License-Identifier: LGPL-3.0-only
#

"""Unit tests for the sampled _log_result_decorator of GlancesExport."""

import logging
import time

import pytest

from glances.exports.export import GlancesExport


class SampledExport(GlancesExport):
    """Minimal export class with a decorated method."""

    @GlancesExport._log_result_decorator
    def do_export(self):
        return 'ok'


@pytest.fixture
def export():
    instance = SampledExport()
    # Ensure tests are not influenced by environment variables
    instance.log_sample_calls = 0
    instance.log_sample_seconds = 0.0
    return instance


def count_debug_logs(caplog):
    return len([r for r in caplog.records if r.levelno == logging.DEBUG and 'do_export' in r.message])


class TestLogResultDecoratorSampling:
    """Tests for the configurable log sampling of _log_result_decorator."""

    def test_logs_every_call_by_default(self, export, caplog):
        """Without sampling configuration, every call is logged."""
        with caplog.at_level(logging.DEBUG):
            for _ in range(5):
                export.do_export()

        assert count_debug_logs(caplog) == 5  # nosec B101

    def test_sample_every_n_calls(self, export, caplog):
        """With log_sample_calls=N, only every Nth call is logged."""
        export.log_sample_calls = 3

        with caplog.at_level(logging.DEBUG):
            for _ in range(10):
                export.do_export()

        # Calls 3, 6 and 9 are logged
        assert count_debug_logs(caplog) == 3  # nosec B101

    def test_sample_every_m_seconds(self, export, caplog):
        """With log_sample_seconds=M, logging is rate-limited in time."""
        export.log_sample_seconds = 0.2

        with caplog.at_level(logging.DEBUG):
            export.do_export()  # logged (first call)
            export.do_export()  # skipped (too soon)
            time.sleep(0.25)
            export.do_export()  # logged (delay elapsed)
            export.do_export()  # skipped (too soon)

        assert count_debug_logs(caplog) == 2  # nosec B101

    def test_sample_calls_or_seconds(self, export, caplog):
        """Sampling triggers when either the call count or the delay matches."""
        export.log_sample_calls = 100
        export.log_sample_seconds = 0.1

        with caplog.at_level(logging.DEBUG):
            export.do_export()  # logged (delay elapsed since start)
            export.do_export()  # skipped
            time.sleep(0.15)
            export.do_export()  # logged (delay elapsed)

        assert count_debug_logs(caplog) == 2  # nosec B101

    def test_return_value_is_preserved(self, export):
        """The decorator should not alter the decorated method result."""
        export.log_sample_calls = 5

        assert export.do_export() == 'ok'  # nosec B101
