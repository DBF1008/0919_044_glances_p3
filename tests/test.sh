#!/usr/bin/env bash
#
# Glances manual unit test runner.
#
# This script runs the Python unit tests related to the export/event-loop
# refactoring (and, by default, the whole safe unit test suite). It must be
# launched manually:
#
#     ./tests/test.sh
#
# Usage:
#     ./tests/test.sh                 # run the refactoring unit tests
#     ./tests/test.sh --all           # run the whole unit test suite
#     ./tests/test.sh --file PATH     # run one specific test file
#     ./tests/test.sh --python BIN    # use a specific python interpreter
#     ./tests/test.sh --integration   # additionally run the docker based
#                                     # exporter shell tests (test_export_*.sh)
#
# The docker based exporter shell tests (test_export_nats.sh, ...) require
# docker and jq and are never started automatically.

set -euo pipefail

# Move to the repository root (parent of the tests/ directory)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${REPO_ROOT}"

# Python interpreter: $PYTHON env var, then --python option, then python3
PYTHON_BIN="${PYTHON:-python3}"
RUN_ALL=0
RUN_INTEGRATION=0
TARGET_FILE=""

usage() {
    grep '^#' "$0" | grep -v '^#!' | sed 's/^# \{0,1\}//'
    exit "${1:-0}"
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --all)
            RUN_ALL=1
            shift
            ;;
        --file)
            TARGET_FILE="$2"
            shift 2
            ;;
        --python)
            PYTHON_BIN="$2"
            shift 2
            ;;
        --integration)
            RUN_INTEGRATION=1
            shift
            ;;
        -h|--help)
            usage 0
            ;;
        *)
            echo "Unknown option: $1"
            usage 1
            ;;
    esac
done

# Unit tests introduced/updated by the refactoring
REFACTORING_TESTS=(
    "tests/test_event_loop_manager.py"
    "tests/test_export_asyncio.py"
    "tests/test_export_sampled_logging.py"
    "tests/test_stats_loaders.py"
    "tests/test_glances_stats.py"
)

echo "=================================================================="
echo " Glances unit tests"
echo " Python interpreter: ${PYTHON_BIN}"
echo " Repository root:    ${REPO_ROOT}"
echo "=================================================================="

# Sanity check: the interpreter must be able to import glances
if ! "${PYTHON_BIN}" -c "import glances" >/dev/null 2>&1; then
    echo "ERROR: 'glances' cannot be imported with ${PYTHON_BIN}."
    echo "Install the dependencies first (example: pip install -r requirements.txt)."
    exit 1
fi

if ! "${PYTHON_BIN}" -m pytest --version >/dev/null 2>&1; then
    echo "ERROR: pytest is not available for ${PYTHON_BIN}."
    echo "Install it first (example: pip install pytest)."
    exit 1
fi

UNIT_STATUS=0

if [[ -n "${TARGET_FILE}" ]]; then
    echo ""
    echo "Running single test file: ${TARGET_FILE}"
    "${PYTHON_BIN}" -m pytest "${TARGET_FILE}" -v || UNIT_STATUS=1
elif [[ "${RUN_ALL}" -eq 1 ]]; then
    echo ""
    echo "Running the whole unit test suite..."
    "${PYTHON_BIN}" -m pytest tests/ -v || UNIT_STATUS=1
else
    echo ""
    echo "Running the refactoring unit tests:"
    printf '  - %s\n' "${REFACTORING_TESTS[@]}"
    "${PYTHON_BIN}" -m pytest "${REFACTORING_TESTS[@]}" -v || UNIT_STATUS=1
fi

INTEGRATION_STATUS=0
if [[ "${RUN_INTEGRATION}" -eq 1 ]]; then
    echo ""
    echo "=================================================================="
    echo " Running docker based exporter integration tests"
    echo "=================================================================="
    for integration_test in tests/test_export_*.sh; do
        [[ -e "${integration_test}" ]] || continue
        echo ""
        echo "------------------------------------------------------------------"
        echo " ${integration_test}"
        echo "------------------------------------------------------------------"
        if bash "${integration_test}"; then
            echo "PASS: ${integration_test}"
        else
            echo "FAIL: ${integration_test}"
            INTEGRATION_STATUS=1
        fi
    done
fi

echo ""
echo "=================================================================="
echo " Test summary"
echo "=================================================================="
if [[ "${UNIT_STATUS}" -eq 0 ]]; then
    echo "  Unit tests:          PASS"
else
    echo "  Unit tests:          FAIL"
fi
if [[ "${RUN_INTEGRATION}" -eq 1 ]]; then
    if [[ "${INTEGRATION_STATUS}" -eq 0 ]]; then
        echo "  Integration tests:   PASS"
    else
        echo "  Integration tests:   FAIL"
    fi
fi

if [[ "${UNIT_STATUS}" -ne 0 || "${INTEGRATION_STATUS}" -ne 0 ]]; then
    exit 1
fi

echo "All requested tests passed."
