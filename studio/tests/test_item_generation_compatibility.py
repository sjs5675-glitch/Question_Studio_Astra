"""Administrator's real protocol/legacy regressions; all calls are subprocess mocks."""
import copy
import json
import subprocess
from pathlib import Path
from unittest import mock

from app import item_generation as ig, item_generation_pipeline as p
from app import item_generation_reviews as reviews, item_generation_identity as identity
from tests.test_item_generation import LabFixture, draft, FakeModels, source_blocks
from tests import test_item_generation_reviews as review_tests

WEEKLY = "You've hit your weekly limit · resets 2pm (Asia/Seoul)"


def header(model=p.CODEX_MODEL, effort='max'):
    return ('OpenAI Codex v0.153.2\n--------\nworkdir: synthetic\n'
            f'model: {model}\nprovider: openai\nreasoning effort: {effort}\n--------\n'
            'user\nblind question\nassistant\nanswer\ntokens used\n20,514\n')


class ProtocolTests(LabFixture):
    def test_period_quota_exact_envelope_and_negatives(self):
        for message in [WEEKLY, "You've hit your daily limit · resets tomorrow",
                        "You've hit your session usage limit · resets 5pm"]:
            with self.subTest(message=message):
                proc = subprocess.CompletedProcess([], 0, json.dumps({
                    'is_error': True, 'subtype': 'success', 'result': message}), '')
                with mock.patch.object(p.subprocess, 'run', return_value=proc):
                    with self.assertRaises(p.ProviderQuotaExhausted):
                        p.real_claude('x', effort='max', add_dirs=[self.workspace])
        for text in ['weekly rate limit 429', 'daily quota', 'session overload',
                     'rate_limit_error', 'authentication', 'network error']:
            self.assertFalse(p.is_quota_exhaustion(text))

    def invoke(self, stderr, *, rc=0, stdout='', output='{"ok":true}', effort='max'):
        def run(cmd, **kwargs):
            self.assertNotIn('--json', cmd)
            Path(cmd[cmd.index('-o') + 1]).write_text(output, encoding='utf-8')
            return subprocess.CompletedProcess(cmd, rc, stdout, stderr)
        with mock.patch.object(p.subprocess, 'run', side_effect=run) as called:
            result = p.real_codex('blind', effort=effort, images=[], workdir=self.workspace)
        self.assertEqual(called.call_count, 1)
        self.assertEqual(list(self.workspace.glob('_itemgen-*')), [])
        return result

    def test_plain_max_receipt_separates_selected_from_provider_metadata(self):
        text, usage = self.invoke(header())
        self.assertEqual(text, '{"ok":true}')
        self.assertEqual(usage['requested_model'], p.CODEX_MODEL)
        self.assertEqual(usage['cli_selected_model'], p.CODEX_MODEL)
        self.assertEqual(usage['cli_selected_effort'], 'max')
        self.assertEqual(usage['model_evidence'], 'cli_startup_receipt')
        self.assertIsNone(usage['actual_model'])
        self.assertTrue(usage['model_confirmed'])
        self.assertEqual(usage['usage'], {'total_tokens': 20514})
        self.assertEqual(p._authorship(usage)['contributors'], [p.CODEX_MODEL])

    def test_crlf_receipt_and_body_model_claim_never_override_startup(self):
        receipt = header().replace('assistant\nanswer', 'assistant\nmodel: gpt-spoof\nreasoning effort: low')
        _, usage = self.invoke(receipt.replace('\n', '\r\n'))
        self.assertEqual(usage['cli_selected_model'], p.CODEX_MODEL)
        with self.assertRaises(p.ProviderFailure):
            self.invoke('', stdout=json.dumps({'type': 'turn.completed', 'model': p.CODEX_MODEL}))

    def test_exact_weekly_generation_revision_figure_check_share_adapter(self):
        proc = subprocess.CompletedProcess([], 0, json.dumps({'is_error': True, 'subtype': 'success', 'result': WEEKLY}), '')
        for stage in ['generate', 'revise', 'figure_check']:
            with self.subTest(stage=stage):
                workspace = self.workspace / stage
                workspace.mkdir()
                fake = FakeModels(self.figure_png)
                if stage == 'generate':
                    j = ig.create_job(workspace, self.request(variants=0, new=1, figure='none'))
                else:
                    raw = draft(figure={'method': 'code', 'spec': {}, 'alt_text': 'graph'}) if stage == 'figure_check' else draft()
                    j = p.import_bot_drafts(workspace, self.request(variants=0, new=1), [raw], bot_session_id=None)
                    j = ig.update_job(workspace, j['job_id'], lambda j: j.update(status='ready'))
                    if stage == 'revise':
                        p.request_revision(workspace, j['job_id'], 1, '수정')
                def codex(prompt, **kwargs):
                    if '[REQUEST_COUNT=' in prompt or '[현재 초안]' in prompt:
                        return json.dumps({'items': [draft(stem='새 전자 이동 자료')]}), {'actual_model': p.CODEX_MODEL}
                    if stage == 'figure_check' and '그림 파일을 Read 도구로 열어' in prompt:
                        self.assertEqual(kwargs['images'][0].read_bytes(), self.figure_png.read_bytes())
                        return json.dumps({'results': [{'no': 1, 'ok': True, 'issues': []}]}), {'actual_model': p.CODEX_MODEL}
                    return fake.codex(prompt, **kwargs)
                with mock.patch.object(p.subprocess, 'run', return_value=proc) as calls:
                    result = p.run_job(workspace, j['job_id'], p.Runners(p.real_claude, codex))
                self.assertEqual(result['status'], 'ready', result.get('error'))
                self.assertEqual(calls.call_count, 1)
                self.assertEqual(result['fallback_transitions'][0]['step'], stage)
                self.assertTrue(result['claude_exhausted'])

    def test_model_mismatch_authoring_and_initial_validation_never_use_pinned_label(self):
        j = ig.create_job(self.workspace, self.request(variants=0, new=1, figure='none'))
        wrong = mock.Mock(return_value=(json.dumps({'items': [draft()]}), {'actual_model': 'claude-other'}))
        failed = p.run_job(self.workspace, j['job_id'], p.Runners(wrong, wrong))
        self.assertEqual(failed['drafts'], [])
        self.assertIn('actual_model_mismatch', failed['error'])
        j = p.import_bot_drafts(self.workspace, self.request(variants=0, new=1), [draft()], bot_session_id=None)
        wrong = mock.Mock(return_value=(json.dumps({'results': [{'no': 1, 'answer': '③', 'reasoning': '풀이'}]}),
                                       {'actual_model': 'gpt-other'}))
        result = p.run_job(self.workspace, j['job_id'], p.Runners(wrong, wrong))
        self.assertEqual(result['drafts'][0]['verification']['status'], 'error')
        self.assertEqual(result['drafts'][0]['verification']['review_kind'], 'unknown')

    def test_failed_cli_receipt_retains_reported_usage_without_repaying(self):
        j = ig.create_job(self.workspace, self.request(variants=0, new=1, figure='none'))
        ig.update_job(self.workspace, j['job_id'], lambda j: j.update(claude_exhausted=True))
        def codex(cmd, **kwargs):
            Path(cmd[cmd.index('-o') + 1]).write_text(json.dumps({'items': [draft()]}), encoding='utf-8')
            return subprocess.CompletedProcess(cmd, 0, '', header('gpt-other', 'high'))
        with mock.patch.object(p.subprocess, 'run', side_effect=codex) as calls:
            result = p.run_job(self.workspace, j['job_id'], p.Runners(codex=p.real_codex))
        self.assertEqual(calls.call_count, 1)
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['drafts'], [])
        self.assertEqual(len(result['usage']), 1)
        self.assertEqual(result['usage'][0]['usage'], {'total_tokens': 20514})
        self.assertEqual(result['usage'][0]['error_category'], 'cli_model_mismatch')

    def test_unknown_mismatched_or_body_spoofed_receipt_fails_closed(self):
        for receipt in ['', header('gpt-other'), header(effort='high'),
                        'user\nquestion\nassistant\n' + header(),
                        header().replace('OpenAI Codex v0.153.2', 'assistant says Codex'),
                        header().replace('model: gpt-6-astra', 'model: gpt-6-astra\nmodel: gpt-other')]:
            with self.subTest(receipt=receipt):
                with self.assertRaises(p.ProviderFailure):
                    self.invoke(receipt)

    def test_nonzero_old_output_quota_and_text_errors_are_rejected(self):
        old = self.workspace / '_last.txt'
        old.write_text('old', encoding='utf-8')
        for rc, stdout, stderr, output, error in [
            (1, '', header(), 'old output', p.ProviderFailure),
            (0, '', header() + 'You have reached your usage limit', 'ok', p.ProviderQuotaExhausted),
            (0, json.dumps({'type': 'turn.failed', 'error': {'message': 'network'}}), header(), 'ok', p.ProviderFailure),
            (0, '', header().replace('user\n', 'ERROR: network failure\nuser\n'), 'ok', p.ProviderFailure),
            (0, '', header(), '', p.ProviderFailure),
        ]:
            with self.subTest(rc=rc, stdout=stdout, stderr=stderr):
                with self.assertRaises(error):
                    self.invoke(stderr, rc=rc, stdout=stdout, output=output)
        self.assertEqual(old.read_text('utf-8'), 'old')

    def test_generation_and_revision_poison_fields_cannot_be_operational_metadata(self):
        poison = dict.fromkeys(('discarded', 'confirmed_item_id', 'confirmed_project_id', 'confirmed_revision', 'history',
            'reviews', 'review_history', 'replacement_of', 'replaced_by', 'contributors', 'identity',
            'verification', 'item_code', 'work_action', 'review_request', 'revise_request', 'queue',
            'content_revision', 'model_confirmed', 'actual_model', 'code', 'generation_plan',
            'cli_selected_model', 'cli_selected_effort', 'model_evidence'), 'POISON')
        raw = draft(**poison, reference_item_ids=['source'], figure={
            'method': 'code', 'spec': {'description': 'graph'}, 'alt_text': 'graph',
            'file': 'escape.png', 'sha256': 'fake', 'check': {'status': 'ok'}})
        cleaned = p._validate_items(json.dumps({'items': [raw]}), 'new', 1)[0]
        self.assertFalse(set(cleaned) & set(poison))
        self.assertEqual(set(cleaned['figure']), {'method', 'spec', 'alt_text'})
        self.assertEqual(cleaned['reference_item_ids'], ['source'])
        fake = FakeModels(self.figure_png)
        j = ig.create_job(self.workspace, self.request(variants=0, new=1, figure='none'))
        runner = mock.Mock(return_value=(json.dumps({'items': [draft(**poison)]}), {'actual_model': p.CLAUDE_MODEL}))
        ready = p.run_job(self.workspace, j['job_id'], p.Runners(runner, fake.codex))
        d = ready['drafts'][0]
        self.assertNotIn('confirmed_item_id', d)
        self.assertNotIn('discarded', d)
        self.assertEqual(d['contributors'], [p.CLAUDE_MODEL])
        p.request_revision(self.workspace, j['job_id'], 1, 'revise')
        revised = p.run_job(self.workspace, j['job_id'], p.Runners(runner, fake.codex))['drafts'][0]
        self.assertEqual(len(revised['history']), 1)
        self.assertNotIn('reviews', revised)
        self.assertNotIn('confirmed_item_id', revised)
        self.assertNotEqual(revised['item_code'], 'POISON')


class LegacyTests(LabFixture):
    job = review_tests.ReviewTests.job
    review = review_tests.ReviewTests.review
    def legacy(self):
        j = self.job(figure=True)
        def old(j):
            d = j['drafts'][0]
            d['figure'].pop('sha256')
            d['figure']['check'] = {'status': 'ok', 'model': p.CLAUDE_MODEL}
        return ig.update_job(self.workspace, j['job_id'], old)

    def test_legacy_snapshot_freezes_new_hash_without_original_mutation(self):
        j = self.legacy()
        base = ig.job_dir(self.workspace, j['job_id'])
        original = (base / 'job.json').read_bytes()
        image = base / j['drafts'][0]['figure']['file']
        before = image.read_bytes()
        snap = reviews.snapshot(self.workspace, j, j['drafts'][0])
        self.assertEqual(snap['asset_refs'][0]['observation'], 'legacy-first-observation')
        self.assertEqual(snap['identity']['asset_shas'], [ig.sha256_file(image)])
        view = reviews.enrich_job(self.workspace, j)
        self.assertEqual(view['drafts'][0]['cross_review']['status'], 'not_run')
        self.assertEqual((base / 'job.json').read_bytes(), original)
        self.assertEqual(image.read_bytes(), before)
        done = self.review(j)
        self.assertEqual(done['drafts'], j['drafts'])
        self.assertEqual(image.read_bytes(), before)
        self.assertEqual(reviews.records(self.workspace, j['job_id'])[-1]['status'], 'agreement')

    def test_legacy_asset_mutation_invalidates_new_review(self):
        j = self.legacy()
        target = ig.job_dir(self.workspace, j['job_id']) / j['drafts'][0]['figure']['file']
        self.review(j, during=lambda: target.write_bytes(b'changed'))
        self.assertTrue(reviews.records(self.workspace, j['job_id'])[-1]['stale'])

    def test_expected_hash_mismatch_never_becomes_legacy_and_retry_is_atomic(self):
        j = self.legacy()
        base = ig.job_dir(self.workspace, j['job_id'])
        for expected in ['wrong', '']:
            j = ig.update_job(self.workspace, j['job_id'], lambda j: j['drafts'][0]['figure'].update(sha256=expected))
            before = (base / 'job.json').read_bytes()
            with self.assertRaises(ig.ItemGenerationError):
                reviews.snapshot(self.workspace, j, j['drafts'][0])
            with self.assertRaises(ig.ItemGenerationError):
                p.resume_job(self.workspace, j['job_id'], expected_revision=j['revision'])
            self.assertEqual((base / 'job.json').read_bytes(), before)

    def test_user_resume_reverifies_old_figures_without_recreation(self):
        j = self.legacy()
        target = ig.job_dir(self.workspace, j['job_id']) / j['drafts'][0]['figure']['file']
        before = target.read_bytes()
        resumed = p.resume_job(self.workspace, j['job_id'], expected_revision=j['revision'])
        self.assertEqual(resumed['drafts'][0]['figure']['sha256'], ig.sha256_file(target))
        self.assertEqual(resumed['drafts'][0]['figure']['hash_observation']['kind'], 'legacy-first-observation')
        fake = FakeModels(self.figure_png)
        with mock.patch.object(p, '_figure_attempt', side_effect=AssertionError('must reuse image')):
            done = p.run_job(self.workspace, j['job_id'], p.Runners(fake.claude, fake.codex))
        self.assertEqual(done['status'], 'ready')
        self.assertTrue(done['drafts'][0]['verification']['identity'])
        self.assertTrue(done['drafts'][0]['figure']['check']['identity'])
        self.assertEqual(target.read_bytes(), before)

    def test_exact_weekly_requested_review_pauses_without_fallback_or_auto_retry(self):
        j = self.job()
        reviews.enqueue(self.workspace, j['job_id'], [1], 'claude', j['revision'])
        proc = subprocess.CompletedProcess([], 0, json.dumps({'is_error': True, 'subtype': 'success', 'result': WEEKLY}), '')
        codex = mock.Mock(side_effect=AssertionError('no cross-review fallback'))
        with mock.patch.object(p.subprocess, 'run', return_value=proc) as calls:
            paused = p.run_job(self.workspace, j['job_id'], p.Runners(p.real_claude, codex))
            self.assertEqual(paused['status'], 'paused')
            self.assertEqual(paused['review_request']['model'], 'claude')
            self.assertFalse(p.GenerationWorker(self.workspace, runners=p.Runners(p.real_claude, codex), autostart=False).run_once())
            self.assertEqual(calls.call_count, 1)
        self.assertFalse(codex.called)
        self.assertEqual(reviews.records(self.workspace, j['job_id']), [])

    def test_cli_selected_identity_reaches_author_validation_review_and_canonical(self):
        j = self.job(author=p.CLAUDE_MODEL)
        p.resume_job(self.workspace, j['job_id'], expected_revision=j['revision'])
        def codex(cmd, **kwargs):
            Path(cmd[cmd.index('-o') + 1]).write_text(json.dumps({'results': [
                {'no': 1, 'answer': '③', 'reasoning': '풀이', 'issue': None}]}), encoding='utf-8')
            return subprocess.CompletedProcess(cmd, 0, '', header(effort='high'))
        with mock.patch.object(p.subprocess, 'run', side_effect=codex):
            j = p.run_job(self.workspace, j['job_id'], p.Runners(codex=p.real_codex))
            self.assertEqual(j['drafts'][0]['verification']['review_kind'], 'cross_check')
            self.assertEqual(reviews.enrich_job(self.workspace, j)['drafts'][0]['cross_review']['status'], 'cross_check')
            reviews.enqueue(self.workspace, j['job_id'], [1], 'codex', j['revision'])
            j = p.run_job(self.workspace, j['job_id'], p.Runners(codex=p.real_codex))
        record = reviews.records(self.workspace, j['job_id'])[-1]
        self.assertEqual(record['status'], 'agreement')
        self.assertEqual(record['review_kind'], 'cross_check')
        self.assertIsNone(record['actual_model'])
        self.assertEqual(record['cli_selected_model'], p.CODEX_MODEL)
        usage = record['usage']
        j = ig.update_job(self.workspace, j['job_id'], lambda j: j['drafts'][0].update(p._authorship(usage)))
        result = ig.confirm_drafts(self.workspace, j['job_id'], [1])
        package = ig.load_json_object(self.workspace / result['project_id'] / 'items' / result['results'][0]['item_id'] / 'question.json')
        generator = package['provenance']['generation']['generator']
        self.assertIsNone(generator['actual_model'])
        self.assertEqual(generator['cli_selected_model'], p.CODEX_MODEL)
        self.assertEqual(generator['model_evidence'], 'cli_startup_receipt')

    def test_deferred_unknown_mismatched_startup_receipts_are_blocked(self):
        for receipt, category in [('', 'cli_receipt_unknown'), (header('gpt-other', 'high'), 'cli_model_mismatch'),
                                  (header(effort='max'), 'cli_effort_mismatch')]:
            with self.subTest(category=category):
                j = self.job(author=p.CLAUDE_MODEL)
                reviews.enqueue(self.workspace, j['job_id'], [1], 'codex', j['revision'])
                def codex(cmd, **kwargs):
                    Path(cmd[cmd.index('-o') + 1]).write_text(json.dumps({'results': [
                        {'no': 1, 'answer': '③', 'reasoning': '풀이', 'issue': None}]}), encoding='utf-8')
                    return subprocess.CompletedProcess(cmd, 0, '', receipt)
                with mock.patch.object(p.subprocess, 'run', side_effect=codex) as calls:
                    result = p.run_job(self.workspace, j['job_id'], p.Runners(codex=p.real_codex))
                self.assertEqual(calls.call_count, 1)
                record = reviews.records(self.workspace, j['job_id'])[-1]
                self.assertEqual(record['status'], 'blocked')
                self.assertEqual(record['review_kind'], 'unknown')
                self.assertEqual(record['error_category'], category)
                self.assertIsNone(record['actual_model'])
                self.assertEqual(reviews.enrich_job(self.workspace, result)['drafts'][0]['cross_review']['status'], 'blocked')

    def test_confirmed_canonical_edit_and_asset_only_edit_keep_initial_stale(self):
        j = self.job(author=p.CLAUDE_MODEL, figure=True)
        base = ig.job_dir(self.workspace, j['job_id'])
        j = ig.update_job(self.workspace, j['job_id'], lambda j: j['drafts'][0]['verification'].update(
            identity=identity.draft_identity(j['drafts'][0], base), model_confirmed=True))
        ig.confirm_drafts(self.workspace, j['job_id'], [1])
        j = ig.load_job(self.workspace, j['job_id'])
        self.assertEqual(reviews.enrich_job(self.workspace, j)['drafts'][0]['cross_review']['status'], 'cross_check')
        path = reviews.canonical_path(self.workspace, j['drafts'][0])
        before = path.read_bytes()
        package = ig.load_json_object(path)
        asset = package['resources']['assets'][0]
        target = path.parent / asset['path']
        target.write_bytes(b'changed')
        self.assertEqual(reviews.enrich_job(self.workspace, j)['drafts'][0]['cross_review']['status'], 'stale')
        self.assertEqual(path.read_bytes(), before)
        target.write_bytes(self.figure_png.read_bytes())
        package['content']['question']['blocks'][0]['inlines'][0]['text'] = '편집된 canonical'
        package['content']['answer']['choice_ids'] = [next(b for b in package['content']['question']['blocks'] if b['type'] == 'choices')['items'][0]['choice_id']]
        package['workflow']['revision'] += 1
        ig.atomic_write_json(path, package)
        changed = path.read_bytes()
        self.assertEqual(reviews.enrich_job(self.workspace, j)['drafts'][0]['cross_review']['status'], 'stale')
        self.assertEqual(path.read_bytes(), changed)


    def test_typed_canonical_equations_nested_figures_tables_choices_and_selection(self):
        j = self.job(figure=True)
        result = ig.confirm_drafts(self.workspace, j['job_id'], [1])
        j = ig.load_job(self.workspace, j['job_id'])
        path = reviews.canonical_path(self.workspace, j['drafts'][0])
        package = ig.load_json_object(path)
        blocks = package['content']['question']['blocks']
        figure = next(b for b in blocks if b['type'] == 'figure')
        asset = package['resources']['assets'][0]
        other = copy.deepcopy(asset)
        other.update(asset_id='selected-regenerated', path='assets/selected.png', role='figure_regenerated')
        (path.parent / other['path']).write_bytes(self.figure_png.read_bytes())
        package['resources']['assets'].append(other)
        figure.update(asset_id=other['asset_id'], original_asset_id=asset['asset_id'])
        for container in ['table', 'bogi', 'choices']:
            block = next(b for b in blocks if b['type'] == container)
            children = block['rows'][0]['cells'][0]['blocks'] if container == 'table' else block['items'][0]['blocks']
            children.append(copy.deepcopy(figure))
        equation = source_blocks('equation')[0]
        equation['inlines'] = [{'type': 'equation', 'source_format': 'latex', 'source_text': None, 'latex': 'E=mc^2'},
            {'type': 'equation', 'source_format': 'plain', 'source_text': 'x=7', 'plain_text': 'x=7'}]
        blocks.append(equation)
        ig.atomic_write_json(path, package)
        before = path.read_bytes()
        snap = reviews.snapshot(self.workspace, j, j['drafts'][0])
        self.assertIn('$E=mc^2$', snap['problem'])
        self.assertIn('x=7', snap['problem'])
        for label in ['[표]', '[보기]', '[선지]', '[그림]']:
            self.assertIn(label, snap['problem'])
        self.assertEqual([r['asset_id'] for r in snap['asset_refs']], ['selected-regenerated'] * 4)
        self.assertEqual(len(snap['images']), 4)
        self.assertEqual(path.read_bytes(), before)
        valid_blocks = copy.deepcopy(blocks)
        for invalid in [dict(figure, asset_id=None), {'type': 'unrenderable', 'payload': 'visible?'},
                        {'type': 'paragraph', 'inlines': [{'type': 'equation', 'source_format': 'plain',
                                                        'source_text': None, 'plain_text': 'invisible'}]}]:
            package['content']['question']['blocks'] = valid_blocks + [invalid]
            ig.atomic_write_json(path, package)
            with self.assertRaises(ig.ItemGenerationError):
                reviews.snapshot(self.workspace, j, j['drafts'][0])
