from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RoleRule:
    keyword: str
    role_name: str


def parse_role_rules(raw_rules: str) -> list[RoleRule]:
    """Parse ROLE_RULES env value in the format: keyword:Role Name,keyword2:Role 2"""
    if not raw_rules or not raw_rules.strip():
        return []

    rules: list[RoleRule] = []
    for segment in raw_rules.split(","):
        part = segment.strip()
        if not part:
            continue

        keyword, separator, role_name = part.partition(":")
        if not separator:
            raise ValueError(f"Invalid role rule '{part}'. Expected keyword:Role Name")

        keyword = keyword.strip().lower()
        role_name = role_name.strip()
        if not keyword or not role_name:
            raise ValueError(f"Invalid role rule '{part}'. Keyword and role name are required")

        rules.append(RoleRule(keyword=keyword, role_name=role_name))

    return rules


def match_role_name(ocr_text: str, rules: list[RoleRule]) -> str | None:
    normalized = (ocr_text or "").lower()
    for rule in rules:
        if rule.keyword in normalized:
            return rule.role_name
    return None
