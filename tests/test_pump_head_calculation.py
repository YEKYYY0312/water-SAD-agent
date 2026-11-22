"""T2-6: independent exact binary64 sum and trusted zero-valued chain."""
from __future__ import annotations

import copy
import json
import math
from decimal import Decimal, localcontext
from itertools import permutations

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from water_agent.graphs.agent_rag import make_citation_validator
from water_agent.tools.hydraulics import calc_pump_head

NAME='calc_pump_head'
RECEIPT='water_agent_programmatic_calculation'
ARGS={'static_head_m':10,'friction_head_m':2.8,'local_head_m':0,'residual_head_m':2}
QUERY='仅按四分量相加计算泵扬程。静扬程10 m，沿程损失扬程2.8 m，局部损失扬程0 m，末端余压扬程2 m。请调用程序化工具，列明四项输入、公式、完整精度结果和适用前提；四项由用户明确给定，本次不核验其工程来源，不另算损失，不选泵，不检索条文，不做规范符合性判断。'
def gold(values):
    with localcontext() as ctx:
        ctx.prec=2000
        return float(sum((Decimal.from_float(float(v)) for v in values),Decimal(0)))
@pytest.mark.parametrize('values',[(10,2.8,0,2),(10,0,0,2),(0,0,0,0),(25,4.2,1.2,3),(1e16,1,1,0),(5e-324,0,0,0),(1e308,1e308,0,0),(1.7976931348623157e308,0,0,0),(1e308,5e-324,1,0)])
def test_independent_gold_permutations(values):
    for items in set(permutations(values)):
        result=calc_pump_head(dict(zip(ARGS,items,strict=True)));ref=gold(items)
        if not math.isfinite(ref):
            assert not result.ok and not result.values and result.errors
        else:
            assert result.ok
            value=result.values['pump_head_m']
            assert math.isfinite(value) and abs(value-ref)<=math.ulp(ref)
            if ref<=10000:assert abs(value-ref)<=1e-12
            if ref in (0,5e-324,10000000000000002):assert value==ref
            if any(items):assert value>0
def invoke(args=None, query=QUERY):
    from water_agent.tools.calc_pump_head import calc_pump_head_spec
    from water_agent.tools.ds_adapters import tool_spec_to_base_tool
    from water_agent.tools.pump_head_request import pump_head_request_scope
    from water_agent.tools.registry import ToolRegistry
    registry = ToolRegistry(); registry.register(calc_pump_head_spec)
    call = {'name': NAME, 'args': ARGS if args is None else args, 'id': 'pump-head-call', 'type': 'tool_call'}
    with pump_head_request_scope(query):
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
    elif mutation=='body':obs.content=obs.content.replace('14.8','99.8')
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
        else:payload['pump_head_m']=42
        obs.content=json.dumps(payload);obs.artifact[RECEIPT]['result']=payload
    assert 'no_citation_programmatic_calculation' not in validate(messages).get('warnings',[])




def test_receipt_requires_observer_current_query_and_is_private_after_serde():
    from langchain_openai.chat_models.base import _convert_message_to_dict

    from water_agent.session import default_checkpoint_serde
    from water_agent.tools.calc_pump_head import verified_pump_head_result
    messages=turn();obs=messages[2];payload=json.loads(obs.content)
    assert not verified_pump_head_result(ARGS,payload,obs.artifact[RECEIPT],'pump-head-call',None)
    assert not verified_pump_head_result(ARGS,payload,obs.artifact[RECEIPT],'pump-head-call',QUERY+' ')
    assert RECEIPT not in json.dumps(_convert_message_to_dict(obs))
    serde=default_checkpoint_serde()
    assert validate(serde.loads_typed(serde.dumps_typed(messages)))['status']=='succeeded'


def test_same_name_external_and_unauthorized_registry_never_mint_receipt():
    from water_agent.runtime.governance.contracts import Principal
    from water_agent.tools.base import ToolSpec
    from water_agent.tools.calc_pump_head import calc_pump_head_spec as spec
    from water_agent.tools.ds_adapters import tool_spec_to_base_tool
    from water_agent.tools.pump_head_request import pump_head_request_scope
    from water_agent.tools.registry import ToolRegistry
    for registry,tool in [(ToolRegistry(),ToolSpec(name=NAME,description=spec.description,args_model=spec.args_model,execute=lambda _: json.loads(invoke()[1].content))),
                          (ToolRegistry(principal=Principal.local('denied',capabilities=frozenset())),spec)]:
        registry.register(tool)
        with pump_head_request_scope(QUERY):
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
    assert seen and '14.8' in seen[0] and '999' not in seen[0]
    assert final['status']=='blocked' and final['answer']==''
    assert final['messages'][0].content==BLOCKED_OUTPUT_PLACEHOLDER


@pytest.mark.parametrize('zero',[False,True])
def test_real_production_web_sse_offline_model(tmp_path,zero):
    from sqlalchemy import create_engine
    from test_web_stream_guardrail import _run

    from water_agent.storage.database import Database
    from water_agent.storage.models import Base
    args={k:0 for k in ARGS} if zero else ARGS
    query='计算泵扬程。'+ '，'.join(f'{k}=0 m' for k in ARGS) if zero else QUERY
    engine=create_engine(f"sqlite:///{tmp_path/'web.sqlite'}");Base.metadata.create_all(engine)
    database=Database(engine=engine)
    try:events=_run(database,tmp_path,[{'tool_calls':[{'name':NAME,'args':args}]},{'content':'水头损失999 m。'}],query)
    finally:database.dispose()
    final=next(e['payload'] for e in events if e['type']=='finalize')
    assert final['status']=='succeeded' and ('0.0 m' if zero else '14.8') in final['answer']
    calculations=[e['payload']['calculation'] for e in events if e['type']=='tool_result' and 'calculation' in e['payload']]
    assert len(calculations)==1 and calculations[0]['tool_name']==NAME
    assert calculations[0]['values']['pump_head_m']==(0 if zero else 14.8)
    assert '工程来源未核验' in ' '.join(calculations[0]['assumptions'])
    assert RECEIPT not in json.dumps(events,ensure_ascii=False)





@pytest.mark.parametrize('field',list(ARGS))
@pytest.mark.parametrize('value',[True,False,'0',None,'',float('nan'),float('inf'),-1,10**400])
def test_strict_inputs(field,value):
    _,obs=invoke({**ARGS,field:value})
    assert json.loads(obs.content)['error'] and not obs.artifact

@pytest.mark.parametrize('field',list(ARGS))
def test_missing_not_zero(field):
    args=dict(ARGS);del args[field]
    assert calc_pump_head(args).missing_inputs==[field]
    _,obs=invoke(args)
    assert json.loads(obs.content)['error'] and not obs.artifact

@pytest.mark.parametrize('values',[(0,0,0,0),(10,0,0,2),(0,2.8,0,2),(10,2.8,0,0)])
def test_zeros_fixed(values):
    args=dict(zip(ARGS,values,strict=True))
    query='计算泵扬程。'+ '，'.join(f'{k}={v} m' for k,v in args.items())
    result=validate(turn(query,args))
    assert result['status']=='succeeded'
    assert str(gold(values)) in result['messages'][0].content

@pytest.mark.parametrize('location',['payload_input','receipt_args','receipt_result','payload_result','both'])
def test_nested_false_cannot_equal_zero(location):
    from water_agent.tools.calc_pump_head import verified_pump_head_result
    _,obs=invoke();payload=json.loads(obs.content);receipt=copy.deepcopy(obs.artifact[RECEIPT])
    if location=='payload_input':payload['inputs']['local_head_m']=False
    elif location=='receipt_args':receipt['args']['local_head_m']=False
    elif location=='receipt_result':receipt['result']['inputs']['local_head_m']=False
    elif location=='payload_result':payload['pump_head_m']=False
    else:
        payload['inputs']['local_head_m']=False;receipt['result']=payload
    assert not verified_pump_head_result(ARGS,payload,receipt,'pump-head-call',QUERY)

@pytest.mark.parametrize('old,new',[
 ('10 m','10'),('10 m','10 M'),('10 m','10 kPa'),('10 m','1e1 m'),('10 m','-10 m'),
 ('10 m','10% m'),('10 m','10 m kg'),('10 m','1说明0 m'),('10 m','静扬程10 m'),
 ('局部损失扬程0 m','无局部损失'),('静扬程10 m','静扬程局部损失扬程0 m10 m'),
 ('计算泵扬程','解释泵扬程'),('计算泵扬程','不计算泵扬程')])
def test_original_rejections(old,new):
    _,obs=invoke(query=QUERY.replace(old,new))
    assert json.loads(obs.content)['error'] and not obs.artifact

@pytest.mark.parametrize('suffix',['静扬程10 m','H_static=10 m','静扬程','长度10 m','流量10 L/s','g=9.81','kg'])
def test_duplicate_unknown(suffix):
    _,obs=invoke(query=QUERY+suffix)
    assert json.loads(obs.content)['error'] and not obs.artifact

@pytest.mark.parametrize('suffix',['并查询规范','并选择水泵','并另算沿程损失','并计算流速'])
def test_mixed_guard_but_no_fixed(suffix):
    _,obs=invoke(query=QUERY+suffix)
    assert 'pump_head_m' in json.loads(obs.content)
    assert 'no_citation_programmatic_calculation' not in validate(turn(QUERY+suffix)).get('warnings',[])

@pytest.mark.parametrize('old,new',[
 ('泵扬程','PUMP HEAD'),('泵扬程','pump_head'),('静扬程','几何扬程'),('静扬程','H_static'),
 ('沿程损失扬程','沿程水头损失'),('沿程损失扬程','H_friction'),('局部损失扬程','H_local'),
 ('末端余压扬程','出水自由水头'),('末端余压扬程','H_residual')])
def test_aliases(old,new):
    assert validate(turn(QUERY.replace(old,new)))['status']=='succeeded'

@pytest.mark.parametrize('old,new',[('静扬程10 m','静扬程(H_static)=10 m'),('沿程损失扬程2.8 m','沿程损失扬程(friction_head_m)=2.8 m')])
def test_compound_same_field_once(old,new):
    assert validate(turn(QUERY.replace(old,new)))['status']=='succeeded'


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
    queries=['你好','读图统计材料',QUERY, QUERY.replace('。','。\n'), QUERY+'并用Darcy另求沿程损失']
    names={'满管流速':'calc_velocity','Darcy沿程损失':'calc_head_loss_darcy_weisbach',
           '计算实际内径':'calc_diameter','Hazen–Williams沿程水头损失':'calc_head_loss_hazen_williams',
           'Shevelev沿程水头损失':'calc_head_loss_shevelev','舍维列夫沿程水头损失':'calc_head_loss_shevelev',
           'Manning非满管坡度':'calc_slope_manning','曼宁非满管坡度':'calc_slope_manning',
           '计算泵扬程':NAME,'计算PUMP HEAD':NAME,'计算pump_head':NAME,
           'Manning公式非满管实际内径充满度坡度':'calc_slope_manning'}
    for query in names:
        queries.extend(query+suffix for suffix in ['', '和统计材料','和读图','和读图统计材料'])
    queries.extend(['Darcy、Hazen、曼宁非满管坡度，计算实际内径',
                    '满管流速、Darcy、Hazen、Shevelev、曼宁非满管坡度、泵扬程，计算实际内径，读图统计材料'])
    rows={}
    for query in queries:
        d=assembly.exposure.decide(query);rows[query]=d.to_state()
        assert d.schema_chars<=4500 and d.guide_chars<=800
        if query in (QUERY,QUERY.replace('。','。\n')):
            assert NAME in d.exposed and 'calc_head_loss_darcy_weisbach' not in d.exposed
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
    sticky=assembly.exposure.decide('计算泵扬程',previous_turn_tools=['calc_head_loss_darcy_weisbach'])
    assert NAME in sticky.exposed and sticky.schema_chars<=4500
    (tmp_path/'budget.json').write_text(json.dumps(rows,ensure_ascii=False,indent=2))





@pytest.mark.parametrize('query',['按Hazen-Williams计算沿程水头损失','按舍维列夫计算沿程水头损失','计算沿程水头损失\nHazen','计算沿程水头损失\nShevelev'])
def test_other_formula_does_not_open_darcy(query):
    from water_agent.plugins.builtin import DARCY_EXPOSURE
    from water_agent.plugins.exposure import normalize
    assert not DARCY_EXPOSURE.matches(normalize(query))


def test_canonical_overflow_keeps_structured_failure_fields():
    args={k:(1e308 if k in ('static_head_m','friction_head_m') else 0) for k in ARGS}
    query='计算泵扬程。'+ '，'.join(f"{k}={'1'+'0'*308 if v else '0'} m" for k,v in args.items())
    _,obs=invoke(args,query)
    body=json.loads(obs.content)
    assert body['error'] and body['values']=={} and body['errors'] and body['missing_inputs']==[]
    assert body['assumptions'] and body['calculation_note'] and not obs.artifact
