"""附件解析器：文档文本、图片尺寸与音视频元数据。"""

from types import SimpleNamespace

import pytest
from PIL import Image

from app.attachments.parsers import extract


@pytest.fixture()
def settings():
    return SimpleNamespace(attachment_text_chars_per_file=12000)


def test_text_markdown_csv_and_svg(tmp_path, settings):
    txt = tmp_path / "note.txt"
    txt.write_text("保养周期为 4000 小时。", encoding="utf-8")
    result = extract(txt, settings)
    assert result.parse_status == "ok" and "4000" in (result.text or "")

    md = tmp_path / "manual.md"
    md.write_text("# 第 1 章\n\n| 代码 | 含义 |\n| --- | --- |\n| E-05 | 排气温度过高 |\n", encoding="utf-8")
    result = extract(md, settings)
    assert result.parse_status == "ok"
    assert "E-05" in (result.text or "")

    csv_file = tmp_path / "codes.csv"
    csv_file.write_text("code,meaning\nE-05,排气温度过高\n", encoding="utf-8")
    result = extract(csv_file, settings)
    assert result.meta["rows"] == 2 and "排气温度过高" in (result.text or "")

    svg = tmp_path / "figure.svg"
    svg.write_text('<svg width="120" height="80"><rect /></svg>', encoding="utf-8")
    result = extract(svg, settings)
    assert result.meta["width"] == 120 and result.meta["height"] == 80


def test_pdf_and_docx_text(tmp_path, settings):
    import pymupdf
    from docx import Document

    pdf_path = tmp_path / "manual.pdf"
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 72), "MTBF rated 8000 hours")
    doc.save(pdf_path)
    doc.close()
    result = extract(pdf_path, settings)
    assert result.parse_status == "ok" and "8000" in (result.text or "")
    assert result.meta["pages"] == 1

    docx_path = tmp_path / "manual.docx"
    word = Document()
    word.add_paragraph("LUB 更换周期为 4000 小时。")
    table = word.add_table(rows=1, cols=2)
    table.rows[0].cells[0].text = "E-05"
    table.rows[0].cells[1].text = "排气温度过高"
    word.save(docx_path)
    result = extract(docx_path, settings)
    assert result.parse_status == "ok"
    assert "4000" in (result.text or "") and "排气温度过高" in (result.text or "")


def test_xlsx_and_pptx_text(tmp_path, settings):
    import openpyxl
    from pptx import Presentation
    from pptx.util import Inches

    xlsx_path = tmp_path / "table.xlsx"
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.append(["代码", "含义"])
    sheet.append(["E-05", "排气温度过高"])
    workbook.save(xlsx_path)
    result = extract(xlsx_path, settings)
    assert result.meta["sheets"] == 1 and "排气温度过高" in (result.text or "")

    pptx_path = tmp_path / "slides.pptx"
    presentation = Presentation()
    slide = presentation.slides.add_slide(presentation.slide_layouts[6])
    box = slide.shapes.add_textbox(Inches(1), Inches(1), Inches(4), Inches(1))
    box.text_frame.text = "保养周期 4000 小时"
    presentation.save(pptx_path)
    result = extract(pptx_path, settings)
    assert result.meta["slides"] == 1 and "4000" in (result.text or "")


def test_image_and_media_metadata(tmp_path, settings):
    image_path = tmp_path / "photo.png"
    Image.new("RGB", (32, 18), (15, 118, 110)).save(image_path)
    result = extract(image_path, settings)
    assert result.meta["width"] == 32 and result.meta["height"] == 18

    audio = tmp_path / "voice.mp3"
    audio.write_bytes(b"ID3\x03\x00\x00\x00\x00\x00\x00")
    result = extract(audio, settings)
    assert result.meta["format"] == "mp3" and result.parse_status == "ok"

    video = tmp_path / "clip.mp4"
    video.write_bytes(b"\x00\x00\x00\x18ftypmp42")
    result = extract(video, settings)
    assert result.meta["format"] == "mp4" and result.parse_status == "ok"
