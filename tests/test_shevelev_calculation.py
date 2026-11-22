"""T2-4 frozen §2.3 independent gold and current-turn execution boundaries."""
from __future__ import annotations

import copy
import json
import math
from decimal import Decimal, localcontext

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from water_agent.graphs.agent_rag import make_citation_validator
from water_agent.tools.hydraulics import calc_head_loss_shevelev

NAME = 'calc_head_loss_shevelev'
ARGS = {'length_m': 100, 'diameter_mm': 200, 'velocity_mps': 1.2}
QUERY = ('仅按舍维列夫（Shevelev）公式计算满管有压给水钢管的沿程水头损失，本次水温10°C。'
         '管长100 m，实际内径200 mm，流速1.2 m/s。请调用程序化工具，列明输入、采用的分段公式、'
         '结果及经验近似的适用前提；经验范围来源本次未核验，不检索条文，不选型，不做规范符合性判断。')
RECEIPT = 'water_agent_programmatic_calculation'


def gold(length=100, diameter=200, velocity='1.2'):
    # Independently evaluated with 80 decimal digits, never imports the kernel.
    with localcontext() as ctx:
        ctx.prec = 80
        v = Decimal(str(velocity)); d = Decimal(str(diameter)) / 1000
        i = ((Decimal('.00107') * v**2 / d**Decimal('1.3')) if v >= Decimal('1.2') else
             Decimal('.000912') * v**2 * (1 + Decimal('.867')/v)**Decimal('.3') / d**Decimal('1.3'))
        return i * Decimal(str(length))


def invoke(args=None, query=QUERY):
    from water_agent.tools.calc_shevelev import calc_head_loss_shevelev_spec
    from water_agent.tools.ds_adapters import tool_spec_to_base_tool
    from water_agent.tools.registry import ToolRegistry
    from water_agent.tools.shevelev_request import shevelev_request_scope
    registry = ToolRegistry(); registry.register(calc_head_loss_shevelev_spec)
    call = {'name': NAME, 'args': ARGS if args is None else args, 'id': 'shevelev-call', 'type': 'tool_call'}
    with shevelev_request_scope(query):
        obs = tool_spec_to_base_tool(registry, NAME).invoke(call)
    return call, obs


def turn(query=QUERY, args=None):
    call, obs = invoke(args, query)
    return [HumanMessage(content=query), AIMessage(content='', tool_calls=[call]), obs,
            AIMessage(content='错误草稿999 m，必须选DN200，符合GB50014-2021。', id='final')]


def validate(messages):
    return make_citation_validator()({'messages': messages, 'errors': [], 'warnings': []})


@pytest.mark.parametrize('velocity,expected', [
    ('0.8', '0.589507726723598123177609201585'),
    ('1.199999', '1.252752929382939519481060121443'),
    ('1.2', '1.248553842092104180039909861415'),
    ('1.200001', '1.248555923016041384826107222501'),
    ('2', '3.468205116922511611221971837263'),
])
def test_independent_decimal_gold_and_exact_branch(velocity, expected):
    reference = gold(velocity=velocity)
    assert abs(reference - Decimal(expected)) < Decimal('1e-30')
    args = {**ARGS, 'velocity_mps': float(velocity)}
    _, obs = invoke(args, QUERY.replace('流速1.2 m/s', f'流速{velocity} m/s'))
    payload = json.loads(obs.content)
    value = payload['head_loss_m']
    assert abs(value - float(reference)) <= 1e-12
    assert abs(value/float(reference) - 1) <= 1e-12
    assert payload['branch'] == ('v>=1.2' if Decimal(velocity) >= Decimal('1.2') else 'v<1.2')
    assert payload['inputs'] == args and set(payload['inputs']) == set(ARGS)
    assert obs.artifact[RECEIPT]['tool_call_id'] == 'shevelev-call'
    assert RECEIPT not in obs.content


@pytest.mark.parametrize('changes', [
    {'velocity_mps': 1e308}, {'velocity_mps': 5e-324}, {'velocity_mps': 1e-160},
    {'diameter_mm': 1e308}, {'diameter_mm': 5e-324}, {'diameter_mm': 1e-240},
    {'length_m': 1e308, 'velocity_mps': 200}, {'length_m': 5e-324},
    {'diameter_mm': 1e-240, 'velocity_mps': 1e-150},
    {'diameter_mm': 1e-242, 'velocity_mps': 1e-160},
])
def test_kernel_extreme_or_subnormal_intermediates_fail_closed(changes):
    result = calc_head_loss_shevelev({**ARGS, **changes})
    assert not result.ok and result.errors and not result.values


@pytest.mark.parametrize('diameter,warn', [(40, True), (50, False), (1000, False), (1200, True)])
def test_kernel_warning_preserved_but_d_is_not_nominal_dn(diameter, warn):
    args = {**ARGS, 'diameter_mm': diameter}
    kernel = calc_head_loss_shevelev(args)
    assert kernel.ok and bool(kernel.assumptions) is warn
    messages = turn(QUERY.replace('200 mm', f'{diameter} mm'), args)
    payload = json.loads(messages[2].content)
    assert payload['assumptions'] == kernel.assumptions
    result = validate(messages)
    assert result['status'] == 'succeeded'
    answer = result['messages'][0].content
    assert '实际DN及来源未提供' in answer and '适用性未核验' in answer
    for warning in kernel.assumptions:
        assert warning in answer


@pytest.mark.parametrize('field', list(ARGS))
@pytest.mark.parametrize('value', [True, False, '1.2', None, float('nan'), float('inf'), -1, 0])
def test_invalid_schema_inputs_never_mint_receipt(field, value):
    _, obs = invoke({**ARGS, field: value})
    assert json.loads(obs.content)['error'] is True and not obs.artifact


@pytest.mark.parametrize('field', list(ARGS))
def test_missing_and_model_invented_inputs(field):
    missing = dict(ARGS); del missing[field]
    for args in [missing, {**ARGS, field: ARGS[field]*2}, {**ARGS, 'temperature_c': 10}]:
        _, obs = invoke(args)
        assert json.loads(obs.content)['error'] is True and not obs.artifact


@pytest.mark.parametrize('old,new', [('100 m', '100'), ('100 m', '100 M'), ('200 mm', '200 MM'),
    ('1.2 m/s', '1.2 M/s'), ('1.2 m/s', '1.2 m/S'), ('1.2 m/s', '1.2 km/h'),
    ('流速1.2 m/s', 'v m/s'), ('流速1.2 m/s', 'v=true'), ('流速1.2 m/s', 'v=NaN'),
    ('流速1.2 m/s', 'v=inf'), ('流速1.2 m/s', 'v=-1.2 m/s'),
    ('实际内径200 mm', 'DN200'), ('流速1.2 m/s', '流量1.2 L/s'),
    ('实际内径200 mm', 'D=200 mm'), ('流速1.2 m/s', '速度1.2 m/s')])
def test_invalid_missing_units_and_unsupported_aliases_fail(old, new):
    _, obs = invoke(query=QUERY.replace(old, new))
    assert json.loads(obs.content)['error'] is True and not obs.artifact


@pytest.mark.parametrize('suffix', ['v=1.2 m/s。', '流速1.2 m/s。', 'v=2 m/s。', 'v m/s。',
    'v=1.2 km/h。', 'd=200 MM。', 'L=100 m。', 'length_m=100。', 'diameter_mm=200。',
    'f=0.02。', 'C=130。', 'n=0.013。', 'Q=20 L/s。', '压力0.2 MPa。', '水温10°C。'])
def test_duplicate_same_value_or_unresolved_extra_declarations_fail(suffix):
    _, obs = invoke(query=QUERY+suffix)
    assert json.loads(obs.content)['error'] is True and not obs.artifact


@pytest.mark.parametrize('query', [None, QUERY.replace('仅按舍维列夫', '不要按舍维列夫'),
    QUERY.replace('公式计算', '公式只解释'), QUERY.replace('公式计算', '公式不计算'),
    QUERY.replace('公式计算', '公式'), QUERY.replace('满管有压给水钢管', '非满管有压给水钢管'),
    QUERY.replace('有压', '无压'), QUERY.replace('钢管', 'PVC管'), QUERY.replace('钢管', '钢管和铸铁管')])
def test_absent_or_negated_intent_and_explicit_incompatible_conditions_rejected(query):
    _, obs = invoke(query=query)
    assert json.loads(obs.content)['error'] is True and not obs.artifact


@pytest.mark.parametrize('old,new', [('舍维列夫（Shevelev）','舍维列夫'),
    ('舍维列夫（Shevelev）','Shevelev'), ('舍维列夫（Shevelev）','SHEVELEV'),
    ('管长100 m','L=100米'), ('实际内径200 mm','d=200毫米'), ('流速1.2 m/s','v=1.2米/秒'),
    ('管长100 m','length_m=100 m'), ('实际内径200 mm','diameter_mm=200 mm'),
    ('流速1.2 m/s','velocity_mps=1.2 m/s')])
def test_finite_supported_formula_aliases_parameters_and_units(old,new):
    result=validate(turn(QUERY.replace(old,new)))
    assert result['status']=='succeeded'


@pytest.mark.parametrize('old,new,text', [('本次水温10°C。','','水温未确认'),
    ('本次水温10°C。','本次水温30°C。','30°C'), ('钢管','管材未知','管材未确认'),
    ('满管有压给水钢管','圆管','满管条件未确认')])
def test_unknown_application_and_temperature_never_invent_range(old,new,text):
    result=validate(turn(QUERY.replace(old,new)))
    assert result['status']=='succeeded'
    answer=result['messages'][0].content
    assert text in answer and '适用性未核验' in answer and '10°C附近未定义精确许可区间' in answer


def test_fixed_answer_branch_formula_si_and_all_disclosures():
    result=validate(turn());assert result['status']=='succeeded'
    answer=result['messages'][0].content
    for text in ['v>=1.2', 'v<1.2', '0.00107', '0.000912', '0.867', '1.3', '0.3',
                 '1.248553842092104', 'mm换算为m', '经验近似', '未核验', '钢管', '10°C']:
        assert text in answer
    assert '999' not in answer and 'GB50014' not in answer


@pytest.mark.parametrize('suffix', ['并查询规范。','并选择管材。','并计算流速。','并判断是否符合规范。'])
def test_mixed_requests_do_not_gain_fixed_answer(suffix):
    assert 'no_citation_programmatic_calculation' not in validate(turn(QUERY+suffix)).get('warnings',[])


@pytest.mark.parametrize('mutation', ['artifact','body','call_id','receipt_id','receipt_name','args',
    'extra_call','new_human','empty','no_final_id','failed','assumptions','both_result','query'])
def test_receipt_body_and_turn_tamper_rejected(mutation):
    messages=copy.deepcopy(turn());obs=messages[2]
    if mutation=='artifact':obs.artifact=None
    elif mutation=='body':obs.content=obs.content.replace('1.248553','99.248553')
    elif mutation=='call_id':obs.tool_call_id='other'
    elif mutation=='receipt_id':obs.artifact[RECEIPT]['tool_call_id']='other'
    elif mutation=='receipt_name':obs.artifact[RECEIPT]['tool_name']='calc_velocity'
    elif mutation=='args':messages[1].tool_calls[0]['args']['length_m']=200
    elif mutation=='extra_call':messages.insert(3,AIMessage(content='',tool_calls=[{'name':'retrieve_clauses','args':{'query':'x'},'id':'extra'}]))
    elif mutation=='new_human':messages.insert(-1,HumanMessage(content=QUERY))
    elif mutation=='empty':messages[-1].content=''
    elif mutation=='no_final_id':messages[-1].id=None
    elif mutation=='failed':obs.status='error'
    elif mutation=='query':obs.artifact[RECEIPT]['query']=QUERY.replace('钢管','铸铁管')
    else:
        payload=json.loads(obs.content)
        if mutation=='assumptions':payload['assumptions']=['made up']
        else:payload['head_loss_m']=42
        obs.content=json.dumps(payload);obs.artifact[RECEIPT]['result']=payload
    assert 'no_citation_programmatic_calculation' not in validate(messages).get('warnings',[])


def test_extreme_valid_length_meets_independent_precision():
    result=calc_head_loss_shevelev({**ARGS,'length_m':1e308})
    reference=float(gold(length='1e308'))
    assert result.ok and abs(result.values['head_loss_m']/reference-1)<=1e-12


@pytest.mark.parametrize('location',['payload_input','receipt_args','receipt_result','payload_result'])
def test_recursive_boolean_receipt_rejection(location):
    from water_agent.tools.calc_shevelev import verified_shevelev_result
    args={**ARGS,'length_m':1};query=QUERY.replace('100 m','1 m')
    _,obs=invoke(args,query);payload=json.loads(obs.content);receipt=copy.deepcopy(obs.artifact[RECEIPT])
    if location=='payload_input':payload['inputs']['length_m']=True
    elif location=='receipt_args':receipt['args']['length_m']=True
    elif location=='receipt_result':receipt['result']['inputs']['length_m']=True
    else:payload['head_loss_m']=True
    assert not verified_shevelev_result(args,payload,receipt,'shevelev-call',query)


def test_receipt_requires_observer_current_query_and_is_private_after_serde():
    from langchain_openai.chat_models.base import _convert_message_to_dict

    from water_agent.session import default_checkpoint_serde
    from water_agent.tools.calc_shevelev import verified_shevelev_result
    messages=turn();obs=messages[2];payload=json.loads(obs.content)
    assert not verified_shevelev_result(ARGS,payload,obs.artifact[RECEIPT],'shevelev-call',None)
    assert not verified_shevelev_result(ARGS,payload,obs.artifact[RECEIPT],'shevelev-call',QUERY.replace('10°C','30°C'))
    assert RECEIPT not in json.dumps(_convert_message_to_dict(obs))
    serde=default_checkpoint_serde()
    assert validate(serde.loads_typed(serde.dumps_typed(messages)))['status']=='succeeded'


def test_same_name_external_and_unauthorized_registry_never_mint_receipt():
    from water_agent.runtime.governance.contracts import Principal
    from water_agent.tools.base import ToolSpec
    from water_agent.tools.calc_shevelev import calc_head_loss_shevelev_spec as spec
    from water_agent.tools.ds_adapters import tool_spec_to_base_tool
    from water_agent.tools.registry import ToolRegistry
    from water_agent.tools.shevelev_request import shevelev_request_scope
    for registry,tool in [(ToolRegistry(),ToolSpec(name=NAME,description=spec.description,args_model=spec.args_model,execute=lambda _: {'head_loss_m':1})),
                          (ToolRegistry(principal=Principal.local('denied',capabilities=frozenset())),spec)]:
        registry.register(tool)
        with shevelev_request_scope(QUERY):
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
    assert seen and '1.248553842092104' in seen[0] and '999' not in seen[0]
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
    assert final['status']=='succeeded' and '1.248553842092104' in final['answer']
    calculations=[e['payload']['calculation'] for e in events if e['type']=='tool_result' and 'calculation' in e['payload']]
    assert len(calculations)==1 and calculations[0]['tool_name']==NAME
    assert calculations[0]['values']['head_loss_m']==pytest.approx(float(gold()),rel=1e-12)
    assert 'v>=1.2' in ' '.join(calculations[0]['assumptions'])
    assert RECEIPT not in json.dumps(events,ensure_ascii=False)


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
    assembly=assemble_tools(AssemblyContext(retriever=InMemoryRetriever([]),drawing_root=tmp_path),base_principal=Principal.local('shevelev-budget'),trace=None,plugins=builtin_plugins(mcp=McpGatewayPlugin.borrowing(None)))
    queries=['你好','读图统计材料']
    names={'满管流速':'calc_velocity','Darcy沿程损失':'calc_head_loss_darcy_weisbach',
           '计算实际内径':'calc_diameter','Hazen–Williams沿程水头损失':'calc_head_loss_hazen_williams',
           'Shevelev沿程水头损失':NAME,'舍维列夫沿程水头损失':NAME,
           'Shevelev公式v>=1.2流速分支':NAME}
    for query in names:
        queries.extend(query+suffix for suffix in ['', '和统计材料','和读图','和读图统计材料'])
    queries.extend(['Darcy、Hazen、舍维列夫沿程水头损失，计算实际内径',
                    '满管流速、Darcy、Hazen、舍维列夫沿程水头损失，计算实际内径，读图统计材料'])
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
    sticky=assembly.exposure.decide('Shevelev沿程水头损失',previous_turn_tools=['calc_head_loss_darcy_weisbach'])
    assert NAME in sticky.exposed and sticky.schema_chars<=4500
    (tmp_path/'budget.json').write_text(json.dumps(rows,ensure_ascii=False,indent=2))


@pytest.mark.parametrize('condition,declared,unknown',[
    ('满管钢管','用户声明满管','有压条件未确认'),
    ('有压钢管','用户声明有压','满管条件未确认'),
    ('给水钢管','用户声明给水','满管条件未确认'),
])
def test_review_partial_user_conditions_disclosed_individually(condition,declared,unknown):
    messages=turn(QUERY.replace('满管有压给水钢管',condition))
    result=validate(messages)
    assert result['status']=='succeeded'
    assert declared in result['messages'][0].content
    assert unknown in result['messages'][0].content
    payload=json.loads(messages[2].content)
    assert declared in payload['calculation_note'] and unknown in payload['calculation_note']


@pytest.mark.parametrize('suffix',['实际内径不等于DN。','内径不是DN。','不由DN推实际内径。'])
def test_semantic_diameter_disclosure_is_not_duplicate_parameter(suffix):
    assert validate(turn(QUERY+suffix))['status']=='succeeded'


@pytest.mark.parametrize('velocity',[math.nextafter(1.2,0),math.nextafter(1.2,2)])
def test_adjacent_float_branch_no_rounding_or_epsilon(velocity):
    args={**ARGS,'velocity_mps':velocity};query=QUERY.replace('流速1.2 m/s',f'流速{velocity} m/s')
    _,obs=invoke(args,query);payload=json.loads(obs.content)
    assert payload['branch']==('v>=1.2' if velocity>=1.2 else 'v<1.2')
    assert abs(payload['head_loss_m']/float(gold(velocity=str(velocity)))-1)<=1e-12


@pytest.mark.parametrize('query',[
    QUERY.replace('给水钢管','给水').replace('管长100 m','管长10钢管0 m'),
    QUERY.replace('本次水温10°C。','').replace('给水钢管','给水').replace('流速1.2 m/s','流速1水温10°C.2 m/s'),
    QUERY.replace('管长100 m，实际内径200 mm','实际内径管长100 m 200 mm'),
    QUERY.replace('实际内径200 mm，流速1.2 m/s','流速实际内径200 mm 1.2 m/s'),
    QUERY.replace('管长100 m','管长100 水温10°C m').replace('本次水温10°C。',''),
])
def test_review_removing_metadata_or_other_fields_cannot_create_a_declaration(query):
    _,obs=invoke(query=query)
    assert json.loads(obs.content)['error'] is True and not obs.artifact


def test_numerically_failed_tool_preserves_existing_kernel_warning():
    args={**ARGS,'diameter_mm':1e-240}
    query=QUERY.replace('200 mm','0.'+'0'*239+'1 mm')
    _,obs=invoke(args,query)
    payload=json.loads(obs.content)
    assert payload['error'] is True and not obs.artifact
    assert payload['assumptions']==calc_head_loss_shevelev(args).assumptions
    assert payload['assumptions'] and '适用性未核验' in payload['calculation_note']
