"""
The run status state machine. Kept as a plain dict on purpose --
no LangGraph, no extra infra. Every transition must be validated
through `transition()` before a run's status is changed anywhere
in the app. Never set run.status directly in a route handler.
"""

ALLOWED_TRANSITIONS = {
    "IDEA_SUBMITTED": {"RESEARCHING"},
    "RESEARCHING": {"RESEARCHED", "RESEARCH_INSUFFICIENT"},
    "RESEARCHED": {"EXPERIMENT_PROPOSED"},
    "EXPERIMENT_PROPOSED": {"APPROVAL_REQUIRED", "SPEC_INVALID"},
    "APPROVAL_REQUIRED": {"ASSET_BUILT", "APPROVAL_REJECTED"},
    "ASSET_BUILT": {"QA_RUNNING"},
    "QA_RUNNING": {"QA_PASSED", "QA_FAILED"},
    "QA_PASSED": {"APPROVAL_REQUIRED_PUBLISH"},
    "APPROVAL_REQUIRED_PUBLISH": {"PUBLISHING", "APPROVAL_REJECTED"},
    "PUBLISHING": {"PUBLISHED", "ACTION_FAILED"},
    "PUBLISHED": {"MEASURING"},
    "MEASURING": {"DECISION_READY", "EXPERIMENT_INCONCLUSIVE"},
    # Failure states: no automatic forward transition. A human/new run restarts the flow.
    "RESEARCH_INSUFFICIENT": set(),
    "SPEC_INVALID": set(),
    "APPROVAL_REJECTED": set(),
    "QA_FAILED": set(),
    "ACTION_FAILED": set(),
    "EXPERIMENT_INCONCLUSIVE": set(),
    "DECISION_READY": set(),
}


class InvalidTransitionError(Exception):
    pass


def transition(current_status: str, new_status: str) -> None:
    allowed = ALLOWED_TRANSITIONS.get(current_status, set())
    if new_status not in allowed:
        raise InvalidTransitionError(
            f"Cannot move run from '{current_status}' to '{new_status}'. "
            f"Allowed next states: {sorted(allowed) or 'none (terminal state)'}"
        )
