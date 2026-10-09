"""Disposable local HTTP fixture with deterministic runners, no model/worker threads."""
import json
import sys
import threading
from pathlib import Path
from unittest import mock

from app import item_generation as ig, item_generation_pipeline as p
from app.server import create_server
from tests.test_item_generation import LabFixture, FakeModels, source_blocks


def main():
    fixture = LabFixture()
    fixture.setUp()
    server = None
    try:
        fake = FakeModels(fixture.figure_png)
        def claude(prompt, **kwargs):
            text, _ = fake.claude(prompt, **kwargs) if 'results' not in prompt else fake.codex(prompt,
                effort=kwargs['effort'], images=[])
            return text, {'actual_model': p.CLAUDE_MODEL, 'model_confirmed': True, 'usage': {'input_tokens': 1}}
        def codex(prompt, **kwargs):
            text, _ = fake.codex(prompt, **kwargs)
            return text, {'actual_model': p.CODEX_MODEL, 'model_confirmed': True, 'usage': {'input_tokens': 1}}
        # Figure comparison needs its own boolean response, not an answer response.
        def author(prompt, **kwargs):
            if '그림 파일을 Read 도구로 열어' in prompt:
                text, _ = fake.claude(prompt, **kwargs)
                return text, {'actual_model': p.CLAUDE_MODEL, 'model_confirmed': True}
            return claude(prompt, **kwargs)
        runners = p.Runners(author, codex)
        job = ig.create_job(fixture.workspace, fixture.scoped_request('고2-물리학'))
        job = p.run_job(fixture.workspace, job['job_id'], runners)
        if job['status'] != 'ready':
            raise RuntimeError(job['error'])
        def fallback(j):
            j['drafts'][1].update(generator_model=p.CODEX_MODEL, actual_model=p.CODEX_MODEL,
                contributors=[p.CODEX_MODEL], model_confirmed=True)
            j['drafts'][1]['verification']['review_kind'] = 'self_check'
        job = ig.update_job(fixture.workspace, job['job_id'], fallback)
        stored = ig.confirm_drafts(fixture.workspace, job['job_id'], [1])
        item_path = fixture.workspace / stored['project_id'] / 'items' / stored['results'][0]['item_id'] / 'question.json'
        package = ig.load_json_object(item_path)
        package['content']['question']['blocks'][0]['inlines'] = [{'type': 'text', 'text': '현재 canonical 수정 본문'}]
        package['workflow']['revision'] += 1
        ig.atomic_write_json(item_path, package)
        before = item_path.read_bytes()
        worker_class = p.GenerationWorker
        # Candidate astra_runtime derives ROOT from its checkout. Test-only root
        # validation reflects the deployed project while every writable path stays synthetic.
        with mock.patch('astra_runtime.ROOT', Path('C:/Question_Studio_Astra')), \
                mock.patch('app.item_generation_pipeline.GenerationWorker', side_effect=lambda workspace, **kw: worker_class(
                workspace, runners=runners, autostart=False)):
            server = create_server(workspace_root=fixture.workspace, web_root=Path(__file__).parents[1] / 'web',
                crop_root=Path('C:/Question_Studio_Astra/crop'), import_roots=[fixture.workspace], port=0,
                token='synthetic-itemgen-ui')
        server.app.item_generation_worker.wake = server.app.item_generation_worker.run_once
        print('READY ' + json.dumps({'port': server.server_address[1], 'job_id': job['job_id']}), flush=True)
        def shutdown_on_stdin():
            for line in sys.stdin:
                if line.strip() == 'shutdown':
                    server.shutdown()
                    return
        threading.Thread(target=shutdown_on_stdin, name='synthetic-fixture-control', daemon=True).start()
        server.serve_forever()
        assert item_path.read_bytes() == before, 'review must preserve the canonical package'
    finally:
        if server:
            server.server_close()
        fixture.tearDown()
        fixture.doCleanups()


if __name__ == '__main__':
    main()
