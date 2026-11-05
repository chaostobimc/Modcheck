"""Offline tests for setup-menu counter creation, Twitch and safe deletion."""
import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import discord
from bot_core.server_store import GuildJsonStore, _default_server_config
import bot_core.twitch_service as twitch
import cogs.feature_status as fs
import cogs.welcome as welcome


class CounterTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addAsyncCleanup(self.cleanup)
        self.store = GuildJsonStore(str(Path(self.tmp.name) / 'cfg.json'), _default_server_config)
        self.patches = [patch.object(welcome, 'server_cfg_store', self.store), patch.object(fs, 'server_cfg_store', self.store)]
        for item in self.patches:
            item.start()
        self.channels = {}
        self.created = []
        self.guild = SimpleNamespace(id=7, name='Server', roles=[], member_count=50,
            members=[SimpleNamespace(bot=False), SimpleNamespace(bot=True)], default_role=object(),
            get_channel=lambda cid: self.channels.get(cid), get_role=lambda rid: None)
        async def create_voice_channel(**kwargs):
            channel = SimpleNamespace(id=100 + len(self.created), mention=f'<#{100 + len(self.created)}>', delete=AsyncMock())
            self.created.append((channel, kwargs))
            self.channels[channel.id] = channel
            return channel
        self.guild.create_voice_channel = AsyncMock(side_effect=create_voice_channel)
        self.cog = object.__new__(welcome.WelcomeCog)
        self.cog._counter_locks = {}
        self.cog.bot = SimpleNamespace(guilds=[self.guild])

    async def cleanup(self):
        for item in reversed(self.patches):
            item.stop()
        self.tmp.cleanup()

    async def test_create_count_and_category_and_reload(self):
        category = object()
        channel = await self.cog.create_stat_counter(self.guild, 'all', category)
        args = self.created[0][1]
        self.assertEqual(args['name'], '👥 Discord Mitglieder: 50')
        self.assertIs(args['category'], category)
        self.assertFalse(args['overwrites'][self.guild.default_role].connect)
        stored = GuildJsonStore(self.store.file_path, _default_server_config).get_guild(7)['stats_channels']
        self.assertEqual(stored[0]['channel_id'], channel.id)
        self.assertEqual(stored[0]['type'], 'all')
        with self.assertRaises(welcome.CounterSetupError):
            await self.cog.create_stat_counter(self.guild, 'all')
        self.assertEqual(len(self.created), 1)

    async def test_failed_twitch_fetch_never_creates_zero_channel(self):
        with patch.object(welcome, 'fetch_twitch_follower_count', new=AsyncMock(return_value=None)):
            with self.assertRaisesRegex(welcome.CounterSetupError, 'nicht abrufbar'):
                await self.cog.create_stat_counter(self.guild, 'twitch_followers')
        self.guild.create_voice_channel.assert_not_awaited()
        self.assertEqual(self.store.get_guild(7)['stats_channels'], [])
        with patch.object(welcome, 'fetch_twitch_follower_count', new=AsyncMock(return_value=0)):
            await self.cog.create_stat_counter(self.guild, 'twitch_followers')
        self.assertEqual(self.created[0][1]['name'], '🟣 Twitch Follower: 0')

    async def test_repeated_clicks_do_not_create_duplicate_channels(self):
        results = await asyncio.gather(
            self.cog.create_stat_counter(self.guild, 'all'),
            self.cog.create_stat_counter(self.guild, 'all'),
            return_exceptions=True,
        )
        self.assertEqual(len(self.created), 1)
        self.assertEqual(sum(isinstance(result, welcome.CounterSetupError) for result in results), 1)
        self.assertEqual(len(self.store.get_guild(7)['stats_channels']), 1)

    async def test_creation_rolls_back_when_store_fails(self):
        with patch.object(self.store, 'mutate', side_effect=OSError('disk full')):
            with self.assertRaises(welcome.CounterSetupError):
                await self.cog.create_stat_counter(self.guild, 'all')
        self.created[0][0].delete.assert_awaited_once()

    async def test_delete_only_managed_and_keep_on_failure(self):
        random_channel = SimpleNamespace(delete=AsyncMock())
        self.channels[999] = random_channel
        with self.assertRaises(welcome.CounterSetupError):
            await self.cog.remove_stat_counter(self.guild, 999)
        random_channel.delete.assert_not_awaited()
        channel = await self.cog.create_stat_counter(self.guild, 'all')
        channel.delete.side_effect = discord.Forbidden(SimpleNamespace(status=403, reason='Forbidden'), {'message': 'no', 'code': 50013})
        with self.assertRaises(welcome.CounterSetupError):
            await self.cog.remove_stat_counter(self.guild, channel.id)
        self.assertEqual(len(self.store.get_guild(7)['stats_channels']), 1)
        channel.delete.side_effect = None
        await self.cog.remove_stat_counter(self.guild, channel.id)
        self.assertEqual(self.store.get_guild(7)['stats_channels'], [])

    async def test_menu_create_and_management_navigation(self):
        main = fs.FeatureSetupView(self.guild, 42, module_key='stats')
        self.assertTrue(any('Zähler erstellen' in getattr(c, 'label', '') for c in main.children))
        view = fs.StatsCreateView(self.guild, 42, metric='all')
        view.to_components()
        self.assertEqual({o.value for o in view.children[0].options}, set(welcome.STAT_COUNTERS))
        interaction = SimpleNamespace(guild=self.guild, client=SimpleNamespace(get_cog=lambda _: self.cog),
            response=SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock()),
            edit_original_response=AsyncMock(), followup=SimpleNamespace(send=AsyncMock()))
        await view.create(interaction)
        interaction.edit_original_response.assert_awaited_once()
        self.assertIn('50', self.created[0][1]['name'])
        manage = fs.StatsManageView(self.guild, 42)
        manage.to_components()
        self.assertEqual(manage.children[0].options[0].value, str(self.created[0][0].id))
        with patch.object(fs, 'is_user_admin', return_value=False):
            interaction.user = SimpleNamespace(id=42)
            self.assertFalse(await manage.interaction_check(interaction))

    async def test_custom_name_and_delete_confirmation(self):
        category = MagicMock(spec=discord.CategoryChannel)
        category.id, category.mention = 20, '<#20>'
        self.channels[20] = category
        view = fs.StatsCreateView(self.guild, 42, metric='all', category_id=20, template='👥 User: {count}')
        embed = view.build_embed()
        self.assertTrue(any('User: 123' in f.value for f in embed.fields))
        interaction = SimpleNamespace(guild=self.guild, client=SimpleNamespace(get_cog=lambda _: self.cog),
            response=SimpleNamespace(defer=AsyncMock(), edit_message=AsyncMock(), send_message=AsyncMock()),
            edit_original_response=AsyncMock(), followup=SimpleNamespace(send=AsyncMock()))
        await view.create(interaction)
        self.assertIs(self.created[0][1]['category'], category)
        self.assertEqual(self.created[0][1]['name'], '👥 User: 50')
        manage = fs.StatsManageView(self.guild, 42, selected_id=self.created[0][0].id)
        await manage.confirm(interaction)
        confirm = interaction.response.edit_message.call_args.kwargs['view']
        self.assertIsInstance(confirm, fs.StatsDeleteConfirmView)
        await confirm.delete(interaction)
        self.created[0][0].delete.assert_awaited_once()
        self.assertFalse(self.store.get_guild(7)['stats_channels'])

    async def test_pagination_and_selection_is_limited_to_managed_channels(self):
        self.store.mutate(7, lambda g: g['stats_channels'].extend(
            {'channel_id': 1000+i, 'type': 'all', 'template': '{count}'} for i in range(31)))
        view = fs.StatsManageView(self.guild, 42, page=1)
        self.assertEqual(len(view.children[0].options), 6)
        self.assertLessEqual(len(view.to_components()), 5)
        interaction = SimpleNamespace(guild=self.guild, response=SimpleNamespace(send_message=AsyncMock()))
        forged = fs.StatsManageView(self.guild, 42, selected_id=99999)
        await forged.confirm(interaction)
        interaction.response.send_message.assert_awaited_once()

    async def test_refresh_uses_one_twitch_query_and_keeps_old_on_failure(self):
        twitch_channel = MagicMock(spec=discord.VoiceChannel)
        twitch_channel.name = 'Twitch: 99'
        twitch_channel.edit = AsyncMock()
        self.channels[44] = twitch_channel
        self.store.mutate(7, lambda g: g['stats_channels'].append({'channel_id': 44, 'type': 'twitch_followers', 'template': 'Twitch: {count}'}))
        with patch.object(welcome, 'fetch_twitch_follower_count', new=AsyncMock(return_value=None)) as fetch:
            await welcome.WelcomeCog.stats_counter_loop.coro(self.cog)
            fetch.assert_awaited_once()
        twitch_channel.edit.assert_not_awaited()
        with patch.object(welcome, 'fetch_twitch_follower_count', new=AsyncMock(return_value=105)):
            await welcome.WelcomeCog.stats_counter_loop.coro(self.cog)
        twitch_channel.edit.assert_awaited_once_with(name='Twitch: 105')


class TwitchFollowerTests(unittest.IsolatedAsyncioTestCase):
    async def test_public_total_and_token_fallback(self):
        response1 = MagicMock(status=401)
        response1.__aenter__ = AsyncMock(return_value=response1)
        response1.__aexit__ = AsyncMock(return_value=None)
        response2 = MagicMock(status=200)
        response2.json = AsyncMock(return_value={'total': 1234, 'data': []})
        response2.__aenter__ = AsyncMock(return_value=response2)
        response2.__aexit__ = AsyncMock(return_value=None)
        session = MagicMock()
        session.get.side_effect = [response1, response2]
        session.__aenter__ = AsyncMock(return_value=session)
        session.__aexit__ = AsyncMock(return_value=None)
        with patch.object(twitch, 'TWITCH_CLIENT_ID', 'client'), patch.object(twitch, 'STREAMER_OAUTH_TOKEN', 'user'), patch.object(twitch, 'TWITCH_TOKEN', ''), patch.object(twitch, 'resolve_broadcaster_id', new=AsyncMock(return_value='123')), patch.object(twitch, 'get_twitch_app_token', new=AsyncMock(return_value='app')), patch.object(twitch.aiohttp, 'ClientSession', return_value=session):
            self.assertEqual(await twitch.fetch_twitch_follower_count(), 1234)
        kwargs = session.get.call_args_list[0].kwargs
        self.assertEqual(kwargs['params']['broadcaster_id'], '123')
        self.assertNotIn('user_id', kwargs['params'])
        self.assertEqual(session.get.call_count, 2)

    async def test_missing_config_returns_none(self):
        with patch.object(twitch, 'TWITCH_CLIENT_ID', ''):
            self.assertIsNone(await twitch.fetch_twitch_follower_count())

    async def test_bad_http_response_does_not_create_fake_count(self):
        response = MagicMock(status=200)
        response.json = AsyncMock(return_value={'total': None})
        response.__aenter__ = AsyncMock(return_value=response)
        response.__aexit__ = AsyncMock(return_value=None)
        session = MagicMock()
        session.get.return_value = response
        session.__aenter__ = AsyncMock(return_value=session)
        session.__aexit__ = AsyncMock(return_value=None)
        with patch.object(twitch, 'TWITCH_CLIENT_ID', 'client'), patch.object(twitch, 'STREAMER_OAUTH_TOKEN', ''), patch.object(twitch, 'TWITCH_TOKEN', ''), patch.object(twitch, 'resolve_broadcaster_id', new=AsyncMock(return_value='123')), patch.object(twitch, 'get_twitch_app_token', new=AsyncMock(return_value='app')), patch.object(twitch.aiohttp, 'ClientSession', return_value=session):
            self.assertIsNone(await twitch.fetch_twitch_follower_count())
