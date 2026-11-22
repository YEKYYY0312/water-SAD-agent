"""T2-5 independent 90-digit Decimal trigonometric oracle and contract."""
from __future__ import annotations

import copy
import json
import math
from decimal import Decimal, localcontext

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from water_agent.graphs.agent_rag import make_citation_validator
from water_agent.tools.hydraulics import calc_slope_manning

NAME = 'calc_slope_manning'
RECEIPT = 'water_agent_programmatic_calculation'
ARGS = {'flow_lps': 10, 'diameter_mm': 200, 'fill_ratio': .5, 'manning_n': .013}
QUERY = ('仅按曼宁（Manning）公式计算非满管圆管重力均匀流的参考坡度。流量10 L/s，'
         '实际内径200 mm，充满度h/d=0.5，曼宁粗糙系数n=0.013（由用户按SI口径给定，'
         '本次不核验其工程来源）。请调用程序化工具，列明输入、几何关系、公式、结果及适用前提；'
         '不推定DN，不选型，不检索条文，不做最小设计坡度或规范符合性判断。')


def gold(r='.5', q='10', d='200', n='.013'):
    # Taylor sin/cos + bisection acos, independent of binary math/kernel.
    with localcontext() as ctx:
        ctx.prec = 90
        def trig(x, cosine=False):
            term = Decimal(1) if cosine else x
            total = term
            for k in range(1, 500):
                term *= -x*x / ((2*k-1 if cosine else 2*k)*(2*k if cosine else 2*k+1))
                previous=total; total+=term
                if total==previous: break
            return total
        lo,hi=Decimal(0),Decimal('3.141592653589793238462643383279502884197169399375105820974944592307816406286208998628034825')
        target=1-2*Decimal(r)
        for _ in range(400):
            mid=(lo+hi)/2
            if trig(mid,True)>target:lo=mid
            else:hi=mid
        theta=lo+hi; diameter=Decimal(d)/1000
        a=diameter**2*(theta-trig(theta))/8
        p=diameter*theta/2; radius=a/p
        return (Decimal(n)*Decimal(q)/1000/(a*radius**(Decimal(2)/3)))**2


@pytest.mark.parametrize('r', ['.01','.1','.5','.8','.99','.0001','.00000001','.000000000001',str(math.nextafter(1,0))])
def test_independent_gold(r):
    result=calc_slope_manning({**ARGS,'fill_ratio':float(r)})
    reference=float(gold(str(Decimal.from_float(float(r)))))
    if math.ulp(reference)>1e-9:
        assert not result.ok and result.errors
        return
    assert result.ok
    actual=result.values['slope']
    assert abs(actual/reference-1)<=1e-12
    assert abs(actual-reference)<=(1e-9 if reference>=1 else 1e-12)


def test_half_full_analytic_and_scaling():
    expected=(.013*.01 / ((math.pi*.2**2/8)*(.2/4)**(2/3)))**2
    assert float(gold())==pytest.approx(expected,rel=1e-14)
    base=calc_slope_manning(ARGS).values['slope']
    for field,mult,power in [('flow_lps',2,2),('manning_n',2,2),('diameter_mm',2,-16/3)]:
        assert calc_slope_manning({**ARGS,field:ARGS[field]*mult}).values['slope']==pytest.approx(base*mult**power,rel=1e-12)


@pytest.mark.parametrize('changes',[{'fill_ratio':5e-324},{'diameter_mm':5e-324},{'diameter_mm':1e308},
    {'flow_lps':5e-324},{'flow_lps':1e308},{'manning_n':5e-324},{'manning_n':1e308},
    {'diameter_mm':10**400}])
def test_numeric_failure_is_structured(changes):
    result=calc_slope_manning({**ARGS,**changes})
    assert not result.ok and result.errors and not result.values


def test_canonical_real_original():
    from water_agent.tools.calc_manning import calc_slope_manning_spec
    from water_agent.tools.manning_request import manning_request_scope
    from water_agent.tools.registry import ToolRegistry
    registry=ToolRegistry();registry.register(calc_slope_manning_spec)
    with manning_request_scope(QUERY):result=registry.invoke('calc_slope_manning',**ARGS)
    assert result['slope']==pytest.approx(float(gold()),rel=1e-12)


def invoke(args=None, query=QUERY):
    from water_agent.tools.calc_manning import calc_slope_manning_spec
    from water_agent.tools.ds_adapters import tool_spec_to_base_tool
    from water_agent.tools.manning_request import manning_request_scope
    from water_agent.tools.registry import ToolRegistry
    registry = ToolRegistry(); registry.register(calc_slope_manning_spec)
    call = {'name': NAME, 'args': ARGS if args is None else args, 'id': 'manning-call', 'type': 'tool_call'}
    with manning_request_scope(query):
        obs = tool_spec_to_base_tool(registry, NAME).invoke(call)
    return call, obs


def turn(query=QUERY, args=None):
    call, obs = invoke(args, query)
    return [HumanMessage(content=query), AIMessage(content='', tool_calls=[call]), obs,
            AIMessage(content='错误草稿999 m，必须选DN200，符合GB50014-2021。', id='final')]


def validate(messages):
    return make_citation_validator()({'messages': messages, 'errors': [], 'warnings': []})




@pytest.mark.parametrize('mutation', ['artifact','body','call_id','receipt_id','receipt_name','args',
    'extra_call','new_human','empty','no_final_id','failed','assumptions','both_result','query'])
def test_receipt_body_and_turn_tamper_rejected(mutation):
    messages=copy.deepcopy(turn());obs=messages[2]
    if mutation=='artifact':obs.artifact=None
    elif mutation=='body':obs.content=obs.content.replace('0.003718','99.003718')
    elif mutation=='call_id':obs.tool_call_id='other'
    elif mutation=='receipt_id':obs.artifact[RECEIPT]['tool_call_id']='other'
    elif mutation=='receipt_name':obs.artifact[RECEIPT]['tool_name']='calc_velocity'
    elif mutation=='args':messages[1].tool_calls[0]['args']['flow_lps']=200
    elif mutation=='extra_call':messages.insert(3,AIMessage(content='',tool_calls=[{'name':'retrieve_clauses','args':{'query':'x'},'id':'extra'}]))
    elif mutation=='new_human':messages.insert(-1,HumanMessage(content=QUERY))
    elif mutation=='empty':messages[-1].content=''
    elif mutation=='no_final_id':messages[-1].id=None
    elif mutation=='failed':obs.status='error'
    elif mutation=='query':obs.artifact[RECEIPT]['query']=QUERY+' '
    else:
        payload=json.loads(obs.content)
        if mutation=='assumptions':payload['assumptions']=['made up']
        else:payload['slope']=42
        obs.content=json.dumps(payload);obs.artifact[RECEIPT]['result']=payload
    assert 'no_citation_programmatic_calculation' not in validate(messages).get('warnings',[])




def test_receipt_requires_observer_current_query_and_is_private_after_serde():
    from langchain_openai.chat_models.base import _convert_message_to_dict

    from water_agent.session import default_checkpoint_serde
    from water_agent.tools.calc_manning import verified_manning_result
    messages=turn();obs=messages[2];payload=json.loads(obs.content)
    assert not verified_manning_result(ARGS,payload,obs.artifact[RECEIPT],'manning-call',None)
    assert not verified_manning_result(ARGS,payload,obs.artifact[RECEIPT],'manning-call',QUERY+' ')
    assert RECEIPT not in json.dumps(_convert_message_to_dict(obs))
    serde=default_checkpoint_serde()
    assert validate(serde.loads_typed(serde.dumps_typed(messages)))['status']=='succeeded'


def test_same_name_external_and_unauthorized_registry_never_mint_receipt():
    from water_agent.runtime.governance.contracts import Principal
    from water_agent.tools.base import ToolSpec
    from water_agent.tools.calc_manning import calc_slope_manning_spec as spec
    from water_agent.tools.ds_adapters import tool_spec_to_base_tool
    from water_agent.tools.manning_request import manning_request_scope
    from water_agent.tools.registry import ToolRegistry
    for registry,tool in [(ToolRegistry(),ToolSpec(name=NAME,description=spec.description,args_model=spec.args_model,execute=lambda _: {'slope':1})),
                          (ToolRegistry(principal=Principal.local('denied',capabilities=frozenset())),spec)]:
        registry.register(tool)
        with manning_request_scope(QUERY):
            obs=tool_spec_to_base_tool(registry,NAME).invoke({'name':NAME,'args':ARGS,'id':'x','type':'tool_call'})
        assert not obs.artifact




def test_output_guardrail_still_blocks_fixed_answer(monkeypatch):
    from water_agent.graphs.agent_rag import BLOCKED_OUTPUT_PLACEHOLDER, make_finalize
    from water_agent.runtime.governance.pipeline import default_guardrail_pipeline
    messages=turn();checked=validate(messages);messages[-1]=checked['messages'][0]
    pipeline=default_guardrail_pipeline();original=pipeline.check_output;seen=[]
    def block(answer,context,**_):
        seen.append(answer);return original(answer,context,citations_verified=False)
    monkeypatch.setattr(pipeline,'check_output',block)
    final=make_finalize(pipeline=pipeline)({**checked,'messages':messages})
    assert seen and '0.0037183786385918' in seen[0] and '999' not in seen[0]
    assert final['status']=='blocked' and final['answer']==''
    assert final['messages'][0].content==BLOCKED_OUTPUT_PLACEHOLDER


def test_real_production_web_sse_offline_model(tmp_path):
    from sqlalchemy import create_engine
    from test_web_stream_guardrail import _run

    from water_agent.storage.database import Database
    from water_agent.storage.models import Base
    engine=create_engine(f"sqlite:///{tmp_path/'web.sqlite'}");Base.metadata.create_all(engine)
    database=Database(engine=engine)
    try:events=_run(database,tmp_path,[{'tool_calls':[{'name':NAME,'args':ARGS}]},{'content':'水头损失999 m。'}],QUERY)
    finally:database.dispose()
    final=next(e['payload'] for e in events if e['type']=='finalize')
    assert final['status']=='succeeded' and '0.0037183786385918' in final['answer']
    calculations=[e['payload']['calculation'] for e in events if e['type']=='tool_result' and 'calculation' in e['payload']]
    assert len(calculations)==1 and calculations[0]['tool_name']==NAME
    assert calculations[0]['values']['slope']==pytest.approx(float(gold()),rel=1e-12)
    assert '工程来源未核验' in ' '.join(calculations[0]['assumptions'])
    assert RECEIPT not in json.dumps(events,ensure_ascii=False)




@pytest.mark.parametrize('field',list(ARGS))
@pytest.mark.parametrize('value',[True,False,'0.5',None,float('nan'),float('inf'),-1,0,10**400])
def test_strict_inputs(field,value):
    _,obs=invoke({**ARGS,field:value})
    assert json.loads(obs.content)['error'] and not obs.artifact


@pytest.mark.parametrize('old,new',[
 ('10 L/s','10'),('10 L/s','10 L/S'),('200 mm','200 MM'),('实际内径200 mm','DN200'),
 ('充满度h/d=0.5','充满度50%'),('充满度h/d=0.5','充满度50'),
 ('充满度h/d=0.5','半满'),('充满度h/d=0.5','h=100 mm'),
 ('n=0.013','n=0.013无量纲'),('n=0.013','n=0.013 mm'),('n=0.013','n'),
 ('流量10 L/s','流量1材料0 L/s'),('流量10 L/s','流量1水温10°C0 L/s'),
 ('实际内径200 mm','实际内径流量10 L/s200 mm'),
 ('公式计算','公式只解释'),('公式计算','公式不计算'),('仅按曼宁','不要按曼宁'),
 ('非满管','满管有压'),('圆管','非圆管'),('均匀流','非均匀流'),('重力','回水'),
 ('充满度h/d=0.5','充满度h/d=1'),('充满度h/d=0.5','充满度h/d=0'),
])
def test_original_rejections(old,new):
    _,obs=invoke(query=QUERY.replace(old,new))
    assert json.loads(obs.content)['error'] and not obs.artifact


@pytest.mark.parametrize('suffix',['Q=10 L/s','流量10 L/s','n=0.013','h/d=0.5','d=200 mm',
 'n','g=9.81','v=1 m/s','C=130','f=0.02','长度100 m','水温10°C','kg','Q=10 L/s kg'])
def test_repeated_unknown_and_extra(suffix):
    _,obs=invoke(query=QUERY+suffix)
    assert json.loads(obs.content)['error'] and not obs.artifact


@pytest.mark.parametrize('old,new',[
 ('曼宁（Manning）','MANNING'),('曼宁（Manning）','曼宁'),
 ('流量10 L/s','Q=10l/s'),('流量10 L/s','flow_lps=10升/秒'),
 ('实际内径200 mm','d=200毫米'),('实际内径200 mm','diameter_mm=200 mm'),
 ('充满度h/d=0.5','fill_ratio=0.5'),('充满度h/d=0.5','h/d=0.5'),
 ('曼宁粗糙系数n=0.013','manning_n=0.013'),('曼宁粗糙系数n=0.013','n=0.013'),
 ('非满管圆管重力均匀流','工况未确认'),
])
def test_aliases_fixed_summary(old,new):
    result=validate(turn(QUERY.replace(old,new)))
    assert result['status']=='succeeded'
    answer=result['messages'][0].content
    for text in ['m/m','s/m^(1/3)','Q从L/s换算为m³/s','几何关系','工程来源未核验','适用性未独立核验','未检索条文']:
        assert text in answer
    assert '999' not in answer


@pytest.mark.parametrize('suffix',['并查询规范','并选择管材','并计算流速','并判断是否符合规范'])
def test_mixed_never_fixed(suffix):
    assert 'no_citation_programmatic_calculation' not in validate(turn(QUERY+suffix)).get('warnings',[])


def test_budget_matrix_exact_formula_and_limited_names(tmp_path):
    from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
    from langgraph.checkpoint.memory import MemorySaver

    from water_agent.drawing.synth import build_synthetic_drawing
    from water_agent.graphs.agent_rag import build_agentic_app
    from water_agent.plugins import (
        AssemblyContext,
        McpGatewayPlugin,
        assemble_tools,
        builtin_plugins,
    )
    from water_agent.retrieval.in_memory import InMemoryRetriever
    from water_agent.runtime.governance.contracts import Principal
    build_synthetic_drawing(tmp_path,drawing_id='P-01')
    assembly=assemble_tools(AssemblyContext(retriever=InMemoryRetriever([]),drawing_root=tmp_path),base_principal=Principal.local('manning-budget'),trace=None,plugins=builtin_plugins(mcp=McpGatewayPlugin.borrowing(None)))
    queries=['你好','读图统计材料']
    names={'满管流速':'calc_velocity','Darcy沿程损失':'calc_head_loss_darcy_weisbach',
           '计算实际内径':'calc_diameter','Hazen–Williams沿程水头损失':'calc_head_loss_hazen_williams',
           'Shevelev沿程水头损失':'calc_head_loss_shevelev','舍维列夫沿程水头损失':'calc_head_loss_shevelev',
           'Manning非满管坡度':NAME,'曼宁非满管坡度':NAME,
           'Manning公式非满管实际内径充满度坡度':NAME}
    for query in names:
        queries.extend(query+suffix for suffix in ['', '和统计材料','和读图','和读图统计材料'])
    queries.extend(['Darcy、Hazen、曼宁非满管坡度，计算实际内径',
                    '满管流速、Darcy、Hazen、曼宁非满管坡度，计算实际内径，读图统计材料'])
    rows={}
    for query in queries:
        d=assembly.exposure.decide(query);rows[query]=d.to_state()
        assert d.schema_chars<=4500 and d.guide_chars<=800
        if query in names:
            assert names[query] in d.exposed
            if names[query]==NAME:
                assert 'calc_head_loss_darcy_weisbach' not in d.exposed
                assert 'calc_diameter' not in d.exposed
        if query=='读图统计材料':assert {'material_stats','read_drawing'}<=set(d.exposed)
        if d.dropped:
            app=build_agentic_app(model=FakeMessagesListChatModel(responses=[AIMessage(content='请拆分请求。')]),tools=assembly.tools,exposure=assembly.exposure,checkpointer=MemorySaver())
            result=app.invoke({'messages':[HumanMessage(content=query)]},{'configurable':{'thread_id':query}})
            warnings=' '.join(result['warnings'])
            assert 'TOOL_EXPOSURE_LIMITED' in warnings
            for group,_ in d.dropped:
                tool=next(g.tools[0] for g in assembly.exposure.groups if g.group_id==group)
                assert tool in warnings
    sticky=assembly.exposure.decide('Manning非满管坡度',previous_turn_tools=['calc_head_loss_darcy_weisbach'])
    assert NAME in sticky.exposed and sticky.schema_chars<=4500
    (tmp_path/'budget.json').write_text(json.dumps(rows,ensure_ascii=False,indent=2))




@pytest.mark.parametrize('r,q',[(.02,220.41214230833145),(.5,163992.2008282553)])
def test_review_cumulative_roundoff_exceeds_absolute_contract(r,q):
    result=calc_slope_manning({**ARGS,'fill_ratio':r,'flow_lps':q})
    reference=float(gold(str(Decimal.from_float(r)),str(Decimal.from_float(q))))
    if result.ok:
        assert abs(result.values['slope']-reference)<=1e-9
    else:
        assert result.errors and not result.values


def test_review_discussing_diameter_does_not_open_diameter_tool():
    from water_agent.plugins.builtin import DIAMETER_EXPOSURE
    from water_agent.plugins.exposure import normalize
    assert not DIAMETER_EXPOSURE.matches(normalize('曼宁计算坡度实际内径200 mm，流量10 L/s，充满度0.5，n=0.013。'))
    assert DIAMETER_EXPOSURE.matches(normalize('曼宁计算坡度，并计算实际内径'))


def test_review_giant_integer_fill_ratio_structured():
    result=calc_slope_manning({**ARGS,'fill_ratio':10**400})
    assert not result.ok and result.errors


@pytest.mark.parametrize('location',['payload_input','receipt_args','receipt_result','payload_result'])
def test_nested_bool_not_numeric_equality(location):
    from water_agent.tools.calc_manning import verified_manning_result
    args={**ARGS,'flow_lps':1};query=QUERY.replace('流量10 L/s','流量1 L/s')
    _,obs=invoke(args,query);payload=json.loads(obs.content);receipt=copy.deepcopy(obs.artifact[RECEIPT])
    if location=='payload_input':payload['inputs']['flow_lps']=True
    elif location=='receipt_args':receipt['args']['flow_lps']=True
    elif location=='receipt_result':receipt['result']['inputs']['flow_lps']=True
    else:payload['slope']=True
    assert not verified_manning_result(args,payload,receipt,'manning-call',query)


def test_review_newline_manning_disambiguation():
    from water_agent.plugins.builtin import DIAMETER_EXPOSURE
    from water_agent.plugins.exposure import normalize
    assert not DIAMETER_EXPOSURE.matches(normalize('计算坡度实际内径200 mm，流量10 L/s，充满度0.5，n=0.013。\n曼宁'))


@pytest.mark.parametrize('diameter,flow',[(1e-110,1.3629674921804383e-295),(1e-100,6.326334692281935e-269)])
def test_review_small_radius_power_exponent_rounding(diameter,flow):
    result=calc_slope_manning({**ARGS,'diameter_mm':diameter,'flow_lps':flow})
    reference=float(gold('.5',str(Decimal.from_float(flow)),str(Decimal.from_float(diameter)),str(Decimal.from_float(.013))))
    if result.ok:
        assert abs(result.values['slope']-reference)<=1e-9
        assert abs(result.values['slope']/reference-1)<=1e-12
    else:assert result.errors and not result.values
