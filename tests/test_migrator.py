"""
Tests for Oracle Migrator Tool
Run: python -m pytest tests/ -v
"""
import sys
import os
import tempfile
import json
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from oracle_migrator.parsers import FormsParser, ReportsParser
from oracle_migrator.parsers.forms_parser import FmbBinaryError
from oracle_migrator.core import ComplexityAnalyzer, ComplexityReport
from oracle_migrator.converters import JavaConverter, JasperConverter
from oracle_migrator.samples import SAMPLES, create_samples


# ── Fixtures ──────────────────────────────────────────────────────────────────

def make_temp_file(content: str, suffix: str = ".fmt") -> str:
    f = tempfile.NamedTemporaryFile(mode="w", suffix=suffix, delete=False)
    f.write(content)
    f.close()
    return f.name

def make_temp_bytes(data: bytes, suffix: str = ".fmb") -> str:
    f = tempfile.NamedTemporaryFile(mode="wb", suffix=suffix, delete=False)
    f.write(data)
    f.close()
    return f.name


# ── Parser tests ──────────────────────────────────────────────────────────────

class TestFormsParser:
    def test_parse_simple_form(self):
        path = make_temp_file(SAMPLES["EMPLOYEE_LOOKUP.fmt"])
        artifact = FormsParser().parse(path)
        assert artifact.artifact_type == "FORM"
        assert len(artifact.form_triggers) + sum(len(b.triggers) for b in artifact.blocks) >= 2

    def test_parse_complex_form(self):
        path = make_temp_file(SAMPLES["PAYROLL_PROCESSING.fmt"])
        artifact = FormsParser().parse(path)
        all_triggers = artifact.all_triggers()
        assert any(t.has_cursor for t in all_triggers), "Should detect cursor"
        assert any(t.has_exec_sql for t in all_triggers), "Should detect dynamic SQL"
        assert any(t.has_exception_handling for t in all_triggers), "Should detect exception handling"
        assert any(t.has_dml for t in all_triggers), "Should detect DML"

    def test_parse_xml_report(self):
        path = make_temp_file(SAMPLES["SALES_REPORT.xml"], suffix=".xml")
        artifact = ReportsParser().parse(path)
        assert artifact.artifact_type == "REPORT"
        assert len(artifact.parameters) >= 2
        assert len(artifact.queries) >= 1

    # ── Program unit tests ────────────────────────────────────────────────────

    def test_fmt_detects_program_units(self):
        """BEGIN_OF_OBJECT PROGRAM_UNIT blocks must be parsed correctly."""
        path = make_temp_file(SAMPLES["PAYROLL_PROCESSING_FULL.fmt"])
        artifact = FormsParser().parse(path)
        assert len(artifact.program_units) == 2, (
            f"Expected 2 program units, got {len(artifact.program_units)}: "
            f"{artifact.program_units}"
        )
        names = {pu["name"] for pu in artifact.program_units}
        assert "VALIDATE_SALARY" in names
        assert "CALC_NET_SALARY" in names

    def test_fmt_program_unit_types_detected(self):
        """PROGRAM_UNIT_TYPE meta line sets the type correctly."""
        path = make_temp_file(SAMPLES["PAYROLL_PROCESSING_FULL.fmt"])
        artifact = FormsParser().parse(path)
        by_name = {pu["name"]: pu for pu in artifact.program_units}
        assert by_name["VALIDATE_SALARY"]["type"] == "FUNCTION"
        assert by_name["CALC_NET_SALARY"]["type"] == "PROCEDURE"

    def test_fmt_program_unit_code_captured(self):
        """The body of each program unit must be stored."""
        path = make_temp_file(SAMPLES["PAYROLL_PROCESSING_FULL.fmt"])
        artifact = FormsParser().parse(path)
        by_name = {pu["name"]: pu for pu in artifact.program_units}
        assert "VALIDATE_SALARY" in by_name["VALIDATE_SALARY"]["code"]
        assert "CALC_NET_SALARY" in by_name["CALC_NET_SALARY"]["code"]

    def test_fmt_program_units_dont_bleed_into_triggers(self):
        """Keywords inside trigger bodies must NOT be counted as program units."""
        path = make_temp_file(SAMPLES["PAYROLL_PROCESSING.fmt"])
        artifact = FormsParser().parse(path)
        # PAYROLL_PROCESSING.fmt has no BEGIN_OF_OBJECT PROGRAM_UNIT blocks
        assert artifact.program_units == [], (
            f"Trigger-body keywords leaked into program_units: {artifact.program_units}"
        )

    def test_xml_detects_program_unit_elements(self):
        """<ProgramUnit> XML elements must be parsed."""
        xml_content = """\
<?xml version="1.0"?>
<Module Name="TEST_FORM">
  <ProgramUnit Name="GET_TAX_RATE" ProgramUnitType="FUNCTION">
    <ProgramUnitText>FUNCTION GET_TAX_RATE(p_id NUMBER) RETURN NUMBER IS
BEGIN RETURN 0.2; END;</ProgramUnitText>
  </ProgramUnit>
  <ProgramUnit Name="RESET_FORM" ProgramUnitType="PROCEDURE">
    <ProgramUnitText>PROCEDURE RESET_FORM IS BEGIN NULL; END;</ProgramUnitText>
  </ProgramUnit>
</Module>"""
        path = make_temp_file(xml_content, suffix=".xml")
        artifact = FormsParser().parse(path)
        assert len(artifact.program_units) == 2
        names = {pu["name"] for pu in artifact.program_units}
        assert "GET_TAX_RATE" in names
        assert "RESET_FORM" in names

    def test_program_unit_type_inferred_from_body_when_no_type_line(self):
        """If PROGRAM_UNIT_TYPE line is absent, type is inferred from the code."""
        fmt = """\
BEGIN_OF_OBJECT PROGRAM_UNIT IS_ACTIVE
  PROGRAM_UNIT_TEXT =
    FUNCTION IS_ACTIVE(p_id NUMBER) RETURN BOOLEAN IS
    BEGIN RETURN TRUE; END;
END_OF_OBJECT PROGRAM_UNIT
"""
        path = make_temp_file(fmt)
        artifact = FormsParser().parse(path)
        assert len(artifact.program_units) == 1
        assert artifact.program_units[0]["type"] == "FUNCTION"

    # ── .fmb binary detection tests ───────────────────────────────────────────

    def test_fmb_extension_raises_clear_error(self):
        """A file with .fmb extension must raise FmbBinaryError."""
        # Write enough non-printable bytes to trigger binary detection
        path = make_temp_bytes(b'\x06\x04' + b'\x00\xFF' * 100, suffix=".fmb")
        try:
            FormsParser().parse(path)
            assert False, "Should have raised FmbBinaryError"
        except FmbBinaryError as e:
            msg = str(e)
            assert "compiled" in msg.lower() or "binary" in msg.lower()
            assert "ifcmp60" in msg       # must include the export command
            assert ".fmt" in msg          # must suggest text export
            assert ".xml" in msg          # must suggest XML export

    def test_binary_content_detected_regardless_of_extension(self):
        """Binary .fmb content in a .txt file must still be caught."""
        binary_payload = b'\x06\x08' + bytes(range(256)) * 3
        path = make_temp_bytes(binary_payload, suffix=".txt")
        try:
            FormsParser().parse(path)
            assert False, "Should have raised FmbBinaryError"
        except FmbBinaryError:
            pass  # correct

    def test_valid_fmt_text_not_mistaken_for_binary(self):
        """A normal text .fmt file must NOT trigger the binary check."""
        path = make_temp_file(SAMPLES["EMPLOYEE_LOOKUP.fmt"])
        # Should parse without error
        artifact = FormsParser().parse(path)
        assert artifact.artifact_type == "FORM"

    def test_fmb_error_message_contains_filename(self):
        """The error message should include the filename for easy identification."""
        path = make_temp_bytes(b'\x09\x00' + b'\x00\xFF' * 100, suffix=".fmb")
        try:
            FormsParser().parse(path)
            assert False, "Should have raised FmbBinaryError"
        except FmbBinaryError as e:
            assert Path(path).name in str(e)

    def test_fmb_error_is_caught_gracefully_in_app(self):
        """The Flask app must turn FmbBinaryError into a user-visible error, not a 500."""
        import app as flask_app
        client = flask_app.app.test_client()
        binary_fmb = b'\x06\x04' + b'\x00\xFF' * 200
        data = {"files": (b'\x06\x04' + b'\x00\xFF' * 200, "MY_FORM.fmb", "application/octet-stream")}
        # We send it as a raw bytes upload
        import io
        fmb_io = io.BytesIO(binary_fmb)
        r = client.post(
            "/analyze",
            data={"files": (fmb_io, "MY_FORM.fmb")},
            content_type="multipart/form-data",
        )
        assert r.status_code == 200, f"Got {r.status_code} — should never be 500"
        # The error should appear somewhere in the response
        assert b"binary" in r.data.lower() or b"fmb" in r.data.lower() or b"compiled" in r.data.lower()


# ── Analyzer tests ────────────────────────────────────────────────────────────

class TestComplexityAnalyzer:
    analyzer = ComplexityAnalyzer()

    def _analyze(self, sample_name: str, suffix: str = ".fmt") -> ComplexityReport:
        path = make_temp_file(SAMPLES[sample_name], suffix=suffix)
        if sample_name.endswith(".xml"):
            artifact = ReportsParser().parse(path)
        else:
            artifact = FormsParser().parse(path)
        return self.analyzer.analyze(artifact)

    def test_simple_form_low_score(self):
        # EMPLOYEE_LOOKUP has GO_BLOCK so it correctly scores Moderate
        rep = self._analyze("EMPLOYEE_LOOKUP.fmt")
        assert rep.score in (1, 2), f"Expected Level 1 or 2, got {rep.score} ({rep.reasons})"

    def test_moderate_form_is_level_2(self):
        rep = self._analyze("ORDER_ENTRY.fmt")
        assert rep.score == 2, f"Expected Level 2, got {rep.score} ({rep.reasons})"
        assert rep.label == "Moderate"

    def test_complex_form_is_level_3(self):
        rep = self._analyze("PAYROLL_PROCESSING.fmt")
        assert rep.score == 3, f"Expected Level 3, got {rep.score} ({rep.reasons})"
        assert rep.label == "Complex"

    def test_report_grade_a_b_c_d(self):
        rep = self._analyze("SALES_REPORT.xml", suffix=".xml")
        assert rep.grade in ("A", "B", "C", "D"), f"grade={rep.grade}"
        assert rep.score in (1, 2, 3, 4)

    def test_report_thresholds_correct(self):
        rep = self._analyze("SALES_REPORT.xml", suffix=".xml")
        if rep.raw_points < 25:
            assert rep.grade == "A"
        elif rep.raw_points < 50:
            assert rep.grade == "B"
        elif rep.raw_points < 75:
            assert rep.grade == "C"
        else:
            assert rep.grade == "D"

    def test_program_units_increase_complexity_score(self):
        rep_with = self._analyze("PAYROLL_PROCESSING_FULL.fmt")
        rep_without = self._analyze("EMPLOYEE_LOOKUP.fmt")
        assert rep_with.program_unit_count == 2
        assert rep_with.raw_points > rep_without.raw_points

    def test_program_unit_count_in_breakdown(self):
        rep = self._analyze("PAYROLL_PROCESSING_FULL.fmt")
        assert rep.breakdown.get("program_units", 0) == 2

    def test_effort_estimate_increases_with_complexity(self):
        r1 = self._analyze("EMPLOYEE_LOOKUP.fmt")
        r2 = self._analyze("ORDER_ENTRY.fmt")
        r3 = self._analyze("PAYROLL_PROCESSING.fmt")
        assert r1.estimated_effort_days < r2.estimated_effort_days < r3.estimated_effort_days

    def test_breakdown_dict_populated(self):
        rep = self._analyze("PAYROLL_PROCESSING.fmt")
        assert "trigger_count" in rep.breakdown
        assert "dynamic_sql" in rep.breakdown
        assert rep.breakdown["dynamic_sql"] > 0

    def test_report_analyzed(self):
        rep = self._analyze("SALES_REPORT.xml", suffix=".xml")
        assert rep.artifact.artifact_type == "REPORT"
        assert rep.score in (1, 2, 3, 4)

    def test_to_dict_serializable(self):
        rep = self._analyze("ORDER_ENTRY.fmt")
        d = rep.to_dict()
        json_str = json.dumps(d)
        assert "complexity_level" in json.loads(json_str)


# ── Converter tests ───────────────────────────────────────────────────────────

class TestJavaConverter:
    def test_generates_all_required_files(self):
        path = make_temp_file(SAMPLES["ORDER_ENTRY.fmt"])
        artifact = FormsParser().parse(path)
        with tempfile.TemporaryDirectory() as tmp:
            created = JavaConverter().convert(artifact, tmp)
            names = [Path(f).name for f in created]
            assert "pom.xml" in names
            assert "application.properties" in names
            assert any("Service.java" in n for n in names)
            assert any("Controller.java" in n for n in names)
            assert any("Repository.java" in n for n in names)
            assert any(".html" in n for n in names)
            assert "oracle-forms.css" in names

    def test_service_contains_trigger_comments(self):
        path = make_temp_file(SAMPLES["ORDER_ENTRY.fmt"])
        artifact = FormsParser().parse(path)
        with tempfile.TemporaryDirectory() as tmp:
            created = JavaConverter().convert(artifact, tmp)
            svc = next(f for f in created if "Service.java" in f)
            content = Path(svc).read_text()
            assert "Migrated from" in content or "preInsert" in content

    def test_pom_includes_jakarta_ee(self):
        path = make_temp_file(SAMPLES["EMPLOYEE_LOOKUP.fmt"])
        artifact = FormsParser().parse(path)
        with tempfile.TemporaryDirectory() as tmp:
            created = JavaConverter().convert(artifact, tmp)
            pom = next(f for f in created if f.endswith("pom.xml"))
            content = Path(pom).read_text()
            assert "jakarta" in content.lower() or "wildfly" in content.lower() or "payara" in content.lower()

    def test_html_contains_css_reference(self):
        path = make_temp_file(SAMPLES["EMPLOYEE_LOOKUP.fmt"])
        artifact = FormsParser().parse(path)
        with tempfile.TemporaryDirectory() as tmp:
            created = JavaConverter().convert(artifact, tmp)
            html_files = [f for f in created if f.endswith(".html") or f.endswith(".xhtml")]
            assert html_files
            content = Path(html_files[0]).read_text()
            assert "css" in content.lower()


class TestJasperConverter:
    def test_generates_jrxml(self):
        path = make_temp_file(SAMPLES["SALES_REPORT.xml"], suffix=".xml")
        artifact = ReportsParser().parse(path)
        with tempfile.TemporaryDirectory() as tmp:
            created = JasperConverter().convert(artifact, tmp)
            names = [Path(f).name for f in created]
            assert any(".jrxml" in n for n in names)
            assert any("Runner.java" in n for n in names)
            assert "pom.xml" in names

    def test_jrxml_is_valid_xml(self):
        import xml.etree.ElementTree as ET
        path = make_temp_file(SAMPLES["SALES_REPORT.xml"], suffix=".xml")
        artifact = ReportsParser().parse(path)
        with tempfile.TemporaryDirectory() as tmp:
            created = JasperConverter().convert(artifact, tmp)
            jrxml = next(f for f in created if f.endswith(".jrxml"))
            content = Path(jrxml).read_text()
            assert "<jasperReport" in content
            assert "</jasperReport>" in content

    def test_parameters_migrated(self):
        path = make_temp_file(SAMPLES["SALES_REPORT.xml"], suffix=".xml")
        artifact = ReportsParser().parse(path)
        with tempfile.TemporaryDirectory() as tmp:
            created = JasperConverter().convert(artifact, tmp)
            jrxml = next(f for f in created if f.endswith(".jrxml"))
            content = Path(jrxml).read_text()
            assert "P_START_DATE" in content or "parameter" in content.lower()


# ── Integration test ──────────────────────────────────────────────────────────

class TestIntegration:
    def test_full_pipeline_all_samples(self):
        with tempfile.TemporaryDirectory() as tmp:
            sample_dir = Path(tmp) / "samples"
            out_dir = Path(tmp) / "output"
            created_samples = create_samples(str(sample_dir))
            assert len(created_samples) > 0

            java_conv = JavaConverter()
            jasper_conv = JasperConverter()
            analyzer = ComplexityAnalyzer()

            for sfile in created_samples:
                if Path(sfile).suffix.lower() in (".xml",):
                    artifact = ReportsParser().parse(sfile)
                else:
                    artifact = FormsParser().parse(sfile)

                report = analyzer.analyze(artifact)
                assert report.score in (1, 2, 3)

                art_out = out_dir / artifact.name
                if artifact.artifact_type == "REPORT":
                    jasper_conv.convert(artifact, str(art_out / "jasper"))
                else:
                    java_conv.convert(artifact, str(art_out / "java"))

            assert out_dir.exists()
            all_outputs = list(out_dir.rglob("*"))
            assert len(all_outputs) > 10, "Should generate many output files"


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v", "--tb=short"])



# ── Fixtures (inline) ─────────────────────────────────────────────────────────

def make_temp_file(content: str, suffix: str = ".fmt") -> str:
    f = tempfile.NamedTemporaryFile(mode="w", suffix=suffix, delete=False)
    f.write(content)
    f.close()
    return f.name



# ── Converter tests ───────────────────────────────────────────────────────────

class TestJavaConverter:
    def test_generates_all_required_files(self):
        path = make_temp_file(SAMPLES["ORDER_ENTRY.fmt"])
        artifact = FormsParser().parse(path)
        with tempfile.TemporaryDirectory() as tmp:
            created = JavaConverter().convert(artifact, tmp)
            names = [Path(f).name for f in created]
            assert "pom.xml" in names
            assert "application.properties" in names
            assert any("Service.java" in n for n in names)
            assert any("Controller.java" in n for n in names)
            assert any("Repository.java" in n for n in names)
            assert any(".html" in n for n in names)
            assert "oracle-forms.css" in names

    def test_service_contains_trigger_comments(self):
        path = make_temp_file(SAMPLES["ORDER_ENTRY.fmt"])
        artifact = FormsParser().parse(path)
        with tempfile.TemporaryDirectory() as tmp:
            created = JavaConverter().convert(artifact, tmp)
            svc = next(f for f in created if "Service.java" in f)
            content = Path(svc).read_text()
            assert "Migrated from" in content or "preInsert" in content

    def test_pom_includes_spring_boot(self):
        path = make_temp_file(SAMPLES["EMPLOYEE_LOOKUP.fmt"])
        artifact = FormsParser().parse(path)
        with tempfile.TemporaryDirectory() as tmp:
            created = JavaConverter().convert(artifact, tmp)
            pom = next(f for f in created if f.endswith("pom.xml"))
            content = Path(pom).read_text()
            assert "spring-boot" in content
            assert "thymeleaf" in content
            assert "h2database" in content

    def test_html_contains_oracle_forms_css(self):
        path = make_temp_file(SAMPLES["EMPLOYEE_LOOKUP.fmt"])
        artifact = FormsParser().parse(path)
        with tempfile.TemporaryDirectory() as tmp:
            created = JavaConverter().convert(artifact, tmp)
            html_files = [f for f in created if f.endswith(".html")]
            assert html_files
            content = Path(html_files[0]).read_text()
            assert "oracle-forms.css" in content


class TestJasperConverter:
    def test_generates_jrxml(self):
        path = make_temp_file(SAMPLES["SALES_REPORT.xml"], suffix=".xml")
        artifact = ReportsParser().parse(path)
        with tempfile.TemporaryDirectory() as tmp:
            created = JasperConverter().convert(artifact, tmp)
            names = [Path(f).name for f in created]
            assert any(".jrxml" in n for n in names)
            assert any("Runner.java" in n for n in names)
            assert "pom.xml" in names

    def test_jrxml_is_valid_xml(self):
        import xml.etree.ElementTree as ET
        path = make_temp_file(SAMPLES["SALES_REPORT.xml"], suffix=".xml")
        artifact = ReportsParser().parse(path)
        with tempfile.TemporaryDirectory() as tmp:
            created = JasperConverter().convert(artifact, tmp)
            jrxml = next(f for f in created if f.endswith(".jrxml"))
            content = Path(jrxml).read_text()
            # Should at least be parseable after stripping comments
            assert "<jasperReport" in content
            assert "</jasperReport>" in content

    def test_parameters_migrated(self):
        path = make_temp_file(SAMPLES["SALES_REPORT.xml"], suffix=".xml")
        artifact = ReportsParser().parse(path)
        with tempfile.TemporaryDirectory() as tmp:
            created = JasperConverter().convert(artifact, tmp)
            jrxml = next(f for f in created if f.endswith(".jrxml"))
            content = Path(jrxml).read_text()
            assert "P_START_DATE" in content or "parameter" in content.lower()


# ── Integration test ──────────────────────────────────────────────────────────

class TestIntegration:
    def test_full_pipeline_all_samples(self):
        """Full pipeline: create samples → parse → analyze → convert."""
        with tempfile.TemporaryDirectory() as tmp:
            sample_dir = Path(tmp) / "samples"
            out_dir = Path(tmp) / "output"
            created_samples = create_samples(str(sample_dir))
            assert len(created_samples) > 0

            java_conv = JavaConverter()
            jasper_conv = JasperConverter()
            analyzer = ComplexityAnalyzer()

            for sfile in created_samples:
                if Path(sfile).suffix.lower() in (".xml",):
                    artifact = ReportsParser().parse(sfile)
                else:
                    artifact = FormsParser().parse(sfile)

                report = analyzer.analyze(artifact)
                assert report.score in (1, 2, 3)

                art_out = out_dir / artifact.name
                if artifact.artifact_type == "REPORT":
                    jasper_conv.convert(artifact, str(art_out / "jasper"))
                else:
                    java_conv.convert(artifact, str(art_out / "java"))

            # Check outputs exist
            assert out_dir.exists()
            all_outputs = list(out_dir.rglob("*"))
            assert len(all_outputs) > 10, "Should generate many output files"


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v", "--tb=short"])
