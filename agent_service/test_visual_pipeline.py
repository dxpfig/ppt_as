"""Publication boundary tests; image/model behavior is covered by reviewer tests."""
import asyncio
from copy import deepcopy
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from types import SimpleNamespace

from agent_service.pptd_store import DocumentStore, RevisionConflict
from agent_service.test_pptd_generation import bundle


class VisualPipelineTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = DocumentStore(self.root)
        self.source = bundle()
        self.store.commit('deck', **self.source, label='Original')
        self.folder = self.root / 'artifacts' / 'job'
        self.checkpoint = {}
        self.events = []
        self.request = SimpleNamespace(scenario='tech-engineering', usage='reading', kind='render')

    async def asyncTearDown(self):
        self.tmp.cleanup()

    async def export(self, store, project_id, folder, progress):
        folder.mkdir(parents=True)
        state = store.read(project_id)
        (folder / 'export-report.json').write_text(json.dumps({'revision':state['revision']}))
        (folder / 'presentation.pptx').write_text(state['pages'][0]['data']['elements'][0]['content']['text'])
        return {'revision':state['revision'],'report':'export-report.json','pageCount':1}

    async def run_pipeline(self, reviewer, repairer=None):
        from agent_service import visual_pipeline as pipeline
        with patch.object(pipeline.pptd_exports, 'export_document', side_effect=self.export), \
             patch.object(pipeline, 'review_deck', side_effect=reviewer), \
             patch.object(pipeline, 'repair_deck', side_effect=repairer):
            return await pipeline.export_reviewed(self.store, 'deck', self.folder, self.request,
                checkpoint=self.checkpoint, save_checkpoint=lambda cp:self.checkpoint.update(deepcopy(cp)),
                on_event=self.events.append, assert_current=lambda:None)

    def report(self, state, issues=None):
        return {'status':'issues' if issues else 'passed','coveredPages':state['document']['pages'],
                'issues':issues or [],'model':'test-vision'}

    def issue(self):
        return {'pagePath':'pages/one.page','pageIndex':0,'elementIds':['title'],'severity':'error',
                'code':'unbalanced','message':'Title too narrow','evidence':'right side empty','repair':'increase title width'}

    async def test_candidate_is_private_until_all_pages_pass_and_repair_is_committed_once(self):
        calls=[]
        async def review(state, folder, **kw):
            self.assertFalse(self.folder.exists())
            self.assertEqual(self.store.read('deck')['revision'],1)
            if calls:
                self.assertEqual(kw['previous_review']['issues'], [self.issue()])
            else:
                self.assertIsNone(kw['previous_review'])
            calls.append(deepcopy(state))
            return self.report(state, [self.issue()] if len(calls)==1 else [])
        async def repair(request,state,issues,**kw):
            value=deepcopy(state)
            value['pages'][0]['data']['elements'][0]['bounds']=[50,60,900,80]
            return value
        result=await self.run_pipeline(review,repair)
        self.assertEqual(len(calls),2)
        self.assertEqual(self.store.read('deck')['revision'],2)
        self.assertEqual(result['revision'],2)
        self.assertEqual(result['visualReview']['rounds'],2)
        self.assertEqual((self.folder/'presentation.pptx').read_text(),'Hello')
        self.assertEqual(json.loads((self.folder/'export-report.json').read_text())['visualReview']['status'],'passed')

    async def test_review_can_run_in_validation_without_publishing_an_export(self):
        from agent_service import visual_pipeline as pipeline
        async def review(state, *args, **kwargs):
            return self.report(state)
        with patch.object(pipeline.pptd_exports, 'export_document', side_effect=self.export), \
             patch.object(pipeline, 'review_deck', side_effect=review):
            result = await pipeline.export_reviewed(
                self.store, 'deck', self.folder, self.request,
                checkpoint=self.checkpoint,
                save_checkpoint=lambda cp: self.checkpoint.update(deepcopy(cp)),
                on_event=self.events.append, assert_current=lambda: None, publish=False,
            )
        self.assertFalse(self.folder.exists())
        self.assertFalse(result['published'])
        self.assertEqual(result['visualReview']['status'], 'passed')
        self.assertEqual(self.store.read('deck')['revision'], 1)
        self.assertEqual(self.checkpoint['stage'], 'validated')

    async def test_visual_repair_is_split_into_bounded_page_batches(self):
        from agent_service import visual_pipeline as pipeline

        state = {
            "document": {"pages": [f"pages/{index}.page" for index in range(1, 8)]},
            "pages": [
                {"path": f"pages/{index}.page", "data": {"elements": []}}
                for index in range(1, 8)
            ],
        }
        issues = [
            {
                "pagePath": page["path"],
                "pageIndex": index,
                "elementIds": [],
                "code": "layout",
                "message": "layout",
            }
            for index, page in enumerate(state["pages"])
        ]
        batches = []

        async def repair(_request, current, batch_issues, **_kwargs):
            batches.append([item["pagePath"] for item in batch_issues])
            return deepcopy(current)

        with patch.dict(os.environ, {"PPTD_VISUAL_REPAIR_PAGE_BATCH": "3"}), \
             patch.object(pipeline, "repair_deck", side_effect=repair):
            result = await pipeline.repair_deck_batched(
                self.request, state, issues, validate_candidate=None
            )
        self.assertEqual(
            [[path.split("/")[-1] for path in batch] for batch in batches],
            [
                ["1.page", "2.page", "3.page"],
                ["4.page", "5.page", "6.page"],
                ["7.page"],
            ],
        )
        self.assertEqual(result["document"], state["document"])

    async def test_partial_coverage_is_not_a_pass(self):
        async def review(state,*args,**kw):return {**self.report(state),'coveredPages':[]}
        with self.assertRaisesRegex(ValueError,'覆盖'):
            await self.run_pipeline(review)
        self.assertFalse(self.folder.exists())
        self.assertEqual(self.store.read('deck')['revision'],1)

    async def test_missing_figure_is_completed_privately_then_rerendered_and_reviewed(self):
        from agent_service import visual_pipeline as pipeline
        from agent_service.visual_completion import apply_completion
        from agent_service.test_visual_completion import completion_answer, plan
        self.checkpoint['plan'] = plan()
        calls = []
        async def review(state, *args, **kwargs):
            self.assertEqual(len(self.store.read('deck')['pages'][0]['data']['elements']), 1)
            calls.append(deepcopy(state))
            return self.report(state, [{**self.issue(), 'code': 'missing-visual-content'}] if len(calls) == 1 else [])
        async def complete(request, state, issues, saved_plan, **kwargs):
            answer = completion_answer()
            candidate = apply_completion(state, saved_plan, answer, issues)
            kwargs['on_audit']({'kind': 'completion', 'status': 'accepted', 'answer': answer})
            return candidate, {'answer': answer, 'issues': issues}
        async def forbidden(*args, **kwargs): self.fail('Missing content must not enter style-only repair')
        with patch.object(pipeline, 'complete_content', side_effect=complete):
            result = await self.run_pipeline(review, forbidden)
        self.assertEqual(len(calls), 2)
        self.assertEqual(result['visualReview']['status'], 'passed')
        self.assertEqual(len(self.store.read('deck')['pages'][0]['data']['elements']), 3)
        self.assertTrue(self.checkpoint['_visualReview']['repairAudit'])

    async def test_forged_completion_changes_and_corrupt_resume_are_rejected(self):
        from agent_service import visual_pipeline as pipeline
        from agent_service.visual_completion import apply_completion
        from agent_service.test_visual_completion import completion_answer, plan
        self.checkpoint['plan'] = plan()
        async def review(state, *args, **kwargs):
            return self.report(state, [{**self.issue(), 'code': 'missing-visual-content'}])
        async def corrupt(request, state, issues, saved_plan, **kwargs):
            answer = completion_answer(); candidate = apply_completion(state, saved_plan, answer, issues)
            candidate['pages'][0]['data']['elements'][0]['content']['text'] = 'LOST SOURCE'
            return candidate, {'answer': answer, 'issues': issues}
        with patch.object(pipeline, 'complete_content', side_effect=corrupt), self.assertRaises(pipeline.VisualIntegrityError):
            await self.run_pipeline(review)
        self.assertEqual(self.store.read('deck')['revision'], 1)
        self.assertFalse(self.folder.exists())

    async def test_completed_candidate_survives_interruption_without_duplicate_additions(self):
        from agent_service import visual_pipeline as pipeline
        from agent_service.visual_completion import apply_completion
        from agent_service.test_visual_completion import completion_answer, plan
        self.checkpoint['plan'] = plan()
        count = 0
        async def review(state, *args, **kwargs):
            nonlocal count
            count += 1
            if count == 2: raise asyncio.CancelledError()
            return self.report(state, [{**self.issue(), 'code': 'missing-visual-content'}])
        async def complete(request, state, issues, saved_plan, **kwargs):
            answer = completion_answer()
            return apply_completion(state, saved_plan, answer, issues), {'answer': answer, 'issues': issues}
        with patch.object(pipeline, 'complete_content', side_effect=complete), self.assertRaises(asyncio.CancelledError):
            await self.run_pipeline(review)
        self.assertEqual(self.store.read('deck')['revision'], 1)
        self.assertEqual(len(self.checkpoint['_visualReview']['completionReceipts']), 1)
        async def passing(state, *args, **kwargs):
            self.assertEqual(len(state['pages'][0]['data']['elements']), 3)
            return self.report(state)
        result = await self.run_pipeline(passing)
        self.assertEqual(result['revision'], 2)
        self.assertEqual(self.checkpoint['_visualReview']['repairs'], 1)

    async def test_completion_commit_receipt_recovers_failed_checkpoint_write(self):
        from agent_service import visual_pipeline as pipeline
        from agent_service.visual_completion import apply_completion
        from agent_service.test_visual_completion import completion_answer, plan
        self.checkpoint['plan'] = plan()
        async def review(state, *args, **kwargs):
            return self.report(state, [{**self.issue(), 'code': 'missing-visual-content'}]
                if len(state['pages'][0]['data']['elements']) == 1 else [])
        async def complete(request, state, issues, saved_plan, **kwargs):
            answer = completion_answer()
            return apply_completion(state, saved_plan, answer, issues), {'answer': answer, 'issues': issues}
        def save(cp):
            if cp.get('_committedRevision') == 2: raise OSError('checkpoint write failed')
            self.checkpoint = deepcopy(cp)
        with patch.object(pipeline.pptd_exports, 'export_document', side_effect=self.export), patch.object(pipeline, 'review_deck', side_effect=review), patch.object(pipeline, 'complete_content', side_effect=complete):
            with self.assertRaises(OSError):
                await pipeline.export_reviewed(self.store, 'deck', self.folder, self.request,
                    checkpoint=self.checkpoint, save_checkpoint=save)
        state = self.store.read('deck')
        self.assertEqual(state['revision'], 2)
        recovered = pipeline.reconcile_visual_checkpoint(self.checkpoint, state)
        self.assertEqual(recovered['_committedRevision'], 2)
        self.assertEqual(recovered['_visualReview']['completionReceipts'], [])
        tampered = deepcopy(self.checkpoint)
        tampered['_visualReview']['sourceSemanticSha256'] = 'wrong-original-proof'
        self.assertEqual(pipeline.reconcile_visual_checkpoint(tampered, state)['_committedRevision'], 1)

    async def test_mixed_findings_use_separate_completion_and_style_passes(self):
        from agent_service import visual_pipeline as pipeline
        from agent_service.visual_completion import apply_completion
        from agent_service.test_visual_completion import completion_answer, plan
        self.checkpoint['plan'] = plan()
        missing = {**self.issue(), 'code': 'missing-visual-content'}
        reviews = []
        async def review(state, *args, **kwargs):
            reviews.append(True)
            return self.report(state, [missing, self.issue()] if len(reviews) == 1 else [self.issue()] if len(reviews) == 2 else [])
        async def complete(request, state, issues, saved_plan, **kwargs):
            self.assertEqual(issues, [missing])
            answer = completion_answer()
            return apply_completion(state, saved_plan, answer, issues), {'answer': answer, 'issues': issues}
        async def repair(request, state, issues, **kwargs):
            self.assertEqual(issues, [self.issue()])
            candidate = deepcopy(state)
            candidate['pages'][0]['data']['elements'][0]['bounds'][2] -= 20
            return candidate
        with patch.object(pipeline, 'complete_content', side_effect=complete):
            result = await self.run_pipeline(review, repair)
        self.assertEqual(len(reviews), 3)
        self.assertEqual(self.checkpoint['_visualReview']['repairs'], 2)
        self.assertEqual(result['revision'], 2)

    async def test_old_chart_style_checkpoint_migrates_only_with_valid_original_proof(self):
        from agent_service import visual_pipeline as pipeline
        from agent_service.visual_repair import semantic_payload
        source = deepcopy(self.source)
        source['pages'][0]['data']['elements'].append({'elementType': 'chart', 'elementId': 'chart',
            'bounds': [40, 200, 400, 240], 'data': {'cols': ['x','y'], 'rows': [['A', 3]]},
            'series': [{'type': 'bar', 'encode': {'x': 'x', 'y': 'y'}}],
            'dataLabels': {'show': True, 'position': 'outsideEnd'}, 'xAxis': {'label': {'rotation': 25}}})
        self.store.commit('deck', **source, label='Old source', expected_revision=1)
        self.checkpoint['_visualReview'] = {'sourceRevision': 2, 'repairs': 0, 'reports': [], 'fingerprints': [],
            'sourceSemanticSha256': pipeline._source_sha256(semantic_payload(source, legacy=True)),
            'candidate': source}
        async def passing(state, *args, **kwargs): return self.report(state)
        result = await self.run_pipeline(passing)
        self.assertEqual(result['revision'], 2)
        self.assertEqual(self.checkpoint['_visualReview']['semanticContractVersion'], 3)

    async def test_exhausted_review_preserves_original_and_resume_does_not_reset_budget(self):
        async def review(state,*args,**kw):return self.report(state,[self.issue()])
        async def repair(request,state,*args,**kw):
            value=deepcopy(state);value['pages'][0]['data']['elements'][0]['bounds'][2]+=5
            return value
        with self.assertRaises(ValueError):await self.run_pipeline(review,repair)
        self.assertFalse(self.folder.exists())
        self.assertEqual(self.store.read('deck')['revision'],1)
        self.assertGreater(self.checkpoint['_visualReview']['repairs'],0)
        async def forbidden(*a,**kw):self.fail('resume must not reset repair budget')
        with self.assertRaises(ValueError):await self.run_pipeline(review,forbidden)

    async def test_identical_issue_fingerprint_stops_repair_before_budget(self):
        repairs = []

        async def review(state, *args, **kwargs):
            return self.report(state, [self.issue()])

        async def repair(request, state, *args, **kwargs):
            repairs.append(True)
            value = deepcopy(state)
            value['pages'][0]['data']['elements'][0]['bounds'][2] += 5
            return value

        with self.assertRaises(ValueError):
            await self.run_pipeline(review, repair)
        self.assertEqual(len(repairs), 1)
        self.assertEqual(self.store.read('deck')['revision'], 1)

    async def test_revision_change_during_review_prevents_publication(self):
        async def review(state,*a,**kw):
            self.store.commit('deck',**self.source,label='Concurrent change',expected_revision=1)
            return self.report(state)
        with self.assertRaises(RevisionConflict):await self.run_pipeline(review)
        self.assertFalse(self.folder.exists())

    async def test_cancel_keeps_repair_checkpoint_without_publishing(self):
        count=0
        async def review(state,*a,**kw):
            nonlocal count
            count+=1
            if count==2:raise asyncio.CancelledError()
            return self.report(state,[self.issue()])
        async def repair(request,state,*a,**kw):
            value=deepcopy(state);value['pages'][0]['data']['elements'][0]['bounds'][2]+=5
            return value
        with self.assertRaises(asyncio.CancelledError):await self.run_pipeline(review,repair)
        self.assertEqual(self.checkpoint['_visualReview']['repairs'],1)
        self.assertEqual(self.store.read('deck')['revision'],1)
        self.assertFalse(self.folder.exists())

    async def test_renderer_regression_after_visual_repair_is_repaired_automatically(self):
        from agent_service.pptd_exports import ValidationError
        original_export=self.export;count=0
        async def export(*args):
            nonlocal count
            count+=1
            if count==2:raise ValidationError([{'page':'pages/one.page','elementId':'title','code':'text-overflow','message':'overflow'}])
            return await original_export(*args)
        self.export=export
        reviews=0
        async def review(state,*a,**kw):
            nonlocal reviews
            reviews+=1
            return self.report(state,[self.issue()] if reviews==1 else [])
        async def repair(request,state,*a,**kw):
            value=deepcopy(state);value['pages'][0]['data']['elements'][0]['bounds'][2]+=5
            return value
        result=await self.run_pipeline(review,repair)
        self.assertEqual(result['visualReview']['status'],'passed')
        self.assertEqual(count,3)

    async def test_recovered_body_overflow_uses_safe_layout_repair_before_image_review(self):
        from agent_service.pptd_exports import ValidationError
        self.request.kind = 'outline'
        self.checkpoint['_contentRecoveryPages'] = ['pages/one.page']
        initial_export = self.export
        attempts = []
        async def export(store, project_id, folder, progress):
            state = store.read(project_id)
            attempts.append(state['pages'][0]['data']['elements'][0]['bounds'][3])
            if len(attempts) == 1:
                raise ValidationError([{'page':'pages/one.page','elementId':'title',
                    'code':'text-overflow','message':'原文换行高度超过文本框'}])
            return await initial_export(store, project_id, folder, progress)
        self.export = export
        async def repair(request, state, issues, **kwargs):
            value = deepcopy(state)
            value['pages'][0]['data']['elements'][0]['bounds'][3] += 40
            return value
        async def review(state, *args, **kwargs):
            self.assertEqual(state['pages'][0]['data']['elements'][0]['content']['text'], 'Hello')
            return self.report(state)
        result = await self.run_pipeline(review, repair)
        self.assertEqual(result['visualReview']['status'], 'passed')
        self.assertEqual(len(attempts), 2)
        self.assertEqual(self.store.read('deck')['pages'][0]['data']['elements'][0]['content']['text'], 'Hello')
        self.assertEqual(self.checkpoint['_contentRecoveryPages'], ['pages/one.page'])

    async def test_generated_page_layout_failure_uses_protected_repair_before_publication(self):
        from agent_service.pptd_exports import ValidationError
        self.request.kind = 'outline'
        original_export = self.export
        async def export(store, project_id, folder, progress):
            if store.read(project_id)['pages'][0]['data']['elements'][0]['bounds'][3] < 140:
                raise ValidationError([{'page': 'pages/one.page', 'elementId': 'title',
                    'code': 'text-overflow', 'message': 'Need 140px height'}])
            return await original_export(store, project_id, folder, progress)
        self.export = export
        async def repair(request, state, issues, **kwargs):
            from agent_service.visual_repair import apply_visual_patches
            self.assertFalse(self.folder.exists())
            return apply_visual_patches(state, [{'pagePath': 'pages/one.page', 'elementId': 'title',
                'op': 'set', 'path': '/bounds/3', 'value': 140}], issues)
        async def review(state, *args, **kwargs):
            return self.report(state)
        try:
            result = await self.run_pipeline(review, repair)
        except ValidationError:
            self.fail('Layout failures must stay in protected repair, not return to full-page generation')
        self.assertEqual(result['visualReview']['status'], 'passed')
        self.assertEqual(self.store.read('deck')['pages'][0]['data']['elements'][0]['content']['text'], 'Hello')
        self.assertEqual(self.store.read('deck')['revision'], 2)

    async def test_changed_content_from_repair_adapter_never_reaches_publication(self):
        reviews=0
        async def review(state,*args,**kwargs):
            nonlocal reviews
            reviews+=1
            return self.report(state,[self.issue()] if reviews==1 else [])
        async def unsafe(request,state,*args,**kwargs):
            candidate=deepcopy(state)
            candidate['pages'][0]['data']['elements'][0]['content']['text']='LOST ORIGINAL CONTENT'
            return candidate
        with self.assertRaisesRegex(ValueError,'内容|语义'):
            await self.run_pipeline(review,unsafe)
        self.assertEqual(reviews,1)
        self.assertEqual(self.store.read('deck')['pages'],self.source['pages'])
        self.assertFalse(self.folder.exists())

    async def test_corrupt_resumed_candidate_is_rejected_before_renderer_or_model(self):
        candidate=deepcopy(self.source)
        candidate['pages'][0]['data']['elements'][0]['content']['text']='CORRUPT CHECKPOINT'
        self.checkpoint['_visualReview']={'sourceRevision':1,'repairs':1,'reports':[],'fingerprints':[],'candidate':candidate}
        async def forbidden(*args,**kwargs):self.fail('corrupt source must fail before review')
        with self.assertRaisesRegex(ValueError,'内容|语义'):
            await self.run_pipeline(forbidden)
        self.assertEqual(self.store.read('deck')['revision'],1)
        self.assertFalse(self.folder.exists())

    async def test_artifact_move_failure_does_not_commit_a_repaired_document(self):
        from agent_service import visual_pipeline as pipeline
        import os
        reviews=0
        async def review(state,*args,**kwargs):
            nonlocal reviews
            reviews+=1
            return self.report(state,[self.issue()] if reviews==1 else [])
        async def repair(request,state,*args,**kwargs):
            candidate=deepcopy(state);candidate['pages'][0]['data']['elements'][0]['bounds'][2]+=5
            return candidate
        original=os.replace
        def replace(source,target):
            if target==self.folder:raise OSError('artifact publication failure')
            return original(source,target)
        with patch.object(pipeline.os,'replace',side_effect=replace),self.assertRaises(OSError):
            await self.run_pipeline(review,repair)
        self.assertEqual(self.store.read('deck')['revision'],1)
        self.assertFalse(self.folder.exists())

    async def test_saved_verification_proof_recovers_checkpoint_failure_after_commit(self):
        from agent_service import visual_pipeline as pipeline
        reviews=0
        async def review(state,*args,**kwargs):
            nonlocal reviews
            reviews+=1
            return self.report(state,[self.issue()] if reviews==1 else [])
        async def repair(request,state,*args,**kwargs):
            candidate=deepcopy(state);candidate['pages'][0]['data']['elements'][0]['bounds'][2]+=5
            return candidate
        def save(cp):
            if cp.get('_committedRevision')==2:raise OSError('checkpoint write failed after commit')
            self.checkpoint=deepcopy(cp)
        with patch.object(pipeline.pptd_exports,'export_document',side_effect=self.export),patch.object(pipeline,'review_deck',side_effect=review),patch.object(pipeline,'repair_deck',side_effect=repair):
            with self.assertRaises(OSError):
                await pipeline.export_reviewed(self.store,'deck',self.folder,self.request,checkpoint=self.checkpoint,save_checkpoint=save)
        self.assertEqual(self.store.read('deck')['revision'],2)
        self.assertEqual(self.checkpoint['_committedRevision'],1)
        self.assertIn('pendingVerified',self.checkpoint['_visualReview'])
        recovered=pipeline.reconcile_visual_checkpoint(self.checkpoint,self.store.read('deck'))
        self.assertEqual(recovered['_committedRevision'],2)
        self.assertEqual(recovered['_visualReview']['sourceRevision'],2)
        self.assertEqual(self.checkpoint['_committedRevision'],1)
        async def passing(state,*args,**kwargs):return self.report(state)
        result=await self.run_pipeline(passing)
        self.assertEqual(result['revision'],2)

    async def test_successful_visual_gate_persists_approved_page_states(self):
        async def passing(state, *args, **kwargs):
            return self.report(state)

        await self.run_pipeline(passing)
        states = self.checkpoint['_pageStates']
        self.assertEqual(states[0]['path'], 'pages/one.page')
        self.assertEqual(states[0]['status'], 'approved')

    async def test_pending_proof_cannot_reconcile_other_content_or_concurrent_revision(self):
        from agent_service import visual_pipeline as pipeline
        async def review(state,*args,**kwargs):return self.report(state)
        await self.run_pipeline(review)
        proof=deepcopy(self.checkpoint)
        current=self.store.read('deck')
        # A different revision containing identical content is still a distinct
        # user change, and a same-revision payload mismatch is never accepted.
        for change in ('revision','source','semantic-proof'):
            cp=deepcopy(proof);state=deepcopy(current)
            cp['_committedRevision']=0;cp['_visualReview']['sourceRevision']=0
            if change=='revision':state['revision']+=1
            elif change=='source':state['pages'][0]['data']['elements'][0]['content']['text']='changed'
            else:cp['_visualReview']['sourceSemanticSha256']='invalid'
            with self.subTest(change=change):
                repaired=pipeline.reconcile_visual_checkpoint(cp,state)
                self.assertEqual(repaired['_committedRevision'],0)


if __name__=='__main__':unittest.main()
