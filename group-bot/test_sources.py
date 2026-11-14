import unittest
from unittest.mock import patch
import sources


class Sources(unittest.TestCase):
    def test_osu_error_response_is_not_a_version(self):
        with patch.object(sources, 'get_json', return_value={'response': 'no stream specified'}):
            with self.assertRaises(sources.SourceUnavailable):
                sources.read_version({'source': 'osu'})

    def test_fortnite_hotfix_changes_identity(self):
        game = {'source': 'fortnite_build'}
        with patch.object(sources, 'get_json', side_effect=[
                {'data': {'build': '++Fortnite+Release-43.10-CL-100'}},
                {'data': {'build': '++Fortnite+Release-43.10-CL-101'}}]):
            self.assertNotEqual(sources.read_version(game), sources.read_version(game))

    def test_fortnite_rejects_test_channel(self):
        with patch.object(sources, 'get_json', return_value={'data': {'build': '++Fortnite+Test-43.10-CL-100'}}):
            with self.assertRaises(ValueError):
                sources.read_version({'source': 'fortnite_build'})

    def test_news_rejects_previews_and_test_servers(self):
        for title in ['New features of the November update', 'Patch 7.2 Preview',
                      'Обновление на ПТС [10 ноября]', 'Upcoming hotfix',
                      'Update 43.20 coming tomorrow', 'Playtest update', 'Dev roadmap update']:
            with self.subTest(title=title):
                self.assertFalse(sources.released_patch(title))
        for title in ['Minor Update - 11-04-2026', 'Hotfix: Timer Change',
                      'Small update, October 28, 2026', 'Обновление в игре [5 ноября]',
                      'Update 2.9.1', 'Обновление [12 ноября]', 'Patch Notes 7.2']:
            with self.subTest(title=title):
                self.assertTrue(sources.released_patch(title))

    def test_news_uses_latest_published_official_patch(self):
        items = [dict(title='Hotfix', date=20, gid='older', feedlabel='Community Announcements'),
                 dict(title='Hotfix', date=2000, gid='future', feedlabel='Community Announcements'),
                 dict(title='New features of the November update', date=80, gid='preview', feedlabel='Community Announcements'),
                 dict(title='Hotfix', date=90, gid='external', feedlabel='PC Gamer'),
                 dict(title='Hotfix', date=60, gid='correct', feedlabel='Community Announcements')]
        with patch.object(sources, 'get_json', return_value={'appnews': {'newsitems': items}}), \
                patch.object(sources.time, 'time', return_value=100):
            self.assertEqual(sources.read_version({'source': 'steam_patch_news', 'appid': 1}), 'correct')

    def test_lol_failure_never_synthesizes_version_from_announcements(self):
        with patch.object(sources, 'get_json', side_effect=ValueError), \
                patch.object(sources, 'fetch_public_page') as page:
            with self.assertRaises(ValueError):
                sources.read_version({'source': 'riot_lol'})
            page.assert_not_called()

    def test_steam_uses_public_not_beta(self):
        data = {'data': {'1': {'common': {'releasestate': 'released'}, 'depots': {'branches': {
            'public': {'buildid': '10'}, 'beta': {'buildid': '99'}}}}}}
        with patch.object(sources, 'get_json', return_value=data):
            self.assertEqual(sources.read_version({'source': 'steam', 'appid': 1}), '10')

    def test_other_launcher_is_not_classified_as_steam_build_coverage(self):
        self.assertEqual(sources.source_quality({'source': 'steam', 'launcher': 'Epic Games'}), 'indirect')
        self.assertEqual(sources.source_quality({'source': 'steam', 'launcher': 'Steam'}), 'build')

    def test_warface_does_not_query_international_steam_version(self):
        page = '<a href="/news/999.html">Обновление на ПТС [10 ноября]</a>' \
               '<a href="/news/100.html">Обновление [5 ноября]</a>'
        with patch.object(sources, 'fetch_public_page', return_value=page), \
                patch.object(sources, 'get_json') as steam:
            self.assertEqual(sources.read_version({'source': 'warface_news'}), '100')
            steam.assert_not_called()


if __name__ == '__main__':
    unittest.main()
