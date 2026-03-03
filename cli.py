#!/usr/bin/env python3
"""
Oracle Migrator — Command Line Interface
Usage: python cli.py <command> [options]
"""
import os
import sys
import json
import argparse
import zipfile
import tempfile
from pathlib import Path
from datetime import datetime

from oracle_migrator.parsers import FormsParser, ReportsParser
from oracle_migrator.core import ComplexityAnalyzer
from oracle_migrator.converters import JavaConverter, JasperConverter

ALLOWED = {".fmt", ".xml", ".rdf", ".fmb", ".rpt", ".txt"}

# ── Terminal colors ───────────────────────────────────────────────────────────
GREEN  = "\033[92m"
YELLOW = "\033[93m"
RED    = "\033[91m"
BLUE   = "\033[94m"
BOLD   = "\033[1m"
RESET  = "\033[0m"
CYAN   = "\033[96m"

def color(text, c): return f"{c}{text}{RESET}"
def success(t): return color(t, GREEN)
def warn(t):    return color(t, YELLOW)
def err(t):     return color(t, RED)
def info(t):    return color(t, CYAN)
def bold(t):    return color(t, BOLD)


# ── Helpers ───────────────────────────────────────────────────────────────────

def detect_type(path: str) -> str:
    name = Path(path).name.lower()
    if any(x in name for x in ("report", "rpt")):
        return "REPORT"
    if Path(path).suffix.lower() in (".rdf",):
        return "REPORT"
    try:
        content = Path(path).read_text(errors="replace")[:600]
        if any(x in content for x in ("BeforeReport", "AfterReport", "<Report ", "BEFORE-REPORT")):
            return "REPORT"
    except Exception:
        pass
    return "FORM"


def collect_files(paths):
    result = []
    for p in paths:
        fp = Path(p)
        if fp.is_dir():
            for f in sorted(fp.rglob("*")):
                if f.is_file() and f.suffix.lower() in ALLOWED:
                    result.append(str(f))
        elif fp.is_file() and fp.suffix.lower() in ALLOWED:
            result.append(str(fp))
    return result


def parse_and_analyze(file_path):
    art_type = detect_type(file_path)
    parser = ReportsParser() if art_type == "REPORT" else FormsParser()
    artifact = parser.parse(file_path)
    analyzer = ComplexityAnalyzer()
    report = analyzer.analyze(artifact)
    return artifact, report


# ── Commands ──────────────────────────────────────────────────────────────────

def cmd_analyze(args):
    files = collect_files(args.paths)
    if not files:
        print(err("No valid Oracle files found."))
        sys.exit(1)

    print(f"\n{bold('ORACLE MIGRATOR — COMPLEXITY ANALYSIS')}")
    print("=" * 62)
    reports = []

    for fp in files:
        try:
            artifact, rep = parse_and_analyze(fp)
            reports.append(rep)

            level_color = (success, warn, err)[rep.score - 1]
            level_bar = ("■□□", "■■□", "■■■")[rep.score - 1]

            print(f"\n{'─' * 62}")
            print(f"  {bold(artifact.name)}  [{artifact.artifact_type}]")
            print(f"  {level_color(f'[{level_bar}] Level {rep.score} — {rep.label}')}")
            print(f"  File: {artifact.file_path}")
            print(f"  Est. effort: ~{rep.estimated_effort_days} days")
            print(f"{'─' * 62}")
            print(f"  {'Metric':<30} {'Value':>5}  Bar")
            print(f"  {'─'*56}")
            for k, v in rep.breakdown.items():
                if v:
                    bar = "█" * min(v if isinstance(v, int) else 0, 20)
                    label = k.replace("_", " ").title()
                    print(f"  {label:<30} {str(v):>5}  {bar}")
            print(f"\n  Complexity factors:")
            for r in rep.reasons:
                print(f"  {BLUE}•{RESET} {r}")
            if rep.migration_notes:
                print(f"\n  Migration notes:")
                for n in rep.migration_notes:
                    print(f"  {CYAN}→{RESET} {n}")
        except Exception as e:
            print(err(f"  Error processing '{fp}': {e}"))

    # Summary table
    print(f"\n{'=' * 62}")
    print(f"  SUMMARY ({len(reports)} artifacts analyzed)")
    print(f"{'─' * 62}")
    print(f"  {'Name':<28} {'Type':<8} {'Level':<22} {'Effort'}")
    print(f"  {'─' * 56}")
    for r in reports:
        lc = (success, warn, err)[r.score - 1]
        bar = ("■□□", "■■□", "■■■")[r.score - 1]
        print(f"  {r.artifact.name:<28} {r.artifact.artifact_type:<8} {lc(f'[{bar}] {r.label}'):<30} {r.estimated_effort_days}d")

    counts = [r.score for r in reports]
    print(f"\n  Simple(1): {counts.count(1)} | Moderate(2): {counts.count(2)} | Complex(3): {counts.count(3)}")
    avg = sum(counts) / len(counts) if counts else 0
    print(f"  Average complexity: {avg:.2f}/3.0")
    print(f"{'=' * 62}\n")

    # JSON export
    if args.json_out:
        data = [r.to_dict() for r in reports]
        Path(args.json_out).write_text(json.dumps(data, indent=2))
        print(success(f"Analysis exported to: {args.json_out}"))


def cmd_convert(args):
    files = collect_files(args.paths)
    if not files:
        print(err("No valid Oracle files found."))
        sys.exit(1)

    out_root = Path(args.output)
    out_root.mkdir(parents=True, exist_ok=True)
    target = args.target

    java_conv = JavaConverter()
    jasper_conv = JasperConverter()

    print(f"\n{bold('ORACLE MIGRATOR — CONVERSION')}")
    print(f"Target: {bold(target)} | Output: {out_root}\n")

    all_files = []
    for fp in files:
        try:
            artifact, rep = parse_and_analyze(fp)
            art_out = out_root / artifact.name
            created = []

            if artifact.artifact_type == "REPORT":
                if target in ("jasper", "both"):
                    created += jasper_conv.convert(artifact, str(art_out / "jasper"))
                if target == "java":
                    created += java_conv.convert(artifact, str(art_out / "java"))
            else:
                if target in ("java", "both"):
                    created += java_conv.convert(artifact, str(art_out / "java"))
                if target in ("jasper", "both"):
                    created += jasper_conv.convert(artifact, str(art_out / "jasper"))

            all_files.extend(created)
            lc = (success, warn, err)[rep.score - 1]
            print(f"  {lc('✓')} {artifact.name} [{artifact.artifact_type}] — {len(created)} files")

        except Exception as e:
            print(err(f"  ✗ Error: {fp} — {e}"))

    # Create ZIP
    if args.zip:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        zip_path = out_root.parent / f"oracle_migrated_{ts}.zip"
        with zipfile.ZipFile(str(zip_path), "w", zipfile.ZIP_DEFLATED) as zf:
            for f in out_root.rglob("*"):
                if f.is_file():
                    zf.write(f, f.relative_to(out_root))
        print(success(f"\n✓ ZIP created: {zip_path}"))
    else:
        print(success(f"\n✓ {len(all_files)} files generated in: {out_root}"))

    # Show tree
    print(f"\nGenerated file tree:")
    for f in all_files[:25]:
        rel = Path(f).relative_to(out_root)
        print(f"  {info('•')} {rel}")
    if len(all_files) > 25:
        print(f"  ... and {len(all_files) - 25} more")


def cmd_pipeline(args):
    """Full analyze + convert pipeline."""
    print(bold("\n⚙️  FULL PIPELINE: Analyze → Convert → ZIP\n"))
    args.json_out = str(Path(args.output) / "analysis.json")
    cmd_analyze(args)
    args.zip = True
    cmd_convert(args)


def cmd_demo(args):
    """Create sample files and run full pipeline."""
    from oracle_migrator.samples import create_samples
    sample_dir = Path("./demo_samples")
    create_samples(str(sample_dir))
    print(success(f"Created sample files in: {sample_dir}\n"))

    args.paths = [str(sample_dir)]
    args.output = "./demo_output"
    args.target = "both"
    args.json_out = "./demo_output/analysis.json"
    args.zip = True
    cmd_pipeline(args)


# ── Argument parser ───────────────────────────────────────────────────────────

def main():
    p = argparse.ArgumentParser(
        prog="oracle_migrator",
        description=bold("Oracle Forms & Reports Migration Tool"),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=f"""
{bold("Examples:")}
  python cli.py demo
  python cli.py analyze sample_files/
  python cli.py analyze form.fmt report.xml --json-out results.json
  python cli.py convert sample_files/ --target java --output ./out
  python cli.py convert sample_files/ --target both --output ./out --zip
  python cli.py pipeline sample_files/ --output ./out
        """,
    )

    sub = p.add_subparsers(dest="command", required=True)

    # analyze
    a = sub.add_parser("analyze", help="Analyze complexity of Oracle files")
    a.add_argument("paths", nargs="+", help="Files or directories (.fmt, .xml, .rdf, ...)")
    a.add_argument("--json-out", default=None, help="Export results to JSON file")

    # convert
    c = sub.add_parser("convert", help="Convert Oracle files to Java/JasperReports")
    c.add_argument("paths", nargs="+", help="Files or directories to convert")
    c.add_argument("--target", choices=["java", "jasper", "both"], default="both")
    c.add_argument("--output", default="./migrated_output", help="Output directory")
    c.add_argument("--zip", action="store_true", help="Create ZIP archive of output")
    c.add_argument("--json-out", default=None)

    # pipeline
    pp = sub.add_parser("pipeline", help="Analyze + convert (full pipeline)")
    pp.add_argument("paths", nargs="+")
    pp.add_argument("--target", choices=["java", "jasper", "both"], default="both")
    pp.add_argument("--output", default="./migrated_output")
    pp.add_argument("--json-out", default=None)

    # demo
    sub.add_parser("demo", help="Run with built-in sample files")

    args = p.parse_args()
    dispatch = {
        "analyze": cmd_analyze,
        "convert": cmd_convert,
        "pipeline": cmd_pipeline,
        "demo": cmd_demo,
    }
    dispatch[args.command](args)


if __name__ == "__main__":
    main()
