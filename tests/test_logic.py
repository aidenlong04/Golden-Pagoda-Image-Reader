import unittest

from logic import RoleRule, match_role_name, parse_role_rules


class ParseRoleRulesTests(unittest.TestCase):
    def test_parse_role_rules_returns_rules(self) -> None:
        rules = parse_role_rules("gold:Gold Role,silver:Silver Role")

        self.assertEqual(
            rules,
            [
                RoleRule(keyword="gold", role_name="Gold Role"),
                RoleRule(keyword="silver", role_name="Silver Role"),
            ],
        )

    def test_parse_role_rules_rejects_invalid_rule(self) -> None:
        with self.assertRaises(ValueError):
            parse_role_rules("missing-separator")


class MatchRoleNameTests(unittest.TestCase):
    def test_match_role_name_is_case_insensitive(self) -> None:
        rules = [RoleRule(keyword="golden pagoda", role_name="Verified")]

        role = match_role_name("Reached GOLDEN PAGODA rank today", rules)

        self.assertEqual(role, "Verified")

    def test_match_role_name_returns_none_without_match(self) -> None:
        rules = [RoleRule(keyword="golden pagoda", role_name="Verified")]

        role = match_role_name("No ranking text here", rules)

        self.assertIsNone(role)


if __name__ == "__main__":
    unittest.main()
