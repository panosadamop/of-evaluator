"""
eEFKA Oracle Forms Migrator — Flask Web Application
"""
import os
import re
import io
import json
import zipfile
import tempfile
import traceback
from pathlib import Path
from datetime import datetime

from reportlab.lib.pagesizes import A4
from reportlab.lib import colors
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import mm
from reportlab.lib.enums import TA_LEFT, TA_CENTER, TA_RIGHT
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle,
    HRFlowable, KeepTogether
)

from flask import (
    Flask, request, render_template, redirect, url_for,
    flash, send_file, jsonify, session
)
from werkzeug.exceptions import RequestEntityTooLarge
from werkzeug.utils import secure_filename

from oracle_migrator.parsers import FormsParser, ReportsParser
from oracle_migrator.parsers.forms_parser import FmbBinaryError
from oracle_migrator.core import ComplexityAnalyzer
from oracle_migrator.converters import JavaConverter, JasperConverter

# ── App setup ─────────────────────────────────────────────────────────────────

app = Flask(__name__, template_folder="oracle_migrator/templates_html")
app.secret_key = os.environ.get("SECRET_KEY", "eefka-migrator-dev-key-change-in-prod")

# No upload size cap — we handle large folders and ZIPs.
# Set via env var EFKA_MAX_UPLOAD_MB if you need a hard limit (default: unlimited).
_max_mb = os.environ.get("EFKA_MAX_UPLOAD_MB")
app.config["MAX_CONTENT_LENGTH"] = int(_max_mb) * 1024 * 1024 if _max_mb else None

UPLOAD_FOLDER = Path(tempfile.gettempdir()) / "eefka_migrator_uploads"
OUTPUT_FOLDER = Path(tempfile.gettempdir()) / "eefka_migrator_outputs"
UPLOAD_FOLDER.mkdir(exist_ok=True)
OUTPUT_FOLDER.mkdir(exist_ok=True)

# Files we can parse directly
ORACLE_EXTENSIONS = {".fmt", ".xml", ".rdf", ".fmb", ".rpt", ".txt"}
# .fmb is accepted so we can give a proper error message instead of "unsupported type"
# We also accept ZIP archives (extracted server-side)
UPLOAD_EXTENSIONS = ORACLE_EXTENSIONS | {".zip"}

SAMPLE_DIR = Path(__file__).parent / "sample_files"

APP_NAME = "eEFKA Oracle Forms Migrator"


# ── Helpers ───────────────────────────────────────────────────────────────────

def allowed_file(filename: str) -> bool:
    return Path(filename).suffix.lower() in UPLOAD_EXTENSIONS


def is_oracle_file(filename: str) -> bool:
    return Path(filename).suffix.lower() in ORACLE_EXTENSIONS


def detect_type(path: str) -> str:
    """
    Determine whether a file is an Oracle Report or Oracle Form.

    Oracle Reports XML exports have root element <report ...> (any case).
    The Re_Forms 21 JAR specifically checks for a 'report' root tag.
    We apply the same check here, plus fallback heuristics.
    """
    suffix = Path(path).suffix.lower()

    # Extension-based fast path
    if suffix in (".rdf", ".rpt"):
        return "REPORT"

    try:
        content = Path(path).read_text(errors="replace")
    except Exception:
        return "FORM"

    # Primary check: XML root tag is 'report' (case-insensitive)
    # Handles: <report ...>, <Report ...>, <REPORT ...>
    # Strip optional XML declaration and BOM first
    stripped = content.lstrip("\ufeff").lstrip()
    # Skip <?xml ...?> processing instruction if present
    if stripped.startswith("<?"):
        end = stripped.find("?>")
        stripped = stripped[end + 2:].lstrip() if end != -1 else stripped

    if re.match(r"<[Rr][Ee][Pp][Oo][Rr][Tt][\s>]", stripped):
        return "REPORT"

    # Secondary heuristics for non-standard / partial exports
    report_signals = (
        "BeforeReport", "AfterReport", "BEFORE-REPORT", "AFTER-REPORT",
        "<repeatingFrame", "<dataSource", "<userParameter",
        "<programUnits", "<attachedLibrary",
    )
    if any(sig in content for sig in report_signals):
        return "REPORT"

    return "FORM"


def parse_and_analyze(file_path: str):
    art_type = detect_type(file_path)
    parser = ReportsParser() if art_type == "REPORT" else FormsParser()
    artifact = parser.parse(file_path)
    analyzer = ComplexityAnalyzer()
    report = analyzer.analyze(artifact)
    return artifact, report


def level_color(score: int) -> str:
    return {1: "success", 2: "info", 3: "warning", 4: "danger", 5: "dark"}.get(score, "secondary")


def level_icon(score: int) -> str:
    return {1: "✅", 2: "🟦", 3: "⚠️", 4: "🔴", 5: "⛔"}.get(score, "❓")


LEVEL_LABELS = {1: "Trivial", 2: "Simple", 3: "Moderate", 4: "Complex", 5: "Very Complex"}


def extract_oracle_files_from_zip(zip_bytes: bytes, dest_dir: Path) -> list[Path]:
    """
    Extract all Oracle-compatible files from a ZIP archive recursively.
    Returns list of extracted file paths.
    """
    extracted = []
    try:
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            for entry in zf.infolist():
                if entry.is_dir():
                    continue
                entry_path = Path(entry.filename)
                # Skip hidden / system files
                if any(part.startswith((".", "__MACOSX")) for part in entry_path.parts):
                    continue
                if entry_path.suffix.lower() not in ORACLE_EXTENSIONS:
                    continue
                # Flatten into dest_dir with a safe unique name
                safe_name = secure_filename(entry_path.name)
                if not safe_name:
                    continue
                # Avoid collisions by prefixing with parent dir name
                parent = entry_path.parent.name
                if parent and parent not in (".", ""):
                    safe_name = f"{secure_filename(parent)}__{safe_name}"
                out_path = dest_dir / safe_name
                # Handle name collision
                if out_path.exists():
                    stem = out_path.stem
                    suffix = out_path.suffix
                    i = 1
                    while out_path.exists():
                        out_path = dest_dir / f"{stem}_{i}{suffix}"
                        i += 1
                out_path.write_bytes(zf.read(entry.filename))
                extracted.append(out_path)
    except zipfile.BadZipFile:
        pass
    return extracted


def save_uploaded_files(file_list) -> tuple[list[Path], list[str]]:
    """
    Save uploaded files to UPLOAD_FOLDER.
    Handles both direct Oracle files and ZIP archives containing Oracle files.
    Returns (oracle_file_paths, error_messages).
    """
    oracle_paths = []
    errors = []

    for f in file_list:
        if not f or not f.filename:
            continue

        fname = secure_filename(f.filename)
        suffix = Path(fname).suffix.lower()

        if suffix == ".zip":
            # Read ZIP in memory and extract
            try:
                zip_bytes = f.read()
                extracted = extract_oracle_files_from_zip(zip_bytes, UPLOAD_FOLDER)
                if extracted:
                    oracle_paths.extend(extracted)
                else:
                    errors.append(f"'{fname}' — no supported Oracle files found inside ZIP")
            except Exception as e:
                errors.append(f"'{fname}' — could not read ZIP: {e}")

        elif suffix in ORACLE_EXTENSIONS:
            save_path = UPLOAD_FOLDER / fname
            try:
                f.save(str(save_path))
                oracle_paths.append(save_path)
            except Exception as e:
                errors.append(f"'{fname}' — save failed: {e}")

        else:
            errors.append(f"Skipped '{fname}' — unsupported file type")

    return oracle_paths, errors


# ── Error handlers ────────────────────────────────────────────────────────────

@app.errorhandler(RequestEntityTooLarge)
def handle_too_large(e):
    limit_mb = app.config.get("MAX_CONTENT_LENGTH")
    if limit_mb:
        limit_str = f"{limit_mb // (1024*1024)} MB"
        msg = (f"Upload rejected: total upload exceeds the {limit_str} server limit. "
               f"Split your files into smaller batches or raise the limit via the "
               f"EFKA_MAX_UPLOAD_MB environment variable.")
    else:
        msg = "Upload rejected: request entity too large."
    if request.is_json or request.path.startswith("/api/"):
        return jsonify({"error": msg}), 413
    flash(msg, "danger")
    # Return to whichever page the user was on
    referrer = request.referrer or url_for("analyze")
    return redirect(referrer), 413


@app.errorhandler(413)
def handle_413(e):
    return handle_too_large(e)


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html", app_name=APP_NAME)


@app.route("/analyze", methods=["GET", "POST"])
def analyze():
    if request.method == "GET":
        return render_template("analyze.html", app_name=APP_NAME)

    oracle_paths, errors = save_uploaded_files(request.files.getlist("files"))
    results = []

    # Sample files
    sample_names = request.form.getlist("samples")
    for sname in sample_names:
        spath = SAMPLE_DIR / sname
        if spath.exists():
            oracle_paths.append(spath)

    for fpath in oracle_paths:
        try:
            artifact, report = parse_and_analyze(str(fpath))
            results.append({
                "report": report,
                "color": level_color(report.score),
                "icon": level_icon(report.score),
            })
        except FmbBinaryError as e:
            errors.append(str(e))
        except Exception as e:
            errors.append(f"Error processing '{fpath.name}': {e}")

    if not results and not errors:
        flash("No valid Oracle files found in the upload.", "warning")
        return redirect(url_for("analyze"))

    session["last_results"] = [r["report"].to_dict() for r in results]

    return render_template("analyze.html",
                           app_name=APP_NAME,
                           results=results,
                           errors=errors,
                           total=len(results),
                           l1=sum(1 for r in results if r["report"].score == 1),
                           l2=sum(1 for r in results if r["report"].score == 2),
                           l3=sum(1 for r in results if r["report"].score == 3),
                           l4=sum(1 for r in results if r["report"].score == 4),
                           l5=sum(1 for r in results if r["report"].score == 5),
                           )


@app.route("/analyze/export-json")
def export_json():
    data = session.get("last_results", [])
    if not data:
        flash("No analysis results to export.", "warning")
        return redirect(url_for("analyze"))
    tmp = tempfile.mktemp(suffix=".json")
    Path(tmp).write_text(json.dumps(data, indent=2))
    return send_file(tmp, as_attachment=True,
                     download_name=f"eefka_analysis_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json",
                     mimetype="application/json")


@app.route("/analyze/export-pdf")
def export_pdf():

    data = session.get("last_results", [])
    if not data:
        flash("No analysis results to export.", "warning")
        return redirect(url_for("analyze"))

    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=A4,
        leftMargin=18*mm, rightMargin=18*mm,
        topMargin=18*mm, bottomMargin=18*mm,
        title="Oracle Complexity Analysis Report",
    )

    # ── Colour palette for 5 levels ──────────────────────────────────────────
    LEVEL_HEX = {
        1: "198754",
        2: "0dcaf0",
        3: "fd7e14",
        4: "dc3545",
        5: "212529",
    }
    LEVEL_BG = {
        1: colors.HexColor("#d1e7dd"),
        2: colors.HexColor("#cff4fc"),
        3: colors.HexColor("#fff3cd"),
        4: colors.HexColor("#f8d7da"),
        5: colors.HexColor("#e2e3e5"),
    }
    LEVEL_COLORS = {k: colors.HexColor(f"#{v}") for k, v in LEVEL_HEX.items()}
    LEVEL_NAMES = {1: "Trivial", 2: "Simple", 3: "Moderate", 4: "Complex", 5: "Very Complex"}

    styles = getSampleStyleSheet()
    normal     = styles["Normal"]
    h1         = ParagraphStyle("H1", parent=styles["Title"],   fontSize=18, spaceAfter=4,  textColor=colors.HexColor("#0d6efd"))
    h2         = ParagraphStyle("H2", parent=styles["Heading2"],fontSize=11, spaceBefore=10,spaceAfter=3, textColor=colors.HexColor("#212529"))
    small_grey = ParagraphStyle("SG", parent=normal, fontSize=8,  textColor=colors.grey)
    mono       = ParagraphStyle("Mono",parent=normal,fontSize=8,  fontName="Courier", textColor=colors.HexColor("#495057"))
    reason_sty = ParagraphStyle("RS", parent=normal,fontSize=8.5,leading=12, textColor=colors.HexColor("#495057"))
    badge_sty  = ParagraphStyle("BS", parent=normal,fontSize=9,   alignment=TA_CENTER)

    story = []

    # ── Title block ──────────────────────────────────────────────────────────
    story.append(Paragraph("Oracle Forms &amp; Reports", h1))
    story.append(Paragraph("Complexity Analysis Report", ParagraphStyle(
        "Sub", parent=styles["Normal"], fontSize=13, textColor=colors.HexColor("#6c757d"), spaceAfter=2)))
    story.append(Paragraph(
        f"Generated: {datetime.now().strftime('%d %b %Y, %H:%M')} &nbsp;|&nbsp; {len(data)} file(s) analysed",
        small_grey))
    story.append(Spacer(1, 6*mm))

    # ── Summary table ─────────────────────────────────────────────────────────
    counts = {i: sum(1 for d in data if d.get("complexity_level") == i) for i in range(1, 6)}
    summary_rows = [
        [Paragraph("<b>Level</b>", badge_sty),
         Paragraph("<b>Label</b>",  badge_sty),
         Paragraph("<b>Count</b>",  badge_sty),
         Paragraph("<b>% of total</b>", badge_sty)],
    ]
    for lvl in range(1, 6):
        cnt = counts[lvl]
        pct = f"{cnt/len(data)*100:.0f}%" if data else "0%"
        summary_rows.append([
            Paragraph(f"<b>L{lvl}</b>", ParagraphStyle("Lv", parent=badge_sty, textColor=LEVEL_COLORS[lvl])),
            Paragraph(LEVEL_NAMES[lvl], badge_sty),
            Paragraph(str(cnt), badge_sty),
            Paragraph(pct, badge_sty),
        ])
    sum_tbl = Table(summary_rows, colWidths=[22*mm, 50*mm, 28*mm, 35*mm])
    sum_tbl.setStyle(TableStyle([
        ("BACKGROUND",  (0,0), (-1,0), colors.HexColor("#e9ecef")),
        ("FONTNAME",    (0,0), (-1,0), "Helvetica-Bold"),
        ("FONTSIZE",    (0,0), (-1,-1), 9),
        ("GRID",        (0,0), (-1,-1), 0.5, colors.HexColor("#dee2e6")),
        ("ROWBACKGROUNDS", (0,1), (-1,-1), [colors.white, colors.HexColor("#f8f9fa")]),
        ("VALIGN",      (0,0), (-1,-1), "MIDDLE"),
        ("TOPPADDING",  (0,0), (-1,-1), 4),
        ("BOTTOMPADDING",(0,0),(-1,-1), 4),
    ]))
    story.append(sum_tbl)
    story.append(Spacer(1, 8*mm))
    story.append(HRFlowable(width="100%", thickness=1, color=colors.HexColor("#dee2e6")))
    story.append(Spacer(1, 4*mm))

    # ── Per-file detail cards ─────────────────────────────────────────────────
    story.append(Paragraph("File Details", h2))
    story.append(Spacer(1, 2*mm))

    for d in data:
        lvl   = d.get("complexity_level", 1)
        name  = Path(d.get("file", "?")).name
        atype = d.get("artifact_type", "")
        label = d.get("label", "")
        pts   = d.get("raw_points", 0)
        effort= d.get("estimated_effort_days", 0)
        grade = d.get("grade", "")
        reasons = d.get("reasons", [])
        breakdown = d.get("breakdown", {})

        bg   = LEVEL_BG[lvl]
        col  = LEVEL_COLORS[lvl]

        # Header row
        hdr_data = [[
            Paragraph(f"<b>{name}</b>", ParagraphStyle("FN", parent=normal, fontSize=10)),
            Paragraph(f"<font color='#6c757d'>{atype}</font>", small_grey),
            Paragraph(
                f"<b><font color='#{LEVEL_HEX[lvl]}'>L{lvl} — {LEVEL_NAMES[lvl]}</font></b>"
                f"{'  (' + grade + ')' if grade and grade not in LEVEL_NAMES[lvl] else ''}",
                ParagraphStyle("Lbl", parent=normal, fontSize=9, alignment=TA_RIGHT)),
        ]]
        hdr_tbl = Table(hdr_data, colWidths=[85*mm, 30*mm, 55*mm])
        hdr_tbl.setStyle(TableStyle([
            ("BACKGROUND", (0,0), (-1,-1), bg),
            ("TOPPADDING",  (0,0), (-1,-1), 5),
            ("BOTTOMPADDING",(0,0),(-1,-1), 5),
            ("LEFTPADDING", (0,0), (0,0), 6),
            ("RIGHTPADDING",(-1,0),(-1,0),6),
            ("LINEBELOW", (0,0), (-1,-1), 1, col),
        ]))

        # Stats bar
        stats_data = [[
            Paragraph(f"<b>{pts}</b> pts", small_grey),
            Paragraph(f"~<b>{effort}</b> days effort", small_grey),
            Paragraph(f"File: <font name='Courier'>{d.get('file','')}</font>",
                      ParagraphStyle("FP", parent=small_grey, fontSize=7)),
        ]]
        stats_tbl = Table(stats_data, colWidths=[25*mm, 35*mm, 110*mm])
        stats_tbl.setStyle(TableStyle([
            ("BACKGROUND", (0,0), (-1,-1), colors.HexColor("#f8f9fa")),
            ("TOPPADDING",  (0,0), (-1,-1), 3),
            ("BOTTOMPADDING",(0,0),(-1,-1), 3),
            ("LEFTPADDING", (0,0), (0,0), 6),
        ]))

        # Body: breakdown + reasons side by side
        body_rows = []

        # Breakdown metrics
        if breakdown:
            bk_lines = [Paragraph("<b>Metric counts</b>", ParagraphStyle("MH", parent=small_grey, spaceAfter=2))]
            for k, v in list(breakdown.items())[:20]:
                bk_lines.append(Paragraph(
                    f"<font color='#6c757d'>{k.replace('_',' ').title()}</font>: <b>{v}</b>",
                    ParagraphStyle("ML", parent=small_grey, leading=10)))
        else:
            bk_lines = [Paragraph("—", small_grey)]

        # Reasons
        if reasons:
            rs_lines = [Paragraph("<b>Scoring factors</b>", ParagraphStyle("RH", parent=small_grey, spaceAfter=2))]
            for r in reasons[:20]:
                rs_lines.append(Paragraph(f"• {r}", reason_sty))
        else:
            rs_lines = [Paragraph("—", small_grey)]

        body_data = [[bk_lines, rs_lines]]
        body_tbl = Table(body_data, colWidths=[85*mm, 85*mm])
        body_tbl.setStyle(TableStyle([
            ("VALIGN",       (0,0), (-1,-1), "TOP"),
            ("TOPPADDING",   (0,0), (-1,-1), 6),
            ("BOTTOMPADDING",(0,0), (-1,-1), 6),
            ("LEFTPADDING",  (0,0), (-1,-1), 6),
            ("RIGHTPADDING", (0,0), (-1,-1), 6),
            ("LINEAFTER",    (0,0), (0,-1),  0.5, colors.HexColor("#dee2e6")),
            ("BACKGROUND",   (0,0), (-1,-1), colors.white),
        ]))

        card = KeepTogether([hdr_tbl, stats_tbl, body_tbl, Spacer(1, 3*mm)])
        story.append(card)

    # ── Footer note ───────────────────────────────────────────────────────────
    story.append(Spacer(1, 4*mm))
    story.append(HRFlowable(width="100%", thickness=0.5, color=colors.HexColor("#dee2e6")))
    story.append(Spacer(1, 2*mm))
    story.append(Paragraph(
        "Generated by eEFKA Oracle Migrator · Re_Forms 21 scoring model (validated against 87 real reports)",
        ParagraphStyle("Footer", parent=small_grey, alignment=TA_CENTER)))

    doc.build(story)
    buf.seek(0)
    fname = f"complexity_report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.pdf"
    return send_file(buf, as_attachment=True, download_name=fname, mimetype="application/pdf")




@app.route("/convert", methods=["GET", "POST"])
def convert():
    if request.method == "GET":
        samples = sorted(SAMPLE_DIR.glob("*")) if SAMPLE_DIR.exists() else []
        return render_template("convert.html", app_name=APP_NAME, samples=[s.name for s in samples])

    oracle_paths, errors = save_uploaded_files(request.files.getlist("files"))
    sample_names = request.form.getlist("samples")
    target = request.form.get("target", "both")

    for sname in sample_names:
        spath = SAMPLE_DIR / sname
        if spath.exists():
            oracle_paths.append(spath)

    if not oracle_paths:
        flash("No valid Oracle files found in the upload.", "warning")
        return redirect(url_for("convert"))

    run_id = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_root = OUTPUT_FOLDER / run_id
    out_root.mkdir(parents=True, exist_ok=True)

    java_conv = JavaConverter()
    jasper_conv = JasperConverter()
    converted = []

    for fpath in oracle_paths:
        try:
            artifact, report = parse_and_analyze(str(fpath))
            art_out = out_root / artifact.name
            created_files = []

            if artifact.artifact_type == "REPORT":
                if target in ("jasper", "both"):
                    created_files.extend(jasper_conv.convert(artifact, str(art_out / "jasper")))
                if target == "java":
                    created_files.extend(java_conv.convert(artifact, str(art_out / "java")))
            else:
                if target in ("java", "both"):
                    created_files.extend(java_conv.convert(artifact, str(art_out / "java")))
                if target in ("jasper", "both"):
                    created_files.extend(jasper_conv.convert(artifact, str(art_out / "jasper")))

            converted.append({
                "name": artifact.name,
                "type": artifact.artifact_type,
                "files_count": len(created_files),
                "complexity": report.label,
                "complexity_score": report.score,
                "color": level_color(report.score),
                "icon": level_icon(report.score),
            })
        except FmbBinaryError as e:
            errors.append(str(e))
            app.logger.debug(traceback.format_exc())
        except Exception as e:
            errors.append(f"Error converting '{fpath.name}': {e}")
            app.logger.debug(traceback.format_exc())

    if not converted:
        flash("No files were successfully converted.", "danger")
        return redirect(url_for("convert"))

    zip_path = OUTPUT_FOLDER / f"eefka_migrated_{run_id}.zip"
    with zipfile.ZipFile(str(zip_path), "w", zipfile.ZIP_DEFLATED) as zf:
        for f in out_root.rglob("*"):
            if f.is_file():
                zf.write(f, f.relative_to(out_root))

    session["last_zip"] = str(zip_path)
    session["last_zip_name"] = zip_path.name

    return render_template("convert.html",
                           app_name=APP_NAME,
                           converted=converted,
                           errors=errors,
                           run_id=run_id,
                           zip_ready=True,
                           samples=[],
                           )


@app.route("/download/<run_id>")
def download(run_id: str):
    zip_path = session.get("last_zip", "")
    if not zip_path or not Path(zip_path).exists():
        flash("Download expired or not found. Please convert again.", "warning")
        return redirect(url_for("convert"))
    return send_file(zip_path, as_attachment=True,
                     download_name=session.get("last_zip_name", f"eefka_migrated_{run_id}.zip"),
                     mimetype="application/zip")


@app.route("/samples")
def samples():
    sample_list = []
    if SAMPLE_DIR.exists():
        for f in sorted(SAMPLE_DIR.glob("*")):
            if f.is_file():
                art_type = detect_type(str(f))
                sample_list.append({"name": f.name, "type": art_type, "size": f.stat().st_size})
    return render_template("samples.html", app_name=APP_NAME, samples=sample_list)


@app.route("/api/analyze", methods=["POST"])
def api_analyze():
    """REST endpoint — accepts a single Oracle file or a ZIP archive."""
    if "file" not in request.files:
        return jsonify({"error": "No file provided"}), 400
    f = request.files["file"]
    if not allowed_file(f.filename):
        return jsonify({"error": "Unsupported file type"}), 400

    oracle_paths, errs = save_uploaded_files([f])
    if not oracle_paths:
        return jsonify({"error": errs[0] if errs else "No Oracle files found"}), 400

    results = []
    for fpath in oracle_paths:
        try:
            artifact, report = parse_and_analyze(str(fpath))
            results.append(report.to_dict())
        except Exception as e:
            results.append({"error": str(e), "file": fpath.name})

    return jsonify(results if len(results) > 1 else results[0])


if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=5050)
