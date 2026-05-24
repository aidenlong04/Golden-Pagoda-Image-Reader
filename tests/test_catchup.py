from __future__ import annotations

import unittest
from unittest.mock import MagicMock, Mock

import discord


class MockClient:
    """Mock Discord client for testing."""
    def __init__(self):
        self.user = Mock(id=12345)


class MessageAlreadyProcessedTests(unittest.TestCase):
    """Test the _message_already_processed logic."""
    
    def setUp(self):
        """Set up test fixtures."""
        # Import here to avoid circular imports and allow mocking.
        import bot as bot_module
        self._original_client = getattr(bot_module, 'client', None)
        self._original_pass_id = getattr(bot_module, 'PASS_REACTION_ID', None)
        self._original_fail_emoji = getattr(bot_module, 'FAIL_REACTION_EMOJI', None)
        
        # Mock the client.
        bot_module.client = MockClient()
        bot_module.PASS_REACTION_ID = 111111
        bot_module.FAIL_REACTION_EMOJI = "\U0001F6A8"  # 🚨
        
        self.bot_module = bot_module
    
    def tearDown(self):
        """Restore original values."""
        if self._original_client is not None:
            self.bot_module.client = self._original_client
        if self._original_pass_id is not None:
            self.bot_module.PASS_REACTION_ID = self._original_pass_id
        if self._original_fail_emoji is not None:
            self.bot_module.FAIL_REACTION_EMOJI = self._original_fail_emoji
    
    def test_no_reactions_returns_false(self):
        """Messages with no reactions are not processed."""
        message = Mock(reactions=[])
        self.assertFalse(self.bot_module._message_already_processed(message))
    
    def test_pass_reaction_from_bot_returns_true(self):
        """Messages with the bot's pass reaction are already processed."""
        pass_emoji = Mock(spec=discord.PartialEmoji)
        pass_emoji.id = 111111
        reaction = Mock(emoji=pass_emoji, me=True)
        message = Mock(reactions=[reaction])
        self.assertTrue(self.bot_module._message_already_processed(message))
    
    def test_fail_reaction_from_bot_returns_true(self):
        """Messages with the bot's fail reaction are already processed."""
        reaction = Mock(emoji="\U0001F6A8", me=True)
        message = Mock(reactions=[reaction])
        self.assertTrue(self.bot_module._message_already_processed(message))
    
    def test_reaction_from_other_user_returns_false(self):
        """Reactions from other users don't count as processed."""
        reaction = Mock(emoji="\U0001F6A8", me=False)
        message = Mock(reactions=[reaction])
        self.assertFalse(self.bot_module._message_already_processed(message))
    
    def test_unrelated_reaction_returns_false(self):
        """Unrelated reactions don't mark a message as processed."""
        reaction = Mock(emoji="\U0001F44D", me=True)  # 👍
        message = Mock(reactions=[reaction])
        self.assertFalse(self.bot_module._message_already_processed(message))


class CatchupStateTests(unittest.TestCase):
    """Test catch-up state persistence."""
    
    def test_load_nonexistent_state_returns_none(self):
        """Loading state from a nonexistent file returns None."""
        import bot as bot_module
        import tempfile
        from pathlib import Path
        
        original_path = bot_module.CATCHUP_STATE_PATH
        try:
            with tempfile.TemporaryDirectory() as tmpdir:
                bot_module.CATCHUP_STATE_PATH = Path(tmpdir) / "nonexistent.json"
                result = bot_module._load_catchup_state()
                self.assertIsNone(result)
        finally:
            bot_module.CATCHUP_STATE_PATH = original_path
    
    def test_save_and_load_state_roundtrip(self):
        """Saving and loading state preserves the message ID."""
        import bot as bot_module
        import tempfile
        from pathlib import Path
        
        original_path = bot_module.CATCHUP_STATE_PATH
        try:
            with tempfile.TemporaryDirectory() as tmpdir:
                bot_module.CATCHUP_STATE_PATH = Path(tmpdir) / "state.json"
                message_id = 987654321
                bot_module._save_catchup_state(message_id)
                loaded = bot_module._load_catchup_state()
                self.assertEqual(loaded, message_id)
        finally:
            bot_module.CATCHUP_STATE_PATH = original_path


if __name__ == "__main__":
    unittest.main()
