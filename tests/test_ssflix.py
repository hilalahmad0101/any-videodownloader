import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import main
import ssflix

URL = 'https://ssflix.pro/watch/serie/into-the-badlands-47450/season-2/episode-1'


class SSFlixTests(unittest.TestCase):
    def test_links_and_host_validation(self):
        t = ssflix.parse_title(URL)
        self.assertEqual((t.id, t.season, t.episode), ('47450', 2, 1))
        self.assertEqual(ssflix.parse_title('https://ssflix.pro/serie/show-12').watch_url(3, 4),
                         'https://ssflix.pro/watch/serie/show-12/season-3/episode-4')
        self.assertEqual(ssflix.parse_title('https://ssflix.pro/movie/film-42').watch_url(),
                         'https://ssflix.pro/watch/movie/film-42')
        for bad in ['https://ssflix.pro.evil.test/movie/42', 'https://ssflix.pro/search', 'https://ssflix.pro/movie/film']:
            with self.assertRaises(ssflix.SSFlixError):
                ssflix.parse_title(bad)

    def test_catalog_uses_real_seasons_and_episodes(self):
        data = {'tv/47450': {'name': 'Show', 'seasons': [
            {'season_number': 1, 'episode_count': 1}, {'season_number': 3, 'episode_count': 1}]},
            'tv/47450/season/1': {'episodes': [{'episode_number': 1, 'name': 'Pilot'}]},
            'tv/47450/season/3': {'episodes': [{'episode_number': 7, 'name': 'Finale'}]}}
        with patch('ssflix.catalog_get', side_effect=data.__getitem__):
            result = ssflix.catalog(URL)
        self.assertEqual(result['count'], 2)
        self.assertEqual([v['season'] for v in result['videos']], [1, 3])
        self.assertTrue(result['videos'][1]['url'].endswith('/season-3/episode-7'))
        self.assertEqual(result['selected_episode'], 1)

    def test_movie_catalog(self):
        with patch('ssflix.catalog_get', return_value={'title': 'Film', 'runtime': 90}):
            result = ssflix.catalog('https://ssflix.pro/movie/film-42')
        self.assertEqual(result['count'], 1)
        self.assertEqual(result['videos'][0]['duration'], 5400)

    def test_batch_continues_and_groups_seasons(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(main, 'PLAYLIST_ROOT', Path(temp)), \
             patch.object(main, 'download_item', side_effect=[RuntimeError('Unavailable'), Path(temp)/'file.mp4']) as download:
            main.jobs['test'] = {}
            urls = [URL, URL.replace('season-2', 'season-3')]
            try:
                main.run_playlist_job('test', urls, '1080', 'Show', None)
                self.assertEqual(main.jobs['test']['failed'], 1)
                self.assertEqual(main.jobs['test']['done'], 1)
                self.assertEqual(main.jobs['test']['status'], 'done')
                self.assertEqual(download.call_args_list[0].args[2], Path(temp)/'Show'/'Season 02')
                self.assertEqual(download.call_args_list[1].args[2], Path(temp)/'Show'/'Season 03')
            finally:
                del main.jobs['test']

    def test_download_preserves_player_headers_and_episode_title(self):
        with tempfile.TemporaryDirectory() as temp, patch('ssflix.selected_entry', return_value={'title':'Show S02E01'}), \
             patch('ssflix.resolve_media', return_value={'url':'https://cdn.example/stream.m3u8', 'headers':{'Referer':'https://player.example/'}}), \
             patch('main.yt_dlp.YoutubeDL') as ydl, patch('main.history.record') as record:
            saved = Path(temp)/'Show S02E01.mp4'
            ydl.return_value.__enter__.return_value.extract_info.return_value = {'requested_downloads':[{'filepath':str(saved)}]}
            result = main.download_item(URL, '720', Path(temp))
            self.assertEqual(result, saved)
            opts = ydl.call_args.args[0]
            self.assertEqual(opts['http_headers']['Referer'], 'https://player.example/')
            self.assertIn('Show S02E01', opts['outtmpl'])
            self.assertEqual(record.call_args.args[0]['webpage_url'], URL)


if __name__ == '__main__':
    unittest.main()
