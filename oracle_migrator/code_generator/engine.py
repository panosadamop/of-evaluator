#!/usr/bin/env python3
"""
Oracle Forms Code Generator - CSV Binding v1

Improvements over v1:
- Better report parsing for block sections:
  * block-fields
  * block-options
  * block-lov
  * block-validations
  * block-buttons
- Field-specific snippet extraction from section text
- Push Button detection from fields table
- Better validation detection from Links to code
- Better LOV detection with exclusions
- Better properties generation from exact Title/Label
- Better xhtml generation with descr/name pairing and duplicate avoidance
- Better WRP SQL named-arg generation and IN/INOUT record mapping
- Better ProceduresEnum param generation
- Better service/controller method generation structure

Still not the final "100% expert parity" generator:
- PL/SQL bodies are still embedded/commented, not fully converted to Java logic
- Table<->package collection mapping in WRP remains TODO
- Screen-specific business semantics still need dedicated converters
"""

from __future__ import annotations

import re
import zipfile
import csv
import threading
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Iterable, Any

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

try:
    from bs4 import BeautifulSoup
except Exception:
    BeautifulSoup = None


APPLICATION_SEARCH_FIELDS = {
    "PAI_PDTA_APPLICATION_UNIT_CODE",
    "PAI_APPLICATION_INCR_SEQ_NO",
    "PAI_PDTA_APPLICATION_YEAR",
}

EXCLUDED_AUTO_LOV_PREFIXES = (
    "PAI_",
    "APPL_",
    "DEC_",
    "DT_DEC_",
    "DOC_TYPE_DEC_",
    "DT_DOCUMENT_TYPE_DEC_",
)

KNOWN_FIELD_LABEL_FALLBACKS = {}

KNOWN_OPTION_ENUMS = {}


NON_VALIDATION_LINK_KEYWORDS = {
    "options",
    "lov",
    "trigger",
}

JAVA_RESERVED = {
    "class", "package", "public", "private", "void", "default", "switch",
}


def strip_plsql_comments(text: str) -> str:
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    text = re.sub(r"--.*", "", text)
    return text


def split_top_level(text: str, delimiter: str = ",") -> List[str]:
    out: List[str] = []
    buf: List[str] = []
    depth = 0
    in_str = False
    prev = ""
    for ch in text:
        if ch == "'" and prev != "\\":
            in_str = not in_str
        if not in_str:
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
        if ch == delimiter and depth == 0 and not in_str:
            out.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
        prev = ch
    if buf:
        out.append("".join(buf))
    return out


def normalize_spaces(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip()

def normalize_block_id(raw_block_id: str) -> str:
    raw = (raw_block_id or "").strip()
    if raw.lower().startswith("block-"):
        raw = raw[6:]
    return raw.replace("-", "_").upper()


def normalize_item_full_name(item_full: str) -> str:
    return (item_full or "").strip().replace("-", "_").upper()


def split_package_type_name(plsql_type: str) -> Tuple[Optional[str], str]:
    t = (plsql_type or "").strip().upper()
    if "." in t and "%TYPE" not in t:
        pkg, name = t.split(".", 1)
        return pkg, name
    return None, t


def resolve_record(records: Dict[str, "SqlRecord"], package_name: str, plsql_type: str) -> Optional["SqlRecord"]:
    pkg, name = split_package_type_name(plsql_type)
    rec = records.get(name)
    if rec is None:
        return None
    if pkg and rec.package_name != pkg:
        return None
    if not pkg and rec.package_name != package_name and name.endswith("REC_HEADER"):
        return None
    return rec


def resolve_table(tables: Dict[str, "SqlTable"], package_name: str, plsql_type: str) -> Optional["SqlTable"]:
    pkg, name = split_package_type_name(plsql_type)
    tab = tables.get(name)
    if tab is None:
        return None
    if pkg and tab.package_name != pkg:
        return None
    return tab


def to_pascal(s: str) -> str:
    return "".join(p.title() for p in re.split(r"[_\s]+", s.lower()) if p)


def to_camel(s: str) -> str:
    parts = [p for p in re.split(r"[_\s]+", s.lower()) if p]
    if not parts:
        return ""
    out = parts[0] + "".join(p.title() for p in parts[1:])
    if out in JAVA_RESERVED:
        return out + "Value"
    return out


def package_prefix(package_name: str) -> str:
    return "".join(p[0] for p in package_name.lower().split("_") if p).upper()


def package_prefix_class(package_name: str) -> str:
    p = package_prefix(package_name).lower()
    return p[:1].upper() + p[1:]


def package_to_lower_camel(package_name: str) -> str:
    return to_camel(package_name)


def escape_properties_value(value: str) -> str:
    value = (value or "").replace("&#10;", " ").strip().rstrip(":")
    out: List[str] = []
    for ch in value:
        code = ord(ch)
        if ch in ("\\", ":", "=", "#", "!"):
            out.append("\\" + ch)
        elif code > 127:
            out.append(f"\\u{code:04x}")
        else:
            out.append(ch)
    return "".join(out)


def detect_schema_and_package(sql_text: str, fallback_filename: str) -> Tuple[Optional[str], str]:
    clean = strip_plsql_comments(sql_text)
    m = re.search(r"\bpackage\s+(?:body\s+)?(?:(\w+)\.)?(\w+)\b", clean, flags=re.I)
    if m:
        return (m.group(1).upper() if m.group(1) else None, m.group(2).upper())
    return None, Path(fallback_filename).stem.upper()


def pretty_label_from_item(item_name: str) -> str:
    return " ".join(p.title() for p in item_name.lower().split("_"))


ORACLE_PRIMITIVE_TYPES = {
    "VARCHAR2", "VARCHAR", "CHAR", "NCHAR", "NVARCHAR2", "NUMBER", "INTEGER", "BINARY_INTEGER",
    "PLS_INTEGER", "DECIMAL", "NUMERIC", "FLOAT", "REAL", "DOUBLE", "DATE", "TIMESTAMP",
    "TIMESTAMP WITH TIME ZONE", "TIMESTAMP WITH LOCAL TIME ZONE", "CLOB", "NCLOB", "BLOB", "BOOLEAN",
    "RAW", "LONG", "LONG RAW", "ROWID", "UROWID", "XMLTYPE"
}


def looks_like_lov_by_name(item_name: str) -> bool:
    return False


def looks_like_insured_search_item(item_name: str) -> bool:
    u = item_name.upper()
    return ("INSURED" in u and u.endswith("ID")) or u in {"INS_INSURED_ID", "INSURED_ID_IND"}


def is_checkbox_field(item_name: str) -> bool:
    return item_name.upper().endswith("_FLG")


def has_varchar2_single_char_boolean_accessors(plsql_type: str, type_catalog: Optional[PackageTypeCatalog] = None, schema_name: Optional[str] = None) -> bool:
    resolved = resolve_actual_plsql_type(plsql_type, type_catalog, schema_name=schema_name)
    normalized = re.sub(r"\s+", " ", resolved.upper()).strip()
    return normalized in {"VARCHAR2(1 CHAR)", "VARCHAR2(1)"}


def is_varchar2_two_char_flag_field(field_name: str, plsql_type: str, type_catalog: Optional[PackageTypeCatalog] = None, schema_name: Optional[str] = None) -> bool:
    resolved = resolve_actual_plsql_type(plsql_type, type_catalog, schema_name=schema_name)
    normalized = re.sub(r"\s+", " ", resolved.upper()).strip()
    return field_name.upper().endswith("_FLG") and normalized in {"VARCHAR2(2 CHAR)", "VARCHAR2(2)"}


def should_generate_boolean_accessors(field_name: str, plsql_type: str, field_def: Optional["FieldDefinition"] = None, type_catalog: Optional[PackageTypeCatalog] = None, schema_name: Optional[str] = None) -> bool:
    name_upper = field_name.upper().strip()
    if name_upper == "STATUS":
        return False
    if field_def is not None and field_def.checkbox_options is not None:
        return True
    return has_varchar2_single_char_boolean_accessors(plsql_type, type_catalog, schema_name=schema_name)


def checkbox_config_for_field(field_name: str, field_def: Optional["FieldDefinition"]) -> CheckboxOptions:
    if field_def is not None and field_def.checkbox_options is not None:
        return field_def.checkbox_options
    return CheckboxOptions()


def java_type_from_plsql(plsql_type: str, field_name: str, type_catalog: Optional[PackageTypeCatalog] = None, schema_name: Optional[str] = None) -> str:
    p = resolve_actual_plsql_type(plsql_type, type_catalog, schema_name=schema_name).upper().strip()
    f = field_name.upper().strip()
    f_norm = re.sub(r"_SR$", "", f)

    if "BOOLEAN" in p:
        return "Boolean"
    if "DATE" in p or f_norm.endswith("_DATE") or f_norm.endswith("DATE"):
        return "Date"
    if any(x in p for x in ("VARCHAR2", "CHAR", "CLOB")):
        return "String"
    if any(x in p for x in ("NUMBER", "INTEGER", "BINARY_INTEGER", "DECIMAL", "NUMERIC")):
        if f_norm.endswith(("_ID", "_NO", "_SEQ", "_TIME", "_SID")):
            return "Long"
        if f_norm.endswith(("_FLG", "_YEAR", "_MONTH", "_CLASS", "_DAYS")):
            return "Integer"
        if any(token in f_norm for token in ("AMOUNT", "SALARY", "WAGE", "DEDUCTION", "TOTAL", "PAID")):
            return "BigDecimal"
        return "BigDecimal"
    return "String"


def sql_type_with_char(plsql_type: str) -> str:
    p = plsql_type.upper().strip()
    if "VARCHAR2(" in p and "CHAR" not in p:
        return re.sub(r"VARCHAR2\((\d+)\)", r"VARCHAR2(\1 CHAR)", p)
    return p


def scalar_sql_type_from_percent_type(percent_type: str) -> str:
    u = percent_type.upper()
    if "DATE" in u:
        return "Types.DATE"
    if any(x in u for x in ("NO", "NUM", "PRCNT", "AMNT", "AMOUNT", "NUMBER")):
        return "Types.NUMERIC"
    return "Types.VARCHAR"




def java_literal_for_value(java_type: str, raw_value: str) -> str:
    value = (raw_value or "").strip()
    if java_type == "String":
        return f'"{clean_java_string_literal(value)}"'
    if java_type == "Long":
        return f"{int(value)}L" if re.fullmatch(r"[-+]?\d+", value) else f'Long.valueOf("{clean_java_string_literal(value)}")'
    if java_type == "Integer":
        return value if re.fullmatch(r"[-+]?\d+", value) else f'Integer.valueOf("{clean_java_string_literal(value)}")'
    if java_type == "BigDecimal":
        return f'new BigDecimal("{clean_java_string_literal(value)}")'
    if java_type == "Boolean":
        return "true" if value.lower() in {"1", "y", "true"} else "false"
    return f'"{clean_java_string_literal(value)}"'


def java_compare_expression(java_type: str, left_expr: str, raw_value: str) -> str:
    literal = java_literal_for_value(java_type, raw_value)
    if java_type in {"Long", "Integer", "BigDecimal", "String", "Boolean"}:
        return f"Objects.equals({left_expr}, {literal})"
    return f"Objects.equals({left_expr}, {literal})"


def enum_class_name(model: "ScreenModel", field_name: str) -> str:
    return model.prefix_class + to_pascal(field_name) + "Option"


def enum_controller_method_name(field_name: str) -> str:
    return f"get{to_pascal(field_name)}Options"

def dedupe_preserve_order(items: Iterable[str]) -> List[str]:
    seen = set()
    out = []
    for x in items:
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out


def clean_java_string_literal(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def java_block_comment(text_value: str, indent: str = "        ") -> str:
    if not text_value:
        return ""
    lines = text_value.splitlines()
    out = [indent + "/*"]
    out.extend(indent + " * " + line for line in lines)
    out.append(indent + " */")
    return "\n".join(out)



@dataclass
class InputBundle:
    main_sql_path: Path
    main_sql_text: str
    additional_sql_paths: List[Path]
    additional_sql_texts: List[str]
    report_path: Path
    report_html: str
    output_zip_path: Path
    schema_name: Optional[str]
    package_name: str
    types_csv_path: Optional[Path] = None
    package_type_catalog: Optional["PackageTypeCatalog"] = None


class PackageTypeCatalog:
    HEADER_ALIASES = {
        "OWNER": "OWNER",
        "SCHEMA_NAME": "OWNER",
        "TABLE_NAME": "TABLE_NAME",
        "REF_TABLE": "TABLE_NAME",
        "COLUMN_NAME": "COLUMN_NAME",
        "REF_COLUMN": "COLUMN_NAME",
        "DATA_TYPE": "DATA_TYPE",
    }

    def __init__(self, rows: Optional[List[Dict[str, str]]] = None):
        self.rows = rows or []

    @classmethod
    def _detect_dialect(cls, sample: str) -> csv.Dialect:
        try:
            return csv.Sniffer().sniff(sample, delimiters=",;\t|")
        except Exception:
            class _Fallback(csv.excel):
                delimiter = ","
            return _Fallback

    @classmethod
    def _normalize_row(cls, row: Dict[str, str]) -> Dict[str, str]:
        normalized: Dict[str, str] = {}
        for k, v in row.items():
            header = cls.HEADER_ALIASES.get((k or "").strip().upper(), (k or "").strip().upper())
            normalized[header] = (v or "").strip()
        return normalized

    @classmethod
    def from_csv(cls, csv_path: Optional[Path]) -> "PackageTypeCatalog":
        if not csv_path:
            return cls([])
        rows: List[Dict[str, str]] = []
        with csv_path.open("r", encoding="utf-8-sig", errors="ignore", newline="") as f:
            sample = f.read(4096)
            f.seek(0)
            dialect = cls._detect_dialect(sample)
            reader = csv.DictReader(f, dialect=dialect)
            for row in reader:
                normalized = cls._normalize_row(row)
                if any((value or "").strip() for value in normalized.values()):
                    rows.append(normalized)
        return cls(rows)

    def resolve_percent_type(self, plsql_type: str, schema_name: Optional[str] = None) -> Optional[str]:
        raw = (plsql_type or "").strip()
        m = re.match(r"^([A-Z0-9_]+)\.([A-Z0-9_]+)%TYPE$", raw, flags=re.I)
        if not m:
            return None
        ref_table = m.group(1).upper()
        ref_column = m.group(2).upper()
        target_schema = (schema_name or "").strip().upper()
        for row in self.rows:
            row_schema = row.get("OWNER", "").strip().upper()
            row_table = row.get("TABLE_NAME", "").strip().upper()
            row_column = row.get("COLUMN_NAME", "").strip().upper()
            if target_schema and row_schema and row_schema != target_schema:
                continue
            if row_table == ref_table and row_column == ref_column:
                data_type = row.get("DATA_TYPE", "").strip().upper()
                if data_type:
                    return data_type
        return None


def resolve_actual_plsql_type(plsql_type: str, type_catalog: Optional[PackageTypeCatalog] = None, schema_name: Optional[str] = None) -> str:
    raw = (plsql_type or "").strip()
    if "%TYPE" in raw.upper() and type_catalog is not None:
        resolved = type_catalog.resolve_percent_type(raw, schema_name=schema_name)
        if resolved:
            return resolved
    if re.fullmatch(r"SERVER_STD\.CONTROL_COLUMN%TYPE", raw, flags=re.I):
        return "VARCHAR2(3 CHAR)"
    return raw


def is_oracle_primitive_type(plsql_type: str) -> bool:
    t = re.sub(r"\s+", " ", (plsql_type or "").strip().upper())
    if not t:
        return False
    if t.endswith("%TYPE"):
        return False
    base = re.split(r"\(|\s", t, maxsplit=1)[0]
    return t in ORACLE_PRIMITIVE_TYPES or base in ORACLE_PRIMITIVE_TYPES


def split_type_reference(plsql_type: str, current_package_name: Optional[str] = None) -> Tuple[Optional[str], str]:
    raw = (plsql_type or "").strip().upper()
    if not raw or "%TYPE" in raw:
        return None, raw
    if "." in raw:
        pkg, name = raw.split(".", 1)
        return pkg, name
    return current_package_name, raw


def is_appl_unit_field_name(item_name: str) -> bool:
    u = (item_name or "").upper()
    return bool(re.search(r"(?:APPL|APPLICATION).*(?:UNIT).*CODE", u))


def is_appl_incr_seq_field_name(item_name: str) -> bool:
    u = (item_name or "").upper()
    return bool(re.search(r"(?:APPL|APPLICATION).*(?:INCR.*SEQ.*NO|SEQ.*NO)", u))


def is_dec_or_appl_related_field(item_name: str) -> bool:
    u = (item_name or "").upper()
    entity_patterns = [
        r"(^|_)(APPL|APPLICATION)(_|$)",
        r"(^|_)(DEC|DECISION)(_|$)",
        r"(^|_)(PAI|PDTA)(_|$)",
    ]
    qualifier_patterns = [
        r"(^|_)(UNIT|UNIT_CODE)(_|$)",
        r"(^|_)(YEAR)(_|$)",
        r"(^|_)(INCR)(_|$)",
        r"(^|_)(INCR_SEQ_NO)(_|$)",
        r"(^|_)(SEQ_NO)(_|$)",
    ]
    return any(re.search(ep, u) for ep in entity_patterns) and any(re.search(qp, u) for qp in qualifier_patterns)


def should_exclude_lov_from_xhtml(fd: Optional["FieldDefinition"]) -> bool:
    return fd is not None and is_dec_or_appl_related_field(fd.item_name)


def dynamic_default_for_field(fd: Optional["FieldDefinition"]) -> Optional[str]:
    if fd is None:
        return None
    if fd.checkbox_options is not None:
        return fd.checkbox_options.initialize_value
    if fd.option_enum_values:
        first = sorted(fd.option_enum_values, key=lambda x: x.index)[0]
        return first.id
    return None


def lov_code_for_field(fd: Optional["FieldDefinition"]) -> str:
    if fd is None:
        return "LXXXX"
    if is_dec_or_appl_related_field(fd.item_name):
        return ""
    return "LXXXX"


def field_subset_of(left: "SqlRecord", right: "SqlRecord") -> bool:
    right_fields = {f.name.upper() for f in right.fields}
    left_fields = [f.name.upper() for f in left.fields]
    return all(name in right_fields for name in left_fields) and len(left_fields) <= len(right_fields)


def normalize_record_alias_name(name: str) -> str:
    n = (name or "").upper().strip()
    n = re.sub(r"_(?:INSERT|INS)$", "", n)
    return n



@dataclass
class SqlField:
    name: str
    plsql_type: str


@dataclass
class SqlRecord:
    name: str
    package_name: str
    schema_name: Optional[str]
    fields: List[SqlField] = field(default_factory=list)


@dataclass
class SqlTable:
    name: str
    package_name: str
    schema_name: Optional[str]
    element_type: str = ""


@dataclass
class SqlParam:
    name: str
    mode: str
    plsql_type: str


@dataclass
class SqlRoutine:
    kind: str
    name: str
    package_name: str
    schema_name: Optional[str]
    params: List[SqlParam] = field(default_factory=list)


@dataclass
class ParsedSql:
    package_name: str
    records: Dict[str, SqlRecord] = field(default_factory=dict)
    tables: Dict[str, SqlTable] = field(default_factory=dict)
    routines: Dict[str, SqlRoutine] = field(default_factory=dict)
    package_schemas: Dict[str, Optional[str]] = field(default_factory=dict)




@dataclass
class OptionValue:
    index: int
    id: str
    description: str


@dataclass
class CheckboxOptions:
    checked_value: str = "1"
    unchecked_value: str = "0"
    initialize_value: str = "0"


@dataclass
class FieldDefinition:
    block_name: str
    item_name: str
    title_label: Optional[str] = None
    canvas: Optional[str] = None
    enabled: Optional[str] = None
    displayed: Optional[str] = None
    required: Optional[str] = None
    max_length: Optional[str] = None
    item_type: Optional[str] = None
    datatype: Optional[str] = None
    links_to_code: List[str] = field(default_factory=list)
    options_body: Optional[str] = None
    lov_body: Optional[str] = None
    validation_body: Optional[str] = None
    button_or_trigger_body: Optional[str] = None
    option_values: List[Tuple[str, str]] = field(default_factory=list)
    option_enum_values: List[OptionValue] = field(default_factory=list)
    checkbox_options: Optional[CheckboxOptions] = None
    has_lov: bool = False
    has_validation: bool = False
    descr_item: Optional[str] = None

    @property
    def property_name(self) -> str:
        return to_camel(self.item_name)


@dataclass
class ButtonDefinition:
    block_name: str
    item_name: str
    title_label: Optional[str]
    links_to_code: List[str] = field(default_factory=list)
    code_body: Optional[str] = None
    summary: Optional[str] = None

    @property
    def action_name(self) -> str:
        n = self.item_name.upper()
        if "CALC" in n:
            return "calculateAmount"
        if "SAVE" in n:
            return "save"
        if "DLT" in n or "DELETE" in n:
            return "delete"
        if "SEARCH" in n or "QRY" in n:
            return "search"
        return to_camel(self.item_name)


@dataclass
class ReportBlock:
    block_name: str
    fields: Dict[str, FieldDefinition] = field(default_factory=dict)
    buttons: Dict[str, ButtonDefinition] = field(default_factory=dict)


@dataclass
class ProcedureCallInfo:
    package_name: str
    routine_name: str
    args: List[str] = field(default_factory=list)
    source_code: str = ""


@dataclass
class ParsedReport:
    blocks: Dict[str, ReportBlock]
    main_block: str
    raw_text: str
    routine_bodies: Dict[str, str] = field(default_factory=dict)
    procedure_calls: Dict[str, List[ProcedureCallInfo]] = field(default_factory=dict)


@dataclass
class ScreenModel:
    package_name: str
    schema_name: Optional[str]
    prefix_upper: str
    prefix_class: str
    screen_name: str
    xhtml_name: str
    form_key: str
    main_block: str
    header_record_name: Optional[str]
    fields: List[FieldDefinition]
    buttons: List[ButtonDefinition]
    records: Dict[str, SqlRecord]
    tables: Dict[str, SqlTable]
    routines: Dict[str, SqlRoutine]
    referenced_records: List[str]
    referenced_tables: List[str]
    validation_fields: List[str]
    create_qry_new_appl: bool
    option_enum_fields: List[str]
    report_blocks: List[ReportBlock] = field(default_factory=list)
    routine_bodies: Dict[str, str] = field(default_factory=dict)
    procedure_calls: Dict[str, List[ProcedureCallInfo]] = field(default_factory=dict)
    raw_report_text: str = ""


@dataclass
class BlockBindingInfo:
    block_name: str
    kind: str
    record_name: Optional[str]
    dto_class: Optional[str]
    class_name: Optional[str]
    instance_name: str
    field_names: List[str] = field(default_factory=list)
    extra_field_names: List[str] = field(default_factory=list)
    table_name: Optional[str] = None
    is_table: bool = False

    @property
    def expr(self) -> str:
        return f"controller.{self.instance_name}"


class InputLoader:
    def load(self, main_sql_path: str, additional_sql_paths: List[str], report_path: str, output_zip_path: str, types_csv_path: Optional[str] = None) -> InputBundle:
        main_sql_p = Path(main_sql_path)
        report_p = Path(report_path)
        add_paths = [Path(p) for p in additional_sql_paths]
        main_sql_text = main_sql_p.read_text(encoding="utf-8", errors="ignore")
        report_html = report_p.read_text(encoding="utf-8", errors="ignore")
        additional_sql_texts = [p.read_text(encoding="utf-8", errors="ignore") for p in add_paths]
        schema_name, package_name = detect_schema_and_package(main_sql_text, main_sql_p.name)
        out_zip = Path(output_zip_path) if output_zip_path else report_p.with_name(report_p.stem + "_generated.zip")
        types_csv_p = Path(types_csv_path) if types_csv_path else None
        type_catalog = PackageTypeCatalog.from_csv(types_csv_p)
        return InputBundle(
            main_sql_path=main_sql_p,
            main_sql_text=main_sql_text,
            additional_sql_paths=add_paths,
            additional_sql_texts=additional_sql_texts,
            report_path=report_p,
            report_html=report_html,
            output_zip_path=out_zip,
            schema_name=schema_name,
            package_name=package_name,
            types_csv_path=types_csv_p,
            package_type_catalog=type_catalog,
        )


class SqlParser:
    def parse(self, bundle: InputBundle) -> ParsedSql:
        parsed = ParsedSql(package_name=bundle.package_name)
        self._parse_one(parsed, bundle.main_sql_text, bundle.schema_name, bundle.package_name, include_routines=True)
        for path, text in zip(bundle.additional_sql_paths, bundle.additional_sql_texts):
            schema_name, pkg = detect_schema_and_package(text, path.name)
            self._parse_one(parsed, text, schema_name, pkg, include_routines=False)
        return parsed

    def _parse_one(self, parsed: ParsedSql, sql_text: str, schema_name: Optional[str], package_name: str, include_routines: bool) -> None:
        parsed.package_schemas[package_name] = schema_name
        clean = strip_plsql_comments(sql_text)
        for m in re.finditer(r"TYPE\s+(\w+)\s+IS\s+RECORD\s*\((.*?)\)\s*;", clean, flags=re.S | re.I):
            record_name = m.group(1).upper()
            fields: List[SqlField] = []
            for raw in split_top_level(m.group(2)):
                raw = raw.strip()
                parts = re.split(r"\s+", raw, maxsplit=1)
                if len(parts) == 2 and re.match(r"^[A-Z0-9_]+$", parts[0], flags=re.I):
                    fields.append(SqlField(parts[0].upper(), parts[1].strip()))
            parsed.records.setdefault(record_name, SqlRecord(record_name, package_name, schema_name, fields))
        for m in re.finditer(r"TYPE\s+(\w+)\s+IS\s+TABLE\s+OF\s+(\w+)\s*(?:INDEX\s+BY\s+\w+)?\s*;", clean, flags=re.S | re.I):
            table_name = m.group(1).upper()
            parsed.tables.setdefault(table_name, SqlTable(
                name=table_name,
                package_name=package_name,
                schema_name=schema_name,
                element_type=m.group(2).upper(),
            ))
        if include_routines:
            for m in re.finditer(r"(PROCEDURE|FUNCTION)\s+(\w+)\s*\((.*?)\)\s*(?:RETURN\s+[^;]+)?\s*;", clean, flags=re.S | re.I):
                params: List[SqlParam] = []
                for raw in split_top_level(m.group(3)):
                    raw = raw.strip()
                    pm = re.match(r"(\w+)\s+(IN\s+OUT|OUT|IN)?\s*(.+)$", raw, flags=re.I | re.S)
                    if pm:
                        params.append(SqlParam(
                            name=pm.group(1),
                            mode=(pm.group(2) or "IN").upper().replace("  ", " "),
                            plsql_type=pm.group(3).strip(),
                        ))
                parsed.routines[m.group(2).upper()] = SqlRoutine(
                    kind=m.group(1).upper(),
                    name=m.group(2).upper(),
                    package_name=package_name,
                    schema_name=schema_name,
                    params=params,
                )


class ReportParser:
    def parse(self, bundle: InputBundle) -> ParsedReport:
        if BeautifulSoup is None:
            raise RuntimeError("BeautifulSoup is required for this full engine version.")
        soup = BeautifulSoup(bundle.report_html, "html.parser")
        raw_text = soup.get_text("\n")
        blocks: Dict[str, ReportBlock] = {}
        field_section_ids = [tag.get("id") for tag in soup.find_all(id=True) if str(tag.get("id", "")).endswith("-fields")]
        main_block = None
        max_items = -1
        for section_id in field_section_ids:
            raw_block_id = section_id[:-7]
            block_name = normalize_block_id(raw_block_id)
            report_block = self._parse_block(soup, block_name, raw_block_id)
            blocks[block_name] = report_block
            if len(report_block.fields) > max_items:
                max_items = len(report_block.fields)
                main_block = block_name
        if not blocks:
            fallback_block = self._detect_main_block_fallback(raw_text)
            blocks[fallback_block] = self._fallback_block(raw_text, fallback_block)
            main_block = fallback_block
        return ParsedReport(
            blocks=blocks,
            main_block=main_block or "FORM_BLOCK",
            raw_text=raw_text,
            routine_bodies=self._extract_routine_bodies(soup, raw_text),
            procedure_calls=self._extract_procedure_calls(raw_text),
        )

    def _parse_block(self, soup, block_name: str, raw_block_id: Optional[str] = None) -> ReportBlock:
        raw_block_id = raw_block_id or block_name.lower()
        block = ReportBlock(block_name=block_name)
        block.fields = self._parse_fields_table(soup, block_name, raw_block_id)
        self._parse_options_section(soup, block_name, raw_block_id, block.fields)
        self._parse_lov_section(soup, block_name, raw_block_id, block.fields)
        self._parse_validation_section(soup, block_name, raw_block_id, block.fields)
        block.buttons = self._parse_buttons_section(soup, block_name, raw_block_id, block.fields)
        self._infer_descr_fields(block.fields)
        return block

    def _section_by_id(self, soup, section_id: str):
        return soup.find(id=section_id)

    def _parse_fields_table(self, soup, block_name: str, raw_block_id: str) -> Dict[str, FieldDefinition]:
        section = self._section_by_id(soup, f"{raw_block_id}-fields")
        if section is None:
            return {}
        table = section.find("table") or (section if getattr(section, "name", "") == "table" else None)
        if table is None:
            return {}
        headers = [normalize_spaces(th.get_text(" ", strip=True)).lower() for th in table.find_all("th")]
        idx = {h: i for i, h in enumerate(headers)}
        need = ["item name", "title/label", "canvas", "enabled", "displayed", "required", "max length", "type", "datatype", "links to code"]
        if not all(h in idx for h in need):
            return {}
        out: Dict[str, FieldDefinition] = {}
        rows = table.find_all("tr")[1:]
        for tr in rows:
            tds = tr.find_all("td")
            if not tds:
                continue
            values = [normalize_spaces(td.get_text(" ", strip=True)) for td in tds]
            if len(values) < len(headers):
                values += [""] * (len(headers) - len(values))
            item_full = normalize_item_full_name(values[idx["item name"]])
            if not item_full.startswith(block_name.upper() + "."):
                continue
            item_name = item_full.split(".")[-1].upper()
            links = [normalize_spaces(x) for x in re.split(r"[|,]", values[idx["links to code"]]) if normalize_spaces(x)]
            out[item_name] = FieldDefinition(
                block_name=block_name,
                item_name=item_name,
                title_label=values[idx["title/label"]] or None,
                canvas=values[idx["canvas"]],
                enabled=values[idx["enabled"]],
                displayed=values[idx["displayed"]],
                required=values[idx["required"]],
                max_length=values[idx["max length"]],
                item_type=values[idx["type"]],
                datatype=values[idx["datatype"]],
                links_to_code=links,
            )
        return out

    def _section_text_by_item(self, section, block_name: str, item_name: str) -> Optional[str]:
        if section is None:
            return None
        full_text = section.get_text("\n")
        pattern = re.compile(rf"(?=^{re.escape(block_name)}\.{re.escape(item_name)}\b)", flags=re.I | re.M)
        matches = list(pattern.finditer(full_text))
        if not matches:
            return None
        start = matches[0].start()
        # end at next BLOCK.ITEM marker or end of section
        next_pattern = re.compile(rf"^\s*{re.escape(block_name)}\.[A-Z0-9_]+\b", flags=re.I | re.M)
        next_matches = [m for m in next_pattern.finditer(full_text, start + 1)]
        end = next_matches[0].start() if next_matches else len(full_text)
        snippet = full_text[start:end].strip()
        return snippet or None


    def _lookup_validation_code(self, soup, link_name: str) -> Optional[str]:
        if not link_name:
            return None
        comp_id = "val-" + link_name.lower().replace("_", "-")
        comp = soup.find(id=comp_id)
        if comp is None:
            return None
        code_tag = comp.find("code")
        return code_tag.get_text("\n").strip() if code_tag else comp.get_text("\n", strip=True)

    def _lookup_button_trigger_code(self, soup, raw_block_id: str, item_name: str) -> Optional[str]:
        block_part = raw_block_id.lower().replace("_", "-")
        item_part = item_name.lower().replace("_", "-")
        candidates = [
            f"trig-{block_part}-{item_part}-when-button-pressed",
            f"trig-block-{block_part}-{item_part}-when-button-pressed",
        ]

        details = None
        for comp_id in candidates:
            details = soup.find("details", id=comp_id)
            if details is not None:
                break

        if details is None:
            suffix = f"-{item_part}-when-button-pressed"
            for d in soup.find_all("details", id=True):
                did = str(d.get("id", "")).lower()
                if did.startswith("trig-") and did.endswith(suffix):
                    details = d
                    break

        if details is None:
            return None

        code_tag = details.select_one("pre code")
        if code_tag and code_tag.get_text("\n", strip=True):
            return code_tag.get_text("\n").strip()

        code_tag = details.find("code")
        if code_tag and code_tag.get_text("\n", strip=True):
            return code_tag.get_text("\n").strip()

        pre_tag = details.find("pre")
        if pre_tag and pre_tag.get_text("\n", strip=True):
            return pre_tag.get_text("\n").strip()

        return None

    def _lookup_button_summary(self, soup, raw_block_id: str, item_name: str) -> Optional[str]:
        block_part = raw_block_id.lower().replace("_", "-")
        item_part = item_name.lower().replace("_", "-")
        comp_id = f"btn-{block_part}-{item_part}"
        comp = soup.find(id=comp_id)
        if comp is None:
            return None
        summary = comp.find("summary")
        if summary is not None:
            return summary.get_text(" ", strip=True)
        return None


    def _parse_options_section(self, soup, block_name: str, raw_block_id: str, fields: Dict[str, FieldDefinition]) -> None:
        section = self._section_by_id(soup, f"{raw_block_id}-options")
        for item_name, fd in fields.items():
            snippet = self._section_text_by_item(section, block_name, item_name)
            if snippet:
                fd.options_body = snippet
            details = self._lookup_option_details(soup, raw_block_id, block_name, item_name)
            if details is not None:
                self._parse_field_options_details(details, fd)

    def _lookup_option_details(self, soup, raw_block_id: str, block_name: str, item_name: str):
        candidates = [
            f"opt-{raw_block_id}-{item_name}",
            f"opt-{block_name}-{item_name}",
            f"opt-{raw_block_id.lower()}-{item_name.lower()}",
            f"opt-{block_name.lower()}-{item_name.lower()}",
            f"opt-{raw_block_id.lower().replace('_','-')}-{item_name.lower().replace('_','-')}",
            f"opt-{block_name.lower().replace('_','-')}-{item_name.lower().replace('_','-')}",
        ]
        for cid in candidates:
            details = soup.find("details", id=cid)
            if details is not None:
                return details
        suffixes = {
            f"-{item_name}", f"-{item_name.lower()}",
            f"-{item_name.lower().replace('_','-')}"
        }
        block_tokens = {raw_block_id.lower(), block_name.lower(), raw_block_id.lower().replace('_','-'), block_name.lower().replace('_','-')}
        for d in soup.find_all("details", id=True):
            did = str(d.get("id", ""))
            low = did.lower()
            if not low.startswith("opt-"):
                continue
            if any(low.endswith(sfx.lower()) for sfx in suffixes) and any(tok in low for tok in block_tokens):
                return d
        return None

    def _parse_field_options_details(self, details, fd: FieldDefinition) -> None:
        table = details.find("table")
        if table is None:
            return
        headers = [th.get_text(" ", strip=True) for th in table.find_all("th")]
        header_map = {h.strip().lower(): idx for idx, h in enumerate(headers)}
        rows = []
        for tr in table.find_all("tr"):
            cells = tr.find_all(["td", "th"])
            if cells and cells[0].name == "td":
                rows.append([c.get_text(" ", strip=True) for c in cells])
        if {"index", "name", "value"}.issubset(header_map):
            values = []
            for row in rows:
                try:
                    idx = int((row[header_map["index"]] or "0").strip())
                except Exception:
                    idx = len(values) + 1
                desc = row[header_map["name"]].strip() if header_map["name"] < len(row) else ""
                val = row[header_map["value"]].strip() if header_map["value"] < len(row) else ""
                values.append(OptionValue(idx, val, desc))
            fd.option_enum_values = sorted(values, key=lambda x: x.index)
        elif {"checkedvalue", "uncheckedvalue", "initializevalue"}.issubset(header_map) and rows:
            row = rows[0]
            fd.checkbox_options = CheckboxOptions(
                checked_value=row[header_map["checkedvalue"]].strip() if header_map["checkedvalue"] < len(row) else "1",
                unchecked_value=row[header_map["uncheckedvalue"]].strip() if header_map["uncheckedvalue"] < len(row) else "0",
                initialize_value=row[header_map["initializevalue"]].strip() if header_map["initializevalue"] < len(row) else "0",
            )

    def _parse_lov_section(self, soup, block_name: str, raw_block_id: str, fields: Dict[str, FieldDefinition]) -> None:
        section = self._section_by_id(soup, f"{raw_block_id}-lov")
        for item_name, fd in fields.items():
            snippet = self._section_text_by_item(section, block_name, item_name)
            if snippet:
                fd.lov_body = snippet
                fd.has_lov = True

    def _parse_validation_section(self, soup, block_name: str, raw_block_id: str, fields: Dict[str, FieldDefinition]) -> None:
        section = self._section_by_id(soup, f"{raw_block_id}-validations") or self._section_by_id(soup, f"block-{raw_block_id.lower().replace('_','-')}-validations")
        for item_name, fd in fields.items():
            chosen = None
            for link_name in fd.links_to_code:
                if link_name and link_name.strip().lower() not in {"lov", "options", "trigger"}:
                    chosen = self._lookup_validation_code(soup, link_name)
                    if chosen:
                        break
            if not chosen:
                chosen = self._section_text_by_item(section, block_name, item_name)
            if chosen:
                fd.validation_body = chosen
                fd.has_validation = True

    def _parse_buttons_section(self, soup, block_name: str, raw_block_id: str, fields: Dict[str, FieldDefinition]) -> Dict[str, ButtonDefinition]:
        section = self._section_by_id(soup, f"{raw_block_id}-buttons") or self._section_by_id(soup, f"block-{raw_block_id.lower().replace('_','-')}-buttons")
        buttons: Dict[str, ButtonDefinition] = {}
        for item_name, fd in fields.items():
            if (fd.item_type or "").strip().lower() == "push button":
                buttons[item_name] = ButtonDefinition(
                    block_name=block_name,
                    item_name=item_name,
                    title_label=fd.title_label,
                    links_to_code=fd.links_to_code[:],
                    summary=self._lookup_button_summary(soup, raw_block_id, item_name),
                )
        for item_name, button in buttons.items():
            snippet = self._lookup_button_trigger_code(soup, raw_block_id, item_name)
            if snippet:
                button.code_body = snippet
        return buttons

    def _infer_descr_fields(self, fields: Dict[str, FieldDefinition]) -> None:
        names = set(fields.keys())
        for item_name, fd in fields.items():
            if item_name == "CNTRY_COUNTRY_CITIZENSHIP" and "COUNTRY_NAME" in names:
                fd.descr_item = "COUNTRY_NAME"
                continue
            for cand in [
                item_name + "_DESCR",
                item_name.replace("_CODE", "_NAME"),
                item_name.replace("_ID", "_NAME"),
                item_name.replace("_CODE", "_DESCR"),
                item_name.replace("_ID", "_DESCR"),
            ]:
                if cand in names:
                    fd.descr_item = cand
                    break

    def _detect_main_block_fallback(self, raw_text: str) -> str:
        pairs = re.findall(r"([A-Z0-9_]+)\.([A-Z0-9_]+)", raw_text)
        freq: Dict[str, int] = {}
        for block, _ in pairs:
            freq[block] = freq.get(block, 0) + 1
        return max(freq, key=freq.get) if freq else "FORM_BLOCK"

    def _fallback_block(self, raw_text: str, block_name: str) -> ReportBlock:
        block = ReportBlock(block_name=block_name)
        for item in sorted({m.group(1).upper() for m in re.finditer(re.escape(block_name) + r"\.([A-Z0-9_]+)", raw_text)}):
            block.fields[item] = FieldDefinition(
                block_name=block_name,
                item_name=item,
                title_label=None,
            )
        return block

    def _extract_routine_bodies(self, soup, raw_text: str) -> Dict[str, str]:
        out: Dict[str, str] = {}
        blocks = [pre.get_text("\n") for pre in soup.find_all("pre")]
        if raw_text:
            blocks.append(raw_text)
        patt = re.compile(r"((PROCEDURE|FUNCTION)\s+(\w+)\s*(\(|IS).*?END\s+\3\s*;)", flags=re.S | re.I)
        for block in blocks:
            for m in patt.finditer(block):
                out[m.group(3).upper()] = m.group(1).strip()
        return out

    def _extract_procedure_calls(self, raw_text: str) -> Dict[str, List[ProcedureCallInfo]]:
        text = self._strip_plsql_comments(raw_text or "")
        ignored = {
            "ADD_PARAMETER", "CALL_FORM", "GO_BLOCK", "GO_ITEM", "DO_KEY", "EXECUTE_QUERY",
            "MESSAGE", "SHOW_ALERT", "SET_ITEM_PROPERTY", "SET_BLOCK_PROPERTY", "GET_ITEM_PROPERTY",
            "GET_BLOCK_PROPERTY", "NAME_IN", "COPY", "CLEAR_BLOCK", "CREATE_RECORD", "DELETE_RECORD",
            "NEXT_RECORD", "PREVIOUS_RECORD", "FIRST_RECORD", "LAST_RECORD", "SYNCHRONIZE",
            "COMMIT_FORM", "ROLLBACK", "RAISE_FORM_TRIGGER_FAILURE", "ENTER_QUERY", "EXIT_FORM",
        }
        out: Dict[str, List[ProcedureCallInfo]] = {}
        i = 0
        n = len(text)
        while i < n:
            m = re.search(r'([A-Z][A-Z0-9_]*)\.([A-Z][A-Z0-9_]*)\s*\(', text[i:], flags=re.I)
            if not m:
                break
            start = i + m.start()
            pkg = m.group(1).upper()
            routine = m.group(2).upper()
            open_idx = i + m.end() - 1
            if routine in ignored:
                i = open_idx + 1
                continue
            close_idx = self._find_matching_paren(text, open_idx)
            if close_idx < 0:
                i = open_idx + 1
                continue
            args_text = text[open_idx + 1:close_idx]
            args = self._split_plsql_args(args_text)
            snippet = text[start:close_idx + 1].strip()
            info = ProcedureCallInfo(package_name=pkg, routine_name=routine, args=args, source_code=snippet)
            out.setdefault(routine, []).append(info)
            i = close_idx + 1
        return out

    def _strip_plsql_comments(self, text: str) -> str:
        text = re.sub(r'/\*.*?\*/', ' ', text, flags=re.S)
        text = re.sub(r'--.*?(?=\n|$)', ' ', text)
        return text

    def _find_matching_paren(self, text: str, open_idx: int) -> int:
        depth = 0
        in_string = False
        i = open_idx
        while i < len(text):
            ch = text[i]
            if ch == "'":
                if in_string and i + 1 < len(text) and text[i + 1] == "'":
                    i += 2
                    continue
                in_string = not in_string
                i += 1
                continue
            if in_string:
                i += 1
                continue
            if ch == '(':
                depth += 1
            elif ch == ')':
                depth -= 1
                if depth == 0:
                    return i
            i += 1
        return -1

    def _split_plsql_args(self, text: str) -> List[str]:
        args: List[str] = []
        buf: List[str] = []
        depth = 0
        in_string = False
        i = 0
        while i < len(text):
            ch = text[i]
            if ch == "'":
                buf.append(ch)
                if in_string and i + 1 < len(text) and text[i + 1] == "'":
                    buf.append(text[i + 1])
                    i += 2
                    continue
                in_string = not in_string
                i += 1
                continue
            if not in_string:
                if ch == '(':
                    depth += 1
                elif ch == ')':
                    depth -= 1
                elif ch == ',' and depth == 0:
                    part = normalize_spaces(''.join(buf))
                    if part:
                        args.append(part)
                    buf = []
                    i += 1
                    continue
            buf.append(ch)
            i += 1
        part = normalize_spaces(''.join(buf))
        if part:
            args.append(part)
        return args


class ScreenModelBuilder:
    def build(self, bundle: InputBundle, parsed_sql: ParsedSql, parsed_report: ParsedReport) -> ScreenModel:
        pkg = bundle.package_name
        prefix_upper = package_prefix(pkg)
        prefix_class = package_prefix_class(pkg)
        screen_name = to_pascal(pkg)
        xhtml_name = package_to_lower_camel(pkg) + ".xhtml"
        main_block = parsed_report.main_block
        main_block_obj = parsed_report.blocks[main_block]
        form_key = main_block[:1].lower() + to_pascal(main_block)[1:]

        header_record_name = self._resolve_header_record_name(parsed_sql, pkg)
        referenced_records, referenced_tables = self._resolve_referenced_types(parsed_sql, pkg, header_record_name)
        report_blocks = list(parsed_report.blocks.values())
        fields = []
        buttons = []
        for block in report_blocks:
            fields.extend(self._collect_fields(block))
            buttons.extend(self._collect_buttons(block))
        fields = self._dedupe_fields(fields)
        buttons = self._dedupe_buttons(buttons)
        validation_fields = self._resolve_validation_fields(fields)
        create_qry_new_appl = self._should_create_qry_new_appl(fields)
        option_enum_fields = [fd.item_name for fd in fields if fd.option_enum_values]

        return ScreenModel(
            package_name=pkg,
            schema_name=bundle.schema_name,
            prefix_upper=prefix_upper,
            prefix_class=prefix_class,
            screen_name=screen_name,
            xhtml_name=xhtml_name,
            form_key=form_key,
            main_block=main_block,
            header_record_name=header_record_name,
            fields=fields,
            buttons=buttons,
            records=parsed_sql.records,
            tables=parsed_sql.tables,
            routines=parsed_sql.routines,
            referenced_records=referenced_records,
            referenced_tables=referenced_tables,
            validation_fields=validation_fields,
            create_qry_new_appl=create_qry_new_appl,
            option_enum_fields=option_enum_fields,
            report_blocks=report_blocks,
            routine_bodies=parsed_report.routine_bodies,
            procedure_calls={k: [c for c in v if c.package_name == pkg.upper()] for k, v in parsed_report.procedure_calls.items() if any(c.package_name == pkg.upper() for c in v)},
            raw_report_text=parsed_report.raw_text,
        )

    def _resolve_header_record_name(self, parsed_sql: ParsedSql, package_name: str) -> Optional[str]:
        for rec_name, rec in parsed_sql.records.items():
            if rec.package_name == package_name and rec_name.endswith("REC_HEADER"):
                return rec_name
        for rec_name in parsed_sql.records:
            if rec_name.endswith("REC_HEADER"):
                return rec_name
        return None

    def _resolve_referenced_types(self, parsed_sql: ParsedSql, package_name: str, header_record_name: Optional[str]) -> Tuple[List[str], List[str]]:
        recs: List[str] = []
        tabs: List[str] = []
        if header_record_name:
            recs.append(header_record_name)
        for routine in parsed_sql.routines.values():
            if routine.package_name != package_name:
                continue
            for p in routine.params:
                raw_type = (p.plsql_type or "").strip().upper()
                if "%TYPE" in raw_type or is_oracle_primitive_type(raw_type):
                    continue
                pkg_ref, obj_name = split_type_reference(raw_type, current_package_name=package_name)
                if pkg_ref and pkg_ref not in parsed_sql.package_schemas:
                    raise RuntimeError(f"Could not find declaration for package {pkg_ref}.")
                lookup_type = f"{pkg_ref}.{obj_name}" if pkg_ref and pkg_ref != package_name else obj_name
                rec = resolve_record(parsed_sql.records, package_name, lookup_type)
                if rec is not None:
                    if rec.name.endswith("_INSERT"):
                        base_name = normalize_record_alias_name(rec.name)
                        base_rec = parsed_sql.records.get(base_name)
                        recs.append(base_rec.name if base_rec and field_subset_of(rec, base_rec) else rec.name)
                    else:
                        recs.append(rec.name)
                    continue
                tab = resolve_table(parsed_sql.tables, package_name, lookup_type)
                if tab is not None:
                    tabs.append(tab.name)
                    if tab.element_type in parsed_sql.records:
                        rec_el = parsed_sql.records[tab.element_type]
                        if rec_el.name.endswith("_INSERT"):
                            base_name = normalize_record_alias_name(rec_el.name)
                            base_rec = parsed_sql.records.get(base_name)
                            recs.append(base_rec.name if base_rec and field_subset_of(rec_el, base_rec) else rec_el.name)
                        else:
                            recs.append(rec_el.name)
                    continue
                raise RuntimeError(f"Could not resolve package object {raw_type} in package declarations.")
        return dedupe_preserve_order(recs), dedupe_preserve_order(tabs)

    def _collect_fields(self, block: ReportBlock) -> List[FieldDefinition]:
        out: List[FieldDefinition] = []
        seen = set()
        for fd in block.fields.values():
            if (fd.item_type or "").strip().lower() == "push button":
                continue
            if fd.item_name in seen:
                continue
            seen.add(fd.item_name)
            fd.has_lov = bool(fd.lov_body)
            explicit_validation = bool(fd.validation_body)
            fallback_validation = any(link and link.strip().lower() not in {"lov", "options", "trigger"} for link in fd.links_to_code)
            fd.has_validation = explicit_validation or fallback_validation
            out.append(fd)
        return out

    def _collect_buttons(self, block: ReportBlock) -> List[ButtonDefinition]:
        seen = set()
        out = []
        for btn in block.buttons.values():
            if btn.item_name not in seen:
                seen.add(btn.item_name)
                out.append(btn)
        return out


    def _dedupe_fields(self, fields: List[FieldDefinition]) -> List[FieldDefinition]:
        out = []
        seen = set()
        for fd in fields:
            key = (fd.block_name, fd.item_name)
            if key not in seen:
                seen.add(key)
                out.append(fd)
        return out

    def _dedupe_buttons(self, buttons: List[ButtonDefinition]) -> List[ButtonDefinition]:
        out = []
        seen = set()
        for btn in buttons:
            key = (btn.block_name, btn.item_name)
            if key not in seen:
                seen.add(key)
                out.append(btn)
        return out

    def _resolve_validation_fields(self, fields: List[FieldDefinition]) -> List[str]:
        out = []
        for fd in fields:
            if is_dec_or_appl_related_field(fd.item_name):
                continue
            if fd.has_validation:
                out.append(fd.item_name)
        return dedupe_preserve_order(out)

    def _should_create_qry_new_appl(self, fields: List[FieldDefinition]) -> bool:
        return any((is_appl_unit_field_name(fd.item_name) or is_appl_incr_seq_field_name(fd.item_name)) and (fd.validation_body or fd.has_validation) for fd in fields)



class ControlFlowConverter:
    @staticmethod
    def detect_if_blocks(body: str) -> List[str]:
        if not body:
            return []
        clean = strip_plsql_comments(body)
        out = []
        patt = re.compile(r"IF\s+.*?\s+THEN(.*?)(?:ELSIF\s+.*?\s+THEN|ELSE|END\s+IF)", flags=re.I | re.S)
        for m in patt.finditer(clean):
            snippet = normalize_spaces(m.group(1))
            if snippet:
                out.append(snippet)
        return out

    @staticmethod
    def detect_utils_sm_messages(body: str) -> List[str]:
        out = []
        for m in re.finditer(r"UTILS\.SM\((.*?)\)", body or "", flags=re.I | re.S):
            val = normalize_spaces(m.group(1))
            if val:
                out.append(val)
        return out

    @staticmethod
    def build_comment_block(body: str, limit: int = 80) -> str:
        if not body:
            return "        // TODO"
        lines = []
        if_blocks = ControlFlowConverter.detect_if_blocks(body)
        if if_blocks:
            lines.append(f"        // Detected IF blocks: {len(if_blocks)}")
            for idx, blk in enumerate(if_blocks[:8], start=1):
                lines.append(f"        // IF[{idx}] -> {blk}")
        utils_msgs = ControlFlowConverter.detect_utils_sm_messages(body)
        if utils_msgs:
            lines.append("        // Detected UTILS.SM:")
            for msg in utils_msgs[:8]:
                lines.append(f"        //   {msg}")
        if not lines:
            lines.extend("        // " + line for line in body.splitlines()[:limit] if line.strip())
        return "\n".join(lines) if lines else "        // TODO"


class PlsqlSemanticConverter:
    @staticmethod
    def first_select_into(body: str):
        if not body:
            return None
        clean = strip_plsql_comments(body)
        m = re.search(r"SELECT\s+(.*?)\s+INTO\s+.*?\s+FROM\s+(.*?)\s+WHERE\s+(.*?)(?:;|$)", clean, flags=re.I | re.S)
        if not m:
            return None
        return normalize_spaces(m.group(1)), normalize_spaces(m.group(2)), normalize_spaces(m.group(3))

    @staticmethod
    def infer_param_name(field: FieldDefinition, where_clause: str) -> str:
        bind = re.search(r":([A-Z0-9_]+)", where_clause, flags=re.I)
        if bind:
            return to_camel(bind.group(1))
        if field.item_name.endswith("_CODE"):
            return "code"
        if field.item_name.endswith("_ID"):
            return "id"
        return field.property_name

    @staticmethod
    def validation_java(field: FieldDefinition) -> str:
        body = field.validation_body or ""
        parsed = PlsqlSemanticConverter.first_select_into(body)
        if parsed:
            select_expr, from_clause, where_clause = parsed
            prop = field.property_name
            descr_prop = to_camel(field.descr_item) if field.descr_item else None
            param_name = PlsqlSemanticConverter.infer_param_name(field, where_clause)
            set_descr = f"            header.set{descr_prop[0].upper()+descr_prop[1:]}(descr);\n" if descr_prop else ""
            clear_descr = f"            header.set{descr_prop[0].upper()+descr_prop[1:]}(null);\n" if descr_prop else ""
            return (
                f"        if (header.get{prop[0].upper()+prop[1:]}() == null || "
                f"header.get{prop[0].upper()+prop[1:]}().toString().isBlank()) {{\n"
                f"{clear_descr}"
                "            return true;\n"
                "        }\n\n"
                "        try {\n"
                "            String q = \"\"\"\n"
                f"                    select {select_expr}\n"
                f"                    from {from_clause}\n"
                f"                    where {where_clause}\n"
                "                    \"\"\";\n"
                "            String descr = (String) em.createNativeQuery(q)\n"
                f"                    .setParameter(\"{param_name}\", header.get{prop[0].upper()+prop[1:]}())\n"
                "                    .getSingleResult();\n"
                f"{set_descr}"
                "            return true;\n"
                "        } catch (Exception e) {\n"
                f"{clear_descr}"
                "            return false;\n"
                "        }"
            )
        return ControlFlowConverter.build_comment_block(body) + "\n        return true;"

    @staticmethod
    def lov_update_java(field: FieldDefinition) -> str:
        return ControlFlowConverter.build_comment_block(field.lov_body or "")

    @staticmethod
    def button_comment(button: ButtonDefinition, routine_bodies: Dict[str, str] | None = None) -> str:
        code = button.code_body or ""
        stripped = [x.strip() for x in code.splitlines() if x.strip()]
        target_code = code
        target_name = "When-Button-Pressed"
        if len(stripped) == 1 and stripped[0].upper() not in {"NULL;", "RETURN;"}:
            m = re.search(r"\b([A-Z][A-Z0-9_]+)\s*\(", stripped[0], flags=re.I)
            if m and routine_bodies:
                target_name = m.group(1)
                target_code = routine_bodies.get(target_name.upper(), code)
        m = re.search(r"call_form\s*\(\s*GET_PATH\('([^']+)'\)", target_code, flags=re.I)
        if m:
            return f"        //TODO Implement the call to Oracle Form {m.group(1)}"
        if target_name != "When-Button-Pressed":
            return f"        //TODO No Oracle Form, Implement the code of {target_name}"
        return "        //TODO No Oracle Form, Implement the code of When-Button-Pressed"


class CodeGenerator:
    def __init__(self, type_catalog: Optional[PackageTypeCatalog] = None):
        self.type_catalog = type_catalog

    def _strip_plsql_comments(self, text: str) -> str:
        text = re.sub(r'/\*.*?\*/', ' ', text or '', flags=re.S)
        text = re.sub(r'--.*?(?=\n|$)', ' ', text)
        return text

    def generate_all(self, model: ScreenModel) -> Dict[str, str]:
        files: Dict[str, str] = {}
        files.update(self._generate_record_dtos(model))
        files.update(self._generate_block_models(model))
        files.update(self._generate_wrapper_dtos(model))
        files.update(self._generate_option_enums(model))
        if model.create_qry_new_appl:
            files[model.prefix_class + "QryNewApplDto.java"] = self._generate_qry_new_appl_dto(model)
        files[model.screen_name + "ProceduresEnum.java"] = self._generate_procedures_enum(model)
        if model.validation_fields:
            files[model.screen_name + "ValidationService.java"] = self._generate_validation_service(model)
        files[model.screen_name + "Service.java"] = self._generate_service(model)
        files[model.screen_name + "ManagementController.java"] = self._generate_controller(model)
        files[model.xhtml_name] = self._generate_xhtml(model)
        files["UIResourceBundle_el.properties"] = self._generate_properties(model)
        files[model.package_name + "_procedures.sql"] = self._generate_procedures_sql(model)
        return files

    def _field_definition(self, model: ScreenModel, block_name: str, item_name: str) -> Optional[FieldDefinition]:
        for block in model.report_blocks:
            if block.block_name == block_name and item_name.upper() in block.fields:
                return block.fields[item_name.upper()]
        return None

    def _render_block_model_class(self, model: ScreenModel, binding: BlockBindingInfo) -> str:
        imports = {"import lombok.Data;", "import lombok.NoArgsConstructor;", "import lombok.AllArgsConstructor;", "import java.io.Serializable;"}
        extends_clause = f" extends {binding.dto_class}" if binding.kind == "extended" and binding.dto_class else ""
        field_lines: List[str] = []
        helper_methods: List[str] = []
        fields_to_render: List[Tuple[str, str, Optional[FieldDefinition]]] = []
        if binding.kind == "scalar":
            for item_name in binding.field_names:
                fd = self._field_definition(model, binding.block_name, item_name)
                oracle_type = self._oracle_type_for_field(model, item_name, binding.block_name)
                fields_to_render.append((item_name, oracle_type, fd))
        else:
            for item_name in binding.extra_field_names:
                fd = self._field_definition(model, binding.block_name, item_name)
                oracle_type = self._oracle_type_for_field(model, item_name, binding.block_name)
                fields_to_render.append((item_name, oracle_type, fd))
        if any(java_type_from_plsql(t, n, self.type_catalog, schema_name=model.schema_name) == "BigDecimal" for n,t,_ in fields_to_render):
            imports.add("import java.math.BigDecimal;")
        if any(java_type_from_plsql(t, n, self.type_catalog, schema_name=model.schema_name) == "Date" for n,t,_ in fields_to_render):
            imports.add("import java.util.Date;")
        if any(should_generate_boolean_accessors(n, t, fd, self.type_catalog, schema_name=model.schema_name) for n,t,fd in fields_to_render):
            imports.add("import java.util.Objects;")
        for item_name, oracle_type, fd in fields_to_render:
            jt = java_type_from_plsql(oracle_type, item_name, self.type_catalog, schema_name=model.schema_name)
            prop = to_camel(item_name)
            cap = prop[:1].upper() + prop[1:]
            field_lines.append(f"    private {jt} {prop};")
            default_value = dynamic_default_for_field(fd)
            if default_value is not None and not should_generate_boolean_accessors(item_name, oracle_type, fd, self.type_catalog, schema_name=model.schema_name):
                helper_methods.append(
                    f"    public {jt} get{cap}() {{\n"
                    f"        return {prop} == null ? {java_literal_for_value(jt, default_value)} : {prop};\n"
                    f"    }}"
                )
            if should_generate_boolean_accessors(item_name, oracle_type, fd, self.type_catalog, schema_name=model.schema_name):
                cfg = checkbox_config_for_field(item_name, fd)
                getter_literal = java_literal_for_value(jt, cfg.initialize_value)
                checked_literal = java_literal_for_value(jt, cfg.checked_value)
                unchecked_literal = java_literal_for_value(jt, cfg.unchecked_value)
                compare_expr = java_compare_expression(jt, f"get{cap}()", cfg.checked_value)
                helper_methods.append(
                    f"    public {jt} get{cap}() {{\n"
                    f"        return {prop} == null ? {getter_literal} : {prop};\n"
                    f"    }}"
                )
                helper_methods.append(f"    public void set{cap}Boolean(boolean value) {{\n        this.{prop} = value ? {checked_literal} : {unchecked_literal};\n    }}")
                helper_methods.append(f"    public boolean get{cap}Boolean() {{\n        return {compare_expr};\n    }}")
        return f"""import java.io.Serializable;
{chr(10).join(sorted(x for x in imports if x != 'import java.io.Serializable;'))}

@Data
@NoArgsConstructor
@AllArgsConstructor
public class {binding.class_name}{extends_clause} implements Serializable {{

{chr(10).join(field_lines)}

{chr(10).join(self._block_model_constructor_lines(model, binding))}

{chr(10).join(helper_methods)}
}}
"""

    def _block_model_constructor_lines(self, model: ScreenModel, binding: BlockBindingInfo) -> List[str]:
        if not binding.dto_class:
            return []
        source_fields = set()
        actual_rec = model.records.get(binding.record_name) if binding.record_name else None
        if actual_rec is not None:
            source_fields = {f.name.upper() for f in actual_rec.fields}
        overlapping = [item for item in binding.field_names if not source_fields or item.upper() in source_fields]
        if not overlapping:
            return []
        lines = [f"    public {binding.class_name}({binding.dto_class} source) {{", "        if (source != null) {"]
        for item in overlapping:
            cap = to_camel(item)[:1].upper() + to_camel(item)[1:]
            lines.append(f"        this.set{cap}(source.get{cap}());")
        lines.extend(["        }", "    }"])
        return lines

    def _extract_list_element_type(self, java_type: str) -> str:
        m = re.match(r"^List<\s*([^>]+?)\s*>$", (java_type or '').strip())
        return m.group(1).strip() if m else "Object"

    def _record_mapping_type_for_param(self, model: ScreenModel, p: SqlParam) -> str:
        return self._java_type_for_param(model, p)

    def _table_element_mapping_type_for_param(self, model: ScreenModel, p: SqlParam) -> str:
        return self._extract_list_element_type(self._java_type_for_param(model, p))

    def _find_report_routine_body(self, raw_text: str, routine_name: str) -> str:
        if not raw_text or not routine_name:
            return ""
        text = raw_text
        pattern = re.compile(rf"\b(PROCEDURE|FUNCTION)\s+{re.escape(routine_name)}\b", flags=re.I)
        m = pattern.search(text)
        if not m:
            return ""
        start = m.start()
        tail = text[start:]
        named_end_pat = re.compile(rf"\bEND\s+{re.escape(routine_name)}\s*;", flags=re.I)
        generic_end_pat = re.compile(r"\bEND\s*;", flags=re.I)
        next_proc = re.compile(r"\b(PROCEDURE|FUNCTION)\s+[A-Z0-9_]+\b", flags=re.I)
        candidates = []
        em = named_end_pat.search(tail)
        if em:
            candidates.append(em.end())
        gm = generic_end_pat.search(tail)
        if gm:
            candidates.append(gm.end())
        nm = next_proc.search(tail, m.end() - m.start())
        if nm:
            candidates.append(nm.start())
        if candidates:
            return tail[:min(candidates)].strip()
        return tail.strip()

    def _assign_keys_comment_lines(self, model: ScreenModel) -> List[str]:
        body = model.routine_bodies.get("ASSIGN_KEYS") or self._find_report_routine_body(model.raw_report_text or "", "ASSIGN_KEYS")
        if not body.strip():
            return []
        return ["        /*", "         * Original PL/SQL of PROCEDURE ASSIGN_KEYS from report.html:"] + [f"         * {line}" for line in body.splitlines()] + ["         */"]


    def _assign_keys_source_classes(self, model: ScreenModel, target_record: SqlRecord) -> List[Tuple[str, List[str]]]:
        body = model.routine_bodies.get("ASSIGN_KEYS") or self._find_report_routine_body(model.raw_report_text or "", "ASSIGN_KEYS")
        if not body.strip():
            return []
        target_fields = {f.name.upper() for f in target_record.fields}
        fields_by_block: Dict[str, List[str]] = {}
        clean_body = self._strip_plsql_comments(body)
        patt = re.compile(r"\b[A-Z0-9_]+(?:\.[A-Z0-9_]+)?\s*:=\s*:([A-Z0-9_]+)\.([A-Z0-9_]+)", flags=re.I)
        for match in patt.finditer(clean_body):
            block_name = match.group(1).upper()
            source_field = match.group(2).upper()
            if source_field in target_fields:
                fields_by_block.setdefault(block_name, []).append(source_field)
        sources: List[Tuple[str, List[str]]] = []
        for binding in self._block_bindings(model).values():
            used_fields = fields_by_block.get(binding.block_name.upper())
            if not used_fields:
                continue
            ordered: List[str] = []
            seen = set()
            for fld in target_record.fields:
                if fld.name.upper() in used_fields and fld.name.upper() not in seen:
                    seen.add(fld.name.upper())
                    ordered.append(fld.name)
            if ordered:
                sources.append((binding.class_name, ordered))
        uniq: List[Tuple[str, List[str]]] = []
        seen = set()
        for src_class, fields in sources:
            key = (src_class, tuple(fields))
            if src_class and key not in seen:
                seen.add(key)
                uniq.append((src_class, fields))
        return uniq

    def _record_ctor_needed(self, model: ScreenModel, source_class: str, target_class: str) -> bool:
        if not source_class or not target_class or source_class == target_class:
            return False
        for binding in self._block_bindings(model).values():
            if binding.class_name == source_class and binding.dto_class == target_class:
                return True
        for routine in model.routines.values():
            if routine.package_name != model.package_name or not self._routine_needs_wrapper(model, routine):
                continue
            for p in routine.params:
                rec = resolve_record(model.records, model.package_name, p.plsql_type)
                if rec is not None:
                    param_class = self._record_mapping_type_for_param(model, p)
                    actual_rec = model.records.get(self._actual_record_name(model, rec.name), rec)
                    if param_class == target_class:
                        for binding in self._block_bindings(model).values():
                            if binding.class_name == source_class and binding.record_name == actual_rec.name and param_class != binding.class_name:
                                return True
                tab = resolve_table(model.tables, model.package_name, p.plsql_type)
                if tab is not None:
                    elem_class = self._table_element_mapping_type_for_param(model, p)
                    table_rec = model.records.get(self._actual_record_name(model, tab.element_type), model.records.get(tab.element_type))
                    if elem_class == target_class and table_rec is not None:
                        for binding in self._block_bindings(model).values():
                            if binding.class_name == source_class and binding.is_table and binding.record_name == table_rec.name and elem_class != binding.class_name:
                                return True
        return False

    def _generate_block_models(self, model: ScreenModel) -> Dict[str, str]:
        files: Dict[str, str] = {}
        for binding in self._block_bindings(model).values():
            if binding.kind in {"scalar", "extended"} and binding.class_name:
                files[binding.class_name + ".java"] = self._render_block_model_class(model, binding)
        return files

    def _header_dto_name(self, model: ScreenModel) -> str:
        if model.header_record_name:
            return model.prefix_class + to_pascal(model.header_record_name) + "Dto"
        return model.prefix_class + "RecHeaderDto"

    def _dto_class_for_record_name(self, model: ScreenModel, rec_name: str) -> str:
        rec = model.records[rec_name]
        return package_prefix_class(rec.package_name) + to_pascal(rec.name) + "Dto"

    def _dto_class_for_record_type(self, model: ScreenModel, plsql_type: str) -> str:
        rec = resolve_record(model.records, model.package_name, plsql_type)
        if rec is None:
            return model.prefix_class + "UnknownDto"
        actual_name = self._actual_record_name(model, rec.name)
        actual_rec = model.records.get(actual_name, rec)
        return package_prefix_class(actual_rec.package_name) + to_pascal(actual_rec.name) + "Dto"

    def _dto_class_for_table_type(self, model: ScreenModel, plsql_type: str) -> str:
        tab = resolve_table(model.tables, model.package_name, plsql_type)
        if tab is None:
            return "List<Object>"
        rec_name = self._actual_record_name(model, tab.element_type)
        rec = model.records[rec_name]
        return f"List<{package_prefix_class(rec.package_name) + to_pascal(rec.name) + 'Dto'}>"

    def _declared_dto_class_for_record_type(self, model: ScreenModel, plsql_type: str) -> str:
        rec = resolve_record(model.records, model.package_name, plsql_type)
        if rec is None:
            return model.prefix_class + "UnknownDto"
        return package_prefix_class(rec.package_name) + to_pascal(rec.name) + "Dto"

    def _declared_table_element_dto_class(self, model: ScreenModel, plsql_type: str) -> str:
        tab = resolve_table(model.tables, model.package_name, plsql_type)
        if tab is None:
            return "Object"
        rec = model.records.get(tab.element_type)
        if rec is None:
            return "Object"
        return package_prefix_class(rec.package_name) + to_pascal(rec.name) + "Dto"

    def _scalar_sql_type(self, plsql_type: str, schema_name: Optional[str] = None) -> str:
        u = resolve_actual_plsql_type(plsql_type, self.type_catalog, schema_name=schema_name).upper()
        if "DATE" in u:
            return "Types.DATE"
        if any(x in u for x in ("NUMBER", "INTEGER", "BINARY_INTEGER", "SID", "SEQ_NO", "YEAR", "MONTH", "AMOUNT", "AMNT", "PRCNT")):
            return "Types.NUMERIC"
        return "Types.VARCHAR"

    def _has_wrapper(self, model: ScreenModel, name: str) -> bool:
        for routine in model.routines.values():
            if routine.package_name == model.package_name and routine.name.upper() == name and self._routine_needs_wrapper(model, routine):
                return True
        return False

    def _db_object_name_for_record(self, model: ScreenModel, rec_name: Optional[str]) -> str:
        if not rec_name or rec_name not in model.records:
            return f"{model.prefix_upper}_REC_HEADER"
        rec = model.records[rec_name]
        return f"{package_prefix(rec.package_name)}_{rec.name}"

    def _db_object_name_for_table(self, model: ScreenModel, table_name: str) -> str:
        tab = model.tables[table_name]
        return f"{package_prefix(tab.package_name)}_{tab.name}"

    def _routine_called_in_report(self, model: ScreenModel, routine_name: str) -> bool:
        if routine_name.upper() in model.procedure_calls:
            return True
        if model.package_name and model.raw_report_text:
            clean_report = self._strip_plsql_comments(model.raw_report_text)
            patt = re.compile(rf"\b{re.escape(model.package_name)}\s*\.\s*{re.escape(routine_name)}\s*\(", flags=re.I)
            if patt.search(clean_report):
                return True
            fallback = re.compile(rf"\b{re.escape(routine_name)}\s*\(", flags=re.I)
            return bool(fallback.search(clean_report))
        return False

    def _actual_record_name(self, model: ScreenModel, rec_name: str) -> str:
        rec = model.records.get(rec_name)
        if rec is None:
            return rec_name
        if rec.name.endswith("_INSERT"):
            base_name = normalize_record_alias_name(rec.name)
            base_rec = model.records.get(base_name)
            if base_rec and field_subset_of(rec, base_rec):
                return base_rec.name
        return rec.name

    def _java_type_for_param(self, model: ScreenModel, p: SqlParam) -> str:
        if p.name.upper() == "P_ERRNO":
            return "Integer"
        if "%TYPE" in p.plsql_type.upper():
            return java_type_from_plsql(p.plsql_type, p.name, self.type_catalog, schema_name=model.schema_name)
        rec = resolve_record(model.records, model.package_name, p.plsql_type)
        if rec is not None:
            actual_name = self._actual_record_name(model, rec.name)
            actual_rec = model.records.get(actual_name, rec)
            return package_prefix_class(actual_rec.package_name) + to_pascal(actual_rec.name) + "Dto"
        tab = resolve_table(model.tables, model.package_name, p.plsql_type)
        if tab is not None:
            rec_name = self._actual_record_name(model, tab.element_type)
            rec = model.records[rec_name]
            return f"List<{package_prefix_class(rec.package_name) + to_pascal(rec.name) + 'Dto'}>"
        return java_type_from_plsql(p.plsql_type, p.name, self.type_catalog, schema_name=model.schema_name)

    def _routine_needs_wrapper(self, model: ScreenModel, routine: SqlRoutine) -> bool:
        if self._routine_called_in_report(model, routine.name):
            return True
        return any("%TYPE" not in p.plsql_type.upper() and (resolve_record(model.records, model.package_name, p.plsql_type) is not None or resolve_table(model.tables, model.package_name, p.plsql_type) is not None) for p in routine.params)

    def _field_by_name(self, model: ScreenModel, name: str) -> Optional[FieldDefinition]:
        for fd in model.fields:
            if fd.item_name == name:
                return fd
        return None

    def _field_def_for_record_field(self, model: ScreenModel, record_name: str, field_name: str) -> Optional[FieldDefinition]:
        if record_name == model.header_record_name:
            return self._field_by_name(model, field_name)
        return None

    def _compatible_source_classes_for_record(self, model: ScreenModel, target_record: SqlRecord) -> List[Tuple[str, List[str]]]:
        sources: List[Tuple[str, List[str]]] = []
        target_fields = {f.name.upper() for f in target_record.fields}
        if not target_fields:
            return sources

        target_actual_name = self._actual_record_name(model, target_record.name)

        for binding in self._block_bindings(model).values():
            available_fields = {f.upper() for f in binding.field_names}
            if target_fields.issubset(available_fields):
                sources.append((binding.class_name, [f.name for f in target_record.fields]))

        generated_record_names = {self._actual_record_name(model, name) for name in model.referenced_records if name in model.records}
        for rec in model.records.values():
            actual_source_name = self._actual_record_name(model, rec.name)
            if actual_source_name not in generated_record_names:
                continue
            if actual_source_name == target_actual_name:
                continue
            actual_source = model.records.get(actual_source_name, rec)
            if actual_source.name.endswith("_KEYS"):
                continue
            rec_fields = {f.name.upper() for f in actual_source.fields}
            if target_fields.issubset(rec_fields):
                src_class = package_prefix_class(actual_source.package_name) + to_pascal(actual_source.name) + "Dto"
                sources.append((src_class, [f.name for f in target_record.fields]))
            elif rec_fields.issubset(target_fields):
                src_class = package_prefix_class(actual_source.package_name) + to_pascal(actual_source.name) + "Dto"
                overlapping = [f.name for f in target_record.fields if f.name.upper() in rec_fields]
                if overlapping:
                    sources.append((src_class, overlapping))

        uniq: List[Tuple[str, List[str]]] = []
        seen = set()
        for src_class, fields in sources:
            key = (src_class, tuple(fields))
            if src_class and key not in seen:
                seen.add(key)
                uniq.append((src_class, fields))
        return uniq

    def _generate_record_dtos(self, model: ScreenModel) -> Dict[str, str]:
        files: Dict[str, str] = {}
        for rec_name in model.referenced_records:
            if rec_name not in model.records:
                continue
            record = model.records[rec_name]
            class_name = self._dto_class_for_record_name(model, rec_name)
            imports = {"import lombok.Data;", "import lombok.NoArgsConstructor;", "import lombok.AllArgsConstructor;", "import java.io.Serializable;"}
            field_defs_map = {fld.name.upper(): self._field_def_for_record_field(model, record.name, fld.name) for fld in record.fields}
            boolean_fields = [
                fld for fld in record.fields
                if should_generate_boolean_accessors(fld.name, fld.plsql_type, field_defs_map.get(fld.name.upper()), self.type_catalog, schema_name=record.schema_name)
            ]
            if boolean_fields:
                imports.add("import java.util.Objects;")
            if any(java_type_from_plsql(fld.plsql_type, fld.name, self.type_catalog, schema_name=record.schema_name) == "BigDecimal" for fld in record.fields):
                imports.add("import java.math.BigDecimal;")
            if any(java_type_from_plsql(fld.plsql_type, fld.name, self.type_catalog, schema_name=record.schema_name) == "Date" for fld in record.fields):
                imports.add("import java.util.Date;")
            if any(fld.name.upper() == "LAST_UPDATED_DATE" for fld in record.fields):
                imports.add("import java.text.SimpleDateFormat;")

            extends_clause = ""
            fields: List[str] = []
            helper_methods: List[str] = []
            constructors: List[str] = []

            is_insert_alias = record.name.endswith("_INSERT")
            is_ins_alias = record.name.endswith("_INS")
            if is_insert_alias:
                base_name = normalize_record_alias_name(record.name)
                base_record = model.records.get(base_name)
                if base_record and field_subset_of(record, base_record):
                    imports.add("import lombok.EqualsAndHashCode;")
                    base_class = package_prefix_class(base_record.package_name) + to_pascal(base_record.name) + "Dto"
                    extends_clause = f" extends {base_class}"
                    ctor_lines = [
                        f"    public {class_name}({base_class} source) {{",
                        "        if (source != null) {"
                    ]
                    for fld in record.fields:
                        prop = to_camel(fld.name)
                        cap = prop[:1].upper() + prop[1:]
                        ctor_lines.append(f"            this.set{cap}(source.get{cap}());")
                    ctor_lines.append("        }")
                    ctor_lines.append("    }")
                    constructors.append("\n".join(ctor_lines))
            elif is_ins_alias:
                base_name = normalize_record_alias_name(record.name)
                base_record = model.records.get(base_name)
                if base_record and field_subset_of(record, base_record):
                    base_class = package_prefix_class(base_record.package_name) + to_pascal(base_record.name) + "Dto"
                    ctor_lines = [
                        f"    public {class_name}({base_class} source) {{",
                        "        if (source != null) {"
                    ]
                    for fld in record.fields:
                        prop = to_camel(fld.name)
                        cap = prop[:1].upper() + prop[1:]
                        ctor_lines.append(f"            this.set{cap}(source.get{cap}());")
                    ctor_lines.append("        }")
                    ctor_lines.append("    }")
                    constructors.append("\n".join(ctor_lines))

            if not extends_clause:
                for fld in record.fields:
                    fd = field_defs_map.get(fld.name.upper())
                    jt = java_type_from_plsql(fld.plsql_type, fld.name, self.type_catalog, schema_name=record.schema_name)
                    prop = to_camel(fld.name)
                    cap = prop[0].upper() + prop[1:]
                    fields.append(f"    private {jt} {prop};")

                    default_value = dynamic_default_for_field(fd)
                    if default_value is not None and not should_generate_boolean_accessors(fld.name, fld.plsql_type, fd, self.type_catalog, schema_name=record.schema_name):
                        helper_methods.append(
                            f"    public {jt} get{cap}() {{\n"
                            f"        return {prop} == null ? {java_literal_for_value(jt, default_value)} : {prop};\n"
                            f"    }}"
                        )

                    if should_generate_boolean_accessors(fld.name, fld.plsql_type, fd, self.type_catalog, schema_name=record.schema_name):
                        cfg = checkbox_config_for_field(fld.name, fd)
                        getter_literal = java_literal_for_value(jt, cfg.initialize_value)
                        checked_literal = java_literal_for_value(jt, cfg.checked_value)
                        unchecked_literal = java_literal_for_value(jt, cfg.unchecked_value)
                        compare_expr = java_compare_expression(jt, f"get{cap}()", cfg.checked_value)
                        helper_methods.append(
                            f"    public {jt} get{cap}() {{\n"
                            f"        return {prop} == null ? {getter_literal} : {prop};\n"
                            f"    }}"
                        )
                        helper_methods.extend([
                            f"    public void set{cap}Boolean(boolean value) {{\n"
                            f"        this.{prop} = value ? {checked_literal} : {unchecked_literal};\n"
                            f"    }}",
                            f"    public boolean get{cap}Boolean() {{\n"
                            f"        return {compare_expr};\n"
                            f"    }}",
                        ])

            assign_keys_comment_lines: List[str] = []
            if record.name.endswith("_KEYS"):
                assign_keys_comment_lines = self._assign_keys_comment_lines(model)
                ctor_sources = self._assign_keys_source_classes(model, record)
            else:
                ctor_sources = self._compatible_source_classes_for_record(model, record)

            for src_class, ctor_fields in ctor_sources:
                if src_class == class_name:
                    continue
                if not record.name.endswith("_KEYS") and not self._record_ctor_needed(model, src_class, class_name):
                    continue
                lines = [f"    public {class_name}({src_class} source) {{"]
                if assign_keys_comment_lines:
                    lines.extend(assign_keys_comment_lines)
                lines.append("        if (source != null) {")
                for fld_name in ctor_fields:
                    prop = to_camel(fld_name)
                    cap = prop[0].upper() + prop[1:]
                    lines.append(f"            this.set{cap}(source.get{cap}());")
                lines.append("        }")
                lines.append("    }")
                ctor_text = "\n".join(lines)
                if ctor_text not in constructors:
                    constructors.append(ctor_text)

            if any(f.name.upper() == "LAST_UPDATED_DATE" for f in record.fields) and not extends_clause:
                helper_methods.append("""
    private String formatDateVariableWithTime(Date variable) {
        if (variable == null) {
            return "";
        }
        SimpleDateFormat sdf = new SimpleDateFormat("dd/MM/yyyy HH:mm");
        return sdf.format(variable);
    }

    public String getLastUpdatedDateAndTime() {
        return lastUpdatedDate == null ? null : formatDateVariableWithTime(lastUpdatedDate);
    }
""")

            annotations = "@Data\n@NoArgsConstructor\n@AllArgsConstructor"
            if extends_clause:
                annotations += "\n@EqualsAndHashCode(callSuper = true)"

            files[class_name + ".java"] = "\n".join(sorted(imports)) + f"""

{annotations}
public class {class_name}{extends_clause} implements Serializable {{
{chr(10).join(fields)}

{chr(10).join(constructors)}

{chr(10).join(helper_methods)}
}}
"""
        return files

    def _generate_wrapper_dtos(self, model: ScreenModel) -> Dict[str, str]:
        files = {}
        for routine in sorted(model.routines.values(), key=lambda x: x.name):
            if routine.package_name != model.package_name or not self._routine_needs_wrapper(model, routine):
                continue
            class_name = model.prefix_class + to_pascal(routine.name) + "WrpDto"
            imports = {"import lombok.Data;", "import lombok.NoArgsConstructor;", "import lombok.AllArgsConstructor;", "import java.io.Serializable;"}
            fields = []
            ctor_params = []
            for p in routine.params:
                jt = self._java_type_for_param(model, p)
                if jt == "Date":
                    imports.add("import java.util.Date;")
                elif jt == "BigDecimal":
                    imports.add("import java.math.BigDecimal;")
                elif jt.startswith("List<"):
                    imports.add("import java.util.List;")
                fields.append(f"    private {jt} {to_camel(p.name)};")
                if p.mode in {"OUT", "IN OUT"} and p.name.upper() not in {"P_ERRNO", "P_ERRTXT"}:
                    ctor_params.append((jt, to_camel(p.name)))
            ctor = ""
            if ctor_params:
                args = ", ".join(f"{t} {n}" for t, n in ctor_params)
                body = "\n".join(f"        this.{n} = {n};" for _t, n in ctor_params)
                ctor = f"""

    public {class_name}({args}) {{
{body}
    }}
"""
            files[class_name + ".java"] = "\n".join(sorted(imports)) + f"""

@Data
@NoArgsConstructor
@AllArgsConstructor
public class {class_name} implements Serializable {{
{chr(10).join(fields)}{ctor}
}}
"""
        return files

    def _generate_option_enums(self, model: ScreenModel) -> Dict[str, str]:
        files: Dict[str, str] = {}
        for field_name in model.option_enum_fields:
            fd = self._field_by_name(model, field_name)
            if fd is None or not fd.option_enum_values:
                continue
            class_name = enum_class_name(model, field_name)
            has_disabled = any(v.description == "Οριστικοποιημένη" for v in fd.option_enum_values)
            value_lines = []
            for idx, val in enumerate(fd.option_enum_values):
                enum_const = re.sub(r"[^A-Z0-9_]+", "_", (val.description or f"OPT_{val.id}").upper())
                enum_const = re.sub(r"_+", "_", enum_const).strip("_") or f"OPTION_{idx + 1}"
                if enum_const[0].isdigit():
                    enum_const = "OPTION_" + enum_const
                args = [f'"{clean_java_string_literal(val.id)}"', f'"{clean_java_string_literal(val.description)}"']
                if has_disabled:
                    args.append("true" if val.description == "Οριστικοποιημένη" else "false")
                value_lines.append(f"    {enum_const}({', '.join(args)})")
            extra_field = "\n    private final boolean disabled;" if has_disabled else ""
            values_block = ",\n".join(value_lines)
            files[class_name + ".java"] = f"""import lombok.Getter;
import lombok.RequiredArgsConstructor;

@Getter
@RequiredArgsConstructor
public enum {class_name} {{
{values_block};

    private final String id;
    private final String description;{extra_field}
}}
"""
        return files
    def _generate_qry_new_appl_dto(self, model: ScreenModel) -> str:
        return f"""import lombok.Data;
import lombok.NoArgsConstructor;
import lombok.AllArgsConstructor;
import java.io.Serializable;

@Data
@NoArgsConstructor
@AllArgsConstructor
public class {model.prefix_class}QryNewApplDto implements Serializable {{
    private {self._header_dto_name(model)} header;
    private Integer pErrno;
    private String pErrtxt;
}}
"""

    def _generate_procedure_param_list(self, model: ScreenModel, routine: SqlRoutine) -> str:
        lines = []
        wrapper_class = model.prefix_class + to_pascal(routine.name) + "WrpDto"
        for idx, p in enumerate(routine.params, start=1):
            direction = "ParamDirection.INOUT" if p.mode == "IN OUT" else ("ParamDirection.OUT" if p.mode == "OUT" else "ParamDirection.IN")
            getter = f"((%s) dto).get%s()" % (wrapper_class, to_camel(p.name)[0].upper() + to_camel(p.name)[1:])
            if "%TYPE" in p.plsql_type.upper() or all(tok not in p.plsql_type.upper() for tok in (".REC_", ".TAB_")):
                rec = resolve_record(model.records, model.package_name, p.plsql_type)
                tab = resolve_table(model.tables, model.package_name, p.plsql_type)
                if rec is None and tab is None:
                    lines.append(f"""                ProcedureParam.createScalarParam({idx}, {direction}, {self._scalar_sql_type(p.plsql_type, schema_name=routine.schema_name)},
                        dto -> {getter})""")
                    continue
            rec = resolve_record(model.records, model.package_name, p.plsql_type)
            tab = resolve_table(model.tables, model.package_name, p.plsql_type)
            if rec is not None:
                actual = normalize_record_alias_name(rec.name) if "INSERT" in rec.name else rec.name
                actual_rec = model.records.get(actual, rec)
                lines.append(f"""                ProcedureParam.createStructParam({idx}, {direction},
                        "{self._db_object_name_for_record(model, actual_rec.name)}",
                        {package_prefix_class(actual_rec.package_name) + to_pascal(actual_rec.name) + "Dto"}.class, dto -> {getter})""")
            elif tab is not None:
                element = tab.element_type
                actual_element = self._actual_record_name(model, element)
                rec_el = model.records[actual_element]
                lines.append(f"""                ProcedureParam.createArrayParam({idx}, {direction},
                        "{self._db_object_name_for_record(model, actual_element)}",
                        "{self._db_object_name_for_table(model, tab.name)}",
                        "{self._db_object_name_for_table(model, tab.name)}",
                        {package_prefix_class(rec_el.package_name) + to_pascal(rec_el.name) + "Dto"}.class, dto -> {getter})""")
            else:
                lines.append(f"""                ProcedureParam.createScalarParam({idx}, {direction}, {self._scalar_sql_type(p.plsql_type, schema_name=routine.schema_name)},
                        dto -> {getter})""")
        return ",\n".join(lines)

    def _generate_qry_new_appl_param_list(self, model: ScreenModel) -> str:
        header_dto = self._header_dto_name(model)
        return f"""                ProcedureParam.createStructParam(1, ParamDirection.INOUT,
                        "{self._db_object_name_for_record(model, model.header_record_name)}",
                        {header_dto}.class, dto -> (({model.prefix_class}QryNewApplDto) dto).getHeader()),
                ProcedureParam.createScalarParam(2, ParamDirection.OUT, Types.INTEGER,
                        dto -> (({model.prefix_class}QryNewApplDto) dto).getPErrno()),
                ProcedureParam.createScalarParam(3, ParamDirection.OUT, Types.VARCHAR,
                        dto -> (({model.prefix_class}QryNewApplDto) dto).getPErrtxt())"""

    def _generate_procedures_enum(self, model: ScreenModel) -> str:
        entries: List[str] = []
        for routine in sorted(model.routines.values(), key=lambda x: x.name):
            if routine.package_name != model.package_name:
                continue
            if not self._routine_needs_wrapper(model, routine):
                continue
            enum_name = f"{model.prefix_upper}_{routine.name}"
            proc_name = f"{model.package_name}.{routine.name}_WRP"
            entries.append(f"""{enum_name}("{proc_name}",
            List.of(
{self._generate_procedure_param_list(model, routine)}
            )
    )""")
        if model.create_qry_new_appl:
            entries.append(f"""{model.prefix_upper}_QRY_NEW_APPL("{model.package_name}.QRY_NEW_APPL",
            List.of(
{self._generate_qry_new_appl_param_list(model)}
            )
    )""")
        return f"""import java.sql.Types;
import java.util.List;

public enum {model.screen_name}ProceduresEnum {{
{",\n".join(entries)};

    private final String procedureName;
    private final List<ProcedureParam> params;

    {model.screen_name}ProceduresEnum(String procedureName, List<ProcedureParam> params) {{
        this.procedureName = procedureName;
        this.params = params;
    }}

    public String getProcedureName() {{
        return procedureName;
    }}

    public List<ProcedureParam> getParams() {{
        return params;
    }}
}}
"""

    def _generate_validation_service(self, model: ScreenModel) -> str:
        header_binding = next((b for b in self._block_bindings(model).values() if b.record_name == model.header_record_name and not b.is_table), None)
        header_dto = self._header_dto_name(model)
        header_param_type = header_binding.class_name if header_binding is not None else header_dto
        cases = []
        methods = []

        for field_name in model.validation_fields:
            fd = self._field_by_name(model, field_name)
            effective_item_name = (fd.item_name if fd else field_name) if field_name else ""
            if is_dec_or_appl_related_field(effective_item_name):
                continue

            method_name = "check" + to_pascal(field_name)
            cases.append(f'            case "{field_name}" -> {method_name}(header);')
            source_name = f"CHK_{field_name}"
            if fd and fd.links_to_code:
                for link_name in fd.links_to_code:
                    if link_name and link_name.strip().lower() not in {"lov", "options", "trigger"}:
                        source_name = link_name
                        break
            raw_body = (fd.validation_body or "") if fd else ""
            comment = "        /*\n"
            comment += f"         * Validation source: {source_name}\n"
            comment += "         * Original PL/SQL from report.html:\n"
            for line in raw_body.splitlines():
                comment += f"         * {line}\n"
            comment += "         */"
            methods.append(f"""    private boolean {method_name}({header_param_type} header) {{
{comment}
        return true;
    }}
""")

        case_block = "\n".join(cases) if cases else "            default -> true;"
        return f"""public class {model.screen_name}ValidationService {{

    public boolean validateField(String fieldName, {header_param_type} header, Object detailRecord) {{
        return switch (fieldName.toUpperCase()) {{
{case_block}
            default -> true;
        }};
    }}

{chr(10).join(methods)}
}}
"""

    def _generate_service_method_qry_new_appl(self, model: ScreenModel) -> str:
        header_dto = self._header_dto_name(model)
        dto_class = f"{model.prefix_class}QryNewApplDto"
        enum_name = f"{model.screen_name}ProceduresEnum.{model.prefix_upper}_QRY_NEW_APPL"
        return f"""    public {dto_class} call{model.prefix_class}QryNewAppl({dto_class} in) {{
        Map<Integer, Object> out = callProcedure({enum_name}, in);

        int errno = (Integer) out.get(2);
        String errtxt = (String) out.get(3);
        throwExceptionIfNeeded({enum_name}.getProcedureName(), errno, errtxt);

        {header_dto} outHeader = ({header_dto}) out.get(1);
        return new {dto_class}(outHeader, null, null);
    }}
"""

    def _generate_service_method_for_routine(self, model: ScreenModel, routine: SqlRoutine) -> str:
        wrapper_class = model.prefix_class + to_pascal(routine.name) + "WrpDto"
        enum_name = f"{model.screen_name}ProceduresEnum.{model.prefix_upper}_{routine.name}"
        method_name = f"call{model.prefix_class}{to_pascal(routine.name)}"
        out_params = [p for p in routine.params if p.mode in {"OUT", "IN OUT"} and p.name.upper() not in {"P_ERRNO", "P_ERRTXT"}]
        errno_idx = len(routine.params) - 1
        errtxt_idx = len(routine.params)
        if not out_params:
            return f"""    public void {method_name}({wrapper_class} in) {{
        Map<Integer, Object> out = callProcedure({enum_name}, in);

        int errno = (Integer) out.get({errno_idx});
        String errtxt = (String) out.get({errtxt_idx});
        throwExceptionIfNeeded({enum_name}.getProcedureName(), errno, errtxt);
    }}
"""
        assigns = []
        ctor_args = []
        out_index = 1
        for p in out_params:
            jt = self._java_type_for_param(model, p)
            prop = to_camel(p.name)
            assigns.append(f"        {jt} {prop} = ({jt}) out.get({out_index});")
            ctor_args.append(prop)
            out_index += 1
        return f"""    public {wrapper_class} {method_name}({wrapper_class} in) {{
        Map<Integer, Object> out = callProcedure({enum_name}, in);

        int errno = (Integer) out.get({errno_idx});
        String errtxt = (String) out.get({errtxt_idx});
        throwExceptionIfNeeded({enum_name}.getProcedureName(), errno, errtxt);

{chr(10).join(assigns)}
        return new {wrapper_class}({", ".join(ctor_args)});
    }}
"""

    def _generate_service(self, model: ScreenModel) -> str:
        header_binding = next((b for b in self._block_bindings(model).values() if b.record_name == model.header_record_name and not b.is_table), None)
        header_dto = self._header_dto_name(model)
        header_param_type = header_binding.class_name if header_binding is not None else header_dto
        methods = []
        if model.create_qry_new_appl:
            methods.append(self._generate_service_method_qry_new_appl(model))
        for routine in sorted(model.routines.values(), key=lambda x: x.name):
            if routine.package_name != model.package_name:
                continue
            if not self._routine_needs_wrapper(model, routine):
                continue
            methods.append(self._generate_service_method_for_routine(model, routine))
        return f"""import java.util.Arrays;
import java.util.List;
import java.util.Map;
import java.util.Optional;
import java.util.Collections;
import jakarta.enterprise.context.ApplicationScoped;

@ApplicationScoped
public class {model.screen_name}Service {{

    private final {model.screen_name}ValidationService validationService = new {model.screen_name}ValidationService();

    public void validateField(String fieldName, {header_param_type} header, Object detailRecord) {{
        validationService.validateField(fieldName, header, detailRecord);
    }}

{chr(10).join(methods)}

    protected void handleAlerts(String alerts) {{
        Optional.ofNullable(alerts)
                .stream()
                .map(errors -> Arrays.asList(errors.split("@")))
                .flatMap(List::stream)
                .filter(s -> s != null && !s.isBlank())
                .forEach(System.out::println);
    }}

    protected void throwExceptionIfNeeded(String procedureName, int errno, String errtxt) {{
        if (errno != 0) {{
            if (errtxt != null && errtxt.contains("ORA-")) {{
                throw new RuntimeException("SSPTechnicalException");
            }} else {{
                throw new RuntimeException(errtxt);
            }}
        }}
    }}

    protected Map<Integer, Object> callProcedure(Enum<?> procedureEnum, Object dto) {{
        return Collections.emptyMap();
    }}
}}
"""

    def _clean_routine_body(self, body: str) -> str:
        return strip_plsql_comments(body or "")

    def _block_assignment_sources(self, model: ScreenModel) -> Dict[str, Dict[str, set]]:
        result: Dict[str, Dict[str, set]] = {block.block_name: {} for block in model.report_blocks}
        bodies = list(model.routine_bodies.values())
        if model.raw_report_text:
            bodies.append(model.raw_report_text)
        for block in model.report_blocks:
            for fd in block.fields.values():
                patt = re.compile(rf":{re.escape(block.block_name)}\.{re.escape(fd.item_name)}\s*:=\s*([A-Z0-9_]+)(?:\s*\([^\)]*\))?\.([A-Z0-9_]+)\s*;", flags=re.I | re.S)
                for body in bodies:
                    clean = self._clean_routine_body(body)
                    for m in patt.finditer(clean):
                        source_var = m.group(1).upper()
                        source_field = m.group(2).upper()
                        result.setdefault(block.block_name, {}).setdefault(source_var, set()).add(source_field)
        return result

    def _table_for_record(self, model: ScreenModel, record_name: Optional[str]) -> Optional[SqlTable]:
        if not record_name:
            return None
        actual = self._actual_record_name(model, record_name)
        for tab in model.tables.values():
            if self._actual_record_name(model, tab.element_type) == actual:
                return tab
        return None

    def _binding_instance_name(self, block_name: str, rec: Optional[SqlRecord], is_table: bool) -> str:
        base = to_camel(block_name)
        if is_table:
            return base if base.endswith("Table") else base + "Table"
        return base if rec is not None else base + "Data"

    def _best_record_for_field_set(self, model: ScreenModel, field_names: set, prefer_package: Optional[str] = None) -> Optional[SqlRecord]:
        candidates: List[Tuple[int,int,int,str,SqlRecord]] = []
        target = {f.upper() for f in field_names if f}
        if not target:
            return None
        for rec in model.records.values():
            rec_fields = {f.name.upper() for f in rec.fields}
            if not target.issubset(rec_fields):
                continue
            same_pkg = 0 if prefer_package and rec.package_name == prefer_package else 1
            header_bias = 0 if rec.name == model.header_record_name else 1
            candidates.append((same_pkg, header_bias, len(rec_fields), rec.name, rec))
        if not candidates:
            return None
        candidates.sort(key=lambda x: (x[0], x[1], x[2], x[3]))
        return candidates[0][4]

    def _block_bindings(self, model: ScreenModel) -> Dict[str, BlockBindingInfo]:
        assignment_sources = self._block_assignment_sources(model)
        bindings: Dict[str, BlockBindingInfo] = {}
        for block in model.report_blocks:
            block_field_names = [fd.item_name for fd in block.fields.values() if (fd.item_type or "").strip().lower() != "push button"]
            field_set = {f.upper() for f in block_field_names}
            exact_rec = self._best_record_for_field_set(model, field_set, prefer_package=model.package_name)
            chosen_rec = exact_rec
            extra_field_names: List[str] = []
            if chosen_rec is None:
                candidates = []
                for source_var, source_fields in assignment_sources.get(block.block_name, {}).items():
                    rec = self._best_record_for_field_set(model, source_fields, prefer_package=model.package_name)
                    if rec is None:
                        continue
                    candidates.append((len(source_fields), source_var, rec))
                if candidates:
                    candidates.sort(key=lambda x: (-x[0], x[1], x[2].name))
                    chosen_rec = candidates[0][2]
                    rec_field_set = {f.name.upper() for f in chosen_rec.fields}
                    extra_field_names = [name for name in block_field_names if name.upper() not in rec_field_set]
            table = self._table_for_record(model, chosen_rec.name if chosen_rec else None)
            is_table = table is not None
            instance_name = self._binding_instance_name(block.block_name, chosen_rec, is_table)
            if chosen_rec is None:
                class_name = model.prefix_class + to_pascal(block.block_name) + "BlockData"
                bindings[block.block_name] = BlockBindingInfo(block_name=block.block_name, kind="scalar", record_name=None, dto_class=None, class_name=class_name, instance_name=instance_name, field_names=block_field_names, extra_field_names=block_field_names, table_name=None, is_table=False)
            else:
                actual_name = self._actual_record_name(model, chosen_rec.name)
                actual_rec = model.records.get(actual_name, chosen_rec)
                dto_class = package_prefix_class(actual_rec.package_name) + to_pascal(actual_rec.name) + "Dto"
                if extra_field_names:
                    class_name = model.prefix_class + to_pascal(block.block_name) + "BlockData"
                    bindings[block.block_name] = BlockBindingInfo(block_name=block.block_name, kind="extended", record_name=actual_rec.name, dto_class=dto_class, class_name=class_name, instance_name=instance_name, field_names=block_field_names, extra_field_names=extra_field_names, table_name=table.name if table else None, is_table=is_table)
                else:
                    bindings[block.block_name] = BlockBindingInfo(block_name=block.block_name, kind="dto", record_name=actual_rec.name, dto_class=dto_class, class_name=dto_class, instance_name=instance_name, field_names=block_field_names, extra_field_names=[], table_name=table.name if table else None, is_table=is_table)
        return bindings

    def _block_instance_info(self, model: ScreenModel) -> Dict[str, Dict[str, Optional[str]]]:
        bindings = self._block_bindings(model)
        info: Dict[str, Dict[str, Optional[str]]] = {}
        for block_name, binding in bindings.items():
            info[block_name] = {
                "record_name": binding.record_name,
                "dto_class": binding.dto_class,
                "class_name": binding.class_name,
                "instance_name": binding.instance_name,
                "expr": binding.expr,
                "is_header": bool(binding.record_name and binding.record_name == model.header_record_name),
                "kind": binding.kind,
                "is_table": binding.is_table,
                "table_name": binding.table_name,
            }
        return info

    def _property_key_for_field(self, fd: FieldDefinition, common_titles: Dict[Tuple[str, str], str]) -> str:
        title = normalize_spaces((fd.title_label or "").replace("&#10;", " "))
        if title and common_titles.get((fd.item_name, title)):
            return f"common.title.{fd.property_name}"
        return f"{to_camel(fd.block_name)}.title.{fd.property_name}"

    def _oracle_type_for_field(self, model: ScreenModel, item_name: str, block_name: Optional[str] = None) -> str:
        target = item_name.upper()
        bindings = self._block_bindings(model)

        def from_record(rec_name: Optional[str]) -> Optional[str]:
            if not rec_name:
                return None
            rec = model.records.get(rec_name.upper())
            if not rec:
                return None
            for fld in rec.fields:
                if fld.name.upper() == target:
                    return fld.plsql_type
            return None

        if block_name:
            binding = bindings.get(block_name.upper()) or bindings.get(block_name)
            typ = from_record(binding.record_name if binding else None)
            if typ:
                return typ
            for block in model.report_blocks:
                if block.block_name.upper() == block_name.upper():
                    fd = block.fields.get(target)
                    if fd and fd.datatype:
                        return fd.datatype
                    break

        for block in model.report_blocks:
            fd = block.fields.get(target)
            if fd and fd.datatype:
                binding = bindings.get(block.block_name)
                typ = from_record(binding.record_name if binding else None)
                if typ:
                    return typ
                return fd.datatype

        for rec in model.records.values():
            for fld in rec.fields:
                if fld.name.upper() == target:
                    return fld.plsql_type

        return "VARCHAR2"

    def _generate_controller(self, model: ScreenModel) -> str:
        header_dto = self._header_dto_name(model)
        block_instance_info = self._block_instance_info(model)
        block_bindings = self._block_bindings(model)
        header_binding = next((b for b in block_bindings.values() if b.record_name == model.header_record_name and not b.is_table), None)
        header_ref = header_binding.instance_name if header_binding is not None else "header"
        dates_dto = None
        for rec_name in model.referenced_records:
            if rec_name.endswith("REC_DATES"):
                rec = model.records[rec_name]
                dates_dto = package_prefix_class(rec.package_name) + to_pascal(rec.name) + "Dto"
                break

        option_methods = []
        for field_name in model.option_enum_fields:
            option_class = enum_class_name(model, field_name)
            controller_method = enum_controller_method_name(field_name)
            option_methods.append(f"""    public {option_class}[] {controller_method}() {{
        return {option_class}.values();
    }}
""")

        extra_list_decl = f"    private List<{dates_dto}> dates = new ArrayList<>();\n" if dates_dto else ""

        block_instance_fields = []
        block_instance_init = []
        for block_name, meta in block_instance_info.items():
            if not meta.get("instance_name"):
                continue
            class_name = meta.get("class_name") or meta.get("dto_class")
            if not class_name:
                continue
            if meta.get("is_table"):
                block_instance_fields.append(f"    private List<{class_name}> {meta['instance_name']} = new ArrayList<>();")
                block_instance_init.append(f"        {meta['instance_name']} = new ArrayList<>();")
            else:
                block_instance_fields.append(f"    private {class_name} {meta['instance_name']};")
                block_instance_init.append(f"        {meta['instance_name']} = new {class_name}();")

        scalar_fields = []
        scalar_init = []
        seen_scalar = set()
        for routine in model.routines.values():
            if routine.package_name != model.package_name or not self._routine_needs_wrapper(model, routine):
                continue
            for p in routine.params:
                rec = resolve_record(model.records, model.package_name, p.plsql_type)
                tab = resolve_table(model.tables, model.package_name, p.plsql_type)
                if rec is None and tab is None and p.name.upper() not in {"P_ERRNO", "P_ERRTXT"}:
                    field_name = to_camel(p.name)
                    if field_name not in seen_scalar:
                        seen_scalar.add(field_name)
                        jt = self._java_type_for_param(model, p)
                        scalar_fields.append(f"    private {jt} {field_name};")
                        scalar_init.append(f"        {field_name} = null;")

        sync_header_method = ""

        def record_request_assignable(binding: Optional[BlockBindingInfo], param_class: str) -> bool:
            if binding is None or not param_class:
                return False
            if param_class == binding.class_name or param_class == binding.dto_class:
                return True
            return bool(binding.kind == "extended" and binding.dto_class == param_class)

        def controller_call_comment(call_info: Optional[ProcedureCallInfo]) -> List[str]:
            if call_info is None or not (call_info.source_code or "").strip():
                return []
            return ["        /*", "         * Original procedure call from report.html:"] + [f"         * {line}" for line in call_info.source_code.splitlines()] + ["         */"]

        def build_method(routine_name: str, method_name: str) -> str:
            routine = next((r for r in model.routines.values() if r.package_name == model.package_name and r.name.upper() == routine_name), None)
            if routine is None:
                return ""
            dto_class = model.prefix_class + to_pascal(routine.name) + "WrpDto"
            call_info = (model.procedure_calls.get(routine.name.upper()) or [None])[0]
            setter_lines = controller_call_comment(call_info) + [f"        {dto_class} req = new {dto_class}();", "        //TODO confirm the data we use in set"]
            read_lines = []
            call_args = call_info.args if call_info is not None else []

            def arg_expr(idx: int) -> Optional[str]:
                return call_args[idx] if idx < len(call_args) else None

            def arg_block_field(expr: Optional[str]):
                if not expr:
                    return None
                m = re.match(r"^:([A-Z0-9_]+)\.([A-Z0-9_]+)$", expr.strip(), flags=re.I)
                if not m:
                    return None
                block_name = m.group(1).upper()
                field_name = m.group(2).upper()
                binding = block_bindings.get(block_name)
                return (binding, field_name) if binding is not None else None

            def getter_from_block(expr: Optional[str]) -> Optional[str]:
                bf = arg_block_field(expr)
                if bf is None:
                    return None
                binding, field_name = bf
                return f"{binding.instance_name}.get{to_pascal(field_name)}()"

            def binding_from_arg(expr: Optional[str]) -> Optional[BlockBindingInfo]:
                bf = arg_block_field(expr)
                return bf[0] if bf is not None else None

            def infer_table_binding_from_call() -> Optional[BlockBindingInfo]:
                bindings = []
                for expr in call_args:
                    bf = arg_block_field(expr)
                    if bf is None:
                        continue
                    binding, _ = bf
                    if binding.block_name not in [b.block_name for b in bindings]:
                        bindings.append(binding)
                if len(bindings) == 1 and bindings[0].is_table:
                    return bindings[0]
                table_bindings = [b for b in bindings if b.is_table]
                if len(table_bindings) == 1:
                    return table_bindings[0]
                all_table_bindings = [b for b in block_bindings.values() if b.is_table]
                if len(all_table_bindings) == 1:
                    return all_table_bindings[0]
                return None

            inferred_table_binding = infer_table_binding_from_call()

            for idx, p in enumerate(routine.params):
                pname = to_camel(p.name)
                cap = pname[0].upper() + pname[1:]
                rec = resolve_record(model.records, model.package_name, p.plsql_type)
                tab = resolve_table(model.tables, model.package_name, p.plsql_type)
                actual_rec = model.records.get(self._actual_record_name(model, rec.name), rec) if rec is not None else None
                matched_binding = binding_from_arg(arg_expr(idx)) if rec is not None else None
                if actual_rec is not None and (matched_binding is None or matched_binding.is_table):
                    matched_binding = next((b for b in block_bindings.values() if b.record_name == actual_rec.name and not b.is_table), None)
                    if matched_binding is None:
                        rec_fields = {f.name.upper() for f in rec.fields}
                        matched_binding = next((b for b in block_bindings.values() if not b.is_table and rec_fields.issubset({x.upper() for x in b.field_names})), None)
                mapped_getter = getter_from_block(arg_expr(idx))
                if p.mode in {"IN", "IN OUT"}:
                    if rec is not None:
                        req_dto = self._record_mapping_type_for_param(model, p)
                        if matched_binding is not None:
                            if record_request_assignable(matched_binding, req_dto):
                                setter_lines.append(f"        req.set{cap}({matched_binding.instance_name});")
                            else:
                                setter_lines.append(f"        req.set{cap}(new {req_dto}({matched_binding.instance_name}));")
                        elif header_binding is not None and record_request_assignable(header_binding, req_dto):
                            setter_lines.append(f"        req.set{cap}({header_ref});")
                        elif req_dto == header_dto:
                            setter_lines.append(f"        req.set{cap}({header_ref});")
                        else:
                            setter_lines.append(f"        req.set{cap}(new {req_dto}({header_ref}));")
                    elif tab is not None:
                        tab_rec = model.records.get(tab.element_type)
                        actual_tab_rec = model.records.get(self._actual_record_name(model, tab.element_type), tab_rec) if tab_rec is not None else None
                        table_binding = next((b for b in block_bindings.values() if b.is_table and actual_tab_rec is not None and b.record_name == actual_tab_rec.name), None) or inferred_table_binding
                        if table_binding is not None and tab_rec is not None:
                            param_element_dto = self._table_element_mapping_type_for_param(model, p)
                            ui_element_dto = table_binding.class_name
                            if param_element_dto == ui_element_dto:
                                setter_lines.append(f"        req.set{cap}({table_binding.instance_name});")
                            else:
                                setter_lines.append(f"        List<{param_element_dto}> {pname}List = new ArrayList<>();")
                                setter_lines.append(f"        {table_binding.instance_name}.forEach(item -> {{")
                                setter_lines.append(f"            {param_element_dto} converted = new {param_element_dto}(item);")
                                setter_lines.append(f"            {pname}List.add(converted);")
                                setter_lines.append("        });")
                                setter_lines.append(f"        req.set{cap}({pname}List);")
                        elif dates_dto and tab.element_type.endswith('REC_DATES'):
                            setter_lines.append(f"        req.set{cap}(dates);")
                        else:
                            setter_lines.append(f"        req.set{cap}(new ArrayList<>());")
                    else:
                        setter_lines.append(f"        req.set{cap}({mapped_getter or pname});")
                if p.mode in {"OUT", "IN OUT"}:
                    if rec is not None:
                        ui_target = matched_binding.instance_name if matched_binding is not None else header_ref
                        ui_target_class = matched_binding.class_name if matched_binding is not None else header_dto
                        param_dto_class = self._record_mapping_type_for_param(model, p)
                        if ui_target_class == param_dto_class:
                            read_lines.append(f"        if (resp.get{cap}() != null) {{ {ui_target} = resp.get{cap}(); }}")
                        else:
                            read_lines.append(f"        if (resp.get{cap}() != null) {{ {ui_target} = new {ui_target_class}(resp.get{cap}()); }}")
                    elif tab is not None:
                        tab_rec = model.records.get(tab.element_type)
                        actual_tab_rec = model.records.get(self._actual_record_name(model, tab.element_type), tab_rec) if tab_rec is not None else None
                        table_binding = next((b for b in block_bindings.values() if b.is_table and actual_tab_rec is not None and b.record_name == actual_tab_rec.name), None) or inferred_table_binding
                        if table_binding is not None and tab_rec is not None:
                            param_element_dto = self._table_element_mapping_type_for_param(model, p)
                            ui_element_dto = table_binding.class_name
                            if param_element_dto == ui_element_dto:
                                read_lines.append(f"        if (resp.get{cap}() != null) {{ {table_binding.instance_name} = resp.get{cap}(); }}")
                            else:
                                read_lines.append(f"        if (resp.get{cap}() != null) {{")
                                read_lines.append(f"            List<{ui_element_dto}> dtoList = new ArrayList<>();")
                                read_lines.append(f"            resp.get{cap}().forEach(detail -> {{")
                                read_lines.append(f"                {ui_element_dto} dto = new {ui_element_dto}(detail);")
                                read_lines.append(f"                dtoList.add(dto);")
                                read_lines.append("            });")
                                read_lines.append(f"            {table_binding.instance_name} = dtoList;")
                                read_lines.append("        }")
                        elif dates_dto and tab.element_type.endswith("REC_DATES"):
                            read_lines.append(f"        if (resp.get{cap}() != null) {{ dates = resp.get{cap}(); }}")
                    elif p.name.upper() not in {"P_ERRNO", "P_ERRTXT"}:
                        read_lines.append(f"        if (resp.get{cap}() != null) {{ {pname} = resp.get{cap}(); }}")
            has_resp = any(p.mode in {"OUT", "IN OUT"} and p.name.upper() not in {"P_ERRNO","P_ERRTXT"} for p in routine.params)
            call = f"        {dto_class} resp = service.call{model.prefix_class}{to_pascal(routine.name)}(req);" if has_resp else f"        service.call{model.prefix_class}{to_pascal(routine.name)}(req);"
            return f"""    public void {method_name}() {{
{chr(10).join(setter_lines)}
{call}
{chr(10).join(read_lines)}
    }}

"""

        perform_search = build_method("QRY", "performSearch")
        insert_method = build_method("INS", "insertAppl")
        update_method = build_method("UPD", "updateAppl")
        delete_method = build_method("DLT", "deleteAppl")
        save_methods = ""
        if insert_method or update_method:
            save_methods = f"""    public void saveAppl() {{
        if (editExisting) {{
            updateAppl();
        }} else {{
            insertAppl();
        }}
    }}

{insert_method}{update_method}"""
        qry_new_appl_req_line = f"req.setHeader({header_ref});" if header_binding is not None else f"req.setHeader(new {header_dto}({header_ref}));"
        qry_new_appl_resp_line = f"{header_ref} = new {header_binding.class_name if header_binding is not None else header_dto}(resp.getHeader());"
        perform_search_for_new_method = f"""    public void performSearchForNew() {{
        {model.prefix_class}QryNewApplDto req = new {model.prefix_class}QryNewApplDto();
        //TODO confirm the data we use in set
        {qry_new_appl_req_line}
        {model.prefix_class}QryNewApplDto resp = service.call{model.prefix_class}QryNewAppl(req);
        if (resp.getHeader() != null) {{
            {qry_new_appl_resp_line}
        }}
    }}

""" if model.create_qry_new_appl else ""
        validate_field_call = "service.validateField(fieldName, header, detailRecord);" if model.validation_fields else "return;"

        button_methods = []
        existing_button_names = {"performSearch","saveAppl","insertAppl","updateAppl","deleteAppl","performSearchForNew"}
        for btn in model.buttons:
            if btn.action_name in existing_button_names:
                continue
            source = btn.summary or f"{btn.block_name}.{btn.item_name}"
            raw_code = btn.code_body or ""
            comment = "        /**\n"
            comment += f"         * Source: {source}\n"
            comment += "         * Original PL/SQL from report.html:\n"
            comment += "         *\n"
            if raw_code.strip():
                for line in raw_code.splitlines():
                    comment += f"         * {line}\n"
            else:
                comment += "         * TODO Trigger code not found in report.html\n"
            comment += "         */"
            button_methods.append(f"""    public void {btn.action_name}() {{
{comment}
    }}
""")

        auto_call_methods = []
        existing_method_names = set(existing_button_names)
        for btn in model.buttons:
            existing_method_names.add(btn.action_name)
        for routine in sorted(model.routines.values(), key=lambda x: x.name):
            if routine.package_name != model.package_name or not self._routine_needs_wrapper(model, routine):
                continue
            routine_name = routine.name.upper()
            if routine_name in {"QRY", "INS", "UPD", "DLT", "QRY_NEW_APPL"}:
                continue
            method_name = to_camel(routine_name)
            if method_name in existing_method_names:
                continue
            auto_body = build_method(routine_name, method_name)
            if auto_body:
                auto_call_methods.append(auto_body)
                existing_method_names.add(method_name)

        validate_method_body = "return;"
        if model.validation_fields:
            if header_binding is not None:
                validate_method_body = f"//TODO check if the Header value is correct\n        service.validateField(fieldName, {header_ref}, detailRecord);"
            else:
                validate_method_body = "//TODO check if the Header value is correct\n        service.validateField(fieldName, header, detailRecord);"

        extra_imports = ""
        if any("BigDecimal" in x for x in scalar_fields):
            extra_imports += "import java.math.BigDecimal;\n"
        if any("Date" in x for x in scalar_fields):
            extra_imports += "import java.util.Date;\n"

        return f"""import jakarta.annotation.PostConstruct;
import jakarta.enterprise.context.ViewScoped;
import jakarta.inject.Inject;
import jakarta.inject.Named;
import lombok.Data;
import java.io.Serializable;
import java.util.ArrayList;
import java.util.List;
{extra_imports}
@Named("controller")
@ViewScoped
@Data
public class {model.screen_name}ManagementController implements Serializable {{

    @Inject private {model.screen_name}Service service;
    @Inject private UserSessionBean userSessionBean;
    @Inject private DashboardBean dashboardBean;
    @Inject private PageResourceBundleProducer pageResourceBundleProducer;
    @Inject private InsuredSearchBean insuredSearchBean;
    @Inject private CommonProcessService commonProcessService;
    @Inject private CommonListOfValueService commonListOfValueService;
    @Inject private DynamicLovPanelBean dynamicLovPanelBean;
    @Inject private DynamicLovPanelContext dynamicLovPanelContext;

    {'' if header_binding is not None else f'private {header_dto} header;'}
{extra_list_decl}{chr(10).join(block_instance_fields)}
{chr(10).join(scalar_fields)}
    private Object dynamicDropdownList;
    private boolean editExisting;

    @PostConstruct
    public void initialize() {{
        initializePanelStates();
        initializeUserSessionPreferences();
        initializeDefaultValues();
    }}

    public void initializePanelStates() {{
        // TODO Implement per UI requirements if needed (enable/disable panels, default tab, etc.)
    }}

    public void initializeUserSessionPreferences() {{
        // TODO Implement per your example controller (RTL/LTR, locale, etc.)
    }}

    public void initializeDefaultValues() {{
        {'' if header_binding is not None else f'header = new {header_dto}();'}
        {'dates = new ArrayList<>();' if dates_dto else ''}
{chr(10).join(block_instance_init)}
{chr(10).join(scalar_init)}
        dynamicDropdownList = null;
        editExisting = false;
    }}

{perform_search}{save_methods}{delete_method}{perform_search_for_new_method}{chr(10).join(auto_call_methods)}    public void validateField(String fieldName, Object detailRecord) {{
        {validate_method_body}
    }}

    public void checkLovCodeAndValidateField(String lovCode, Object detailObj, String fieldName) {{
        updateLovDescriptionForCode(lovCode, detailObj);
        validateField(fieldName, detailObj);
    }}

    public void updateLovDescriptionForCode(String lovCode, Object detailObj) {{
        // TODO
    }}

    public void openLovDynamicPanel(String lovCode, Object detailObj) {{
        // TODO
    }}

    public void clearFormData() {{
        initializeDefaultValues();
    }}

{chr(10).join(option_methods)}

{chr(10).join(button_methods)}
}}
"""
    def _generate_xhtml(self, model: ScreenModel) -> str:
        validation_set = set(model.validation_fields)
        excluded = {"STATUS", "CREATED_BY", "CREATION_DATE", "LAST_UPDATED_BY", "LAST_UPDATED_TIME"}
        block_instance_info = self._block_instance_info(model)
        block_bindings = self._block_bindings(model)
        header_binding = next((b for b in block_bindings.values() if b.record_name == model.header_record_name and not b.is_table), None)
        header_ref = header_binding.instance_name if header_binding is not None else "header"
        title_occurrences: Dict[Tuple[str, str], set] = {}
        for fd in model.fields:
            title = normalize_spaces((fd.title_label or "").replace("&#10;", " "))
            if title:
                title_occurrences.setdefault((fd.item_name, title), set()).add(fd.block_name)
        common_titles = {k: k[1] for k, blocks in title_occurrences.items() if len(blocks) > 1}

        used_descr_items = set()
        block_components: Dict[str, List[str]] = {block.block_name: [] for block in model.report_blocks}
        for fd in model.fields:
            if fd.item_name in excluded or fd.item_name in used_descr_items:
                continue
            prop = fd.property_name
            bundle_key = self._property_key_for_field(fd, common_titles)
            components = block_components.setdefault(fd.block_name, [])
            value_root = (block_instance_info.get(fd.block_name) or {}).get("expr") or "controller"
            if fd.item_name == "LAST_UPDATED_DATE":
                components.append(f'''                <p:outputLabel for="lastUpdatedDateAndTime" value="#{{pageMsgs['{bundle_key}']}}"/>
                <p:inputText id="lastUpdatedDateAndTime" value="#{{{value_root}.lastUpdatedDateAndTime}}" disabled="true"/>''')
                continue
            if looks_like_insured_search_item(fd.item_name):
                components.append(f'''                <p:outputLabel for="insuredSearch" value="#{{pageMsgs['{bundle_key}']}}"/>
                <dc:InsuredSearch bean="#{{insuredSearchBean}}"
                                  target="#{{{value_root}}}"
                                  dropdownData="#{{controller.dynamicDropdownList}}"
                                  mode="edit"
                                  update=":viewsPanel"/>''')
                continue
            if fd.option_enum_values:
                option_method = enum_controller_method_name(fd.item_name)
                has_disabled = any(v.description == "Οριστικοποιημένη" for v in fd.option_enum_values)
                disabled_attr = '\n                                   itemDisabled="#{{opt.disabled}}"' if has_disabled else ""
                ajax = f'<p:ajax event="change" process="@this" listener="#{{controller.validateField(\'{fd.item_name}\', {value_root})}}" update="@form"/>' if fd.item_name in validation_set else ""
                components.append(f'''                <p:outputLabel for="{prop}" value="#{{pageMsgs['{bundle_key}']}}"/>
                <p:selectOneMenu id="{prop}" value="#{{{value_root}.{prop}}}">
                    <f:selectItems value="#{{controller.{option_method}()}}"
                                   var="opt"
                                   itemLabel="#{{opt.description}}"
                                   itemValue="#{{opt.id}}"{disabled_attr}/>
                    {ajax}
                </p:selectOneMenu>''')
                continue
            oracle_type = self._oracle_type_for_field(model, fd.item_name, fd.block_name)
            if should_generate_boolean_accessors(fd.item_name, oracle_type, fd, self.type_catalog, schema_name=model.schema_name):
                components.append(f'''                <p:outputLabel for="{prop}" value="#{{pageMsgs['{bundle_key}']}}"/>
                <p:selectBooleanCheckbox id="{prop}" value="#{{{value_root}.{prop}Boolean}}"/>''')
                continue
            if (fd.datatype or "").upper() == "DATE" or fd.item_name.endswith("_DATE") or fd.item_name.endswith("DATE"):
                ajax = f'<p:ajax event="dateSelect" process="@this" listener="#{{controller.validateField(\'{fd.item_name}\', {value_root})}}" update="@form"/>' if fd.item_name in validation_set else ""
                components.append(f'''                <p:outputLabel for="{prop}" value="#{{pageMsgs['{bundle_key}']}}"/>
                <p:datePicker id="{prop}" value="#{{{value_root}.{prop}}}" pattern="dd/MM/yyyy">
                    {ajax}
                </p:datePicker>''')
                continue
            if fd.has_lov and not should_exclude_lov_from_xhtml(fd):
                listener = f"#{{controller.checkLovCodeAndValidateField('{lov_code_for_field(fd)}', {value_root}, '{fd.item_name}')}}" if fd.item_name in validation_set else f"#{{controller.updateLovDescriptionForCode('{lov_code_for_field(fd)}', {value_root})}}"
                has_descr = fd.descr_item and fd.descr_item != fd.item_name
                if has_descr:
                    descr_prop = to_camel(fd.descr_item)
                    used_descr_items.add(fd.descr_item)
                    components.append(f'''                <p:outputLabel for="{prop}" value="#{{pageMsgs['{bundle_key}']}}"/>
                <p:inputText id="{prop}" value="#{{{value_root}.{prop}}}" title="#{{pageMsgs['{bundle_key}']}}" style="width: 70%">
                    <p:ajax event="blur" process="@this" listener="{listener}" update="@this :{descr_prop} :messages"/>
                </p:inputText>
                <h:outputText value="-" styleClass="separator" />
                <p:inputText id="{descr_prop}" value="#{{{value_root}.{descr_prop}}}" style="width: 70%" disabled="true"/>
                <p:commandButton icon="ui-icon-search" tabindex="-1" title="#{{beansMsgs['uibeans.buttonSearch']}}" action="#{{controller.openLovDynamicPanel('{lov_code_for_field(fd)}', {value_root})}}" update=":viewsPanel" oncomplete="views_SlideStart();"/>''')
                else:
                    components.append(f'''                <p:outputLabel for="{prop}" value="#{{pageMsgs['{bundle_key}']}}"/>
                <p:inputText id="{prop}" value="#{{{value_root}.{prop}}}" title="#{{pageMsgs['{bundle_key}']}}" style="width: 70%">
                    <p:ajax event="blur" process="@this" listener="{listener}" update="@this :messages"/>
                </p:inputText>
                <p:commandButton icon="ui-icon-search" tabindex="-1" title="#{{beansMsgs['uibeans.buttonSearch']}}" action="#{{controller.openLovDynamicPanel('{lov_code_for_field(fd)}', {value_root})}}" update=":viewsPanel" oncomplete="views_SlideStart();"/>''')
                continue
            ajax = f'<p:ajax event="blur" process="@this" listener="#{{controller.validateField(\'{fd.item_name}\', {value_root})}}" update="@form"/>' if fd.item_name in validation_set else ""
            components.append(f'''                <p:outputLabel for="{prop}" value="#{{pageMsgs['{bundle_key}']}}"/>
                <p:inputText id="{prop}" value="#{{{value_root}.{prop}}}" title="#{{pageMsgs['{bundle_key}']}}">
                    {ajax}
                </p:inputText>''')
        panel_groups = []
        for block in model.report_blocks:
            comps = block_components.get(block.block_name, [])
            binding = block_bindings.get(block.block_name)
            if binding is not None and binding.is_table:
                cols = []
                var_name = to_camel(block.block_name)
                for fd in block.fields.values():
                    if fd.item_name in excluded:
                        continue
                    bundle_key = self._property_key_for_field(fd, common_titles)
                    prop = fd.property_name
                    ajax_blur = f"""<p:ajax event="blur" process="@this" listener="#{{controller.validateField('{fd.item_name}', {var_name})}}" update="@form"/>""" if fd.item_name in validation_set else ""
                    oracle_type = self._oracle_type_for_field(model, fd.item_name, fd.block_name)
                    column_body = f'<h:outputText value="#{{{var_name}.{prop}}}"/>'
                    if fd.option_enum_values:
                        option_method = enum_controller_method_name(fd.item_name)
                        has_disabled = any(v.description == "Οριστικοποιημένη" for v in fd.option_enum_values)
                        disabled_attr = '\n                                   itemDisabled="#{{opt.disabled}}"' if has_disabled else ""
                        ajax_change = f"""<p:ajax event="change" process="@this" listener="#{{controller.validateField('{fd.item_name}', {var_name})}}" update="@form"/>""" if fd.item_name in validation_set else ""
                        column_body = f'''<p:selectOneMenu value="#{{{var_name}.{prop}}}">
                            <f:selectItems value="#{{controller.{option_method}()}}"
                                           var="opt"
                                           itemLabel="#{{opt.description}}"
                                           itemValue="#{{opt.id}}"{disabled_attr}/>
                            {ajax_change}
                        </p:selectOneMenu>'''
                    elif should_generate_boolean_accessors(fd.item_name, oracle_type, fd, self.type_catalog, schema_name=model.schema_name):
                        column_body = f'''<p:selectBooleanCheckbox value="#{{{var_name}.{prop}Boolean}}">
                            {ajax_blur}
                        </p:selectBooleanCheckbox>'''
                    elif (fd.datatype or "").upper() == "DATE" or fd.item_name.endswith("_DATE") or fd.item_name.endswith("DATE"):
                        ajax_date = f"""<p:ajax event="dateSelect" process="@this" listener="#{{controller.validateField('{fd.item_name}', {var_name})}}" update="@form"/>""" if fd.item_name in validation_set else ""
                        column_body = f'''<p:datePicker value="#{{{var_name}.{prop}}}" pattern="dd/MM/yyyy">
                            {ajax_date}
                        </p:datePicker>'''
                    elif fd.has_lov and not should_exclude_lov_from_xhtml(fd):
                        listener = f"#{{controller.checkLovCodeAndValidateField('{lov_code_for_field(fd)}', {var_name}, '{fd.item_name}')}}" if fd.item_name in validation_set else f"#{{controller.updateLovDescriptionForCode('{lov_code_for_field(fd)}', {var_name})}}"
                        column_body = f'''<p:inputText value="#{{{var_name}.{prop}}}" title="#{{pageMsgs['{bundle_key}']}}" style="width: 70%">
                            <p:ajax event="blur" process="@this" listener="{listener}" update="@form"/>
                        </p:inputText>
                        <p:commandButton icon="ui-icon-search" tabindex="-1" title="#{{beansMsgs['uibeans.buttonSearch']}}" action="#{{controller.openLovDynamicPanel('{lov_code_for_field(fd)}', {var_name})}}" update=":viewsPanel" oncomplete="views_SlideStart();"/>'''
                    else:
                        column_body = f'''<p:inputText value="#{{{var_name}.{prop}}}" title="#{{pageMsgs['{bundle_key}']}}">
                            {ajax_blur}
                        </p:inputText>''' if fd.item_name in validation_set else f'<h:outputText value="#{{{var_name}.{prop}}}"/>'
                    cols.append(f'''                    <p:column headerText="#{{pageMsgs['{bundle_key}']}}" sortBy="#{{{var_name}.{prop}}}">
                        {column_body}
                    </p:column>''')
                panel_groups.append(f'''            <h:panelGroup id="{to_camel(block.block_name)}PanelGroup" layout="block">
                <p:dataTable id="{to_camel(block.block_name)}Table" reflow="true"
                             value="#{{controller.{binding.instance_name}}}"
                             var="{var_name}"
                             paginator="true" rows="10"
                             styleClass="cars-datalist intra_header"
                             paginatorTemplate="{{RowsPerPageDropdown}} {{FirstPageLink}} {{PreviousPageLink}} {{CurrentPageReport}} {{NextPageLink}} {{LastPageLink}}"
                             rowsPerPageTemplate="#{{pageMsgs['datatable.rowsPerPageTemplate']}}">
{chr(10).join(cols)}
                </p:dataTable>
            </h:panelGroup>''')
            elif comps:
                panel_groups.append(f'''            <h:panelGroup id="{to_camel(block.block_name)}PanelGroup" layout="block">
                <p:panelGrid columns="4" layout="grid" styleClass="ui-fluid">
{chr(10).join(comps)}
                </p:panelGrid>
            </h:panelGroup>''')
        buttons_markup = []
        if self._has_wrapper(model, "QRY"):
            buttons_markup.append(f'            <p:commandButton id="btnSearch" value="#{{pageMsgs[\'{model.form_key}.performSearch\']}}" actionListener="#{{controller.performSearch}}" update="@form"/>')
        if self._has_wrapper(model, "INS") or self._has_wrapper(model, "UPD"):
            buttons_markup.append(f'            <p:commandButton id="btnSave" value="#{{pageMsgs[\'{model.form_key}.saveAppl\']}}" actionListener="#{{controller.saveAppl}}" update="@form"/>')
        if self._has_wrapper(model, "DLT"):
            buttons_markup.append(f'            <p:commandButton id="btnDelete" value="#{{pageMsgs[\'{model.form_key}.deleteAppl\']}}" actionListener="#{{controller.deleteAppl}}" update="@form"/>')
        if model.create_qry_new_appl:
            buttons_markup.append(f'            <p:commandButton id="btnNew" value="#{{pageMsgs[\'{model.form_key}.performSearchForNew\']}}" actionListener="#{{controller.performSearchForNew}}" update="@form"/>')
        for btn in model.buttons:
            label_key = f"{model.form_key}.{to_camel(btn.item_name)}"
            buttons_markup.append(f'            <p:commandButton id="{to_camel(btn.item_name)}" value="#{{pageMsgs[\'{label_key}\']}}" actionListener="#{{controller.{btn.action_name}}}" update="@form"/>')
        return f'''<!DOCTYPE html>
<ui:composition xmlns="http://www.w3.org/1999/xhtml"
                xmlns:h="http://xmlns.jcp.org/jsf/html"
                xmlns:f="http://xmlns.jcp.org/jsf/core"
                xmlns:ui="http://xmlns.jcp.org/jsf/facelets"
                xmlns:p="http://primefaces.org/ui"
                xmlns:c="http://xmlns.jcp.org/jsp/jstl/core"
                xmlns:cc="http://xmlns.jcp.org/jsf/composite"
                xmlns:dc="http://xmlns.jcp.org/jsf/composite/dc">
    <h:form id="viewsPanel">
        <p:messages id="messages" showDetail="true" closable="true"/>

        <h:panelGroup id="dynamicLovPanelGroup">
            <p:outputPanel id="dynamicLovPanel" autoUpdate="true" layout="block" rendered="#{{dynamicLovPanelBean.panelStack.size() > 0}}">
                <c:forEach items="#{{dynamicLovPanelBean.panelStack.toArray()}}" var="ps" varStatus="status">
                    <cc:NewDynamicLovPanel id="dynamicLovPanel_#{{status.index}}" panelState="#{{ps}}" updatePanel="dynamicLovPanel_#{{status.index}}" parentPanelGroupId="dynamicLovPanelGroup"/>
                </c:forEach>
            </p:outputPanel>
        </h:panelGroup>

        <p:panel id="contentBody" header="#{{pageMsgs['{model.form_key}.title']}}">
{chr(10).join(panel_groups)}

            <p:separator/>

{chr(10).join(buttons_markup)}
        </p:panel>
    </h:form>
</ui:composition>
'''

    def _generate_properties(self, model: ScreenModel) -> str:
        lines = []
        seen = set()
        title_occurrences: Dict[Tuple[str, str], set] = {}
        for fd in model.fields:
            title = normalize_spaces((fd.title_label or "").replace("&#10;", " "))
            if title:
                title_occurrences.setdefault((fd.item_name, title), set()).add(fd.block_name)
        common_titles = {k: k[1] for k, blocks in title_occurrences.items() if len(blocks) > 1}

        def add(key: str, value: Optional[str]):
            if value and key not in seen:
                seen.add(key)
                lines.append(f"{key} = {escape_properties_value(value)}")

        add(f"{model.form_key}.title", model.package_name)
        for fd in model.fields:
            title = normalize_spaces((fd.title_label or "").replace("&#10;", " "))
            if title:
                add(self._property_key_for_field(fd, common_titles), title)
        for btn in model.buttons:
            add(f"{model.form_key}.{to_camel(btn.item_name)}", (btn.title_label or pretty_label_from_item(btn.item_name) or "").replace("&#10;", " "))
        if self._has_wrapper(model, "QRY"):
            add(f"{model.form_key}.performSearch", "Αναζήτηση")
        if self._has_wrapper(model, "INS") or self._has_wrapper(model, "UPD"):
            add(f"{model.form_key}.saveAppl", "Αποθήκευση")
        if self._has_wrapper(model, "DLT"):
            add(f"{model.form_key}.deleteAppl", "Διαγραφή")
        if model.create_qry_new_appl:
            add(f"{model.form_key}.performSearchForNew", "Νέα")
        return "\n".join(lines) + "\n"

    def _generate_table_mapping_before(self, model: ScreenModel, param_name: str, local_name: str, table_name: str) -> str:
        tab = model.tables[table_name]
        rec = model.records[self._actual_record_name(model, tab.element_type)]
        lines = [
            f"    IF {param_name}.COUNT > 0 THEN",
            "        BEGIN",
            f"            FOR r IN {param_name}.FIRST..{param_name}.LAST LOOP",
        ]
        for f in rec.fields:
            lines.append(f"                {local_name}(r).{f.name} := {param_name}(r).{f.name};")
        lines.extend([
            "            END LOOP;",
            "        END;",
            "    END IF;",
        ])
        return "\n".join(lines)

    def _generate_table_mapping_after(self, model: ScreenModel, param_name: str, local_name: str, table_name: str) -> str:
        tab = model.tables[table_name]
        rec = model.records[self._actual_record_name(model, tab.element_type)]
        db_table = self._db_object_name_for_table(model, table_name)
        db_record = self._db_object_name_for_record(model, rec.name)
        ctor_vals = ",\n                                                            ".join(f"{local_name}(r).{f.name}" for f in rec.fields)
        lines = [
            f"    IF {local_name}.COUNT > 0 THEN",
            "        BEGIN",
            f"            {param_name} := {db_table}();",
            f"            FOR r IN {local_name}.FIRST..{local_name}.LAST LOOP",
            f"                {param_name}.EXTEND;",
            f"                {param_name}({param_name}.LAST) := {db_record}({ctor_vals});",
            "            END LOOP;",
            "        END;",
            "    END IF;",
        ]
        return "\n".join(lines)

    def _generate_procedures_sql(self, model: ScreenModel) -> str:
        parts = [f"-- Generated SQL for {model.package_name}"]
        for rec_name in model.referenced_records:
            if rec_name not in model.records:
                continue
            rec = model.records[rec_name]
            attrs = [f"    {f.name} {sql_type_with_char(resolve_actual_plsql_type(f.plsql_type, self.type_catalog, schema_name=rec.schema_name))}" for f in rec.fields]
            parts.append(f"CREATE OR REPLACE TYPE {self._db_object_name_for_record(model, rec_name)} AS OBJECT (\n" + ",\n".join(attrs) + "\n);")
        for tab_name in model.referenced_tables:
            if tab_name not in model.tables:
                continue
            tab = model.tables[tab_name]
            parts.append(f"CREATE OR REPLACE TYPE {self._db_object_name_for_table(model, tab_name)} AS TABLE OF {self._db_object_name_for_record(model, self._actual_record_name(model, tab.element_type))};")
        for routine in sorted(model.routines.values(), key=lambda x: x.name):
            if routine.package_name != model.package_name:
                continue
            if not self._routine_needs_wrapper(model, routine):
                continue
            parts.append(self._generate_wrapper_procedure_sql(model, routine))
        if model.create_qry_new_appl:
            parts.append(self._generate_qry_new_appl_sql(model))
        return "\n\n".join(parts) + "\n"

    def _generate_wrapper_procedure_sql(self, model: ScreenModel, routine: SqlRoutine) -> str:
        params = []
        decls = []
        before = []
        named_args = []
        after = []
        for p in routine.params:
            ptype = p.plsql_type.upper()
            lname = "l_" + to_camel(p.name)
            if "%TYPE" in ptype:
                signature_type = sql_type_with_char(resolve_actual_plsql_type(p.plsql_type, self.type_catalog, schema_name=routine.schema_name))
                params.append(f"    {p.name} {p.mode} {signature_type}")
                named_args.append(f"          {p.name} => {p.name}")
                continue
            rec = resolve_record(model.records, model.package_name, p.plsql_type)
            tab = resolve_table(model.tables, model.package_name, p.plsql_type)
            if rec is not None:
                actual = normalize_record_alias_name(rec.name) if "INSERT" in rec.name else rec.name
                params.append(f"    {p.name} {p.mode} {self._db_object_name_for_record(model, actual)}")
                decls.append(f"    {lname} {rec.package_name}.{ptype.split('.')[-1]};")
                if p.mode in {"IN", "IN OUT"}:
                    for f in rec.fields:
                        before.append(f"    {lname}.{f.name} := {p.name}.{f.name};")
                if p.mode in {"OUT", "IN OUT"}:
                    for f in rec.fields:
                        after.append(f"    {p.name}.{f.name} := {lname}.{f.name};")
                named_args.append(f"          {p.name} => {lname}")
            elif tab is not None:
                params.append(f"    {p.name} {p.mode} {self._db_object_name_for_table(model, tab.name)}")
                decls.append(f"    {lname} {tab.package_name}.{ptype.split('.')[-1]};")
                if p.mode in {"IN", "IN OUT"}:
                    before.append(self._generate_table_mapping_before(model, p.name, lname, tab.name))
                if p.mode in {"OUT", "IN OUT"}:
                    after.append(self._generate_table_mapping_after(model, p.name, lname, tab.name))
                named_args.append(f"          {p.name} => {lname}")
            else:
                params.append(f"    {p.name} {p.mode} {ptype}")
                named_args.append(f"          {p.name} => {p.name}")
        call_sql = f"""    {routine.name}(
{",\n".join(named_args)}
      );"""
        all_decls = decls
        return f"""PROCEDURE {routine.name}_WRP(
{",\n".join(params)}
) IS
{chr(10).join(all_decls) if all_decls else ''}
BEGIN
{chr(10).join(before)}
{call_sql}
{chr(10).join(after)}
END {routine.name}_WRP;"""

    def _generate_qry_new_appl_sql(self, model: ScreenModel) -> str:
        header_obj = self._db_object_name_for_record(model, model.header_record_name)
        appl_unit_fd = next((fd for fd in model.fields if fd.validation_body and is_appl_unit_field_name(fd.item_name)), None)
        appl_incr_fd = next((fd for fd in model.fields if fd.validation_body and is_appl_incr_seq_field_name(fd.item_name)), None)
        chosen = appl_unit_fd or appl_incr_fd
        validation_name = "QRY_NEW_APPL"
        if chosen and chosen.links_to_code:
            for link_name in chosen.links_to_code:
                if link_name and link_name.strip().lower() not in {"lov", "options", "trigger"}:
                    validation_name = link_name
                    break
        source_code = (chosen.validation_body if chosen and chosen.validation_body else "NULL;").rstrip()
        comment_lines = [f"Source validation: {validation_name}"]
        if appl_unit_fd and appl_incr_fd and (appl_unit_fd.validation_body or "").strip() != (appl_incr_fd.validation_body or "").strip():
            comment_lines.append(f"TODO Review additional validation from {appl_incr_fd.item_name} as well.")
        comment_lines.append("")
        comment_lines.extend(source_code.splitlines())
        comment = "\n".join(["    /*"] + [f"     * {line}" if line else "     *" for line in comment_lines] + ["     */"])
        return f"""PROCEDURE QRY_NEW_APPL(
    p_header IN OUT {header_obj},
    p_errno  OUT NUMBER,
    p_errtxt OUT VARCHAR2
) IS
BEGIN
{comment}
    NULL;
END QRY_NEW_APPL;"""


class GeneratorEngine:
    def run(self, bundle: InputBundle) -> Tuple[ScreenModel, Dict[str, str]]:
        parsed_sql = SqlParser().parse(bundle)
        parsed_report = ReportParser().parse(bundle)
        model = ScreenModelBuilder().build(bundle, parsed_sql, parsed_report)
        files = CodeGenerator(bundle.package_type_catalog).generate_all(model)
        return model, files


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("OracleFormsCodeGenerator__CSV_v14")
        self.geometry("1180x780")
        self.main_sql_var = tk.StringVar()
        self.report_var = tk.StringVar()
        self.types_csv_var = tk.StringVar()
        self.output_var = tk.StringVar()
        self.additional_sql_files: List[str] = []
        self._build()

    def _build(self):
        frm = ttk.Frame(self, padding=10)
        frm.pack(fill="both", expand=True)

        ttk.Label(frm, text="Main package SQL").grid(row=0, column=0, sticky="w")
        ttk.Entry(frm, textvariable=self.main_sql_var, width=108).grid(row=0, column=1, columnspan=3, sticky="we")
        ttk.Button(frm, text="Browse", command=lambda: self._pick_file(self.main_sql_var, [("SQL", "*.sql"), ("All", "*.*")])).grid(row=0, column=4, sticky="w")

        ttk.Label(frm, text="Additional SQL files (optional)").grid(row=1, column=0, sticky="w", pady=(4, 0))
        self.listbox = tk.Listbox(frm, height=5, width=120)
        self.listbox.grid(row=2, column=0, columnspan=3, sticky="we")
        ttk.Button(frm, text="Add SQL", command=self._add_sql).grid(row=2, column=3, sticky="w", padx=8)
        ttk.Button(frm, text="Remove selected", command=self._remove_sql).grid(row=2, column=4, sticky="w")

        ttk.Label(frm, text="report.html").grid(row=3, column=0, sticky="w", pady=(4, 0))
        ttk.Entry(frm, textvariable=self.report_var, width=108).grid(row=3, column=1, columnspan=3, sticky="we", pady=(4, 0))
        ttk.Button(frm, text="Browse", command=lambda: self._pick_file(self.report_var, [("HTML", "*.html;*.htm"), ("All", "*.*")])).grid(row=3, column=4, sticky="w", pady=(4, 0))

        ttk.Label(frm, text="Package types CSV (optional)").grid(row=4, column=0, sticky="w", pady=(4, 0))
        ttk.Entry(frm, textvariable=self.types_csv_var, width=108).grid(row=4, column=1, columnspan=3, sticky="we", pady=(4, 0))
        ttk.Button(frm, text="Browse", command=lambda: self._pick_file(self.types_csv_var, [("CSV", "*.csv"), ("All", "*.*")])).grid(row=4, column=4, sticky="w", pady=(4, 0))

        ttk.Label(frm, text="Output ZIP").grid(row=5, column=0, sticky="w", pady=(4, 0))
        ttk.Entry(frm, textvariable=self.output_var, width=108).grid(row=5, column=1, columnspan=3, sticky="we", pady=(4, 0))
        ttk.Button(frm, text="Browse", command=self._pick_out_zip).grid(row=5, column=4, sticky="w", pady=(4, 0))

        self.progress = ttk.Progressbar(frm, mode="determinate", maximum=100)
        self.progress.grid(row=6, column=0, columnspan=5, sticky="we", pady=(10, 4))

        self.log = tk.Text(frm, height=18, width=140)
        self.log.grid(row=7, column=0, columnspan=5, sticky="nsew")

        btns = ttk.Frame(frm)
        btns.grid(row=8, column=0, columnspan=5, sticky="e", pady=(4, 0))
        ttk.Button(btns, text="Generate", command=self._generate).pack(side="right")
        ttk.Button(btns, text="Exit", command=self.destroy).pack(side="right", padx=(0, 8))

    def _add_sql(self):
        for p in filedialog.askopenfilenames(filetypes=[("SQL", "*.sql"), ("All", "*.*")]):
            if p and p not in self.additional_sql_files:
                self.additional_sql_files.append(p)
                self.listbox.insert("end", p)

    def _remove_sql(self):
        selection = list(self.listbox.curselection())
        selection.reverse()
        for idx in selection:
            self.additional_sql_files.pop(idx)
            self.listbox.delete(idx)

    def _pick_file(self, var, filetypes):
        p = filedialog.askopenfilename(filetypes=filetypes)
        if p:
            var.set(p)
            if var is self.report_var and not self.output_var.get():
                self.output_var.set(str(Path(p).with_name(Path(p).stem + "_generated.zip")))

    def _pick_out_zip(self):
        p = filedialog.asksaveasfilename(defaultextension=".zip", filetypes=[("ZIP", "*.zip")])
        if p:
            self.output_var.set(p)

    def _log(self, msg: str):
        self.log.insert("end", msg + "\n")
        self.log.see("end")

    def _set_progress(self, value: int):
        self.progress["value"] = value
        self.update_idletasks()

    def _generate(self):
        if not self.main_sql_var.get() or not self.report_var.get():
            messagebox.showerror("Missing inputs", "Select main SQL and report.html")
            return

        def run():
            try:
                self._set_progress(10)
                loader = InputLoader()
                bundle = loader.load(
                    self.main_sql_var.get(),
                    self.additional_sql_files,
                    self.report_var.get(),
                    self.output_var.get(),
                    self.types_csv_var.get(),
                )
                self._log("Loaded inputs.")
                self._log(f"Main package: {bundle.package_name}")
                if bundle.types_csv_path:
                    self._log(f"Loaded package types CSV: {bundle.types_csv_path}")
                self._set_progress(25)

                engine = GeneratorEngine()
                model, files = engine.run(bundle)
                self._log(f"Detected main block: {model.main_block}")
                self._log(f"Detected fields: {len(model.fields)}")
                self._log(f"Detected buttons: {len(model.buttons)}")
                self._log(f"Detected validations: {len(model.validation_fields)}")
                self._set_progress(70)

                with zipfile.ZipFile(bundle.output_zip_path, "w", compression=zipfile.ZIP_DEFLATED) as z:
                    for name, content in files.items():
                        z.writestr(name, content)

                self._set_progress(100)
                self._log(f"Generated files: {len(files)}")
                self._log(f"DONE -> {bundle.output_zip_path}")
                messagebox.showinfo("Success", f"Generated: {bundle.output_zip_path}")
            except Exception:
                self._log(traceback.format_exc())
                messagebox.showerror("Error", "Generator failed. Check log.")

        threading.Thread(target=run, daemon=True).start()


if __name__ == "__main__":
    App().mainloop()


# ===============================
# Binding v2 enhancements
# ===============================

def map_ui_to_procedure_dto(ui_obj, dto_obj, mapping):
    """Smart mapping from UI object to Procedure DTO"""
    for src, dst in mapping.items():
        if hasattr(ui_obj, src):
            setattr(dto_obj, dst, getattr(ui_obj, src))

def map_procedure_out_to_ui(dto_obj, ui_obj, mapping):
    """Map OUT / INOUT procedure values back to UI object"""
    for src, dst in mapping.items():
        if hasattr(dto_obj, src):
            setattr(ui_obj, dst, getattr(dto_obj, src))

def legacy_syncHeaderFromBlocks(controller):
    """Placeholder for header synchronization logic"""
    return controller