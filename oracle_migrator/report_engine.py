"""
report_engine.py — oracle-migrator Report Engine
============================================
Modeled after the ssp.efka.template JasperEngine / DTO architecture:

  JasperEngine  →  ReportEngine      (compile once, fill from bean collection)
  JasperReport  →  ReportTemplate    (compiled / prepared report definition)
  JasperPrint   →  RenderedReport    (filled report ready for export)
  DebtorsForProclamationReportDTO → AnalysisReportDTO  (main + nested TableDTO)

Usage (mirrors DashboardController.printReport()):
    dto = AnalysisReportDTO.from_session(session_data)
    print_report = ReportEngine.render_report(dto)
    buf = ReportEngine.export_pdf(print_report)
"""

from __future__ import annotations

import io
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import (
    HRFlowable, KeepTogether, PageBreak, Paragraph, SimpleDocTemplate,
    Spacer, Table, TableStyle,
)


# ── Colour palette (mirrors LEVEL_HEX in DashboardController) ─────────────────

_LEVEL_HEX = {
    1: "#22c55e",  # L1 Trivial   — green
    2: "#06b6d4",  # L2 Simple    — cyan
    3: "#f97316",  # L3 Moderate  — orange
    4: "#ef4444",  # L4 Complex   — red
    5: "#94a3b8",  # L5 Very Complex — slate
}
_LEVEL_BG = {
    1: colors.HexColor("#dcfce7"),
    2: colors.HexColor("#cffafe"),
    3: colors.HexColor("#ffedd5"),
    4: colors.HexColor("#fee2e2"),
    5: colors.HexColor("#f1f5f9"),
}
_LEVEL_NAMES = {
    1: "Trivial",
    2: "Simple",
    3: "Moderate",
    4: "Complex",
    5: "Very Complex",
}
_EFFORT_RANGES = {
    1: "< 0.5 days",
    2: "0.5–2 days",
    3: "2–5 days",
    4: "5–15 days",
    5: "> 15 days",
}


# ─────────────────────────────────────────────────────────────────────────────
# DTO layer  (mirrors DebtorsForProclamationReportDTO + inner TableDTO)
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class FileRowDTO:
    """
    Inner record — mirrors DebtorsForProclamationReportDTO.TableDTO.
    One row per analysed file in the detail table.
    """
    filename: str = ""
    artifact_type: str = ""
    complexity_level: int = 1
    label: str = ""
    grade: str = ""
    raw_points: int = 0
    estimated_effort_days: float = 0.0
    reasons: List[str] = field(default_factory=list)
    breakdown: dict = field(default_factory=dict)
    migration_notes: List[str] = field(default_factory=list)

    @property
    def level_name(self) -> str:
        return _LEVEL_NAMES.get(self.complexity_level, "Unknown")

    @property
    def effort_range(self) -> str:
        return _EFFORT_RANGES.get(self.complexity_level, "")

    @classmethod
    def from_dict(cls, d: dict) -> "FileRowDTO":
        return cls(
            filename=Path(d.get("file", "")).name or d.get("file", "?"),
            artifact_type=d.get("artifact_type", ""),
            complexity_level=int(d.get("complexity_level", 1)),
            label=d.get("label", ""),
            grade=d.get("grade", ""),
            raw_points=int(d.get("raw_points", 0)),
            estimated_effort_days=float(d.get("estimated_effort_days", 0)),
            reasons=d.get("reasons", []),
            breakdown=d.get("breakdown", {}),
            migration_notes=d.get("migration_notes", []),
        )


@dataclass
class AnalysisReportDTO:
    """
    Main report record — mirrors DebtorsForProclamationReportDTO (top-level fields
    + LinkedList<TableDTO>).
    """
    # Header metadata (mirrors branchName, branchAddress, userEmail …)
    generated_at: datetime = field(default_factory=datetime.now)
    app_name: str = "Oracle Migrator"
    scoring_model: str = "Re_Forms 21 (validated against real reports)"
    total_files: int = 0
    export_format: str = "PDF"

    # Detail collection (mirrors LinkedList<TableDTO> tableDTO)
    rows: List[FileRowDTO] = field(default_factory=list)

    # ── Computed aggregates (like JasperReport summary variables) ─────────────
    @property
    def counts_by_level(self) -> dict:
        c = {i: 0 for i in range(1, 6)}
        for r in self.rows:
            c[r.complexity_level] = c.get(r.complexity_level, 0) + 1
        return c

    @property
    def total_effort_days(self) -> float:
        return sum(r.estimated_effort_days for r in self.rows)

    @classmethod
    def from_session(cls, session_data: list) -> "AnalysisReportDTO":
        """Build DTO from Flask session data — mirrors createDummyRecord() pattern."""
        rows = [FileRowDTO.from_dict(d) for d in session_data]
        return cls(
            total_files=len(rows),
            rows=rows,
        )


# ─────────────────────────────────────────────────────────────────────────────
# ReportTemplate  (mirrors JasperReport — the compiled/prepared definition)
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class ReportTemplate:
    """Holds the style definitions for a report — equivalent to JasperReport."""
    page_width_mm: float = 210    # A4 portrait width
    page_height_mm: float = 297   # A4 portrait height
    orientation: str = "portrait"  # "portrait" | "landscape"

    # Style cache (populated by ReportEngine.compile_report)
    _styles: dict = field(default_factory=dict, repr=False)

    @property
    def page_size(self):
        ps = A4
        return landscape(ps) if self.orientation == "landscape" else ps


# ─────────────────────────────────────────────────────────────────────────────
# RenderedReport  (mirrors JasperPrint — a filled report ready for export)
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class RenderedReport:
    """Holds the fully-built Platypus story — equivalent to JasperPrint."""
    template: ReportTemplate
    story: list
    metadata: dict = field(default_factory=dict)


# ─────────────────────────────────────────────────────────────────────────────
# ReportEngine  (mirrors JasperEngine static utility class)
# ─────────────────────────────────────────────────────────────────────────────

class ReportEngine:
    """
    Static utility that mirrors JasperEngine:
      compile_report()  → JasperCompileManager.compileReport()
      render_report()   → JasperFillManager.fillReport() + JRBeanCollectionDataSource
      export_pdf()      → JRPdfExporter
    """

    # ── compile_report  (mirrors compileBatchReport) ──────────────────────────

    @staticmethod
    def compile_report(orientation: str = "portrait") -> ReportTemplate:
        """
        Prepare the report template (styles, layout) — equivalent to
        JasperCompileManager.compileReport(templatePath).
        """
        tmpl = ReportTemplate(orientation=orientation)
        base = getSampleStyleSheet()

        tmpl._styles = {
            "title": ParagraphStyle(
                "RPT_Title", parent=base["Normal"],
                fontSize=20, fontName="Helvetica-Bold",
                textColor=colors.HexColor("#3b6ef8"),
                spaceAfter=2,
            ),
            "subtitle": ParagraphStyle(
                "RPT_Sub", parent=base["Normal"],
                fontSize=12, textColor=colors.HexColor("#64748b"),
                spaceAfter=2,
            ),
            "meta": ParagraphStyle(
                "RPT_Meta", parent=base["Normal"],
                fontSize=8, textColor=colors.HexColor("#94a3b8"),
            ),
            "section": ParagraphStyle(
                "RPT_Section", parent=base["Normal"],
                fontSize=11, fontName="Helvetica-Bold",
                textColor=colors.HexColor("#1e293b"),
                spaceBefore=10, spaceAfter=4,
            ),
            "normal": ParagraphStyle(
                "RPT_Normal", parent=base["Normal"],
                fontSize=9, textColor=colors.HexColor("#334155"),
            ),
            "small": ParagraphStyle(
                "RPT_Small", parent=base["Normal"],
                fontSize=8, textColor=colors.HexColor("#64748b"),
            ),
            "mono": ParagraphStyle(
                "RPT_Mono", parent=base["Normal"],
                fontSize=8, fontName="Courier",
                textColor=colors.HexColor("#475569"),
            ),
            "badge": ParagraphStyle(
                "RPT_Badge", parent=base["Normal"],
                fontSize=9, alignment=TA_CENTER,
            ),
            "right": ParagraphStyle(
                "RPT_Right", parent=base["Normal"],
                fontSize=9, alignment=TA_RIGHT,
            ),
            "reason": ParagraphStyle(
                "RPT_Reason", parent=base["Normal"],
                fontSize=8.5, leading=12,
                textColor=colors.HexColor("#475569"),
            ),
        }
        return tmpl

    # ── render_report  (mirrors JasperFillManager.fillReport + JRBeanCollectionDataSource) ──

    @staticmethod
    def render_report(
        template: ReportTemplate,
        dto: AnalysisReportDTO,
    ) -> RenderedReport:
        """
        Fill the report template with DTO data — equivalent to:
            JasperFillManager.fillReport(jasperReport, parameters,
                                         new JRBeanCollectionDataSource(collection))
        """
        s = template._styles
        story = []

        # ── Title band  (mirrors <title> band in JRXML) ───────────────────────
        story += ReportEngine._build_title_band(dto, s)

        # ── Summary band  (mirrors summary table / pageHeader) ────────────────
        story += ReportEngine._build_summary_band(dto, s)

        # ── Detail band  (mirrors <detail> band iterating dataSourceBeanCollection) ──
        story.append(Paragraph("File Details", s["section"]))
        story.append(Spacer(1, 2 * mm))
        for row in dto.rows:
            story.append(ReportEngine._build_file_card(row, s))

        # ── Footer / page footer band ─────────────────────────────────────────
        story += ReportEngine._build_footer_band(dto, s)

        return RenderedReport(
            template=template,
            story=story,
            metadata={
                "title": "oracle-migrator · Oracle Complexity Report",
                "author": dto.app_name,
                "subject": f"{dto.total_files} files analysed",
                "creator": dto.scoring_model,
            },
        )

    # ── export_pdf  (mirrors JRPdfExporter.exportReport) ─────────────────────

    @staticmethod
    def export_pdf(rendered: RenderedReport) -> io.BytesIO:
        """
        Export RenderedReport to PDF bytes — equivalent to:
            JRPdfExporter + SimpleOutputStreamExporterOutput
        """
        buf = io.BytesIO()
        doc = SimpleDocTemplate(
            buf,
            pagesize=rendered.template.page_size,
            leftMargin=18 * mm, rightMargin=18 * mm,
            topMargin=18 * mm, bottomMargin=18 * mm,
            **rendered.metadata,
        )
        doc.build(rendered.story)
        buf.seek(0)
        return buf

    # ── renderReport convenience (mirrors JasperEngine.renderReport) ──────────

    @classmethod
    def render_and_export(
        cls,
        dto: AnalysisReportDTO,
        orientation: str = "portrait",
    ) -> io.BytesIO:
        """
        One-shot: compile → fill → export.
        Mirrors the single-call renderReport() path in JasperEngine.
        """
        template = cls.compile_report(orientation=orientation)
        rendered = cls.render_report(template, dto)
        return cls.export_pdf(rendered)

    # ── Private band builders (mirrors JRXML band elements) ──────────────────

    @staticmethod
    def _build_title_band(dto: AnalysisReportDTO, s: dict) -> list:
        """Title band — mirrors <title><band> in debtorsForProclamationReport.jrxml."""
        band = []

        # Header row: logo placeholder + title + date  (mirrors staticText + textField)
        header_data = [[
            Paragraph(
                f"<b>{dto.app_name}</b>",
                ParagraphStyle("Logo", parent=s["title"], fontSize=22,
                               textColor=colors.HexColor("#3b6ef8")),
            ),
            Paragraph(
                f"<font color='#94a3b8' size='8'>"
                f"Generated: {dto.generated_at.strftime('%d/%m/%Y %H:%M')}</font>",
                s["right"],
            ),
        ]]
        hdr = Table(header_data, colWidths=[120 * mm, None])
        hdr.setStyle(TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("TOPPADDING", (0, 0), (-1, -1), 0),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
        ]))
        band.append(hdr)
        band.append(Spacer(1, 2 * mm))
        band.append(Paragraph("Oracle Forms &amp; Reports · Complexity Analysis", s["title"]))
        band.append(Paragraph("Migration Complexity Report", s["subtitle"]))
        band.append(Paragraph(
            f"{dto.total_files} file(s) analysed &nbsp;|&nbsp; "
            f"Scoring model: {dto.scoring_model}",
            s["meta"],
        ))
        band.append(Spacer(1, 6 * mm))
        band.append(HRFlowable(
            width="100%", thickness=1.5,
            color=colors.HexColor("#3b6ef8"),
        ))
        band.append(Spacer(1, 4 * mm))
        return band

    @staticmethod
    def _build_summary_band(dto: AnalysisReportDTO, s: dict) -> list:
        """Summary band — mirrors the header table in JRXML."""
        band = []
        band.append(Paragraph("Summary", s["section"]))
        band.append(Spacer(1, 2 * mm))

        counts = dto.counts_by_level
        total = max(dto.total_files, 1)

        # Summary table: Level | Name | Effort range | Count | %
        rows = [[
            Paragraph("<b>Level</b>", s["badge"]),
            Paragraph("<b>Classification</b>", s["badge"]),
            Paragraph("<b>Effort range</b>", s["badge"]),
            Paragraph("<b>Count</b>", s["badge"]),
            Paragraph("<b>%</b>", s["badge"]),
        ]]
        for lvl in range(1, 6):
            cnt = counts[lvl]
            pct = f"{cnt / total * 100:.0f}%"
            lc = colors.HexColor(_LEVEL_HEX[lvl])
            rows.append([
                Paragraph(
                    f"<b><font color='{_LEVEL_HEX[lvl]}'>L{lvl}</font></b>",
                    s["badge"],
                ),
                Paragraph(_LEVEL_NAMES[lvl], s["normal"]),
                Paragraph(_EFFORT_RANGES[lvl],
                          ParagraphStyle("ER", parent=s["mono"], fontSize=8)),
                Paragraph(str(cnt),
                          ParagraphStyle("Cnt", parent=s["badge"],
                                         textColor=lc if cnt else colors.HexColor("#94a3b8"))),
                Paragraph(pct, s["badge"]),
            ])

        # Totals row
        rows.append([
            Paragraph("", s["badge"]),
            Paragraph("<b>Total</b>", s["normal"]),
            Paragraph(
                f"~{dto.total_effort_days:.1f} days",
                ParagraphStyle("TotEff", parent=s["mono"], fontSize=8,
                               textColor=colors.HexColor("#3b6ef8")),
            ),
            Paragraph(f"<b>{dto.total_files}</b>", s["badge"]),
            Paragraph("100%", s["badge"]),
        ])

        tbl = Table(rows, colWidths=[18 * mm, 50 * mm, 40 * mm, 22 * mm, 22 * mm])
        style = TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#e2e8f0")),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("FONTSIZE", (0, 0), (-1, -1), 9),
            ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#cbd5e1")),
            ("ROWBACKGROUNDS", (0, 1), (-1, -2), [colors.white, colors.HexColor("#f8fafc")]),
            ("BACKGROUND", (0, -1), (-1, -1), colors.HexColor("#eff6ff")),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("TOPPADDING", (0, 0), (-1, -1), 4),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ("LEFTPADDING", (0, 0), (-1, -1), 5),
        ])
        # Colour-tint the count cell for non-zero rows
        for lvl in range(1, 6):
            if counts[lvl]:
                style.add("BACKGROUND", (0, lvl), (0, lvl), _LEVEL_BG[lvl])
        tbl.setStyle(style)

        band.append(tbl)
        band.append(Spacer(1, 8 * mm))
        band.append(HRFlowable(
            width="100%", thickness=0.5,
            color=colors.HexColor("#e2e8f0"),
        ))
        band.append(Spacer(1, 4 * mm))
        return band

    @staticmethod
    def _build_file_card(row: FileRowDTO, s: dict) -> KeepTogether:
        """
        Per-file detail card — mirrors one iteration of <detail><band> in JRXML,
        driven by JRBeanCollectionDataSource($F{tableDTO}).
        """
        lhex = _LEVEL_HEX[row.complexity_level]
        lbg = _LEVEL_BG[row.complexity_level]
        lc = colors.HexColor(lhex)

        # ── Card header (mirrors jr:columnHeader staticText fields) ───────────
        hdr_data = [[
            Paragraph(
                f"<b>{row.filename}</b>",
                ParagraphStyle("FN", parent=s["normal"], fontSize=10,
                               textColor=colors.HexColor("#0f172a")),
            ),
            Paragraph(
                f"<font color='#64748b'>{row.artifact_type}</font>",
                s["small"],
            ),
            Paragraph(
                f"<b><font color='{lhex}'>L{row.complexity_level} — {row.level_name}</font></b>"
                + (f"  ({row.grade})" if row.grade else ""),
                ParagraphStyle("Lbl", parent=s["right"], fontSize=9),
            ),
        ]]
        hdr = Table(hdr_data, colWidths=[85 * mm, 32 * mm, 52 * mm])
        hdr.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, -1), lbg),
            ("TOPPADDING", (0, 0), (-1, -1), 5),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
            ("LEFTPADDING", (0, 0), (0, 0), 7),
            ("RIGHTPADDING", (-1, 0), (-1, 0), 7),
            ("LINEBELOW", (0, 0), (-1, -1), 1.5, lc),
        ]))

        # ── Stats bar (mirrors textField rows for programNo, balance, etc.) ───
        stats_data = [[
            Paragraph(f"<b>{row.raw_points}</b> pts", s["small"]),
            Paragraph(f"~<b>{row.estimated_effort_days:.1f}</b> days", s["small"]),
            Paragraph(f"<i>{row.effort_range}</i>", s["small"]),
            Paragraph(
                f"<font name='Courier' size='7'>{row.filename}</font>",
                ParagraphStyle("FP", parent=s["small"], fontSize=7),
            ),
        ]]
        stats = Table(stats_data, colWidths=[22 * mm, 28 * mm, 30 * mm, None])
        stats.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#f8fafc")),
            ("TOPPADDING", (0, 0), (-1, -1), 3),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
            ("LEFTPADDING", (0, 0), (0, 0), 7),
        ]))

        # ── Body: breakdown metrics | scoring factors  (mirrors table columns) ─
        bk_lines: list = [
            Paragraph("<b>Metric counts</b>",
                      ParagraphStyle("MH", parent=s["small"], spaceAfter=2))
        ] if row.breakdown else [Paragraph("—", s["small"])]
        for k, v in list(row.breakdown.items())[:18]:
            bk_lines.append(Paragraph(
                f"<font color='#64748b'>{k.replace('_', ' ').title()}</font>: <b>{v}</b>",
                ParagraphStyle("ML", parent=s["small"], leading=10),
            ))

        rs_lines: list = [
            Paragraph("<b>Scoring factors</b>",
                      ParagraphStyle("RH", parent=s["small"], spaceAfter=2))
        ] if row.reasons else [Paragraph("—", s["small"])]
        for r in row.reasons[:18]:
            rs_lines.append(Paragraph(f"· {r}", s["reason"]))

        mn_lines: list = []
        if row.migration_notes:
            mn_lines.append(Paragraph(
                "<b>Migration notes</b>",
                ParagraphStyle("NH", parent=s["small"], spaceAfter=2),
            ))
            for n in row.migration_notes[:10]:
                mn_lines.append(Paragraph(f"→ {n}", s["reason"]))

        body_cols = [bk_lines, rs_lines + ([""] if not mn_lines else mn_lines)]
        body = Table([body_cols], colWidths=[85 * mm, 84 * mm])
        body.setStyle(TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("TOPPADDING", (0, 0), (-1, -1), 6),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
            ("LEFTPADDING", (0, 0), (-1, -1), 7),
            ("RIGHTPADDING", (0, 0), (-1, -1), 7),
            ("LINEAFTER", (0, 0), (0, -1), 0.5, colors.HexColor("#e2e8f0")),
            ("BACKGROUND", (0, 0), (-1, -1), colors.white),
        ]))

        outer = Table(
            [[hdr], [stats], [body]],
            colWidths=[169 * mm],
        )
        outer.setStyle(TableStyle([
            ("BOX", (0, 0), (-1, -1), 0.5, colors.HexColor("#e2e8f0")),
            ("TOPPADDING", (0, 0), (-1, -1), 0),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
            ("LEFTPADDING", (0, 0), (-1, -1), 0),
            ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ]))

        return KeepTogether([outer, Spacer(1, 3 * mm)])

    @staticmethod
    def _build_footer_band(dto: AnalysisReportDTO, s: dict) -> list:
        """Page footer band — mirrors <pageFooter> in JRXML."""
        band = []
        band.append(Spacer(1, 6 * mm))
        band.append(HRFlowable(
            width="100%", thickness=0.5,
            color=colors.HexColor("#e2e8f0"),
        ))
        band.append(Spacer(1, 2 * mm))
        band.append(Paragraph(
            f"Generated by {dto.app_name} · {dto.scoring_model}",
            ParagraphStyle("Footer", parent=s["meta"], alignment=TA_CENTER),
        ))
        return band
