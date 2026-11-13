import unittest
import json
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


if __name__ == '__main__':
    unittest.main()
