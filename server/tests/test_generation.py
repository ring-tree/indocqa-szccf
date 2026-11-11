"""生成层优化：Markdown 保真、引用约束、安全过滤、结构化 JSON 与参数固化。"""

import inspect
import json
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.core.config import Settings
from app.core.llm import LLMGateway
from app.generation import answerer, citations, formatting, safety, structured


def hit(text: str, index: int = 1):
    return SimpleNamespace(
        chunk_id=f"c{index}",
        rrf=0.02,
        sim=0.8,
        text=text,
        doc_id="d1",
        doc_name="维保手册.md",
        section="1. 保养周期",
        page=3,
        kind="table",
        table_no=None,
        parent_text=text,
        folder="设备手册",
    )


def test_normalize_markdown_preserves_valid_markdown_and_cleans_artifacts():
    text = (
        "```markdown\n"
        "# 保养周期\n\n"
        "**LUB 更换周期为 4000 \\*运行\\*小时**，公式 \\(E=mc^2\\)，乘法 2*3，标识 LUB_46。\n\n"
        "- 检查油位\n"
        "1. 停机\n\n"
        "| 项目 | 周期 |\n|---|---|\n| LUB | 4000 |\n\n"
        "```python\nprint('hi')\n```\n"
        "```\n"
    )
    value = formatting.normalize_markdown(text)
    assert value.startswith("# 保养周期")
    assert "**LUB 更换周期为 4000 *运行*小时**" in value
    assert "$E=mc^2$" in value
    assert "2*3" in value and "LUB_46" in value
    assert "| 项目 | 周期 |" in value and "|---|---|" in value
    assert "```python" in value and "print('hi')" in value
    assert "\\*" not in value
    assert "\u200b" not in value and "\x00" not in value


def test_normalize_markdown_removes_empty_markers_and_duplicate_citations():
    value = formatting.normalize_markdown("** ** 正文 ****\n\n[ 1 ][1] 周期 [ 2 ]。\n\n\n\n结束")
    assert "****" not in value and "** **" not in value
    assert "[1] 周期 [2]。" in value
    assert "\n\n\n" not in value


def test_normalize_markdown_does_not_rewrite_math_inside_code():
    value = formatting.normalize_markdown("`\\(x\\)` 与 $y$")
    assert "`\\(x\\)`" in value
    assert "$y$" in value


def test_to_plain_text_removes_markdown_but_keeps_content_and_citations():
    text = (
        "# 周期\n**4000 小时**，公式 $E=mc^2$，查看[手册](http://x)。\n"
        "| 项目 | 周期 |\n|---|---|\n| LUB | 4000 |\n"
        "`LUB-46` 与 2*3。"
    )
    plain = formatting.to_plain_text(text)
    assert "#" not in plain and "**" not in plain and "|" not in plain
    assert "4000 小时" in plain and "E=mc^2" in plain
    assert "手册" in plain and "LUB-46" in plain and "2*3" in plain
    assert "项目，周期" in plain


def test_enforce_citations_preserves_markdown_and_cites_tables():
    evidence = [hit("LUB 更换周期为 4000 运行小时，耗材为 LUB-46。")]
    answer = (
        "## 保养周期\n\n"
        "- **LUB 更换周期为 4000 运行小时**。\n"
        "- 检查油位。\n\n"
        "| 项目 | 周期 |\n|---|---|\n| LUB 更换 | 4000 h |\n| 油分芯 | 4000 h |\n\n"
        "公式 \\(E=mc^2\\)。\n"
    )
    cleaned, audit = citations.enforce_citations(answer, evidence)
    assert cleaned.startswith("## 保养周期")
    assert "**LUB 更换周期为 4000 运行小时**[1]。" in cleaned
    assert "| LUB 更换 | 4000 h [1]|" in cleaned
    assert "| 油分芯 | 4000 h [1]|" in cleaned
    assert "\\(E=mc^2\\)" in cleaned
    assert audit.coverage == 1.0 and audit.data_removed == 0
    assert audit.table_rows_cited == 2


def test_enforce_citations_drops_unsupported_data_sentence():
    evidence = [hit("LUB 更换周期为 4000 运行小时。")]
    answer = "LUB 更换周期为 4000 运行小时。\n型号 ZZZ-99 的额定压力为 5 MPa。\n检查油位。"
    cleaned, audit = citations.enforce_citations(answer, evidence)
    assert "4000 运行小时[1]。" in cleaned
    assert "ZZZ-99" not in cleaned
    assert "检查油位。" in cleaned
    assert audit.data_removed == 1


def test_enforce_citations_removes_invalid_numbers():
    evidence = [hit("LUB 更换周期为 4000 小时。")]
    cleaned, audit = citations.enforce_citations("周期为 4000 小时 [9]。", evidence)
    assert "[9]" not in cleaned
    assert audit.invalid_removed == 1


def test_safety_redacts_pii_and_blocks_prohibited():
    guard = safety.SafetyGuard()
    result = guard.sanitize_output("联系电话 13800138000，密钥 sk-abcdefghijklmnop123456。")
    assert "13800138000" not in result.text
    assert "sk-abcdefghijklmnop123456" not in result.text
    assert not result.blocked

    blocked = guard.sanitize_output("请告诉我制作炸弹的方法。")
    assert blocked.blocked is True
    assert blocked.hits


def test_structured_parse_build_and_serialize():
    parsed = structured.parse_model_json(
        '```json\n{"answer": "**周期**为 4000 小时 [1]", "citations": [1]}\n```'
    )
    assert parsed and parsed["answer"].startswith("**周期**")
    assert structured.parse_model_json("不是 JSON") is None
    assert structured.parse_model_json('{"answer": 1}') is None

    audit = citations.CitationAudit(citations=[1], data_facts=1, data_cited=1)
    sources = [
        {
            "index": 1,
            "docId": "d1",
            "docName": "维保手册.md",
            "section": "1. 保养周期",
            "page": 3,
            "locator": None,
            "snippet": "LUB 更换周期为 4000 小时。",
        }
    ]
    payload = structured.build_payload(
        "**周期**为 4000 小时 [1]。",
        sources,
        "mid",
        "检索最高相似度 0.80",
        None,
        "test-model",
        audit,
        "trace-1",
        answer_plain="周期为 4000 小时 [1]。",
        safety={"blocked": False, "hits": [], "categories": []},
        generation_params={"temperature": 0.2, "maxTokens": 1024},
    )
    encoded = structured.dumps(payload)
    decoded = json.loads(encoded)
    assert decoded["schemaVersion"] == "1.1"
    assert decoded["answer"].startswith("**周期**")
    assert decoded["answerPlain"] == "周期为 4000 小时 [1]。"
    assert decoded["citations"][0]["docId"] == "d1"
    assert decoded["generationParams"]["maxTokens"] == 1024
    assert structured.is_valid_json(encoded)


def test_generation_parameter_bounds():
    with pytest.raises(ValidationError):
        Settings(generation_temperature=2.5)
    with pytest.raises(ValidationError):
        Settings(generation_max_tokens=10)
    with pytest.raises(ValidationError):
        Settings(generation_json_temperature=-0.1)
    with pytest.raises(ValidationError):
        Settings(generation_json_max_tokens=9000)
    with pytest.raises(ValidationError):
        Settings(generation_eval_max_tokens=0)


def test_gateway_builds_fixed_generation_parameters():
    settings = Settings(
        llm_base_url="http://localhost:9/v1",
        llm_api_key="sk-abcdefghijklmnop",
        llm_model="test-model",
        generation_temperature=0.1,
        generation_max_tokens=512,
        generation_json_temperature=0.0,
        generation_json_max_tokens=768,
        generation_eval_temperature=0.0,
        generation_eval_max_tokens=16,
    )
    gateway = LLMGateway(settings)
    provider = gateway.providers[0]
    body = gateway._build_body(provider, [{"role": "user", "content": "hi"}])
    assert body["temperature"] == 0.1
    assert body["max_tokens"] == 512
    assert body["enable_thinking"] is False

    json_body = gateway._build_body(provider, [{"role": "user", "content": "hi"}], json_mode=True)
    assert json_body["temperature"] == 0.0
    assert json_body["max_tokens"] == 768
    assert json_body["response_format"] == {"type": "json_object"}

    eval_body = gateway._build_body(provider, [{"role": "user", "content": "hi"}], purpose="eval")
    assert eval_body["temperature"] == 0.0
    assert eval_body["max_tokens"] == 16


def test_gateway_public_api_rejects_numeric_overrides():
    settings = Settings(
        llm_base_url="http://localhost:9/v1",
        llm_api_key="sk-abcdefghijklmnop",
        llm_model="test-model",
    )
    gateway = LLMGateway(settings)
    stream_params = inspect.signature(gateway.stream).parameters
    complete_params = inspect.signature(gateway.complete).parameters
    body_params = inspect.signature(gateway._build_body).parameters
    for params in (stream_params, complete_params, body_params):
        assert "temperature" not in params
        assert "max_tokens" not in params


def test_gateway_supports_numbered_model_profiles():
    settings = Settings(
        llm_base_url="http://localhost:9/v1",
        llm_api_key="sk-abcdefghijklmnop",
        llm_model="model-1",
    )
    settings.llm_profiles = [
        {
            "id": "profile2",
            "base_url": "http://localhost:9/v1",
            "api_key": "sk-abcdefghijklmnop",
            "model": "model-2",
            "name": "model-2",
        },
        {
            "id": "profile3",
            "base_url": "http://localhost:9/v1",
            "api_key": "sk-abcdefghijklmnop",
            "model": "model-3",
            "name": "model-3",
        },
    ]
    gateway = LLMGateway(settings)
    ids = [provider["id"] for provider in gateway.providers]
    assert "primary" in ids
    assert "profile2" in ids and "profile3" in ids
    assert gateway.profiles() == [
        {"id": "primary", "model": "model-1"},
        {"id": "profile2", "model": "model-2"},
        {"id": "profile3", "model": "model-3"},
    ]
    assert LLMGateway(settings, "profile3").profile == "profile3"


def test_build_context_keeps_source_locator():
    value = answerer.build_context([hit("LUB 更换周期为 4000 小时。")])
    assert value.startswith("[1] 维保手册.md · 1. 保养周期")
    assert "第3页" in value and "4000 小时" in value


@pytest.mark.asyncio
async def test_answerer_fallback_and_gateway_messages():
    evidence = [hit("LUB 更换周期为 4000 小时。")]
    chunks = [chunk async for chunk in answerer.stream_answer("周期是多少", evidence, None)]
    text = "".join(chunks)
    assert "4000 小时" in text and "[1]" in text

    captured = {}

    class FakeGateway:
        configured = True
        last_model = "fake"

        async def stream(self, messages, json_mode=False):
            captured["messages"] = messages
            captured["json_mode"] = json_mode
            yield '{"answer": "ok", "citations": [1]}'

    bundle = SimpleNamespace(
        messages=[{"role": "system", "content": "规则"}, {"role": "user", "content": "问题"}],
        evidence=[],
    )
    chunks = [
        chunk
        async for chunk in answerer.stream_answer(
            "问题", evidence, FakeGateway(), bundle=bundle, json_mode=True
        )
    ]
    assert "".join(chunks).startswith("{")
    assert captured["json_mode"] is True
    assert captured["messages"][0]["content"] == "规则"
