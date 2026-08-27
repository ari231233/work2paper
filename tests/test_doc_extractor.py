"""M28 主流文档解析与 OCR 回归测试。"""
from __future__ import annotations

import io
from pathlib import Path

import pytest

from papermine.extractor import doc_extractor
from papermine.models import Asset


def _asset(path: str) -> Asset:
    return Asset(path=path, kind="doc")


def _minimal_pdf(text: str) -> bytes:
    escaped = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    stream = "BT /F1 18 Tf 72 720 Td ({}) Tj ET".format(escaped).encode("ascii")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length " + str(len(stream)).encode("ascii") + b" >>\nstream\n" + stream + b"\nendstream",
    ]
    output = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for index, obj in enumerate(objects, 1):
        offsets.append(len(output))
        output.extend("{} 0 obj\n".format(index).encode("ascii"))
        output.extend(obj + b"\nendobj\n")
    xref = len(output)
    output.extend("xref\n0 {}\n".format(len(objects) + 1).encode("ascii"))
    output.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        output.extend("{:010d} 00000 n \n".format(offset).encode("ascii"))
    output.extend(
        "trailer << /Size {} /Root 1 0 R >>\nstartxref\n{}\n%%EOF".format(
            len(objects) + 1, xref
        ).encode("ascii")
    )
    return bytes(output)


def _png_bytes() -> bytes:
    Image = pytest.importorskip("PIL.Image")
    image = Image.new("RGB", (120, 50), "white")
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def test_plain_text_preserved(tmp_path: Path) -> None:
    (tmp_path / "paper.tex").write_text("NASA GUAM trajectory tracking", encoding="utf-8")
    result = doc_extractor.extract_asset_text(str(tmp_path), _asset("paper.tex"))
    assert "NASA GUAM" in result.text
    assert result.warnings == []


def test_text_pdf_uses_text_layer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pytest.importorskip("pypdfium2")
    (tmp_path / "paper.pdf").write_bytes(
        _minimal_pdf("Online obstacle avoidance and prescribed performance control")
    )
    monkeypatch.setattr(doc_extractor, "_ocr_image", lambda image: "OCR SHOULD NOT WIN")
    result = doc_extractor.extract_asset_text(str(tmp_path), _asset("paper.pdf"))
    assert "obstacle avoidance" in result.text
    assert "OCR SHOULD NOT WIN" not in result.text


def test_scanned_pdf_falls_back_to_ocr(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pytest.importorskip("pypdfium2")
    (tmp_path / "scan.pdf").write_bytes(_minimal_pdf("scan"))
    monkeypatch.setattr(doc_extractor, "_ocr_image", lambda image: "中英文 OCR 识别结果")
    result = doc_extractor.extract_asset_text(str(tmp_path), _asset("scan.pdf"))
    assert "中英文 OCR 识别结果" in result.text


def test_docx_paragraph_table_header_and_image_ocr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    docx = pytest.importorskip("docx")
    document = docx.Document()
    document.add_paragraph("论文标题：eVTOL 飞行控制")
    table = document.add_table(rows=1, cols=2)
    table.cell(0, 0).text = "指标"
    table.cell(0, 1).text = "跟踪误差"
    document.sections[0].header.paragraphs[0].text = "NASA GUAM"
    document.add_picture(io.BytesIO(_png_bytes()))
    path = tmp_path / "paper.docx"
    document.save(str(path))
    monkeypatch.setattr(doc_extractor, "_ocr_image", lambda image: "图片中的中文结论")
    result = doc_extractor.extract_asset_text(str(tmp_path), _asset("paper.docx"))
    assert "eVTOL 飞行控制" in result.text
    assert "指标\t跟踪误差" in result.text
    assert "NASA GUAM" in result.text
    assert "图片中的中文结论" in result.text


def test_pptx_text_table_notes_and_image_ocr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pptx = pytest.importorskip("pptx")
    presentation = pptx.Presentation()
    slide = presentation.slides.add_slide(presentation.slide_layouts[5])
    slide.shapes.title.text = "DAIDALUS 避障"
    table = slide.shapes.add_table(1, 2, 0, 0, 3_000_000, 500_000).table
    table.cell(0, 0).text = "方法"
    table.cell(0, 1).text = "鲁棒控制"
    slide.notes_slide.notes_text_frame.text = "演讲备注：闭环验证"
    slide.shapes.add_picture(io.BytesIO(_png_bytes()), 0, 600_000)
    path = tmp_path / "slides.pptx"
    presentation.save(str(path))
    monkeypatch.setattr(doc_extractor, "_ocr_image", lambda image: "图表 OCR 结果")
    result = doc_extractor.extract_asset_text(str(tmp_path), _asset("slides.pptx"))
    assert "DAIDALUS 避障" in result.text
    assert "方法\t鲁棒控制" in result.text
    assert "演讲备注：闭环验证" in result.text
    assert "图表 OCR 结果" in result.text


def test_corrupt_binary_document_degrades_safely(tmp_path: Path) -> None:
    (tmp_path / "broken.pdf").write_bytes(b"not a pdf\x00\xff")
    result = doc_extractor.extract_asset_text(str(tmp_path), _asset("broken.pdf"))
    assert result.text == ""
    assert result.warnings


def test_character_budget_marks_truncation(tmp_path: Path) -> None:
    (tmp_path / "long.txt").write_text("abcdefghij", encoding="utf-8")
    result = doc_extractor.extract_asset_text(str(tmp_path), _asset("long.txt"), max_chars=5)
    assert result.text == "abcde"
    assert result.truncated is True
