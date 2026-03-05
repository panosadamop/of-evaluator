"""
oracle-migrator — Flask Web Application
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

from oracle_migrator.report_engine import ReportEngine, AnalysisReportDTO

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
app.secret_key = os.environ.get("SECRET_KEY", "oracle-migrator-dev-key-change-in-prod")

# No upload size cap — we handle large folders and ZIPs.
# Set via env var EFKA_MAX_UPLOAD_MB if you need a hard limit (default: unlimited).
_max_mb = os.environ.get("EFKA_MAX_UPLOAD_MB")
app.config["MAX_CONTENT_LENGTH"] = int(_max_mb) * 1024 * 1024 if _max_mb else None

UPLOAD_FOLDER = Path(tempfile.gettempdir()) / "oracle_migrator_uploads"
OUTPUT_FOLDER = Path(tempfile.gettempdir()) / "oracle_migrator_outputs"
UPLOAD_FOLDER.mkdir(exist_ok=True)
OUTPUT_FOLDER.mkdir(exist_ok=True)

# Files we can parse directly
ORACLE_EXTENSIONS = {".fmt", ".xml", ".rdf", ".fmb", ".rpt", ".txt"}
# .fmb is accepted so we can give a proper error message instead of "unsupported type"
# We also accept ZIP archives (extracted server-side)
UPLOAD_EXTENSIONS = ORACLE_EXTENSIONS | {".zip"}

SAMPLE_DIR = Path(__file__).parent / "sample_files"

APP_NAME = "oracle-migrator"


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


@app.route("/analyze/export-pdf")
def export_pdf():
    """
    PDF export endpoint — mirrors DashboardController.printReport().
    Uses ReportEngine (JasperEngine pattern) + AnalysisReportDTO (bean DTO pattern).
    """
    data = session.get("last_results", [])
    if not data:
        flash("No analysis results to export.", "warning")
        return redirect(url_for("analyze"))

    # Build DTO from session — mirrors createDummyRecord() / bean collection pattern
    dto = AnalysisReportDTO.from_session(data)

    # Compile → fill → export (mirrors JasperEngine.renderReport one-shot path)
    buf = ReportEngine.render_and_export(dto)

    fname = f"complexity_report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.pdf"
    return send_file(buf, as_attachment=True, download_name=fname, mimetype="application/pdf")


@app.route("/analyze/export-json")
def export_json():
    data = session.get("last_results", [])
    if not data:
        flash("No analysis results to export.", "warning")
        return redirect(url_for("analyze"))
    tmp = tempfile.mktemp(suffix=".json")
    Path(tmp).write_text(json.dumps(data, indent=2))
    return send_file(tmp, as_attachment=True,
                     download_name=f"oracle_migrator_analysis_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json",
                     mimetype="application/json")







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

    zip_path = OUTPUT_FOLDER / f"oracle_migrator_migrated_{run_id}.zip"
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
                     download_name=session.get("last_zip_name", f"oracle_migrator_migrated_{run_id}.zip"),
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


@app.route("/content-types")
def content_types():
    return render_template("content_types.html", app_name=APP_NAME)


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
