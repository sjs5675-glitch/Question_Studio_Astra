"""Subscription adapters and durable quota/resume contracts; never call providers."""
import json
import re
import subprocess
from pathlib import Path
from unittest import mock
import unittest

from app import item_generation as ig
from app import item_generation_pipeline as p
from tests.test_item_generation import LabFixture, draft, FakeModels


class ProviderTests(unittest.TestCase):
    def test_quota_classifier_is_narrow(self):
        positive = ["You've hit your limit · resets 5pm", "You have reached your usage limit",
                    {"error": {"type": "usage_limit_reached", "message": "subscription"}}]
        for value in positive:
            self.assertTrue(p.is_quota_exhaustion(value))
        for value in ["429 rate limit", "overloaded", "capacity_unavailable", "quota", "timeout",
                      "authentication failed", "unsupported model", "policy denied", "invalid JSON",
                      {"error": {"type": "rate_limit_error"}}, "connection reset", "billing quota exceeded"]:
            self.assertFalse(p.is_quota_exhaustion(value), value)

    def test_claude_rc_and_error_envelope_are_classified_before_generic_failure(self):
        for rc in (0, 1):
            proc = subprocess.CompletedProcess([], rc, json.dumps({"is_error": True,
                "result": "You've hit your limit · resets 5pm"}), "")
            with mock.patch.object(p.subprocess, "run", return_value=proc):
                with self.assertRaises(p.ProviderQuotaExhausted):
                    p.real_claude("blind", effort="high", add_dirs=[])
        proc = subprocess.CompletedProcess([], 0, json.dumps({"result": "ok", "model": "claude-opus-5-5",
                "usage": {"input_tokens": 3}}), "")
        with mock.patch.object(p.subprocess, "run", return_value=proc):
            _, usage = p.real_claude("blind", effort="high", add_dirs=[])
        self.assertEqual(usage["actual_model"], p.CLAUDE_MODEL)
        self.assertEqual(usage["usage"]["input_tokens"], 3)
        unknown = subprocess.CompletedProcess([], 0, json.dumps({'result': 'ok'}), '')
        with mock.patch.object(p.subprocess, 'run', return_value=unknown):
            _, usage = p.real_claude('x', effort='high', add_dirs=[])
        self.assertIsNone(usage['actual_model'])
        self.assertFalse(usage['model_confirmed'])


class AdapterWorkspaceTests(LabFixture):
    def test_nonzero_codex_never_accepts_stale_last_text(self):
        (self.workspace / "_last.txt").write_text('{"results": []}', encoding="utf-8")
        proc = subprocess.CompletedProcess([], 1, "", "auth failed token=SECRET")
        with mock.patch.object(p.subprocess, "run", return_value=proc):
            with self.assertRaises(ig.ItemGenerationError) as error:
                p.real_codex("x", effort="high", images=[], workdir=self.workspace)
        self.assertNotIn("SECRET", str(error.exception))
        self.assertEqual((self.workspace / "_last.txt").read_text('utf-8'), '{"results": []}')

    def test_codex_fresh_output_usage_and_quota_envelope(self):
        def success(cmd, **kwargs):
            output = Path(cmd[cmd.index('-o')+1])
            self.assertNotEqual(output.name, '_last.txt')
            output.write_text('fresh', encoding='utf-8')
            return subprocess.CompletedProcess(cmd, 0, 'fresh',
                'OpenAI Codex v0.153.2\n--------\nmodel: gpt-6-astra\nreasoning effort: max\n--------\nuser\nx\nassistant\nfresh\ntokens used\n20,514\n')
        with mock.patch.object(p.subprocess, 'run', side_effect=success):
            text, usage = p.real_codex('x', effort='max', images=[], workdir=self.workspace)
        self.assertEqual(text, 'fresh')
        self.assertIsNone(usage['actual_model'])
        self.assertEqual(usage['cli_selected_model'], p.CODEX_MODEL)
        self.assertEqual(usage['usage'], {'total_tokens': 20514})
        self.assertEqual(list(self.workspace.glob('_itemgen-*')), [])
        proc = subprocess.CompletedProcess([], 1, json.dumps({'type': 'error', 'message': 'You have reached your usage limit'}), '')
        with mock.patch.object(p.subprocess, 'run', return_value=proc):
            with self.assertRaises(p.ProviderQuotaExhausted):
                p.real_codex('x', effort='high', images=[], workdir=self.workspace)

    def test_generic_429_does_not_fallback_or_retry(self):
        claude = mock.Mock(side_effect=p.ProviderFailure('claude', 'rate_limited'))
        codex = mock.Mock()
        j = ig.create_job(self.workspace, self.request(variants=0, new=1))
        failed = p.run_job(self.workspace, j['job_id'], p.Runners(claude, codex))
        self.assertEqual(failed['status'], 'failed')
        self.assertEqual(claude.call_count, 1)
        self.assertFalse(codex.called)
        self.assertNotIn('fallback_transitions', failed)

    def test_blind_subscription_commands_remove_ambient_access_without_auth_changes(self):
        from app.item_generation_identity import image_sandbox
        with image_sandbox(self.workspace / '.scratch', [self.figure_png.read_bytes()]) as (folder, images):
            def claude(cmd, **kwargs):
                self.assertIn('--restricted', cmd)
                self.assertIn('--safe-mode', cmd)
                self.assertIn('dontAsk', cmd)
                self.assertEqual(kwargs['cwd'], str(folder))
                self.assertEqual(cmd[cmd.index('--add-dir') + 1], str(folder))
                self.assertIn('Read(./image-*.png)', cmd)
                self.assertNotIn('--bare', cmd)  # bare requires API keys instead of subscription OAuth
                return subprocess.CompletedProcess(cmd, 0, json.dumps({'result': 'ok'}), '')
            with mock.patch.object(p.subprocess, 'run', side_effect=claude):
                p.real_claude('blind', effort='high', add_dirs=[folder])
            def codex(cmd, **kwargs):
                self.assertIn('--ignore-user-config', cmd)
                self.assertIn('project_doc_max_bytes=0', cmd)
                self.assertIn('shell_tool', cmd)
                self.assertIn('unified_exec', cmd)
                self.assertIn('memories', cmd)
                self.assertEqual(cmd[cmd.index('-i') + 1], str(images[0]))
                Path(cmd[cmd.index('-o')+1]).write_text('ok', encoding='utf-8')
                return subprocess.CompletedProcess(cmd, 0, '',
                    'OpenAI Codex v0.153.2\n--------\nmodel: gpt-6-astra\nreasoning effort: high\n--------\nuser\nblind\nassistant\nok\n')
            with mock.patch.object(p.subprocess, 'run', side_effect=codex):
                p.real_codex('blind', effort='high', images=images, workdir=folder)


class CheckpointTests(LabFixture):
    def models(self, *, both=False, short=False):
        self.serial = getattr(self, 'serial', 0) + 100
        calls = []
        def respond(prompt, **kwargs):
            count = int(re.search(r'\[REQUEST_COUNT=(\d+)\]', prompt).group(1))
            start = self.serial + len(calls) * 10
            return json.dumps({'items': [draft(stem=chr(0xAC00+start+i) * 40,
                bogi=None, choices=None, table=None, format='단답형', answer='3')
                for i in range(1 if short else count)]}, ensure_ascii=False), {'actual_model': p.CODEX_MODEL}
        def claude(prompt, **kwargs):
            calls.append('claude')
            if len(calls) > 1:
                raise p.ProviderQuotaExhausted('claude')
            text, _ = respond(prompt, **kwargs)
            return text, {'actual_model': p.CLAUDE_MODEL}
        def codex(prompt, **kwargs):
            calls.append('codex')
            if both:
                raise p.ProviderQuotaExhausted('codex')
            if '[REQUEST_COUNT=' in prompt:
                return respond(prompt, **kwargs)
            nos = [int(n) for n in re.findall(r'### 문항 (\d+)', prompt)]
            return json.dumps({'results': [{'no': n, 'answer': '3', 'reasoning': '계산', 'issue': None} for n in nos]}), {'actual_model': p.CODEX_MODEL}
        return p.Runners(claude, codex), calls

    def test_second_chunk_quota_fallback_only_unfinished(self):
        job = ig.create_job(self.workspace, self.request(variants=0, new=6, figure='none'))
        runners, calls = self.models()
        result = p.run_job(self.workspace, job['job_id'], runners)
        self.assertEqual(result['status'], 'ready')
        self.assertEqual([d['no'] for d in result['drafts']], list(range(1, 7)))
        self.assertEqual([d['generator_model'] for d in result['drafts']], [p.CLAUDE_MODEL]*5+[p.CODEX_MODEL])
        self.assertEqual(len(result['fallback_transitions']), 1)
        before = result['drafts'][:5]
        count = len(calls)
        p.run_job(self.workspace, job['job_id'], runners)
        self.assertEqual(len(calls), count)
        self.assertEqual(ig.load_job(self.workspace, job['job_id'])['drafts'][:5], before)

    def test_both_exhausted_pause_no_auto_retry_then_exact_resume(self):
        job = ig.create_job(self.workspace, self.request(variants=0, new=6, figure='none'))
        runners, calls = self.models(both=True)
        result = p.run_job(self.workspace, job['job_id'], runners)
        self.assertEqual(result['status'], 'paused')
        before = result['drafts'][:]
        worker = p.GenerationWorker(self.workspace, runners=runners, autostart=False)
        self.assertFalse(worker.run_once())
        self.assertEqual(p.run_job(self.workspace, job['job_id'], runners), result)
        runners, calls = self.models()
        p.resume_job(self.workspace, job['job_id'], expected_revision=result['revision'])
        result = p.run_job(self.workspace, job['job_id'], runners)
        self.assertEqual(len(result['drafts']), 6)
        self.assertEqual([d['item_code'] for d in result['drafts'][:5]], [d['item_code'] for d in before])
        self.assertNotIn('claude', calls)

    def test_short_payload_checkpoint_and_legacy_partial(self):
        job = ig.create_job(self.workspace, self.request(variants=0, new=3, figure='none'))
        runners, _ = self.models(short=True)
        result = p.run_job(self.workspace, job['job_id'], runners)
        self.assertEqual(result['status'], 'partial')
        self.assertEqual(len(result['drafts']), 1)
        self.assertIn('short', result['error'])
        first = result['drafts'][0]
        # Emulate an older quick job with no plan; preserve the saved draft and its number.
        ig.update_job(self.workspace, job['job_id'], lambda j: j.pop('generation_plan'))
        result = ig.load_job(self.workspace, job['job_id'])
        p.resume_job(self.workspace, job['job_id'], expected_revision=result['revision'])
        runners, _ = self.models()
        result = p.run_job(self.workspace, job['job_id'], runners)
        self.assertEqual(len(result['drafts']), 3)
        self.assertEqual(result['drafts'][0]['stem'], first['stem'])
        self.assertEqual(result['drafts'][0]['no'], first['no'])

    def test_variant_lineage_preserved_and_wrong_kind_rejected(self):
        from tests.test_item_generation import SOURCE_ID
        j = ig.create_job(self.workspace, self.request(variants=1, new=0, figure='none'))
        bad = mock.Mock(return_value=(json.dumps({'items': [draft('new')]}), {'actual_model': p.CLAUDE_MODEL}))
        failed = p.run_job(self.workspace, j['job_id'], p.Runners(bad, bad))
        self.assertEqual(failed['status'], 'failed')
        self.assertIn('invalid_item_kind', failed['error'])
        self.assertEqual(failed['drafts'], [])
        self.assertEqual(len(failed['usage']), 1)  # measured successful call retained even invalid payload
        fake = FakeModels(self.figure_png)
        p.resume_job(self.workspace, j['job_id'], expected_revision=failed['revision'])
        result = p.run_job(self.workspace, j['job_id'], p.Runners(fake.claude, fake.codex))
        self.assertEqual(result['drafts'][0]['source_item_id'], SOURCE_ID)
        self.assertEqual(result['generation_plan'][0]['source_ids'], [SOURCE_ID])

    def test_duplicate_replacement_quota_checkpoint_is_bounded_and_preserves_candidates(self):
        j = ig.create_job(self.workspace, self.request(variants=0, new=2, figure='none'))
        calls = []
        def claude(prompt, **kwargs):
            calls.append('claude')
            if len(calls) > 1:
                raise p.ProviderQuotaExhausted('claude')
            return json.dumps({'items': [draft(), draft()]}), {'actual_model': p.CLAUDE_MODEL}
        def exhausted(*args, **kwargs):
            calls.append('codex'); raise p.ProviderQuotaExhausted('codex')
        paused = p.run_job(self.workspace, j['job_id'], p.Runners(claude, exhausted))
        self.assertEqual(paused['status'], 'paused')
        self.assertEqual(len(paused['drafts']), 2)
        self.assertEqual(len(paused['generation_plan']), 2)
        original = paused['drafts'][1]
        def codex(prompt, **kwargs):
            if '[REQUEST_COUNT=' in prompt:
                return json.dumps({'items': [draft(stem='우주 먼지와 별의 진화', table=None, bogi=None, choices=None,
                    format='단답형', answer='3')]}), {'actual_model': p.CODEX_MODEL}
            import re
            nos = [int(n) for n in re.findall(r'### 문항 (\d+)', prompt)]
            return json.dumps({'results': [{'no': n, 'answer': '③' if n == 1 else '3', 'reasoning': '풀이', 'issue': None}
                                         for n in nos]}), {'actual_model': p.CODEX_MODEL}
        p.resume_job(self.workspace, j['job_id'], expected_revision=paused['revision'])
        ready = p.run_job(self.workspace, j['job_id'], p.Runners(claude, codex))
        self.assertEqual(ready['status'], 'ready')
        self.assertEqual(len(ready['drafts']), 3)
        self.assertEqual(ready['drafts'][1]['stem'], original['stem'])
        self.assertEqual(ready['drafts'][1]['item_code'], original['item_code'])
        self.assertEqual(ready['drafts'][2]['replacement_of'], 2)
        self.assertEqual(len(ready['generation_plan']), 2)

    def test_missing_variant_sources_are_explicit_and_bot_never_generates(self):
        j = ig.create_job(self.workspace, self.request(variants=2, new=0))
        never = mock.Mock(side_effect=AssertionError('no provider call'))
        failed = p.run_job(self.workspace, j['job_id'], p.Runners(never, never))
        self.assertEqual(failed['status'], 'failed')
        self.assertIn('source_count_short', failed['error'])
        fake = FakeModels(self.figure_png)
        j = p.import_bot_drafts(self.workspace, self.request(variants=1, new=10), [draft()], bot_session_id='x')
        ready = p.run_job(self.workspace, j['job_id'], p.Runners(never, fake.codex))
        self.assertEqual(ready['status'], 'ready')
        self.assertEqual(len(ready['drafts']), 1)
        self.assertFalse(never.called)


class StageTests(LabFixture):
    def imported(self, **fields):
        j = p.import_bot_drafts(self.workspace, self.request(variants=0, new=1), [draft(**fields)], bot_session_id=None)
        return ig.update_job(self.workspace, j['job_id'], lambda j: j.update(status='ready'))

    def test_revision_quota_retains_request_and_records_mixed_contributors(self):
        job = self.imported(generator_model=p.CLAUDE_MODEL)
        ig.update_job(self.workspace, job['job_id'], lambda j: j['drafts'][0].update(
            generator_model=p.CLAUDE_MODEL, contributors=[p.CLAUDE_MODEL]))
        p.request_revision(self.workspace, job['job_id'], 1, '조건 수정')
        def exhausted(*args, **kwargs):
            raise p.ProviderQuotaExhausted('claude')
        def codex_exhausted(*args, **kwargs):
            raise p.ProviderQuotaExhausted('codex')
        paused = p.run_job(self.workspace, job['job_id'], p.Runners(exhausted, codex_exhausted))
        self.assertEqual(paused['status'], 'paused')
        self.assertEqual(paused['drafts'][0]['revise_request'], '조건 수정')
        def codex(prompt, **kwargs):
            if '[현재 초안]' in prompt:
                return json.dumps({'items': [draft(stem='바뀐 자료')]}), {'actual_model': p.CODEX_MODEL}
            return json.dumps({'results': [{'no': 1, 'answer': '③', 'reasoning': '풀이', 'issue': None}]}), {'actual_model': p.CODEX_MODEL}
        p.resume_job(self.workspace, job['job_id'], expected_revision=paused['revision'])
        ready = p.run_job(self.workspace, job['job_id'], p.Runners(exhausted, codex))
        self.assertEqual(ready['status'], 'ready')
        self.assertEqual(set(ready['drafts'][0]['contributors']), {p.CLAUDE_MODEL, p.CODEX_MODEL})
        self.assertEqual(ready['drafts'][0]['verification']['review_kind'], 'mixed')

    def test_figure_check_fallback_passes_actual_image_and_completed_steps_reused(self):
        figure = {'method': 'code', 'alt_text': '그림', 'spec': {'type': 'graph'}}
        job = self.imported(figure=figure)
        fake = FakeModels(self.figure_png)
        calls = []
        def claude(*args, **kwargs):
            raise p.ProviderQuotaExhausted('claude')
        def codex(prompt, **kwargs):
            calls.append((prompt, kwargs))
            if '그림 파일을 Read 도구로 열어' in prompt:
                self.assertEqual(len(kwargs['images']), 1)
                self.assertEqual(kwargs['images'][0].read_bytes(), self.figure_png.read_bytes())
                return json.dumps({'results': [{'no': 1, 'ok': True, 'issues': []}]}), {'actual_model': p.CODEX_MODEL}
            return fake.codex(prompt, **kwargs)
        ready = p.run_job(self.workspace, job['job_id'], p.Runners(claude, codex))
        self.assertEqual(ready['status'], 'ready')
        self.assertEqual(ready['drafts'][0]['figure']['check']['model'], p.CODEX_MODEL)
        count = len(calls)
        p.run_job(self.workspace, job['job_id'], p.Runners(claude, codex))
        self.assertEqual(len(calls), count)

    def test_codex_figure_exhaustion_never_attempts_second_method(self):
        job = self.imported(figure={'method': 'code', 'alt_text': '그림', 'spec': {}})
        calls = []
        def exhausted(*args, **kwargs):
            calls.append(kwargs)
            raise p.ProviderQuotaExhausted('codex')
        result = p.run_job(self.workspace, job['job_id'], p.Runners(exhausted, exhausted))
        self.assertEqual(result['status'], 'paused')
        self.assertEqual(len(calls), 1)

    def test_quota_in_verifier_preserves_figure_receipts_then_resumes_only_verify(self):
        job = self.imported(figure={'method': 'code', 'alt_text': '그래프', 'spec': {}})
        fake = FakeModels(self.figure_png)
        def codex(prompt, **kwargs):
            if not kwargs.get('write'):
                raise p.ProviderQuotaExhausted('codex')
            return fake.codex(prompt, **kwargs)
        paused = p.run_job(self.workspace, job['job_id'], p.Runners(fake.claude, codex))
        self.assertEqual(paused['status'], 'paused')
        figure = paused['drafts'][0]['figure']
        p.resume_job(self.workspace, job['job_id'], expected_revision=paused['revision'])
        fake.calls.clear()
        ready = p.run_job(self.workspace, job['job_id'], p.Runners(fake.claude, fake.codex))
        self.assertEqual(ready['status'], 'ready')
        self.assertEqual(ready['drafts'][0]['figure'], figure)
        self.assertEqual(fake.calls, [('codex', 'high', False, 1)])

    def test_manual_edit_invalidates_receipts_without_forging_author(self):
        j = self.imported()
        ig.update_job(self.workspace, j['job_id'], lambda j: j['drafts'][0].update(generator_model=p.CODEX_MODEL,
            model_confirmed=True, contributors=[p.CODEX_MODEL]))
        edited = p.edit_draft(self.workspace, j['job_id'], 1, {'answer': '①'})
        self.assertEqual(edited['drafts'][0]['verification']['status'], 'pending')
        self.assertEqual(edited['drafts'][0]['generator_model'], p.CODEX_MODEL)
        self.assertEqual(set(edited['drafts'][0]['contributors']), {p.CODEX_MODEL, 'human'})


if __name__ == '__main__':
    unittest.main()
