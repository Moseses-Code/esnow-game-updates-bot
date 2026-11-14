import unittest
import json
import sqlite3
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
import bot


class Notifications(unittest.TestCase):
    def setUp(self):
        self.db = bot.database(':memory:')
        self.game = {'id': 'cs2', 'name': 'Counter-Strike 2'}

    def tearDown(self):
        self.db.close()

    def test_baseline_and_same_version_are_silent(self):
        bot.remember(self.db, self.game, '1')
        bot.remember(self.db, self.game, '1')
        with patch.object(bot, 'api') as api:
            bot.deliver(self.db, {'chat_id': -123})
            api.assert_not_called()

    def test_update_sends_only_game_name_once(self):
        bot.remember(self.db, self.game, '1')
        bot.remember(self.db, self.game, '2')
        with patch.object(bot, 'api') as api:
            bot.deliver(self.db, {'chat_id': -123})
            bot.deliver(self.db, {'chat_id': -123})
            api.assert_called_once_with({'chat_id': -123}, 'sendMessage',
                                        {'chat_id': -123, 'text': 'Counter-Strike 2'})

    def test_failed_delivery_stays_in_queue(self):
        bot.remember(self.db, self.game, '1')
        bot.remember(self.db, self.game, '2')
        with patch.object(bot, 'api', side_effect=RuntimeError):
            with self.assertRaises(RuntimeError):
                bot.deliver(self.db, {'chat_id': -123})
        self.assertEqual(self.db.execute('SELECT sent FROM notices').fetchone()[0], 0)
        with patch.object(bot, 'api') as api:
            bot.deliver(self.db, {'chat_id': -123})
            api.assert_called_once()

    def test_private_until_group_bound_without_replaying_sent_notices(self):
        config = {'owner_chat_id': 456}
        bot.remember(self.db, self.game, '1')
        bot.remember(self.db, self.game, '2')
        with patch.object(bot, 'api') as api:
            bot.deliver(self.db, config)
            self.assertEqual(api.call_args.args[2], {'chat_id': 456, 'text': 'Counter-Strike 2'})
            config.update(chat_id=-123, chat_type='supergroup')
            bot.deliver(self.db, config)
            self.assertEqual(api.call_count, 1)
            bot.remember(self.db, self.game, '3')
            bot.deliver(self.db, config)
            self.assertEqual(api.call_args.args[2], {'chat_id': -123, 'text': 'Counter-Strike 2'})

    def test_group_error_does_not_redirect_to_private(self):
        config = {'owner_chat_id': 456, 'chat_id': -123}
        bot.remember(self.db, self.game, '1')
        bot.remember(self.db, self.game, '2')
        with patch.object(bot, 'api', side_effect=RuntimeError) as api:
            with self.assertRaises(RuntimeError):
                bot.deliver(self.db, config)
            self.assertEqual(api.call_count, 1)
            self.assertEqual(api.call_args.args[2]['chat_id'], -123)

    def test_missing_or_invalid_destination_rejected(self):
        for config in ({}, {'owner_chat_id': -12}, {'chat_id': 123, 'owner_chat_id': 456}):
            with self.subTest(config=config), self.assertRaises(ValueError):
                bot.destination(config)

    def test_repeated_release_after_rollback_is_delivered(self):
        for version in ('A', 'B', 'A', 'B', 'B'):
            bot.remember(self.db, self.game, version)
        self.assertEqual(self.db.execute('SELECT version FROM notices ORDER BY id').fetchall(),
                         [('B',), ('A',), ('B',)])

    def test_source_change_establishes_baseline_without_fake_update(self):
        bot.remember(self.db, self.game, 'old-format')
        self.game['source_revision'] = 2
        bot.remember(self.db, self.game, 'new-format-1')
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM notices').fetchone()[0], 0)
        bot.remember(self.db, self.game, 'new-format-2')
        self.assertEqual(self.db.execute('SELECT version FROM notices').fetchall(), [('new-format-2',)])

    def test_legacy_database_migrates_queue_and_versions_without_resending(self):
        with TemporaryDirectory() as folder:
            path = Path(folder) / 'state.sqlite3'
            legacy = sqlite3.connect(path)
            legacy.executescript('''
                CREATE TABLE versions (game TEXT PRIMARY KEY, value TEXT NOT NULL);
                INSERT INTO versions VALUES ('cs2', 'B');
                CREATE TABLE notices (id INTEGER PRIMARY KEY, game TEXT NOT NULL,
                    version TEXT NOT NULL, name TEXT NOT NULL, sent INTEGER NOT NULL DEFAULT 0,
                    UNIQUE(game, version));
                INSERT INTO notices VALUES (1, 'cs2', 'A', 'Counter-Strike 2', 1);
                INSERT INTO notices VALUES (2, 'cs2', 'B', 'Counter-Strike 2', 0);
            ''')
            legacy.close()
            db = bot.database(path)
            bot.remember(db, self.game, 'A')
            self.assertEqual(db.execute('SELECT version,sent FROM notices ORDER BY id').fetchall(),
                             [('A', 1), ('B', 0), ('A', 0)])
            db.close()
            db = bot.database(path)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM notices').fetchone()[0], 3)
            db.close()


class Setup(unittest.TestCase):
    def test_private_then_owner_only_group_binding(self):
        def update(number, sender, chat_id, chat_type, command):
            return {'update_id': number, 'message': {
                'from': {'id': sender}, 'chat': {'id': chat_id, 'type': chat_type}, 'text': command}}

        with TemporaryDirectory() as folder, patch.object(bot, 'ROOT', Path(folder)), \
                patch.object(bot.getpass, 'getpass', return_value='test-token'), \
                patch.object(bot.secrets, 'token_hex', return_value='code'), patch('builtins.print'):
            private = [update(1, 456, 456, 'private', '/start wrong'),
                       update(2, 456, 456, 'private', '/start code')]
            with patch.object(bot, 'api', side_effect=[{'username': 'testbot'}, private, []]):
                bot.setup()
            settings = Path(folder) / 'settings.json'
            config = json.loads(settings.read_text())
            self.assertEqual(bot.destination(config), 456)
            group = [update(3, 999, -123, 'supergroup', '/connect@testbot code'),
                     update(4, 456, -123, 'supergroup', '/connect@testbot code')]
            with patch.object(bot, 'api', side_effect=[{'username': 'testbot'}, group,
                                                       {'status': 'administrator'}, []]) as api:
                bot.setup(group=True)
                members = [call for call in api.call_args_list if call.args[1] == 'getChatMember']
                self.assertEqual(len(members), 1)
                self.assertEqual(members[0].args[2]['user_id'], 456)
            config = json.loads(settings.read_text())
            self.assertEqual(bot.destination(config), -123)
            self.assertEqual(config['owner_chat_id'], 456)


class Status(unittest.TestCase):
    def setUp(self):
        self.db = bot.database(':memory:')
        self.config = {'owner_chat_id': 456}
        self.health = dict(started=0, last_scan=None, scanning=True, checked=2,
                           total=122, delivery_error=None)

    def tearDown(self):
        self.db.close()

    def test_first_scan_and_moscow_time(self):
        text = bot.status_text(self.db, self.config, self.health)
        self.assertIn('01.01 03:00:00 МСК', text)
        self.assertIn('Первая проверка ещё не завершена', text)
        self.assertIn('Сейчас проверено: 2/122', text)
        self.assertIn('твой личный чат', text)

    def test_completed_scan_queue_group_and_delivery_error(self):
        self.config['chat_id'] = -123
        self.health.update(scanning=False, last_scan=dict(finished=60, ok=120, errors=2),
                           delivery_error='URLError')
        bot.remember(self.db, {'id': 'pubg', 'name': 'PUBG'}, '1')
        bot.remember(self.db, {'id': 'pubg', 'name': 'PUBG'}, '2')
        text = bot.status_text(self.db, self.config, self.health)
        for expected in ('120/122', 'Ошибок источников: 2', 'рабочая группа',
                         'В очереди отправки: 1', 'Ошибка отправки: URLError'):
            self.assertIn(expected, text)
        self.assertNotIn('Сейчас проверено:', text)

    def test_commands_only_in_owner_private_chat_and_offset_persisted(self):
        updates = []
        for i, (sender, chat, kind, text) in enumerate([
                (999, 999, 'private', '/status'), (456, -123, 'supergroup', '/status'),
                (456, 456, 'private', '/status'), (456, 456, 'private', '/start'),
                (456, 456, 'private', 'PUBG')], start=1):
            updates.append({'update_id': i, 'message': {'from': {'id': sender},
                            'chat': {'id': chat, 'type': kind}, 'text': text}})
        with patch.object(bot, 'api') as api:
            offset = bot.handle_commands(self.db, self.config, self.health, updates, 0)
            self.assertEqual(api.call_count, 2)
            for call in api.call_args_list:
                self.assertEqual(call.args[2]['chat_id'], 456)
                self.assertIn('ESnow работает', call.args[2]['text'])
        self.assertEqual(offset, 6)
        self.assertEqual(self.db.execute('SELECT value FROM metadata').fetchone()[0], '6')

    def test_failed_status_reply_is_not_acknowledged(self):
        update = {'update_id': 10, 'message': {'from': {'id': 456},
                  'chat': {'id': 456, 'type': 'private'}, 'text': '/status'}}
        with patch.object(bot, 'api', side_effect=RuntimeError), self.assertRaises(RuntimeError):
            bot.handle_commands(self.db, self.config, self.health, [update], 0)
        self.assertIsNone(self.db.execute('SELECT value FROM metadata').fetchone())


if __name__ == '__main__':
    unittest.main()
