"""
Core data models for Oracle Forms & Reports Migration Tool
"""
from dataclasses import dataclass, field
from typing import List, Dict, Optional, Any


@dataclass
class TriggerInfo:
    name: str
    trigger_type: str       # FORM, BLOCK, ITEM, REPORT
    code: str
    line_count: int
    has_dml: bool
    has_exec_sql: bool
    has_go_block: bool
    has_go_item: bool
    has_complex_logic: bool
    has_exception_handling: bool
    has_cursor: bool
    has_loops: bool

    def to_dict(self) -> Dict:
        return {
            "name": self.name,
            "trigger_type": self.trigger_type,
            "line_count": self.line_count,
            "has_dml": self.has_dml,
            "has_exec_sql": self.has_exec_sql,
            "has_go_block": self.has_go_block,
            "has_go_item": self.has_go_item,
            "has_complex_logic": self.has_complex_logic,
            "has_exception_handling": self.has_exception_handling,
            "has_cursor": self.has_cursor,
            "has_loops": self.has_loops,
            "code_preview": self.code[:200] + "..." if len(self.code) > 200 else self.code,
        }


@dataclass
class FormItem:
    name: str
    item_type: str
    data_type: str
    required: bool
    list_of_values: Optional[str] = None
    triggers: List[TriggerInfo] = field(default_factory=list)

    def to_dict(self) -> Dict:
        return {
            "name": self.name,
            "item_type": self.item_type,
            "data_type": self.data_type,
            "required": self.required,
            "list_of_values": self.list_of_values,
            "triggers": [t.to_dict() for t in self.triggers],
        }


@dataclass
class DataBlock:
    name: str
    data_source: Optional[str]
    query: Optional[str]
    items: List[FormItem] = field(default_factory=list)
    triggers: List[TriggerInfo] = field(default_factory=list)

    def to_dict(self) -> Dict:
        return {
            "name": self.name,
            "data_source": self.data_source,
            "query": self.query,
            "items": [i.to_dict() for i in self.items],
            "triggers": [t.to_dict() for t in self.triggers],
        }


@dataclass
class OracleArtifact:
    name: str
    artifact_type: str          # FORM or REPORT
    file_path: str
    file_size: int = 0
    blocks: List[DataBlock] = field(default_factory=list)
    form_triggers: List[TriggerInfo] = field(default_factory=list)
    parameters: List[Dict] = field(default_factory=list)
    program_units: List[Dict] = field(default_factory=list)
    queries: List[Dict] = field(default_factory=list)
    raw_content: str = ""

    def all_triggers(self) -> List[TriggerInfo]:
        triggers = list(self.form_triggers)
        for block in self.blocks:
            triggers.extend(block.triggers)
            for item in block.items:
                triggers.extend(item.triggers)
        return triggers

    def to_dict(self) -> Dict:
        return {
            "name": self.name,
            "artifact_type": self.artifact_type,
            "file_path": self.file_path,
            "file_size": self.file_size,
            "blocks": [b.to_dict() for b in self.blocks],
            "form_triggers": [t.to_dict() for t in self.form_triggers],
            "parameters": self.parameters,
            "program_units": self.program_units,
            "queries": self.queries,
        }


@dataclass
class ComplexityReport:
    artifact: OracleArtifact
    score: int                  # 1, 2, or 3
    label: str                  # Simple, Moderate, Complex
    raw_points: int
    trigger_count: int
    dml_trigger_count: int
    complex_logic_count: int
    program_unit_count: int
    block_count: int
    query_count: int
    reasons: List[str] = field(default_factory=list)
    breakdown: Dict[str, Any] = field(default_factory=dict)
    migration_notes: List[str] = field(default_factory=list)
    estimated_effort_days: float = 0.0

    def to_dict(self) -> Dict:
        return {
            "name": self.artifact.name,
            "type": self.artifact.artifact_type,
            "file": self.artifact.file_path,
            "file_size_bytes": self.artifact.file_size,
            "complexity_level": self.score,
            "complexity_label": self.label,
            "raw_points": self.raw_points,
            "estimated_effort_days": self.estimated_effort_days,
            "metrics": self.breakdown,
            "reasons": self.reasons,
            "migration_notes": self.migration_notes,
            "summary": {
                "triggers": self.trigger_count,
                "dml_triggers": self.dml_trigger_count,
                "complex_logic": self.complex_logic_count,
                "program_units": self.program_unit_count,
                "blocks": self.block_count,
                "queries": self.query_count,
            },
        }
