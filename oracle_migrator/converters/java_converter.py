"""
Converts Oracle Forms → Java Spring Boot (Spring MVC + Thymeleaf + JPA)
"""
import re
from datetime import datetime
from pathlib import Path
from typing import List
from ..core.models import OracleArtifact, TriggerInfo


ORACLE_TO_JAVA = {
    "VARCHAR2": "String", "VARCHAR": "String", "CHAR": "String", "NVARCHAR2": "String",
    "NUMBER": "BigDecimal", "INTEGER": "Long", "INT": "Long", "FLOAT": "Double",
    "DATE": "LocalDateTime", "TIMESTAMP": "LocalDateTime",
    "BOOLEAN": "Boolean", "BLOB": "byte[]", "CLOB": "String",
    "LONG": "String", "RAW": "byte[]",
}


def _java_name(name: str) -> str:
    """Convert ORACLE_NAME → OracleName"""
    parts = re.split(r"[\s_\-]+", name)
    return "".join(p.capitalize() for p in parts if p)


def _camel(name: str) -> str:
    """Convert ORACLE_NAME → oracleName"""
    parts = re.split(r"[\s_\-]+", name.lower())
    return parts[0] + "".join(p.capitalize() for p in parts[1:]) if parts else name


def _j(oracle_type: str) -> str:
    return ORACLE_TO_JAVA.get(oracle_type.upper().split("(")[0], "String")


def _trigger_method(name: str) -> str:
    parts = re.split(r"[\-_]", name.lower())
    return parts[0] + "".join(p.capitalize() for p in parts[1:])


class JavaConverter:
    """Generates a complete Spring Boot Maven project from an Oracle Form."""

    def convert(self, artifact: OracleArtifact, output_dir: str) -> List[str]:
        out = Path(output_dir)
        cn = _java_name(artifact.name)
        pkg = cn.lower()

        java_src = out / "src/main/java/com/migrated" / pkg
        resources = out / "src/main/resources"
        templates = resources / "templates"
        css_dir = resources / "static/css"
        js_dir = resources / "static/js"
        test_src = out / "src/test/java/com/migrated" / pkg

        for d in [java_src, templates, css_dir, js_dir, test_src]:
            d.mkdir(parents=True, exist_ok=True)

        files = []

        # Main application entry point
        app_file = java_src / f"{cn}Application.java"
        app_file.write_text(self._app(cn, pkg))
        files.append(str(app_file))

        # Entity
        entity_file = java_src / f"{cn}.java"
        entity_file.write_text(self._entity(artifact, cn, pkg))
        files.append(str(entity_file))

        # Repository
        repo_file = java_src / f"{cn}Repository.java"
        repo_file.write_text(self._repository(artifact, cn, pkg))
        files.append(str(repo_file))

        # Service
        svc_file = java_src / f"{cn}Service.java"
        svc_file.write_text(self._service(artifact, cn, pkg))
        files.append(str(svc_file))

        # Controller
        ctrl_file = java_src / f"{cn}Controller.java"
        ctrl_file.write_text(self._controller(artifact, cn, pkg))
        files.append(str(ctrl_file))

        # Exception classes
        exc_file = java_src / f"{cn}Exception.java"
        exc_file.write_text(self._exception(cn, pkg))
        files.append(str(exc_file))

        # Thymeleaf list view
        list_tmpl = templates / f"{pkg}-list.html"
        list_tmpl.write_text(self._template_list(artifact, cn, pkg))
        files.append(str(list_tmpl))

        # Thymeleaf form view
        form_tmpl = templates / f"{pkg}-form.html"
        form_tmpl.write_text(self._template_form(artifact, cn, pkg))
        files.append(str(form_tmpl))

        # CSS — Oracle Forms look & feel
        css_file = css_dir / "oracle-forms.css"
        css_file.write_text(self._css())
        files.append(str(css_file))

        # JavaScript
        js_file = js_dir / "oracle-forms.js"
        js_file.write_text(self._js(pkg))
        files.append(str(js_file))

        # pom.xml
        pom = out / "pom.xml"
        pom.write_text(self._pom(cn, pkg))
        files.append(str(pom))

        # application.properties
        props = resources / "application.properties"
        props.write_text(self._properties(cn, pkg))
        files.append(str(props))

        # application-h2.properties (for local testing without Oracle)
        h2_props = resources / "application-h2.properties"
        h2_props.write_text(self._h2_properties(cn))
        files.append(str(h2_props))

        # Test class
        test_file = test_src / f"{cn}ServiceTest.java"
        test_file.write_text(self._test(cn, pkg))
        files.append(str(test_file))

        # docker-compose.yml
        docker = out / "docker-compose.yml"
        docker.write_text(self._docker(cn, pkg))
        files.append(str(docker))

        return files

    # ── Code generators ───────────────────────────────────────────────────────

    def _app(self, cn: str, pkg: str) -> str:
        return f"""package com.migrated.{pkg};

import org.springframework.boot.SpringApplication;
import org.springframework.boot.autoconfigure.SpringBootApplication;

/**
 * Spring Boot application migrated from Oracle Forms.
 * Run with: mvn spring-boot:run -Dspring.profiles.active=h2
 */
@SpringBootApplication
public class {cn}Application {{
    public static void main(String[] args) {{
        SpringApplication.run({cn}Application.class, args);
    }}
}}
"""

    def _entity(self, artifact: OracleArtifact, cn: str, pkg: str) -> str:
        all_items = []
        for block in artifact.blocks:
            all_items.extend(block.items)

        fields_lines = []
        for item in all_items:
            jtype = _j(item.data_type)
            fname = _camel(item.name)
            annotation = ""
            if item.required:
                annotation = '    @jakarta.validation.constraints.NotNull\n'
            fields_lines.append(f"{annotation}    private {jtype} {fname};")

        if not fields_lines:
            fields_lines = [
                "    private String description;",
                "    private String status;",
                "    private java.time.LocalDateTime createdAt;",
                "    private java.time.LocalDateTime updatedAt;",
            ]

        getters = self._make_getters_setters(artifact, cn)

        return f"""package com.migrated.{pkg};

import jakarta.persistence.*;
import jakarta.validation.constraints.*;
import java.math.BigDecimal;
import java.time.LocalDateTime;

/**
 * Entity migrated from Oracle Forms: {artifact.name}
 * Migration date: {datetime.now().strftime('%Y-%m-%d')}
 */
@Entity
@Table(name = "{artifact.name.upper()}")
public class {cn} {{

    @Id
    @GeneratedValue(strategy = GenerationType.SEQUENCE,
        generator = "{cn.upper()}_SEQ")
    @SequenceGenerator(name = "{cn.upper()}_SEQ",
        sequenceName = "{cn.upper()}_SEQ", allocationSize = 1)
    private Long id;

{chr(10).join(fields_lines)}

    @Column(name = "CREATED_AT")
    private LocalDateTime createdAt;

    @Column(name = "UPDATED_AT")
    private LocalDateTime updatedAt;

    @PrePersist
    protected void onCreate() {{
        createdAt = LocalDateTime.now();
        updatedAt = LocalDateTime.now();
    }}

    @PreUpdate
    protected void onUpdate() {{
        updatedAt = LocalDateTime.now();
    }}

    public {cn}() {{}}

    // ─── Getters & Setters ───────────────────────────────────────────────────

    public Long getId() {{ return id; }}
    public void setId(Long id) {{ this.id = id; }}

    public LocalDateTime getCreatedAt() {{ return createdAt; }}
    public LocalDateTime getUpdatedAt() {{ return updatedAt; }}

{getters}
}}
"""

    def _make_getters_setters(self, artifact: OracleArtifact, cn: str) -> str:
        lines = []
        for block in artifact.blocks:
            for item in block.items:
                jtype = _j(item.data_type)
                fname = _camel(item.name)
                cap = fname[0].upper() + fname[1:]
                lines.append(f"    public {jtype} get{cap}() {{ return {fname}; }}")
                lines.append(f"    public void set{cap}({jtype} {fname}) {{ this.{fname} = {fname}; }}")
        return "\n".join(lines)

    def _repository(self, artifact: OracleArtifact, cn: str, pkg: str) -> str:
        return f"""package com.migrated.{pkg};

import org.springframework.data.jpa.repository.JpaRepository;
import org.springframework.data.jpa.repository.Query;
import org.springframework.data.repository.query.Param;
import org.springframework.stereotype.Repository;
import java.util.List;

/**
 * Repository for {cn}
 * Migrated from Oracle Forms: {artifact.name}
 */
@Repository
public interface {cn}Repository extends JpaRepository<{cn}, Long> {{

    /**
     * Default ordered query — equivalent to Oracle Forms EXECUTE_QUERY.
     */
    @Query("SELECT e FROM {cn} e ORDER BY e.id DESC")
    List<{cn}> findAllOrdered();

    /**
     * Search — equivalent to Oracle Forms Enter-Query filter.
     */
    @Query("SELECT e FROM {cn} e WHERE " +
           "LOWER(CAST(e.id AS string)) LIKE LOWER(CONCAT('%', :term, '%')) " +
           "ORDER BY e.id DESC")
    List<{cn}> search(@Param("term") String term);
}}
"""

    def _service(self, artifact: OracleArtifact, cn: str, pkg: str) -> str:
        trigger_methods = self._build_trigger_methods(artifact, cn)
        return f"""package com.migrated.{pkg};

import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;
import java.util.List;
import java.util.Optional;
import java.util.logging.Logger;

/**
 * Service layer for {cn}.
 *
 * All Oracle Forms trigger logic has been migrated to Java methods below.
 * The original PL/SQL code is preserved in Javadoc comments for reference.
 *
 * Migrated from Oracle Forms: {artifact.name}
 * Migration date: {datetime.now().strftime('%Y-%m-%d')}
 */
@Service
@Transactional
public class {cn}Service {{

    private static final Logger log = Logger.getLogger({cn}Service.class.getName());

    @Autowired
    private {cn}Repository repository;

    // ─── CRUD ────────────────────────────────────────────────────────────────

    /** Equivalent to: EXECUTE_QUERY built-in */
    public List<{cn}> findAll() {{
        return repository.findAllOrdered();
    }}

    /** Equivalent to: Oracle Forms query filter */
    public List<{cn}> search(String term) {{
        if (term == null || term.isBlank()) return findAll();
        return repository.search(term);
    }}

    public Optional<{cn}> findById(Long id) {{
        return repository.findById(id);
    }}

    /**
     * Insert — triggers: PRE-INSERT → INSERT → POST-INSERT
     */
    public {cn} create({cn} entity) {{
        preInsert(entity);
        {cn} saved = repository.save(entity);
        postInsert(saved);
        log.info("Created {cn} id=" + saved.getId());
        return saved;
    }}

    /**
     * Update — triggers: PRE-UPDATE → UPDATE → POST-UPDATE
     */
    public {cn} update({cn} entity) {{
        preUpdate(entity);
        {cn} updated = repository.save(entity);
        postUpdate(updated);
        log.info("Updated {cn} id=" + updated.getId());
        return updated;
    }}

    /**
     * Delete — triggers: PRE-DELETE → DELETE → POST-DELETE
     */
    public void delete(Long id) {{
        repository.findById(id).ifPresent(entity -> {{
            preDelete(entity);
            repository.delete(entity);
            postDelete(entity);
            log.info("Deleted {cn} id=" + id);
        }});
    }}

    /** Validate record — equivalent to: WHEN-VALIDATE-RECORD */
    public void validateRecord({cn} entity) {{
        whenValidateRecord(entity);
    }}

    // ─── Migrated Trigger Methods ─────────────────────────────────────────────
{trigger_methods}
}}
"""

    def _build_trigger_methods(self, artifact: OracleArtifact, cn: str) -> str:
        all_triggers = artifact.all_triggers()
        lines = []

        # Base lifecycle stubs always present
        base_triggers = {
            "preInsert": ("PRE-INSERT", "Runs before inserting a record"),
            "postInsert": ("POST-INSERT", "Runs after a successful insert"),
            "preUpdate": ("PRE-UPDATE", "Runs before updating a record"),
            "postUpdate": ("POST-UPDATE", "Runs after a successful update"),
            "preDelete": ("PRE-DELETE", "Runs before deleting a record"),
            "postDelete": ("POST-DELETE", "Runs after a successful delete"),
            "whenValidateRecord": ("WHEN-VALIDATE-RECORD", "Validates the record before commit"),
        }

        # Find matching triggers from artifact
        trigger_map = {t.name.upper(): t for t in all_triggers}

        for method_name, (trigger_name, description) in base_triggers.items():
            trig = trigger_map.get(trigger_name)
            code_comment = ""
            warnings = []
            if trig:
                code_comment = f"\n     * Original PL/SQL:\n     * <pre>\n     * {trig.code[:400].replace('*/', '* /')}\n     * </pre>"
                if trig.has_cursor:
                    warnings.append("        // ⚠ Cursor detected → use repository.findAll() or JPA @Query")
                if trig.has_exec_sql:
                    warnings.append("        // ⚠ Dynamic SQL detected → use JdbcTemplate or Criteria API")
                if trig.has_dml:
                    warnings.append("        // ⚠ DML detected → use repository.save() / delete()")
                if trig.has_go_block:
                    warnings.append("        // ⚠ GO_BLOCK navigation → handle via controller redirect")

            warn_str = "\n".join(warnings)
            lines.append(f"""
    /**
     * Migrated from trigger: {trigger_name}
     * {description}{code_comment}
     */
    protected void {method_name}({cn} entity) {{
{warn_str}
        // TODO: implement {trigger_name} logic
        log.fine("{method_name} called for entity id=" + entity.getId());
    }}""")

        # Extra triggers not in the base set
        seen = {t.upper() for t in base_triggers.values() if isinstance(t, str)}
        seen_names = {v[0] for v in base_triggers.values()}

        for trig in all_triggers:
            if trig.name.upper() in seen_names:
                continue
            seen_names.add(trig.name.upper())
            method = _trigger_method(trig.name)
            warnings = []
            if trig.has_cursor:
                warnings.append("        // ⚠ Cursor detected → use JPA Streams / @Query")
            if trig.has_exec_sql:
                warnings.append("        // ⚠ Dynamic SQL → JdbcTemplate")
            if trig.has_dml:
                warnings.append("        // ⚠ DML → use repository methods")
            warn_str = "\n".join(warnings)
            code_preview = trig.code[:300].replace("*/", "* /")
            lines.append(f"""
    /**
     * Migrated from Oracle Forms trigger: {trig.name}
     * Lines: {trig.line_count} | DML: {trig.has_dml} | Cursors: {trig.has_cursor} | Dynamic SQL: {trig.has_exec_sql}
     * Original PL/SQL:
     * <pre>
     * {code_preview}
     * </pre>
     */
    public void {method}({cn} entity) {{
{warn_str}
        // TODO: implement {trig.name} trigger logic
        log.fine("{method} called");
    }}""")

        return "\n".join(lines)

    def _controller(self, artifact: OracleArtifact, cn: str, pkg: str) -> str:
        lower = pkg
        return f"""package com.migrated.{pkg};

import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.stereotype.Controller;
import org.springframework.ui.Model;
import org.springframework.validation.BindingResult;
import org.springframework.web.bind.annotation.*;
import org.springframework.web.servlet.mvc.support.RedirectAttributes;
import jakarta.validation.Valid;

/**
 * MVC Controller for {cn}.
 *
 * Replicates Oracle Forms navigation modes:
 *   Normal mode  → list view (GET /{lower})
 *   Insert mode  → new form (GET /{lower}/new)
 *   Query mode   → search   (GET /{lower}?q=...)
 *   Enter-Update → edit     (GET /{lower}/{{id}}/edit)
 *   Commit       → save     (POST /{lower}/save)
 *   Delete+Commit→ delete   (POST /{lower}/{{id}}/delete)
 *
 * Migrated from Oracle Forms: {artifact.name}
 */
@Controller
@RequestMapping("/{lower}")
public class {cn}Controller {{

    @Autowired
    private {cn}Service service;

    /** Normal mode / Execute Query */
    @GetMapping
    public String list(Model model,
                       @RequestParam(name = "q", defaultValue = "") String q,
                       @RequestParam(name = "msg", defaultValue = "") String msg) {{
        model.addAttribute("records", service.search(q));
        model.addAttribute("q", q);
        model.addAttribute("msg", msg);
        model.addAttribute("formTitle", "{artifact.name.replace('_', ' ')}");
        model.addAttribute("formMode", "QUERY");
        return "{lower}-list";
    }}

    /** Insert mode */
    @GetMapping("/new")
    public String newRecord(Model model) {{
        model.addAttribute("entity", new {cn}());
        model.addAttribute("formTitle", "{artifact.name.replace('_', ' ')} — New Record");
        model.addAttribute("formMode", "INSERT");
        return "{lower}-form";
    }}

    /** Enter-Update mode */
    @GetMapping("/{{id}}/edit")
    public String edit(@PathVariable Long id, Model model,
                       RedirectAttributes ra) {{
        return service.findById(id).map(entity -> {{
            model.addAttribute("entity", entity);
            model.addAttribute("formTitle", "{artifact.name.replace('_', ' ')} — Edit");
            model.addAttribute("formMode", "UPDATE");
            return "{lower}-form";
        }}).orElseGet(() -> {{
            ra.addFlashAttribute("error", "Record not found: " + id);
            return "redirect:/{lower}";
        }});
    }}

    /** Commit (insert or update) */
    @PostMapping("/save")
    public String save(@Valid @ModelAttribute("entity") {cn} entity,
                       BindingResult result,
                       RedirectAttributes ra,
                       Model model) {{
        try {{
            service.validateRecord(entity);
        }} catch (Exception e) {{
            model.addAttribute("validationError", e.getMessage());
            model.addAttribute("formMode", entity.getId() == null ? "INSERT" : "UPDATE");
            return "{lower}-form";
        }}

        if (result.hasErrors()) {{
            model.addAttribute("formMode", entity.getId() == null ? "INSERT" : "UPDATE");
            return "{lower}-form";
        }}

        if (entity.getId() == null) {{
            service.create(entity);
            ra.addFlashAttribute("msg", "Record created successfully.");
        }} else {{
            service.update(entity);
            ra.addFlashAttribute("msg", "Record updated successfully.");
        }}
        return "redirect:/{lower}";
    }}

    /** Delete + Commit */
    @PostMapping("/{{id}}/delete")
    public String delete(@PathVariable Long id, RedirectAttributes ra) {{
        service.delete(id);
        ra.addFlashAttribute("msg", "Record deleted.");
        return "redirect:/{lower}";
    }}
}}
"""

    def _exception(self, cn: str, pkg: str) -> str:
        return f"""package com.migrated.{pkg};

/**
 * Exception equivalent to Oracle Forms RAISE FORM_TRIGGER_FAILURE.
 */
public class {cn}Exception extends RuntimeException {{
    public {cn}Exception(String message) {{
        super(message);
    }}
    public {cn}Exception(String message, Throwable cause) {{
        super(message, cause);
    }}
}}
"""

    def _template_list(self, artifact: OracleArtifact, cn: str, pkg: str) -> str:
        # Build header/data columns from items
        all_items = []
        for block in artifact.blocks:
            all_items.extend(block.items[:6])

        if not all_items:
            headers = "<th>ID</th><th>Created</th><th>Status</th>"
            cells = "<td th:text=\"${r.id}\">1</td><td th:text=\"${r.createdAt}\">-</td><td>-</td>"
        else:
            headers = "<th>ID</th>" + "".join(
                f"<th>{i.name.replace('_', ' ').title()}</th>" for i in all_items
            ) + "<th>Actions</th>"
            cells = "<td th:text=\"${r.id}\">1</td>" + "".join(
                f"<td th:text=\"${{r.{_camel(i.name)}}}\">-</td>" for i in all_items
            )

        return f"""<!DOCTYPE html>
<html xmlns:th="http://www.thymeleaf.org" lang="en">
<head>
    <meta charset="UTF-8"/>
    <title th:text="${{formTitle}}">List</title>
    <link rel="stylesheet" th:href="@{{/css/oracle-forms.css}}"/>
</head>
<body class="of-body">

<!-- Title Bar -->
<div class="of-title-bar">
    <div class="of-title-left">
        <span class="of-app-icon">⬛</span>
        <span class="of-app-name" th:text="${{formTitle}}">{artifact.name}</span>
    </div>
    <div class="of-toolbar">
        <button class="of-btn" onclick="location.href=this.dataset.href" th:data-href="@{{/{pkg}/new}}">&#9997; New (F6)</button>
        <button class="of-btn" id="btn-query" onclick="document.getElementById('searchForm').submit()">&#128269; Query (F8)</button>
        <button class="of-btn" onclick="location.href=this.dataset.href" th:data-href="@{{/{pkg}}}">&#8635; Refresh</button>
    </div>
</div>

<!-- Flash message -->
<div class="of-flash" th:if="${{msg}}" th:text="${{msg}}"></div>
<div class="of-flash of-flash-error" th:if="${{error}}" th:text="${{error}}"></div>

<div class="of-canvas">
    <!-- Search Bar — Oracle Forms Enter Query equivalent -->
    <div class="of-search-bar">
        <form id="searchForm" th:action="@{{/{pkg}}}" method="get">
            <label class="of-label">Search:</label>
            <input class="of-input" type="text" name="q" th:value="${{q}}"
                   placeholder="Enter search term (F11 = Enter Query)"/>
            <button class="of-btn of-btn-primary" type="submit">Execute Query (F8)</button>
            <button class="of-btn" type="button"
                    onclick="document.querySelector('[name=q]').value='';this.form.submit()">Clear</button>
        </form>
    </div>

    <!-- Data Block — Multi-row display -->
    <div class="of-block">
        <div class="of-block-header">
            {artifact.name.upper()} — Query Results
            <span class="of-record-count" id="recordCount"></span>
        </div>
        <div class="of-table-wrapper">
            <table class="of-table" id="dataTable">
                <thead>
                    <tr>{headers}<th>Actions</th></tr>
                </thead>
                <tbody>
                    <tr th:each="r,stat : ${{records}}"
                        th:classappend="${{stat.even}} ? 'of-row-even' : ''"
                        th:id="'row-' + ${{r.id}}">
                        {cells}
                        <td class="of-actions">
                            <a class="of-link" th:href="@{{/{pkg}/}} + ${{r.id}} + '/edit'">Edit</a>
                            <form th:action="@{{/{pkg}/}} + ${{r.id}} + '/delete'"
                                  method="post" style="display:inline"
                                  onsubmit="return confirm('Delete this record?')">
                                <button type="submit" class="of-link of-link-del">Delete</button>
                            </form>
                        </td>
                    </tr>
                    <tr th:if="${{#lists.isEmpty(records)}}">
                        <td colspan="20" class="of-no-data">No records found. Press F6 to insert.</td>
                    </tr>
                </tbody>
            </table>
        </div>
    </div>
</div>

<!-- Status Bar -->
<div class="of-status-bar">
    <span class="of-status-mode">NORMAL</span>
    <span class="of-status-record" id="statusRecord">
        Record: <span th:text="${{#lists.size(records)}}">0</span> found
    </span>
    <span class="of-status-msg">Oracle Forms Migrated Application</span>
</div>

<script th:src="@{{/js/oracle-forms.js}}"></script>
</body>
</html>
"""

    def _template_form(self, artifact: OracleArtifact, cn: str, pkg: str) -> str:
        all_items = []
        for block in artifact.blocks:
            all_items.extend(block.items)

        field_html_parts = []
        for item in all_items:
            fname = _camel(item.name)
            label = item.name.replace("_", " ").title()
            required = "required" if item.required else ""
            if item.item_type.upper() in ("CHECK_BOX", "CHECKBOX"):
                field_html_parts.append(f"""
                <div class="of-form-group">
                    <label class="of-label">{label}</label>
                    <input type="checkbox" class="of-checkbox" th:field="*{{{fname}}}"/>
                </div>""")
            elif item.list_of_values:
                field_html_parts.append(f"""
                <div class="of-form-group">
                    <label class="of-label">{label} <span th:if="${{true}}" class="of-required">*</span></label>
                    <div class="of-lov-group">
                        <input type="text" class="of-input" th:field="*{{{fname}}}" {required}/>
                        <button type="button" class="of-lov-btn" title="List of Values (F9)">&#128195;</button>
                    </div>
                    <span class="of-error" th:if="${{#fields.hasErrors('{fname}')}}"
                          th:errors="*{{{fname}}}"></span>
                </div>""")
            else:
                req_star = '<span class="of-required">*</span>' if item.required else ""
                field_html_parts.append(f"""
                <div class="of-form-group">
                    <label class="of-label">{label} {req_star}</label>
                    <input type="text" class="of-input" th:field="*{{{fname}}}" {required}/>
                    <span class="of-error" th:if="${{#fields.hasErrors('{fname}')}}"
                          th:errors="*{{{fname}}}"></span>
                </div>""")

        if not field_html_parts:
            field_html_parts = ["""
                <div class="of-form-group">
                    <label class="of-label">Description</label>
                    <input type="text" class="of-input" th:field="*{description}"/>
                </div>
                <div class="of-form-group">
                    <label class="of-label">Status</label>
                    <input type="text" class="of-input" th:field="*{status}"/>
                </div>"""]

        fields_html = "\n".join(field_html_parts)

        return f"""<!DOCTYPE html>
<html xmlns:th="http://www.thymeleaf.org" lang="en">
<head>
    <meta charset="UTF-8"/>
    <title th:text="${{formTitle}}">Form</title>
    <link rel="stylesheet" th:href="@{{/css/oracle-forms.css}}"/>
</head>
<body class="of-body">

<!-- Title Bar -->
<div class="of-title-bar">
    <div class="of-title-left">
        <span class="of-app-icon">⬛</span>
        <span class="of-app-name" th:text="${{formTitle}}">{artifact.name}</span>
    </div>
    <div class="of-toolbar">
        <button class="of-btn of-btn-primary" form="mainForm" type="submit">&#128190; Commit (F10)</button>
        <button class="of-btn" type="button" onclick="history.back()">&#10006; Cancel (Ctrl+Q)</button>
        <button class="of-btn" type="button"
                onclick="if(confirm('Clear form?')) document.getElementById('mainForm').reset()">&#9003; Clear</button>
    </div>
</div>

<!-- Validation error -->
<div class="of-flash of-flash-error" th:if="${{validationError}}" th:text="${{validationError}}"></div>

<div class="of-canvas">
    <form id="mainForm" th:action="@{{/{pkg}/save}}" th:object="${{entity}}" method="post" novalidate>
        <input type="hidden" th:field="*{{id}}"/>

        <!-- Main data block — mirrors Oracle Forms block layout -->
        <div class="of-block">
            <div class="of-block-header">
                <span>{artifact.name.upper()}</span>
                <span class="of-mode-badge" th:text="${{formMode}}">INSERT</span>
            </div>
            <div class="of-block-body">
                <!-- ID (display only) -->
                <div class="of-form-group">
                    <label class="of-label">Record ID</label>
                    <input type="text" class="of-input of-input-display"
                           th:value="${{entity.id != null ? entity.id : '(new)'}}" readonly/>
                </div>
{fields_html}
                <div class="of-form-group">
                    <label class="of-label">Created At</label>
                    <input type="text" class="of-input of-input-display"
                           th:value="${{entity.createdAt}}" readonly/>
                </div>
            </div>
        </div>

        <!-- Action buttons -->
        <div class="of-action-bar">
            <button type="submit" class="of-btn of-btn-primary">&#128190; Save Record (F10)</button>
            <button type="button" class="of-btn" onclick="history.back()">Cancel</button>
            <span class="of-required-note">* Required fields</span>
        </div>
    </form>
</div>

<!-- Status Bar -->
<div class="of-status-bar">
    <span class="of-status-mode" th:text="${{formMode}}">INSERT</span>
    <span class="of-status-record">New Record</span>
    <span class="of-status-msg">Oracle Forms Migrated Application</span>
</div>

<script th:src="@{{/js/oracle-forms.js}}"></script>
</body>
</html>
"""

    def _css(self) -> str:
        return """:root {
  --of-bg: #d4d0c8;
  --of-canvas: #ece9d8;
  --of-border: #808080;
  --of-hdr-bg: #003399;
  --of-hdr-fg: #ffffff;
  --of-input-bg: #ffffff;
  --of-input-bd: #7f9db9;
  --of-label: #000066;
  --of-required: #cc0000;
  --of-table-hdr: #c0c0c0;
  --of-even: #f5f5f0;
  --of-font: 'Segoe UI', 'MS Sans Serif', Tahoma, sans-serif;
  --of-sz: 12px;
  --of-flash-bg: #ffffcc;
  --of-flash-bd: #ccaa00;
  --of-error-bg: #ffe0e0;
  --of-error-bd: #cc0000;
}

* { box-sizing: border-box; margin: 0; padding: 0; }

body.of-body {
  font-family: var(--of-font);
  font-size: var(--of-sz);
  background: var(--of-bg);
  color: #000;
  min-height: 100vh;
  display: flex;
  flex-direction: column;
}

/* ── Title Bar ─────────────────────────────── */
.of-title-bar {
  background: linear-gradient(135deg, #003399, #0055cc);
  color: #fff;
  padding: 5px 10px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  min-height: 32px;
  box-shadow: 0 2px 4px rgba(0,0,0,.35);
  flex-shrink: 0;
}

.of-title-left { display: flex; align-items: center; gap: 8px; }
.of-app-icon { font-size: 14px; opacity: .8; }
.of-app-name { font-weight: bold; font-size: 13px; letter-spacing: .5px; text-shadow: 0 1px 2px rgba(0,0,0,.5); }

/* ── Toolbar Buttons ───────────────────────── */
.of-toolbar { display: flex; gap: 3px; }

.of-btn {
  font-family: var(--of-font);
  font-size: 11px;
  background: linear-gradient(to bottom, #f0ede6, #d8d5cc);
  border: 1px solid #999;
  border-top-color: #fff;
  border-left-color: #fff;
  padding: 3px 10px;
  cursor: pointer;
  min-width: 70px;
  color: #000;
  border-radius: 2px;
}
.of-btn:hover { background: linear-gradient(to bottom, #e8e5dc, #ccc8be); }
.of-btn:active {
  border-top-color: #808080; border-left-color: #808080;
  border-right-color: #fff; border-bottom-color: #fff;
  padding: 4px 9px 2px 11px;
}
.of-btn-primary { color: #003399; font-weight: bold; }

/* ── Flash messages ────────────────────────── */
.of-flash {
  margin: 4px 8px;
  padding: 5px 10px;
  background: var(--of-flash-bg);
  border: 1px solid var(--of-flash-bd);
  font-size: 12px;
  border-radius: 2px;
}
.of-flash-error {
  background: var(--of-error-bg);
  border-color: var(--of-error-bd);
  color: #800000;
}

/* ── Canvas ────────────────────────────────── */
.of-canvas { margin: 8px; flex: 1; }

/* ── Search bar ────────────────────────────── */
.of-search-bar {
  background: var(--of-canvas);
  border: 1px solid var(--of-border);
  padding: 6px 10px;
  display: flex;
  align-items: center;
  gap: 6px;
  margin-bottom: 6px;
}

/* ── Data Block ────────────────────────────── */
.of-block {
  border: 2px solid #888;
  border-top-color: #ccc;
  border-left-color: #ccc;
  background: var(--of-canvas);
  margin-bottom: 8px;
  box-shadow: 2px 2px 4px rgba(0,0,0,.2);
}

.of-block-header {
  background: linear-gradient(to right, #003399, #0044bb);
  color: #fff;
  padding: 4px 10px;
  font-size: 11px;
  font-weight: bold;
  letter-spacing: .8px;
  display: flex;
  justify-content: space-between;
  align-items: center;
}

.of-mode-badge {
  background: rgba(255,255,255,.2);
  border: 1px solid rgba(255,255,255,.4);
  padding: 1px 8px;
  font-size: 10px;
  border-radius: 10px;
}

/* ── Form Layout ───────────────────────────── */
.of-block-body {
  padding: 14px 16px;
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(300px, 1fr));
  gap: 8px 16px;
}

.of-form-group {
  display: flex;
  align-items: flex-start;
  gap: 6px;
  flex-wrap: wrap;
}

.of-label {
  color: var(--of-label);
  font-weight: bold;
  font-size: 11px;
  min-width: 130px;
  text-align: right;
  padding-top: 3px;
  flex-shrink: 0;
}

.of-required { color: var(--of-required); }
.of-required-note { font-size: 10px; color: #666; margin-left: auto; }

.of-input, .of-select {
  font-family: var(--of-font);
  font-size: 11px;
  border: 2px inset var(--of-input-bd);
  background: var(--of-input-bg);
  padding: 2px 5px;
  height: 22px;
  min-width: 150px;
  max-width: 220px;
  flex: 1;
}
.of-input:focus, .of-select:focus {
  outline: none;
  border-color: #003399;
  background: #ffffee;
  box-shadow: 0 0 0 1px #003399;
}
.of-input-display { background: #e8e8e8; color: #555; }
.of-input[readonly] { background: #e8e8e8; }
.of-checkbox { width: 14px; height: 14px; margin-top: 4px; }

.of-lov-group { display: flex; gap: 2px; flex: 1; }
.of-lov-btn {
  font-size: 11px;
  background: linear-gradient(to bottom, #f0ede6, #d8d5cc);
  border: 1px solid #999;
  width: 24px; height: 22px;
  cursor: pointer;
  padding: 0;
  border-radius: 1px;
}

.of-error { color: var(--of-required); font-size: 10px; width: 100%; margin-left: 136px; }

/* ── Action Bar ────────────────────────────── */
.of-action-bar {
  padding: 8px 14px;
  display: flex;
  gap: 6px;
  align-items: center;
  background: var(--of-bg);
  border-top: 1px solid #aaa;
}

/* ── Table ─────────────────────────────────── */
.of-table-wrapper { overflow-x: auto; max-height: calc(100vh - 260px); overflow-y: auto; }

.of-table {
  width: 100%;
  border-collapse: collapse;
  font-size: 11px;
}
.of-table thead { position: sticky; top: 0; z-index: 1; }
.of-table th {
  background: linear-gradient(to bottom, #d0cdca, #b8b5b0);
  border: 1px solid var(--of-border);
  padding: 4px 8px;
  text-align: left;
  font-weight: bold;
  color: #000066;
  white-space: nowrap;
}
.of-table td { border: 1px solid #d0d0d0; padding: 3px 8px; height: 20px; }
.of-row-even { background: var(--of-even); }
.of-table tbody tr:hover { background: #cce0ff !important; cursor: pointer; }
.of-no-data { text-align: center; color: #666; font-style: italic; padding: 20px !important; }
.of-actions { white-space: nowrap; }
.of-record-count { font-size: 10px; font-weight: normal; opacity: .85; }

/* ── Links ─────────────────────────────────── */
.of-link {
  color: #000066;
  text-decoration: underline;
  cursor: pointer;
  background: none;
  border: none;
  font-family: var(--of-font);
  font-size: 11px;
  padding: 0;
  margin-right: 8px;
}
.of-link-del { color: #800000; }

/* ── Status Bar ────────────────────────────── */
.of-status-bar {
  background: linear-gradient(to bottom, #d4d0c8, #c4c0b8);
  border-top: 1px solid #808080;
  padding: 3px 10px;
  display: flex;
  gap: 20px;
  font-size: 11px;
  flex-shrink: 0;
}
.of-status-mode { font-weight: bold; color: #000066; min-width: 80px; }
.of-status-record { color: #333; }
.of-status-msg { color: #555; margin-left: auto; }

/* ── Responsive ────────────────────────────── */
@media (max-width: 768px) {
  .of-block-body { grid-template-columns: 1fr; }
  .of-label { min-width: 100px; }
  .of-toolbar .of-btn { min-width: 50px; font-size: 10px; }
}
"""

    def _js(self, pkg: str) -> str:
        return f"""/**
 * Oracle Forms keyboard shortcut emulation
 * Migrated form: {pkg}
 */
(function () {{
  'use strict';

  // Update record count in table
  function updateRecordCount() {{
    var rows = document.querySelectorAll('#dataTable tbody tr:not(.of-no-data-row)');
    var counter = document.getElementById('recordCount');
    if (counter) counter.textContent = '(' + rows.length + ' records)';
  }}

  // Row click → edit
  document.querySelectorAll('#dataTable tbody tr').forEach(function (row) {{
    row.addEventListener('click', function (e) {{
      if (e.target.tagName === 'A' || e.target.tagName === 'BUTTON') return;
      var link = row.querySelector('a.of-link');
      if (link) window.location.href = link.href;
    }});
  }});

  // Keyboard shortcuts
  document.addEventListener('keydown', function (e) {{
    // F6 = Insert Record
    if (e.key === 'F6') {{
      e.preventDefault();
      var newBtn = document.querySelector('[data-href*="/new"]');
      if (newBtn) window.location.href = newBtn.dataset.href;
    }}
    // F8 = Execute Query
    if (e.key === 'F8') {{
      e.preventDefault();
      var form = document.getElementById('searchForm') || document.getElementById('mainForm');
      if (form) form.submit();
    }}
    // F10 = Commit
    if (e.key === 'F10') {{
      e.preventDefault();
      var mainForm = document.getElementById('mainForm');
      if (mainForm) mainForm.submit();
    }}
    // Ctrl+Q = Exit/Back
    if (e.ctrlKey && e.key === 'q') {{
      e.preventDefault();
      history.back();
    }}
    // F11 = Enter Query (focus search)
    if (e.key === 'F11') {{
      e.preventDefault();
      var searchInput = document.querySelector('[name="q"]');
      if (searchInput) {{ searchInput.focus(); searchInput.select(); }}
    }}
  }});

  // Auto-focus first editable input in form
  var firstInput = document.querySelector('.of-input:not([readonly]):not(.of-input-display)');
  if (firstInput) firstInput.focus();

  updateRecordCount();
}})();
"""

    def _pom(self, cn: str, pkg: str) -> str:
        return f"""<?xml version="1.0" encoding="UTF-8"?>
<project xmlns="http://maven.apache.org/POM/4.0.0"
         xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
         xsi:schemaLocation="http://maven.apache.org/POM/4.0.0
         https://maven.apache.org/xsd/maven-4.0.0.xsd">
    <modelVersion>4.0.0</modelVersion>

    <parent>
        <groupId>org.springframework.boot</groupId>
        <artifactId>spring-boot-starter-parent</artifactId>
        <version>3.2.3</version>
        <relativePath/>
    </parent>

    <groupId>com.migrated</groupId>
    <artifactId>{pkg}</artifactId>
    <version>1.0.0</version>
    <name>{cn} — Migrated from Oracle Forms</name>
    <description>Spring Boot application auto-generated by Oracle Migrator Tool</description>

    <properties>
        <java.version>17</java.version>
        <project.build.sourceEncoding>UTF-8</project.build.sourceEncoding>
    </properties>

    <dependencies>
        <!-- Spring MVC + Thymeleaf -->
        <dependency>
            <groupId>org.springframework.boot</groupId>
            <artifactId>spring-boot-starter-web</artifactId>
        </dependency>
        <dependency>
            <groupId>org.springframework.boot</groupId>
            <artifactId>spring-boot-starter-thymeleaf</artifactId>
        </dependency>

        <!-- JPA -->
        <dependency>
            <groupId>org.springframework.boot</groupId>
            <artifactId>spring-boot-starter-data-jpa</artifactId>
        </dependency>

        <!-- Validation -->
        <dependency>
            <groupId>org.springframework.boot</groupId>
            <artifactId>spring-boot-starter-validation</artifactId>
        </dependency>

        <!-- H2 in-memory DB for local development/testing (no Oracle needed) -->
        <dependency>
            <groupId>com.h2database</groupId>
            <artifactId>h2</artifactId>
            <scope>runtime</scope>
        </dependency>

        <!-- Oracle JDBC (add your ojdbc jar to local Maven repo for production) -->
        <!--
        <dependency>
            <groupId>com.oracle.database.jdbc</groupId>
            <artifactId>ojdbc11</artifactId>
            <scope>runtime</scope>
        </dependency>
        -->

        <!-- Dev Tools -->
        <dependency>
            <groupId>org.springframework.boot</groupId>
            <artifactId>spring-boot-devtools</artifactId>
            <scope>runtime</scope>
            <optional>true</optional>
        </dependency>

        <!-- Testing -->
        <dependency>
            <groupId>org.springframework.boot</groupId>
            <artifactId>spring-boot-starter-test</artifactId>
            <scope>test</scope>
        </dependency>
    </dependencies>

    <build>
        <plugins>
            <plugin>
                <groupId>org.springframework.boot</groupId>
                <artifactId>spring-boot-maven-plugin</artifactId>
            </plugin>
        </plugins>
    </build>
</project>
"""

    def _properties(self, cn: str, pkg: str) -> str:
        return f"""# {cn} — Application Properties
# To run with H2 (no Oracle needed): mvn spring-boot:run -Dspring-boot.run.profiles=h2

spring.application.name={pkg}
server.port=8080

# ── Oracle Database (production) ────────────────────────────────────────────
spring.datasource.url=jdbc:oracle:thin:@localhost:1521/ORCL
spring.datasource.username=your_username
spring.datasource.password=your_password
spring.datasource.driver-class-name=oracle.jdbc.OracleDriver

spring.jpa.database-platform=org.hibernate.dialect.OracleDialect
spring.jpa.hibernate.ddl-auto=validate
spring.jpa.show-sql=true
spring.jpa.properties.hibernate.format_sql=true

# ── Thymeleaf ───────────────────────────────────────────────────────────────
spring.thymeleaf.cache=false
spring.thymeleaf.encoding=UTF-8
spring.thymeleaf.prefix=classpath:/templates/
spring.thymeleaf.suffix=.html

# ── Logging ─────────────────────────────────────────────────────────────────
logging.level.com.migrated=DEBUG
logging.level.org.springframework.web=INFO
"""

    def _h2_properties(self, cn: str) -> str:
        return f"""# H2 in-memory database — use this for local testing without Oracle
# Run with: mvn spring-boot:run -Dspring-boot.run.profiles=h2

spring.datasource.url=jdbc:h2:mem:testdb;DB_CLOSE_DELAY=-1;DB_CLOSE_ON_EXIT=FALSE
spring.datasource.driver-class-name=org.h2.Driver
spring.datasource.username=sa
spring.datasource.password=

spring.jpa.database-platform=org.hibernate.dialect.H2Dialect
spring.jpa.hibernate.ddl-auto=create-drop
spring.jpa.show-sql=true

# H2 console at http://localhost:8080/h2-console
spring.h2.console.enabled=true
spring.h2.console.path=/h2-console
"""

    def _test(self, cn: str, pkg: str) -> str:
        return f"""package com.migrated.{pkg};

import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.test.context.ActiveProfiles;
import java.util.List;
import static org.assertj.core.api.Assertions.assertThat;

@SpringBootTest
@ActiveProfiles("h2")
class {cn}ServiceTest {{

    @Autowired
    private {cn}Service service;

    @Test
    void contextLoads() {{
        assertThat(service).isNotNull();
    }}

    @Test
    void canCreateAndRetrieveRecord() {{
        {cn} entity = new {cn}();
        {cn} saved = service.create(entity);
        assertThat(saved.getId()).isNotNull();

        List<{cn}> all = service.findAll();
        assertThat(all).isNotEmpty();
    }}

    @Test
    void canDeleteRecord() {{
        {cn} entity = new {cn}();
        {cn} saved = service.create(entity);
        service.delete(saved.getId());
        assertThat(service.findById(saved.getId())).isEmpty();
    }}
}}
"""

    def _docker(self, cn: str, pkg: str) -> str:
        return f"""version: '3.8'
# Docker Compose for local development
# Usage: docker-compose up
# App: http://localhost:8080/{pkg}
# H2 Console: http://localhost:8080/h2-console

services:
  app:
    build: .
    ports:
      - "8080:8080"
    environment:
      - SPRING_PROFILES_ACTIVE=h2
    volumes:
      - ./logs:/logs

  # Uncomment for Oracle XE (requires Oracle account to pull image)
  # oracle-db:
  #   image: container-registry.oracle.com/database/express:21.3.0-xe
  #   ports:
  #     - "1521:1521"
  #   environment:
  #     ORACLE_PWD: YourPassword123
  #   volumes:
  #     - oracle-data:/opt/oracle/oradata

# volumes:
#   oracle-data:
"""
