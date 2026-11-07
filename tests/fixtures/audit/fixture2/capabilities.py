# This module simulates capability registration but the handlers are never selected

# Simulated capability registry - these handlers are "registered" but never called
HANDLER_REGISTRY = {
    "audit_report": "_handle_audit_report",  # registered but unreachable
    "data_export": "_handle_data_export",  # registered but unreachable
    "system_check": "_handle_system_check",  # registered but unreachable
}

# These are the actually selectable capabilities (from the planner/intent system)
SELECTABLE_CAPABILITIES = [
    "code_edit",
    "file_inspection",
    "test_runner",
    "project_setup",
]

# Note: audit_report, data_export, system_check are in the registry
# but NOT in the selectable capabilities list
# And even if they were selectable, they have no callers


def _handle_audit_report() -> str:
    """Handler that would generate an audit report - never called."""
    return "Audit report generated"


def _handle_data_export() -> str:
    """Handler that would export data - never called."""
    return "Data exported"


def _handle_system_check() -> str:
    """Handler that would check system status - never called."""
    return "System check complete"