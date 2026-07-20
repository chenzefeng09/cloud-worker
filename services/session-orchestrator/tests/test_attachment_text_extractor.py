"""Coverage for :mod:`agent_session_orchestrator.services.attachment_text_extractor`.

Fixtures are generated in-memory by the same parsing libraries the extractor
uses (pypdf / python-docx / openpyxl / python-pptx), so no binary test assets
are checked in.  The whole chain is fail-open — the corrupted/unsupported cases
assert that a bad input yields a Chinese diagnostic string, never an exception.
"""

from __future__ import annotations

import io

from agent_session_orchestrator.services import attachment_text_extractor as ex

# ─────────────────────────────────────────────────────────────────────────────
# Fixture builders (tiny in-memory documents)
# ─────────────────────────────────────────────────────────────────────────────


def _make_docx_bytes(paragraphs: list[str], table: list[list[str]] | None = None) -> bytes:
    import docx

    doc = docx.Document()
    for p in paragraphs:
        doc.add_paragraph(p)
    if table:
        t = doc.add_table(rows=len(table), cols=len(table[0]))
        for r, row in enumerate(table):
            for c, val in enumerate(row):
                t.cell(r, c).text = val
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


def _make_xlsx_bytes(sheets: dict[str, list[list]]) -> bytes:
    from openpyxl import Workbook

    wb = Workbook()
    # Remove the default sheet so we control names exactly.
    wb.remove(wb.active)
    for name, rows in sheets.items():
        ws = wb.create_sheet(title=name)
        for row in rows:
            ws.append(row)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _make_pptx_bytes(slides: list[list[str]]) -> bytes:
    from pptx import Presentation
    from pptx.util import Inches

    prs = Presentation()
    blank = prs.slide_layouts[6]  # fully blank layout
    for lines in slides:
        slide = prs.slides.add_slide(blank)
        box = slide.shapes.add_textbox(Inches(1), Inches(1), Inches(6), Inches(4))
        tf = box.text_frame
        tf.text = lines[0] if lines else ""
        for extra in lines[1:]:
            tf.add_paragraph().text = extra
    buf = io.BytesIO()
    prs.save(buf)
    return buf.getvalue()


def _make_pdf_bytes(text: str) -> bytes:
    """Hand-assemble a minimal single-page PDF with a text object.

    Avoids a reportlab dependency: a raw PDF with one ``BT … Tj … ET`` content
    stream is enough for pypdf's ``extract_text`` to recover the ASCII string.
    (PDF content strings are latin-1; this fixture uses ASCII text so the
    extraction is exercised for real rather than via the fail-open branch.)
    """
    stream = f"BT /F1 24 Tf 72 720 Td ({text}) Tj ET".encode("latin-1")
    objs: list[bytes] = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    buf = bytearray(b"%PDF-1.4\n")
    offsets: list[int] = []
    for i, body in enumerate(objs, start=1):
        offsets.append(len(buf))
        buf += f"{i} 0 obj\n".encode() + body + b"\nendobj\n"
    xref_pos = len(buf)
    buf += f"xref\n0 {len(objs) + 1}\n".encode()
    buf += b"0000000000 65535 f \n"
    for off in offsets:
        buf += f"{off:010d} 00000 n \n".encode()
    buf += (
        f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\n"
        f"startxref\n{xref_pos}\n%%EOF"
    ).encode()
    return bytes(buf)


# ─────────────────────────────────────────────────────────────────────────────
# Text / plain formats
# ─────────────────────────────────────────────────────────────────────────────


def test_plain_text_utf8() -> None:
    out = ex.extract_text("notes.txt", "text/plain", "你好，世界\nhello".encode())
    assert "你好，世界" in out
    assert "hello" in out


def test_plain_text_gbk_fallback() -> None:
    data = "中文内容".encode("gbk")
    out = ex.extract_text("legacy.txt", "text/plain", data)
    assert out == "中文内容"


def test_plain_text_replace_fallback_never_raises() -> None:
    # Bytes that are neither valid utf-8 nor valid gbk → errors="replace".
    out = ex.extract_text("bad.txt", "text/plain", b"\xff\xfe\x00\x01abc")
    assert isinstance(out, str)
    assert "abc" in out  # the ascii tail survives


def test_csv_and_json_treated_as_text() -> None:
    assert "a,b,c" in ex.extract_text("d.csv", "text/csv", b"a,b,c\n1,2,3")
    assert '"k"' in ex.extract_text("d.json", "application/json", b'{"k": 1}')


# ─────────────────────────────────────────────────────────────────────────────
# Office formats
# ─────────────────────────────────────────────────────────────────────────────


def test_docx_paragraphs_and_table() -> None:
    data = _make_docx_bytes(
        ["第一段落", "第二段落"],
        table=[["姓名", "年龄"], ["张三", "30"]],
    )
    out = ex.extract_text(
        "doc.docx",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        data,
    )
    assert "第一段落" in out
    assert "第二段落" in out
    # Table row rendered tab-separated.
    assert "姓名\t年龄" in out
    assert "张三\t30" in out


def test_docx_by_extension_when_mime_generic() -> None:
    data = _make_docx_bytes(["仅凭扩展名识别"])
    out = ex.extract_text("report.docx", "application/octet-stream", data)
    assert "仅凭扩展名识别" in out


def test_xlsx_multi_sheet_with_names() -> None:
    data = _make_xlsx_bytes(
        {
            "销售": [["产品", "数量"], ["苹果", 10]],
            "库存": [["产品", "余量"], ["香蕉", 5]],
        }
    )
    out = ex.extract_text(
        "book.xlsx",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        data,
    )
    assert "工作表：销售" in out
    assert "工作表：库存" in out
    assert "产品\t数量" in out
    assert "苹果\t10" in out
    assert "香蕉\t5" in out


def test_xlsx_row_cap_truncates() -> None:
    # One sheet with more rows than the per-sheet cap.
    rows = [[f"row{i}", i] for i in range(ex.MAX_XLSX_ROWS_PER_SHEET + 50)]
    data = _make_xlsx_bytes({"大表": rows})
    out = ex.extract_text(
        "big.xlsx",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        data,
    )
    assert f"工作表行数超过 {ex.MAX_XLSX_ROWS_PER_SHEET} 行" in out
    # The row just past the cap must not appear.
    assert f"row{ex.MAX_XLSX_ROWS_PER_SHEET + 10}" not in out


def test_pptx_per_slide_labels_and_table() -> None:
    data = _make_pptx_bytes([["幻灯片一标题", "要点甲"], ["幻灯片二标题"]])
    out = ex.extract_text(
        "deck.pptx",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        data,
    )
    assert "第1页" in out
    assert "第2页" in out
    assert "幻灯片一标题" in out
    assert "要点甲" in out
    assert "幻灯片二标题" in out


def test_pdf_extracts_text() -> None:
    data = _make_pdf_bytes("Hello PDF 123")
    out = ex.extract_text("file.pdf", "application/pdf", data)
    assert isinstance(out, str)
    assert "Hello PDF 123" in out


def test_corrupt_pdf_fails_open() -> None:
    out = ex.extract_text("broken.pdf", "application/pdf", b"%PDF-1.4 not really a pdf")
    # Either a parse-failure notice or a "no text" notice — never raises.
    assert out.startswith("（文件解析失败：") or "未从文件中解析出文本" in out


# ─────────────────────────────────────────────────────────────────────────────
# Unsupported / oversize / corrupt (all fail-open)
# ─────────────────────────────────────────────────────────────────────────────


def test_unsupported_legacy_doc() -> None:
    assert ex.extract_text("old.doc", "application/msword", b"\xd0\xcf\x11\xe0stuff") \
        == "（暂不支持解析该格式）"


def test_unsupported_image() -> None:
    assert ex.extract_text("pic.png", "image/png", b"\x89PNG\r\n") \
        == "（暂不支持解析该格式）"


def test_unknown_binary_mime_and_ext() -> None:
    assert ex.extract_text("blob.dat", "application/octet-stream", b"\x00\x01\x02") \
        == "（暂不支持解析该格式）"


def test_oversize_file_skipped() -> None:
    big = b"x" * (ex.MAX_BYTES_PER_FILE + 1)
    out = ex.extract_text("huge.txt", "text/plain", big)
    assert "文件过大" in out


def test_empty_file() -> None:
    assert ex.extract_text("e.txt", "text/plain", b"") == "（文件为空）"


def test_corrupt_docx_fails_open_not_raises() -> None:
    # Garbage bytes with a .docx name → python-docx raises → caught → notice.
    out = ex.extract_text("broken.docx", "application/octet-stream", b"not a real docx zip")
    assert out.startswith("（文件解析失败：")


def test_corrupt_xlsx_fails_open() -> None:
    out = ex.extract_text("broken.xlsx", "application/octet-stream", b"not a zip")
    assert out.startswith("（文件解析失败：")


def test_per_file_truncation() -> None:
    long_text = "字" * (ex.MAX_CHARS_PER_FILE + 500)
    out = ex.extract_text("long.txt", "text/plain", long_text.encode("utf-8"))
    assert len(out) <= ex.MAX_CHARS_PER_FILE + len("\n……（内容过长，已截断）") + 5
    assert "已截断" in out


# ─────────────────────────────────────────────────────────────────────────────
# build_attachments_block
# ─────────────────────────────────────────────────────────────────────────────


def test_build_block_empty_returns_none() -> None:
    assert ex.build_attachments_block([]) is None


def test_build_block_wraps_each_file() -> None:
    block = ex.build_attachments_block(
        [("att_a", "a.txt", "内容甲"), ("att_b", "b.txt", "内容乙")]
    )
    assert block is not None
    # The id from the triple is surfaced as an id attribute on the tag so the
    # model can reference the file via [文件名](#file:<id>).
    assert '<attachment id="att_a" filename="a.txt">' in block
    assert "内容甲" in block
    assert '<attachment id="att_b" filename="b.txt">' in block
    assert "内容乙" in block
    assert "用户在本会话上传了以下文件" in block


def test_build_block_omits_id_attr_when_id_empty() -> None:
    # A missing/empty id (fail-open) yields the plain tag — never id="" (which
    # would hand the model a fabricatable anchor).
    block = ex.build_attachments_block([("", "a.txt", "内容")])
    assert block is not None
    assert '<attachment filename="a.txt">' in block
    assert 'id="' not in block


def test_build_block_session_budget_drops_overflow() -> None:
    # Two files whose combined length exceeds a tiny budget → the second is
    # dropped and a notice is appended.
    block = ex.build_attachments_block(
        [("att_a", "a.txt", "x" * 30), ("att_b", "b.txt", "y" * 30)],
        session_budget=30,
    )
    assert block is not None
    assert "a.txt" in block
    assert "另有 1 个文件因内容预算已满未展示" in block


def test_build_block_quote_in_filename_sanitized() -> None:
    block = ex.build_attachments_block([("att_x", 'he"llo".txt', "内容")])
    assert block is not None
    # Double quote replaced so the attribute stays well-formed.
    assert '"he' not in block.split("filename=")[1][:20] or "he'llo'.txt" in block


def test_file_link_convention_prompt_describes_link_format() -> None:
    # The convention constant instructs the model to reference files via the
    # markdown link the frontend intercepts, and to only use real listed ids.
    prompt = ex.FILE_LINK_CONVENTION_PROMPT
    assert isinstance(prompt, str) and prompt.strip()
    assert "[文件名](#file:文件id)" in prompt
    assert "不要编造" in prompt
