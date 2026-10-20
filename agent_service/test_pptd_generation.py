"""Contract tests for authoritative PPTD generation; no provider credentials required."""
import copy
import json
import importlib.util
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def bundle(text="Hello"):
    return {"document": {"version": "v2", "title": "Deck", "size": [960, 540], "theme": {"colors": {"ink": "#112233"}}, "pages": ["pages/one.page"]}, "pages": [{"path": "pages/one.page", "data": {"elements": [{"elementId": "title", "elementType": "text", "bounds": [50, 60, 860, 80], "content": {"text": text, "fontFamily": "思源黑体", "fontSize": 32, "color": "$ink"}, "futureField": {"keep": True}}]}}]}


def request(**kwargs):
    values = dict(kind="outline", prompt="Make a deck", pageCount=None, audience="Team", references="", selectedSlideId=None, workflow="new", designMode="autonomous", usage="reading", allowResearch=False, speakerNotes=False, animationRequested=False, designSystem="", scenario="education-training", attachments=[], templateId=None, project=SimpleNamespace(id="test-pptd", title="Deck", references="", messages=[], imageSource="none", templateId=None))
    values.update(kwargs)
    return SimpleNamespace(**values)


class ModuleContract(unittest.TestCase):
    def test_workflow_module_exists(self):
        self.assertIsNotNone(importlib.util.find_spec("agent_service.pptd_generation"), "PPTD workflow module is missing")


class ValidationContract(unittest.TestCase):
    def setUp(self):
        from agent_service.pptd_validation import validate_document
        self.validate = validate_document

    def test_source_is_not_mutated(self):
        source = bundle(); before = copy.deepcopy(source)
        self.assertEqual(self.validate(source), [])
        self.assertEqual(source, before)

    def test_rejects_path_escape_unknown_theme_duplicate_ids(self):
        source = bundle(); source["document"]["pages"] = ["../one.page"]
        self.assertTrue(any(i["code"] == "UnsafePath" for i in self.validate(source)))
        source = bundle(); e = source["pages"][0]["data"]["elements"][0]
        e["content"]["color"] = "$missing"
        source["pages"][0]["data"]["elements"].append(copy.deepcopy(e))
        codes = {i["code"] for i in self.validate(source)}
        self.assertTrue({"InvalidTheme", "DuplicateId"} <= codes)

    def test_notes_and_animation_policy_does_not_remove_existing(self):
        source = bundle(); source["pages"][0]["data"]["notes"] = "Existing note"
        self.assertTrue(any(i["code"] == "UnrequestedNotes" for i in self.validate(source, speaker_notes=False)))
        self.assertEqual(self.validate(source, speaker_notes=False, existing=source), [])

    def test_malformed_model_data_becomes_actionable_issues(self):
        mutations=[
            lambda b:b["document"].update(theme={"colors":None}),
            lambda b:b["pages"][0]["data"]["elements"][0].update(elementType=[]),
            lambda b:b["pages"][0]["data"].update(animations=[{"elementId":[],"effect":[]}]),
            lambda b:b["pages"][0]["data"]["elements"][0]["content"].update(color=[]),
        ]
        for mutate in mutations:
            source=bundle();mutate(source)
            with self.subTest(source=source): self.assertTrue(self.validate(source))

    def test_table_merges_are_validated_without_flattening(self):
        source=bundle(); source["pages"][0]["data"]["elements"]=[{"elementId":"table","elementType":"table","bounds":[40,40,800,300],"columnWidths":[.5,.5],"rowHeights":[.5,.5],"rows":[[{"text":"merge","rowSpan":2,"colSpan":2}],[]]}]
        self.assertEqual(self.validate(source), [])
        source["pages"][0]["data"]["elements"][0]["rows"][1]=[{"text":"invalid"}]
        self.assertTrue(any(i["code"]=="InvalidTable" for i in self.validate(source)))


class RulesContract(unittest.TestCase):
    def test_complete_archives_are_injected(self):
        from agent_service.kimi_rules import KimiRules
        rules=KimiRules(); prompt=rules.system("page", "academic-research")
        for relative in rules.files("academic-research"):
            self.assertIn((rules.root / relative).read_bytes().decode("utf-8"), prompt)
        self.assertIn("local PPTD", prompt)
        self.assertEqual(len(rules.inventory()), 13)


class ProviderJSONContract(unittest.IsolatedAsyncioTestCase):
    async def test_agentscope_free_text_preserves_pptd_json_types(self):
        from agentscope.message import Msg
        from agent_service import pptd_generation as generation
        from agent_service.kimi_rules import KimiRules
        payload={"data":{"elements":[{"elementId":"t","elementType":"text","bounds":[1,2,300,40],"content":{"text":"Literal [1,2]","fontSize":24,"bold":True}}]}}
        class Agent:
            async def reply(self, msg):
                return Msg(name="assistant",role="assistant",content=[{"type":"text","text":json.dumps(payload)}])
        with patch.object(generation,"make_agent",return_value=Agent()):
            result=await generation._ask(object(),"page",KimiRules(),{"pagePath":"pages/a.page"})
        self.assertEqual(result,payload)
        self.assertIs(result["data"]["elements"][0]["content"]["bold"],True)
        self.assertIsInstance(result["data"]["elements"][0]["bounds"],list)

    def test_strict_json_rejects_duplicate_keys_nonfinite_and_prose(self):
        from agent_service.pptd_generation import _parse_json_response
        for text in ('{"data":{},"data":{}}','{"value":NaN}','Here is JSON: {"data":{}}'):
            with self.subTest(text=text),self.assertRaises(ValueError): _parse_json_response(text)
        self.assertEqual(_parse_json_response('```json\n{"data":{}}\n```'),{"data":{}})

    def test_minimax_reasoning_envelope_is_separate_from_strict_json_payload(self):
        from agent_service.pptd_generation import _parse_json_response
        payload={"data":{"elements":[],"content":{"text":"Literal <think> remains inside JSON"}}}
        response="<think>Provider reasoning</think>\n"+json.dumps(payload)
        with self.assertRaises(ValueError): _parse_json_response(response)
        self.assertEqual(_parse_json_response(response,reasoning_prefix=True),payload)
        with self.assertRaises(ValueError): _parse_json_response("<think>Unclosed",reasoning_prefix=True)
        with self.assertRaises(ValueError): _parse_json_response("Explanation "+json.dumps(payload),reasoning_prefix=True)

    async def test_invalid_transport_retries_without_guessing_or_coercing_values(self):
        from agentscope.message import Msg
        from agent_service import pptd_generation as generation
        from agent_service.kimi_rules import KimiRules
        responses=['<data><elements><item>bad</item></elements></data>',json.dumps({"data":{"elements":{"item":[]},"size":"[960,540]"}})]
        class Agent:
            async def reply(self,msg): return Msg(name="assistant",role="assistant",content=[{"type":"text","text":responses.pop(0)}])
        with patch.object(generation,"make_agent",return_value=Agent()):
            result=await generation._ask(object(),"page",KimiRules(),{})
        self.assertEqual(result["data"]["elements"],{"item":[]})
        self.assertEqual(result["data"]["size"],"[960,540]")


class GenerationContract(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        from agent_service import pptd_generation
        self.generation = pptd_generation
        self.closed = False
        async def close(): self.closed=True
        self.model = SimpleNamespace(client=SimpleNamespace(close=close))
        self.plan={"title":"Deck", "narrative":"Explain", "scenario":"education-training", "inputType":"topic", "document":bundle()["document"], "pages":[{"title":"Hello", "readerTask":"Understand", "content":"Hello", "visualPlan":"Typography", "imageQueries":[]}]}

    async def run_draft(self, responses, **kwargs):
        names=[]
        async def ask(role, data, scenario="auto"):
            names.append(role)
            response=responses.pop(0)
            if isinstance(response, Exception): raise response
            return copy.deepcopy(response)
        async def provider(*a, **k):
            return await ask(a[1], a[3], k.get("scenario", "auto"))
        with patch.object(self.generation, "make_model", return_value=self.model), patch.object(self.generation, "_ask", side_effect=provider):
            result=await self.generation.generate_pptd(request(**kwargs), lambda *a: None)
        self.assertTrue(self.closed)
        return result,names

    async def test_auto_page_count_and_unmodified_seven_element_document(self):
        result,names=await self.run_draft([self.plan,{"data":bundle()["pages"][0]["data"]}])
        self.assertEqual(result["pages"], bundle()["pages"])
        self.assertEqual(result["generationMeta"]["validation"]["status"], "structure-passed")
        self.assertTrue(result["generationMeta"]["validation"]["renderRequired"])
        self.assertEqual(names,["plan","page"])

    async def test_plan_without_table_styles_still_resolves_a_table_reference(self):
        plan=copy.deepcopy(self.plan)
        plan["document"]["theme"]={"colors":{"ink":"#112233","primaryTint":"#F0B4B8","muted":"#6B7280"}}
        page=copy.deepcopy(bundle()["pages"][0]["data"])
        page["elements"].append({"elementId":"routes","elementType":"table","bounds":[40,180,880,300],
                                 "style":"$default","columnWidths":[0.5,0.5],"rowHeights":[0.5,0.5],
                                 "rows":[[{"text":"维度"},{"text":"方案 A"}],[{"text":"算力"},{"text":"16 卡"}]]})
        result,_=await self.run_draft([plan,{"data":page}])
        theme=result["document"]["theme"]
        self.assertIn("default",theme["tableStyles"])
        self.assertEqual(theme["tableStyles"]["default"]["firstRowStyle"]["fill"]["color"],"#F0B4B8")
        self.assertEqual(self.generation.validate_document(result),[])

    async def test_company_template_is_reference_material_not_the_deck(self):
        shell_element=copy.deepcopy(bundle()["pages"][0]["data"]["elements"][0])
        shell_element["content"]["text"]="模板标题"
        shell={"elements":[shell_element]}
        reference={"summary":{"kind":"company-template-reference","pageCount":19,
                              "pages":[{"path":"pages/01.page","sourceId":"template-page-01"}]},
                   "sources":{"template-page-01":json.dumps(shell)},
                   "media":[{"path":"media/image-1.png","sourcePath":"/templates/corp/reference/media/image-1.png","template":True}]}
        template_plan=copy.deepcopy(self.plan)
        template_plan["pages"][0]["templatePage"]="template-page-01"
        seen={}
        async def ask(role,data,scenario="auto"):
            seen.setdefault(role,{key:value for key,value in data.items() if key!="_contextSources"})
            seen.setdefault("sources",dict(data.get("_contextSources") or {}))
            return copy.deepcopy(template_plan) if role=="plan" else {"data":bundle()["pages"][0]["data"]}
        async def provider(*a,**k): return await ask(a[1],a[3],k.get("scenario","auto"))
        with patch.object(self.generation,"make_model",return_value=self.model),patch.object(self.generation,"_ask",side_effect=provider),patch.object(self.generation.template_reference,"load",return_value=reference):
            result=await self.generation.generate_pptd(request(designMode="template",templateId="corp"),lambda *a:None)
        self.assertEqual(seen["plan"]["templateReference"]["pageCount"],19)
        self.assertNotIn("existingDocument",seen["plan"])
        self.assertIn("template-page-01",seen["sources"])
        # The writer receives the exact shell it must reproduce.
        self.assertEqual(seen["page"]["templatePage"]["elements"][0]["elementId"],"title")
        # The generated deck is the model's own pages, not the template's pages.
        self.assertEqual(result["pages"],bundle()["pages"])
        # Template artwork travels with the deck so the reused shell keeps its images.
        self.assertIn("media/image-1.png",[item["path"] for item in result["assets"]])
        self.assertTrue(self.closed)

    def test_template_shell_is_reimposed_over_model_output(self):
        shell={"background":{"type":"solid","color":"#101010"},
               "elements":[{"elementId":"logo","elementType":"image","bounds":[10,10,40,40],"src":"media/logo.png","fit":{"mode":"fill"}},
                           {"elementId":"title","elementType":"text","bounds":[50,60,860,80],
                            "content":{"text":'<p><strong><span style="font-family:微软雅黑;font-size:30.0px;color:#E50011">模板标题</span></strong></p>'}}]}
        noisy={"elements":[{"elementId":"title","elementType":"text","bounds":[0,0,10,10],
                            "content":{"text":'<p><span style="font-family:宋体;font-size:12px">新标题</span></p>'}},
                           {"elementId":"extra","elementType":"text","bounds":[1,2,3,4],"content":{"text":"补充说明"}}]}
        merged=self.generation.apply_template_shell(noisy,shell)
        self.assertEqual([element["elementId"] for element in merged["elements"]],["logo","title","extra"])
        self.assertEqual(merged["background"],shell["background"])
        title=merged["elements"][1]
        self.assertEqual(title["bounds"],[50,60,860,80])
        self.assertIn("微软雅黑",title["content"]["text"])
        self.assertIn("<strong>",title["content"]["text"])
        self.assertIn("新标题",title["content"]["text"])
        self.assertNotIn("模板标题",title["content"]["text"])
        self.assertEqual(self.generation.template_binding_issues(merged,shell,"pages/one.page",540),[])

    def test_template_shell_never_inherits_template_speaker_notes(self):
        shell={"notes":"模板作者的备注","elements":[{"elementId":"title","elementType":"text","bounds":[50,60,860,80],
                                                       "content":{"text":"模板标题"}}]}
        merged=self.generation.apply_template_shell({"elements":[{"elementId":"title","elementType":"text",
                                                                  "bounds":[0,0,10,10],"content":{"text":"新标题"}}]},shell)
        self.assertNotIn("notes",merged)
        authored=self.generation.apply_template_shell({"notes":"本页备注","elements":[{"elementId":"title","elementType":"text",
                                                                                     "bounds":[0,0,10,10],"content":{"text":"新标题"}}]},shell)
        self.assertEqual(authored["notes"],"本页备注")

    def test_template_binding_reports_a_missing_shell_element(self):
        shell={"background":{"type":"solid","color":"#101010"},
               "elements":[{"elementId":"logo","elementType":"image","bounds":[10,10,40,40],"src":"media/logo.png","fit":{"mode":"fill"}},
                           {"elementId":"title","elementType":"text","bounds":[50,60,860,80],
                            "content":{"text":"模板标题","fontFamily":"思源黑体","fontSize":32,"color":"#112233"}}]}
        page={"elements":[{"elementId":"title","elementType":"text","bounds":[50,60,860,80],
                           "content":{"text":"新标题","fontFamily":"思源黑体","fontSize":32,"color":"#112233"}}]}
        codes={issue["code"] for issue in self.generation.template_binding_issues(page,shell,"pages/one.page",540)}
        self.assertIn("TemplateElementDropped",codes)
        self.assertIn("TemplateBackgroundChanged",codes)
        # Keeping the shell but changing only the text is the accepted contract.
        bound={"background":shell["background"],"elements":[shell["elements"][0],page["elements"][0]]}
        self.assertEqual(self.generation.template_binding_issues(bound,shell,"pages/one.page",540),[])
        # A left-over template headline is still a failure.
        stale={"background":shell["background"],"elements":[shell["elements"][0],copy.deepcopy(shell["elements"][1])]}
        self.assertIn("TemplateTextNotReplaced",
                      {issue["code"] for issue in self.generation.template_binding_issues(stale,shell,"pages/one.page",540)})

    def test_template_charter_is_added_only_in_template_mode(self):
        from agent_service.kimi_rules import KimiRules
        rules=KimiRules()
        self.assertIn("TEMPLATE MODE CHARTER",rules.system("page","management-report",template=True))
        self.assertNotIn("TEMPLATE MODE CHARTER",rules.system("page","management-report"))

    def test_deck_opens_and_closes_on_the_template_cover_and_closing(self):
        reference={"pages":[{"sourceId":"template-page-01","archetype":"cover"},
                            {"sourceId":"template-page-05","archetype":"content"},
                            {"sourceId":"template-page-19","archetype":"closing"}]}
        plan={"pages":[{"templatePage":"template-page-05"},{"templatePage":"template-page-05"},{"templatePage":"template-page-05"}]}
        self.assertTrue(self.generation.bind_template_archetypes(plan,reference))
        self.assertEqual([page["templatePage"] for page in plan["pages"]],
                         ["template-page-01","template-page-05","template-page-19"])
        self.assertFalse(self.generation.bind_template_archetypes(plan,reference))

    def test_rewritten_template_text_must_keep_the_template_typography(self):
        shell={"elements":[{"elementId":"title","elementType":"text","bounds":[50,60,860,80],
                            "content":{"text":'<p><span style="font-family:微软雅黑;font-size:30.0px;color:#E50011">模板标题</span></p>'}}]}
        page={"elements":[{"elementId":"title","elementType":"text","bounds":[50,60,860,80],
                           "content":{"text":'<p><span style="font-family:宋体;font-size:18.0px;color:#000000">新标题</span></p>'}}]}
        codes={issue["code"] for issue in self.generation.template_binding_issues(page,shell,"pages/one.page",540)}
        self.assertIn("TemplateTextFontChanged",codes)
        page["elements"][0]["content"]["text"]='<p><span style="font-family:微软雅黑;font-size:30.0px;color:#E50011">新标题</span></p>'
        self.assertEqual(self.generation.template_binding_issues(page,shell,"pages/one.page",540),[])

    def test_narrow_body_slot_accepts_a_modest_wrap_safe_expansion(self):
        original = "模" * 44
        shell = {"elements": [{
            "elementId": "body",
            "elementType": "text",
            "bounds": [52, 268, 139, 156],
            "content": {
                "text": f'<p><span style="font-family:微软雅黑;font-size:13.0px;color:#000000">{original}</span></p>'
            },
        }]}
        page = copy.deepcopy(shell)
        page["elements"][0]["content"]["text"] = (
            '<p><span style="font-family:微软雅黑;font-size:13.0px;color:#000000">'
            + "新" * 54
            + "</span></p>"
        )
        self.assertEqual(
            self.generation.template_binding_issues(page, shell, "pages/one.page", 540),
            [],
        )
        page["elements"][0]["content"]["text"] = (
            '<p><span style="font-family:微软雅黑;font-size:13.0px;color:#000000">'
            + "新" * 60
            + "</span></p>"
        )
        self.assertIn(
            "TemplateTextTooLong",
            {
                issue["code"]
                for issue in self.generation.template_binding_issues(page, shell, "pages/one.page", 540)
            },
        )

    def test_template_badges_and_band_text_may_stay_verbatim(self):
        shell={"elements":[{"elementId":"badge","elementType":"text","bounds":[288,161,62,34],
                            "content":{"text":'<p><span style="font-size:26.0px;color:#FFFFFF">01</span></p>'}},
                           {"elementId":"foot","elementType":"text","bounds":[805,516,156,15],
                            "content":{"text":'<p><span style="font-size:12.0px;color:#333333">长沙湘计海盾科技有限公司</span></p>'}},
                           {"elementId":"title","elementType":"text","bounds":[60,80,600,40],
                            "content":{"text":'<p><span style="font-size:30.0px;color:#E50011">模板标题</span></p>'}}]}
        page=copy.deepcopy(shell)
        codes={issue["code"]:issue["location"] for issue in self.generation.template_binding_issues(page,shell,"pages/one.page",540)}
        self.assertIn("TemplateTextNotReplaced",codes)
        self.assertTrue(codes["TemplateTextNotReplaced"].endswith("title"))

    async def test_template_mode_without_a_reference_is_rejected(self):
        with patch.object(self.generation,"make_model",return_value=self.model),patch.object(self.generation.template_reference,"load",return_value=None):
            with self.assertRaisesRegex(ValueError,"模板模式"):
                await self.generation.generate_pptd(request(designMode="template",templateId="corp"),lambda *a:None)

    async def test_auto_scenario_is_selected_and_complete_guide_loaded_before_plan(self):
        from agent_service.kimi_rules import KimiRules
        calls=[]; checkpoints=[]
        async def ask(model, role, rules, data, **kwargs):
            calls.append((role,copy.deepcopy(data),kwargs))
            if role=="scenario": return {"scenario":"tech-engineering"}
            if role=="plan":
                self.assertEqual(kwargs["scenario"],"tech-engineering")
                self.assertEqual(data["scenario"],"tech-engineering")
                self.assertIn(rules._source["reference/slides_categories/tech-engineering.md"],rules.system(role,kwargs["scenario"]))
                plan=copy.deepcopy(self.plan);plan["scenario"]="tech-engineering";return plan
            return {"data":bundle()["pages"][0]["data"]}
        with patch.object(self.generation,"make_model",return_value=self.model),patch.object(self.generation,"_ask",side_effect=ask):
            result=await self.generation.generate_pptd(request(scenario="auto"),lambda *a:None,save_checkpoint=checkpoints.append)
        self.assertEqual([c[0] for c in calls],["scenario","plan","page"])
        self.assertEqual(result["checkpoint"]["selectedScenario"],"tech-engineering")
        self.assertTrue(any(c.get("selectedScenario")=="tech-engineering" and "plan" not in c for c in checkpoints))
        self.assertTrue(self.closed)

    async def test_invalid_auto_scenario_never_reaches_planner(self):
        calls=[]
        async def ask(*args, **kwargs):calls.append(args[1]);return {"scenario":"../../untrusted"}
        with patch.object(self.generation,"make_model",return_value=self.model),patch.object(self.generation,"_ask",side_effect=ask):
            with self.assertRaises(ValueError): await self.generation.generate_pptd(request(scenario="auto"),lambda *a:None)
        self.assertEqual(calls,["scenario"])
        self.assertTrue(self.closed)

    async def test_multiple_repair_rounds_until_valid(self):
        bad=bundle()["pages"][0]["data"]; bad["elements"][0]["bounds"]=[0,0,0,20]
        bad2=copy.deepcopy(bad); bad2["elements"][0]["bounds"]=[0,0,100,-20]
        result,names=await self.run_draft([self.plan,{"data":bad},{"data":bad2},{"data":bundle()["pages"][0]["data"]}])
        self.assertEqual(names,["plan","page","repair","repair"])
        self.assertEqual(result["generationMeta"]["validation"]["repairRounds"],2)

    async def test_stagnation_fails_with_recoverable_checkpoint(self):
        bad=bundle()["pages"][0]["data"]; bad["elements"][0]["bounds"]=[0,0,0,20]
        with self.assertRaises(self.generation.GenerationIncomplete) as caught:
            await self.run_draft([self.plan,{"data":bad},{"data":bad}])
        self.assertEqual(caught.exception.checkpoint["stage"],"page")
        self.assertTrue(caught.exception.issues)
        self.assertTrue(self.closed)

    async def test_local_revision_preserves_unknown_fields_and_page_identity(self):
        existing=bundle(); other=copy.deepcopy(existing["pages"][0]);other["path"]="pages/two.page"; existing["pages"].append(other);existing["document"]["pages"].append(other["path"])
        async def answer(*args, **kwargs):
            return {"operations":[{"pagePath":"pages/one.page","elementId":"title","path":"/content/text","op":"set","value":"Edited"}],"reply":"Edited"}
        with patch.object(self.generation,"make_model",return_value=self.model),patch.object(self.generation,"_ask",side_effect=answer):
            result=await self.generation.generate_pptd(request(kind="revise",selectedSlideId="pages/one.page"),lambda *a:None,existing=existing)
        self.assertEqual(result["pages"][0]["data"]["elements"][0]["content"]["text"],"Edited")
        self.assertEqual(result["pages"][0]["data"]["elements"][0]["futureField"],{"keep":True})
        self.assertEqual(result["pages"][1],existing["pages"][1])
        self.assertEqual(existing["pages"][0]["data"]["elements"][0]["content"]["text"],"Hello")

    async def test_revision_cannot_touch_unselected_page(self):
        from agent_service.pptd_generation import apply_edit_operations
        with self.assertRaises(ValueError):
            apply_edit_operations(bundle(),[{"pagePath":"pages/elsewhere.page","elementId":"title","path":"/content/text","op":"set","value":"No"}],selected="pages/one.page")

    async def test_resume_does_not_regenerate_completed_pages(self):
        checkpoints=[]
        async def first(*args, **kwargs):
            if args[1]=="plan": return copy.deepcopy(self.plan)
            raise RuntimeError("Provider disconnected")
        with patch.object(self.generation,"make_model",return_value=self.model),patch.object(self.generation,"_ask",side_effect=first):
            with self.assertRaises(RuntimeError):
                await self.generation.generate_pptd(request(),lambda *a:None,save_checkpoint=checkpoints.append)
        self.assertIn("plan",checkpoints[-1])
        calls=[]
        async def resume(*args, **kwargs):
            calls.append(args[1]); return {"data":bundle()["pages"][0]["data"]}
        with patch.object(self.generation,"make_model",return_value=self.model),patch.object(self.generation,"_ask",side_effect=resume):
            result=await self.generation.generate_pptd(request(),lambda *a:None,checkpoint=checkpoints[-1])
        self.assertEqual(calls,["page"])
        self.assertEqual(result["pages"],bundle()["pages"])

    async def test_external_render_failure_runs_repair_even_when_structure_is_valid(self):
        result,_=await self.run_draft([self.plan,{"data":bundle()["pages"][0]["data"]}])
        cp=result["checkpoint"]; cp["externalIssues"]=[{"code":"TextOverflow","location":"pages/one.page.elements[0]","message":"Rendered height exceeds box"}]
        calls=[]
        async def repair(*args, **kwargs):
            calls.append(args[1]); page=bundle()["pages"][0]["data"];page["elements"][0]["bounds"][3]=120
            return {"data":page}
        with patch.object(self.generation,"make_model",return_value=self.model),patch.object(self.generation,"_ask",side_effect=repair):
            fixed=await self.generation.generate_pptd(request(),lambda *a:None,checkpoint=cp)
        self.assertEqual(calls,["repair"])
        self.assertEqual(fixed["pages"][0]["data"]["elements"][0]["bounds"][3],120)
        self.assertNotIn("externalIssues",fixed["checkpoint"])

    async def test_external_validation_is_included_in_local_revision(self):
        existing=bundle();cp=None
        async def first(*args, **kwargs):
            return {"operations":[{"pagePath":"pages/one.page","elementId":"title","path":"/content/text","op":"set","value":"Edited"}]}
        with patch.object(self.generation,"make_model",return_value=self.model),patch.object(self.generation,"_ask",side_effect=first):
            result=await self.generation.generate_pptd(request(kind="revise",selectedSlideId="pages/one.page"),lambda *a:None,existing=existing)
        cp=result["checkpoint"];cp["externalIssues"]=[{"code":"TextOverflow","location":"pages/one.page.elements[0]","message":"Overflow"}]
        observed=[]
        async def repair(*args, **kwargs):
            observed.extend(args[3]["issues"])
            return {"operations":[{"pagePath":"pages/one.page","elementId":"title","path":"/content/text","op":"set","value":"Edited"},{"pagePath":"pages/one.page","elementId":"title","path":"/bounds/3","op":"set","value":120}]}
        with patch.object(self.generation,"make_model",return_value=self.model),patch.object(self.generation,"_ask",side_effect=repair):
            result=await self.generation.generate_pptd(request(kind="revise",selectedSlideId="pages/one.page"),lambda *a:None,existing=existing,checkpoint=cp)
        self.assertEqual(observed[0]["code"],"TextOverflow")
        self.assertEqual(result["pages"][0]["data"]["elements"][0]["bounds"][3],120)
        self.assertNotIn("externalIssues",result["checkpoint"])

    def test_multimodal_reference_is_sent_as_image_not_encoded_text(self):
        from agent_service.pptd_generation import _message_content
        blocks=_message_content({"attachments":[{"name":"reference","imageData":"data:image/png;base64,aGVsbG8="}]})
        self.assertEqual(blocks[1]["type"],"data")
        self.assertEqual(blocks[1]["source"]["data"],"aGVsbG8=")
        self.assertNotIn("aGVsbG8=",blocks[0]["text"])

    async def test_render_never_calls_llm(self):
        with patch.object(self.generation,"make_model", side_effect=AssertionError("render must not call model")):
            result=await self.generation.generate_pptd(request(kind="render"), lambda *a:None, existing=bundle())
        self.assertEqual(result["document"],bundle()["document"])


class PlannedContentContract(unittest.IsolatedAsyncioTestCase):
    """A syntactically valid heading must not count as a completed content page."""

    def setUp(self):
        from agent_service import pptd_generation
        self.generation = pptd_generation
        self.content = ("三层治理（占位建议）：① 决策层——AI办公领导小组（公司分管领导牵头，季度评审）；"
                        "② 归口层——数字化办公室（PMO，负责路线图与预算）；"
                        "③ 执行层——业务BP+AI工程师+数据/安全合规（虚拟小组，按场景编组）。"
                        "机制：双周例会、月度里程碑评审、季度业务复盘。")
        self.source = bundle("谁来做、怎么做决策")
        self.source["pages"][0]["data"].update(pageType="content", notes="", animations=[], futurePage={"keep": True})
        self.source["pages"][0]["data"]["elements"].extend([
            {"elementId": "section-tag", "elementType": "text", "bounds": [60, 48, 240, 24], "content": {"text": "03 / 落地路径", "fontSize": 14}},
            {"elementId": "title-rule", "elementType": "line", "bounds": [60, 144, 60, 2], "viewBox": [1, 1], "points": "0,0 1,0", "border": {"width": 2, "color": "$ink"}},
        ])
        self.plan = {"title": "Deck", "narrative": "Explain", "scenario": "education-training", "inputType": "topic",
                     "document": self.source["document"], "pages": [{"title": "组织：谁来做、怎么做决策", "readerTask": "明确三层治理与协同机制", "content": self.content, "visualPlan": "左侧三层治理，右侧协同机制", "imageQueries": []}]}
        async def close(): pass
        self.model = SimpleNamespace(client=SimpleNamespace(close=close))

    def addition(self, text=None):
        return {"elementId": "recovered-body", "elementType": "text", "bounds": [60, 190, 840, 250],
                "content": {"text": self.content if text is None else text, "fontSize": 20, "color": "$ink"}}

    def layout(self, payload):
        return {"blocks": [{"segmentIds": [s["id"] for s in payload["contentRecovery"]["segments"]],
                            "bounds": [60, 190, 840, 250], "fontSize": 20, "color": "$ink", "bold": False}]}

    def test_heading_only_content_and_decision_pages_fail_plan_coverage(self):
        from agent_service.planned_content import planned_content_issues
        before = copy.deepcopy(self.source)
        for page_type in ["content", "final"]:
            self.source["pages"][0]["data"]["pageType"] = page_type
            issues = planned_content_issues(self.source, self.plan)
            self.assertEqual([i["code"] for i in issues], ["PlannedContentMissing"])
            self.assertEqual(issues[0]["pagePath"], "pages/one.page")
        self.source["pages"][0]["data"]["pageType"] = "content"
        self.assertEqual(self.source, before)

    def test_intentional_sparse_pages_and_real_body_content_are_not_rejected(self):
        from agent_service.planned_content import planned_content_issues
        for page_type in ["cover", "section", "divider"]:
            source = copy.deepcopy(self.source); source["pages"][0]["data"]["pageType"] = page_type
            self.assertEqual(planned_content_issues(source, self.plan), [])
        simple_plan = copy.deepcopy(self.plan); simple_plan["pages"][0]["content"] = "谢谢大家，欢迎交流。"
        source = copy.deepcopy(self.source); source["pages"][0]["data"]["pageType"] = "final"
        self.assertEqual(planned_content_issues(source, simple_plan), [])
        for body in [self.addition(), {"elementId": "chart", "elementType": "chart", "bounds": [60, 200, 800, 260], "data": {"cols": ["类别", "数值"], "rows": [["A", 10]]}, "series": [{"type": "bar"}]},
                     {"elementId": "table", "elementType": "table", "bounds": [60, 200, 800, 260], "rows": [[{"text": "已有内容"}]]}]:
            source = copy.deepcopy(self.source); source["pages"][0]["data"]["elements"].append(body)
            self.assertEqual(planned_content_issues(source, self.plan), [])

    def test_native_rich_text_and_formula_bodies_are_not_mistaken_for_empty_pages(self):
        from agent_service.planned_content import planned_content_issues
        from agent_service.pptd_validation import validate_document
        # PPTD represents both rich text and formulas in content.text strings.
        for text in ["<p><strong>三层治理</strong>：决策层、归口层、执行层</p>", r"\(\int_0^1 x^2\,dx=\frac13\)"]:
            source = copy.deepcopy(self.source)
            source["pages"][0]["data"]["elements"].append(self.addition(text))
            self.assertEqual(validate_document(source), [])
            self.assertEqual(planned_content_issues(source, self.plan), [])

    def test_recovery_keeps_old_elements_and_requires_exact_planned_text(self):
        from agent_service.planned_content import apply_content_additions
        original = copy.deepcopy(self.source["pages"][0]["data"])
        good = apply_content_additions(original, self.plan["pages"][0], [self.addition()])
        self.assertEqual(good["elements"][:-1], original["elements"])
        self.assertEqual(good["futurePage"], {"keep": True})
        self.assertEqual(original, self.source["pages"][0]["data"])
        split = [self.addition(self.content[:55] + "\n"), {**self.addition(self.content[55:]), "elementId": "recovered-body-2"}]
        self.assertEqual(len(apply_content_additions(original, self.plan["pages"][0], split)["elements"]), len(original["elements"]) + 2)
        for additions in [[self.addition("新增投资回报率：100%")], [self.addition(self.content[:-5])],
                          [self.addition(), self.addition("其他内容")],
                          [{**self.addition(), "elementId": "title"}],
                          [{**self.addition(), "elementType": "image", "src": "invented.png"}],
                          [{**self.addition(), "speakerNotes": "新增事实"}]]:
            with self.subTest(additions=additions), self.assertRaises(ValueError):
                apply_content_additions(original, self.plan["pages"][0], additions)

    async def test_new_generation_recovers_planned_body_before_completion(self):
        async def provider(model, role, rules, data, **kwargs):
            if role == "plan": return copy.deepcopy(self.plan)
            if role == "page": return {"data": copy.deepcopy(self.source["pages"][0]["data"])}
            if data.get("contentRecovery"): return self.layout(data)
            raise AssertionError("Content recovery must have a restricted contract")
        with patch.object(self.generation, "make_model", return_value=self.model), patch.object(self.generation, "_ask", side_effect=provider):
            result = await self.generation.generate_pptd(request(), lambda *a: None)
        self.assertIn(self.content, [e.get("content", {}).get("text") for e in result["pages"][0]["data"]["elements"]])
        self.assertEqual(result["pages"][0]["data"]["elements"][:-1], self.source["pages"][0]["data"]["elements"])
        self.assertEqual(result["checkpoint"].get("_contentRecoveryPages"), ["pages/one.page"])

    async def test_recovery_marker_survives_checkpoint_resume_without_rewriting_content(self):
        req = request()
        async def provider(model, role, rules, data, **kwargs):
            if role == "plan": return copy.deepcopy(self.plan)
            if role == "page": return {"data": copy.deepcopy(self.source["pages"][0]["data"])}
            return self.layout(data)
        with patch.object(self.generation, "make_model", return_value=self.model), patch.object(self.generation, "_ask", side_effect=provider):
            recovered = await self.generation.generate_pptd(req, lambda *a: None)
        with patch.object(self.generation, "make_model", return_value=self.model), patch.object(self.generation, "_ask", side_effect=AssertionError("Recovered page must be preserved on resume")):
            resumed = await self.generation.generate_pptd(req, lambda *a: None, checkpoint=recovered["checkpoint"])
        self.assertEqual(resumed["pages"], recovered["pages"])
        self.assertEqual(resumed["checkpoint"].get("_contentRecoveryPages"), ["pages/one.page"])

    async def test_each_recovered_page_is_recorded_in_durable_checkpoint(self):
        plan = copy.deepcopy(self.plan)
        plan["document"]["pages"].append("pages/two.page")
        plan["pages"].append(copy.deepcopy(plan["pages"][0]))
        checkpoints = []
        async def provider(model, role, rules, data, **kwargs):
            if role == "plan": return copy.deepcopy(plan)
            if role == "page": return {"data": copy.deepcopy(self.source["pages"][0]["data"])}
            return self.layout(data)
        with patch.object(self.generation, "make_model", return_value=self.model), patch.object(self.generation, "_ask", side_effect=provider):
            result = await self.generation.generate_pptd(request(), lambda *a: None, save_checkpoint=checkpoints.append)
        self.assertEqual(set(result["checkpoint"].get("_contentRecoveryPages", [])), {"pages/one.page", "pages/two.page"})
        self.assertEqual(len(result["checkpoint"]["_contentRecoveryPages"]), 2)
        self.assertEqual(checkpoints[-1]["_contentRecoveryPages"], result["checkpoint"]["_contentRecoveryPages"])

    async def test_resume_repairs_only_incomplete_checkpoint_page(self):
        from agent_service.kimi_rules import KimiRules
        req = request()
        source = copy.deepcopy(self.source)
        other = {"path": "pages/two.page", "data": {"elements": [self.addition("其他页已经完成的正文")], "future": "keep"}}
        source["pages"].append(other); source["document"]["pages"].append(other["path"])
        plan = copy.deepcopy(self.plan); plan["document"] = source["document"]; plan["pages"].append({"title": "已完成页", "readerTask": "Read", "content": "其他页已经完成的正文", "visualPlan": "Body", "imageQueries": []})
        cp = {"version": 1, "requestHash": self.generation._digest({"context": self.generation._context(req), "kind": req.kind, "selected": None, "existing": None}), "rulesVersion": KimiRules().version, "stage": "validated", "repairRounds": 0, "plan": plan, "pages": source["pages"], "research": {}, "assets": []}
        async def provider(model, role, rules, data, **kwargs):
            if data.get("pagePath") != "pages/one.page" or not data.get("contentRecovery"):
                raise AssertionError("Only the missing planned body may be regenerated")
            self.assertNotIn("previousPages", data)
            self.assertNotIn("plan", data)
            self.assertNotIn("conversation", data)
            self.assertEqual(data["document"]["pages"], ["pages/one.page"])
            return self.layout(data)
        with patch.object(self.generation, "make_model", return_value=self.model), patch.object(self.generation, "_ask", side_effect=provider):
            result = await self.generation.generate_pptd(req, lambda *a: None, checkpoint=cp)
        self.assertEqual(result["pages"][1], other)
        self.assertIn(self.content, [e.get("content", {}).get("text") for e in result["pages"][0]["data"]["elements"]])
        self.assertEqual(cp["pages"][0], self.source["pages"][0])

    async def test_external_missing_body_issue_cannot_reach_unrestricted_repair(self):
        from agent_service.kimi_rules import KimiRules
        req = request()
        cp = {"version": 1, "requestHash": self.generation._digest({"context": self.generation._context(req), "kind": req.kind, "selected": None, "existing": None}), "rulesVersion": KimiRules().version, "stage": "validated", "repairRounds": 0, "plan": self.plan, "pages": copy.deepcopy(self.source["pages"]), "research": {}, "assets": [],
              "externalIssues": [{"code": "PlannedContentMissing", "pagePath": "pages/one.page", "location": "pages/one.page.elements", "message": "Missing body; must never turn into permission to rewrite data"}]}
        async def provider(model, role, rules, data, **kwargs):
            if not data.get("contentRecovery"):
                raise AssertionError("A missing-body diagnostic may not authorize whole-page replacement")
            return self.layout(data)
        with patch.object(self.generation, "make_model", return_value=self.model), patch.object(self.generation, "_ask", side_effect=provider):
            result = await self.generation.generate_pptd(req, lambda *a: None, checkpoint=cp)
        self.assertEqual(result["pages"][0]["data"]["elements"][:-1], self.source["pages"][0]["data"]["elements"])

    async def test_invented_recovery_text_never_completes_or_modifies_checkpoint(self):
        checkpoints = []
        recovery_attempts = []
        async def provider(model, role, rules, data, **kwargs):
            if role == "plan": return copy.deepcopy(self.plan)
            if role == "page": return {"data": copy.deepcopy(self.source["pages"][0]["data"])}
            recovery_attempts.append(data)
            return {"appendElements": [self.addition("新增季度收益：100万元") ]}
        with patch.object(self.generation, "make_model", return_value=self.model), patch.object(self.generation, "_ask", side_effect=provider):
            with self.assertRaises(self.generation.GenerationIncomplete) as caught:
                await self.generation.generate_pptd(request(), lambda *a: None, save_checkpoint=checkpoints.append)
        self.assertEqual(caught.exception.checkpoint["pages"], [])
        self.assertEqual(caught.exception.checkpoint["drafts"]["pages/one.page"]["data"], self.source["pages"][0]["data"])
        self.assertEqual(caught.exception.issues[0]["code"], "PlannedContentMissing")
        self.assertEqual(len(recovery_attempts), 2)

    def test_source_block_layout_keeps_quotes_numbers_and_all_punctuation(self):
        from agent_service.planned_content import content_segments, apply_content_layout
        content = '季度目标："营收增长12.5%"；预算保持1,200万元。\n假设标记（待确认），不补写收益。'
        segments = content_segments(content)
        self.assertEqual([s["text"] for s in segments], ['季度目标："营收增长12.5%"；', '预算保持1,200万元。\n', '假设标记（待确认），不补写收益。'])
        page_plan = {**self.plan["pages"][0], "content": content}
        blocks = [{"segmentIds": [0], "bounds": [70, 190, 400, 90], "fontSize": 22, "color": "$ink", "bold": True},
                  {"segmentIds": [1, 2], "bounds": [70, 300, 800, 160], "fontSize": 20, "color": "$ink", "bold": False, "align": ["left", "top"]}]
        original = copy.deepcopy(self.source["pages"][0]["data"])
        # Generated IDs must avoid previously authored IDs without touching them.
        original["elements"][0]["elementId"] = "recovered-body-1"
        result = apply_content_layout(original, page_plan, {"blocks": blocks}, self.source["document"])
        self.assertEqual(result["elements"][:3], original["elements"])
        self.assertEqual("".join(e["content"]["text"] for e in result["elements"][3:]), content)
        self.assertEqual([e["bounds"] for e in result["elements"][3:]], [[70, 190, 400, 90], [70, 300, 800, 160]])
        self.assertEqual(len({e["elementId"] for e in result["elements"]}), len(result["elements"]))

    def test_source_block_layout_rejects_omission_duplication_reordering_and_text_injection(self):
        from agent_service.planned_content import apply_content_layout
        plan = {"content": "第一条；第二条；第三条。"}
        def block(ids): return {"segmentIds": ids, "bounds": [60, 190, 840, 250], "fontSize": 20, "color": "$ink", "bold": False}
        layouts = [{"blocks": [block([0, 2])]}, {"blocks": [block([0, 1, 1, 2])]}, {"blocks": [block([1, 0, 2])]},
                   {"blocks": [block([0, 1, 2, 3])]}, {"blocks": [{**block([0, 1, 2]), "text": "新数字100%"}]},
                   {"blocks": [{**block([0, 1, 2]), "bounds": [0, 0, 0, 10]}]},
                   {"blocks": [{**block([0, 1, 2]), "bounds": [0, 0, float("nan"), 10]}]}]
        for layout in layouts:
            with self.subTest(layout=layout), self.assertRaises(ValueError):
                apply_content_layout(self.source["pages"][0]["data"], plan, layout, self.source["document"])


if __name__ == "__main__": unittest.main()
