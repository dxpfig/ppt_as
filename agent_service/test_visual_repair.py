"""Visual repairs may alter presentation styling, never the source meaning."""
import asyncio
import copy
import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from agent_service.test_pptd_generation import bundle, request


def source_deck():
    source = bundle("Revenue 12.5% in 2026; $x^2+y^2=z^2$")
    page = source["pages"][0]["data"]
    page.update(notes="Speaker note 42", animations=[{"elementId": "title", "effect": "appear"}])
    page["elements"] += [
        {"elementId": "chart", "elementType": "chart", "bounds": [50, 170, 400, 220],
         "data": {"cols": ["Quarter", "Revenue"], "rows": [["Q1", 12.5], ["Q2", 42]]},
         "series": [{"type": "bar", "encode": {"x": "Quarter", "y": "Revenue"}}]},
        {"elementId": "table", "elementType": "table", "bounds": [500, 170, 350, 200],
         "columnWidths": [.5, .5], "rowHeights": [1], "rows": [[{"text": "A"}, {"text": "42%"}]]},
        {"elementId": "image", "elementType": "image", "bounds": [500, 400, 100, 80], "src": "media/original.png"},
        {"elementId": "decoration", "elementType": "shape", "shapeName": "rect", "bounds": [10, 10, 930, 2],
         "fill": {"type": "solid", "color": "#FFFFFF"}},
    ]
    other = copy.deepcopy(source["pages"][0]); other["path"] = "pages/two.page"
    source["pages"].append(other); source["document"]["pages"].append(other["path"])
    return source


def issue(**kwargs):
    return {"pagePath": "pages/one.page", "pageIndex": 0, "elementIds": ["title"],
            "severity": "error", "code": "UnreadableText", "message": "Title overlaps chart", 
            "evidence": "Last title line touches chart labels", "repair": "Move chart down", **kwargs}


def operation(path="/bounds", value=None, **kwargs):
    return {"pagePath": "pages/one.page", "elementId": "title", "op": "set", "path": path,
            "value": [50, 50, 850, 100] if value is None else value, **kwargs}


class VisualPatchSafety(unittest.TestCase):
    def test_chart_label_layout_preserves_data_and_prevents_hiding_categories(self):
        from agent_service.visual_repair import apply_visual_patches, semantic_payload
        source = source_deck()
        operations = [operation(path, value, elementId='chart') for path, value in [
            ('/xAxis/label/rotation', 25), ('/xAxis/label/interval', 0),
            ('/dataLabels/position', 'outsideEnd'), ('/dataLabels/fontSize', 13)]]
        result = apply_visual_patches(source, operations, [issue(elementIds=['chart'])])
        self.assertEqual(semantic_payload(source), semantic_payload(result))
        self.assertEqual(result['pages'][0]['data']['elements'][1]['data'], source['pages'][0]['data']['elements'][1]['data'])
        for path, value in [('/xAxis/label/interval', 2), ('/xAxis/label/rotation', 120),
                            ('/dataLabels/position', 'hidden'), ('/dataLabels/show', False)]:
            with self.subTest(path=path), self.assertRaises(ValueError):
                apply_visual_patches(source, [operation(path, value, elementId='chart')], [issue()])

    def test_marker_keeps_original_heading_attachment_when_ai_moves_only_text(self):
        from agent_service.visual_geometry import complete_geometry
        source = bundle()
        source['pages'][0]['data']['elements'] = [
            {'elementId': 'heading', 'elementType': 'text', 'bounds': [104, 454, 178, 24],
             'content': {'text': '第四项', 'fontSize': 17}},
            {'elementId': 'body', 'elementType': 'text', 'bounds': [104, 480, 178, 50],
             'content': {'text': '完整正文', 'fontSize': 15}},
            {'elementId': 'bullet', 'elementType': 'shape', 'shapeName': 'ellipse', 'bounds': [80, 466, 12, 12]}]
        proposed = copy.deepcopy(source)
        proposed['pages'][0]['data']['elements'][0]['bounds'][1] = 380
        proposed['pages'][0]['data']['elements'][1]['bounds'][1] = 410
        result = complete_geometry(proposed, source, {}, {'pages/one.page'})
        heading, _, bullet = result['pages'][0]['data']['elements']
        self.assertEqual(bullet['bounds'][1] - heading['bounds'][1], 12)

    def test_aligned_different_widths_stay_in_same_column(self):
        from agent_service.visual_geometry import complete_geometry
        source = bundle()
        source['pages'][0]['data']['elements'] = [
            {'elementId': 'body', 'elementType': 'text', 'bounds': [600, 198, 300, 178], 'content': {'text': '正文'}},
            {'elementId': 'label', 'elementType': 'text', 'bounds': [600, 292, 210, 24], 'content': {'text': '标签'}}]
        result = complete_geometry(source, source, {}, {'pages/one.page'})
        body, label = result['pages'][0]['data']['elements']
        self.assertEqual(body['bounds'][0], label['bounds'][0])
        self.assertGreaterEqual(label['bounds'][1], body['bounds'][1] + body['bounds'][3])

    def test_measured_geometry_completion_preserves_text_and_separates_neighbors(self):
        from agent_service.visual_geometry import complete_geometry
        from agent_service.visual_repair import semantic_payload
        source = bundle()
        source['pages'][0]['data']['elements'] = [
            {'elementId': name, 'elementType': 'text', 'bounds': bounds,
             'content': {'text': name, 'fontSize': 16}}
            for name, bounds in [('heading', [50, 50, 300, 30]),
                                 ('body', [50, 80, 300, 20]),
                                 ('footer', [50, 110, 850, 24])]]
        layout = {'pages/one.page': {'elements': [
            {'elementId': e['elementId'], 'measuredTextSize': [e['bounds'][2], h], 'paddingSize': [0, 0]}
            for e, h in zip(source['pages'][0]['data']['elements'], [27, 90, 22])]}}
        original = copy.deepcopy(source)
        result = complete_geometry(source, source, layout, {'pages/one.page'})
        self.assertEqual(source, original)
        self.assertEqual(semantic_payload(source), semantic_payload(result))
        a, b, c = result['pages'][0]['data']['elements']
        self.assertGreaterEqual(b['bounds'][3], 90)
        self.assertLessEqual(a['bounds'][1] + a['bounds'][3], b['bounds'][1])
        self.assertLessEqual(b['bounds'][1] + b['bounds'][3], c['bounds'][1])

    def test_geometry_never_reuses_measurements_after_width_or_typography_change(self):
        from agent_service.visual_geometry import complete_geometry
        source = bundle()
        original = copy.deepcopy(source)
        element = source['pages'][0]['data']['elements'][0]
        element['bounds'][2] /= 2
        element['content']['fontSize'] = 36
        result = complete_geometry(source, original, {'pages/one.page': {'elements': [
            {'elementId': element['elementId'], 'measuredTextSize': [800, 400], 'paddingSize': [0, 0]}]}}, {'pages/one.page'})
        self.assertEqual(result['pages'][0]['data']['elements'][0]['bounds'][3], element['bounds'][3])

    def test_impossible_measured_layout_is_rejected_without_shrinking_text(self):
        from agent_service.visual_geometry import complete_geometry
        source = bundle()
        element = source['pages'][0]['data']['elements'][0]
        with self.assertRaisesRegex(ValueError, 'fit|空间'):
            complete_geometry(source, source, {'pages/one.page': {'elements': [
                {'elementId': element['elementId'], 'measuredTextSize': [800, 900], 'paddingSize': [0, 0]}]}}, {'pages/one.page'})

    def test_equivalent_line_coordinate_units_preserve_geometry_and_meaning(self):
        from agent_service.visual_repair import apply_visual_patches, semantic_payload
        source = source_deck()
        source['pages'][0]['data']['elements'].append({'elementId': 'connector', 'elementType': 'line',
            'bounds': [100, 200, 240, 44], 'viewBox': [378, 144], 'points': '0,144 378,0', 'curve': 'sharp'})
        ops = [operation('/viewBox', [240, 44], elementId='connector'),
               operation('/points', '0,44 240,0', elementId='connector')]
        fixed = apply_visual_patches(source, ops, [issue()])
        self.assertEqual(semantic_payload(source), semantic_payload(fixed))
        with self.assertRaises(ValueError):
            apply_visual_patches(source, [ops[0], operation('/points', '0,0 240,44', elementId='connector')], [issue()])

    def test_unchanged_wrapping_must_respect_measured_text_height(self):
        from agent_service.visual_repair import apply_visual_patches
        source = source_deck()
        layout = {'pages/one.page': {'elements': [{'elementId': 'title',
            'measuredTextSize': [800, 80], 'paddingSize': [0, 0]}]}}
        with self.assertRaisesRegex(ValueError, '80'):
            apply_visual_patches(source, [operation('/bounds/3', 22)], [issue()], layout_measurements=layout)
    def test_rejects_text_repair_outside_slide_before_saving_candidate(self):
        with self.assertRaisesRegex(ValueError, 'canvas|画布'):
            self.apply([operation('/bounds', [104, 616, 230, 34])])

    def apply(self, operations, issues=None):
        from agent_service.visual_repair import apply_visual_patches
        source = source_deck(); original = copy.deepcopy(source)
        result = apply_visual_patches(source, operations, [issue()] if issues is None else issues)
        self.assertEqual(source, original)
        return source, result

    def test_changes_layout_and_styles_preserving_every_semantic_field(self):
        before, after = self.apply([
            operation(), operation("/content/fontSize", 30),
            operation("/bounds/1", 200, elementId="chart"),
            operation("/rows/0/1/fontSize", 24, elementId="table"),
            operation("/fill/color", "#222222", elementId="decoration"),
        ])
        self.assertEqual(before["document"], after["document"])
        self.assertEqual(before["pages"][1], after["pages"][1])
        for key in ("notes", "animations"):
            self.assertEqual(before["pages"][0]["data"][key], after["pages"][0]["data"][key])
        a, b = before["pages"][0]["data"]["elements"], after["pages"][0]["data"]["elements"]
        self.assertEqual(a[0]["content"]["text"], b[0]["content"]["text"])
        self.assertEqual(a[0]["futureField"], b[0]["futureField"])
        self.assertEqual(a[1]["data"], b[1]["data"])
        self.assertEqual(a[1]["series"], b[1]["series"])
        self.assertEqual(a[2]["rows"][0][1]["text"], b[2]["rows"][0][1]["text"])
        self.assertEqual(a[3]["src"], b[3]["src"])
        self.assertEqual(b[0]["bounds"], [50, 50, 850, 100])

    def test_rejects_text_numbers_formula_chart_data_assets_notes_and_animation_edits(self):
        cases = [
            operation("/content/text", "Revenue 13%"), operation("/content", {"text": "Lost formula"}),
            operation("/data/rows/0/1", 999, elementId="chart"),
            operation("/series/0/encode/y", "Wrong", elementId="chart"),
            operation("/rows/0/1/text", "41%", elementId="table"),
            operation("/src", "media/new.png", elementId="image"),
            operation("/notes", "new notes", elementId=None),
            operation("/animations", [], elementId=None),
            operation("/elementId", "new"), operation("/elementType", "shape"),
            operation("/futureField/keep", False),
            operation("/fill", {"type": "solid", "color": "#FFFFFF", "text": "smuggled"}, elementId="decoration"),
        ]
        for patch_item in cases:
            with self.subTest(patch=patch_item), self.assertRaises(ValueError): self.apply([patch_item])

    def test_rejects_unknown_ids_unlocated_pages_removal_and_empty_repairs(self):
        cases = [[operation(elementId="missing")], [operation(pagePath="pages/two.page")],
                 [operation(op="remove-element")], [operation(op="append-element")], [],
                 [operation(path="/bounds", value=[0, 0, 0, 0])],
                 [operation(path="/content/fontSize", value=1)],
                 [operation(path="/content/fontSize", value={"text": "hidden"})]]
        for operations in cases:
            with self.subTest(operations=operations), self.assertRaises(ValueError): self.apply(operations)
        for bad_issue in (issue(pagePath="missing"), issue(pageIndex=1), issue(pagePath=None, pageIndex=None), issue(elementIds=["missing"])):
            with self.subTest(issue=bad_issue), self.assertRaises(ValueError): self.apply([operation()], [bad_issue])

    def test_rejects_background_asset_replacement_and_semantic_table_restructure(self):
        from agent_service.visual_repair import apply_visual_patches
        source = source_deck(); source["pages"][0]["data"]["background"] = {"type": "image", "src": "media/bg.png"}
        for patch_item in (operation("/background", {"type": "solid", "color": "#FFFFFF"}, elementId=None),
                           operation("/columnWidths", [1], elementId="table")):
            with self.assertRaises(ValueError): apply_visual_patches(source, [patch_item], [issue()])

    def test_only_page_index_location_is_resolved_to_real_path(self):
        before, after = self.apply([operation()], [issue(pagePath=None)])
        self.assertNotEqual(before["pages"][0], after["pages"][0])

    def test_real_compound_border_repair_matches_equivalent_leaf_operations(self):
        from agent_service.visual_repair import apply_visual_patches, semantic_payload
        source = bundle()
        source["document"]["pages"] = ["pages/7_blueprint.page", "pages/13_budget.page", "pages/15_final.page"]
        source["document"]["theme"]["colors"].update(bg="#F7F3E8", primary="#1B2A4A", rule="#D9D2BF")
        source["pages"] = [
            {"path": "pages/7_blueprint.page", "data": {"elements": [
                {"elementId": f"pillar{i}-box", "elementType": "shape", "shapeName": "rect", "bounds": [520, 220, 180, 110],
                 "fill": {"type": "solid", "color": "$bg"}, "border": {"style": "solid", "width": 1, "color": "$primary"}} for i in range(1, 5)]}},
            {"path": "pages/13_budget.page", "data": {"elements": [
                {"elementId": "recovered-staffing", "elementType": "text", "bounds": [60, 474, 840, 46], "content": {"text": "预算人员安排（数字占位，待补充）。", "fontSize": 16}}]}},
            {"path": "pages/15_final.page", "data": {"elements": [
                {"elementId": "title-rule", "elementType": "line", "bounds": [60, 402, 840, 1], "viewBox": [1, 1], "points": "0,0 1,0", "border": {"style": "solid", "width": 1, "color": "$rule"}}]}},
        ]
        compound = []
        for index in range(1, 5):
            compound += [operation("/fill", {"type": "solid", "color": "#00000000"}, pagePath="pages/7_blueprint.page", elementId=f"pillar{index}-box"),
                         operation("/border", {"style": "solid", "width": 0, "color": "#00000000"}, pagePath="pages/7_blueprint.page", elementId=f"pillar{index}-box")]
        compound += [operation("/content/fontSize", 14, pagePath="pages/13_budget.page", elementId="recovered-staffing"),
                     operation("/bounds/1", 468, pagePath="pages/13_budget.page", elementId="recovered-staffing"),
                     operation("/bounds/1", 158, pagePath="pages/15_final.page", elementId="title-rule"),
                     operation("/bounds/2", 80, pagePath="pages/15_final.page", elementId="title-rule"),
                     operation("/border/width", 2, pagePath="pages/15_final.page", elementId="title-rule"),
                     operation("/border/color", "#B08D3E", pagePath="pages/15_final.page", elementId="title-rule")]
        leaves = []
        for item in compound:
            if item["path"] == "/border":
                leaves.extend({**item, "path": "/border/" + key, "value": value} for key, value in item["value"].items())
            else: leaves.append(item)
        findings = [issue(pagePath=page["path"], pageIndex=index, elementIds=[]) for index, page in enumerate(source["pages"])]
        self.assertEqual(len(compound), 14); self.assertEqual(len(leaves), 22)
        actual = apply_visual_patches(source, compound, findings)
        self.assertEqual(actual, apply_visual_patches(source, leaves, findings))
        self.assertEqual(semantic_payload(source), semantic_payload(actual))

    def test_compound_border_merges_styles_without_replacing_unknown_fields(self):
        from agent_service.visual_repair import apply_visual_patches
        source = source_deck()
        source["pages"][0]["data"]["elements"][-1]["border"] = {"style": "dash", "width": 3, "color": "#112233", "future": {"retain": [1, 2, 3]}}
        original = copy.deepcopy(source)
        result = apply_visual_patches(source, [operation("/border", {"width": 0}, elementId="decoration")], [issue()])
        self.assertEqual(result["pages"][0]["data"]["elements"][-1]["border"], {"style": "dash", "width": 0, "color": "#112233", "future": {"retain": [1, 2, 3]}})
        self.assertEqual(source, original)

    def test_compound_border_rejects_null_empty_unknown_or_invalid_styles(self):
        values = [None, {}, {"width": 0, "future": {"replace": True}}, {"width": -1}, {"width": True}, {"color": "transparent"}, {"style": "unsupported"}]
        for value in values:
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.apply([{**operation("/border", elementId="decoration"), "value": value}])
        with self.assertRaises(ValueError): self.apply([operation("/border", {"width": 0})])


class VisualRepairWorkflow(unittest.IsolatedAsyncioTestCase):
    async def test_rejections_are_audited_and_have_actionable_public_diagnostics(self):
        from agent_service import visual_repair as repair
        from agent_service.job_execution import diagnose_error
        audits = []
        async def close(): pass
        async def ask(*args, **kwargs):
            return {'operations': [operation('/content/text', 'forbidden replacement')]}
        with patch.object(repair, 'make_model', return_value=SimpleNamespace(client=SimpleNamespace(close=close))), patch.object(repair, '_ask', side_effect=ask):
            with self.assertRaises(repair.VisualRepairError) as caught:
                await repair.repair_deck(request(), source_deck(), [issue()], on_audit=audits.append)
        self.assertEqual(len(audits), 2)
        self.assertEqual(audits[-1]['operations'][0]['path'], '/content/text')
        self.assertIn('/content/text', audits[-1]['reason'])
        _, diagnostic, _ = diagnose_error(caught.exception)
        self.assertTrue(any('/content/text' in i['message'] for i in diagnostic['issues']))
        self.assertNotIn('forbidden replacement', json.dumps(diagnostic))

    async def test_changed_wrapping_is_remeasured_and_checked_before_acceptance(self):
        from agent_service import visual_repair as repair
        from agent_service.pptd_exports import ValidationError
        source = bundle(); validations = []; calls = []
        source['pages'][0]['data']['elements'] = [source['pages'][0]['data']['elements'][0]]
        async def close(): pass
        async def ask(*args, **kwargs):
            calls.append(True)
            return {'operations': [operation('/bounds', [50, 50, 300, 40])]}
        async def validate(candidate):
            validations.append(copy.deepcopy(candidate))
            if len(validations) == 1:
                raise ValidationError([issue(code='text-overflow')], layouts={'pages/one.page': {'elements': [
                    {'elementId': 'title', 'measuredTextSize': [300, 120], 'paddingSize': [0, 0]}]}})
        initial = {'pages/one.page': {'elements': [
            {'elementId': 'title', 'measuredTextSize': [850, 30], 'paddingSize': [0, 0]}]}}
        with patch.object(repair, 'make_model', return_value=SimpleNamespace(client=SimpleNamespace(close=close))), \
             patch.object(repair, '_ask', side_effect=ask):
            result = await repair.repair_deck(request(), source, [issue(code='text-overflow')],
                layout_measurements=initial, validate_candidate=validate)
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(validations), 2)
        self.assertGreaterEqual(result['pages'][0]['data']['elements'][0]['bounds'][3], 120)
        self.assertEqual(repair.semantic_payload(result), repair.semantic_payload(source))

    async def test_render_rejected_candidate_is_not_accepted_and_pixels_reach_retry(self):
        from agent_service import visual_repair as repair
        from agent_service.pptd_exports import ValidationError
        from agent_service.pptd_generation import _message_content
        source = source_deck(); original = copy.deepcopy(source); calls = []
        async def close(): pass
        async def ask(*args, **kwargs):
            calls.append(copy.deepcopy(args[3]))
            return {'operations': [operation('/bounds/3', 40 if len(calls) == 1 else 140)]}
        async def validate(candidate):
            if candidate['pages'][0]['data']['elements'][0]['bounds'][3] == 40:
                raise ValidationError([issue(code='text-overflow', message='Need height 140')],
                                      previews={'pages/one.page': b'png-candidate'})
        with patch.object(repair, 'make_model', return_value=SimpleNamespace(client=SimpleNamespace(close=close))), \
             patch.object(repair, '_ask', side_effect=ask):
            result = await repair.repair_deck(request(), source, [issue()],
                rendered_pages={'pages/one.page': b'png-original'}, validate_candidate=validate)
        self.assertEqual(source, original)
        self.assertEqual(result['pages'][0]['data']['elements'][0]['bounds'][3], 140)
        self.assertEqual(calls[1]['rejectedCandidate']['issues'][0]['message'], 'Need height 140')
        self.assertEqual(calls[1]['pages'], calls[0]['pages'])
        self.assertTrue(any(block['type'] == 'data' for block in _message_content(calls[0])))

    async def test_real_ask_wire_contract_explains_canonical_element_relative_pointers(self):
        from agentscope.message import Msg
        from agent_service import visual_repair as repair, pptd_generation as generation
        captured={}
        async def close():pass
        class Agent:
            async def reply(self,message):
                captured['request']=json.loads(message.get_text_content())
                return Msg(name='repair',role='assistant',content=[{'type':'text','text':json.dumps({'operations':[operation('/content/color','#223344')]})}])
        def agent(name,system,model,session):
            captured['system']=system
            return Agent()
        with patch.object(repair,'make_model',return_value=SimpleNamespace(client=SimpleNamespace(close=close))),patch.object(generation,'make_agent',side_effect=agent):
            result=await repair.repair_deck(request(),source_deck(),[issue()])
        schema=repair.VisualPatch.model_json_schema()
        self.assertIn('pattern',schema['properties']['path'])
        self.assertIn('/content/color',schema['properties']['path']['description'])
        self.assertIn('/content/color',captured['system'])
        contract=captured['request']['patchContract']
        example=contract['example']['operations'][0]
        self.assertEqual(example['path'],'/content/color')
        self.assertEqual(example['pagePath'],'pages/one.page')
        self.assertEqual(example['elementId'],'title')
        self.assertTrue(all(p.startswith('/') for p in captured['request']['allowedPaths']))
        self.assertEqual(result['pages'][0]['data']['elements'][0]['content']['color'],'#223344')

    async def test_unsafe_answer_is_retried_from_original_and_preservation_verified(self):
        from agent_service import visual_repair as repair
        closed = []; calls = []; events = []
        async def close(): closed.append(True)
        async def ask(*args, **kwargs):
            calls.append((args, kwargs))
            if len(calls) == 2:
                self.assertEqual(args[3]['rejectedCandidate']['operations'], [operation('/content/text', 'Lost text')])
            return {"operations": [operation("/content/text", "Lost text")] if len(calls) == 1 else [operation()]}
        model = SimpleNamespace(client=SimpleNamespace(close=close))
        source = source_deck(); original = copy.deepcopy(source)
        with patch.object(repair, "make_model", return_value=model), patch.object(repair, "_ask", side_effect=ask):
            result = await repair.repair_deck(request(), source, [issue()], on_event=events.append)
        self.assertEqual(source, original)
        self.assertEqual(len(calls), 2); self.assertEqual(closed, [True])
        self.assertEqual(set(result), {"document", "pages"})
        self.assertEqual(result["pages"][0]["data"]["elements"][0]["content"]["text"], original["pages"][0]["data"]["elements"][0]["content"]["text"])
        self.assertTrue(any(e["status"] == "retrying" for e in events))
        self.assertTrue(all(e["phase"] == "visual" for e in events))
        self.assertTrue(all(e.get("scope") == "repair" for e in events))
        self.assertTrue(all("attempt" not in e for e in events))
        self.assertEqual(events[-1]["status"], "completed")

    async def test_nested_model_attempt_does_not_reset_outer_visual_round(self):
        from agent_service import visual_repair as repair
        events=[]
        async def close():pass
        async def ask(*args, **kwargs):
            await kwargs['on_event']({'phase':'visual','status':'retrying','attempt':2,'message':'Model format retry'})
            return {'operations':[operation()]}
        with patch.object(repair,'make_model',return_value=SimpleNamespace(client=SimpleNamespace(close=close))),patch.object(repair,'_ask',side_effect=ask):
            await repair.repair_deck(request(),source_deck(),[issue()],on_event=events.append)
        model_event=next(e for e in events if e['message']=='Model format retry')
        self.assertNotIn('attempt',model_event)
        self.assertEqual(model_event['scope'],'repair')

    async def test_persistent_unsafe_answer_fails_after_bounded_attempts(self):
        from agent_service import visual_repair as repair
        calls = []; closed = []
        async def close(): closed.append(True)
        async def ask(*args, **kwargs): calls.append(True); return {"operations": [operation("/content/text", "loss")]}
        with patch.object(repair, "make_model", return_value=SimpleNamespace(client=SimpleNamespace(close=close))), patch.object(repair, "_ask", side_effect=ask):
            with self.assertRaises(repair.VisualRepairError) as caught:
                await repair.repair_deck(request(), source_deck(), [issue()])
        self.assertLessEqual(len(calls), 3)
        self.assertEqual(closed, [True]); self.assertTrue(caught.exception.issues)

    async def test_cancellation_closes_model_and_cancels_request(self):
        from agent_service import visual_repair as repair
        started = asyncio.Event(); cancelled = []; closed = []
        async def close(): closed.append(True)
        async def ask(*args, **kwargs):
            started.set()
            try: await asyncio.Event().wait()
            finally: cancelled.append(True)
        with patch.object(repair, "make_model", return_value=SimpleNamespace(client=SimpleNamespace(close=close))), patch.object(repair, "_ask", side_effect=ask):
            task = asyncio.create_task(repair.repair_deck(request(), source_deck(), [issue()]))
            await started.wait(); task.cancel()
            with self.assertRaises(asyncio.CancelledError): await task
        self.assertEqual(cancelled, [True]); self.assertEqual(closed, [True])
