"""T2-3 independent SI gold, hostile declarations and production trust boundaries."""
from __future__ import annotations

import copy
import json
from decimal import Decimal, localcontext

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from water_agent.graphs.agent_rag import make_citation_validator
from water_agent.tools.hydraulics import calc_head_loss_hazen_williams

NAME = 'calc_head_loss_hazen_williams'
ARGS = {'length_m': 100, 'diameter_mm': 200, 'flow_lps': 20, 'hazen_williams_c': 130}
QUERY = ('仅按Hazen–Williams的SI公式计算满管有压给水圆管的沿程水头损失，本次水温20°C。'
         '管长100 m，实际内径200 mm，流量20 L/s，Hazen–Williams系数C=130'
         '（无量纲，由用户给定，本次不核验或推导系数）。'
         '请调用程序化工具，列明输入、公式、结果及经验近似的适用前提，不检索条文，不选型，不做规范符合性判断。')
RECEIPT = 'water_agent_programmatic_calculation'


def invoke(args=None, query=QUERY):
    from water_agent.tools.calc_hazen import calc_head_loss_hazen_williams_spec
    from water_agent.tools.ds_adapters import tool_spec_to_base_tool
    from water_agent.tools.hazen_request import hazen_request_scope
    from water_agent.tools.registry import ToolRegistry
    registry = ToolRegistry(); registry.register(calc_head_loss_hazen_williams_spec)
    call = {'name': NAME, 'args': ARGS if args is None else args, 'id': 'hazen-call', 'type': 'tool_call'}
    with hazen_request_scope(query):
        observation = tool_spec_to_base_tool(registry, NAME).invoke(call)
    return call, observation


def turn(query=QUERY, args=None):
    call, obs = invoke(args, query)
    return [HumanMessage(content=query), AIMessage(content='', tool_calls=[call]), obs,
            AIMessage(content='错误草稿：水头损失999 m，必须选DN200，符合GB50014-2021。', id='final')]


def validate(messages):
    return make_citation_validator()({'messages': messages, 'errors': [], 'warnings': []})


@pytest.mark.parametrize('coefficient,expected', [(130, '0.234915888948175562332970741352'),
                                                 (150, '0.180224765345958925540559436635')])
def test_decimal_si_gold_independent_of_kernel(coefficient, expected):
    # Formula provenance: docs/design/09-hydraulics-formulas.md §2.2, fixed SI exponents.
    with localcontext() as ctx:
        ctx.prec = 60
        reference = (Decimal('10.67') * 100 * Decimal('.02') ** Decimal('1.852') /
                     (Decimal(coefficient) ** Decimal('1.852') * Decimal('.2') ** Decimal('4.8704')))
        assert abs(reference - Decimal(expected)) < Decimal('1e-30')
    _, obs = invoke({**ARGS, 'hazen_williams_c': coefficient}, QUERY.replace('C=130', f'C={coefficient}'))
    payload = json.loads(obs.content)
    assert payload['head_loss_m'] == pytest.approx(float(reference), rel=1e-14)
    assert payload['inputs'] == {**ARGS, 'hazen_williams_c': coefficient}
    assert obs.artifact[RECEIPT]['tool_call_id'] == 'hazen-call'
    assert RECEIPT not in obs.content


@pytest.mark.parametrize('changes', [{'flow_lps': 1e308}, {'hazen_williams_c': 1e308},
    {'diameter_mm': 1e308}, {'diameter_mm': 5e-324}, {'flow_lps': 5e-324},
    {'hazen_williams_c': 5e-324}, {'length_m': 5e-324}, {'length_m': 1e308},
    {'flow_lps': 1e-200}, {'diameter_mm': 1e-100}])
def test_kernel_extreme_intermediates_structured_failure(changes):
    result = calc_head_loss_hazen_williams({**ARGS, **changes})
    assert not result.ok and result.errors and not result.values


@pytest.mark.parametrize('field', list(ARGS))
@pytest.mark.parametrize('value', [True, False, '20', None, float('nan'), float('inf'), -1, 0])
def test_invalid_tool_args_never_mint_receipt(field, value):
    _, obs = invoke({**ARGS, field: value})
    assert json.loads(obs.content)['error'] is True and not obs.artifact


@pytest.mark.parametrize('field', list(ARGS))
def test_missing_and_invented_inputs(field):
    missing = dict(ARGS); del missing[field]
    for args in [missing, {**ARGS, field: ARGS[field] * 2}, {**ARGS, 'temperature_c': 20}]:
        _, obs = invoke(args)
        assert json.loads(obs.content)['error'] is True and not obs.artifact


@pytest.mark.parametrize('old,new', [('100 m', '100'), ('100 m', '100 M'), ('200 mm', '200 MM'),
    ('20 L/s', '20 L/S'), ('20 L/s', '20 m³/h'), ('C=130', 'C=true'), ('C=130', 'C=NaN'),
    ('C=130', 'C=inf'), ('C=130', 'C=-130'), ('C=130', 'C=0'), ('实际内径200 mm', 'DN200'),
    ('流量20 L/s', '流速20 m/s'), ('Hazen–Williams系数C=130', 'Darcy f=0.02'),
    ('Hazen–Williams系数C=130', '曼宁n=0.013'), ('Hazen–Williams系数C=130', '按PVC猜C')])
def test_missing_ambiguous_units_or_parameters_fail(old, new):
    _, obs = invoke(query=QUERY.replace(old, new))
    assert json.loads(obs.content)['error'] is True and not obs.artifact


@pytest.mark.parametrize('suffix', ['C=130。', 'C=150。', 'c=true。', 'hazen_williams_c=150。',
    'Q=20 L/s。', 'flow_lps=21。', 'd=200 MM。', 'diameter_mm=201。', 'L=100 m。',
    'length_m=101。', 'f=0.02。', 'n=0.013。', 'v=2 m/s。'])
def test_duplicate_or_unresolved_alias_declarations_fail(suffix):
    _, obs = invoke(query=QUERY+suffix)
    assert json.loads(obs.content)['error'] is True and not obs.artifact


@pytest.mark.parametrize('query', [None, 'Hazen–Williams数据供参考。管长100 m，实际内径200 mm，流量20 L/s，C=130。',
    QUERY.replace('公式计算', '公式不要计算'), QUERY.replace('仅按Hazen', '不要按Hazen'),
    QUERY.replace('公式计算', '公式不计算'), QUERY.replace('公式计算', '公式只解释')])
def test_no_current_or_negated_calculation_intent_fails(query):
    _, obs = invoke(query=query)
    assert json.loads(obs.content)['error'] is True and not obs.artifact


def test_fixed_prose_and_application_metadata_are_not_calculation_inputs():
    result = validate(turn())
    assert result['status'] == 'succeeded'
    answer = result['messages'][0].content
    for text in ['10.67', '1.852', '4.8704', '0.234915888948175', '经验近似', '用户给定', '未核验', '20', '10–20']:
        assert text in answer
    assert '999' not in answer and 'DN200' not in answer and 'GB50014' not in answer
    _, obs = invoke(); assert set(json.loads(obs.content)['inputs']) == set(ARGS)


@pytest.mark.parametrize('old,new,disclosure', [('本次水温20°C。', '', '未确认'),
    ('本次水温20°C。', '本次水温30°C。', '超出'),
    ('满管有压给水圆管', '圆管', '未确认')])
def test_unknown_or_out_of_range_application_is_disclosed(old, new, disclosure):
    result = validate(turn(QUERY.replace(old, new)))
    assert result['status'] == 'succeeded'
    assert disclosure in result['messages'][0].content


@pytest.mark.parametrize('suffix', ['并查询规范。', '并选择管材。', '并计算流速。', '并判断是否符合规范。'])
def test_mixed_tasks_cannot_use_fixed_answer(suffix):
    result = validate(turn(QUERY+suffix))
    assert result.get('status') != 'succeeded'
    assert 'no_citation_programmatic_calculation' not in result.get('warnings', [])


@pytest.mark.parametrize('mutation', ['artifact', 'body_only', 'id', 'receipt_id', 'receipt_name',
    'args', 'result', 'extra_call', 'history', 'empty', 'no_final_id', 'assumptions', 'failed'])
def test_private_receipt_and_current_turn_counterexamples(mutation):
    messages=copy.deepcopy(turn());obs=messages[2]
    if mutation=='artifact': obs.artifact=None
    elif mutation=='body_only': obs.artifact=None; obs.content=json.dumps({RECEIPT:ARGS})
    elif mutation=='id': obs.tool_call_id='other'
    elif mutation=='receipt_id': obs.artifact[RECEIPT]['tool_call_id']='other'
    elif mutation=='receipt_name': obs.artifact[RECEIPT]['tool_name']='calc_velocity'
    elif mutation=='args': messages[1].tool_calls[0]['args']['flow_lps']=40
    elif mutation=='result': obs.content=obs.content.replace('0.234915888948175', '99.234915888948175')
    elif mutation=='extra_call': messages.insert(3, AIMessage(content='',tool_calls=[{'name': 'retrieve_clauses','args': {'query':'x'},'id': 'extra'}]))
    elif mutation=='history': messages.insert(-1, HumanMessage(content=QUERY))
    elif mutation=='empty': messages[-1].content=''
    elif mutation=='no_final_id': messages[-1].id=None
    elif mutation=='assumptions':
        payload=json.loads(obs.content);payload['assumptions']=['assume'];obs.content=json.dumps(payload);obs.artifact[RECEIPT]['result']=payload
    elif mutation=='failed': obs.status='error'
    assert 'no_citation_programmatic_calculation' not in validate(messages).get('warnings',[])


@pytest.mark.parametrize('location', ['payload_input','receipt_args','receipt_result','payload_result'])
def test_nested_booleans_cannot_compare_equal_to_numeric_one(location):
    from water_agent.tools.calc_hazen import verified_hazen_result
    args={**ARGS,'length_m':1};_,obs=invoke(args,QUERY.replace('100 m','1 m'))
    payload=json.loads(obs.content);receipt=copy.deepcopy(obs.artifact[RECEIPT])
    if location=='payload_input':payload['inputs']['length_m']=True
    elif location=='receipt_args':receipt['args']['length_m']=True
    elif location=='receipt_result':receipt['result']['inputs']['length_m']=True
    else:payload['head_loss_m']=True
    assert not verified_hazen_result(args,payload,receipt,'hazen-call')


def test_same_name_external_tool_and_authorization_denial():
    from water_agent.runtime.governance.contracts import Principal
    from water_agent.tools.base import ToolSpec
    from water_agent.tools.calc_hazen import calc_head_loss_hazen_williams_spec as spec
    from water_agent.tools.ds_adapters import tool_spec_to_base_tool
    from water_agent.tools.registry import ToolRegistry
    for registry,tool in [(ToolRegistry(),ToolSpec(name=NAME,description=spec.description,args_model=spec.args_model,execute=lambda _: {'head_loss_m':1})),
                          (ToolRegistry(principal=Principal.local('denied',capabilities=frozenset())),spec)]:
        registry.register(tool)
        obs=tool_spec_to_base_tool(registry,NAME).invoke({'name': NAME,'args': ARGS,'id': 'x','type': 'tool_call'})
        assert not obs.artifact


def test_receipt_private_and_checkpoint_roundtrip():
    from langchain_openai.chat_models.base import _convert_message_to_dict

    from water_agent.session import default_checkpoint_serde
    messages=turn();assert RECEIPT not in json.dumps(_convert_message_to_dict(messages[2]))
    serde=default_checkpoint_serde()
    assert validate(serde.loads_typed(serde.dumps_typed(messages)))['status']=='succeeded'


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
    assert final['status']=='succeeded' and '0.234915888948175' in final['answer']
    calculations=[e['payload']['calculation'] for e in events if e['type']=='tool_result' and 'calculation' in e['payload']]
    assert len(calculations)==1 and calculations[0]['tool_name']==NAME
    assert calculations[0]['values']['head_loss_m']==pytest.approx(.23491588894817556)
    assert RECEIPT not in json.dumps(events,ensure_ascii=False)


def test_budget_matrix_and_explicit_limited_combinations(tmp_path):
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
    assembly=assemble_tools(AssemblyContext(retriever=InMemoryRetriever([]),drawing_root=tmp_path),base_principal=Principal.local('hazen-budget'),trace=None,plugins=builtin_plugins(mcp=McpGatewayPlugin.borrowing(None)))
    queries=['你好','满管流速','Darcy沿程损失','计算实际内径','Hazen–Williams沿程水头损失','读图统计材料']
    for formula in ['满管流速','Darcy沿程损失','计算实际内径','Hazen–Williams沿程水头损失']:
        queries.extend(formula+suffix for suffix in ['和统计材料','和读图','和读图统计材料'])
    queries.extend(['Darcy和Hazen–Williams沿程水头损失','计算实际内径和Hazen–Williams沿程水头损失',
                    'Darcy和计算实际内径和Hazen–Williams沿程水头损失、读图统计材料'])
    rows={}
    for query in queries:
        d=assembly.exposure.decide(query);rows[query]=d.to_state()
        assert d.schema_chars<=4500 and d.guide_chars<=800
        if query=='Hazen–Williams沿程水头损失':
            assert NAME in d.exposed and 'calc_head_loss_darcy_weisbach' not in d.exposed
        if 'Hazen' not in query:assert NAME not in d.exposed
        if query=='读图统计材料':assert {'material_stats','read_drawing'}<=set(d.exposed)
        expected={NAME} if 'Hazen' in query else set()
        if 'Darcy' in query:expected.add('calc_head_loss_darcy_weisbach')
        if '实际内径' in query:expected.add('calc_diameter')
        if not expected<=set(d.exposed):assert d.dropped
        if d.dropped:
            app=build_agentic_app(model=FakeMessagesListChatModel(responses=[AIMessage(content='请拆分请求。')]),tools=assembly.tools,exposure=assembly.exposure,checkpointer=MemorySaver())
            result=app.invoke({'messages':[HumanMessage(content=query)]},{'configurable':{'thread_id':query}})
            assert any('TOOL_EXPOSURE_LIMITED' in w for w in result['warnings'])
    (tmp_path/'budget.json').write_text(json.dumps(rows,ensure_ascii=False,indent=2))


def test_output_guardrail_can_block_fixed_answer(monkeypatch):
    from water_agent.graphs.agent_rag import BLOCKED_OUTPUT_PLACEHOLDER, make_finalize
    from water_agent.runtime.governance.pipeline import default_guardrail_pipeline
    messages = turn(); checked = validate(messages); messages[-1] = checked["messages"][0]
    pipeline = default_guardrail_pipeline(); original = pipeline.check_output; seen = []

    def block(answer, context, **_):
        seen.append(answer)
        return original(answer, context, citations_verified=False)

    monkeypatch.setattr(pipeline, "check_output", block)
    final = make_finalize(pipeline=pipeline)({**checked, "messages": messages})
    assert seen and "0.234915888948175" in seen[0] and "999" not in seen[0]
    assert final["status"] == "blocked" and final["answer"] == ""
    assert final["messages"][0].content == BLOCKED_OUTPUT_PLACEHOLDER


def test_stream_checkpoint_restart_and_current_turn_isolation(tmp_path):
    from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
    from langgraph.checkpoint.sqlite import SqliteSaver

    from water_agent.graphs.agent_rag import build_agentic_app, stream_agentic
    from water_agent.session import default_checkpoint_serde
    from water_agent.tools.calc_hazen import calc_head_loss_hazen_williams_spec
    from water_agent.tools.ds_adapters import tool_spec_to_base_tool
    from water_agent.tools.registry import ToolRegistry
    registry = ToolRegistry(); registry.register(calc_head_loss_hazen_williams_spec)
    model = FakeMessagesListChatModel(responses=[AIMessage(content="", tool_calls=[{"name": NAME, "args": ARGS, "id": "stream"}]), AIMessage(content="999 m")])
    config = {"configurable": {"thread_id": "hazen-restart"}}
    tools = [tool_spec_to_base_tool(registry, NAME)]
    with SqliteSaver.from_conn_string(str(tmp_path/"cp.sqlite")) as saver:
        saver.serde = default_checkpoint_serde()
        app = build_agentic_app(model=model, tools=tools, checkpointer=saver)
        stream_agentic(app, QUERY, config=config)
        state = app.get_state(config).values
        assert state["status"] == "succeeded"
        assert "0.234915888948175" in state["answer"]
    with SqliteSaver.from_conn_string(str(tmp_path/"cp.sqlite")) as saver:
        saver.serde = default_checkpoint_serde()
        app = build_agentic_app(model=model, tools=tools, checkpointer=saver)
        restored = app.get_state(config).values
        assert restored["answer"] == state["answer"]
        assert restored["messages"][-1].additional_kwargs["water_agent_turn_status"] == "succeeded"
        messages = restored["messages"] + [HumanMessage(content=QUERY), AIMessage(content="999 m", id="later")]
        assert validate(messages).get("status") != "succeeded"



@pytest.mark.parametrize('old,new', [('C=130','C=130 m'),('C=130','C=130 kg'),
    ('C=130','C=130（m）'),('无量纲','不是无量纲'),
    ('满管有压给水圆管','非满管有压给水圆管')])
def test_review_dimensioned_c_and_negated_conditions_fail_closed(old,new):
    _,obs=invoke(query=QUERY.replace(old,new))
    assert json.loads(obs.content)['error'] is True and not obs.artifact


@pytest.mark.parametrize('flow', ['3e-172', '1e-171', '5e-172'])
def test_review_positive_subnormal_power_is_not_a_success(flow):
    # Positive IEEE subnormals lose significant digits: final finite > 0 is insufficient.
    with localcontext() as ctx:
        ctx.prec=70
        reference=(Decimal('10.67')*100*(Decimal(flow)/1000)**Decimal('1.852') /
                   (Decimal(130)**Decimal('1.852')*(Decimal('1e-60')/1000)**Decimal('4.8704')))
        assert reference.is_finite() and reference>0
    result=calc_head_loss_hazen_williams({**ARGS,'diameter_mm':1e-60,'flow_lps':float(flow)})
    assert not result.ok and result.errors and not result.values
