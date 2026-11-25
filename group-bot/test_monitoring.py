import tempfile
from pathlib import Path
import unittest
from unittest.mock import Mock, patch
import bot
import monitoring


class Monitoring(unittest.TestCase):
    def setUp(self):
        self.db = bot.database(':memory:')
        self.config = {'owner_chat_id': 123, 'chat_id': -456}

    def tearDown(self):
        self.db.close()

    def observe(self, error):
        monitoring.observe(self.db, 'source:game', 'Dota 2', error)

    def test_threshold_reset_single_warning_and_recovery(self):
        api = Mock()
        self.observe('TimeoutError')
        self.observe('TimeoutError')
        monitoring.send_diagnostics(self.db, self.config, api)
        api.assert_not_called()
        self.observe(None)
        for _ in range(3):
            self.observe('TimeoutError')
        monitoring.send_diagnostics(self.db, self.config, api)
        monitoring.send_diagnostics(self.db, self.config, api)
        self.assertEqual(api.call_count, 1)
        self.assertEqual(api.call_args.args[2]['chat_id'], 123)
        self.observe(None)
        monitoring.send_diagnostics(self.db, self.config, api)
        monitoring.send_diagnostics(self.db, self.config, api)
        self.assertEqual(api.call_count, 2)
        self.assertIn('восстановлена', api.call_args.args[2]['text'])

    def test_failed_warning_retries_but_does_not_report_old_outage_after_recovery(self):
        for _ in range(3):
            self.observe('URLError')
        with self.assertRaises(RuntimeError):
            monitoring.send_diagnostics(self.db, self.config, Mock(side_effect=RuntimeError))
        self.assertEqual(self.db.execute('SELECT warned FROM incidents').fetchone()[0], 0)
        self.observe(None)
        api = Mock()
        monitoring.send_diagnostics(self.db, self.config, api)
        api.assert_not_called()

    def test_warning_survives_restart_without_repeat(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'state.sqlite3'
            db = bot.database(path)
            for _ in range(3):
                monitoring.observe(db, 'source:g', 'Game', 'HTTPError')
            monitoring.send_diagnostics(db, self.config, Mock())
            db.close()
            db = bot.database(path)
            api = Mock()
            monitoring.send_diagnostics(db, self.config, api)
            api.assert_not_called()
            monitoring.observe(db, 'source:g', 'Game', None)
            monitoring.send_diagnostics(db, self.config, api)
            api.assert_called_once()
            db.close()

    def test_delivery_failure_history_retry_and_private_recovery(self):
        game = {'id': 'g', 'name': 'Dota 2'}
        bot.remember(self.db, game, '1')
        bot.remember(self.db, game, '2')
        with patch.object(bot, 'api', side_effect=RuntimeError):
            with self.assertRaises(RuntimeError):
                bot.deliver(self.db, self.config)
        text = monitoring.history(self.db)
        self.assertIn('В очереди', text)
        self.assertIn('RuntimeError', text)
        api = Mock()
        monitoring.send_diagnostics(self.db, self.config, api)
        self.assertEqual(api.call_args.args[2]['chat_id'], 123)
        with patch.object(bot, 'api') as delivery:
            bot.deliver(self.db, self.config)
            self.assertEqual(delivery.call_args.args[2], {'chat_id': -456, 'text': 'Dota 2'})
        monitoring.send_diagnostics(self.db, self.config, api)
        self.assertIn('восстановлена', api.call_args.args[2]['text'])
        self.assertIn('Отправлено:', monitoring.history(self.db))
        self.assertNotIn('RuntimeError', monitoring.history(self.db))

    def test_test_delivery_not_repeated_after_confirmation_failure(self):
        api = Mock(side_effect=[{}, RuntimeError('owner unavailable')])
        with self.assertRaises(RuntimeError):
            monitoring.test_delivery(self.db, self.config, 9, -456, api)
        retry = Mock()
        monitoring.test_delivery(self.db, self.config, 9, -456, retry)
        retry.assert_called_once()
        self.assertEqual(retry.call_args.args[2]['chat_id'], 123)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM notices').fetchone()[0], 0)

    def test_failed_test_reports_failure_not_success(self):
        api = Mock(side_effect=[RuntimeError('secret URL'), {}])
        monitoring.test_delivery(self.db, self.config, 1, -456, api)
        text = api.call_args.args[2]['text']
        self.assertIn('не подтверждена', text)
        self.assertNotIn('secret URL', text)

    def test_private_test_only_sends_one_message(self):
        api = Mock()
        monitoring.test_delivery(self.db, self.config, 1, 123, api)
        api.assert_called_once()
        self.assertEqual('тест', api.call_args.args[2]['text'])

    def test_old_history_does_not_invent_timestamps(self):
        with self.db:
            self.db.execute("INSERT INTO notices(game,version,name,sent) VALUES ('g','1','Game',1)")
        self.assertIn('время не записано', monitoring.history(self.db))

    def test_commands_only_authorised_owner(self):
        updates = []
        for i, (sender, chat, kind, command) in enumerate([
                (999, 999, 'private', '/test'), (123, -456, 'supergroup', '/last'),
                (123, 123, 'private', '/last'), (123, 123, 'private', '/test')]):
            updates.append({'update_id': i, 'message': {'from': {'id': sender},
                            'chat': {'id': chat, 'type': kind}, 'text': command}})
        with patch.object(bot, 'api') as api:
            bot.handle_commands(self.db, self.config, {}, updates, 0)
            self.assertEqual([call.args[2]['chat_id'] for call in api.call_args_list], [123, -456, 123])


if __name__ == '__main__':
    unittest.main()
