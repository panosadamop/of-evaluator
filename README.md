# Oracle Forms & Reports Migration Tool

Analyzes Oracle Forms (.fmt/.xml) and Oracle Reports (.rdf/.xml) complexity (Level 1–3),
then converts them to Java Spring Boot and JasperReports JRXML.

---

## Quick Start

### Requirements
- Python 3.9 or newer
- Internet connection (to install Flask on first run)

### Run the Web App

**Linux / macOS:**
```bash
chmod +x run.sh
./run.sh
```

**Windows:**
```
double-click run.bat
```

Then open **http://localhost:5000** in your browser.

On first run the script will:
1. Create a virtual environment (`venv/`)
2. Install Flask and pytest inside it
3. Generate the sample files
4. Start the app

Subsequent runs just activate the existing venv and start the app.

---

### CLI Usage

**Linux / macOS:**
```bash
chmod +x migrate.sh

./migrate.sh demo                                        # sample files demo
./migrate.sh analyze sample_files/                       # analyze a directory
./migrate.sh analyze my_form.fmt --json-out out.json     # save results as JSON
./migrate.sh convert sample_files/ --target java --output ./out
./migrate.sh convert sample_files/ --target both --output ./out --zip
./migrate.sh pipeline sample_files/ --output ./out
```

**Windows:**
```bat
migrate.bat demo
migrate.bat analyze sample_files\
migrate.bat convert sample_files\ --target both --output .\out --zip
migrate.bat pipeline sample_files\ --output .\out
```

Or activate the venv manually and use `cli.py` directly:
```bash
source venv/bin/activate          # Linux/macOS
venv\Scripts\activate.bat         # Windows

python cli.py analyze sample_files/
python cli.py convert sample_files/ --target both --output ./out --zip
```

---

### Run Tests
```bash
source venv/bin/activate          # Linux/macOS
venv\Scripts\activate.bat         # Windows

pytest tests/ -v
```

---

## Project Structure

```
oracle_migrator_project/
├── run.sh / run.bat           ← Start the web app (creates venv automatically)
├── migrate.sh / migrate.bat   ← CLI wrapper (creates venv automatically)
├── app.py                     ← Flask web application
├── cli.py                     ← Command-line interface
├── requirements.txt           ← flask, pytest
├── setup.py
├── .gitignore                 ← venv/ and outputs excluded
├── sample_files/              ← 6 built-in Oracle Forms & Reports
├── oracle_migrator/
│   ├── core/                  ← Models + complexity analyzer
│   ├── parsers/               ← Oracle Forms & Reports parsers
│   ├── converters/            ← Java Spring Boot + JasperReports generators
│   ├── templates_html/        ← Flask/Jinja2 HTML templates (Bootstrap 5)
│   └── samples.py             ← Built-in sample file content
└── tests/
    └── test_migrator.py       ← Pytest test suite
```

---

## Complexity Levels

| Level | Label    | Characteristics                                      | Est. Effort |
|-------|----------|------------------------------------------------------|-------------|
| 1     | Simple   | ≤3 triggers, no DML, no cursors                      | 1–3 days    |
| 2     | Moderate | DML triggers, validation, exception handling          | 5–10 days   |
| 3     | Complex  | Cursors, dynamic SQL, ON-INSERT/UPDATE/DELETE, loops  | 15+ days    |

## REST API

```bash
curl -X POST http://localhost:5000/api/analyze \
  -F "file=@my_form.fmt" | python -m json.tool
```
