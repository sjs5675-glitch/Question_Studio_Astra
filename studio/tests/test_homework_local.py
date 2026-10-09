from __future__ import annotations

import hashlib
import http.client
import json
import tempfile
import threading
import unittest
from pathlib import Path

from app.model import load_json_object, save_json_cas
from app.server import create_server
from tests.test_question_bank import make_project

PREFIX = '/api/integrations/homework/local/v1'


class HomeworkLocalTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.workspace = self.root / 'workspace'
        allowed = self.root / 'allowed'
        allowed.mkdir()
        make_project(self.workspace, 'project-one', [('item-1', 2, 'original')])
        self.server = create_server(workspace_root=self.workspace, import_roots=[allowed], port=0, token='operator-test-token')
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)
        self.tmp.cleanup()

    def request(self, path, method='GET', headers=None):
        conn = http.client.HTTPConnection('127.0.0.1', self.server.server_address[1], timeout=5)
        try:
            conn.request(method, path, headers={'X-Astra-Local-Client': 'homework', **(headers or {})})
            response = conn.getresponse()
            raw = response.read()
            return response.status, dict(response.getheaders()), raw
        finally:
            conn.close()

    def test_handoff_has_completion_not_review_gate(self):
        from app.model import atomic_write_json
        path = self.workspace / 'project-one/items/item-1/question.json'
        package = load_json_object(path)
        package['content']['answer'] = {'kind': 'content', 'blocks': [{'type': 'paragraph', 'inlines': [{'type': 'text', 'text': 'actual answer'}]}]}
        package['workflow']['status'] = 'needs_review'
        atomic_write_json(path, package)
        _, _, raw = self.request(PREFIX + '/items')
        row = json.loads(raw)['items'][0]
        self.assertEqual('complete', row['status']['workflow'])
        self.assertNotIn('review_issue_count', row['status'])
        _, _, raw = self.request(PREFIX + '/projects/project-one/items/item-1?revision=2')
        detail = json.loads(raw)
        self.assertEqual('complete', detail['completion_status'])
        self.assertTrue(detail['usable_for_homework'])
        self.assertNotIn('approval_valid', detail)
        self.assertEqual(package, detail['package'])

    def test_changed_poll_rereads_only_changed_packages(self):
        from unittest.mock import patch
        make_project(self.workspace, 'project-many', [(f'item-{n}', 1, f'question {n}') for n in range(30)])
        status, _, _ = self.request(PREFIX + '/items')
        self.assertEqual(200, status)
        path = self.workspace / 'project-many/items/item-4/question.json'
        package = load_json_object(path)
        package['content']['question']['blocks'][0]['inlines'][0]['text'] = 'newly saved'
        save_json_cas(path, package, 1)
        calls = []
        original = Path.read_text
        def measured(path, *args, **kwargs):
            if path.name == 'question.json':
                calls.append(str(path))
            return original(path, *args, **kwargs)
        with patch.object(Path, 'read_text', measured):
            status, _, raw = self.request(PREFIX + '/items')
        self.assertEqual(200, status, raw)
        item = next(i for i in json.loads(raw)['items'] if i['project_id'] == 'project-many' and i['item_id'] == 'item-4')
        self.assertEqual(2, item['revision'])
        self.assertEqual('newly saved', item['preview']['text'])
        self.assertLessEqual(len(calls), 2)

    def test_same_revision_canonical_changes_invalidate_change_token(self):
        from app.model import atomic_write_json
        path = self.workspace / 'project-one/items/item-1/question.json'
        _, _, raw = self.request(PREFIX + '/items')
        previous = json.loads(raw)
        for section in ('answer', 'explanation', 'review', 'resources'):
            package = load_json_object(path)
            if section == 'answer':
                package['content']['answer']['text'] = 'changed answer'
            elif section == 'explanation':
                package['content']['explanation'] = {'blocks': [{'type': 'paragraph', 'inlines': [{'type': 'text', 'text': 'changed explanation'}]}]}
            elif section == 'review':
                package['review']['answer'] = 'approved'
            else:
                package['resources']['assets'] = [{'asset_id': 'new', 'sha256': '1' * 64, 'state': 'ready'}]
            atomic_write_json(path, package)
            _, _, raw = self.request(PREFIX + '/items')
            current = json.loads(raw)
            self.assertEqual(2, current['items'][0]['revision'])
            self.assertNotEqual(previous['items'][0]['change_token'], current['items'][0]['change_token'], section)
            self.assertNotEqual(previous['generation'], current['generation'], section)
            previous = current

    def test_pending_purge_is_unavailable_not_active_or_confirmed_deletion(self):
        from app.model import atomic_write_json
        app = self.server.app
        app.trash.journals.mkdir(parents=True, exist_ok=True)
        journal = app.trash.journals / 'purge-test.json'
        atomic_write_json(journal, {'stage': 'removing', 'preview': {'kind': 'item', 'project_id': 'project-one', 'item_id': 'item-1'}})
        for route in (PREFIX + '/items', PREFIX + '/projects/project-one/items/item-1?revision=2'):
            status, _, raw = self.request(route)
            self.assertEqual(503, status, raw)
            self.assertEqual('source_unavailable', json.loads(raw)['error']['code'])
        atomic_write_json(journal, {'stage': 'done', 'preview': {'kind': 'item', 'project_id': 'project-one', 'item_id': 'item-1'}})
        status, _, raw = self.request(PREFIX + '/items')
        self.assertEqual(200, status, raw)
        self.assertTrue(json.loads(raw)['complete'])

    def test_poll_reads_one_trash_snapshot_not_one_per_item(self):
        make_project(self.workspace, 'project-many', [(f'item-{n}', 1, f'question {n}') for n in range(30)])
        calls = []
        original = self.server.app.trash._state
        def measured():
            calls.append(1)
            return original()
        self.server.app.trash._state = measured
        status, _, raw = self.request(PREFIX + '/items')
        self.assertEqual(200, status, raw)
        self.assertEqual(31, len(json.loads(raw)['items']))
        self.assertLessEqual(len(calls), 2)

    def test_canonical_dotted_identifiers_and_missing_items(self):
        make_project(self.workspace, 'project-dotted.one', [('item.2', 1, 'dotted')])
        status, _, raw = self.request(PREFIX + '/projects/project-dotted.one/items/item.2?revision=1')
        self.assertEqual(200, status, raw)
        status, _, _ = self.request(PREFIX + '/projects/project-one/items/missing?revision=1')
        self.assertEqual(404, status)

    def test_local_header_never_authorizes_operator_or_browser_writes(self):
        for headers in ({'X-Astra-Local-Client': ''}, {'Origin': f'http://127.0.0.1:{self.server.server_address[1]}'}, {'Sec-Fetch-Site': 'same-origin'}):
            status, _, _ = self.request(PREFIX + '/items', headers=headers)
            self.assertEqual(403, status)
        for method in ('POST', 'PATCH', 'DELETE', 'OPTIONS'):
            status, _, _ = self.request(PREFIX + '/items', method=method)
            self.assertEqual(403, status)
        status, _, _ = self.request('/api/bank/items')
        self.assertEqual(401, status)
        status, _, _ = self.request('/api/health', headers={'X-Session-Token': 'operator-test-token'})
        self.assertEqual(200, status)

    def test_unreadable_source_is_not_a_confirmed_deletion(self):
        path = self.workspace / 'project-one/items/item-1/question.json'
        path.write_bytes(b'{broken')
        status, _, raw = self.request(PREFIX + '/items')
        self.assertEqual(200, status, raw)
        result = json.loads(raw)
        self.assertFalse(result['complete'])
        self.assertEqual([{'project_id': 'project-one', 'item_id': 'item-1'}], result['unavailable'])

    def test_trash_and_restore_change_membership_at_same_revision(self):
        _, _, raw = self.request(PREFIX + '/items')
        first = json.loads(raw)
        trash = self.server.app.trash
        preview = trash.preview('item', 'project-one', 'item-1')
        trash.move_restore('move', {'kind': 'item', 'project_id': 'project-one', 'item_id': 'item-1', 'expected_trash_revision': 0, 'target_fingerprint': preview['target_fingerprint']})
        _, _, raw = self.request(PREFIX + '/items')
        removed = json.loads(raw)
        self.assertTrue(removed['complete'])
        self.assertEqual([], removed['items'])
        self.assertNotEqual(first['generation'], removed['generation'])
        status, _, _ = self.request(PREFIX + '/projects/project-one/items/item-1?revision=2')
        self.assertEqual(404, status)
        trash.move_restore('restore', {'kind': 'item', 'project_id': 'project-one', 'item_id': 'item-1', 'expected_trash_revision': 1})
        _, _, raw = self.request(PREFIX + '/items')
        restored = json.loads(raw)
        self.assertEqual(2, restored['items'][0]['revision'])
        self.assertNotEqual(removed['generation'], restored['generation'])

    def test_assets_are_existing_revision_and_sha_bound_bytes(self):
        import struct
        import zlib
        def chunk(kind, data):
            return struct.pack('>I', len(data)) + kind + data + struct.pack('>I', zlib.crc32(kind + data) & 0xffffffff)
        png = b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', 1, 1, 8, 2, 0, 0, 0)) + chunk(b'IDAT', zlib.compress(b'\x00\xff\xff\xff')) + chunk(b'IEND', b'')
        sha = hashlib.sha256(png).hexdigest()
        path = self.workspace / 'project-one/items/item-1/question.json'
        asset_path = path.parent / 'assets/source.png'
        asset_path.parent.mkdir()
        asset_path.write_bytes(png)
        package = load_json_object(path)
        package['resources']['assets'] = [{'asset_id': 'source', 'path': 'assets/source.png', 'state': 'ready', 'section': 'question', 'role': 'source_crop', 'sha256': sha, 'media_type': 'image/png'}]
        save_json_cas(path, package, 2)
        route = PREFIX + '/projects/project-one/items/item-1/assets/source?revision=3&sha256=' + sha
        before = path.read_bytes()
        status, headers, raw = self.request(route)
        self.assertEqual(200, status, raw)
        self.assertEqual(png, raw)
        self.assertEqual('image/png', headers['Content-Type'])
        self.assertEqual('3', headers['X-Astra-Revision'])
        self.assertEqual(sha, headers['X-Astra-Asset-SHA256'])
        self.assertEqual(before, path.read_bytes())
        status, _, _ = self.request(route.replace('revision=3', 'revision=2'))
        self.assertEqual(409, status)
        status, _, _ = self.request(route.replace(sha, '0' * 64))
        self.assertEqual(409, status)
        asset_path.write_bytes(b'corrupt')
        status, _, raw = self.request(route)
        self.assertEqual(409, status, raw)
        status, _, _ = self.request(PREFIX + '/projects/project-one/items/item-1/assets/solution-page-1?revision=3&sha256=' + sha)
        self.assertEqual(404, status)
        self.assertFalse((path.parent / 'jobs').exists())

    def test_detail_binds_revision_and_keeps_canonical_answer(self):
        status, _, raw = self.request(PREFIX + '/projects/project-one/items/item-1?revision=2')
        self.assertEqual(200, status, raw)
        detail = json.loads(raw)
        self.assertEqual(2, detail['revision'])
        self.assertEqual({'kind': 'text', 'text': '정답'}, detail['package']['content']['answer'])
        self.assertNotIn('approval_valid', detail)
        self.assertNotIn('candidates', detail)
        self.assertNotIn('jobs', detail)
        for query in ('', '?revision=02', '?revision=2&revision=2', '?revision=2&other=1'):
            status, _, _ = self.request(PREFIX + '/projects/project-one/items/item-1' + query)
            self.assertEqual(400, status, query)
        status, _, raw = self.request(PREFIX + '/projects/project-one/items/item-1?revision=1')
        self.assertEqual(409, status, raw)
        self.assertEqual('revision_conflict', json.loads(raw)['error']['code'])

    def test_list_tracks_cas_save_and_new_item_without_operator_secret(self):
        status, headers, raw = self.request(PREFIX + '/items')
        self.assertEqual(200, status, raw)
        first = json.loads(raw)
        self.assertTrue(first['complete'])
        self.assertEqual(1, len(first['items']))
        self.assertEqual(2, first['items'][0]['revision'])
        self.assertEqual(hashlib.sha256(raw).hexdigest(), headers['X-Astra-DTO-SHA256'])
        path = self.workspace / 'project-one/items/item-1/question.json'
        package = load_json_object(path)
        package['content']['question']['blocks'][0]['inlines'][0]['text'] = 'saved change'
        save_json_cas(path, package, 2)
        make_project(self.workspace, 'project-two', [('item-2', 1, 'new item')])
        status, _, raw = self.request(PREFIX + '/items')
        self.assertEqual(200, status, raw)
        updated = json.loads(raw)
        self.assertEqual(2, len(updated['items']))
        item = next(i for i in updated['items'] if i['item_id'] == 'item-1')
        self.assertEqual(3, item['revision'])
        self.assertEqual('saved change', item['preview']['text'])
        self.assertNotEqual(first['items'][0]['change_token'], item['change_token'])
        self.assertNotEqual(first['generation'], updated['generation'])
        self.assertEqual(first['source_id'], updated['source_id'])
        self.assertNotIn(b'operator-test-token', raw)


if __name__ == '__main__':
    unittest.main()
