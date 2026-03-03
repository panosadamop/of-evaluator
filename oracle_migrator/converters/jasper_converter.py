"""
Converts Oracle Reports → JasperReports JRXML
"""
import re
from datetime import datetime
from pathlib import Path
from typing import List
from ..core.models import OracleArtifact


ORACLE_TO_JAVA = {
    "VARCHAR2": "java.lang.String", "VARCHAR": "java.lang.String",
    "CHAR": "java.lang.String", "NUMBER": "java.lang.Double",
    "INTEGER": "java.lang.Integer", "DATE": "java.util.Date",
    "TIMESTAMP": "java.util.Date", "BOOLEAN": "java.lang.Boolean",
}


def _j(t: str) -> str:
    return ORACLE_TO_JAVA.get(t.upper().split("(")[0], "java.lang.String")


def _java_name(name: str) -> str:
    parts = re.split(r"[\s_\-]+", name)
    return "".join(p.capitalize() for p in parts if p)


class JasperConverter:
    """Generates JasperReports JRXML from Oracle Reports artifacts."""

    def convert(self, artifact: OracleArtifact, output_dir: str) -> List[str]:
        out = Path(output_dir)
        out.mkdir(parents=True, exist_ok=True)

        files = []

        # Main JRXML
        jrxml = out / f"{artifact.name}.jrxml"
        jrxml.write_text(self._jrxml(artifact))
        files.append(str(jrxml))

        # Java runner
        runner = out / f"{_java_name(artifact.name)}Runner.java"
        runner.write_text(self._runner(artifact))
        files.append(str(runner))

        # pom.xml for standalone Jasper project
        pom = out / "pom.xml"
        pom.write_text(self._pom(artifact))
        files.append(str(pom))

        # application.properties
        props = out / "jasper.properties"
        props.write_text(self._props(artifact))
        files.append(str(props))

        return files

    def _jrxml(self, artifact: OracleArtifact) -> str:
        params = self._params(artifact)
        query = self._query(artifact)
        fields = self._fields(artifact)
        variables = self._variables(artifact)
        bands = self._bands(artifact)

        return f"""<?xml version="1.0" encoding="UTF-8"?>
<!--
  JasperReports JRXML
  Migrated from Oracle Reports: {artifact.name}
  Migration date: {datetime.now().strftime('%Y-%m-%d')}
  Source file: {artifact.file_path}

  HOW TO COMPILE:
    mvn jasperreports:compile-reports
  HOW TO RUN:
    java -cp target/classes:{artifact.name}-runner.jar {_java_name(artifact.name)}Runner
-->
<jasperReport
    xmlns="http://jasperreports.sourceforge.net/jasperreports"
    xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
    xsi:schemaLocation="http://jasperreports.sourceforge.net/jasperreports
        http://jasperreports.sourceforge.net/xsd/jasperreport.xsd"
    name="{artifact.name}"
    pageWidth="595"
    pageHeight="842"
    columnWidth="535"
    leftMargin="30"
    rightMargin="30"
    topMargin="25"
    bottomMargin="25"
    whenNoDataType="AllSectionsNoDetail"
    isFloatColumnFooter="true">

    <!--
      ═══════════════════════════════════════════════════════
        PARAMETERS — migrated from Oracle Reports parameter form
      ═══════════════════════════════════════════════════════
    -->
{params}

    <!--
      ═══════════════════════════════════════════════════════
        QUERY STRING — migrated from Oracle Reports data model
      ═══════════════════════════════════════════════════════
    -->
{query}

    <!--
      ═══════════════════════════════════════════════════════
        FIELDS — mapped from query columns
      ═══════════════════════════════════════════════════════
    -->
{fields}

    <!--
      ═══════════════════════════════════════════════════════
        VARIABLES — migrated from formula columns / summaries
      ═══════════════════════════════════════════════════════
    -->
{variables}

    <!--
      ═══════════════════════════════════════════════════════
        REPORT BANDS
      ═══════════════════════════════════════════════════════
    -->
{bands}

</jasperReport>
"""

    def _params(self, artifact: OracleArtifact) -> str:
        lines = []
        if artifact.parameters:
            for p in artifact.parameters:
                jtype = _j(p.get("type", "VARCHAR2"))
                default = p.get("default", "")
                dexpr = f'\n        <defaultValueExpression><![CDATA["{default}"]]></defaultValueExpression>' if default else ""
                lines.append(
                    f'    <parameter name="{p["name"]}" class="{jtype}" isForPrompting="true">{dexpr}\n    </parameter>'
                )
        else:
            lines = [
                '    <parameter name="P_START_DATE" class="java.util.Date" isForPrompting="true"/>',
                '    <parameter name="P_END_DATE" class="java.util.Date" isForPrompting="true"/>',
                '    <parameter name="P_TITLE" class="java.lang.String" isForPrompting="false">\n'
                f'        <defaultValueExpression><![CDATA["{artifact.name.replace("_", " ").upper()}"]]></defaultValueExpression>\n    </parameter>',
            ]
        return "\n".join(lines)

    def _query(self, artifact: OracleArtifact) -> str:
        if artifact.queries:
            sql = artifact.queries[0].get("sql", "SELECT 1 FROM DUAL")
            sql = re.sub(r"\s+", " ", sql).strip()
        else:
            sql = f"-- TODO: Add SELECT statement\nSELECT * FROM {artifact.name.upper()}"

        return f"""    <queryString language="SQL">
        <![CDATA[{sql}]]>
    </queryString>"""

    def _fields(self, artifact: OracleArtifact) -> str:
        cols = self._extract_cols(artifact)
        lines = []
        for col in sorted(cols):
            lines.append(f'    <field name="{col}" class="java.lang.Object"/>')
        return "\n".join(lines) if lines else '    <!-- TODO: Add fields matching SELECT columns -->'

    def _extract_cols(self, artifact: OracleArtifact) -> set:
        cols = set()
        for q in artifact.queries:
            sql = q.get("sql", "")
            m = re.search(r"SELECT\s+(.+?)\s+FROM", sql, re.I | re.DOTALL)
            if m:
                col_str = m.group(1)
                for col in col_str.split(","):
                    col = col.strip()
                    # Handle aliases: expr AS alias
                    alias_m = re.search(r"\bAS\s+(\w+)\s*$", col, re.I)
                    if alias_m:
                        cols.add(alias_m.group(1).upper())
                    else:
                        # Last word of col expression
                        word = col.split()[-1].split(".")[-1].strip('"\'')
                        if word and word != "*" and re.match(r"^\w+$", word):
                            cols.add(word.upper())
        for block in artifact.blocks:
            for item in block.items:
                cols.add(item.name.upper())
        return cols if cols else {"COLUMN1", "COLUMN2", "COLUMN3"}

    def _variables(self, artifact: OracleArtifact) -> str:
        lines = [
            """    <!-- Row count -->
    <variable name="ROW_COUNT" class="java.lang.Integer"
              resetType="Report" calculation="Count">
        <variableExpression><![CDATA[Boolean.TRUE]]></variableExpression>
    </variable>""",
            """    <!-- Page number -->
    <variable name="PAGE_NUM" class="java.lang.Integer"
              resetType="Page" calculation="Count">
        <variableExpression><![CDATA[Boolean.TRUE]]></variableExpression>
    </variable>""",
        ]

        # Formula column triggers
        for trig in artifact.form_triggers:
            if any(k in trig.name.upper() for k in ("SUM", "TOTAL", "COUNT", "AVG", "FORMULA")):
                var_name = re.sub(r"[\-\s]", "_", trig.name.upper()) + "_VAR"
                code_comment = trig.code[:200].replace("--", "- -")
                lines.append(f"""    <!-- Variable migrated from trigger/formula: {trig.name}
         Original PL/SQL: {code_comment[:80]}... -->
    <variable name="{var_name}" class="java.lang.Double"
              resetType="Report" calculation="Sum">
        <variableExpression><![CDATA[/* TODO: implement {trig.name} */0.0]]></variableExpression>
    </variable>""")

        return "\n\n".join(lines)

    def _bands(self, artifact: OracleArtifact) -> str:
        cols = list(self._extract_cols(artifact))[:8]  # max 8 columns
        col_w = max(50, 535 // max(len(cols), 1))

        # Column headers row
        col_headers = "\n".join(
            f'            <staticText>\n'
            f'                <reportElement x="{i * col_w}" y="0" width="{col_w}" height="16" '
            f'forecolor="#000066"/>\n'
            f'                <textElement><font size="8" isBold="true"/></textElement>\n'
            f'                <text><![CDATA[{c.replace("_", " ").title()}]]></text>\n'
            f'            </staticText>'
            for i, c in enumerate(cols)
        )

        # Detail cells
        detail_cells = "\n".join(
            f'            <textField isBlankWhenNull="true">\n'
            f'                <reportElement x="{i * col_w}" y="0" width="{col_w}" height="16"/>\n'
            f'                <textElement><font size="8"/></textElement>\n'
            f'                <textFieldExpression><![CDATA[$F{{{c}}}]]></textFieldExpression>\n'
            f'            </textField>'
            for i, c in enumerate(cols)
        )

        # Trigger-based notes for before/after report bands
        before_note = ""
        after_note = ""
        for trig in artifact.form_triggers:
            if "BEFORE" in trig.name.upper():
                before_note = f"<!-- Migrated from trigger: {trig.name} -->\n            "
            if "AFTER" in trig.name.upper():
                after_note = f"<!-- Migrated from trigger: {trig.name} -->\n            "

        return f"""    <!-- ── Title Band ─────────────────────────────────────────── -->
    <title>
        <band height="60">
            <rectangle>
                <reportElement x="0" y="0" width="535" height="60" backcolor="#003399" mode="Opaque"/>
            </rectangle>
            <textField>
                <reportElement x="10" y="10" width="515" height="28" forecolor="#FFFFFF"/>
                <textElement textAlignment="Center" verticalAlignment="Middle">
                    <font fontName="DejaVu Sans" size="16" isBold="true"/>
                </textElement>
                <textFieldExpression><![CDATA[$P{{P_TITLE}} != null ? $P{{P_TITLE}} : "{artifact.name.replace('_', ' ').upper()}"]]></textFieldExpression>
            </textField>
            <staticText>
                <reportElement x="10" y="42" width="515" height="14" forecolor="#CCDDFF"/>
                <textElement textAlignment="Center">
                    <font size="8"/>
                </textElement>
                <text><![CDATA[Migrated from Oracle Reports · {artifact.name} · {datetime.now().strftime('%Y-%m-%d')}]]></text>
            </staticText>
        </band>
    </title>

    <!-- ── Page Header ──────────────────────────────────────────── -->
    <pageHeader>
        <band height="30">
            {before_note}<line>
                <reportElement x="0" y="0" width="535" height="1" forecolor="#003399"/>
            </line>
{col_headers}
            <line>
                <reportElement x="0" y="18" width="535" height="1" forecolor="#AAAACC"/>
            </line>
        </band>
    </pageHeader>

    <!-- ── Column Header ────────────────────────────────────────── -->
    <columnHeader>
        <band height="0"/>
    </columnHeader>

    <!-- ── Detail Band — one row per record ─────────────────────── -->
    <detail>
        <band height="18" splitType="Stretch">
            <!-- Alternating row background -->
            <rectangle>
                <reportElement x="0" y="0" width="535" height="18"
                               backcolor="#F2F2EE" mode="Opaque"
                               isPrintWhenDetailOverflows="true">
                    <printWhenExpression><![CDATA[$V{{ROW_COUNT}} % 2 == 0]]></printWhenExpression>
                </reportElement>
            </rectangle>
{detail_cells}
            <line>
                <reportElement x="0" y="17" width="535" height="1" forecolor="#DDDDDD"/>
            </line>
        </band>
    </detail>

    <!-- ── Column Footer ────────────────────────────────────────── -->
    <columnFooter>
        <band height="0"/>
    </columnFooter>

    <!-- ── Page Footer ──────────────────────────────────────────── -->
    <pageFooter>
        <band height="28">
            <line>
                <reportElement x="0" y="2" width="535" height="1" forecolor="#003399"/>
            </line>
            <staticText>
                <reportElement x="0" y="8" width="250" height="14" forecolor="#555555"/>
                <textElement><font size="8"/></textElement>
                <text><![CDATA[{artifact.name} — Confidential]]></text>
            </staticText>
            <textField>
                <reportElement x="380" y="8" width="155" height="14" forecolor="#333333"/>
                <textElement textAlignment="Right"><font size="8"/></textElement>
                <textFieldExpression>
                    <![CDATA["Page " + $V{{PAGE_NUMBER}} + " of " + $V{{PAGE_COUNT}}]]>
                </textFieldExpression>
            </textField>
        </band>
    </pageFooter>

    <!-- ── Last Page Footer ─────────────────────────────────────── -->
    <lastPageFooter>
        <band height="28">
            <line>
                <reportElement x="0" y="2" width="535" height="1" forecolor="#003399"/>
            </line>
            <textField>
                <reportElement x="0" y="8" width="535" height="14" forecolor="#555555"/>
                <textElement textAlignment="Center"><font size="8" isItalic="true"/></textElement>
                <textFieldExpression>
                    <![CDATA["*** END OF REPORT — " + $V{{ROW_COUNT}} + " record(s) ***"]]>
                </textFieldExpression>
            </textField>
        </band>
    </lastPageFooter>

    <!-- ── Summary ──────────────────────────────────────────────── -->
    <summary>
        <band height="25">
            {after_note}<rectangle>
                <reportElement x="0" y="0" width="535" height="25" backcolor="#E8E8F0" mode="Opaque"/>
            </rectangle>
            <staticText>
                <reportElement x="10" y="5" width="150" height="14" forecolor="#000066"/>
                <textElement><font size="9" isBold="true"/></textElement>
                <text><![CDATA[Total Records:]]></text>
            </staticText>
            <textField>
                <reportElement x="160" y="5" width="80" height="14" forecolor="#003399"/>
                <textElement><font size="9" isBold="true"/></textElement>
                <textFieldExpression><![CDATA[$V{{ROW_COUNT}}]]></textFieldExpression>
            </textField>
        </band>
    </summary>"""

    def _runner(self, artifact: OracleArtifact) -> str:
        cn = _java_name(artifact.name)
        params_init = "\n".join([
            f'        params.put("{p["name"]}", /* TODO: provide value */ null);'
            for p in artifact.parameters
        ]) if artifact.parameters else '        // params.put("P_START_DATE", new Date());'

        return f"""import net.sf.jasperreports.engine.*;
import net.sf.jasperreports.engine.export.*;
import net.sf.jasperreports.export.*;
import java.io.*;
import java.sql.*;
import java.util.*;

/**
 * JasperReports Runner for: {artifact.name}
 * Migrated from Oracle Reports
 *
 * Usage:
 *   java -cp "lib/*" {cn}Runner [output.pdf]
 */
public class {cn}Runner {{

    // ── Database config (override via env vars) ────────────────────────
    private static final String DB_URL  = System.getenv().getOrDefault("DB_URL",  "jdbc:h2:mem:testdb");
    private static final String DB_USER = System.getenv().getOrDefault("DB_USER", "sa");
    private static final String DB_PASS = System.getenv().getOrDefault("DB_PASS", "");

    public static void main(String[] args) throws Exception {{
        String outputFile = args.length > 0 ? args[0] : "{artifact.name}.pdf";
        byte[] pdf = generatePdf();
        try (FileOutputStream fos = new FileOutputStream(outputFile)) {{
            fos.write(pdf);
        }}
        System.out.println("Report generated: " + outputFile + " (" + pdf.length + " bytes)");
    }}

    public static byte[] generatePdf() throws Exception {{
        JasperPrint print = fill();
        return JasperExportManager.exportReportToPdf(print);
    }}

    public static void exportToXlsx(String outputPath) throws Exception {{
        JasperPrint print = fill();
        JRXlsxExporter exporter = new JRXlsxExporter();
        exporter.setExporterInput(new SimpleExporterInput(print));
        exporter.setExporterOutput(new SimpleOutputStreamExporterOutput(outputPath));
        SimpleXlsxReportConfiguration config = new SimpleXlsxReportConfiguration();
        config.setOnePagePerSheet(false);
        exporter.setConfiguration(config);
        exporter.exportReport();
        System.out.println("Excel exported: " + outputPath);
    }}

    private static JasperPrint fill() throws Exception {{
        // Load compiled report (compile .jrxml first with JasperCompileManager)
        InputStream is = {cn}Runner.class.getResourceAsStream("/{artifact.name}.jasper");
        if (is == null) {{
            // Try to compile on the fly
            is = {cn}Runner.class.getResourceAsStream("/{artifact.name}.jrxml");
            if (is == null) throw new FileNotFoundException("{artifact.name}.jrxml not found on classpath");
            JasperReport report = JasperCompileManager.compileReport(is);
            return fillReport(report);
        }}
        JasperReport report = (JasperReport) JRLoader.loadObject(is);
        return fillReport(report);
    }}

    private static JasperPrint fillReport(JasperReport report) throws Exception {{
        // Build parameters
        Map<String, Object> params = new HashMap<>();
{params_init}

        // Connect and fill
        try (Connection conn = DriverManager.getConnection(DB_URL, DB_USER, DB_PASS)) {{
            return JasperFillManager.fillReport(report, params, conn);
        }}
    }}
}}
"""

    def _pom(self, artifact: OracleArtifact) -> str:
        return f"""<?xml version="1.0" encoding="UTF-8"?>
<project xmlns="http://maven.apache.org/POM/4.0.0"
         xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
         xsi:schemaLocation="http://maven.apache.org/POM/4.0.0
         https://maven.apache.org/xsd/maven-4.0.0.xsd">
    <modelVersion>4.0.0</modelVersion>

    <groupId>com.migrated.reports</groupId>
    <artifactId>{artifact.name.lower()}-report</artifactId>
    <version>1.0.0</version>
    <n>{artifact.name} — Migrated Oracle Report</n>

    <properties>
        <java.version>17</java.version>
        <jasperreports.version>6.21.0</jasperreports.version>
    </properties>

    <dependencies>
        <dependency>
            <groupId>net.sf.jasperreports</groupId>
            <artifactId>jasperreports</artifactId>
            <version>${{jasperreports.version}}</version>
        </dependency>
        <dependency>
            <groupId>net.sf.jasperreports</groupId>
            <artifactId>jasperreports-pdf</artifactId>
            <version>${{jasperreports.version}}</version>
        </dependency>
        <dependency>
            <groupId>net.sf.jasperreports</groupId>
            <artifactId>jasperreports-xlsx</artifactId>
            <version>${{jasperreports.version}}</version>
        </dependency>
        <dependency>
            <groupId>com.h2database</groupId>
            <artifactId>h2</artifactId>
            <version>2.2.224</version>
        </dependency>
    </dependencies>

    <build>
        <plugins>
            <plugin>
                <groupId>com.alexnederlof</groupId>
                <artifactId>jasperreports-plugin</artifactId>
                <version>2.6</version>
                <executions>
                    <execution>
                        <phase>process-sources</phase>
                        <goals><goal>jasper</goal></goals>
                        <configuration>
                            <sourceDirectory>src/main/resources/reports</sourceDirectory>
                            <outputDirectory>${{project.build.outputDirectory}}/reports</outputDirectory>
                        </configuration>
                    </execution>
                </executions>
            </plugin>
        </plugins>
    </build>
</project>
"""

    def _props(self, artifact: OracleArtifact) -> str:
        return f"""# JasperReports configuration for: {artifact.name}
report.name={artifact.name}
report.output.formats=PDF,XLSX,HTML

db.url=jdbc:oracle:thin:@localhost:1521/ORCL
db.user=your_username
db.password=your_password

# For local testing with H2:
# db.url=jdbc:h2:mem:testdb
# db.user=sa
# db.password=
"""
