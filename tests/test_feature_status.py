"""Offline tests for every setup field and the Discord component layout."""
import copy
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import discord
from bot_core import server_store as stores
import cogs.feature_status as fs


class SetupCenterTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.stack = ExitStack()
        self.addAsyncCleanup(self.cleanup)
        factories = {
            'server_cfg_store': stores._default_server_config,
            'birthdays_store': stores._default_birthdays_guild,
            'counting_store': stores._default_counting_guild,
            'levels_store': stores._default_levels_guild,
            'roles_store': stores._default_roles_guild,
            'moderation_store': stores._default_moderation_guild,
            'community_store': stores._default_community_guild,
        }
        self.db = {}
        for name, factory in factories.items():
            store = stores.GuildJsonStore(str(Path(self.tmp.name) / (name + '.json')), factory)
            self.db[name] = store
            self.stack.enter_context(patch.object(fs, name, store))
        self.stack.enter_context(patch.object(stores, 'server_cfg_store', self.db['server_cfg_store']))
        self.alerts = {'config': {}}
        self.stack.enter_context(patch.object(fs, 'load_alerts_state', side_effect=lambda: copy.deepcopy(self.alerts)))
        self.stack.enter_context(patch.object(fs, 'save_alerts_state', side_effect=lambda state: self.alerts.update(copy.deepcopy(state))))
        self.guild = SimpleNamespace(id=1, name='Test', roles=[],
                                     get_channel=lambda i: SimpleNamespace(id=i, mention=f'<#{i}>') if i else None,
                                     get_role=lambda i: SimpleNamespace(id=i, mention=f'<@&{i}>') if i else None)

    async def cleanup(self):
        self.stack.close()
        self.tmp.cleanup()

    async def test_every_field_can_be_selected_saved_and_reset(self):
        for module, spec in fs.MODULE_SPECS.items():
            for slot, field in spec.get('fields', {}).items():
                with self.subTest(module=module, slot=slot):
                    view = fs.FeatureSetupView(self.guild, 42, module, field_key=slot)
                    view.to_components()
                    self.assertIsNotNone(next((child for child in view.children
                        if isinstance(child, discord.ui.RoleSelect if field.kind == 'role' else discord.ui.ChannelSelect)), None))
                    if field.kind == 'role':
                        view.apply_role(self.guild, 12345, slot)
                    else:
                        view.apply_channel(self.guild, 12345, slot)
                    self.assertIn('12345', fs._display_field(self.guild, field))
                    fs._set_field(self.guild, field, 0)
                    self.assertNotIn('12345', fs._display_field(self.guild, field))

    async def test_navigation_and_embeds(self):
        overview = fs.FeatureSetupView(self.guild, 42)
        self.assertIsInstance(overview.children[0], fs.CategorySelect)
        overview.to_components()
        for category in fs.CATEGORIES:
            view = fs.FeatureSetupView(self.guild, 42, category=category)
            view.to_components()
            options = view.children[0].options
            self.assertEqual({o.value for o in options}, {k for k, s in fs.MODULE_SPECS.items() if s['category'] == category})
            self.assertLessEqual(len(options), 25)
        for key, spec in fs.MODULE_SPECS.items():
            view = fs.FeatureSetupView(self.guild, 42, key)
            embed = view.build_current_embed()
            view.to_components()
            self.assertLessEqual(len(embed), 6000)
            for field in embed.fields:
                self.assertLessEqual(len(field.value), 1024)
            if len(spec.get('fields', {})) == 1:
                self.assertTrue(any(isinstance(item, (discord.ui.ChannelSelect, discord.ui.RoleSelect)) for item in view.children))

    async def test_no_implicit_activation_and_autoroles_are_preserved(self):
        view = fs.FeatureSetupView(self.guild, 42, 'counting')
        view.apply_channel(self.guild, 100, 'channel')
        self.assertFalse(self.db['counting_store'].get_guild(1)['enabled'])
        role_view = fs.FeatureSetupView(self.guild, 42, 'autorole')
        role_view.apply_role(self.guild, 200, 'primary')
        role_view.apply_role(self.guild, 201, 'primary')
        self.assertEqual(self.db['roles_store'].get_guild(1)['autoroles']['humans'], [200, 201])

    async def test_owner_and_admin_are_checked(self):
        view = fs.FeatureSetupView(self.guild, 42)
        response = SimpleNamespace(send_message=AsyncMock())
        interaction = SimpleNamespace(guild=self.guild, user=SimpleNamespace(id=42), response=response)
        with patch.object(fs, 'is_user_admin', return_value=True):
            self.assertTrue(await view.interaction_check(interaction))
            interaction.user.id = 99
            self.assertFalse(await view.interaction_check(interaction))
        interaction.user.id = 42
        with patch.object(fs, 'is_user_admin', return_value=False):
            self.assertFalse(await view.interaction_check(interaction))

    async def test_toggle_button_updates_state_and_label(self):
        view = fs.FeatureSetupView(self.guild, 42, 'counting')
        interaction = SimpleNamespace(guild=self.guild, response=SimpleNamespace(edit_message=AsyncMock()))
        await view._toggle(interaction)
        self.assertTrue(self.db['counting_store'].get_guild(1)['enabled'])
        new_view = interaction.response.edit_message.call_args.kwargs['view']
        self.assertTrue(any('deaktivieren' in getattr(child, 'label', '') for child in new_view.children))

    async def test_ticket_support_role_uses_selected_role(self):
        import cogs.tickets as tickets
        role = SimpleNamespace(id=12345, name='Team')
        guild = SimpleNamespace(id=1, roles=[role], get_role=lambda value: role if value == role.id else None)
        with patch.object(tickets, 'get_dynamic_role_id', return_value=12345):
            self.assertIs(tickets._support_role(guild), role)
        with patch.object(tickets, 'get_dynamic_role_id', return_value=0), patch.object(tickets, 'TICKET_SUPPORT_ROLE', 'Team'):
            self.assertIs(tickets._support_role(guild), role)

    async def test_clip_delivery_uses_each_guild_channel(self):
        import cogs.activity as activity
        ch1 = SimpleNamespace(send=AsyncMock())
        ch2 = SimpleNamespace(send=AsyncMock())
        g1 = SimpleNamespace(id=1, get_channel=lambda i: ch1 if i == 10 else None)
        g2 = SimpleNamespace(id=2, get_channel=lambda i: ch2 if i == 20 else None)
        cog = SimpleNamespace(bot=SimpleNamespace(guilds=[g1, g2]), _clips_initialized=True, _known_clip_ids=set())
        clips = [{'id': 'new', 'title': 'Clip', 'url': 'https://example.com', 'creator_name': 'User'}]
        with patch.object(activity, 'STREAMER_CHANNEL', 'streamer'), patch.object(activity, 'get_dynamic_channel_id', side_effect=lambda gid, key, fallback: {1: 10, 2: 20}[gid]), patch.object(activity, 'fetch_recent_clips', new=AsyncMock(return_value=clips)), patch.object(activity, 'save_clip_tracker_state'):
            await activity.ActivityCog.clip_tracker.coro(cog)
        ch1.send.assert_awaited_once()
        ch2.send.assert_awaited_once()

    async def test_status_command_uses_dynamic_channels(self):
        import cogs.general as general
        bot = SimpleNamespace(get_cog=lambda name: None, latency=0.05)
        cog = general.GeneralCog(bot)
        interaction = SimpleNamespace(guild_id=1, response=SimpleNamespace(send_message=AsyncMock()))
        channels = {'twitch_log_channel_id': 41, 'review_channel_id': 0,
                    'temp_voice_channel_id': 42, 'gambling_channel_id': 43, 'fishing_channel_id': 44}
        with patch.object(general, 'load_data', return_value={'streams': {}, 'users': {}}), \
             patch.object(general, 'get_dynamic_channel_id', side_effect=lambda gid, key, default: channels[key]), \
             patch.object(general, 'is_stream_currently_live_cached', return_value=False), \
             patch.object(general, 'load_alerts_state', return_value={'config': {}}), \
             patch.object(general, 'real_stream_days', return_value=[]):
            await general.GeneralCog.cmd_status.callback(cog, interaction)
        embed = interaction.response.send_message.call_args.kwargs['embed']
        values = '\n'.join(field.value for field in embed.fields)
        self.assertIn('<#43>', values)
        self.assertIn('<#44>', values)
        self.assertIn('Twitch Mod Log ── ✅ Aktiv', values)
