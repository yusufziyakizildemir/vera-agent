"""PDF, PowerPoint (.pptx), Word (.docx), Excel (.xlsx), CSV ve metin dosyası analiz motoru."""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Any

from vera.tools.registry import ToolContext, ToolDef, ToolRegistry


def extract_document_text(file_path: Path, max_chars: int = 18000) -> dict[str, Any]:
    """Extracts structured text cleanly from PDF, PPTX, DOCX, XLSX, CSV, or plain text."""
    if not file_path.exists():
        return {"error": f"Dosya bulunamadı: {file_path}"}

    ext = file_path.suffix.lower()
    text_parts: list[str] = []
    meta: dict[str, Any] = {"filename": file_path.name, "ext": ext, "size_bytes": file_path.stat().st_size}

    try:
        if ext == ".pdf":
            from pypdf import PdfReader

            reader = PdfReader(str(file_path))
            meta["pages"] = len(reader.pages)
            for idx, page in enumerate(reader.pages, start=1):
                page_txt = (page.extract_text() or "").strip()
                if page_txt:
                    text_parts.append(f"--- Sayfa {idx} ---\n{page_txt}")

        elif ext == ".pptx":
            from pptx import Presentation

            prs = Presentation(str(file_path))
            meta["slides"] = len(prs.slides)
            for idx, slide in enumerate(prs.slides, start=1):
                lines = []
                for shape in slide.shapes:
                    if hasattr(shape, "text") and shape.text.strip():
                        lines.append(shape.text.strip())
                if slide.has_notes_slide and slide.notes_slide.notes_text_frame:
                    notes = slide.notes_slide.notes_text_frame.text.strip()
                    if notes:
                        lines.append(f"[Konuşmacı Notu]: {notes}")
                if lines:
                    text_parts.append(f"--- Slayt {idx} ---\n" + "\n".join(lines))

        elif ext == ".docx":
            from docx import Document

            doc = Document(str(file_path))
            for p in doc.paragraphs:
                if p.text.strip():
                    text_parts.append(p.text.strip())
            for t_idx, table in enumerate(doc.tables, start=1):
                rows = [
                    " | ".join(cell.text.strip() for cell in row.cells)
                    for row in table.rows
                ]
                if rows:
                    text_parts.append(f"--- Tablo {t_idx} ---\n" + "\n".join(rows))

        elif ext in {".xlsx", ".xlsm"}:
            import openpyxl

            wb = openpyxl.load_workbook(str(file_path), data_only=True, read_only=True)
            meta["sheets"] = wb.sheetnames
            for sheet_name in wb.sheetnames[:8]:
                ws = wb[sheet_name]
                rows = []
                for r_idx, row in enumerate(ws.iter_rows(values_only=True), start=1):
                    if r_idx > 120:
                        rows.append("... (satır limiti)")
                        break
                    vals = [str(v) if v is not None else "" for v in row]
                    if any(vals):
                        rows.append(" | ".join(vals))
                if rows:
                    text_parts.append(f"--- Sayfa: {sheet_name} ---\n" + "\n".join(rows))
            wb.close()

        elif ext == ".csv":
            with file_path.open("r", encoding="utf-8", errors="replace", newline="") as f:
                reader = csv.reader(f)
                rows = [" | ".join(row) for _, row in zip(range(150), reader)]
            text_parts.append("\n".join(rows))

        else:
            raw = file_path.read_text(encoding="utf-8", errors="replace")
            text_parts.append(raw)

    except Exception as exc:
        return {"error": f"Belge çözümlenemedi ({ext}): {exc}", **meta}

    full_text = "\n\n".join(text_parts).strip()
    return {
        **meta,
        "text": full_text[:max_chars],
        "truncated": len(full_text) > max_chars,
        "total_chars": len(full_text),
    }


def register_document_tools(registry: ToolRegistry) -> None:
    async def analyze_uploaded_file(ctx: ToolContext, filename: str) -> dict[str, Any]:
        safe_name = Path(filename).name
        candidates = [
            ctx.settings.uploads_dir / safe_name,
            ctx.settings.workspace_dir / safe_name,
        ]
        for path in candidates:
            if path.exists():
                return extract_document_text(path)
        return {"error": f"Dosya bulunamadı: {safe_name}"}

    async def list_uploaded_files(ctx: ToolContext) -> dict[str, Any]:
        files = []
        for folder in (ctx.settings.uploads_dir, ctx.settings.workspace_dir):
            if folder.exists():
                for p in sorted(folder.iterdir(), key=lambda x: x.stat().st_mtime, reverse=True)[:30]:
                    if p.is_file():
                        files.append(
                            {
                                "name": p.name,
                                "folder": folder.name,
                                "size_bytes": p.stat().st_size,
                            }
                        )
        return {"count": len(files), "files": files}

    registry.register(
        ToolDef(
            name="analyze_uploaded_file",
            category="documents",
            description="Yüklenen bir PDF, PPTX (PowerPoint), DOCX (Word), XLSX (Excel), CSV veya metin dosyasının içeriğini okur.",
            parameters={
                "type": "object",
                "properties": {
                    "filename": {"type": "string", "description": "Analiz edilecek dosyanın adı."},
                },
                "required": ["filename"],
            },
            handler=analyze_uploaded_file,
        )
    )

    registry.register(
        ToolDef(
            name="list_uploaded_files",
            category="documents",
            description="Yüklenen belgeleri ve çalışma alanındaki dosyaları listeler.",
            parameters={"type": "object", "properties": {}, "required": []},
            handler=list_uploaded_files,
        )
    )
