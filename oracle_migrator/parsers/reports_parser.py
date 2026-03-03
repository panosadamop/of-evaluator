"""
Parser for Oracle Reports files (.rdf XML, .xml)
"""
import re
import xml.etree.ElementTree as ET
from pathlib import Path
from ..core.models import OracleArtifact, TriggerInfo
from .forms_parser import FormsParser


class ReportsParser:
    """Parses Oracle Reports .rdf / .xml files."""

    def parse(self, file_path: str) -> OracleArtifact:
        path = Path(file_path)
        content = self._read(path)
        artifact = OracleArtifact(
            name=path.stem,
            artifact_type="REPORT",
            file_path=str(path),
            file_size=path.stat().st_size if path.exists() else 0,
            raw_content=content,
        )

        if content.strip().startswith("<"):
            self._parse_xml(artifact, content)
        else:
            self._parse_text(artifact, content)

        return artifact

    def _read(self, path: Path) -> str:
        try:
            return path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            return ""

    def _parse_xml(self, artifact: OracleArtifact, content: str):
        try:
            root = ET.fromstring(content)
        except ET.ParseError:
            self._parse_text(artifact, content)
            return

        for el in root.iter():
            tag = el.tag.split("}")[-1].lower()

            # Queries / data model
            if "query" in tag or "datamodel" in tag:
                sql = el.get("SelectStatement", el.get("selectStatement", el.text or ""))
                if sql and len(sql.strip()) > 5:
                    artifact.queries.append({
                        "name": el.get("Name", el.get("name", "Q")),
                        "sql": sql.strip(),
                    })

            # Triggers / formulas / before-after hooks
            elif any(x in tag for x in ("trigger", "formula", "beforereport", "afterreport", "betweenpages")):
                code = el.get("FunctionBody", el.get("functionBody", el.text or ""))
                name = el.get("Name", el.get("name", el.tag.split("}")[-1]))
                if code and code.strip():
                    trig = FormsParser._analyze_trigger(name, "REPORT", code)
                    artifact.form_triggers.append(trig)

            # Parameters
            elif "parameter" in tag:
                artifact.parameters.append({
                    "name": el.get("Name", el.get("name", "PARAM")),
                    "type": el.get("DataType", el.get("dataType", "VARCHAR2")),
                    "default": el.get("InitialValue", el.get("initialValue", "")),
                })

    def _parse_text(self, artifact: OracleArtifact, content: str):
        query_re = re.compile(r"SELECT\s+.+?\s+FROM\s+\w+", re.I | re.DOTALL)
        trigger_re = re.compile(
            r"(BEFORE-REPORT|AFTER-REPORT|BETWEEN-PAGES|FORMAT-TRIGGER|VALIDATION-TRIGGER|BEFORE-FORM|AFTER-FORM)",
            re.I,
        )
        func_re = re.compile(r"(FUNCTION|PROCEDURE)\s+(\w+)", re.I)
        param_re = re.compile(r"PARAMETER\s+(\w+)\s+(VARCHAR2|NUMBER|DATE)", re.I)

        for m in query_re.finditer(content):
            artifact.queries.append({"sql": m.group(0)[:400]})

        for m in trigger_re.finditer(content):
            tname = m.group(1).upper()
            start = max(0, m.start() - 30)
            end = min(len(content), m.end() + 600)
            code = content[start:end]
            trig = FormsParser._analyze_trigger(tname, "REPORT", code)
            artifact.form_triggers.append(trig)

        for m in func_re.finditer(content):
            artifact.program_units.append({
                "name": m.group(2),
                "type": m.group(1).upper(),
            })

        for m in param_re.finditer(content):
            artifact.parameters.append({
                "name": m.group(1),
                "type": m.group(2).upper(),
                "default": "",
            })
