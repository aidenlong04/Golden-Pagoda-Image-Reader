"""Pure onboarding-flow helpers.

Parsing the ``onboard:`` custom-id family and deciding what a reprompt sweep
should do with each pending prompt row. All Discord I/O (posting welcomes,
modals, role assignment) stays in bot.py; keeping the decision logic here
makes the flow unit-testable without a gateway connection.
"""
from __future__ import annotations

import enum
from typing import NamedTuple


class OnboardAction(NamedTuple):
    """A parsed ``onboard:<user_id>:<action>[...]`` component custom id.

    ``slot_token`` is a clan-slot number string, ``"none"`` for the
    "Not listed / No" path, or None when the id carried no usable choice.
    """

    target_id: int
    action: str
    slot_token: str | None


def parse_onboard_custom_id(
    custom_id: str, *, select_values: list | None = None
) -> OnboardAction | None:
    """Parse an onboarding component custom id into an :class:`OnboardAction`.

    Formats: ``onboard:<user_id>:clanselect`` (slot in ``select_values``),
    ``onboard:<user_id>:none``, and the legacy ``onboard:<user_id>:clan:<n>``
    buttons. Returns None when the user id is missing/invalid.
    """
    parts = custom_id.split(":")
    try:
        target_id = int(parts[1])
    except (IndexError, ValueError):
        return None
    action = parts[2] if len(parts) > 2 else ""

    slot_token: str | None = None
    if action == "clanselect":
        values = select_values or []
        slot_token = str(values[0]) if values else None
    elif action == "clan":
        slot_token = parts[3] if len(parts) > 3 else None
    elif action == "none":
        slot_token = "none"
    return OnboardAction(target_id, action, slot_token)


class RepromptDecision(enum.Enum):
    """What the reprompt sweep should do with one pending prompt row."""

    WAIT = "wait"          # window hasn't elapsed yet
    CLEANUP = "cleanup"    # member left — delete the row
    STOP = "stop"          # max reprompts hit — mark complete, stop nagging
    REPOST = "repost"      # delete the old prompt and post a fresh one


def reprompt_decision(
    *,
    posted_ts: float,
    reprompt_count: int,
    now: float,
    window_secs: float,
    max_reprompts: int,
    member_present: bool,
) -> RepromptDecision:
    """Pure decision for one pending onboarding prompt row."""
    if now - posted_ts < window_secs:
        return RepromptDecision.WAIT
    if not member_present:
        return RepromptDecision.CLEANUP
    if reprompt_count >= max_reprompts:
        return RepromptDecision.STOP
    return RepromptDecision.REPOST
