import datetime as dt
import json
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path
import bot

class MonitorTests(unittest.TestCase):
    def setUp(self):
        self.c = bot.connect(':memory:')
        self.g = {'id':'a','name':'Game','launcher':'Steam','source':'steam','appid':1}
    def tearDown(self):self.c.close()
    def stamp(self, s):return dt.datetime.fromisoformat(s).replace(tzinfo=bot.MSK).timestamp()
    def test_moscow_boundaries(self):
        for s, expected in [('2026-11-12T08:29:59','2026-11-11T20:30'),('2026-11-12T08:30','2026-11-12T08:30'),('2026-11-12T20:30','2026-11-12T20:30'),('2026-11-12T00:00','2026-11-11T20:30')]:
            self.assertEqual(bot.boundary(self.stamp(s)),self.stamp(expected))
    def test_baseline_not_update(self):
        bot.record(self.c,self.g,'1',None,100)
        self.assertEqual(self.c.execute('SELECT COUNT(*) FROM events').fetchone()[0],0)
    def test_change_and_duplicate(self):
        for t,v in [(100,'1'),(200,'2'),(300,'2')]:bot.record(self.c,self.g,v,None,t)
        self.assertEqual(self.c.execute('SELECT COUNT(*) FROM events').fetchone()[0],1)
    def test_failure_preserves_version_and_marks_gap(self):
        bot.record(self.c,self.g,'1',None,100)
        bot.record(self.c,self.g,None,'HTTPError',200)
        self.assertEqual(self.c.execute('SELECT version FROM games').fetchone()[0],'1')
        bot.record(self.c,self.g,'2',None,3000)
        self.assertEqual(self.c.execute('SELECT uncertain FROM events').fetchone()[0],1)
    def test_half_day_events(self):
        bot.record(self.c,self.g,'1',None,100)
        bot.record(self.c,self.g,'2',None,200)
        self.assertIn('Обнаружены изменения',bot.report(self.c,[self.g],100,200))
        self.assertNotIn('Обнаружены изменения',bot.report(self.c,[self.g],200,300))
    def test_unconnected_never_counted_as_checked(self):
        g={'id':'b','name':'Unknown','launcher':'Other'}
        s=bot.report(self.c,[g],0,100)
        self.assertIn('Не подключено: 1',s)
        self.assertIn('нужна ручная проверка',s)
    def test_minecraft_excludes_snapshot(self):
        d={'latest':{'release':'1.0','snapshot':'2.0-pre'},'versions':[{'id':'1.0','type':'release'},{'id':'2.0-pre','type':'snapshot'}]}
        with patch.object(bot,'get_json',return_value=d):self.assertEqual(bot.read_version({'source':'minecraft'}),'1.0')
    def test_steam_without_optional_release_state(self):
        d={'data':{'1':{'common':{},'depots':{'branches':{'public':{'buildid':'42'}}}}}}
        with patch.object(bot,'get_json',return_value=d):self.assertEqual(bot.read_version(self.g),'42')
    def test_only_public_steam_branch(self):
        d={'data':{'1':{'common':{'releasestate':'released'},'depots':{'branches':{'public':{'buildid':'42'},'beta':{'buildid':'99'}}}}}}
        with patch.object(bot,'get_json',return_value=d):self.assertEqual(bot.read_version(self.g),'42')
    def test_outbox_restarts_no_repeat_when_confirmed(self):
        with self.c:
            self.c.execute('INSERT INTO outbox(slot,parts) VALUES (?,?)',(1,json.dumps(['one','two'])))
            self.c.execute('INSERT INTO subscribers VALUES (?,?,?,?)',(42,0,None,None))
        with patch.object(bot,'send',side_effect=[None,RuntimeError()]):
            bot.deliver(self.c,{})
        with patch.object(bot,'send') as send:
            bot.deliver(self.c,{})
            send.assert_called_once_with({},42,'two')
        with patch.object(bot,'send') as send:
            bot.deliver(self.c,{})
            send.assert_not_called()
    def test_new_subscriber_does_not_receive_old_report(self):
        with self.c:
            self.c.execute('INSERT INTO outbox(slot,parts) VALUES (?,?)',(1,json.dumps(['old'])))
            self.c.execute('INSERT INTO subscribers VALUES (?,?,?,?)',(42,2,None,None))
        with patch.object(bot,'send') as send:
            bot.deliver(self.c,{})
            send.assert_not_called()
    def test_catchup_reports_once(self):
        start=self.stamp('2026-11-11T20:30')
        with self.c:bot.put(self.c,'last_slot',start)
        bot.queue_reports(self.c,[],start+86400)
        bot.queue_reports(self.c,[],start+86400)
        self.assertEqual(self.c.execute('SELECT COUNT(*) FROM outbox').fetchone()[0],2)
    def test_persistence(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'state.db'
            c=bot.connect(path)
            with c:bot.record(c,self.g,'1',None,100)
            c.close()
            c=bot.connect(path)
            with c:bot.record(c,self.g,'2',None,200)
            self.assertEqual(c.execute('SELECT COUNT(*) FROM events').fetchone()[0],1)
            c.close()
    def test_long_report_split(self):
        chunks=bot.split_text(('Строка отчета\n')*1000)
        self.assertGreater(len(chunks),1)
        self.assertTrue(all(len(x)<=3500 for x in chunks))

if __name__=='__main__':unittest.main()
