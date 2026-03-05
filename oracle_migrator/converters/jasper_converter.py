"""
Converts Oracle Reports → JasperReports WAR project
modelled on the ssp.efka.template pattern (Intrasoft / KEAO):

  Generated structure mirrors ssp.efka.template exactly:
  ─ service/JasperEngine.java          static engine, ExecutorService 60s timeout
  ─ dto/<Name>ReportDTO.java           outer DTO + non-static inner TableDTO
  ─ controller/<Name>ReportController  extends AbstractCommonInternalParentController
  ─ controller/AbstractCommonInternalParentController.java
  ─ bean/<Name>Bean.java               @ViewScoped @Named (locale / calendarTypeFlg)
  ─ reports/<artifact>/<artifact>.jrxml  landscape A4, subDataset jr:table, DejaVu Sans
  ─ pom.xml                            Jakarta EE 10, jasperreports, Java 21, war plugin
  ─ persistence.xml                    JTA datasource, Hibernate/Wildfly style
  ─ jboss-deployment-structure.xml
  ─ jboss-web.xml
  ─ web.xml                            JSF/Primefaces, OIDC security
  ─ jasperreports.properties
  ─ beans.xml / faces-config.xml
  ─ microprofile-config.properties
  ─ log4j2.xml
"""
import re
import uuid as _uuid_mod
from datetime import datetime
from pathlib import Path
from typing import List
from ..core.models import OracleArtifact


# ── type helpers ─────────────────────────────────────────────────────────────

ORACLE_TO_JAVA = {
    "VARCHAR2": "String", "VARCHAR": "String", "CHAR": "String",
    "NVARCHAR2": "String", "CLOB": "String", "LONG": "String",
    "NUMBER": "BigDecimal", "FLOAT": "Double",
    "INTEGER": "Long", "INT": "Long",
    "DATE": "String", "TIMESTAMP": "String",
    "BOOLEAN": "Boolean",
    "BLOB": "byte[]", "RAW": "byte[]",
}

JAVA_FULL = {
    "String": "java.lang.String", "BigDecimal": "java.math.BigDecimal",
    "Double": "java.lang.Double", "Long": "java.lang.Long",
    "Integer": "java.lang.Integer", "Boolean": "java.lang.Boolean",
    "byte[]": "byte[]",
}


def _cn(name: str) -> str:
    """ORACLE_NAME → OracleName (strips trailing _REPORT/_FORM to avoid doubling)"""
    clean = re.sub(r"(?i)[_\-]?(REPORT|FORM|RPT)$", "", name)
    return "".join(p.capitalize() for p in re.split(r"[\s_\-]+", clean) if p)


def _cc(name: str) -> str:
    """ORACLE_NAME → oracleName"""
    parts = re.split(r"[\s_\-]+", name.lower())
    return parts[0] + "".join(p.capitalize() for p in parts[1:]) if parts else name


def _jt(oracle_type: str) -> str:
    return ORACLE_TO_JAVA.get(oracle_type.upper().split("(")[0], "String")


def _jrxml_type(java_short: str) -> str:
    return JAVA_FULL.get(java_short, "java.lang.String")


def _pkg(cn: str) -> str:
    return f"com.intrasoft.ssp.efka.{cn.lower()}"


def _artifact_id(name: str) -> str:
    return f"ssp.efka.{name.lower().replace('_', '.')}"


def _uuid() -> str:
    return str(_uuid_mod.uuid4())


# ── converter ─────────────────────────────────────────────────────────────────

class JasperConverter:
    """
    Generates a full ssp.efka-style Jakarta EE WAR from an Oracle Reports artifact.
    Output mirrors ssp.efka.template project layout exactly.
    """

    def convert(self, artifact: OracleArtifact, output_dir: str) -> List[str]:
        out   = Path(output_dir)
        cn    = _cn(artifact.name)
        pkg   = _pkg(cn)
        aid   = _artifact_id(artifact.name)
        cols  = self._extract_cols(artifact)
        lname = artifact.name.lower()

        # ── directory tree ────────────────────────────────────────────────────
        java_root  = out / "src/main/java" / pkg.replace(".", "/")
        svc_root   = java_root / "service"
        dto_root   = java_root / "dto"
        bean_root  = java_root / "bean"
        ctrl_root  = java_root / "controller"
        res_root   = out / "src/main/resources"
        meta_root  = res_root / "META-INF"
        rpt_root   = res_root / "reports" / lname
        web_root   = out / "src/main/webapp"
        webinf     = web_root / "WEB-INF"
        views      = web_root / "views" / "reports"

        for d in [java_root, svc_root, dto_root, bean_root, ctrl_root,
                  meta_root, rpt_root, webinf, views]:
            d.mkdir(parents=True, exist_ok=True)

        files = []

        def w(path: Path, content: str):
            path.write_text(content, encoding="utf-8")
            files.append(str(path))

        # ── Java sources ──────────────────────────────────────────────────────
        w(svc_root  / "JasperEngine.java",
          self._jasper_engine(pkg))
        w(dto_root  / f"{cn}ReportDTO.java",
          self._dto(artifact, cn, pkg, cols))
        w(bean_root / f"{cn}Bean.java",
          self._bean(cn, pkg))
        w(ctrl_root / "AbstractCommonInternalParentController.java",
          self._abstract_controller(pkg))
        w(ctrl_root / f"{cn}ReportController.java",
          self._controller(artifact, cn, pkg))

        # ── JRXML ─────────────────────────────────────────────────────────────
        w(rpt_root / f"{lname}.jrxml",
          self._jrxml(artifact, cn, cols))

        # ── Maven POM ─────────────────────────────────────────────────────────
        w(out / "pom.xml",
          self._pom(artifact, cn, aid))

        # ── Jakarta EE / Wildfly config ───────────────────────────────────────
        w(meta_root / "persistence.xml",                self._persistence_xml(cn))
        w(meta_root / "microprofile-config.properties", self._microprofile(artifact, cn))
        w(res_root  / "jasperreports.properties",       self._jr_props())
        w(res_root  / "log4j2.xml",                     self._log4j2(cn, pkg))

        w(webinf / "web.xml",                        self._web_xml(artifact, cn))
        w(webinf / "jboss-web.xml",                  self._jboss_web(artifact))
        w(webinf / "jboss-deployment-structure.xml", self._jboss_ds())
        w(webinf / "beans.xml",                      self._beans_xml())
        w(webinf / "faces-config.xml",               self._faces_config(pkg, cn))

        # ── JSF view ──────────────────────────────────────────────────────────
        w(views / f"{lname}.xhtml",
          self._xhtml(artifact, cn))

        # ── README ────────────────────────────────────────────────────────────
        w(out / "README.md",
          self._readme(artifact, cn, aid))

        return files

    # ══════════════════════════════════════════════════════════════════════════
    #  Java sources
    # ══════════════════════════════════════════════════════════════════════════

    def _jasper_engine(self, pkg: str) -> str:
        """
        Mirrors com.intrasoft.ssp.efka.service.JasperEngine exactly:
          – same method signatures (renderReport / compileBatchReport / renderBatchReport / produceJasperPrintReport)
          – same ExecutorService concurrency pattern with 60-second timeout
          – MimeType parameter kept as String here (avoids pulling in ssp.shared dependency)
        """
        return f"""\
package {pkg}.service;

import net.sf.jasperreports.engine.*;
import net.sf.jasperreports.engine.data.JRBeanCollectionDataSource;
import org.apache.logging.log4j.LogManager;
import org.apache.logging.log4j.Logger;

import java.sql.Connection;
import java.util.Collection;
import java.util.HashMap;
import java.util.Map;
import java.util.ResourceBundle;
import java.util.concurrent.*;

/**
 * Static JasperReports rendering engine.
 *
 * Mirrors com.intrasoft.ssp.efka.service.JasperEngine from
 * ssp.efka.template (Intrasoft / KEAO) exactly:
 * <ul>
 *   <li>compiles JRXML from the classloader (not pre-compiled .jasper)</li>
 *   <li>fills via JRBeanCollectionDataSource OR a JDBC Connection</li>
 *   <li>wraps the fill in a single-thread ExecutorService with a
 *       60-second timeout — same concurrency model as the template</li>
 * </ul>
 *
 * Migrated by oracle-migrator Oracle Migrator Tool — {datetime.now().strftime('%Y-%m-%d')}
 */
public class JasperEngine implements java.io.Serializable {{

    private static final Logger logger = LogManager.getLogger(JasperEngine.class);

    /**
     * Compile + fill in one call (single-use, non-batch).
     * Signature mirrors ssp.efka.template renderReport().
     */
    public static JasperPrint renderReport(String templatePath,
                                           ResourceBundle resourceBundle,
                                           Map<String, Object> parameters,
                                           Collection<?> dataSourceJavaBeanCollection,
                                           Connection jdbcConnection,
                                           String name,
                                           String mimeTypeEnum,
                                           Boolean expectCompiledReport) {{
        if (parameters == null) parameters = new HashMap<>();
        try {{
            JasperReport jasperReport = JasperCompileManager.compileReport(
                JasperEngine.class.getClassLoader().getResourceAsStream(templatePath));
            return produceJasperPrintReport(
                jasperReport, parameters, dataSourceJavaBeanCollection,
                mimeTypeEnum, jdbcConnection);
        }} catch (JRException e) {{
            throw new UnsupportedOperationException("Failed to render report: " + templatePath, e);
        }}
    }}

    /**
     * Pre-compile for batch / multi-page use.
     * Mirrors ssp.efka.template compileBatchReport().
     */
    public static JasperReport compileBatchReport(String templatePath) {{
        try {{
            return JasperCompileManager.compileReport(
                JasperEngine.class.getClassLoader().getResourceAsStream(templatePath));
        }} catch (JRException e) {{
            throw new UnsupportedOperationException("Failed to compile report: " + templatePath, e);
        }}
    }}

    /**
     * Fill a pre-compiled report (batch variant).
     * Mirrors ssp.efka.template renderBatchReport().
     */
    public static JasperPrint renderBatchReport(JasperReport jasperReport,
                                                ResourceBundle resourceBundle,
                                                Map<String, Object> parameters,
                                                Collection<?> dataSourceJavaBeanCollection,
                                                Connection jdbcConnection,
                                                String name,
                                                String mimeTypeEnum,
                                                Boolean expectCompiledReport) throws JRException {{
        if (parameters == null) parameters = new HashMap<>();
        return produceJasperPrintReport(
            jasperReport, parameters, dataSourceJavaBeanCollection,
            mimeTypeEnum, jdbcConnection);
    }}

    /**
     * Core fill method.
     * Runs in a dedicated thread with a 60-second timeout — same model
     * as ssp.efka.template produceJasperPrintReport().
     */
    public static JasperPrint produceJasperPrintReport(JasperReport jasperReport,
                                                       Map<String, Object> parameters,
                                                       Collection<?> dataSourceJavaBeanCollection,
                                                       String mimeType,
                                                       Connection connection) throws JRException {{
        try (ExecutorService executor = Executors.newSingleThreadExecutor()) {{
            Future<JasperPrint> future = executor.submit(() -> {{
                try {{
                    if (dataSourceJavaBeanCollection != null) {{
                        return JasperFillManager.fillReport(
                            jasperReport, parameters,
                            new JRBeanCollectionDataSource(dataSourceJavaBeanCollection));
                    }} else {{
                        return JasperFillManager.fillReport(jasperReport, parameters, connection);
                    }}
                }} catch (JRException e) {{
                    logger.error("Error when preparing report", e);
                    throw e;
                }}
            }});
            return future.get(60, TimeUnit.SECONDS);
        }} catch (ExecutionException e) {{
            throw new RuntimeException("Report fill failed", e);
        }} catch (InterruptedException e) {{
            Thread.currentThread().interrupt();
            throw new RuntimeException("Report fill interrupted", e);
        }} catch (TimeoutException e) {{
            throw new RuntimeException("Report fill timed out after 60 seconds", e);
        }}
    }}
}}
"""

    def _dto(self, artifact: OracleArtifact, cn: str, pkg: str, cols: list) -> str:
        """
        Mirrors DebtorsForProclamationReportDTO:
          – outer class = one JRBeanCollectionDataSource row
          – non-static inner class TableDTO = sub-dataset rows, instantiated via record.new TableDTO()
          – LinkedList<TableDTO> tableDTO field
        """
        params = artifact.parameters if artifact.parameters else [
            {"name": "P_DATE_FROM",   "type": "VARCHAR2"},
            {"name": "P_DATE_TO",     "type": "VARCHAR2"},
            {"name": "P_BRANCH_NAME", "type": "VARCHAR2"},
            {"name": "P_USER_EMAIL",  "type": "VARCHAR2"},
        ]

        outer_fields = ""
        outer_getset = ""
        for p in params:
            fname = _cc(p["name"].lstrip("P_"))
            jtype = _jt(p.get("type", "VARCHAR2"))
            outer_fields += f"    private {jtype} {fname};\n"
            cap = fname[0].upper() + fname[1:]
            outer_getset += (
                f"    public {jtype} get{cap}() {{ return {fname}; }}\n"
                f"    public void set{cap}({jtype} {fname}) {{ this.{fname} = {fname}; }}\n"
            )

        inner_fields = ""
        inner_getset = ""
        for col in cols:
            fname = _cc(col["name"])
            jtype = col["jtype"]
            inner_fields += f"        {jtype} {fname};\n"
            cap = fname[0].upper() + fname[1:]
            inner_getset += (
                f"        public {jtype} get{cap}() {{ return {fname}; }}\n"
                f"        public void set{cap}({jtype} {fname}) {{ this.{fname} = {fname}; }}\n"
            )

        return f"""\
package {pkg}.dto;

import java.io.Serializable;
import java.math.BigDecimal;
import java.util.LinkedList;

/**
 * Report DTO for {artifact.name}.
 *
 * Mirrors DebtorsForProclamationReportDTO from ssp.efka.template:
 * <ul>
 *   <li>outer object  = one row fed to the main JRBeanCollectionDataSource</li>
 *   <li>non-static inner TableDTO = the list fed to the sub-dataset via
 *       $F{{tableDTO, instantiated with record.new TableDTO()</li>
 * </ul>
 *
 * Migrated from Oracle Reports: {artifact.file_path}
 * Migration date: {datetime.now().strftime('%Y-%m-%d')}
 */
public class {cn}ReportDTO implements Serializable {{

    // ── Header / filter fields (from Oracle Reports parameters) ─────────────
{outer_fields}
    // ── Detail rows (sub-dataset, mirrors tableDTO field in template DTO) ────
    private LinkedList<TableDTO> tableDTO = new LinkedList<>();

    public LinkedList<TableDTO> getTableDTO() {{ return tableDTO; }}
    public void setTableDTO(LinkedList<TableDTO> tableDTO) {{ this.tableDTO = tableDTO; }}

{outer_getset}
    // ════════════════════════════════════════════════════════════════════════
    //  Non-static inner class — one instance per detail row.
    //  Bound to the JRXML subDataset via:
    //    <dataSourceExpression>
    //      new net.sf.jasperreports.engine.data.JRBeanCollectionDataSource($F{{tableDTO}})
    //    </dataSourceExpression>
    //
    //  Instantiate with: {cn}ReportDTO.TableDTO row = record.new TableDTO();
    // ════════════════════════════════════════════════════════════════════════
    public class TableDTO implements Serializable {{
{inner_fields}
{inner_getset}
    }}
}}
"""

    def _bean(self, cn: str, pkg: str) -> str:
        """
        Mirrors DashboardBean: @ViewScoped @Named with locale + calendarTypeFlg.
        """
        bc = _cc(cn) + "Bean"
        return f"""\
package {pkg}.bean;

import jakarta.faces.view.ViewScoped;
import jakarta.inject.Named;
import lombok.Data;
import lombok.Getter;
import lombok.Setter;

import java.io.Serializable;

/**
 * JSF view-scoped bean for {cn} report.
 * Mirrors DashboardBean from ssp.efka.template.
 */
@Data
@Getter
@Setter
@ViewScoped
@Named(value = "{bc}")
public class {cn}Bean implements Serializable {{

    /** Locale string (e.g. "el", "en") — set from UserSessionBean in controller. */
    private String locale;

    /**
     * Calendar type flag — used by the UI date pickers.
     * Mirrored from UserSessionBean.getCalendarType().
     */
    private String calendarTypeFlg;
}}
"""

    def _abstract_controller(self, pkg: str) -> str:
        """
        Mirrors AbstractCommonInternalParentController from ssp.efka.template.
        """
        return f"""\
package {pkg}.controller;

import lombok.Getter;
import lombok.Setter;
import org.apache.logging.log4j.LogManager;
import org.apache.logging.log4j.Logger;

/**
 * Abstract base controller for ssp.efka report controllers.
 *
 * Mirrors AbstractCommonInternalParentController from ssp.efka.template.
 * In the real SSP stack this extends PageControllerParent which provides
 * panel/navigation state management — replaced here with a no-op base.
 *
 * Migrated by oracle-migrator Oracle Migrator Tool — {datetime.now().strftime('%Y-%m-%d')}
 */
@Getter
@Setter
public abstract class AbstractCommonInternalParentController {{

    private static final Logger logger =
        LogManager.getLogger(AbstractCommonInternalParentController.class);

    public static final String MAIN_PANEL_NAME = "mainPanel";

    /** Override to validate the current panel state before save. */
    public abstract boolean validate(String panelName);

    /** Override to persist the model for the given panel. */
    public abstract void saveInternal(String panelName);

    /** Override to build the domain model for the given panel. */
    public abstract void createObjectModel(String panelName);

    /** Override to load master records into the panel. */
    public abstract void getMasterRecords();
}}
"""

    def _controller(self, artifact: OracleArtifact, cn: str, pkg: str) -> str:
        """
        Mirrors DashboardController from ssp.efka.template:
          – extends AbstractCommonInternalParentController
          – @Inject UserSessionBean + <cn>Bean
          – @PostConstruct initializes locale/calendar from session
          – printReport() uses JasperCompileManager + JRPdfExporter + ByteArrayOutputStream
          – exportXlsx() variant
          – createDummyRecord() fixture
          – streamToResponse() helper (replaces HttpRequestUtil.httpRequestForBinaryFile)
        """
        jrxml_path = f"reports/{artifact.name.lower()}/{artifact.name.lower()}.jrxml"
        bc = _cc(cn) + "Bean"
        cc_ctrl = _cc(cn) + "ReportController"

        return f"""\
package {pkg}.controller;

import {pkg}.bean.{cn}Bean;
import {pkg}.dto.{cn}ReportDTO;
import {pkg}.service.JasperEngine;
import jakarta.annotation.PostConstruct;
import jakarta.faces.context.FacesContext;
import jakarta.faces.view.ViewScoped;
import jakarta.inject.Inject;
import jakarta.inject.Named;
import jakarta.servlet.http.HttpServletResponse;
import lombok.Getter;
import lombok.Setter;
import net.sf.jasperreports.engine.*;
import net.sf.jasperreports.engine.data.JRBeanCollectionDataSource;
import net.sf.jasperreports.engine.export.JRPdfExporter;
import net.sf.jasperreports.engine.export.ooxml.JRXlsxExporter;
import net.sf.jasperreports.export.*;
import org.apache.logging.log4j.LogManager;
import org.apache.logging.log4j.Logger;

import java.io.*;
import java.math.BigDecimal;
import java.time.format.DateTimeFormatter;
import java.util.*;

/**
 * JSF @ViewScoped controller for {artifact.name} report.
 *
 * Mirrors DashboardController from ssp.efka.template:
 * <ul>
 *   <li>extends AbstractCommonInternalParentController</li>
 *   <li>&#64;Inject UserSessionBean (stub) + {cn}Bean</li>
 *   <li>@PostConstruct calls initializeUserSessionPreferences()</li>
 *   <li>printReport() — compile JRXML, fill, export to PDF, stream to browser</li>
 *   <li>exportXlsx() — same pipeline, XLSX output</li>
 *   <li>createDummyRecord() — sample data fixture, replace with real EJB call</li>
 * </ul>
 *
 * Migrated from Oracle Reports: {artifact.file_path}
 * Migration date: {datetime.now().strftime('%Y-%m-%d')}
 */
@Getter
@Setter
@ViewScoped
@Named(value = "{cc_ctrl}")
public class {cn}ReportController extends AbstractCommonInternalParentController
    implements Serializable {{

    private static final long serialVersionUID = 1L;
    private static final Logger logger = LogManager.getLogger({cn}ReportController.class);
    private static final String JRXML_PATH = "{jrxml_path}";

    private transient DateTimeFormatter formatter = DateTimeFormatter.ofPattern("dd/MM/yyyy");

    /**
     * In the full SSP stack inject the real UserSessionBean.
     * Kept as a typed stub here to compile cleanly without the SSP dependency.
     */
    // @Inject
    // protected UserSessionBean userSessionBean;

    @Inject
    protected {cn}Bean {bc};

    private boolean messageRender = false;

    public {cn}ReportController() {{ super(); }}

    @PostConstruct
    private void initialize() {{
        initializeUserSessionPreferences();
    }}

    /**
     * Mirrors DashboardController.initializeUserSessionPreferences().
     * Reads locale + calendarType from the session bean.
     */
    private void initializeUserSessionPreferences() {{
        try {{
            // {bc}.setLocale(userSessionBean.getLang().toLowerCase());
            // {bc}.setCalendarTypeFlg(userSessionBean.getCalendarType());
            {bc}.setLocale("el");
            {bc}.setCalendarTypeFlg("GR");
        }} catch (Exception ex) {{
            logger.debug("{cn}ReportController: session preferences not available.");
        }}
    }}

    // ── PDF export ────────────────────────────────────────────────────────────

    /**
     * Compile the JRXML, fill with a JRBeanCollectionDataSource, export to PDF
     * and stream to the browser.
     *
     * Pattern mirrors DashboardController.printReport() in ssp.efka.template.
     */
    public void printReport() throws JRException {{
        JasperReport compiled = JasperCompileManager.compileReport(
            JasperEngine.class.getClassLoader().getResourceAsStream(JRXML_PATH));

        System.setProperty("net.sf.jasperreports.debug", "true");

        Collection<{cn}ReportDTO> data = Arrays.asList(createDummyRecord());
        Map<String, Object> parameters = new HashMap<>();
        parameters.put("SUBREPORT_ALLOW_SPLIT", Boolean.TRUE);

        JasperPrint jasperPrint = JasperFillManager.fillReport(
            compiled, parameters,
            new JRBeanCollectionDataSource(data));

        JRPdfExporter exporter = new JRPdfExporter();
        List<JasperPrint> prints = new ArrayList<>();
        prints.add(jasperPrint);
        exporter.setExporterInput(SimpleExporterInput.getInstance(prints));

        SimplePdfExporterConfiguration pdfCfg = new SimplePdfExporterConfiguration();
        String filename = "{artifact.name}.pdf";
        pdfCfg.setMetadataTitle(filename);
        exporter.setConfiguration(pdfCfg);

        ByteArrayOutputStream os = new ByteArrayOutputStream();
        exporter.setExporterOutput(new SimpleOutputStreamExporterOutput(os));
        exporter.exportReport();

        // In real SSP stack: HttpRequestUtil.httpRequestForBinaryFile(filename, os.toByteArray(), MimeType.PDF.getDescription())
        streamToResponse(filename, os.toByteArray(), "application/pdf");
    }}

    // ── XLSX export ───────────────────────────────────────────────────────────

    /**
     * Same pipeline as printReport() but exports to Excel (XLSX).
     */
    public void exportXlsx() throws JRException {{
        JasperReport compiled = JasperCompileManager.compileReport(
            JasperEngine.class.getClassLoader().getResourceAsStream(JRXML_PATH));

        Collection<{cn}ReportDTO> data = Arrays.asList(createDummyRecord());
        Map<String, Object> parameters = new HashMap<>();

        JasperPrint jasperPrint = JasperFillManager.fillReport(
            compiled, parameters,
            new JRBeanCollectionDataSource(data));

        JRXlsxExporter exporter = new JRXlsxExporter();
        exporter.setExporterInput(new SimpleExporterInput(jasperPrint));

        SimpleXlsxReportConfiguration xlsxCfg = new SimpleXlsxReportConfiguration();
        xlsxCfg.setOnePagePerSheet(false);
        xlsxCfg.setDetectCellType(true);
        xlsxCfg.setRemoveEmptySpaceBetweenRows(true);
        exporter.setConfiguration(xlsxCfg);

        ByteArrayOutputStream os = new ByteArrayOutputStream();
        exporter.setExporterOutput(new SimpleOutputStreamExporterOutput(os));
        exporter.exportReport();

        String filename = "{artifact.name}.xlsx";
        streamToResponse(filename, os.toByteArray(),
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet");
    }}

    // ── Dummy data fixture ────────────────────────────────────────────────────

    /**
     * Creates a sample DTO record for development / demo.
     * Mirrors createDummyRecord() in DashboardController.
     * Replace with a real @EJB / @Inject service call before deploying.
     */
    public {cn}ReportDTO createDummyRecord() {{
        {cn}ReportDTO record = new {cn}ReportDTO();
{self._dummy_outer(artifact)}
        // Detail rows — non-static inner class, same as DebtorsForProclamationReportDTO.TableDTO
{self._dummy_rows(artifact, cn)}
        return record;
    }}

    // ── HTTP streaming helper ─────────────────────────────────────────────────

    /**
     * Writes the byte array to the current HTTP response.
     * Replaces HttpRequestUtil.httpRequestForBinaryFile() from the SSP stack.
     */
    private void streamToResponse(String filename, byte[] bytes, String mimeType) {{
        try {{
            FacesContext fc = FacesContext.getCurrentInstance();
            HttpServletResponse response =
                (HttpServletResponse) fc.getExternalContext().getResponse();
            response.reset();
            response.setContentType(mimeType);
            response.setContentLength(bytes.length);
            response.setHeader("Content-Disposition",
                "attachment; filename=\\"" + filename + "\\"");
            response.getOutputStream().write(bytes);
            response.getOutputStream().flush();
            fc.responseComplete();
        }} catch (IOException e) {{
            logger.error("Failed to stream report response", e);
            throw new UncheckedIOException(e);
        }}
    }}

    // ── AbstractCommonInternalParentController stubs ──────────────────────────

    @Override public boolean validate(String panelName) {{ return true; }}
    @Override public void saveInternal(String panelName) {{}}
    @Override public void createObjectModel(String panelName) {{}}
    @Override public void getMasterRecords() {{}}
}}
"""

    # ══════════════════════════════════════════════════════════════════════════
    #  JRXML
    # ══════════════════════════════════════════════════════════════════════════

    def _jrxml(self, artifact: OracleArtifact, cn: str, cols: list) -> str:
        now = datetime.now().strftime("%Y-%m-%d")
        lname = artifact.name.lower()
        params = artifact.parameters if artifact.parameters else [
            {"name": "P_DATE_FROM",   "type": "VARCHAR2"},
            {"name": "P_DATE_TO",     "type": "VARCHAR2"},
            {"name": "P_BRANCH_NAME", "type": "VARCHAR2"},
            {"name": "P_USER_EMAIL",  "type": "VARCHAR2"},
        ]

        # Outer JRXML field declarations (from parameters + tableDTO list)
        outer_field_xml = ""
        for p in params:
            fname = _cc(p["name"].lstrip("P_"))
            jfull = _jrxml_type(_jt(p.get("type", "VARCHAR2")))
            outer_field_xml += f'\t<field name="{fname}" class="{jfull}"/>\n'
        outer_field_xml += '\t<field name="tableDTO" class="java.util.List"/>\n'

        # Sub-dataset field declarations
        sub_fields_xml = ""
        for col in cols:
            sub_fields_xml += (
                f'\t\t<field name="{_cc(col["name"])}" '
                f'class="{_jrxml_type(col["jtype"])}"/>\n'
            )

        # Column width — landscape A4 = 802px usable
        n = max(len(cols), 1)
        col_w = max(40, 802 // n)
        table_cols_xml = self._jrxml_table_cols(cols, col_w)

        # Title band header text fields from parameters
        # Mirrors the branchName/userEmail text block in debtorsForProclamationReport.jrxml
        header_fields_expr = ""
        for p in params:
            fname = _cc(p["name"].lstrip("P_"))
            label = p["name"].replace("P_", "").replace("_", " ").title()
            header_fields_expr += f'"{label}: " + ($F{{{fname}}} != null ? $F{{{fname}}} : "") + "\\n" +\n'
        # strip trailing + \n
        header_fields_expr = header_fields_expr.rstrip().rstrip("+").rstrip()

        report_title = artifact.name.replace("_", " ").upper()

        return f"""\
<?xml version="1.0" encoding="UTF-8"?>
<!--
  JasperReports JRXML — generated by oracle-migrator Oracle Migrator Tool
  Template pattern : ssp.efka.template / debtorsForProclamationReport.jrxml (Intrasoft / KEAO)

  Source           : {artifact.name}  ({artifact.file_path})
  Generated        : {now}

  DATA FLOW:
    {cn}ReportController.printReport()
      └─ JasperFillManager.fillReport(compiled, params,
             new JRBeanCollectionDataSource(List<{cn}ReportDTO>))
              │
              └─ subDataset "tableDTO"
                   └─ new JRBeanCollectionDataSource($F{{tableDTO}})
                        maps to inner {cn}ReportDTO.TableDTO
-->
<jasperReport
    xmlns="http://jasperreports.sourceforge.net/jasperreports"
    xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
    xsi:schemaLocation="http://jasperreports.sourceforge.net/jasperreports
        http://jasperreports.sourceforge.net/xsd/jasperreport.xsd"
    name="{lname}"
    pageWidth="842" pageHeight="595" orientation="Landscape"
    columnWidth="802" leftMargin="20" rightMargin="20"
    topMargin="20" bottomMargin="20"
    whenNoDataType="AllSectionsNoDetail"
    uuid="{_uuid()}">

\t<property name="com.jaspersoft.studio.unit."          value="pixel"/>
\t<property name="com.jaspersoft.studio.unit.pageHeight" value="pixel"/>
\t<property name="com.jaspersoft.studio.unit.pageWidth"  value="pixel"/>
\t<property name="com.jaspersoft.studio.unit.topMargin"  value="pixel"/>
\t<property name="com.jaspersoft.studio.unit.bottomMargin" value="pixel"/>
\t<property name="com.jaspersoft.studio.unit.leftMargin" value="pixel"/>
\t<property name="com.jaspersoft.studio.unit.rightMargin" value="pixel"/>
\t<property name="com.jaspersoft.studio.unit.columnWidth" value="pixel"/>

\t<!-- ═══════════════════════════════════════════════════════════════════════
\t     SUB-DATASET — mirrors tableDTO subDataset in debtorsForProclamationReport.jrxml
\t     Fields map to {cn}ReportDTO.TableDTO properties.
\t     ═══════════════════════════════════════════════════════════════════════ -->
\t<subDataset name="tableDTO" uuid="{_uuid()}">
\t\t<property name="com.jaspersoft.studio.data.defaultdataadapter" value="One Empty Record"/>
\t\t<queryString><![CDATA[]]></queryString>
{sub_fields_xml}\t</subDataset>

\t<!-- ═══════════════════════════════════════════════════════════════════════
\t     MAIN QUERY — empty; data provided via JRBeanCollectionDataSource
\t     ═══════════════════════════════════════════════════════════════════════ -->
\t<queryString><![CDATA[]]></queryString>

\t<!-- ═══════════════════════════════════════════════════════════════════════
\t     OUTER FIELDS — map to {cn}ReportDTO properties
\t     ═══════════════════════════════════════════════════════════════════════ -->
{outer_field_xml}
\t<background><band splitType="Stretch"/></background>

\t<!-- ═══════════════════════════════════════════════════════════════════════
\t     TITLE BAND — report header, date (top-right), page counter
\t     Mirrors title band structure of debtorsForProclamationReport.jrxml
\t     ═══════════════════════════════════════════════════════════════════════ -->
\t<title>
\t\t<band height="210" splitType="Stretch">

\t\t\t<!-- Date label (top-right) — mirrors staticText "Ημερομηνία:" -->
\t\t\t<staticText>
\t\t\t\t<reportElement x="652" y="0" width="58" height="10" uuid="{_uuid()}"/>
\t\t\t\t<textElement>
\t\t\t\t\t<font fontName="DejaVu Sans" size="8" isBold="true"/>
\t\t\t\t</textElement>
\t\t\t\t<text><![CDATA[Ημερομηνία:]]></text>
\t\t\t</staticText>

\t\t\t<!-- Current date (top-right) — mirrors textField with new java.util.Date() -->
\t\t\t<textField pattern="dd/MM/yyyy">
\t\t\t\t<reportElement x="716" y="-2" width="100" height="21" uuid="{_uuid()}"/>
\t\t\t\t<textFieldExpression><![CDATA[new java.util.Date()]]></textFieldExpression>
\t\t\t</textField>

\t\t\t<!-- Page counter (evaluationTime=Master) — mirrors template pattern -->
\t\t\t<textField evaluationTime="Master">
\t\t\t\t<reportElement x="667" y="20" width="126" height="23"
\t\t\t\t    isPrintWhenDetailOverflows="true" uuid="{_uuid()}"/>
\t\t\t\t<textElement textAlignment="Center">
\t\t\t\t\t<font fontName="DejaVu Sans" size="8" isBold="true" isPdfEmbedded="true"/>
\t\t\t\t</textElement>
\t\t\t\t<textFieldExpression><![CDATA["Σελίδα " + $V{{MASTER_CURRENT_PAGE}} + " από " + $V{{MASTER_TOTAL_PAGES}}]]></textFieldExpression>
\t\t\t</textField>

\t\t\t<!-- Report title -->
\t\t\t<staticText>
\t\t\t\t<reportElement positionType="Float" x="123" y="132" width="556" height="18"
\t\t\t\t    isPrintWhenDetailOverflows="true" uuid="{_uuid()}"/>
\t\t\t\t<textElement textAlignment="Center">
\t\t\t\t\t<font fontName="DejaVu Sans" isBold="true" isPdfEmbedded="true"/>
\t\t\t\t</textElement>
\t\t\t\t<text><![CDATA[{report_title}]]></text>
\t\t\t</staticText>

\t\t\t<!-- Migration note -->
\t\t\t<staticText>
\t\t\t\t<reportElement x="123" y="152" width="556" height="12"
\t\t\t\t    isPrintWhenDetailOverflows="true" uuid="{_uuid()}"/>
\t\t\t\t<textElement textAlignment="Center">
\t\t\t\t\t<font fontName="DejaVu Sans" size="7" isItalic="true" isPdfEmbedded="true"/>
\t\t\t\t</textElement>
\t\t\t\t<text><![CDATA[Migrated from Oracle Reports · {artifact.name} · {now}]]></text>
\t\t\t</staticText>

\t\t\t<!-- Header parameter values block — mirrors branchName/userEmail text block -->
\t\t\t<textField isStretchWithOverflow="true">
\t\t\t\t<reportElement x="0" y="80" width="650" height="48"
\t\t\t\t    isPrintWhenDetailOverflows="true" uuid="{_uuid()}"/>
\t\t\t\t<textElement verticalAlignment="Bottom">
\t\t\t\t\t<font fontName="DejaVu Sans" size="8" isPdfEmbedded="true"/>
\t\t\t\t</textElement>
\t\t\t\t<textFieldExpression><![CDATA[
{header_fields_expr}
\t\t\t\t]]></textFieldExpression>
\t\t\t</textField>

\t\t</band>
\t</title>

\t<!-- ═══════════════════════════════════════════════════════════════════════
\t     DETAIL BAND — jr:table component bound to $F{{tableDTO}} sub-dataset
\t     Mirrors componentElement in debtorsForProclamationReport.jrxml detail band.
\t     ═══════════════════════════════════════════════════════════════════════ -->
\t<detail>
\t\t<band height="221" splitType="Stretch">
\t\t\t<componentElement>
\t\t\t\t<reportElement positionType="Float"
\t\t\t\t    x="-20" y="10" width="842" height="122"
\t\t\t\t    isPrintWhenDetailOverflows="true"
\t\t\t\t    uuid="{_uuid()}">
\t\t\t\t\t<property name="com.jaspersoft.studio.layout"
\t\t\t\t\t    value="com.jaspersoft.studio.editor.layout.VerticalRowLayout"/>
\t\t\t\t\t<property name="com.jaspersoft.studio.table.style.table_header" value="Table_TH"/>
\t\t\t\t\t<property name="com.jaspersoft.studio.table.style.column_header" value="Table_CH"/>
\t\t\t\t\t<property name="com.jaspersoft.studio.table.style.detail"       value="Table_TD"/>
\t\t\t\t\t<property name="com.jaspersoft.studio.unit.y" value="px"/>
\t\t\t\t\t<property name="com.jaspersoft.studio.components.autoresize.proportional" value="true"/>
\t\t\t\t\t<property name="com.jaspersoft.studio.components.autoresize.next" value="true"/>
\t\t\t\t</reportElement>
\t\t\t\t<jr:table xmlns:jr="http://jasperreports.sourceforge.net/jasperreports/components"
\t\t\t\t    xsi:schemaLocation="http://jasperreports.sourceforge.net/jasperreports/components
\t\t\t\t        http://jasperreports.sourceforge.net/xsd/components.xsd"
\t\t\t\t    whenNoDataType="AllSectionsNoDetail">
\t\t\t\t\t<datasetRun subDataset="tableDTO" uuid="{_uuid()}">
\t\t\t\t\t\t<!-- Mirrors ssp.efka.template pattern:
\t\t\t\t\t\t     feed inner list from outer DTO field $F{{tableDTO}} -->
\t\t\t\t\t\t<dataSourceExpression><![CDATA[new net.sf.jasperreports.engine.data.JRBeanCollectionDataSource($F{{tableDTO}})]]></dataSourceExpression>
\t\t\t\t\t</datasetRun>
{table_cols_xml}
\t\t\t\t</jr:table>
\t\t\t</componentElement>
\t\t\t<break>
\t\t\t\t<reportElement x="0" y="20" width="80" height="1" uuid="{_uuid()}"/>
\t\t\t</break>
\t\t</band>
\t</detail>

</jasperReport>
"""

    def _jrxml_table_cols(self, cols: list, col_w: int) -> str:
        """
        Generate jr:column elements mirroring the column structure in
        debtorsForProclamationReport.jrxml — identical box/pen/font pattern.
        """
        out = ""
        for col in cols:
            fname  = _cc(col["name"])
            header = col["name"].replace("_", " ").title()
            uid1, uid2, uid3 = (_uuid() for _ in range(3))
            out += f"""\
\t\t\t\t\t<jr:column width="{col_w}" uuid="{uid1}">
\t\t\t\t\t\t<property name="com.jaspersoft.studio.components.table.model.column.name"
\t\t\t\t\t\t    value="{header}"/>
\t\t\t\t\t\t<jr:columnHeader height="50" rowSpan="1">
\t\t\t\t\t\t\t<staticText>
\t\t\t\t\t\t\t\t<reportElement x="0" y="0" width="{col_w}" height="50"
\t\t\t\t\t\t\t\t    isPrintWhenDetailOverflows="true" uuid="{uid2}">
\t\t\t\t\t\t\t\t\t<property name="com.jaspersoft.studio.unit.width" value="px"/>
\t\t\t\t\t\t\t\t</reportElement>
\t\t\t\t\t\t\t\t<box topPadding="1" leftPadding="1" bottomPadding="1" rightPadding="1">
\t\t\t\t\t\t\t\t\t<topPen    lineWidth="1.0" lineStyle="Solid" lineColor="#000000"/>
\t\t\t\t\t\t\t\t\t<leftPen   lineWidth="1.0" lineStyle="Solid" lineColor="#000000"/>
\t\t\t\t\t\t\t\t\t<bottomPen lineWidth="1.0" lineStyle="Solid" lineColor="#000000"/>
\t\t\t\t\t\t\t\t\t<rightPen  lineWidth="1.0" lineStyle="Solid" lineColor="#000000"/>
\t\t\t\t\t\t\t\t</box>
\t\t\t\t\t\t\t\t<textElement textAlignment="Center" verticalAlignment="Middle">
\t\t\t\t\t\t\t\t\t<font fontName="DejaVu Sans" size="9" isPdfEmbedded="true"/>
\t\t\t\t\t\t\t\t</textElement>
\t\t\t\t\t\t\t\t<text><![CDATA[{header}]]></text>
\t\t\t\t\t\t\t</staticText>
\t\t\t\t\t\t</jr:columnHeader>
\t\t\t\t\t\t<jr:detailCell height="40">
\t\t\t\t\t\t\t<textField isStretchWithOverflow="true" isBlankWhenNull="true">
\t\t\t\t\t\t\t\t<reportElement x="0" y="0" width="{col_w}" height="40"
\t\t\t\t\t\t\t\t    isPrintWhenDetailOverflows="true" uuid="{uid3}">
\t\t\t\t\t\t\t\t\t<property name="com.jaspersoft.studio.unit.width" value="pixel"/>
\t\t\t\t\t\t\t\t</reportElement>
\t\t\t\t\t\t\t\t<box topPadding="1" leftPadding="1" bottomPadding="1" rightPadding="1">
\t\t\t\t\t\t\t\t\t<topPen    lineWidth="1.0" lineStyle="Solid" lineColor="#000000"/>
\t\t\t\t\t\t\t\t\t<leftPen   lineWidth="1.0" lineStyle="Solid" lineColor="#000000"/>
\t\t\t\t\t\t\t\t\t<bottomPen lineWidth="1.0" lineStyle="Solid" lineColor="#000000"/>
\t\t\t\t\t\t\t\t\t<rightPen  lineWidth="1.0" lineStyle="Solid" lineColor="#000000"/>
\t\t\t\t\t\t\t\t</box>
\t\t\t\t\t\t\t\t<textElement textAlignment="Center" verticalAlignment="Middle">
\t\t\t\t\t\t\t\t\t<font fontName="DejaVu Sans" size="8" isPdfEmbedded="true"/>
\t\t\t\t\t\t\t\t</textElement>
\t\t\t\t\t\t\t\t<textFieldExpression><![CDATA[$F{{{fname}}}]]></textFieldExpression>
\t\t\t\t\t\t\t</textField>
\t\t\t\t\t\t</jr:detailCell>
\t\t\t\t\t</jr:column>
"""
        return out

    # ══════════════════════════════════════════════════════════════════════════
    #  Maven POM — mirrors ssp.efka.template pom.xml structure
    # ══════════════════════════════════════════════════════════════════════════

    def _pom(self, artifact: OracleArtifact, cn: str, aid: str) -> str:
        now = datetime.now().strftime("%Y-%m-%d")
        return f"""\
<?xml version="1.0" encoding="UTF-8"?>
<!--
  Maven POM for {artifact.name} JasperReports WAR.
  Migrated from Oracle Reports: {artifact.file_path}
  Migration date: {now}

  Modelled on ssp.efka.template/pom.xml (Intrasoft / KEAO):
    – parent: com.intrasoft:ssp.efka.additional.apps (stub — remove if building standalone)
    – packaging: war
    – Jakarta EE 10 (jakarta.jakartaee-api, CDI, EJB, JAX-RS)
    – PrimeFaces 13 (jakarta classifier)
    – net.sf.jasperreports: jasperreports + jasperreports-fonts
    – Lombok, Log4j 2.19
    – Java 21, maven-war-plugin 3.2.2
    – WildFly / Payara deployment target
-->
<project xmlns="http://maven.apache.org/POM/4.0.0"
         xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
         xsi:schemaLocation="http://maven.apache.org/POM/4.0.0
             http://maven.apache.org/xsd/maven-4.0.0.xsd">
    <modelVersion>4.0.0</modelVersion>

    <!--
      Mirrors ssp.efka.template parent structure.
      Remove the <parent> block if building as a standalone project.
    -->
    <!--
    <parent>
        <groupId>com.intrasoft</groupId>
        <artifactId>ssp.efka.additional.apps</artifactId>
        <version>${{revision}}</version>
    </parent>
    -->

    <groupId>com.intrasoft.ssp.efka</groupId>
    <artifactId>{aid}</artifactId>
    <version>1.0-SNAPSHOT</version>
    <packaging>war</packaging>
    <name>{aid}</name>

    <properties>
        <java.version>21</java.version>
        <maven.compiler.source>21</maven.compiler.source>
        <maven.compiler.target>21</maven.compiler.target>
        <project.build.sourceEncoding>UTF-8</project.build.sourceEncoding>
        <!-- Versions mirror ssp.efka.template dependencies -->
        <jakartaee.version>10.0.0</jakartaee.version>
        <jasperreports.version>6.21.3</jasperreports.version>
        <lombok.version>1.18.30</lombok.version>
        <log4j.version>2.19.0</log4j.version>
        <infinispan.version>14.0.21.Final</infinispan.version>
    </properties>

    <dependencies>

        <!-- ── Jakarta EE 10 (provided by WildFly / Payara) ──────────────── -->
        <dependency>
            <groupId>jakarta.platform</groupId>
            <artifactId>jakarta.jakartaee-api</artifactId>
            <version>${{jakartaee.version}}</version>
            <scope>provided</scope>
        </dependency>

        <dependency>
            <groupId>jakarta.enterprise</groupId>
            <artifactId>jakarta.enterprise.cdi-api</artifactId>
            <version>4.0.1</version>
            <scope>provided</scope>
        </dependency>

        <dependency>
            <groupId>jakarta.ejb</groupId>
            <artifactId>jakarta.ejb-api</artifactId>
            <version>4.0.1</version>
            <scope>provided</scope>
        </dependency>

        <dependency>
            <groupId>jakarta.ws.rs</groupId>
            <artifactId>jakarta.ws.rs-api</artifactId>
            <version>3.1.0</version>
            <scope>provided</scope>
        </dependency>

        <dependency>
            <groupId>jakarta.persistence</groupId>
            <artifactId>jakarta.persistence-api</artifactId>
            <version>3.1.0</version>
        </dependency>

        <!-- ── PrimeFaces (jakarta classifier — matches template) ─────────── -->
        <dependency>
            <groupId>org.primefaces</groupId>
            <artifactId>primefaces</artifactId>
            <version>13.0.4</version>
            <classifier>jakarta</classifier>
            <scope>provided</scope>
        </dependency>

        <!-- ── JasperReports (matches ssp.efka.template) ──────────────────── -->
        <dependency>
            <groupId>net.sf.jasperreports</groupId>
            <artifactId>jasperreports</artifactId>
            <version>${{jasperreports.version}}</version>
        </dependency>
        <dependency>
            <groupId>net.sf.jasperreports</groupId>
            <artifactId>jasperreports-fonts</artifactId>
            <version>${{jasperreports.version}}</version>
        </dependency>

        <!-- ── Hibernate ORM ───────────────────────────────────────────────── -->
        <dependency>
            <groupId>org.hibernate.orm</groupId>
            <artifactId>hibernate-core</artifactId>
            <version>6.4.4.Final</version>
            <scope>compile</scope>
        </dependency>

        <!-- ── Infinispan (provided — deployed as WildFly module) ─────────── -->
        <dependency>
            <groupId>org.infinispan</groupId>
            <artifactId>infinispan-core-jakarta</artifactId>
            <version>${{infinispan.version}}</version>
            <exclusions>
                <exclusion>
                    <groupId>org.jboss.logging</groupId>
                    <artifactId>jboss-logging</artifactId>
                </exclusion>
            </exclusions>
            <scope>provided</scope>
        </dependency>

        <!-- ── Lombok ──────────────────────────────────────────────────────── -->
        <dependency>
            <groupId>org.projectlombok</groupId>
            <artifactId>lombok</artifactId>
            <version>${{lombok.version}}</version>
            <scope>provided</scope>
        </dependency>

        <!-- ── Log4j 2 (matches ssp.efka.template — 2.19.0) ──────────────── -->
        <dependency>
            <groupId>org.apache.logging.log4j</groupId>
            <artifactId>log4j-api</artifactId>
            <version>${{log4j.version}}</version>
            <scope>provided</scope>
        </dependency>
        <dependency>
            <groupId>org.apache.logging.log4j</groupId>
            <artifactId>log4j-core</artifactId>
            <version>${{log4j.version}}</version>
        </dependency>

        <!-- ── Commons ─────────────────────────────────────────────────────── -->
        <dependency>
            <groupId>org.apache.commons</groupId>
            <artifactId>commons-text</artifactId>
            <version>1.11.0</version>
            <scope>provided</scope>
        </dependency>
        <dependency>
            <groupId>commons-io</groupId>
            <artifactId>commons-io</artifactId>
            <version>2.15.1</version>
            <scope>provided</scope>
        </dependency>
        <dependency>
            <groupId>commons-validator</groupId>
            <artifactId>commons-validator</artifactId>
            <version>1.8.0</version>
        </dependency>

        <!-- ── ICU4J (matches ssp.efka.template — icu4j 77.1) ─────────────── -->
        <dependency>
            <groupId>com.ibm.icu</groupId>
            <artifactId>icu4j</artifactId>
            <version>77.1</version>
        </dependency>

        <!-- ── OWASP HTML Sanitizer (provided) ────────────────────────────── -->
        <dependency>
            <groupId>com.googlecode.owasp-java-html-sanitizer</groupId>
            <artifactId>owasp-java-html-sanitizer</artifactId>
            <version>20240325.1</version>
            <scope>provided</scope>
        </dependency>

    </dependencies>

    <build>
        <finalName>${{project.artifactId}}</finalName>
        <plugins>

            <!-- WAR plugin — mirrors ssp.efka.template build config exactly -->
            <plugin>
                <groupId>org.apache.maven.plugins</groupId>
                <artifactId>maven-war-plugin</artifactId>
                <version>3.2.2</version>
                <configuration>
                    <archive>
                        <manifestEntries>
                            <Dependencies>
                                <!-- workaround for ClassNotFoundException: sun.misc.Unsafe in fastjson -->
                                jdk.unsupported
                            </Dependencies>
                        </manifestEntries>
                    </archive>
                </configuration>
            </plugin>

            <!-- Compiler — Java 21 with Lombok annotation processor -->
            <plugin>
                <groupId>org.apache.maven.plugins</groupId>
                <artifactId>maven-compiler-plugin</artifactId>
                <version>3.11.0</version>
                <configuration>
                    <source>21</source>
                    <target>21</target>
                    <annotationProcessorPaths>
                        <path>
                            <groupId>org.projectlombok</groupId>
                            <artifactId>lombok</artifactId>
                            <version>${{lombok.version}}</version>
                        </path>
                    </annotationProcessorPaths>
                </configuration>
            </plugin>

        </plugins>
    </build>

</project>
"""

    # ══════════════════════════════════════════════════════════════════════════
    #  Config files — exact mirror of ssp.efka.template
    # ══════════════════════════════════════════════════════════════════════════

    def _persistence_xml(self, cn: str) -> str:
        """Mirrors ssp.efka.template persistence.xml with JTA datasource."""
        return f"""\
<?xml version="1.0" encoding="UTF-8"?>
<!--
  persistence.xml — mirrors ssp.efka.template/src/main/resources/META-INF/persistence.xml
  JTA datasource for WildFly (Hibernate 6, Jakarta EE 10).
  Replace datasource JNDI names to match your server config.
-->
<persistence version="2.1"
             xmlns="http://xmlns.jcp.org/xml/ns/persistence"
             xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
             xsi:schemaLocation="
    http://xmlns.jcp.org/xml/ns/persistence
    http://xmlns.jcp.org/xml/ns/persistence/persistence_2_1.xsd">

    <!-- Primary datasource — mirrors commonPU from template -->
    <persistence-unit name="reportPU" transaction-type="JTA">

        <!-- WildFly JNDI datasource — update to match your server config -->
        <jta-data-source>java:/jboss/datasources/jdbc/ssakeao</jta-data-source>

        <!--  Empty jar-file element for Wildfly  -->
        <jar-file></jar-file>

        <exclude-unlisted-classes>false</exclude-unlisted-classes>

        <properties>
            <property name="hibernate.transaction.jta.platform" value="${{jta.platform}}"/>
            <property name="hibernate.jdbc.use_get_generated_keys" value="true"/>
            <property name="hibernate.show_sql"    value="false"/>
            <property name="hibernate.format_sql"  value="true"/>
            <property name="hibernate.enable_lazy_load_no_trans" value="true"/>
            <property name="hibernate.jpa.compliance.query" value="false"/>
        </properties>
    </persistence-unit>

    <!--
      Optional dummy PU — mirrors opsikaCommonPU / opsikaContrPU in template.
      Required if any deployed jar references these PU names.
      Remove if not needed.
    -->
    <persistence-unit name="reportCommonPU" transaction-type="RESOURCE_LOCAL">
        <properties>
            <property name="javax.persistence.jdbc.driver"   value="java.sql.Driver"/>
            <property name="javax.persistence.jdbc.url"      value="jdbc:dummy:dummy"/>
            <property name="javax.persistence.jdbc.user"     value="dummy"/>
            <property name="javax.persistence.jdbc.password" value="dummy"/>
            <property name="hibernate.dialect"               value="org.hibernate.dialect.OracleDialect"/>
            <property name="hibernate.hbm2ddl.auto"          value="none"/>
        </properties>
    </persistence-unit>

</persistence>
"""

    def _microprofile(self, artifact: OracleArtifact, cn: str) -> str:
        """Mirrors ssp.efka.template microprofile-config.properties exactly."""
        lname = artifact.name.lower()
        return f"""\
# MicroProfile config
# Mirrors ssp.efka.template/src/main/resources/META-INF/microprofile-config.properties
# Adjust paths to match your WildFly / Payara deployment context root.

zeno.services.redirectPage.name=/ssp.efka.{lname}/common/access.xhtml
zeno.services.logInPage.name=/ssp.efka.{lname}/views/reports/{lname}.xhtml

# Common Pages links
loginErrorPage.buttonLink=/secure/index
loginPage.forgotButtonLink=/ssp.enduser/views/recoveryForm/recoveryForm
errorPage.buttonLink=/secure/index
accessPage.buttonLink=/secure/index
error404Page.buttonLink=/secure/index

# Breadcrumbs links
topbar.applicationLink=/ssp.efka.{lname}/views/reports/{lname}.xhtml
topbar.platformLink=/ssp.commonservices.home/views/secure/index

# Tasks links
topbar.pendingTasksLink=/ssp.backoffice.tasks/tasksDynamic
topbar.groupTasksLink=/ssp.backoffice.tasks/views/revenueType/tasksDynamic

# Application settings API URL
applicationSettings.api=http://app.settings.host:7003/common.application.rest/api
entityAuditing=true
"""

    def _jr_props(self) -> str:
        """Exact copy of ssp.efka.template jasperreports.properties."""
        return """\
# JasperReports runtime properties
# Mirrors ssp.efka.template/src/main/resources/jasperreports.properties (exact copy)

net.sf.jasperreports.awt.ignore.missing.font=true
net.sf.jasperreports.export.pdf.force.svg.shapes=true
net.sf.jasperreports.debug=true
net.sf.jasperreports.fill.timeout=500
"""

    def _log4j2(self, cn: str, pkg: str) -> str:
        """Mirrors ssp.efka.template log4j2.xml structure."""
        return f"""\
<?xml version="1.0" encoding="UTF-8"?>
<!--
  Log4j 2 configuration
  Mirrors ssp.efka.template/src/main/resources/log4j2.xml
-->
<Configuration status="WARN">
    <Appenders>
        <Console name="Console" target="SYSTEM_OUT">
            <PatternLayout pattern="%d{{HH:mm:ss.SSS}} [%t] %-5level %logger{{36}} - %msg%n"/>
        </Console>
    </Appenders>
    <Loggers>
        <Logger name="{pkg}" level="debug" additivity="false">
            <AppenderRef ref="Console"/>
        </Logger>
        <Logger name="net.sf.jasperreports" level="warn" additivity="false">
            <AppenderRef ref="Console"/>
        </Logger>
        <Root level="info">
            <AppenderRef ref="Console"/>
        </Root>
    </Loggers>
</Configuration>
"""

    def _web_xml(self, artifact: OracleArtifact, cn: str) -> str:
        """Mirrors ssp.efka.template web.xml exactly (JSF, OIDC, PrimeFaces, MIME)."""
        lname = artifact.name.lower()
        return f"""\
<?xml version="1.0" encoding="UTF-8"?>
<!--
  web.xml — mirrors ssp.efka.template/src/main/webapp/WEB-INF/web.xml exactly:
    – JSF FacesServlet mapped to *.xhtml
    – OIDC auth-method
    – PrimeFaces ultima theme + Font Awesome
    – Security constraint on /views/*
    – Standard error pages
    – Web font MIME mappings
-->
<web-app xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
         xmlns="http://java.sun.com/xml/ns/javaee"
         xsi:schemaLocation="http://java.sun.com/xml/ns/javaee
             http://java.sun.com/xml/ns/javaee/web-app_3_0.xsd"
         metadata-complete="false" version="3.0">

    <welcome-file-list>
        <welcome-file>/views/reports/{lname}.xhtml</welcome-file>
    </welcome-file-list>

    <login-config>
        <auth-method>OIDC</auth-method>
    </login-config>

    <security-constraint>
        <display-name>SecureConstraint</display-name>
        <web-resource-collection>
            <web-resource-name>Secure folders</web-resource-name>
            <url-pattern>/views/*</url-pattern>
            <http-method>GET</http-method>
        </web-resource-collection>
        <auth-constraint>
            <description>These are the roles who have access.</description>
            <role-name>*</role-name>
        </auth-constraint>
    </security-constraint>

    <security-role>
        <description>These are the roles who have access</description>
        <role-name>*</role-name>
    </security-role>

    <!-- Primefaces and JSF -->
    <context-param>
        <param-name>primefaces.THEME</param-name>
        <param-value>ultima-#{{guestPreferences.theme}}</param-value>
    </context-param>
    <context-param>
        <param-name>primefaces.FONT_AWESOME</param-name>
        <param-value>true</param-value>
    </context-param>
    <context-param>
        <param-name>jakarta.faces.FACELETS_LIBRARIES</param-name>
        <param-value>/WEB-INF/templates/primefaces-ultima.taglib.xml</param-value>
    </context-param>
    <context-param>
        <param-name>jakarta.faces.PROJECT_STAGE</param-name>
        <param-value>Production</param-value>
    </context-param>
    <context-param>
        <param-name>jakarta.faces.STATE_SAVING_METHOD</param-name>
        <param-value>server</param-value>
    </context-param>
    <context-param>
        <param-name>jakarta.faces.FACELETS_SKIP_COMMENTS</param-name>
        <param-value>true</param-value>
    </context-param>
    <context-param>
        <param-name>jakarta.faces.DATETIMECONVERTER_DEFAULT_TIMEZONE_IS_SYSTEM_TIMEZONE</param-name>
        <param-value>true</param-value>
    </context-param>

    <filter>
        <filter-name>Character Encoding Filter</filter-name>
        <filter-class>org.primefaces.ultima.filter.CharacterEncodingFilter</filter-class>
    </filter>
    <filter-mapping>
        <filter-name>Character Encoding Filter</filter-name>
        <servlet-name>Faces Servlet</servlet-name>
    </filter-mapping>

    <servlet>
        <servlet-name>Faces Servlet</servlet-name>
        <servlet-class>jakarta.faces.webapp.FacesServlet</servlet-class>
    </servlet>
    <servlet-mapping>
        <servlet-name>Faces Servlet</servlet-name>
        <url-pattern>*.xhtml</url-pattern>
    </servlet-mapping>

    <!-- Error pages -->
    <error-page>
        <exception-type>java.lang.Throwable</exception-type>
        <location>/common/error.xhtml</location>
    </error-page>
    <error-page>
        <exception-type>jakarta.faces.application.ViewExpiredException</exception-type>
        <location>/common/access.xhtml</location>
    </error-page>
    <error-page>
        <error-code>403</error-code>
        <location>/common/access.xhtml</location>
    </error-page>
    <error-page>
        <error-code>404</error-code>
        <location>/common/404.xhtml</location>
    </error-page>
    <error-page>
        <exception-type>java.lang.Exception</exception-type>
        <location>/common/error.xhtml</location>
    </error-page>

    <!-- Web font MIME mappings — mirrors template exactly -->
    <mime-mapping><extension>ttf</extension><mime-type>application/font-sfnt</mime-type></mime-mapping>
    <mime-mapping><extension>woff</extension><mime-type>application/font-woff</mime-type></mime-mapping>
    <mime-mapping><extension>woff2</extension><mime-type>application/font-woff2</mime-type></mime-mapping>
    <mime-mapping><extension>eot</extension><mime-type>application/vnd.ms-fontobject</mime-type></mime-mapping>
    <mime-mapping><extension>svg</extension><mime-type>image/svg+xml</mime-type></mime-mapping>

</web-app>
"""

    def _jboss_web(self, artifact: OracleArtifact) -> str:
        """Mirrors ssp.efka.template jboss-web.xml."""
        return f"""\
<?xml version="1.0" encoding="UTF-8"?>
<!--
  jboss-web.xml — WildFly context root
  Mirrors ssp.efka.template/src/main/webapp/WEB-INF/jboss-web.xml
-->
<jboss-web xmlns="http://www.jboss.com/xml/ns/javaee"
           xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
           xsi:schemaLocation="
    http://www.jboss.com/xml/ns/javaee
    http://www.jboss.org/j2ee/schema/jboss-web_5_1.xsd">

    <context-root>/ssp.efka.{artifact.name.lower()}</context-root>

</jboss-web>
"""

    def _jboss_ds(self) -> str:
        """
        Mirrors ssp.efka.template jboss-deployment-structure.xml exactly:
          – <exclude-subsystems><subsystem name="logging"/>
          – perseus sspcommonlibs module
          – Oracle JDBC, SLF4J, Infinispan modules
        """
        return """\
<?xml version="1.0" encoding="UTF-8"?>
<!--
  jboss-deployment-structure.xml
  Mirrors ssp.efka.template/src/main/webapp/WEB-INF/jboss-deployment-structure.xml
  Declares WildFly module dependencies and excludes the logging subsystem
  so Log4j 2 can manage logging directly.
-->
<jboss-deployment-structure xmlns="urn:jboss:deployment-structure:1.2">

    <ear-subdeployments-isolated>false</ear-subdeployments-isolated>

    <deployment>
        <exclude-subsystems>
            <!-- Let Log4j 2 handle logging — same as ssp.efka.template -->
            <subsystem name="logging"/>
        </exclude-subsystems>

        <dependencies>
            <!-- SSP / KEAO shared libraries (deployed as WildFly modules) -->
            <module name="com.intrasoft.perseus.sspcommonlibs"
                    services="import" meta-inf="import"/>

            <!-- Oracle JDBC driver -->
            <module name="com.oracle" services="import" meta-inf="import"/>

            <!-- SLF4J bridge -->
            <module name="org.slf4j" slot="main" services="import"/>

            <!-- Infinispan distributed cache -->
            <module name="org.infinispan"/>
            <module name="org.infinispan.commons"/>
        </dependencies>
    </deployment>

</jboss-deployment-structure>
"""

    def _beans_xml(self) -> str:
        """CDI activator — mirrors ssp.efka.template beans.xml."""
        return """\
<?xml version="1.0" encoding="UTF-8"?>
<!-- CDI activator — required for @Named, @ViewScoped, @Inject to work -->
<beans xmlns="https://jakarta.ee/xml/ns/jakartaee"
       xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
       xsi:schemaLocation="https://jakarta.ee/xml/ns/jakartaee
           https://jakarta.ee/xml/ns/jakartaee/beans_3_0.xsd"
       bean-discovery-mode="all"
       version="3.0">
</beans>
"""

    def _faces_config(self, pkg: str, cn: str) -> str:
        """Mirrors ssp.efka.template faces-config.xml."""
        return f"""\
<?xml version="1.0"?>
<!--
  faces-config.xml — JSF / Jakarta Faces configuration
  Mirrors ssp.efka.template/src/main/webapp/WEB-INF/faces-config.xml
-->
<faces-config version="2.2"
              xmlns="http://xmlns.jcp.org/xml/ns/javaee"
              xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
              xsi:schemaLocation="
    http://xmlns.jcp.org/xml/ns/javaee
    http://xmlns.jcp.org/xml/ns/javaee/web-facesconfig_2_2.xsd">

    <application>

        <!-- PrimeFaces dialog framework -->
        <action-listener>org.primefaces.application.DialogActionListener</action-listener>
        <navigation-handler>org.primefaces.application.DialogNavigationHandler</navigation-handler>
        <view-handler>org.primefaces.application.DialogViewHandler</view-handler>

        <locale-config>
            <default-locale>el_GR</default-locale>
            <supported-locale>en_US</supported-locale>
            <supported-locale>el_GR</supported-locale>
        </locale-config>

    </application>

    <navigation-rule>
        <from-view-id>*</from-view-id>
        <navigation-case>
            <from-outcome>logout</from-outcome>
            <to-view-id>/common/login.xhtml</to-view-id>
            <redirect/>
        </navigation-case>
    </navigation-rule>

</faces-config>
"""

    def _xhtml(self, artifact: OracleArtifact, cn: str) -> str:
        """
        JSF view — mirrors ssp.efka.template dashboard.xhtml:
          – ui:composition with ssp-template.xhtml
          – h:form with p:commandButton for PDF and XLSX export (ajax=false, target=_blank)
          – p:growl for messages
        """
        bc = _cc(cn) + "ReportController"
        lname = artifact.name.lower()
        title = artifact.name.replace("_", " ").title()
        return f"""\
<ui:composition xmlns="http://www.w3.org/1999/xhtml"
                template="/WEB-INF/templates/ssp-template.xhtml"
                xmlns:ui="http://java.sun.com/jsf/facelets"
                xmlns:h="http://xmlns.jcp.org/jsf/html"
                xmlns:p="http://primefaces.org/ui"
                xmlns:f="http://xmlns.jcp.org/jsf/core"
                xmlns:c="http://java.sun.com/jsp/jstl/core">

    <!--
      JSF view for {artifact.name} report.
      Mirrors ssp.efka.template/src/main/webapp/views/dashboard.xhtml structure.
      Controller: {cn}ReportController (@ViewScoped @Named="{bc}")
    -->

    <ui:define name="title">{title}</ui:define>

    <ui:define name="head">
        <style>
            .dash_main_content a.box_view {{
                margin-top: 9px !important;
                word-break: break-word !important;
                hyphens: auto !important;
            }}
            #reportUnit {{
                margin-top: 1.35%;
            }}
        </style>
    </ui:define>

    <ui:define name="contentHeader">
        <ui:define name="pageMetadata">
            <f:metadata>
                <f:event type="preRenderView"
                         listener="#{{euPrefsController.forcePageLoad(templateController.preRender())}}"/>
            </f:metadata>
        </ui:define>

        <h:form id="mainHeaderForm" prependId="true"
                rendered="#{{dashboardController.getNavBean().viewIsCurrent('mainPanel')}}"
                dir="#{{guestPreferences.orientationRTL ? 'rtl':'ltr'}}">

            <div class="ui-g-12 ui-lg-12 alternative_header top_sticker">
                <div class="right_area_options">
                    <div class="ui-g-12 ui-lg-12 title">
                        <span>{title}</span>
                    </div>
                </div>
            </div>

            <div id="reportUnit" class="card card-w-title">
                <div class="ui-g ui-fluid">
                    <div class="ui-md-12 ui-g-12">
                        <div class="ui-g">
                            <div class="ui-g-12 ui-md-12">

                                <!-- PDF export — mirrors DashboardController.printReport() pattern -->
                                <p:commandButton value="Export PDF"
                                                 action="#{{{bc}.printReport()}}"
                                                 target="_blank"
                                                 ajax="false"
                                                 immediate="true"
                                                 icon="pi pi-file-pdf"
                                                 styleClass="ui-button-danger"
                                                 style="margin-right: 0.5rem"/>

                                <!-- XLSX export -->
                                <p:commandButton value="Export Excel"
                                                 action="#{{{bc}.exportXlsx()}}"
                                                 target="_blank"
                                                 ajax="false"
                                                 immediate="true"
                                                 icon="pi pi-file-excel"
                                                 styleClass="ui-button-success"/>

                            </div>
                        </div>
                    </div>
                </div>
            </div>
        </h:form>
    </ui:define>

    <ui:define name="contentBody">
        <p:growl id="growl" showDetail="true" for="infogrowl"/>
    </ui:define>

</ui:composition>
"""

    def _readme(self, artifact: OracleArtifact, cn: str, aid: str) -> str:
        now = datetime.now().strftime("%Y-%m-%d")
        lname = artifact.name.lower()
        return f"""\
# {cn} — JasperReports WAR
**Migrated from Oracle Reports:** `{artifact.file_path}`  
**Migration date:** {now}  
**Template pattern:** `ssp.efka.template` (Intrasoft / KEAO)

---

## Project layout (mirrors ssp.efka.template)

```
src/main/java/com/intrasoft/ssp/efka/{cn.lower()}/
│
├── service/
│   └── JasperEngine.java                  ← static engine (compile/fill/60s timeout)
│
├── dto/
│   └── {cn}ReportDTO.java                 ← outer DTO + non-static inner TableDTO
│
├── bean/
│   └── {cn}Bean.java                      ← @ViewScoped @Named (locale/calendarTypeFlg)
│
└── controller/
    ├── AbstractCommonInternalParentController.java
    └── {cn}ReportController.java          ← @ViewScoped, printReport() / exportXlsx()

src/main/resources/
├── reports/{lname}/
│   └── {lname}.jrxml                      ← landscape A4, subDataset jr:table, DejaVu Sans
├── META-INF/
│   ├── persistence.xml                    ← JTA datasource (WildFly)
│   └── microprofile-config.properties
├── jasperreports.properties               ← exact copy of template properties
└── log4j2.xml

src/main/webapp/
├── views/reports/
│   └── {lname}.xhtml                      ← PrimeFaces view (Export PDF / Export Excel)
└── WEB-INF/
    ├── web.xml                            ← JSF, OIDC security, MIME mappings
    ├── jboss-web.xml                      ← context root: /ssp.efka.{lname}
    ├── jboss-deployment-structure.xml     ← logging subsystem excluded, modules declared
    ├── beans.xml                          ← CDI activator
    └── faces-config.xml
```

---

## Build & deploy (WildFly)

```bash
mvn clean package
cp target/{aid}.war $WILDFLY_HOME/standalone/deployments/
```

Access: `http://localhost:8080/ssp.efka.{lname}/`

---

## Report data flow

```
{cn}ReportController.printReport()
    │
    ├── JasperCompileManager.compileReport( JRXML_PATH )   ← from classpath
    │
    ├── createDummyRecord()    ← replace with real @EJB / @Inject service
    │       returns {cn}ReportDTO
    │
    ├── JasperFillManager.fillReport( compiled, params,
    │       new JRBeanCollectionDataSource( Collection<{cn}ReportDTO> ) )
    │
    ├── JRPdfExporter  →  ByteArrayOutputStream
    │
    └── streamToResponse( "{lname}.pdf", bytes, "application/pdf" )
        (replaces HttpRequestUtil.httpRequestForBinaryFile in real SSP stack)
```

### Sub-dataset binding (mirrors debtorsForProclamationReport.jrxml)

Each `{cn}ReportDTO` exposes `LinkedList<TableDTO> tableDTO`.  
The JRXML `jr:table` binds to it via:

```xml
<dataSourceExpression>
    <![CDATA[
        new net.sf.jasperreports.engine.data.JRBeanCollectionDataSource($F{{tableDTO}})
    ]]>
</dataSourceExpression>
```

---

## Replacing the dummy data

```java
@Inject
private YourReportService reportService;

// replace createDummyRecord() body:
{cn}ReportDTO dto = reportService.getReportData(dateFrom, dateTo, branchId);
```

---

## Key alignment with ssp.efka.template

| Aspect | ssp.efka.template | This output |
|--------|------------------|-------------|
| JasperEngine | `renderReport` / `compileBatchReport` / `renderBatchReport` / `produceJasperPrintReport` | ✅ Identical signatures |
| Thread model | `ExecutorService.newSingleThreadExecutor()`, `future.get(60, SECONDS)` | ✅ Identical |
| DTO pattern | outer DTO + non-static inner `TableDTO`, `record.new TableDTO()` | ✅ Identical |
| Sub-dataset | `$F{{tableDTO}}` → `JRBeanCollectionDataSource` | ✅ Identical |
| Controller base | extends `AbstractCommonInternalParentController` | ✅ Included |
| Bean | `@ViewScoped @Named` with locale + calendarTypeFlg | ✅ Included |
| PDF export | `JRPdfExporter` + `SimplePdfExporterConfiguration` + `ByteArrayOutputStream` | ✅ Identical |
| JRXML fonts | `DejaVu Sans`, `isPdfEmbedded="true"` | ✅ Identical |
| JRXML orientation | Landscape A4 (842×595, margins 20) | ✅ Identical |
| `jasperreports.properties` | `awt.ignore.missing.font=true`, `fill.timeout=500` | ✅ Exact copy |
| `jboss-deployment-structure.xml` | `<exclude-subsystems><subsystem name="logging"/>` | ✅ Exact copy |
| `web.xml` | OIDC auth, PrimeFaces ultima theme, `*.xhtml` servlet | ✅ Exact copy |
| `persistence.xml` | JTA, WildFly datasource JNDI, Hibernate properties | ✅ Mirrors template |
| Log4j version | 2.19.0 | ✅ Matches |
| Java version | 21 | ✅ Matches |
"""

    # ══════════════════════════════════════════════════════════════════════════
    #  Helpers
    # ══════════════════════════════════════════════════════════════════════════

    def _extract_cols(self, artifact: OracleArtifact) -> list:
        """Extract column name + Java type from queries and block items."""
        seen = {}

        for q in artifact.queries:
            sql = q.get("sql", "")
            m = re.search(r"SELECT\s+(.+?)\s+FROM", sql, re.I | re.DOTALL)
            if m:
                for col in m.group(1).split(","):
                    col = col.strip()
                    alias = re.search(r"\bAS\s+(\w+)\s*$", col, re.I)
                    if alias:
                        name = alias.group(1).upper()
                    else:
                        parts = col.split()
                        name = parts[-1].split(".")[-1].strip("\"'").upper() if parts else ""
                    if name and name != "*" and re.match(r"^\w+$", name):
                        jtype = "String"
                        if any(k in col.upper() for k in ("AMOUNT", "BALANCE", "TOTAL", "SUM", "PRICE")):
                            jtype = "BigDecimal"
                        elif any(k in col.upper() for k in ("COUNT", "ID", "NO", "NUM", "CODE")):
                            jtype = "Long"
                        seen[name] = jtype

        for block in artifact.blocks:
            for item in block.items:
                name = item.name.upper()
                if name not in seen:
                    seen[name] = _jt(getattr(item, "data_type", "VARCHAR2"))

        if not seen:
            seen = {
                "RECORD_ID":   "Long",
                "NAME":        "String",
                "DESCRIPTION": "String",
                "AMOUNT":      "BigDecimal",
                "DATE_FROM":   "String",
                "DATE_TO":     "String",
                "STATUS":      "String",
            }

        return [{"name": k, "jtype": v} for k, v in seen.items()]

    def _dummy_outer(self, artifact: OracleArtifact) -> str:
        params = artifact.parameters if artifact.parameters else [
            {"name": "P_DATE_FROM",   "type": "VARCHAR2"},
            {"name": "P_DATE_TO",     "type": "VARCHAR2"},
            {"name": "P_BRANCH_NAME", "type": "VARCHAR2"},
            {"name": "P_USER_EMAIL",  "type": "VARCHAR2"},
        ]
        defaults = {
            "VARCHAR2": '"Sample Value"', "VARCHAR": '"Sample Value"',
            "CHAR": '"A"', "NUMBER": 'new java.math.BigDecimal("0")',
            "INTEGER": "0L", "DATE": '"01/01/2024"',
        }
        lines = []
        for p in params:
            fname  = _cc(p["name"].lstrip("P_"))
            setter = f"set{fname[0].upper()+fname[1:]}"
            default = defaults.get(p.get("type", "VARCHAR2").upper().split("(")[0], '"Sample"')
            lines.append(f"        record.{setter}({default});")
        return "\n".join(lines)

    def _dummy_rows(self, artifact: OracleArtifact, cn: str) -> str:
        """
        Mirrors createDummyRecord() in DashboardController.
        Uses record.new TableDTO() — non-static inner class pattern.
        """
        cols = self._extract_cols(artifact)
        defaults = {
            "String":     '"Sample"',
            "BigDecimal": 'new java.math.BigDecimal("100.00")',
            "Long":       "1L",
            "Integer":    "1",
            "Boolean":    "Boolean.TRUE",
            "Double":     "1.0",
        }
        defaults2 = {
            "String":     '"Sample 2"',
            "BigDecimal": 'new java.math.BigDecimal("200.00")',
            "Long":       "2L",
            "Integer":    "2",
            "Boolean":    "Boolean.FALSE",
            "Double":     "2.0",
        }
        lines = [f"        {cn}ReportDTO.TableDTO row1 = record.new TableDTO();"]
        for col in cols:
            fname  = _cc(col["name"])
            setter = f"set{fname[0].upper()+fname[1:]}"
            lines.append(f"        row1.{setter}({defaults.get(col['jtype'], '\"Sample\"')});")
        lines.append("        record.getTableDTO().add(row1);")
        lines.append("")
        lines.append(f"        {cn}ReportDTO.TableDTO row2 = record.new TableDTO();")
        for col in cols:
            fname  = _cc(col["name"])
            setter = f"set{fname[0].upper()+fname[1:]}"
            lines.append(f"        row2.{setter}({defaults2.get(col['jtype'], '\"Sample 2\"')});")
        lines.append("        record.getTableDTO().add(row2);")
        return "\n".join(lines)
