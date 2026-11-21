import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yt_dlp
from pydantic import ValidationError

import main
import ssflix


class QuietLogger:
    def debug(self, text): pass
    def warning(self, text): pass
    def error(self, text): pass


class DownloadProgressTests(unittest.TestCase):
    def test_five_videos_have_independent_progress_and_continue_after_failure(self):
        urls = [f'https://example.com/video/{i}' for i in range(5)]
        snapshots = []
        with tempfile.TemporaryDirectory() as temp:
            def download(url, quality, outdir, hook, browser, audio_language='auto'):
                snapshots.append(copy.deepcopy(main.jobs['progress-test']))
                i = urls.index(url)
                if i == 1:
                    raise RuntimeError('Hindi audio unavailable')
                hook({'status': 'metadata', 'title': f'Episode {i + 1}'})
                hook({'status': 'downloading', 'downloaded_bytes': 40, 'total_bytes': 100,
                      'filename': 'video.mp4', 'speed': 20, 'eta': 3})
                snapshots.append(copy.deepcopy(main.jobs['progress-test']))
                hook({'status': 'finished'})
                hook({'status': 'downloading', 'downloaded_bytes': 1, 'total_bytes': 10,
                      'filename': 'audio.m4a', 'speed': 10, 'eta': 1})
                snapshots.append(copy.deepcopy(main.jobs['progress-test']))
                return Path(temp)/f'episode-{i}.mp4'

            main.jobs['progress-test'] = {'items': main.playlist_items(urls)}
            try:
                with patch.object(main, 'PLAYLIST_ROOT', Path(temp)), patch.object(main, 'download_item', side_effect=download):
                    main.run_playlist_job('progress-test', urls, '720', 'Show', None, 'hi')
                job = main.jobs['progress-test']
                self.assertEqual([i['status'] for i in snapshots[0]['items']], ['starting'] + ['queued']*4)
                first = snapshots[1]['items'][0]
                self.assertEqual((first['percent'], first['downloaded_bytes'], first['speed'], first['eta']), (40, 40, 20, 3))
                self.assertEqual(snapshots[2]['items'][0]['percent'], 99)
                self.assertEqual(snapshots[2]['items'][0]['downloaded_bytes'], 41)
                self.assertEqual(snapshots[3]['items'][0]['status'], 'done')
                self.assertEqual(snapshots[4]['items'][1]['status'], 'error')
                self.assertEqual([i['status'] for i in job['items']], ['done','error','done','done','done'])
                self.assertEqual((job['done'], job['failed'], job['status']), (4, 1, 'done'))
                self.assertIn('Hindi audio unavailable', job['items'][1]['error'])
            finally:
                del main.jobs['progress-test']

    def test_language_selection_never_falls_back_to_english(self):
        formats = [
            {'format_id':'v720', 'url':'https://example.com/video', 'ext':'mp4', 'height':720, 'vcodec':'h264', 'acodec':'none'},
            {'format_id':'v1080', 'url':'https://example.com/video', 'ext':'mp4', 'height':1080, 'vcodec':'h264', 'acodec':'none'},
            {'format_id':'en', 'url':'https://example.com/en', 'ext':'m4a', 'language':'en', 'vcodec':'none', 'acodec':'aac'},
            {'format_id':'hi', 'url':'https://example.com/hi', 'ext':'m4a', 'language':'hi-IN', 'vcodec':'none', 'acodec':'aac'},
        ]
        opts = {'quiet':True, 'logger':QuietLogger(), 'format':main.format_selector('720', 'hi')}
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.process_ie_result({'id':'test', 'title':'test', 'extractor':'generic', 'formats':copy.deepcopy(formats)}, download=False)
            self.assertEqual([f['format_id'] for f in info['requested_formats']], ['v720', 'hi'])
            with self.assertRaises((yt_dlp.utils.DownloadError, yt_dlp.utils.ExtractorError)):
                ydl.process_ie_result({'id':'test', 'title':'test', 'extractor':'generic', 'formats':copy.deepcopy(formats[:3])}, download=False)
            # Unlabelled audio cannot be used as proof of a Hindi dub.
            formats[3].pop('language')
            with self.assertRaises((yt_dlp.utils.DownloadError, yt_dlp.utils.ExtractorError)):
                ydl.process_ie_result({'id':'test', 'title':'test', 'extractor':'generic', 'formats':formats}, download=False)

    def test_unsupported_audio_language_is_rejected(self):
        with self.assertRaises(ValidationError):
            main.PlaylistDownloadIn(urls=['https://example.com/video'], audio_language='invalid')

    def test_hindi_filename_does_not_reuse_existing_english_file(self):
        with tempfile.TemporaryDirectory() as temp, patch('ssflix.selected_entry', return_value={'title':'Show S02E01'}), \
             patch('ssflix.resolve_media', return_value={'url':'https://example.com/video', 'headers':{}}) as resolve, \
             patch('main.yt_dlp.YoutubeDL') as ydl, patch('main.history.record'):
            path = Path(temp)/'Show S02E01 [Hindi].mp4'
            ydl.return_value.__enter__.return_value.extract_info.return_value = {'requested_downloads':[{'filepath':str(path)}]}
            main.download_item('https://ssflix.pro/watch/serie/show-12/season-2/episode-1', '720', Path(temp), audio_language='hi')
            self.assertIn('[Hindi]', ydl.call_args.args[0]['outtmpl'])
            self.assertIn('language', ydl.call_args.args[0]['format'])
            self.assertEqual(resolve.call_args.args[1], 'hi')

    def test_server_five_dub_label_can_identify_unlabelled_manifest(self):
        streams = [
            {'name':'Orion · Original', 'url':'/api/orion/original/master.m3u8'},
            {'name':'Orion · Hindi', 'url':'/api/orion/hindi/master.m3u8'},
        ]
        result = ssflix.select_labelled_stream(streams, 'hi', 'https://embed.vidrift.net/embed/tv/12/2/1')
        self.assertEqual(result['audio_language'], 'hi')
        self.assertEqual(result['url'], 'https://embed.vidrift.net/api/orion/hindi/master.m3u8')
        self.assertIsNone(ssflix.select_labelled_stream(streams, 'ur', 'https://embed.vidrift.net/'))
        with self.assertRaises(ValidationError):
            main.PlaylistDownloadIn(urls=['https://example.com'], ssflix_server=7)

    def test_selected_server_and_verified_dub_are_preserved(self):
        with tempfile.TemporaryDirectory() as temp, patch('ssflix.selected_entry', return_value={'title':'Show S02E01'}), \
             patch('ssflix.resolve_media', return_value={'url':'https://example.com/hindi.m3u8', 'headers':{}, 'audio_language':'hi', 'server':5}) as resolve, \
             patch('main.yt_dlp.YoutubeDL') as ydl, patch('main.history.record'):
            path = Path(temp)/'Show S02E01 [Hindi].mp4'
            ydl.return_value.__enter__.return_value.extract_info.return_value = {'requested_downloads':[{'filepath':str(path)}]}
            main.download_item('https://ssflix.pro/watch/serie/show-12/season-2/episode-1', '720', Path(temp), audio_language='hi', ssflix_server=5)
            self.assertEqual(resolve.call_args.kwargs['server'], 5)
            self.assertEqual(ydl.call_args.args[0]['format'], main.format_selector('720'))
            self.assertIn('[Hindi]', ydl.call_args.args[0]['outtmpl'])


if __name__ == '__main__':
    unittest.main()
