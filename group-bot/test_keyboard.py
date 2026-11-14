import json
import unittest
from unittest.mock import patch
import bot


class Keyboard(unittest.TestCase):
    def test_keyboard_attached_only_to_owner_private_messages(self):
        config = {'token': 'test', 'owner_chat_id': 123, 'chat_id': -456}
        with patch.object(bot, 'fetch', return_value=json.dumps({'ok': True, 'result': {}})) as fetch:
            payload = {'chat_id': 123, 'text': 'status'}
            bot.api(config, 'sendMessage', payload)
            markup = fetch.call_args.args[1]['reply_markup']
            self.assertEqual([row[0]['text'] for row in markup['keyboard']], list(bot.BUTTON_COMMANDS))
            self.assertNotIn('inline_keyboard', markup)
            self.assertNotIn('reply_markup', payload)
            bot.api(config, 'sendMessage', {'chat_id': -456, 'text': 'Dota 2'})
            self.assertNotIn('reply_markup', fetch.call_args.args[1])

    def test_all_buttons_dispatch_to_current_commands(self):
        db = bot.database(':memory:')
        config = {'owner_chat_id': 123}
        health = dict(started=0, last_scan=None, scanning=False, total=0, delivery_error=None)
        try:
            for i, label in enumerate(bot.BUTTON_COMMANDS):
                update = {'update_id': i, 'message': {'from': {'id': 123},
                          'chat': {'id': 123, 'type': 'private'}, 'text': label}}
                with patch.object(bot, 'api') as api:
                    bot.handle_commands(db, config, health, [update], i)
                    api.assert_called_once()
                    self.assertEqual(api.call_args.args[2]['chat_id'], 123)
        finally:
            db.close()


if __name__ == '__main__':
    unittest.main()
