"""Offline regression tests for the all-in-one modules (no Discord token required)."""
import asyncio
import datetime
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from discord import app_commands
from bot_core import server_store as stores
from cogs.birthdays import _is_birthday_today, _next_birthday_date
from cogs.counting import parse_counting_message
from cogs.leveling import _extract_mee6_players
from cogs.community import PersistentGiveawayButton, PersistentPollButton
from cogs.roles import PersistentRoleButton, PersistentRoleSelect


class StoreTests(unittest.TestCase):
    def test_persistence_defaults_and_isolation(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / 'data.json')
            store = stores.GuildJsonStore(path, lambda: {'nested': {'items': []}})
            store.mutate(1, lambda g: g['nested']['items'].append(7))
            copy = store.get_guild(1)
            copy['nested']['items'].append(8)
            self.assertEqual(store.get_guild(1)['nested']['items'], [7])
            self.assertEqual(stores.GuildJsonStore(path, lambda: {'nested': {'items': [], 'new': 9}}).get_guild(1), {'nested': {'items': [7], 'new': 9}})
            self.assertEqual(store.get_guild(2)['nested']['items'], [])
            with self.assertRaises(RuntimeError):
                def fail(g):
                    g['nested']['items'].append(99)
                    raise RuntimeError()
                store.mutate(1, fail)
            self.assertEqual(store.get_guild(1)['nested']['items'], [7])
            with patch('bot_core.server_store._atomic_write_json', side_effect=OSError):
                with self.assertRaises(OSError):
                    store.mutate(1, lambda g: g['nested']['items'].append(42))
            self.assertEqual(store.get_guild(1)['nested']['items'], [7])

    def test_expected_schemas(self):
        mod = stores._default_moderation_guild()
        self.assertIsInstance(mod['cases'], dict)
        self.assertIsInstance(mod['warnings'], dict)
        self.assertIsInstance(mod['warn_escalation'], dict)
        self.assertIn('automod', mod)
        roles = stores._default_roles_guild()
        self.assertIn('humans', roles['autoroles'])
        self.assertIn('saved', roles['sticky_roles'])
        self.assertEqual(stores._default_community_guild()['custom_commands'], {})


class FeatureTests(unittest.TestCase):
    def test_xp_curve_and_import(self):
        self.assertEqual([stores.mee6_xp_for_next_level(i) for i in range(3)], [100, 155, 220])
        for level in list(range(150)) + [1000000]:
            total = stores.mee6_total_xp_for_level(level)
            need = stores.mee6_xp_for_next_level(level)
            self.assertEqual(stores.mee6_level_from_xp(total), (level, 0, need))
            self.assertEqual(stores.mee6_level_from_xp(total + need - 1), (level, need - 1, need))
        players = _extract_mee6_players({'players': [{'id': '123', 'xp': 4425}, {'id': '456', 'detailed_xp': [50, 220, 305], 'level': 2}]})
        self.assertEqual([p['xp'] for p in players], [4425, 305])

    def test_counting_parser(self):
        self.assertEqual(parse_counting_message('42', False), 42)
        self.assertEqual(parse_counting_message('(10 * 5) - 8', True), 42)
        self.assertIsNone(parse_counting_message("__import__('os').system('ls')", True))
        self.assertIsNone(parse_counting_message('9' * 5000, False))
        self.assertIsNone(parse_counting_message('1/0', True))

    def test_leap_day(self):
        self.assertTrue(_is_birthday_today(29, 2, datetime.date(2026, 2, 28)))
        self.assertFalse(_is_birthday_today(29, 2, datetime.date(2028, 2, 28)))
        self.assertTrue(_is_birthday_today(29, 2, datetime.date(2028, 2, 29)))
        self.assertEqual(_next_birthday_date(29, 2, datetime.date(2026, 11, 5)), datetime.date(2027, 2, 28))

    def test_dynamic_item_patterns(self):
        for cls, value in [(PersistentRoleButton, 'mc:rolebtn:abc123:123'), (PersistentRoleSelect, 'mc:rolesel:abc123'), (PersistentGiveawayButton, 'mc:gway:abc123'), (PersistentPollButton, 'mc:poll:abc123:1')]:
            self.assertIsNotNone(cls.__discord_ui_compiled_template__.fullmatch(value))


class BotTests(unittest.IsolatedAsyncioTestCase):
    async def test_load_extensions_and_command_limits(self):
        from main import ModcheckBot
        from cogs import COG_EXTENSIONS
        bot = ModcheckBot()
        try:
            bot.add_dynamic_items(PersistentRoleButton, PersistentRoleSelect, PersistentGiveawayButton, PersistentPollButton)
            for ext in COG_EXTENSIONS:
                await bot.load_extension(ext)
            cmds = bot.tree.get_commands()
            self.assertLessEqual(len(cmds), 100)
            self.assertEqual(len(cmds), 98)
            for cmd in cmds:
                self.assertLessEqual(len(cmd.name), 32)
                self.assertLessEqual(len(cmd.description), 100)
                cmd.to_dict(bot.tree)
                if isinstance(cmd, app_commands.Group):
                    self.assertLessEqual(len(cmd.commands), 25)
                    for sub in cmd.commands:
                        self.assertLessEqual(len(sub.description), 100)
        finally:
            await bot.close()

    async def test_moderation_case_and_automod_toggle(self):
        import cogs.moderation as moderation
        import cogs.automod_logs as automod
        with tempfile.TemporaryDirectory() as tmp:
            store = stores.GuildJsonStore(str(Path(tmp) / 'mod.json'), stores._default_moderation_guild)
            cfg = stores.GuildJsonStore(str(Path(tmp) / 'cfg.json'), stores._default_server_config)
            guild = SimpleNamespace(id=123, get_channel=lambda _: None)
            target = SimpleNamespace(id=2, mention='<@2>')
            moderator = SimpleNamespace(id=3, mention='<@3>')
            with patch.object(moderation, 'moderation_store', store), patch.object(moderation, 'server_cfg_store', cfg), patch.object(moderation, 'get_dynamic_channel_id', return_value=0), patch.object(automod, 'moderation_store', store), patch.object(automod, 'is_user_admin', return_value=True):
                self.assertEqual(await moderation.log_mod_case(guild, 'WARN', target, moderator, 'test'), 1)
                self.assertEqual(store.get_guild(123)['cases']['1']['reason'], 'test')
                interaction = SimpleNamespace(guild=guild, user=moderator, response=SimpleNamespace(send_message=AsyncMock()))
                await automod.AutoModGroup.cmd_toggle.callback(automod.AutoModGroup(), interaction, 'enabled', True)
                self.assertTrue(store.get_guild(123)['automod']['enabled'])

    async def test_status_embed_limits(self):
        import cogs.feature_status as fs
        import cogs.general as general
        guild = SimpleNamespace(id=123, name='Test', roles=[], get_channel=lambda _: None, get_role=lambda _: None)
        embed = fs.build_feature_status_embed(guild)
        self.assertLessEqual(len(embed), 6000)
        for field in embed.fields:
            self.assertLessEqual(len(field.value), 1024)
        for category in ('overview', 'activity', 'music', 'alerts', 'casino', 'jobs', 'community', 'level_bday', 'roles_welcome', 'moderation_suite'):
            embed = general._build_help_embed(category)
            self.assertLessEqual(len(embed), 6000)
            for field in embed.fields:
                self.assertLessEqual(len(field.value), 1024)

    async def test_feature_setup_uses_same_stores(self):
        import cogs.feature_status as fs
        with tempfile.TemporaryDirectory() as tmp:
            roles = stores.GuildJsonStore(str(Path(tmp) / 'roles.json'), stores._default_roles_guild)
            levels = stores.GuildJsonStore(str(Path(tmp) / 'levels.json'), stores._default_levels_guild)
            mod = stores.GuildJsonStore(str(Path(tmp) / 'mod.json'), stores._default_moderation_guild)
            guild = SimpleNamespace(id=123, name='Test', roles=[], get_channel=lambda _: None, get_role=lambda _: None)
            with patch.object(fs, 'roles_store', roles), patch.object(fs, 'levels_store', levels), patch.object(fs, 'moderation_store', mod):
                view = fs.FeatureSetupView(guild, 42, module_key='autorole')
                view.apply_role(guild, 99, 'primary')
                self.assertEqual(roles.get_guild(123)['autoroles']['humans'], [99])
                view = fs.FeatureSetupView(guild, 42, module_key='leveling')
                view.apply_channel(guild, 55, 'primary')
                self.assertEqual(levels.get_guild(123)['config']['announce_mode'], 'channel')


if __name__ == '__main__':
    unittest.main()
