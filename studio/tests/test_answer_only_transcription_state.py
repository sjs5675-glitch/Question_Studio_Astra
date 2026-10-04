import copy
import unittest

from app.model import solution_link_summary


class AnswerOnlyTranscriptionStateTest(unittest.TestCase):
    def setUp(self):
        self.document = {
            'workflow': {'revision': 3, 'status': 'needs_review'},
            'review': {'issues': ['source needs review']},
            'content': {
                'answer': {'kind': 'choice', 'choice_ids': ['choice-2']},
                'explanation': {'blocks': []},
            },
            'resources': {'assets': [{
                'asset_id': 'answer-source', 'sha256': 'a' * 64,
                'role': 'source_crop', 'section': 'answer',
                'state': 'ready', 'path': 'assets/answer.png',
            }]},
            'provenance': {
                'solution_links': [{
                    'kind': 'quick_answer', 'status': 'review',
                    'source_asset_id': 'answer-source',
                }],
                'transcriptions': [{
                    'section': 'explanation', 'status': 'succeeded',
                    'input_asset_ids': ['answer-source'],
                    'shared_source_sha256': 'a' * 64,
                }],
            },
        }

    def test_saved_answer_with_current_success_counts_as_complete_without_filling_explanation(self):
        before = copy.deepcopy(self.document)
        result = solution_link_summary(self.document)
        self.assertTrue(result['transcribed'])
        self.assertEqual('complete', result['transcription_state'])
        self.assertEqual([], self.document['content']['explanation']['blocks'])
        self.assertEqual(before, self.document)
        self.assertEqual('missing', result['status'])


    def test_all_supported_saved_answer_kinds_can_complete_without_explanation(self):
        answers = [
            {'kind': 'choice', 'choice_ids': ['choice-2']},
            {'kind': 'content', 'blocks': [{'type': 'paragraph', 'inlines': [{'type': 'text', 'text': '⑤'}]}]},
            {'kind': 'true_false', 'items': [{'statement_id': 'statement-1', 'value': True}]},
        ]
        for answer in answers:
            with self.subTest(kind=answer['kind']):
                self.document['content']['answer'] = answer
                result = solution_link_summary(self.document)
                self.assertTrue(result['answer_only_transcribed'])
                self.assertTrue(result['transcribed'])
                self.assertEqual('complete', result['transcription_state'])

    def test_empty_or_unknown_answer_is_not_success(self):
        answers = [None, {'kind': 'unknown'}, {'kind': 'choice', 'choice_ids': []},
                   {'kind': 'content', 'blocks': []}, {'kind': 'true_false', 'items': []}]
        for answer in answers:
            with self.subTest(answer=answer):
                self.document['content']['answer'] = answer
                result = solution_link_summary(self.document)
                self.assertFalse(result['transcribed'])
                self.assertFalse(result['answer_only_transcribed'])

    def test_failed_or_unmatched_success_does_not_complete(self):
        original = self.document['provenance']['transcriptions'][0]
        for updates in [
            {'status': 'failed'}, {'section': 'question'},
            {'input_asset_ids': ['another-source']}, {'shared_source_sha256': 'b' * 64},
        ]:
            with self.subTest(updates=updates):
                self.document['provenance']['transcriptions'] = [dict(original, **updates)]
                result = solution_link_summary(self.document)
                self.assertFalse(result['transcribed'])
                self.assertNotEqual('complete', result['transcription_state'])
        self.document['provenance']['transcriptions'] = []
        self.assertFalse(solution_link_summary(self.document)['transcribed'])

    def test_current_selection_revision_remains_required(self):
        self.document['provenance']['solution_selection'] = {
            'source_asset_id': 'answer-source', 'source_sha256': 'a' * 64, 'revision': 2,
        }
        record = self.document['provenance']['transcriptions'][0]
        record['selection_revision'] = 1
        self.assertFalse(solution_link_summary(self.document)['transcribed'])
        record['selection_revision'] = 2
        self.assertTrue(solution_link_summary(self.document)['transcribed'])

    def test_rejected_or_ambiguous_source_stays_on_hold(self):
        self.document['provenance']['solution_links'][0]['status'] = 'rejected'
        self.assertFalse(solution_link_summary(self.document)['transcribed'])
        self.document['provenance']['solution_links'][0]['status'] = 'review'
        self.document['provenance']['solution_links'].append({
            'kind': 'quick_answer', 'status': 'review', 'source_asset_id': 'another-source',
        })
        self.assertFalse(solution_link_summary(self.document)['transcribed'])

    def test_answer_only_from_a_detailed_source_has_its_own_flag(self):
        self.document['provenance']['solution_links'][0]['kind'] = 'detailed_solution'
        result = solution_link_summary(self.document)
        self.assertTrue(result['transcribed'])
        self.assertTrue(result['answer_only_transcribed'])
        self.assertTrue(result['requires_review'])
        self.assertEqual('review', result['status'])
        self.document['provenance']['solution_links'][0]['review_reasons'] = ['wrong_link']
        self.assertFalse(solution_link_summary(self.document)['transcribed'])

    def test_existing_explanation_can_complete_without_answer_and_is_not_erased(self):
        self.document['content']['answer'] = None
        self.document['content']['explanation']['blocks'] = [{'type': 'paragraph', 'inlines': [{'type': 'text', 'text': 'Actual explanation'}]}]
        before = copy.deepcopy(self.document)
        result = solution_link_summary(self.document)
        self.assertTrue(result['transcribed'])
        self.assertFalse(result['answer_only_transcribed'])
        self.assertEqual(before, self.document)

    def test_missing_or_changed_source_file_does_not_complete(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            result = solution_link_summary(self.document, item_root=root)
            self.assertFalse(result['transcribed'])
            self.assertEqual('missing_file', result['source_integrity'])
            source = root / 'assets/answer.png'
            source.parent.mkdir()
            source.write_bytes(b'changed source bytes')
            result = solution_link_summary(self.document, item_root=root)
            self.assertFalse(result['transcribed'])
            self.assertEqual('hash_mismatch', result['source_integrity'])

    def test_real_job_apply_records_success_and_keeps_saved_explanation_empty(self):
        import json
        import tempfile
        from pathlib import Path
        from test_transcription import FakeAdapter, TranscriptionTest
        from app.model import atomic_write_json
        from app.transcription import create_transcription_job, apply_candidate_job
        with tempfile.TemporaryDirectory() as temporary:
            path, source_id = TranscriptionTest().make_item(Path(temporary), section='answer')
            document = json.loads(path.read_text(encoding='utf-8'))
            document['provenance']['solution_links'] = [{
                'kind': 'quick_answer', 'status': 'review', 'source_asset_id': source_id,
            }]
            atomic_write_json(path, document)
            candidate = {
                'schema_version': '0.1.0', 'section': 'explanation', 'blocks': [],
                'answer': {'kind': 'content', 'choice_labels': [], 'runs': [{'type': 'text', 'text': '⑤'}], 'true_false': []},
                'coverage_complete': True, 'ignored_regions': [], 'warnings': [],
            }
            job = create_transcription_job(path, source_id, 'explanation', adapter=FakeAdapter(candidate))
            saved = apply_candidate_job(path, job.path, expected_base_revision=1)
            reread = json.loads(path.read_text(encoding='utf-8'))
            self.assertEqual(saved, reread)
            self.assertIsNone(reread['content']['explanation'])
            self.assertEqual('⑤', reread['content']['answer']['blocks'][0]['inlines'][0]['text'])
            self.assertEqual('succeeded', reread['provenance']['transcriptions'][-1]['status'])
            self.assertEqual('needs_review', reread['workflow']['status'])
            self.assertEqual('review', reread['provenance']['solution_links'][0]['status'])
            result = solution_link_summary(reread, item_root=path.parent)
            self.assertTrue(result['answer_only_transcribed'])
            self.assertEqual('complete', result['transcription_state'])


if __name__ == '__main__':
    unittest.main()
