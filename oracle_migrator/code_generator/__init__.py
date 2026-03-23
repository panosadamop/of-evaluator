"""
oracle_migrator.code_generator
================================
Wraps OracleFormsCodeGenerator_CSV_Binding_v20 as a Flask-embeddable module.
Tkinter and threading imports in the original file are neutralised via a
sys.modules stub so the engine logic can be imported in a headless server.
"""

import sys
import types

# ── Stub out tkinter before the engine is imported ────────────────────────────
# The original file has `import tkinter as tk` and uses tk.Tk, tk.StringVar etc.
# We provide empty stub modules so Python doesn't raise ModuleNotFoundError.
for _mod in ("tkinter", "tkinter.filedialog", "tkinter.messagebox", "tkinter.ttk"):
    if _mod not in sys.modules:
        _stub = types.ModuleType(_mod)
        # Provide no-op stand-ins for names referenced at module scope
        _stub.StringVar = object
        _stub.Listbox   = object
        _stub.Text      = object
        _stub.Tk        = object
        _stub.Frame     = object
        _stub.Label     = object
        _stub.Entry     = object
        _stub.Button    = object
        _stub.Progressbar = object
        sys.modules[_mod] = _stub

# Now it is safe to import the engine
from .engine import (          # noqa: E402
    InputLoader,
    GeneratorEngine,
    ScreenModel,
    SqlParser,
    ReportParser,
    ScreenModelBuilder,
    CodeGenerator,
    PackageTypeCatalog,
)

__all__ = [
    "InputLoader", "GeneratorEngine", "ScreenModel",
    "SqlParser", "ReportParser", "ScreenModelBuilder",
    "CodeGenerator", "PackageTypeCatalog",
    "run_generator",
]


def run_generator(
    main_sql_path: str,
    report_html_path: str,
    output_zip_path: str,
    additional_sql_paths=None,
    types_csv_path: str = None,
):
    """
    One-shot convenience wrapper used by the Flask route.
    Returns (ScreenModel, dict[filename -> content]).
    Raises on any parse / generation error.
    """
    loader = InputLoader()
    bundle = loader.load(
        main_sql_path=main_sql_path,
        additional_sql_paths=additional_sql_paths or [],
        report_path=report_html_path,
        output_zip_path=output_zip_path,
        types_csv_path=types_csv_path,
    )
    model, files = GeneratorEngine().run(bundle)
    return model, files
