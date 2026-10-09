"""Source-bound REVIEW fragment admissions; no generated content or verification."""
from __future__ import annotations
import collections, copy, hashlib, json, os, pathlib, re
try:
    from solution_link import normalize_subquestion_label
except ModuleNotFoundError:  # imported from Studio: crop modules use bare sibling imports
    import sys as _sys
    _sys.path.append(str(pathlib.Path(__file__).resolve().parent))
    from solution_link import normalize_subquestion_label
CONTRACT = 'original-fragment-review-v1'
SIDECAR = 'solutions/review_admission.json'

def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()

def safe_file(leaf, relative):
    from studio.app.solution_reconciliation import _safe_relative_file
    return _safe_relative_file(leaf, relative)[0]

def compact(text):
 return re.sub(r'[^가-힣a-zA-Z]','',text).lower()

def replay(data):
 rows=data['rows'];ns=data['natives'];out=[]
 qtexts={r['unit_id']:compact(r['question_text']) for r in rows}
 # Unique textual witnesses are indexed across owners, NOT raw child rows.
 owners=collections.defaultdict(set)
 for r in rows:
  t=qtexts[r['unit_id']]
  for i in range(max(0,len(t)-11)):
   owners[t[i:i+12]].add(r['problem']['owner_id'])
 identities=collections.defaultdict(list)
 for r in rows:identities[(r['problem']['parent_number'],normalize_subquestion_label(r['problem']['subquestion_label']))].append(r['unit_id'])
 natives=collections.defaultdict(list)
 for n in ns:natives[(n['parent_number'],normalize_subquestion_label(n['subquestion_label']))].append(n)
 for row in rows:
  p=row['problem'];key=(p['parent_number'],normalize_subquestion_label(p['subquestion_label']));exact=natives[key]
  result={'unit_id':row['unit_id'],'item_id':row['item_id'],'prior_state':row['state'],'parent_number':key[0],'child_label':key[1],'candidate_ids':list(row['manifest_candidates']),'new_candidate_ids':[],'status':'review' if row['manifest_candidates'] else 'held','verified':False,'classification':'existing' if row['manifest_candidates'] else 'missing','evidence':[],'blockers':[]}
  if row['state']!='no_source':out.append(result);continue
  if len(identities[key])!=1:
   result.update(classification='ambiguous',blockers=['repeated_problem_identity']);out.append(result);continue
  if not p['crop_search_eligible'] or not p['crop_verified']:
   result['classification']='ambiguous' if exact else 'missing'
   result['blockers'].append('problem_crop_ineligible');out.append(result);continue
  supported=[]
  for n in exact:
   if n['continuation_unresolved']:continue
   text=compact(n['original_text']);w=[]
   for i in range(max(0,len(text)-11)):
    token=text[i:i+12]
    if token in qtexts[row['unit_id']] and owners[token]=={p['owner_id']}:
     w.append(token)
   if w:
    supported.append(n);result['evidence'].append({'native_asset_id':n['native_asset_id'],'solution_episode_id':n['solution_scope_id'],'role':n['role'],'raw_child_label':n['subquestion_label'],'canonical_child_label':normalize_subquestion_label(n['subquestion_label']),'witnesses':list(dict.fromkeys(w)),'parts':n['parts'],'question_parts':row['question_parts'],'original_text_sha256':hashlib.sha256(n['original_text'].encode()).hexdigest(),'question_text_sha256':hashlib.sha256(row['question_text'].encode()).hexdigest(),'source_binding':data['proof']})
  # A text witness supports a candidate; it never chooses between competing detections.
  if supported:
   detailed=[n for n in supported if n['role']=='detailed_solution'];chosen=detailed or supported
   result['candidate_ids']=[n['native_asset_id'] for n in chosen];result['new_candidate_ids']=result['candidate_ids'];result['status']='review';result['classification']='recovered' if len(chosen)==1 else 'ambiguous';result['role']='detailed_solution' if detailed else 'quick_answer';result['blockers']=['original_text_review_admission_not_full_visual_verification']
   if len(chosen)>1:result['blockers'].append('multiple_original_fragments_require_crop_boundary_review')
  elif exact:
   result['blockers'].append('exact_identity_detected_but_no_unique_original_text_witness')
   result['detected_ids']=[n['native_asset_id'] for n in exact]
   result['classification']='ambiguous'
  else:result['blockers'].append('exact_child_detection_missing')
  out.append(result)
 # Expand only within doubly ORIGINAL-TEXT-anchored, complete monotonic intervals.
 # Preserve episode boundaries; this is per-identity REVIEW evidence, not scope trust.
 stream=sorted([n for n in ns if n['parent_number'] and all(p['printed_page'] is not None for p in n['parts'])],key=lambda n:n['sequence'])
 ranks=[int(n['parent_number']) for n in stream if n['parent_number'].isdigit()]
 qranks=[int(r['problem']['parent_number']) for r in rows if r['problem']['parent_number'].isdigit()]
 monotonic=len(ranks)==len(stream) and len(qranks)==len(rows) and all(a<=b for a,b in zip(ranks,ranks[1:])) and all(a<=b for a,b in zip(qranks,qranks[1:]))
 anchors=sorted([r for r in out if r['classification']=='recovered' and r.get('role')=='detailed_solution' and r['evidence']],key=lambda r:int(r['parent_number']))
 if monotonic and all(e['coverage_complete'] for e in data['episodes']):
  for rr in out:
   if rr['prior_state']!='no_source' or rr['classification']=='recovered' or 'repeated_problem_identity' in rr['blockers']:continue
   p=next(r['problem'] for r in rows if r['unit_id']==rr['unit_id'])
   if not p['crop_search_eligible'] or not p['crop_verified']:continue
   rank=int(p['parent_number']);before=[a for a in anchors if int(a['parent_number'])<rank];after=[a for a in anchors if int(a['parent_number'])>rank]
   if not before or not after:continue
   left,right=before[-1],after[0];lo,hi=int(left['parent_number']),int(right['parent_number'])
   interval=set(range(lo,hi+1))
   if not interval<=set(ranks) or not interval<=set(qranks):continue
   matches=[n for n in stream if n['parent_number']==p['parent_number'] and normalize_subquestion_label(n['subquestion_label'])==normalize_subquestion_label(p['subquestion_label']) and not n['continuation_unresolved']]
   # Ambiguous exact duplicates/fragments are not silently selected or merged.
   if len(matches)!=1:continue
   n=matches[0]
   rr.update(candidate_ids=[n['native_asset_id']],new_candidate_ids=[n['native_asset_id']],classification='recovered',status='review',role=n['role'],blockers=['anchored_interval_review_not_full_visual_verification'])
   rr['evidence'].append({'admission':'complete_monotonic_original_anchor_interval','left_anchor':left['unit_id'],'right_anchor':right['unit_id'],'left_anchor_evidence':left['evidence'],'right_anchor_evidence':right['evidence'],'interval_parent_numbers':sorted(interval),'native_asset_id':n['native_asset_id'],'solution_episode_id':n['solution_scope_id'],'parts':n['parts'],'raw_child_label':n['subquestion_label'],'canonical_child_label':normalize_subquestion_label(n['subquestion_label']),'source_binding':data['proof']})
 # Printed-head pass: the ORIGINAL solution text itself starts the block with this
 # exact problem number (or, for a numbered child continuation, with that child marker).
 # Answer tables are excluded. Identical sibling splits of one printed block collapse to
 # the first detection. Repeated problem identities (misprints) stay held for manual entry.
 for rr in out:
  if rr['prior_state']!='no_source' or rr['classification']=='recovered' or 'repeated_problem_identity' in rr['blockers']:continue
  p=next(r['problem'] for r in rows if r['unit_id']==rr['unit_id']);num=str(p['parent_number']);child=normalize_subquestion_label(p['subquestion_label'])
  if not num.isdigit():continue
  pool=[n for n in ns if str(n['parent_number'])==num and (normalize_subquestion_label(n['subquestion_label'])==child or (child is None and n['subquestion_label']))]
  pool=[n for n in pool if not compact(n['original_text']).startswith('문항정답')]
  head=re.compile(r'\s*'+num+r'(?:\s*\)|\s)')
  headed=[n for n in pool if head.match(n['original_text'])]
  if not headed:
   inner=re.compile(r'(?:^|\n)\s*'+num+r'\s*\)')
   headed=[n for n in pool if inner.search(n['original_text'].replace('\r','\n'))]
  chosen=None;basis=None
  detailed=[n for n in headed if n['role']=='detailed_solution']
  group=detailed or headed
  if len(group)==1:chosen,basis=group[0],'printed_parent_number_head'
  elif group and len({n['original_text'] for n in group})==1 and len({json.dumps(n['parts'],sort_keys=True) for n in group})==1:
   chosen,basis=min(group,key=lambda n:n['sequence']),'identical_printed_block_split'
  elif not headed and child is not None:
   marker=re.compile(r'\s*\(?'+re.escape(child.strip('()'))+r'\)')
   cont=[n for n in pool if n['role']=='detailed_solution' and normalize_subquestion_label(n['subquestion_label'])==child and marker.match(n['original_text'])]
   if len(cont)==1:chosen,basis=cont[0],'printed_child_marker_continuation'
  if chosen is None and child is not None and len({n['original_text'] for n in ns if str(n['parent_number'])==num and not compact(n['original_text']).startswith('문항정답')})==1:
   # Every detection for this number is the same printed parent-headed block, so it covers all children.
   whole=[n for n in ns if str(n['parent_number'])==num and n['role']=='detailed_solution' and head.match(n['original_text']) and not compact(n['original_text']).startswith('문항정답')]
   if whole and len({n['original_text'] for n in whole})==1:
    chosen,basis=min(whole,key=lambda n:n['sequence']),'printed_parent_block_covers_children'
  if chosen is None:continue
  rr.update(candidate_ids=[chosen['native_asset_id']],new_candidate_ids=[chosen['native_asset_id']],classification='recovered',status='review',role=chosen['role'],blockers=['printed_head_review_not_full_visual_verification'])
  rr['evidence'].append({'admission':basis,'native_asset_id':chosen['native_asset_id'],'solution_episode_id':chosen['solution_scope_id'],'parts':chosen['parts'],'raw_child_label':chosen['subquestion_label'],'original_text_sha256':hashlib.sha256(chosen['original_text'].encode()).hexdigest(),'source_binding':data['proof']})
 if len(out)!=len(rows) or len({r['unit_id'] for r in out})!=len(rows):raise ValueError('duplicate admission target')
 totals=collections.Counter(r['classification'] for r in out if r['prior_state']=='no_source')
 return {'contract':'offline-review-admission-v1','source_binding':data['proof'],'scope_changes':False,'automatic_selection':False,'synthetic_semantic_labels':False,'totals_no_source':dict(totals),'rows':out}


def load_evidence(leaf, manifest, *, reextract=True):
    path = safe_file(leaf, SIDECAR)
    initial_sha = digest(path)
    request = json.loads(path.read_text(encoding='utf8'))
    if set(request) != {'contract', 'baseline_link_sha256', 'alignment_index_path', 'corpus', 'diagnosis_path'} or request['contract'] != CONTRACT:
        raise ValueError('invalid fragment admission request')
    data = request['corpus']; proof = data['proof']
    paths = {'manifest_sha256': safe_file(leaf, 'manifest.json'),
             'alignment_index_sha256': safe_file(leaf, request['alignment_index_path']),
             'diagnosis_sha256': safe_file(leaf, request['diagnosis_path'])}
    for key, source in paths.items():
        if digest(source) != proof[key]: raise ValueError('fragment proof SHA changed: '+key)
    for prefix in ('problem', 'solution'):
        source = pathlib.Path(proof[prefix+'_pdf_path'])
        # All sources remain original, pinned PDFs; no regenerated text is admitted.
        if digest(source) != proof[prefix+'_sha256'] or manifest[prefix]['source_sha256'] != proof[prefix+'_sha256']:
            raise ValueError('fragment source SHA changed: '+prefix)
        paths[prefix+'_sha256'] = source
    original = json.loads(paths['manifest_sha256'].read_text(encoding='utf8'))
    index = json.loads(paths['alignment_index_sha256'].read_text(encoding='utf8'))
    diagnosis = json.loads(paths['diagnosis_sha256'].read_text(encoding='utf8'))
    ni = {n['native_asset_id']: n for n in index['native_assets']}
    mi = {r['unit_id']: r for r in original['items']}
    di = {r['unit_id']: r for r in diagnosis['rows']}
    if data['episodes'] != index['episodes'] or len(data['natives']) != len(ni) or {n['native_asset_id'] for n in data['natives']} != set(ni):
        raise ValueError('fragment native/episode inventory changed')
    if len(data['rows']) != len(mi) or {r['unit_id'] for r in data['rows']} != set(mi) or set(mi) != set(di) or set(mi) != set(manifest['items']):
        raise ValueError('fragment target inventory changed')
    for n in data['natives']:
        raw = ni[n['native_asset_id']]
        if any(n[k] != v for k, v in raw.items()) or n['canonical_child'] != normalize_subquestion_label(raw['subquestion_label']):
            raise ValueError('fragment native identity/geometry changed')
    for row in data['rows']:
        raw = mi[row['unit_id']]; prior = di[row['unit_id']]
        if row['question_parts'] != raw['parts'] or row['problem'] != prior['problem'] or row['state'] != prior['state'] or row['manifest_candidates'] != prior['manifest_candidates'] or row['question_crop_sha256'] != raw['output_sha256']:
            raise ValueError('fragment question identity/geometry changed')
    if reextract:
        import pypdfium2 as pdf
        def original_text(doc, index, bbox):
            page = doc[index]; width, height = page.get_size(); textpage = page.get_textpage()
            try:
                x1,y1,x2,y2 = bbox
                return textpage.get_text_bounded(x1*width/1000,(1000-y2)*height/1000,x2*width/1000,(1000-y1)*height/1000)
            finally: textpage.close(); page.close()
        qdoc = pdf.PdfDocument(str(paths['problem_sha256'])); sdoc = pdf.PdfDocument(str(paths['solution_sha256']))
        try:
            for n in data['natives']:
                text = '\n'.join(original_text(sdoc,p['pdf_index'],p['bbox']) for p in n['parts'])
                if text != n['original_text']: raise ValueError('fragment original solution text changed')
            for row in data['rows']:
                text = '\n'.join(original_text(qdoc,p['source_page']-1,p.get('crop_bbox',p['bbox'])) for p in row['question_parts'])
                if text != row['question_text']: raise ValueError('fragment original question text changed')
        finally: qdoc.close(); sdoc.close()
    for key, source in paths.items():
        if digest(source) != proof[key]: raise ValueError('fragment proof changed during validation')
    if digest(path) != initial_sha: raise ValueError('fragment request changed during validation')
    return request, initial_sha, replay(data)


def validate_header(manifest):
    header = manifest.get('fragment_review_admission')
    if header is None: return
    if not isinstance(header, dict) or set(header) != {'contract','sidecar_path','sidecar_sha256','base_link_sha256','base_cache_key','recovered','held'} or header['contract'] != CONTRACT or header['sidecar_path'] != SIDECAR:
        raise ValueError('invalid published fragment review contract')
    from astra_cli import quality
    if manifest['cache_key'] != quality.identity({'source_cache_key':header['base_cache_key'],'fragment_review_sidecar_sha256':header['sidecar_sha256']}):
        raise ValueError('fragment cache signature changed')
    for key in ('sidecar_sha256', 'base_link_sha256'):
        if not isinstance(header[key],str) or not re.fullmatch('[0-9a-f]{64}',header[key]): raise ValueError('invalid fragment review SHA')
    if not isinstance(header['recovered'],dict) or not isinstance(header['held'],dict) or set(header['recovered']) & set(header['held']):
        raise ValueError('invalid fragment review identities')
    for uid, asset_id in header['recovered'].items():
        item = manifest['items'].get(uid,{}); asset = manifest['assets'].get(asset_id,{})
        if item.get('status') != 'review' or item.get('answer_ids') or item.get('explanation_ids') or item.get('candidate_ids') != [asset_id] or asset.get('selection_status') != 'review_only':
            raise ValueError('fragment review admission cannot grant verification/selection')
    for uid, reasons in header['held'].items():
        if uid not in manifest['items'] or not isinstance(reasons,list) or manifest['items'][uid].get('candidate_ids'):
            raise ValueError('ambiguous fragment admission must remain held')


def validate_published(leaf, manifest):
    validate_header(manifest)
    header = manifest.get('fragment_review_admission')
    if header is None: return
    request, side_sha, result = load_evidence(leaf, manifest, reextract=False)
    recovered = {r['unit_id']: r['candidate_ids'][0] for r in result['rows'] if r['classification']=='recovered'}
    held = {r['unit_id']: r['blockers'] for r in result['rows'] if r['prior_state']=='no_source' and r['classification']!='recovered'}
    if header['sidecar_sha256'] != side_sha or header['base_link_sha256'] != request['baseline_link_sha256'] or header['recovered'] != recovered or header['held'] != held:
        raise ValueError('published fragment authority differs from original-bound proof')


def try_review_pipeline(context, hooks):
    """Consume an explicit original-bound request inside the real linker, model-free.

    Existing sources are additive and locked/CAS-published; no changes to DP,
    fixed-edge snapshots, receipts, manual choices, or source-selection revisions.
    """
    leaf = context.run_dir
    request_path = leaf / SIDECAR
    if not request_path.exists(): return None
    from solution_link import (read_link_manifest, write_link_manifest, LinkDecision,
                               LinkStatus, SolutionAsset, SolutionPart, PageIdentity,
                               derive_link_statuses)
    from astra_cli import quality
    from astra_cli.quality_manifest import decision_body
    from solution_runtime import SolutionRuntimeAdapter
    lock = leaf / 'solutions' / '.fragment-review.lock'
    # Existing linker writes are atomic. This admission writer additionally owns
    # an exclusive lock plus an exact-byte compare-and-swap against its approved base.
    fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        os.close(fd)
        link_path = safe_file(leaf,'solutions/link_manifest.json'); base_sha = digest(link_path)
        current = read_link_manifest(leaf)
        if current is None: raise ValueError('fragment admission requires a current real linker manifest')
        if pathlib.Path(current['solution']['source_pdf']).resolve() != context.solution_path.resolve():
            raise ValueError('fragment request solution source differs from linker context')
        if current.get('fragment_review_admission') is not None:
            validate_published(leaf,current)
            return current
        request, side_sha, result = load_evidence(leaf,current)
        if request['baseline_link_sha256'] != base_sha: raise ValueError('fragment admission base CAS changed')
        updated = copy.deepcopy(current)
        natives = {n['native_asset_id']:n for n in request['corpus']['natives']}
        recovered = {}; held = {}
        renderer = SolutionRuntimeAdapter(provider_profile=str(current.get('provider_profile') or 'codex_only'))
        for row in result['rows']:
            if row['prior_state'] != 'no_source':
                if updated['items'][row['unit_id']]['candidate_ids'] != row['candidate_ids']:
                    raise ValueError('existing candidate list differs from admission baseline')
                continue
            uid = row['unit_id']
            if row['classification'] != 'recovered':
                held[uid] = row['blockers']; continue
            aid = row['candidate_ids'][0]; n = natives[aid]
            if updated['items'][uid]['candidate_ids'] or updated['items'][uid]['answer_ids'] or updated['items'][uid]['explanation_ids'] or updated['items'][uid]['status'] in {'verified','conflict'}:
                raise ValueError('fragment admission cannot replace current decision')
            asset = SolutionAsset(aid,n['role'],n['sequence'],'episode:'+n['solution_scope_id'],n['parent_number'],n['subquestion_label'],
                tuple(SolutionPart(PageIdentity(p['pdf_index'],p['pdf_page'],p['printed_page'],p['segment_id']),tuple(p['bbox'])) for p in n['parts']),
                fingerprints=tuple(n['fingerprints']),explicit_answer=n['explicit_answer'],confidence=n['confidence'],review_reason='original_fragment_review_not_verified')
            decision = LinkDecision(uid,LinkStatus.REVIEW,candidate_ids=(aid,),solution_printed_pages=asset.printed_pages,confidence=asset.confidence,
                evidence=('original_fragment_review_admission',),review_reasons=tuple(row['blockers']))
            if aid not in updated['assets']:
                record = dict(renderer.render_solution_assets(context,(asset,),(decision,))[aid]);record.pop('_snapshot_signature',None)
                updated['assets'][aid] = record
            else:
                record = updated['assets'][aid]
                if record.get('quality_signature') != asset.signature() or record.get('selection_status') != 'review_only':
                    raise ValueError('existing native asset differs from fragment identity')
            new_link = decision.to_dict();new_link.update(derive_link_statuses(new_link,updated['assets']))
            new_link['search_states'] = {role:'candidate_review' if role==n['role'] else 'not_searched' for role in ('quick_answer','detailed_solution')}
            new_link['answer_consistency']='not_checked'
            new_link['quality_decision']={'policy':quality.QUALITY_POLICY,'decision_sha256':quality.identity(decision_body(new_link)),'receipts':{}}
            updated['items'][uid]=new_link;recovered[uid]=aid
        updated['fragment_review_admission']={'contract':CONTRACT,'sidecar_path':SIDECAR,'sidecar_sha256':side_sha,'base_link_sha256':base_sha,'base_cache_key':current['cache_key'],'recovered':recovered,'held':held}
        updated['cache_key']=quality.identity({'source_cache_key':current['cache_key'],'fragment_review_sidecar_sha256':side_sha})
        # Revalidate original files and sidecar after rendering, before publication.
        validate_published(leaf,updated)
        if digest(link_path)!=base_sha: raise ValueError('fragment link CAS changed during rendering')
        write_link_manifest(leaf,updated)
        persisted=read_link_manifest(leaf)
        if persisted!=updated: raise ValueError('fragment publication readback differs')
        return persisted
    finally:
        lock.unlink(missing_ok=True)


