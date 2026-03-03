"""
Parser for Oracle Forms files (.fmt text export, .xml export, .fmb binary)
"""
import re
import xml.etree.ElementTree as ET
from pathlib import Path
from ..core.models import OracleArtifact, DataBlock, FormItem, TriggerInfo


TRIGGER_WEIGHTS = {
    'ON-INSERT': 3, 'ON-UPDATE': 3, 'ON-DELETE': 3, 'ON-LOCK': 3,
    'ON-FETCH': 3, 'ON-COUNT': 3, 'ON-POPULATE-DETAILS': 3, 'ON-COMMIT': 3,
    'PRE-COMMIT': 2, 'POST-COMMIT': 2,
    'WHEN-VALIDATE-ITEM': 2, 'WHEN-VALIDATE-RECORD': 2,
    'KEY-COMMIT': 2, 'KEY-EXEQRY': 2, 'KEY-DUPREC': 2,
    'PRE-INSERT': 2, 'PRE-UPDATE': 2, 'PRE-DELETE': 2,
    'POST-INSERT': 2, 'POST-UPDATE': 2, 'POST-DELETE': 2,
    'WHEN-NEW-FORM-INSTANCE': 2, 'WHEN-NEW-BLOCK-INSTANCE': 2,
    'WHEN-NEW-RECORD-INSTANCE': 2, 'WHEN-NEW-ITEM-INSTANCE': 1,
    'WHEN-BUTTON-PRESSED': 1, 'KEY-NXTBLK': 1, 'KEY-PRVBLK': 1,
    'KEY-NXTREC': 1, 'KEY-PRVREC': 1, 'WHEN-CHECKBOX-CHANGED': 1,
    'WHEN-LIST-CHANGED': 1, 'WHEN-RADIO-CHANGED': 1, 'WHEN-TIMER-EXPIRED': 1,
}

# Magic bytes that identify a binary .fmb file
_FMB_MAGIC = [
    b'\x06\x04',          # Oracle Forms 6i
    b'\x06\x08',          # Oracle Forms 6i variant
    b'\x09\x00',          # Oracle Forms 9i/10g
    b'JDAPI',             # Oracle JDAPI compiled form
    b'\x89FMB',           # common FMB header marker
]


def _is_binary_fmb(raw_bytes: bytes) -> bool:
    """Return True if the file looks like a compiled Oracle Forms binary (.fmb)."""
    if not raw_bytes:
        return False
    # Check known magic byte sequences
    for magic in _FMB_MAGIC:
        if raw_bytes[:len(magic)] == magic:
            return True
    # Heuristic: if > 30% of the first 512 bytes are non-printable → binary
    sample = raw_bytes[:512]
    non_printable = sum(1 for b in sample if b < 9 or (13 < b < 32) or b > 126)
    return non_printable / len(sample) > 0.30


class FmbBinaryError(ValueError):
    """Raised when a .fmb binary file is uploaded without prior text export."""
    pass


class FormsParser:
    """Parses Oracle Forms .fmt (text) and .xml files.

    .fmb files are compiled binaries — they cannot be parsed directly.
    Export them first with Oracle Forms Developer:
        ifcmp60 module=form.fmb logon=user/pass@db output_file=form.fmt
    or for XML:
        ifcmp60 module=form.fmb module_type=form output_type=xml output_file=form.xml
    """

    def parse(self, file_path: str) -> OracleArtifact:
        path = Path(file_path)

        # Read raw bytes first so we can detect binary .fmb
        try:
            raw_bytes = path.read_bytes()
        except Exception:
            raw_bytes = b""

        # Reject binary .fmb with a clear, actionable message
        if path.suffix.lower() == ".fmb" or _is_binary_fmb(raw_bytes):
            raise FmbBinaryError(
                f"'{path.name}' is a compiled Oracle Forms binary (.fmb) and cannot be "
                f"parsed directly. Export it to text or XML first:\n"
                f"  Text:  ifcmp60 module={path.name} logon=user/pass@db "
                f"output_file={path.stem}.fmt\n"
                f"  XML:   ifcmp60 module={path.name} module_type=form "
                f"output_type=xml output_file={path.stem}.xml"
            )

        content = raw_bytes.decode("utf-8", errors="replace")

        artifact = OracleArtifact(
            name=path.stem,
            artifact_type="FORM",
            file_path=str(path),
            file_size=len(raw_bytes),
            raw_content=content,
        )

        if content.strip().startswith("<"):
            self._parse_xml(artifact, content)
        else:
            self._parse_text(artifact, content)

        return artifact

    # ── XML Parser ───────────────────────────────────────────────────────────

    def _parse_xml(self, artifact: OracleArtifact, content: str):
        try:
            root = ET.fromstring(content)
        except ET.ParseError:
            self._parse_text(artifact, content)
            return

        for el in root.iter():
            tag = el.tag.split("}")[-1]

            if tag in ("Block", "BLOCK"):
                block = DataBlock(
                    name=el.get("Name", el.get("name", "BLOCK")),
                    data_source=el.get("DatabaseDataBlock"),
                    query=el.get("QueryDataSourceName"),
                )
                for child in el:
                    ctag = child.tag.split("}")[-1]
                    if ctag in ("Item", "ITEM"):
                        item = FormItem(
                            name=child.get("Name", "ITEM"),
                            item_type=child.get("ItemType", "TEXT_ITEM"),
                            data_type=child.get("DataType", "VARCHAR2"),
                            required=child.get("Required", "false").lower() == "true",
                            list_of_values=child.get("Lov"),
                        )
                        block.items.append(item)
                artifact.blocks.append(block)

            elif tag in ("Trigger", "TRIGGER"):
                code = el.get("TriggerCode", el.text or "")
                trig = self._analyze_trigger(
                    el.get("Name", "TRIGGER"), "FORM", code
                )
                artifact.form_triggers.append(trig)

            elif tag in ("ProgramUnit", "PROGRAM_UNIT"):
                # XML export: <ProgramUnit Name="VALIDATE_SALARY" ProgramUnitType="FUNCTION">
                #   <ProgramUnitText>FUNCTION VALIDATE_SALARY ...
                code = el.get("ProgramUnitText", el.text or "")
                artifact.program_units.append({
                    "name": el.get("Name", el.get("name", "PU")),
                    "type": el.get("ProgramUnitType",
                                   el.get("programUnitType", "PROCEDURE")),
                    "code": code.strip(),
                })

    # ── Text (.fmt) Parser ───────────────────────────────────────────────────

    def _parse_text(self, artifact: OracleArtifact, content: str):
        lines = content.split("\n")

        # Compiled regexes for the structural markers in .fmt files
        block_start_re  = re.compile(r"^\s*BEGIN_OF_OBJECT\s+BLOCK\s+(\w+)", re.I)
        block_end_re    = re.compile(r"^\s*END_OF_OBJECT\s+BLOCK", re.I)
        pu_start_re     = re.compile(r"^\s*BEGIN_OF_OBJECT\s+PROGRAM_UNIT\s+(\w+)", re.I)
        pu_end_re       = re.compile(r"^\s*END_OF_OBJECT\s+PROGRAM_UNIT", re.I)
        pu_type_re      = re.compile(r"^\s*PROGRAM_UNIT_TYPE\s*=\s*(\w+)", re.I)
        trig_start_re   = re.compile(r"^\s*BEGIN_OF_TRIGGER\s+([\w\-]+)", re.I)
        trig_end_re     = re.compile(r"^\s*END_OF_TRIGGER", re.I)
        query_re        = re.compile(r"SELECT\s+.+?\s+FROM\s+\w+", re.I | re.DOTALL)

        current_block       = None
        current_trig_name   = None
        current_trig_lines  = []
        current_pu_name     = None
        current_pu_type     = "PROCEDURE"
        current_pu_lines    = []

        in_trigger      = False
        in_program_unit = False

        for line in lines:

            # ── Program unit start ────────────────────────────────────────
            pu_m = pu_start_re.match(line)
            if pu_m and not in_trigger:
                current_pu_name  = pu_m.group(1)
                current_pu_type  = "PROCEDURE"   # default; overridden below
                current_pu_lines = []
                in_program_unit  = True
                continue

            # ── Program unit type meta line ───────────────────────────────
            if in_program_unit and not in_trigger:
                pt_m = pu_type_re.match(line)
                if pt_m:
                    current_pu_type = pt_m.group(1).upper()
                    continue

            # ── Program unit end ──────────────────────────────────────────
            if in_program_unit and pu_end_re.match(line):
                code = "\n".join(current_pu_lines).strip()
                # Infer type from body if PROGRAM_UNIT_TYPE line was absent
                if re.search(r"^\s*FUNCTION\b", code, re.I | re.MULTILINE):
                    current_pu_type = "FUNCTION"
                artifact.program_units.append({
                    "name": current_pu_name,
                    "type": current_pu_type,
                    "code": code,
                })
                in_program_unit = False
                current_pu_name = None
                current_pu_lines = []
                continue

            # ── Collect program unit body lines ───────────────────────────
            if in_program_unit and not in_trigger:
                # Skip the PROGRAM_UNIT_TEXT = line itself; collect everything after
                if re.match(r"^\s*PROGRAM_UNIT_TEXT\s*=", line, re.I):
                    continue
                current_pu_lines.append(line)
                continue

            # ── Block start ───────────────────────────────────────────────
            bm = block_start_re.match(line)
            if bm and not in_trigger:
                current_block = DataBlock(
                    name=bm.group(1), data_source=None, query=None
                )
                artifact.blocks.append(current_block)
                continue

            # ── Block end ─────────────────────────────────────────────────
            if block_end_re.match(line) and not in_trigger:
                current_block = None
                continue

            # ── Trigger start ─────────────────────────────────────────────
            tm = trig_start_re.match(line)
            if tm:
                current_trig_name  = tm.group(1).upper()
                current_trig_lines = []
                in_trigger = True
                continue

            # ── Trigger end ───────────────────────────────────────────────
            if trig_end_re.match(line) and in_trigger:
                code = "\n".join(current_trig_lines)
                trig = self._analyze_trigger(current_trig_name, "FORM", code)
                if current_block is not None:
                    current_block.triggers.append(trig)
                else:
                    artifact.form_triggers.append(trig)
                in_trigger = False
                continue

            # ── Collect trigger body lines ────────────────────────────────
            if in_trigger:
                current_trig_lines.append(line)

        # ── SQL queries (full-file scan, outside structural parsing) ──────
        for match in query_re.finditer(content):
            artifact.queries.append({"sql": match.group(0)[:300]})

        # ── Heuristic trigger scan if no structured triggers found ────────
        if not artifact.all_triggers():
            self._heuristic_trigger_scan(artifact, content)

    def _heuristic_trigger_scan(self, artifact: OracleArtifact, content: str):
        """Last-resort: look for known trigger names embedded in raw text."""
        for tname in TRIGGER_WEIGHTS:
            if re.search(r"\b" + re.escape(tname) + r"\b", content, re.I):
                match = re.search(
                    r"\b" + re.escape(tname) + r"\b(.{0,600})",
                    content, re.I | re.DOTALL,
                )
                code = match.group(1) if match else ""
                trig = self._analyze_trigger(tname, "FORM", code)
                artifact.form_triggers.append(trig)

    # ── Trigger analyzer ─────────────────────────────────────────────────────

    @staticmethod
    def _analyze_trigger(name: str, trigger_type: str, code: str) -> TriggerInfo:
        up = code.upper()
        lines = [l for l in code.split("\n") if l.strip()]
        return TriggerInfo(
            name=name,
            trigger_type=trigger_type,
            code=code,
            line_count=len(lines),
            has_dml=bool(re.search(r"\b(INSERT|UPDATE|DELETE|MERGE)\b", up)),
            has_exec_sql=bool(re.search(r"\bEXEC_SQL\b|EXECUTE\s+IMMEDIATE", up)),
            has_go_block=bool(re.search(r"\bGO_BLOCK\b", up)),
            has_go_item=bool(re.search(r"\bGO_ITEM\b", up)),
            has_complex_logic=bool(re.search(
                r"\b(CASE\s+WHEN|IF.+ELSIF|CURSOR\s+\w+|FOR\s+\w+\s+IN|WHILE\s+\w+)", up
            )),
            has_exception_handling=bool(re.search(r"\bEXCEPTION\b", up)),
            has_cursor=bool(re.search(r"\bCURSOR\s+\w+", up)),
            has_loops=bool(re.search(r"\b(FOR\s+\w+\s+IN|WHILE\s+\w+|LOOP\b)", up)),
        )
