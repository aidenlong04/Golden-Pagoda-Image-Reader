"""`.env` file persistence for the Golden Pagoda bot.

In-place rewriters for the runtime-resolved config the bot writes back to
its ``.env`` (clan slots, platform role IDs, generic id lists), plus the
atomic-write helper underneath them. Extracted from bot.py; imports nothing
from bot (in production the bot runs as ``python -u bot.py`` / ``__main__``,
so a back-import would re-import the whole bot as a second module). The only
cross-module dependency is the ``ClanSlot`` model in logic.py, used for type
hints + reading slot fields.
"""
from __future__ import annotations

import os
import re
from collections.abc import Callable
from pathlib import Path

from logic import ClanSlot

# Where the bot reads/writes its .env. bot.py imports this back so its
# role-sync logging + the slash-command persisters all agree on the path.
ENV_FILE_PATH = Path(os.getenv("ENV_FILE_PATH", ".env"))

# Platform name → .env key for the resolved role ID.
PLATFORM_ROLE_ID_ENV_KEYS: dict[str, str] = {
    "PC": "PLATFORM_ROLE_PC_ID",
    "Xbox": "PLATFORM_ROLE_XBOX_ID",
    "PlayStation": "PLATFORM_ROLE_PLAYSTATION_ID",
    "Switch": "PLATFORM_ROLE_SWITCH_ID",
    "Mobile": "PLATFORM_ROLE_MOBILE_ID",
}

_ENV_CLAN_SLOT_RE = re.compile(r"^(\s*)CLAN_ROLE_(\d+)_(NAME|ID|EMOJI)\s*=.*$")
_ENV_PLATFORM_ID_RE = re.compile(r"^(\s*)(PLATFORM_ROLE_[A-Z]+_ID)\s*=.*$")
_ENV_GENERIC_KEY_RE = re.compile(r"^(\s*)([A-Za-z_][A-Za-z0-9_]*)\s*=.*$")


def _slot_field_value(slot: ClanSlot, field: str) -> str:
    if field == "NAME":
        return slot.clan_name or ""
    if field == "ID":
        return str(slot.role_id) if slot.role_id else ""
    if field == "EMOJI":
        return slot.emoji or ""
    return ""


def _atomic_write_text(path: Path, content: str) -> None:
    """Write to ``path`` atomically: stage in a sibling tempfile, then rename.

    Prevents a half-written .env if the process is killed mid-write (e.g.
    OOM under the 512m container cap), which would leave the bot unable
    to start on next boot.
    """
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content)
    os.replace(tmp, path)


def _rewrite_env_file(
    replace_line: Callable[[str], str | None],
    missing_lines: Callable[[], list[str]],
) -> bool:
    """Rewrite ``.env`` in place using a shared read→replace→append skeleton.

    ``replace_line`` is called for every existing line and returns either a
    replacement string or ``None`` to leave the line untouched. Any entries
    returned by ``missing_lines()`` are appended (after a blank separator).
    Returns ``False`` when the file doesn't exist. Centralises the logic the
    clan / platform / id-list writers previously duplicated.
    """
    if not ENV_FILE_PATH.exists():
        return False
    lines = ENV_FILE_PATH.read_text().splitlines()
    for idx, line in enumerate(lines):
        replacement = replace_line(line)
        if replacement is not None:
            lines[idx] = replacement
    missing = missing_lines()
    if missing:
        if lines and lines[-1].strip():
            lines.append("")
        lines.extend(missing)
    _atomic_write_text(ENV_FILE_PATH, "\n".join(lines) + "\n")
    return True


def _update_env_clan_slots(slots: list[ClanSlot]) -> bool:
    """Rewrite the CLAN_ROLE_{i}_NAME/_ID/_EMOJI entries in the .env file in place."""
    by_slot = {s.slot: s for s in slots}
    seen: set[tuple[int, str]] = set()

    def replace(line: str) -> str | None:
        m = _ENV_CLAN_SLOT_RE.match(line)
        if not m:
            return None
        indent, slot_num, field = m.group(1), int(m.group(2)), m.group(3)
        slot = by_slot.get(slot_num)
        if slot is None:
            return None
        seen.add((slot_num, field))
        return f"{indent}CLAN_ROLE_{slot_num}_{field}={_slot_field_value(slot, field)}"

    def missing() -> list[str]:
        out: list[str] = []
        for i in sorted(by_slot):
            for field in ("NAME", "ID", "EMOJI"):
                if (i, field) not in seen:
                    out.append(f"CLAN_ROLE_{i}_{field}={_slot_field_value(by_slot[i], field)}")
        return out

    return _rewrite_env_file(replace, missing)


def _update_env_platform_ids(ids: dict[str, int | None]) -> bool:
    """Rewrite the PLATFORM_ROLE_*_ID entries in the .env file in place."""
    key_to_platform = {v: k for k, v in PLATFORM_ROLE_ID_ENV_KEYS.items()}
    seen: set[str] = set()

    def replace(line: str) -> str | None:
        m = _ENV_PLATFORM_ID_RE.match(line)
        if not m:
            return None
        indent, key = m.group(1), m.group(2)
        platform = key_to_platform.get(key)
        if platform is None:
            return None
        seen.add(key)
        return f"{indent}{key}={str(ids.get(platform)) if ids.get(platform) else ''}"

    def missing() -> list[str]:
        return [
            f"{key}={str(ids.get(platform)) if ids.get(platform) else ''}"
            for platform, key in PLATFORM_ROLE_ID_ENV_KEYS.items()
            if key not in seen
        ]

    return _rewrite_env_file(replace, missing)


def _update_env_id_list(env_key: str, ids: list[int]) -> bool:
    """Rewrite (or append) ``ENV_KEY=id1,id2,...`` in the .env file."""
    return _update_env_value(env_key, ",".join(str(i) for i in ids))


def _update_env_value(env_key: str, value: str) -> bool:
    """Rewrite (or append) ``ENV_KEY=value`` in the .env file (generic string).

    Shared skeleton for the id-list writer and any caller that needs to persist
    a single ``KEY=value`` line (e.g. a resolved comma-joined name list).
    """
    seen = False

    def replace(line: str) -> str | None:
        nonlocal seen
        if seen:
            return None
        m = _ENV_GENERIC_KEY_RE.match(line)
        if not m or m.group(2) != env_key:
            return None
        seen = True
        return f"{m.group(1)}{env_key}={value}"

    def missing() -> list[str]:
        return [] if seen else [f"{env_key}={value}"]

    return _rewrite_env_file(replace, missing)
