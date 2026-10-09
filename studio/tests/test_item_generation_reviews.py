"""Blind, versioned review ledger; synthetic workspaces and runners only."""
import copy
import json
from unittest import mock
from app import item_generation as ig, item_generation_pipeline as p, item_generation_identity as identity
from tests.test_item_generation import LabFixture, draft, source_blocks


class ReviewTests(LabFixture):
    def job(self, author=p.CODEX_MODEL, *, figure=False):
        j = p.import_bot_drafts(self.workspace, self.request(variants=0, new=1), [draft()], bot_session_id=None)
        def seed(j):
            j['status'] = 'ready'
            d = j['drafts'][0]
            d.update(generator_model=author, contributors=[author], actual_model=author, model_confirmed=True,
                     verification={'status': 'verified', 'model': p.CODEX_MODEL, 'actual_model': p.CODEX_MODEL})
            if figure:
                target = ig.job_dir(self.workspace, j['job_id']) / 'figures' / 'answer-3-solution.png'
                target.parent.mkdir()
                target.write_bytes(self.figure_png.read_bytes())
                d['figure'] = {'method': 'code', 'spec': {}, 'alt_text': '그래프',
                    'file': 'figures/answer-3-solution.png', 'sha256': ig.sha256_file(target)}
        return ig.update_job(self.workspace, j['job_id'], seed)

    def review(self, job, model='claude', answer='③', during=None):
        from app import item_generation_reviews as reviews
        j = reviews.enqueue(self.workspace, job['job_id'], [1], model, job['revision'])
        def runner(prompt, **kwargs):
            for forbidden in ('구리는 전자를 잃어 산화되고', '전자 이동으로 산화·환원을 판단한다',
                              'answer-3-solution', 'job.json', 'question.json', 'verifier_answer', 'design_intent'):
                self.assertNotIn(forbidden, prompt)
            folder = kwargs.get('workdir') or kwargs['add_dirs'][0]
            self.assertTrue(all(f.name.startswith('image-') for f in folder.iterdir()))
            if job['drafts'][0].get('figure'):
                images = kwargs.get('images') or list(folder.iterdir())
                self.assertEqual(images[0].read_bytes(), self.figure_png.read_bytes())
            if during:
                during()
            return json.dumps({'results': [{'no': 1, 'answer': answer, 'reasoning': '독립 풀이', 'issue': None}]}), {
                'actual_model': p.CLAUDE_MODEL if model == 'claude' else p.CODEX_MODEL, 'usage': {'input_tokens': 1}}
        return p.run_job(self.workspace, j['job_id'], p.Runners(runner, runner))

    def test_selfcheck_unknown_mixed_are_not_independent(self):
        self.assertEqual(identity.independence([p.CODEX_MODEL], p.CODEX_MODEL), 'self_check')
        self.assertEqual(identity.independence([p.CLAUDE_MODEL], p.CODEX_MODEL), 'cross_check')
        self.assertEqual(identity.independence(['item-analyst (hermes bot)'], p.CODEX_MODEL), 'unknown')
        self.assertEqual(identity.independence([p.CLAUDE_MODEL, p.CODEX_MODEL], p.CODEX_MODEL), 'mixed')
        from app import item_generation_reviews as reviews
        j = self.review(self.job(), model='codex')
        self.assertEqual(reviews.enrich_job(self.workspace, j)['drafts'][0]['cross_review']['status'], 'self_check')

    def test_blind_images_append_only_history_agreement_conflict_reopen(self):
        from app import item_generation_reviews as reviews
        j = self.review(self.job(figure=True))
        self.assertEqual(j['status'], 'ready')
        history = reviews.records(self.workspace, j['job_id'])
        self.assertEqual(history[0]['status'], 'agreement')
        self.assertEqual(history[0]['review_kind'], 'cross_check')
        for _ in range(6):
            j = self.review(ig.load_job(self.workspace, j['job_id']), answer='①')
        reopened = reviews.enrich_job(self.workspace, ig.load_job(self.workspace, j['job_id']))
        self.assertEqual(len(reopened['drafts'][0]['review_history']), 7)
        self.assertEqual(reopened['drafts'][0]['cross_review']['status'], 'conflict')
        self.assertEqual(reviews.records(self.workspace, j['job_id'])[0], history[0])

    def test_confirmed_current_typed_content_preserves_canonical_bytes(self):
        from app import item_generation_reviews as reviews
        j = self.job()
        result = ig.confirm_drafts(self.workspace, j['job_id'], [1])
        item_id = result['results'][0]['item_id']
        path = self.workspace / result['project_id'] / 'items' / item_id / 'question.json'
        package = json.loads(path.read_text('utf-8'))
        choices = next(b for b in package['content']['question']['blocks'] if b['type'] == 'choices')
        package['content']['question']['blocks'] = source_blocks('현재 수정된 본문 $v=3$') + [choices]
        package['content']['answer']['choice_ids'] = [choices['items'][0]['choice_id']]
        package['workflow']['revision'] += 1
        ig.atomic_write_json(path, package)
        before = path.read_bytes()
        j = ig.load_job(self.workspace, j['job_id'])
        snap = reviews.snapshot(self.workspace, j, j['drafts'][0])
        self.assertIn('현재 수정된 본문', snap['problem'])
        self.assertEqual(snap['answer'], '①')
        self.review(j, answer='①')
        self.assertEqual(path.read_bytes(), before)

    def test_confirmed_short_answer_uses_typed_value_without_rendering_role_prefix(self):
        from app import item_generation_reviews as r
        j = self.job()
        j = ig.update_job(self.workspace, j['job_id'], lambda j: j['drafts'][0].update(
            choices=None, bogi=None, table=None, format='단답형', answer='3'))
        ig.confirm_drafts(self.workspace, j['job_id'], [1])
        j = ig.load_job(self.workspace, j['job_id'])
        snap = r.snapshot(self.workspace, j, j['drafts'][0])
        self.assertEqual(snap['answer'], '3')
        done = self.review(j, answer='3')
        self.assertEqual(r.records(self.workspace, done['job_id'])[-1]['status'], 'agreement')

    def test_during_call_edit_is_historical_stale(self):
        from app import item_generation_reviews as reviews
        j = self.job(figure=True)
        done = self.review(j, during=lambda: ig.update_job(self.workspace, j['job_id'],
            lambda j: j['drafts'][0].update(stem='동시 수정')))
        self.assertTrue(reviews.records(self.workspace, j['job_id'])[-1]['stale'])
        self.assertEqual(reviews.enrich_job(self.workspace, done)['drafts'][0]['cross_review']['status'], 'stale')

    def test_enqueue_validation_quota_resume_and_queue_separation(self):
        from app import item_generation_reviews as reviews
        j = self.job()
        with self.assertRaises(ig.ItemGenerationConflict):
            reviews.enqueue(self.workspace, j['job_id'], [1], 'claude', j['revision'] - 1)
        for numbers, model in (([1, 1], 'claude'), ([99], 'claude'), ([1], 'other'), ([], 'codex'), ([True], 'claude')):
            with self.assertRaises(ig.ItemGenerationError):
                reviews.enqueue(self.workspace, j['job_id'], numbers, model, j['revision'])
        queued = reviews.enqueue(self.workspace, j['job_id'], [1], 'claude', j['revision'])
        with self.assertRaises(ig.ItemGenerationConflict):
            reviews.enqueue(self.workspace, j['job_id'], [1], 'claude', queued['revision'])
        def exhausted(*args, **kwargs):
            raise p.ProviderQuotaExhausted('claude')
        codex = mock.Mock(side_effect=AssertionError('must not silently switch reviewer'))
        paused = p.run_job(self.workspace, j['job_id'], p.Runners(exhausted, codex))
        self.assertEqual(paused['status'], 'paused')
        self.assertFalse(codex.called)
        with mock.patch.object(p, 'step_generate', side_effect=AssertionError('review must not generate')):
            p.resume_job(self.workspace, j['job_id'], expected_revision=paused['revision'])
            p.run_job(self.workspace, j['job_id'], p.Runners(exhausted, codex))
        self.assertEqual(reviews.records(self.workspace, j['job_id']), [])

    def test_asset_rejections_and_missing_answer_never_success(self):
        from app import item_generation_reviews as reviews
        j = self.job(figure=True)
        for path, sha in (('../question.json', 'a'*64), ('missing.png', 'a'*64), ('figures/answer-3-solution.png', 'b'*64)):
            bad = copy.deepcopy(j)
            bad['drafts'][0]['figure'].update(file=path, sha256=sha)
            with self.assertRaises(ig.ItemGenerationError):
                reviews.snapshot(self.workspace, bad, bad['drafts'][0])
        j = self.review(j, answer='')
        self.assertEqual(reviews.records(self.workspace, j['job_id'])[-1]['status'], 'error')

    def test_confirmed_during_call_revision_and_deletion_conflicts_preserve_bytes(self):
        from app import item_generation_reviews as r
        from app.trash import Trash
        j = self.job()
        result = ig.confirm_drafts(self.workspace, j['job_id'], [1])
        item_id = result['results'][0]['item_id']
        path = self.workspace / result['project_id'] / 'items' / item_id / 'question.json'
        j = ig.load_job(self.workspace, j['job_id'])
        after = []
        def mutate():
            package = ig.load_json_object(path)
            package['workflow']['revision'] += 1
            package['content']['question']['blocks'][0]['inlines'][0]['text'] = '호출 도중 수정'
            ig.atomic_write_json(path, package)
            after.append(path.read_bytes())
        self.review(j, during=mutate)
        self.assertTrue(r.records(self.workspace, j['job_id'])[-1]['stale'])
        self.assertEqual(path.read_bytes(), after[0])
        def delete():
            root = self.workspace / '_internal'
            root.mkdir(exist_ok=True)
            ig.atomic_write_json(root / 'trash.json', {'schema_version': 1, 'revision': 1, 'projects': {},
                'items': {result['project_id']: {item_id: ig.utc_now()}}})
        self.review(ig.load_job(self.workspace, j['job_id']), during=delete)
        self.assertTrue(r.records(self.workspace, j['job_id'])[-1]['stale'])
        self.assertEqual(path.read_bytes(), after[0])

    def test_asset_mutation_during_call_and_later_edit_show_stale_without_rewriting_history(self):
        from app import item_generation_reviews as r
        j = self.job(figure=True)
        target = ig.job_dir(self.workspace, j['job_id']) / j['drafts'][0]['figure']['file']
        self.review(j, during=lambda: target.write_bytes(b'changed'))
        history = r.records(self.workspace, j['job_id'])
        self.assertTrue(history[-1]['stale'])
        self.assertEqual(r.enrich_job(self.workspace, ig.load_job(self.workspace, j['job_id']))['drafts'][0]['cross_review']['status'], 'stale')
        self.assertEqual(r.records(self.workspace, j['job_id']), history)

    def test_requested_actual_model_mismatch_is_blocked(self):
        from app import item_generation_reviews as r
        j = self.job()
        r.enqueue(self.workspace, j['job_id'], [1], 'claude', j['revision'])
        def wrong(*args, **kwargs):
            return json.dumps({'results': [{'no': 1, 'answer': '③', 'reasoning': '풀이', 'issue': None}]}), {'actual_model': p.CODEX_MODEL}
        p.run_job(self.workspace, j['job_id'], p.Runners(wrong, wrong))
        record = r.records(self.workspace, j['job_id'])[-1]
        self.assertEqual(record['status'], 'blocked')
        self.assertEqual(record['review_kind'], 'unknown')
        self.assertEqual(record['error_category'], 'actual_model_mismatch')

    def test_review_resume_reuses_completed_entries_and_selected_version(self):
        from app import item_generation_reviews as r
        j = self.job()
        def second(j):
            other = draft(no=2, stem='두 번째 문항', generator_model=p.CODEX_MODEL, contributors=[p.CODEX_MODEL], model_confirmed=True)
            j['drafts'].append(other)
        j = ig.update_job(self.workspace, j['job_id'], second)
        r.enqueue(self.workspace, j['job_id'], [1, 2], 'claude', j['revision'])
        calls = []
        def runner(prompt, **kwargs):
            import re
            no = int(re.search(r'### 문항 (\d+)', prompt).group(1)); calls.append(no)
            if no == 2:
                raise p.ProviderQuotaExhausted('claude')
            return json.dumps({'results': [{'no': no, 'answer': '③', 'reasoning': '풀이', 'issue': None}]}), {'actual_model': p.CLAUDE_MODEL}
        paused = p.run_job(self.workspace, j['job_id'], p.Runners(runner, runner))
        self.assertEqual(paused['status'], 'paused')
        self.assertEqual(len(r.records(self.workspace, j['job_id'])), 1)
        saved = r.records(self.workspace, j['job_id'])[0]
        calls.clear()
        p.resume_job(self.workspace, j['job_id'], expected_revision=paused['revision'])
        p.run_job(self.workspace, j['job_id'], p.Runners(runner, runner))
        self.assertEqual(calls, [2])
        self.assertEqual(r.records(self.workspace, j['job_id'])[0], saved)

    def test_link_paths_rejected_and_stale_initial_receipt_does_not_allow_confirm(self):
        from app import item_generation_reviews as r
        j = self.job(figure=True)
        base = ig.job_dir(self.workspace, j['job_id'])
        # Link APIs can require Windows privilege; mock just the filesystem predicate,
        # preserving containment and hash verification on the real synthetic asset.
        with mock.patch('pathlib.Path.is_symlink', side_effect=lambda: True):
            with self.assertRaises(ig.ItemGenerationError):
                identity.safe_path(base, j['drafts'][0]['figure']['file'])
        receipt = identity.draft_identity(j['drafts'][0], base)
        j = ig.update_job(self.workspace, j['job_id'], lambda j: j['drafts'][0].update(
            verification={'status': 'verified', 'identity': receipt}, stem='수정된 내용'))
        with self.assertRaises(ig.ItemGenerationConflict):
            ig.confirm_drafts(self.workspace, j['job_id'], [1])

    def test_authenticated_http_review_contract_and_canonical_get_projection(self):
        import http.client
        import threading
        from app.server import create_server
        from app import item_generation_reviews as r
        j = self.job()
        cls = p.GenerationWorker
        calls = []
        def reviewer(prompt, **kwargs):
            calls.append(prompt)
            return json.dumps({'results': [{'no': 1, 'answer': '③', 'reasoning': '풀이', 'issue': None}]}), {'actual_model': p.CLAUDE_MODEL}
        with mock.patch('app.item_generation_pipeline.GenerationWorker', side_effect=lambda workspace, **kw: cls(
                workspace, runners=p.Runners(reviewer, reviewer), autostart=False)):
            server = create_server(workspace_root=self.workspace, import_roots=[self.workspace], port=0,
                                   token='synthetic-reviews', web_root=__import__('pathlib').Path(__file__).parents[1] / 'web')
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        def request(method, suffix, body=None, auth=True):
            connection = http.client.HTTPConnection('127.0.0.1', server.server_address[1])
            headers = {'Content-Type': 'application/json'}
            if auth:
                headers['X-Session-Token'] = 'synthetic-reviews'
            try:
                connection.request(method, suffix, json.dumps(body) if body is not None else None, headers)
                response = connection.getresponse()
                return response.status, json.loads(response.read().decode('utf-8'))
            finally:
                connection.close()
        base = f"/api/item-generation/jobs/{j['job_id']}"
        try:
            self.assertEqual(request('GET', base + '/reviews', auth=False)[0], 401)
            saved_bytes = (ig.job_dir(self.workspace, j['job_id']) / 'job.json').read_bytes()
            for _ in range(2):
                status, projection = request('GET', base)
                self.assertEqual(status, 200)
                self.assertIn('item_code', projection['drafts'][0])
            self.assertEqual((ig.job_dir(self.workspace, j['job_id']) / 'job.json').read_bytes(), saved_bytes)
            for body in ({'numbers': [1], 'model': 'claude'}, {'numbers': [99], 'model': 'claude', 'expected_revision': j['revision']},
                         {'numbers': [1, 1], 'model': 'claude', 'expected_revision': j['revision']},
                         {'numbers': [1], 'model': [], 'expected_revision': j['revision']}):
                self.assertEqual(request('POST', base + '/reviews', body)[0], 400)
            self.assertEqual(request('POST', base + '/reviews', {'numbers': [1], 'model': 'claude', 'expected_revision': j['revision']-1})[0], 409)
            status, queued = request('POST', base + '/reviews', {'numbers': [1], 'model': 'claude', 'expected_revision': j['revision']})
            self.assertEqual(status, 202)
            for action, body in (('reviews', {'numbers': [1], 'model': 'claude'}), ('retry', {}),
                ('confirm', {'numbers': [1]}), ('drafts/1/edit', {'fields': {'answer': '①'}})):
                body['expected_revision'] = queued['revision']
                self.assertEqual(request('POST', base + '/' + action, body)[0], 409)
            self.assertFalse(calls)
            self.assertTrue(server.app.item_generation_worker.run_once())
            status, history = request('GET', base + '/reviews')
            self.assertEqual(len(history['records']), 1)
            self.assertEqual(len(calls), 1)
            done = ig.load_job(self.workspace, j['job_id'])
            result = ig.confirm_drafts(self.workspace, j['job_id'], [1], expected_revision=done['revision'])
            project, item = result['project_id'], result['results'][0]['item_id']
            path = self.workspace / project / 'items' / item / 'question.json'
            before = path.read_bytes()
            status, projected = request('GET', f'/api/projects/{project}/items/{item}')
            self.assertEqual(status, 200)
            self.assertIn('generation_review', projected)
            self.assertEqual(path.read_bytes(), before)
        finally:
            server.shutdown(); server.server_close(); thread.join(timeout=2)
