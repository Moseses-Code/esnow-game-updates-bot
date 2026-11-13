import unittest
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


if __name__ == '__main__':
    unittest.main()
