"""生成层优化：Markdown 清理、引用约束、安全过滤、结构化 JSON 与参数固化。"""

import json
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.core.config import Settings
from app.core.llm import LLMGateway
from app.generation import citations, formatting, safety, structured


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


def test_strip_markdown_removes_markers_but_keeps_formula_and_identifiers():
    text = (
        "# 保养周期\n"
        "**LUB 更换周期为 4000 小时**，详见 *维保手册*。\n"
        "- 检查油位\n"
        "- [ ] 待办事项\n"
        "1. 停机\n"
        "> 注意安全\n"
        "[维保手册](http://example.com)\n"
        "| 项目 | 周期 |\n|---|---|\n| LUB | 4000 |\n"
        "公式 $E=mc^2$，乘法 2*3，标识 LUB_46。\n"
    )
    plain = formatting.strip_markdown(text)
    assert "#" not in plain and "**" not in plain and "|" not in plain
    assert "[维保手册](" not in plain
    assert "LUB 更换周期为 4000 小时" in plain
    assert "$E=mc^2$" in plain
    assert "2*3" in plain and "LUB_46" in plain
    assert "1、" in plain and "[ ]" not in plain


def test_enforce_citations_binds_data_sentence_and_drops_unsupported():
    evidence = [hit("LUB 更换周期为 4000 运行小时，耗材为 LUB-46。")]
    answer = (
        "LUB 更换周期为 4000 运行小时。\n"
        "设备需要定期保养。\n"
        "型号 ZZZ-99 的额定压力为 5 MPa。\n"
        "办公用品由行政部采购。"
    )
    cleaned, audit = citations.enforce_citations(answer, evidence)
    assert "4000 运行小时[1]。" in cleaned
    assert "ZZZ-99" not in cleaned
    assert audit.data_facts >= 1 and audit.data_cited >= 1
    assert audit.data_removed == 1
    assert 1 in audit.citations


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
        '```json\n{"answer": "周期为 4000 小时 [1]", "citations": [1]}\n```'
    )
    assert parsed and parsed["answer"].startswith("周期")
    assert structured.parse_model_json("不是 JSON") is None

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
        "周期为 4000 小时 [1]。",
        sources,
        "mid",
        "检索最高相似度 0.80",
        None,
        "test-model",
        audit,
        "trace-1",
    )
    encoded = structured.dumps(payload)
    decoded = json.loads(encoded)
    assert decoded["answer"].endswith("[1]。")
    assert decoded["citations"][0]["docId"] == "d1"
    assert decoded["schemaVersion"] == "1.0"


def test_generation_parameter_bounds():
    with pytest.raises(ValidationError):
        Settings(generation_temperature=2.5)
    with pytest.raises(ValidationError):
        Settings(generation_max_tokens=10)
    with pytest.raises(ValidationError):
        Settings(generation_json_temperature=-0.1)
    with pytest.raises(ValidationError):
        Settings(generation_json_max_tokens=9000)


def test_gateway_builds_fixed_generation_parameters():
    settings = Settings(
        llm_base_url="http://localhost:9/v1",
        llm_api_key="sk-abcdefghijklmnop",
        llm_model="test-model",
        generation_temperature=0.1,
        generation_max_tokens=512,
        generation_json_temperature=0.0,
        generation_json_max_tokens=768,
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

    override = gateway._build_body(
        provider, [{"role": "user", "content": "hi"}], temperature=0.0, max_tokens=8
    )
    assert override["temperature"] == 0.0 and override["max_tokens"] == 8
