"""Tests for gpbot.records — pure record-body parsing."""
from __future__ import annotations

from gpbot.records import (
    DISCORD_EPOCH_MS,
    collect_v2_text,
    is_exact_mastery_rank,
    parse_record_embed,
    parse_record_profile_text,
    snowflake_ts,
)


class TestSnowflakeTs:
    def test_epoch_snowflake(self):
        assert snowflake_ts(0) == DISCORD_EPOCH_MS // 1000

    def test_known_offset(self):
        # 1 hour after the Discord epoch.
        snowflake = (3600 * 1000) << 22
        assert snowflake_ts(snowflake) == DISCORD_EPOCH_MS // 1000 + 3600


class TestCollectV2Text:
    def test_collects_type_10_content(self):
        tree = [
            {"type": 17, "components": [
                {"type": 10, "content": "first"},
                {"type": 12, "items": [{"type": 10, "content": "nope"}]},
            ]},
            {"type": 10, "content": "second"},
        ]
        text = collect_v2_text(tree)
        assert "first" in text and "second" in text

    def test_walks_items(self):
        tree = {"type": 9, "items": [{"type": 10, "content": "inner"}]}
        assert collect_v2_text(tree) == "inner"

    def test_non_dict_nodes_ignored(self):
        assert collect_v2_text(None) == ""
        assert collect_v2_text([1, "x", {"type": 10, "content": "ok"}]) == "ok"


class TestIsExactMasteryRank:
    def test_accepts_mr_and_lr(self):
        assert is_exact_mastery_rank("MR 12")
        assert is_exact_mastery_rank("LR 3")
        assert is_exact_mastery_rank("mr7")

    def test_rejects_rank_zero(self):
        assert not is_exact_mastery_rank("MR 0")
        assert not is_exact_mastery_rank("LR 0")

    def test_rejects_bucket_names_and_junk(self):
        assert not is_exact_mastery_rank("MR 1-10")
        assert not is_exact_mastery_rank("Legendary")
        assert not is_exact_mastery_rank("")
        assert not is_exact_mastery_rank(None)  # type: ignore[arg-type]


class TestParseRecordProfileText:
    def test_parses_labelled_lines(self):
        text = (
            "-# In-Game Name: **Player#123**\n"
            "-# Clan: **Golden Pagoda**\n"
            "-# Platform: **PC**\n"
            "-# Mastery Rank: **MR 22**\n"
        )
        out = parse_record_profile_text(text)
        assert out == {
            "in_game_name": "Player#123",
            "clan": "Golden Pagoda",
            "platform": "PC",
            "mastery_rank": "MR 22",
        }

    def test_drops_coarse_mastery_bucket(self):
        out = parse_record_profile_text("Mastery Rank: **MR 1-10**")
        assert "mastery_rank" not in out

    def test_first_occurrence_wins(self):
        text = "Clan: **First**\nClan: **Second**"
        assert parse_record_profile_text(text)["clan"] == "First"

    def test_empty_input(self):
        assert parse_record_profile_text("") == {}
        assert parse_record_profile_text(None) == {}  # type: ignore[arg-type]


class TestParseRecordEmbed:
    def test_parses_embed_fields(self):
        embeds = [{"fields": [
            {"name": "In-Game Name", "value": "**Player#123**"},
            {"name": "Mastery Rank:", "value": "`LR 2`"},
            {"name": "Platform", "value": "PC"},
        ]}]
        out = parse_record_embed(embeds)
        assert out == {
            "in_game_name": "Player#123",
            "mastery_rank": "LR 2",
            "platform": "PC",
        }

    def test_rejects_mr_zero_in_embed(self):
        embeds = [{"fields": [{"name": "Mastery Rank", "value": "**MR 0**"}]}]
        assert parse_record_embed(embeds) == {}

    def test_non_list_input(self):
        assert parse_record_embed(None) == {}
        assert parse_record_embed({"fields": []}) == {}
