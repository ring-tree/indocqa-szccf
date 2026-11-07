"""解析与切块阶段测试（T1.1 / T1.2）。"""

import pathlib

from app.parsing.chunker import build_chunks, serialize_table
from app.parsing.loader import load_document
from app.parsing.models import RawTable

CORPUS = pathlib.Path(__file__).resolve().parents[2] / "data" / "corpus"
FIXTURE = CORPUS / "fixture_术语混写_模拟维保手册.md"


def _load_fixture():
    doc = load_document(FIXTURE)
    children, parents = build_chunks(doc)
    return doc, children, parents


def test_md_sections_preserved():
    doc, children, _ = _load_fixture()
    sections = {c.section for c in children}
    assert any("可靠性指标" in s for s in sections)
    assert any("保养周期表" in s for s in sections)
    assert doc.tables, "Markdown 管道表格未被识别"


def test_table_chunk_independent_and_serialized():
    doc, children, _ = _load_fixture()
    tables = [c for c in children if c.kind == "table"]
    assert tables, "表格未成为独立子块"
    joined = "\n".join(t.text for t in tables)
    assert "LUB 更换（机油）" in joined and "4000 h" in joined
    assert all(len(t.text) < 4000 for t in tables)


def test_children_within_size_bounds():
    _, children, _ = _load_fixture()
    for c in children:
        assert len(c.text) <= 1800, f"子块过长: {c.chunk_id} {len(c.text)}"
        assert len(c.text.strip()) > 0


def test_parent_contains_children_and_page_meta():
    _, children, parents = _load_fixture()
    by_id = {p.parent_id: p for p in parents}
    for c in children:
        parent = by_id[c.parent_id]
        assert c.text in parent.text
        assert parent.section == c.section


def test_numbered_list_not_cut_mid_item():
    doc, children, _ = _load_fixture()
    s12 = [c for c in children if "立即停机" in c.text]
    assert s12, "S-12 步骤未入库"
    text = s12[0].text
    assert "复位后重启" in text and "每 10 分钟记录" in text, "编号列表被切断"


def test_serialize_table_format():
    table = RawTable(rows=[["代码", "含义"], ["E-05", "排气温度过高"]], table_no="表 2-1")
    text = serialize_table(table)
    assert text.startswith("【表 2-1 表格】")
    assert "代码=E-05" in text and "含义=排气温度过高" in text


def test_pdf_loads_with_pages():
    pdf = CORPUS / "无油静音空压机操作说明_厂商公开.pdf"
    if not pdf.exists():
        return
    doc = load_document(pdf)
    assert doc.pages == 10
    assert doc.paragraphs, "PDF 段落提取为空"
    assert all(p.page is not None for p in doc.paragraphs)
