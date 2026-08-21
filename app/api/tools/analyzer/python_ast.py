from __future__ import annotations

import ast
import time
from typing import Any, Literal
from pydantic import BaseModel, Field


MAX_CANDIDATE_FILES = 20
MAX_FILE_SIZE_BYTES = 512 * 1024  # 500 KB
MAX_TOTAL_PAYLOAD_BYTES = 10 * 1024 * 1024  # 10 MB
MAX_AST_DEPTH = 50
MAX_AST_NODES_PER_FILE = 25000


class PythonFileItem(BaseModel):
    path: str
    content: str


class PythonASTRequest(BaseModel):
    protocolVersion: str = Field(default="1.0.0")
    taskId: str
    sessionId: str
    files: list[PythonFileItem]
    extractors: list[str] = Field(
        default=["routes", "models", "env_references", "imports"]
    )


class RouteItem(BaseModel):
    method: str
    path: str
    sourceFile: str
    lineNumber: int | None = None
    inferredHandler: str | None = None
    authRequired: bool = False
    framework: str | None = None


class ModelFieldItem(BaseModel):
    name: str
    type: str
    required: bool = True
    tag: str | None = None


class ModelItem(BaseModel):
    name: str
    sourceFile: str
    lineNumber: int
    framework: str | None = None
    fields: list[ModelFieldItem] = Field(default_factory=list)


class EnvReferenceItem(BaseModel):
    name: str
    sourceFile: str
    lineNumber: int
    accessMechanism: str


class EvidenceItem(BaseModel):
    filePath: str
    ruleType: str
    detail: str
    lineNumber: int | None = None


class DiagnosticItem(BaseModel):
    sourceFile: str | None = None
    code: str
    message: str
    severity: Literal["info", "warning", "error"] = "warning"


class ErrorDetail(BaseModel):
    code: str
    message: str


class PythonASTResponse(BaseModel):
    protocolVersion: str = "1.0.0"
    taskId: str
    status: Literal["SUCCESS", "ERROR"]
    durationMs: int
    nodesProcessed: int = 0
    routes: list[RouteItem] = Field(default_factory=list)
    models: list[ModelItem] = Field(default_factory=list)
    envReferences: list[EnvReferenceItem] = Field(default_factory=list)
    evidence: list[EvidenceItem] = Field(default_factory=list)
    diagnostics: list[DiagnosticItem] = Field(default_factory=list)
    error: ErrorDetail | None = None


class ASTVisitor(ast.NodeVisitor):
    def __init__(self, filename: str, extractors: set[str]):
        self.filename = filename
        self.extractors = extractors
        self.depth = 0
        self.nodes_visited = 0
        self.max_depth_exceeded = False
        self.max_nodes_exceeded = False

        self.routes: list[RouteItem] = []
        self.models: list[ModelItem] = []
        self.env_references: list[EnvReferenceItem] = []
        self.evidence: list[EvidenceItem] = []
        self.diagnostics: list[DiagnosticItem] = []

    def visit(self, node: ast.AST) -> Any:
        self.nodes_visited += 1

        if self.depth > MAX_AST_DEPTH:
            if not self.max_depth_exceeded:
                self.max_depth_exceeded = True
                self.diagnostics.append(
                    DiagnosticItem(
                        sourceFile=self.filename,
                        code="MAX_DEPTH_EXCEEDED",
                        message=f"AST depth exceeded {MAX_AST_DEPTH}",
                        severity="info",
                    )
                )
            return

        if self.nodes_visited > MAX_AST_NODES_PER_FILE:
            if not self.max_nodes_exceeded:
                self.max_nodes_exceeded = True
                self.diagnostics.append(
                    DiagnosticItem(
                        sourceFile=self.filename,
                        code="MAX_NODES_EXCEEDED",
                        message=f"AST node limit {MAX_AST_NODES_PER_FILE} reached",
                        severity="info",
                    )
                )
            return

        self.depth += 1
        try:
            super().visit(node)
        finally:
            self.depth -= 1

    def visit_Import(self, node: ast.Import) -> Any:
        if "imports" in self.extractors:
            for alias in node.names:
                tech = self._map_module_to_tech(alias.name)
                if tech:
                    self.evidence.append(
                        EvidenceItem(
                            filePath=self.filename,
                            ruleType="source_import",
                            detail=f"import {alias.name}",
                            lineNumber=getattr(node, "lineno", None),
                        )
                    )
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> Any:
        if "imports" in self.extractors and node.module:
            tech = self._map_module_to_tech(node.module)
            if tech:
                self.evidence.append(
                    EvidenceItem(
                        filePath=self.filename,
                        ruleType="source_import",
                        detail=f"from {node.module} import ...",
                        lineNumber=getattr(node, "lineno", None),
                    )
                )
        self.generic_visit(node)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> Any:
        if "routes" in self.extractors:
            self._inspect_route_decorators(node)
        self.generic_visit(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> Any:
        if "routes" in self.extractors:
            self._inspect_route_decorators(node)
        self.generic_visit(node)

    def visit_ClassDef(self, node: ast.ClassDef) -> Any:
        if "models" in self.extractors:
            self._inspect_class_model(node)
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> Any:
        if "env_references" in self.extractors:
            self._inspect_env_call(node)
        if "routes" in self.extractors:
            self._inspect_django_path_call(node)
        self.generic_visit(node)

    def visit_Subscript(self, node: ast.Subscript) -> Any:
        if "env_references" in self.extractors:
            self._inspect_env_subscript(node)
        self.generic_visit(node)

    def _map_module_to_tech(self, mod_name: str) -> str | None:
        mod_lower = mod_name.lower()
        if "fastapi" in mod_lower:
            return "FastAPI"
        if "flask" in mod_lower:
            return "Flask"
        if "django" in mod_lower:
            return "Django"
        if "pydantic" in mod_lower:
            return "Pydantic"
        if "sqlalchemy" in mod_lower:
            return "SQLAlchemy"
        if "tortoise" in mod_lower:
            return "Tortoise ORM"
        return None

    def _inspect_route_decorators(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        for dec in node.decorator_list:
            if isinstance(dec, ast.Call) and isinstance(dec.func, ast.Attribute):
                method_name = dec.func.attr.upper()
                # FastAPI: @app.get("/path"), @router.post("/path"), etc.
                if method_name in {"GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"}:
                    path_val = self._extract_first_str_arg(dec)
                    if path_val:
                        auth_required = self._detect_auth_in_decorator(dec)
                        self.routes.append(
                            RouteItem(
                                method=method_name,
                                path=path_val,
                                sourceFile=self.filename,
                                lineNumber=getattr(node, "lineno", None),
                                inferredHandler=node.name,
                                authRequired=auth_required,
                                framework="fastapi",
                            )
                        )
                # Flask: @app.route("/path", methods=["GET", "POST"])
                elif dec.func.attr == "route":
                    path_val = self._extract_first_str_arg(dec)
                    if path_val:
                        methods = self._extract_flask_methods(dec)
                        for m in methods:
                            self.routes.append(
                                RouteItem(
                                    method=m,
                                    path=path_val,
                                    sourceFile=self.filename,
                                    lineNumber=getattr(node, "lineno", None),
                                    inferredHandler=node.name,
                                    authRequired=False,
                                    framework="flask",
                                )
                            )

    def _detect_auth_in_decorator(self, call: ast.Call) -> bool:
        # Static inspection of dependencies=[Depends(get_current_user)] or Security(...)
        for keyword in call.keywords:
            if keyword.arg in {"dependencies", "security"}:
                return True
        return False

    def _extract_flask_methods(self, call: ast.Call) -> list[str]:
        for kw in call.keywords:
            if kw.arg == "methods" and isinstance(kw.value, (ast.List, ast.Tuple)):
                res = []
                for elt in kw.value.elts:
                    if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
                        res.append(elt.value.upper())
                if res:
                    return res
        return ["GET"]

    def _extract_first_str_arg(self, call: ast.Call) -> str | None:
        if call.args and isinstance(call.args[0], ast.Constant) and isinstance(call.args[0].value, str):
            return call.args[0].value
        for kw in call.keywords:
            if kw.arg in {"path", "rule"} and isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, str):
                return kw.value.value
        return None

    def _inspect_django_path_call(self, node: ast.Call) -> None:
        # Django: path("users/", views.users) or re_path(r"^users/", ...)
        func_name = ""
        if isinstance(node.func, ast.Name):
            func_name = node.func.id
        elif isinstance(node.func, ast.Attribute):
            func_name = node.func.attr

        if func_name in {"path", "re_path"} and node.args:
            path_val = self._extract_first_str_arg(node)
            if path_val is not None:
                if not path_val.startswith("/"):
                    path_val = "/" + path_val
                handler_name = None
                if len(node.args) >= 2:
                    handler_name = self._ast_to_name_str(node.args[1])
                self.routes.append(
                    RouteItem(
                        method="GET",
                        path=path_val,
                        sourceFile=self.filename,
                        lineNumber=getattr(node, "lineno", None),
                        inferredHandler=handler_name,
                        authRequired=False,
                        framework="django",
                    )
                )

    def _inspect_class_model(self, node: ast.ClassDef) -> None:
        base_names = [self._ast_to_name_str(b) for b in node.bases]
        is_pydantic = any("BaseModel" in b for b in base_names)
        is_sqlalchemy = any("Base" in b or "Model" in b for b in base_names)

        if not (is_pydantic or is_sqlalchemy):
            # Check body for Column declarations
            for item in node.body:
                if isinstance(item, ast.Assign) and isinstance(item.value, ast.Call):
                    func_str = self._ast_to_name_str(item.value.func)
                    if "Column" in func_str:
                        is_sqlalchemy = True
                        break

        if is_pydantic or is_sqlalchemy:
            framework = "pydantic" if is_pydantic else "sqlalchemy"
            model_item = ModelItem(
                name=node.name,
                sourceFile=self.filename,
                lineNumber=getattr(node, "lineno", 1),
                framework=framework,
            )

            for stmt in node.body:
                # Pydantic annotations: name: type [= default]
                if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
                    field_name = stmt.target.id
                    type_str = self._ast_to_name_str(stmt.annotation)
                    required = stmt.value is None or not (
                        type_str.startswith("Optional") or "None" in type_str
                    )
                    model_item.fields.append(
                        ModelFieldItem(
                            name=field_name,
                            type=type_str,
                            required=required,
                        )
                    )
                # SQLAlchemy Column assigns: id = Column(Integer, primary_key=True)
                elif isinstance(stmt, ast.Assign):
                    for target in stmt.targets:
                        if isinstance(target, ast.Name) and isinstance(stmt.value, ast.Call):
                            func_str = self._ast_to_name_str(stmt.value.func)
                            if "Column" in func_str:
                                col_type = "Any"
                                if stmt.value.args:
                                    col_type = self._ast_to_name_str(stmt.value.args[0])
                                model_item.fields.append(
                                    ModelFieldItem(
                                        name=target.id,
                                        type=col_type,
                                        required=True,
                                    )
                                )

            if model_item.fields or is_pydantic or is_sqlalchemy:
                self.models.append(model_item)

    def _inspect_env_call(self, node: ast.Call) -> None:
        # os.getenv("VAR") or os.environ.get("VAR")
        if isinstance(node.func, ast.Attribute):
            attr_name = node.func.attr
            caller = self._ast_to_name_str(node.func.value)
            if (attr_name == "getenv" and "os" in caller) or (
                attr_name == "get" and "environ" in caller
            ):
                if node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
                    var_name = node.args[0].value
                    self.env_references.append(
                        EnvReferenceItem(
                            name=var_name,
                            sourceFile=self.filename,
                            lineNumber=getattr(node, "lineno", 1),
                            accessMechanism=f"{caller}.{attr_name}",
                        )
                    )

    def _inspect_env_subscript(self, node: ast.Subscript) -> None:
        # os.environ["VAR"]
        caller = self._ast_to_name_str(node.value)
        if "environ" in caller and isinstance(node.slice, ast.Constant) and isinstance(node.slice.value, str):
            var_name = node.slice.value
            self.env_references.append(
                EnvReferenceItem(
                    name=var_name,
                    sourceFile=self.filename,
                    lineNumber=getattr(node, "lineno", 1),
                    accessMechanism=f"{caller}[]",
                )
            )

    def _ast_to_name_str(self, node: ast.AST) -> str:
        if isinstance(node, ast.Name):
            return node.id
        if isinstance(node, ast.Attribute):
            return f"{self._ast_to_name_str(node.value)}.{node.attr}"
        if isinstance(node, ast.Constant):
            return str(node.value)
        if isinstance(node, ast.Subscript):
            return f"{self._ast_to_name_str(node.value)}[{self._ast_to_name_str(node.slice)}]"
        return "Unknown"


def analyze_python_ast(request: PythonASTRequest) -> PythonASTResponse:
    start_time = time.time()

    # 1. Payload & File Bound Validation
    if len(request.files) > MAX_CANDIDATE_FILES:
        return PythonASTResponse(
            taskId=request.taskId,
            status="ERROR",
            durationMs=int((time.time() - start_time) * 1000),
            error=ErrorDetail(
                code="PAYLOAD_TOO_LARGE",
                message=f"candidate files ({len(request.files)}) exceeds maximum {MAX_CANDIDATE_FILES}",
            ),
        )

    total_bytes = sum(len(f.content.encode("utf-8")) for f in request.files)
    if total_bytes > MAX_TOTAL_PAYLOAD_BYTES:
        return PythonASTResponse(
            taskId=request.taskId,
            status="ERROR",
            durationMs=int((time.time() - start_time) * 1000),
            error=ErrorDetail(
                code="PAYLOAD_TOO_LARGE",
                message=f"total payload size {total_bytes} bytes exceeds {MAX_TOTAL_PAYLOAD_BYTES} limit",
            ),
        )

    all_routes: list[RouteItem] = []
    all_models: list[ModelItem] = []
    all_env: list[EnvReferenceItem] = []
    all_evidence: list[EvidenceItem] = []
    all_diagnostics: list[DiagnosticItem] = []
    total_nodes = 0

    extractors_set = set(request.extractors)

    # 2. Parse Each Candidate File with Per-File Exception Isolation
    for file_item in request.files:
        norm_path = file_item.path.replace("\\", "/")

        if len(file_item.content.encode("utf-8")) > MAX_FILE_SIZE_BYTES:
            all_diagnostics.append(
                DiagnosticItem(
                    sourceFile=norm_path,
                    code="FILE_SIZE_LIMIT_EXCEEDED",
                    message=f"file exceeds {MAX_FILE_SIZE_BYTES} bytes limit",
                    severity="info",
                )
            )
            continue

        try:
            # PURE STATIC PARSE ONLY — NO EXECUTION
            tree = ast.parse(file_item.content, filename=norm_path)
            visitor = ASTVisitor(filename=norm_path, extractors=extractors_set)
            visitor.visit(tree)

            total_nodes += visitor.nodes_visited
            all_routes.extend(visitor.routes)
            all_models.extend(visitor.models)
            all_env.extend(visitor.env_references)
            all_evidence.extend(visitor.evidence)
            all_diagnostics.extend(visitor.diagnostics)

        except SyntaxError as se:
            all_diagnostics.append(
                DiagnosticItem(
                    sourceFile=norm_path,
                    code="PARSE_SYNTAX_ERROR",
                    message=f"syntax error at line {se.lineno}: {se.msg}",
                    severity="warning",
                )
            )
        except Exception as ex:
            all_diagnostics.append(
                DiagnosticItem(
                    sourceFile=norm_path,
                    code="PARSER_EXCEPTION",
                    message=str(ex),
                    severity="warning",
                )
            )

    # 3. Deterministic Sorting of Results
    all_routes.sort(key=lambda r: (r.method, r.path, r.sourceFile))
    all_models.sort(key=lambda m: (m.name, m.sourceFile))
    all_env.sort(key=lambda e: (e.name, e.sourceFile, e.lineNumber))
    all_evidence.sort(key=lambda ev: (ev.filePath, ev.ruleType, ev.detail))
    all_diagnostics.sort(key=lambda d: (d.sourceFile or "", d.code))

    duration_ms = int((time.time() - start_time) * 1000)

    return PythonASTResponse(
        protocolVersion="1.0.0",
        taskId=request.taskId,
        status="SUCCESS",
        durationMs=duration_ms,
        nodesProcessed=total_nodes,
        routes=all_routes,
        models=all_models,
        envReferences=all_env,
        evidence=all_evidence,
        diagnostics=all_diagnostics,
    )
