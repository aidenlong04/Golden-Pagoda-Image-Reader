"""Golden Pagoda bot package.

Modular pieces of the bot, extracted from the ``bot.py`` orchestrator:

- :mod:`gpbot.bootstrap` — Discord client + CommandTree factory.
- :mod:`gpbot.routing` — prefix-based custom-id interaction router.
- :mod:`gpbot.components_v2` — raw Components-V2 HTTP helpers.
- :mod:`gpbot.discord_http` — shared Discord REST retry wrapper.
- :mod:`gpbot.concurrency` — heavy-job gate, bg tasks, per-key locks.
- :mod:`gpbot.records` — pure member-record body parsing.
- :mod:`gpbot.verify` — verification pipeline states + pure helpers.
- :mod:`gpbot.onboarding` — onboarding custom-id parse + reprompt decisions.
"""
from .bootstrap import build_client_tree
from .concurrency import (
    get_or_create_lock,
    run_heavy_job,
    spawn_bg_task,
)
from .discord_http import discord_call_with_retry
from .onboarding import (
    OnboardAction,
    RepromptDecision,
    parse_onboard_custom_id,
    reprompt_decision,
)
from .records import (
    collect_v2_text,
    is_exact_mastery_rank,
    parse_record_embed,
    parse_record_profile_text,
    snowflake_ts,
)
from .routing import CustomIDRouter
from .verify import (
    VerifyResult,
    VerifyState,
    parse_mastery_token,
    validate_image_bytes,
)

__all__ = [
    "CustomIDRouter",
    "OnboardAction",
    "RepromptDecision",
    "VerifyResult",
    "VerifyState",
    "build_client_tree",
    "collect_v2_text",
    "discord_call_with_retry",
    "get_or_create_lock",
    "is_exact_mastery_rank",
    "parse_mastery_token",
    "parse_onboard_custom_id",
    "parse_record_embed",
    "parse_record_profile_text",
    "reprompt_decision",
    "run_heavy_job",
    "snowflake_ts",
    "spawn_bg_task",
    "validate_image_bytes",
]
