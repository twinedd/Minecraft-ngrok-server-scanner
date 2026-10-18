import asyncio
import concurrent.futures
import json
import queue
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import ngrok_bot as app


class DatabaseTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / 'servers.json'
        self.path_patch = patch.object(app, 'DATABASE_PATH', self.path)
        self.path_patch.start()
        self.addCleanup(self.path_patch.stop)

    def test_failed_replace_preserves_database_and_removes_temp_file(self):
        app.save_to_json({'existing': {'players_online': 1}})
        original = self.path.read_bytes()
        with patch.object(app.os, 'replace', side_effect=OSError('disk failure')):
            with self.assertRaises(OSError):
                app.save_to_json({'replacement': {}})
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(list(self.path.parent.iterdir()), [self.path])

    def test_corrupt_database_is_not_overwritten_by_failed_query(self):
        self.path.write_text('{broken', encoding='utf-8')
        with patch.object(app.JavaServer, 'lookup', side_effect=TimeoutError):
            with self.assertRaises(json.JSONDecodeError):
                app.ping_server('known')
        self.assertEqual(self.path.read_text(encoding='utf-8'), '{broken')

    def test_failure_preserves_last_seen_and_success_resets_status(self):
        app.save_to_json({'known': {'last_updated': 'old', 'players_online': 2}})
        with patch.object(app.JavaServer, 'lookup', side_effect=TimeoutError):
            app.ping_server('known')
            app.ping_server('known')
            app.ping_server('unknown')
        record = app.load_from_json()['known']
        self.assertEqual(record['last_updated'], 'old')
        self.assertEqual(record['players_online'], 2)
        self.assertEqual(record['consecutive_failures'], 2)
        self.assertFalse(record['available'])
        self.assertNotIn('unknown', app.load_from_json())
        response = SimpleNamespace(
            version=SimpleNamespace(name="A server's version"),
            players=SimpleNamespace(sample=None, online=0),
            motd=SimpleNamespace(raw={'text': 'Welcome'}),
        )
        with patch.object(app.JavaServer, 'lookup') as lookup:
            lookup.return_value.status.return_value = response
            app.ping_server('known')
        record = app.load_from_json()['known']
        self.assertTrue(record['available'])
        self.assertEqual(record['consecutive_failures'], 0)
        self.assertEqual(record['version'], "A server's version")
        self.assertNotEqual(record['last_updated'], 'old')

    def test_concurrent_updates_and_reads_keep_all_records(self):
        app.save_to_json({})

        def update(index):
            with app.ngrok_settings.database_lock:
                db = app.load_from_json()
                db[str(index)] = {'players_online': index}
                app.save_to_json(db)
            self.assertIsInstance(app.load_from_json(), dict)

        with concurrent.futures.ThreadPoolExecutor(max_workers=13) as pool:
            list(pool.map(update, range(50)))
        self.assertEqual(len(app.load_from_json()), 50)


class ServerHistoryTests(unittest.TestCase):
    def observation(self, motd='Welcome', version='1.21', time='2026-10-18T10:00:00+00:00'):
        return {'motd': motd, 'version': version, 'last_updated': time,
                'players_list': '', 'players_online': 0, 'icon': '',
                'available': True, 'consecutive_failures': 0}

    def test_empty_sample_keeps_history_across_json_reload(self):
        first = app.merge_server_observation(None, self.observation(), ['Alice', 'Bob', 'Steve'])
        restored = json.loads(json.dumps(first))
        later = self.observation(time='2026-10-18T11:00:00+00:00')
        updated = app.merge_server_observation(restored, later, [])
        self.assertEqual(updated['player_history'], first['player_history'])
        self.assertEqual(updated['instance_id'], first['instance_id'])
        self.assertEqual(updated['players_online'], 0)
        self.assertEqual(updated['last_updated'], later['last_updated'])

    def test_returning_player_updates_last_seen_without_duplicate(self):
        first = app.merge_server_observation(None, self.observation(), ['Alice'])
        updated = app.merge_server_observation(first, self.observation(time='later'), ['ALICE', 'Bob'])
        self.assertEqual(len(updated['player_history']), 2)
        self.assertEqual(updated['player_history']['alice']['first_seen'], first['last_updated'])
        self.assertEqual(updated['player_history']['alice']['last_seen'], 'later')

    def test_description_or_version_change_archives_separate_instance(self):
        for changed in (self.observation(motd='Other server'), self.observation(version='1.22')):
            with self.subTest(changed=changed):
                first = app.merge_server_observation(None, self.observation(), ['Alice'])
                new = app.merge_server_observation(first, changed, ['Bob'])
                self.assertNotEqual(new['instance_id'], first['instance_id'])
                self.assertEqual(set(new['player_history']), {'bob'})
                self.assertEqual(set(new['previous_servers'][0]['player_history']), {'alice'})
                self.assertFalse(new['previous_servers'][0]['available'])
                self.assertNotIn('archived_at', first)

    def test_reconnect_and_motd_formatting_preserve_identity(self):
        first = app.merge_server_observation(None, self.observation(), ['Alice'])
        first['available'] = False
        first['consecutive_failures'] = 4
        new = app.merge_server_observation(first, self.observation(motd={'text': ' Welcome ', 'color': 'red'}), [])
        self.assertEqual(new['instance_id'], first['instance_id'])
        self.assertTrue(new['available'])
        self.assertEqual(new['consecutive_failures'], 0)
        self.assertIn('alice', new['player_history'])

    def test_reused_old_description_does_not_merge_archived_players(self):
        first = app.merge_server_observation(None, self.observation(), ['Alice'])
        second = app.merge_server_observation(first, self.observation(motd='Other'), ['Bob'])
        third = app.merge_server_observation(second, self.observation(), ['Steve'])
        self.assertEqual(len(third['previous_servers']), 2)
        self.assertEqual(set(third['player_history']), {'steve'})
        self.assertTrue(all('previous_servers' not in item for item in third['previous_servers']))

    def test_legacy_names_are_imported_before_replacement(self):
        old = self.observation()
        old['players_list'] = 'Alice, Bob'
        new = app.merge_server_observation(old, self.observation(motd='Other'), [])
        self.assertEqual(set(new['previous_servers'][0]['player_history']), {'alice', 'bob'})
        self.assertEqual(new['player_history'], {})

    def test_history_pages_include_all_names_and_fit_embed_limits(self):
        record = app.merge_server_observation(None, self.observation(), [f'Player{i}' for i in range(100)])
        pages = app.create_player_history_pages('test:123', record)
        self.assertEqual(sum(len(page.fields) for page in pages), 100)
        self.assertTrue(all(len(page) < 6000 for page in pages))
        self.assertEqual(app.create_player_history_pages('test', self.observation()), [])


class ServerListTests(unittest.IsolatedAsyncioTestCase):
    async def test_server_and_player_dates_use_discord_timestamps(self):
        timestamp = '2026-10-18T10:00:00+00:00'
        formatted = app.format_seen_time(timestamp)
        self.assertRegex(formatted, r'^<t:\d+:f> \(<t:\d+:R>\)$')
        self.assertEqual(formatted, app.format_seen_time('2026-10-18T13:00:00+03:00'))
        embed = await app.create_server_embed('', 'Welcome', 0, '1.21', 'test', '', timestamp, timestamp)
        self.assertIn(f'First seen:** {formatted}', embed.fields[2].value)
        self.assertIn(f'Last seen:** {formatted}', embed.fields[2].value)
        record = app.merge_server_observation(None, ServerHistoryTests().observation(), ['Alice'])
        pages = app.create_player_history_pages('test', record)
        self.assertEqual(pages[0].fields[0].value, f'First seen: {formatted}\nLast seen: {formatted}')

    async def test_missing_or_invalid_dates_show_unknown(self):
        for value in (None, '', 'invalid'):
            self.assertEqual(app.format_seen_time(value), 'Unknown')

    async def test_only_successfully_checked_available_servers_are_listed(self):
        template = ServerHistoryTests().observation()
        db = {'online': dict(template), 'offline': dict(template, available=False),
              'legacy': {k: v for k, v in template.items() if k != 'available'}}
        pages = await app.create_pages_FromDB(db)
        self.assertEqual(len(pages), 1)
        self.assertEqual(len(pages[0]['embed'].fields), 1)
        self.assertIn('online', pages[0]['embed'].fields[0].name)
        self.assertEqual(await app.create_pages_FromDB({'offline': db['offline']}), [])
        self.assertEqual(len(db), 3)

    async def test_history_button_opens_paginated_history(self):
        record = app.merge_server_observation(None, ServerHistoryTests().observation(), ['Alice'])
        view = app.create_history_view('test', record)
        interaction = SimpleNamespace()
        with patch.object(app.Paginator, 'respond', new_callable=AsyncMock) as respond:
            await view.children[0].callback(interaction)
        respond.assert_awaited_once_with(interaction=interaction, ephemeral=True)


class InteractiveCardTests(unittest.IsolatedAsyncioTestCase):
    async def test_cache_combines_concurrent_requests_and_expires(self):
        cache = app.AsyncTTLCache(limit=2)
        fetch = AsyncMock(return_value={'status': 'found'})
        await asyncio.gather(*(cache.get('name', fetch, lambda value: 60) for _ in range(5)))
        await cache.get('name', fetch, lambda value: 60)
        self.assertEqual(fetch.await_count, 1)
        cache.values['name'] = (0, {'status': 'found'})
        await cache.get('name', fetch, lambda value: 60)
        self.assertEqual(fetch.await_count, 2)

    async def test_rate_limit_prevents_more_profile_requests(self):
        with patch.object(app, 'profile_retry_at', app.time.monotonic() + 60):
            with patch.object(app.aiohttp, 'ClientSession') as session:
                result = await app.fetch_player_profile('Alice')
                self.assertEqual(result['status'], 'unavailable')
                session.assert_not_called()

    async def test_expired_click_stops_before_api_lookup(self):
        interaction = self.interaction()
        interaction.created_at = app.datetime.datetime.now(app.datetime.timezone.utc)
        response = SimpleNamespace(status=404, reason='Not Found')
        interaction.response.defer.side_effect = app.discord.NotFound(
            response, {'code': 10062, 'message': 'Unknown interaction'})
        view = app.player_buttons('test', [{'name': 'Alice'}])
        with patch.object(app, 'player_card', new_callable=AsyncMock) as card:
            with self.assertLogs(app.logger, level='WARNING'):
                await view.children[0].callback(interaction)
            card.assert_not_awaited()
        interaction.followup.send.assert_not_awaited()

    async def test_click_acknowledges_with_loading_before_profile_request(self):
        interaction = self.interaction()
        async def build_card(*args):
            interaction.response.defer.assert_awaited_once_with(ephemeral=True, invisible=False)
            return app.discord.Embed(title='Player Information')
        with patch.object(app, 'player_card', side_effect=build_card):
            await app.player_buttons('test', [{'name': 'Alice'}]).children[0].callback(interaction)

    def interaction(self):
        return SimpleNamespace(response=SimpleNamespace(edit_message=AsyncMock(),
                               send_message=AsyncMock(), defer=AsyncMock()),
                               followup=SimpleNamespace(send=AsyncMock()),
                               edit_original_response=AsyncMock())

    async def test_refresh_updates_existing_card_and_history(self):
        initial = app.merge_server_observation(None, ServerHistoryTests().observation(), ['Alice'])
        updated = app.merge_server_observation(initial, ServerHistoryTests().observation(motd='New server'), ['Bob'])
        view = app.create_history_view('test', initial)
        interaction = self.interaction()
        with patch.object(app, 'refresh_server_record', return_value=updated) as refresh:
            await view.children[1].callback(interaction)
            refresh.assert_called_once_with('test')
        embed = interaction.edit_original_response.await_args_list[0].kwargs['embed']
        self.assertIn('identity changed', embed.description)
        self.assertFalse(view.children[1].disabled)
        with patch.object(app.Paginator, 'respond', new_callable=AsyncMock) as respond:
            with patch.object(app, 'player_buttons', wraps=app.player_buttons) as buttons:
                await view.children[0].callback(self.interaction())
                self.assertEqual(buttons.call_args.args[1][0]['name'], 'Bob')
        with patch.object(app, 'refresh_server_record') as refresh:
            await view.children[1].callback(self.interaction())
            refresh.assert_not_called()

    async def test_offline_and_failed_refresh_restore_controls(self):
        initial = app.merge_server_observation(None, ServerHistoryTests().observation(), [])
        view = app.create_history_view('test', initial)
        interaction = self.interaction()
        with patch.object(app, 'refresh_server_record', return_value=dict(initial, available=False)):
            await view.children[1].callback(interaction)
        embed = interaction.edit_original_response.await_args_list[0].kwargs['embed']
        self.assertIn('did not respond', embed.description)
        view = app.create_history_view('test', initial)
        interaction = self.interaction()
        with patch.object(app, 'refresh_server_record', side_effect=OSError('disk')):
            with self.assertLogs(app.logger, level='ERROR'):
                await view.children[1].callback(interaction)
        self.assertFalse(view.children[1].disabled)
        interaction.followup.send.assert_awaited_once()

    async def test_numbered_player_buttons_match_each_page(self):
        record = app.merge_server_observation(None, ServerHistoryTests().observation(),
                                              ['Alice', 'Bob', 'Carol', 'Dave', 'Eve', 'Frank'])
        view = app.create_history_view('test', record)
        with patch.object(app.Paginator, 'respond', new_callable=AsyncMock):
            with patch.object(app, 'player_buttons', wraps=app.player_buttons) as factory:
                await view.children[0].callback(self.interaction())
        self.assertEqual([len(call.args[1]) for call in factory.call_args_list], [5, 1])
        for call in factory.call_args_list:
            buttons = app.player_buttons(*call.args)
            for i, button in enumerate(buttons.children):
                self.assertEqual(button.label, str(i + 1))
                with patch.object(app, 'player_card', new_callable=AsyncMock) as card:
                    card.return_value = app.discord.Embed(title='Player Information')
                    await button.callback(self.interaction())
                    self.assertEqual(card.await_args.args[1]['name'], call.args[1][i]['name'])

    async def test_profile_api_handles_not_found_errors_and_valid_uuid(self):
        for status, payload, expected in [
            (200, {'id': '069a79f444e94726a5befca90e38aaf5', 'name': 'Notch'}, 'found'),
            (404, {}, 'not_found'), (204, {}, 'not_found'),
            (429, {}, 'unavailable'), (500, {}, 'unavailable'),
            (200, {'id': 'invalid', 'name': 'Notch'}, 'unavailable'),
        ]:
            with self.subTest(status=status, payload=payload):
                response = SimpleNamespace(status=status, headers={}, json=AsyncMock(return_value=payload))
                session = MagicMock()
                session.get.return_value.__aenter__ = AsyncMock(return_value=response)
                with patch.object(app.aiohttp, 'ClientSession') as client:
                    client.return_value.__aenter__.return_value = session
                    with patch.object(app, 'profile_retry_at', 0):
                        result = await app.fetch_player_profile('Notch')
                self.assertEqual(result['status'], expected)
        with patch.object(app.aiohttp, 'ClientSession', side_effect=asyncio.TimeoutError):
            self.assertEqual((await app.fetch_player_profile('Notch'))['status'], 'unavailable')

    async def test_player_face_uses_resolved_uuid_only(self):
        entry = {'name': 'Notch'}
        with patch.object(app, 'lookup_player_profile', new_callable=AsyncMock) as lookup:
            lookup.return_value = {'status': 'found', 'id': '069a79f444e94726a5befca90e38aaf5'}
            embed = await app.player_card('test', entry)
            self.assertIn(lookup.return_value['id'], embed.thumbnail.url)
            self.assertIsNone(embed.footer)
            lookup.return_value = {'status': 'unavailable'}
            embed = await app.player_card('test', entry)
            self.assertIn('/avatar/Notch/', embed.thumbnail.url)
            self.assertIn('unavailable', embed.fields[-1].value)

    async def test_face_is_uploaded_as_matching_embed_attachment(self):
        embed = app.discord.Embed(title='Player Information')
        embed.set_thumbnail(url='https://crafatar.com/avatars/test')
        interaction = self.interaction()
        with patch.object(app, 'download_player_face', new_callable=AsyncMock, return_value=b'png'):
            await app.send_player_card(interaction, embed)
        sent = interaction.followup.send.await_args.kwargs
        self.assertEqual(sent['embed'].thumbnail.url, 'attachment://face.png')
        self.assertEqual(sent['file'].filename, 'face.png')
        self.assertTrue(sent['ephemeral'])

    async def test_failed_face_download_still_sends_profile_and_link(self):
        embed = app.discord.Embed(title='Player Information')
        embed.set_thumbnail(url='https://crafatar.com/avatars/test')
        interaction = self.interaction()
        with patch.object(app, 'download_player_face', new_callable=AsyncMock, return_value=None):
            await app.send_player_card(interaction, embed)
        sent = interaction.followup.send.await_args.kwargs
        self.assertNotIn('file', sent)
        self.assertIn('Open skin image', sent['embed'].fields[-1].value)


class WorkerTests(unittest.TestCase):
    def test_bad_response_does_not_kill_worker_or_leave_pending_tasks(self):
        jobs = queue.Queue()
        jobs.put('bad')
        jobs.put('good')
        with patch.object(app, 'ping_server', side_effect=[ValueError('bad response'), None]) as ping:
            with self.assertLogs(app.logger, level='ERROR'):
                app.worker(jobs)
        self.assertEqual(ping.call_count, 2)
        self.assertEqual(jobs.unfinished_tasks, 0)

    def test_each_pass_is_created_once_and_finishes_before_next(self):
        seen = []
        lock = threading.Lock()
        passes = []

        def make_queue():
            if passes:
                self.assertEqual(len(seen), 40)
                self.assertEqual(passes[-1].unfinished_tasks, 0)
            jobs = queue.Queue()
            for i in range(40):
                jobs.put((len(passes), i))
            passes.append(jobs)
            if len(passes) == 2:
                app.ngrok_settings.recreate = False
            return jobs

        def ping(ip):
            with lock:
                seen.append(ip)

        with patch.object(app.ngrok_settings, 'recreate', True):
            with patch.object(app, 'create_queue_ip', side_effect=make_queue):
                with patch.object(app, 'ping_server', side_effect=ping):
                    app.scan_loop()
        self.assertEqual(len(passes), 2)
        self.assertEqual(len(seen), 80)
        self.assertEqual(len(set(seen)), 80)

    def test_coordinator_observes_worker_exception(self):
        with patch.object(app, 'create_queue_ip', return_value=queue.Queue()):
            with patch.object(app, 'worker', side_effect=RuntimeError('worker failed')):
                with self.assertRaisesRegex(RuntimeError, 'worker failed'):
                    app.scan_loop()


class CommandLogTests(unittest.IsolatedAsyncioTestCase):
    async def test_start_is_logged_before_long_command_finishes(self):
        entered = asyncio.Event()
        release = asyncio.Event()
        ctx = SimpleNamespace(author=SimpleNamespace(id=1), guild=None, channel=None)

        @app.log_to_webhook('test-url')
        async def long_command(ctx):
            entered.set()
            await release.wait()

        with patch.object(app, 'send_webhook_log', new_callable=AsyncMock) as send:
            task = asyncio.create_task(long_command(ctx))
            await entered.wait()
            await asyncio.gather(*app.webhook_tasks)
            self.assertFalse(task.done())
            self.assertEqual(send.await_args.args[-1], 'started')
            release.set()
            await task
            await asyncio.gather(*app.webhook_tasks)
            self.assertEqual([call.args[-1] for call in send.await_args_list], ['started', 'completed'])

    async def test_failure_is_logged_and_propagated(self):
        ctx = SimpleNamespace(author=SimpleNamespace(id=1), guild=None, channel=None)

        @app.log_to_webhook('test-url')
        async def broken_command(ctx):
            raise ValueError('command failed')

        with patch.object(app, 'send_webhook_log', new_callable=AsyncMock) as send:
            with self.assertLogs(app.logger, level='ERROR'):
                with self.assertRaises(ValueError):
                    await broken_command(ctx)
            await asyncio.gather(*app.webhook_tasks)
            self.assertEqual([call.args[-1] for call in send.await_args_list], ['started', 'failed'])


if __name__ == '__main__':
    unittest.main()
