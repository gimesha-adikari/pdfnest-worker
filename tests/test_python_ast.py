from __future__ import annotations

import pytest
from app.api.tools.analyzer.python_ast import (
    PythonASTRequest,
    PythonFileItem,
    analyze_python_ast,
)


def test_fastapi_and_pydantic_extraction() -> None:
    source = """
from fastapi import APIRouter, Depends, Security
from pydantic import BaseModel
import os

router = APIRouter(prefix="/api/v1")

class UserDTO(BaseModel):
    id: int
    email: str
    is_active: bool = True

@router.get("/users")
async def list_users():
    db = os.getenv("DATABASE_URL")
    return []

@router.post("/users", dependencies=[Depends(lambda: None)])
async def create_user(user: UserDTO):
    secret = os.environ["API_KEY"]
    return user
"""
    req = PythonASTRequest(
        taskId="test-task-1",
        sessionId="test-session-1",
        files=[PythonFileItem(path="app/api/users.py", content=source)],
    )

    res = analyze_python_ast(req)
    assert res.status == "SUCCESS"
    assert res.taskId == "test-task-1"
    assert len(res.routes) == 2

    # Verify routes
    get_route = next(r for r in res.routes if r.method == "GET")
    assert get_route.path == "/users"
    assert get_route.inferredHandler == "list_users"
    assert get_route.framework == "fastapi"
    assert not get_route.authRequired

    post_route = next(r for r in res.routes if r.method == "POST")
    assert post_route.path == "/users"
    assert post_route.inferredHandler == "create_user"
    assert post_route.authRequired is True

    # Verify models
    assert len(res.models) == 1
    assert res.models[0].name == "UserDTO"
    assert res.models[0].framework == "pydantic"
    assert len(res.models[0].fields) == 3

    # Verify environment references
    env_names = {e.name for e in res.envReferences}
    assert "DATABASE_URL" in env_names
    assert "API_KEY" in env_names

    # Verify evidence
    evidence_details = [ev.detail for ev in res.evidence]
    assert any("fastapi" in d for d in evidence_details)
    assert any("pydantic" in d for d in evidence_details)


def test_flask_and_sqlalchemy_extraction() -> None:
    source = """
from flask import Flask, request
from sqlalchemy import Column, Integer, String
from sqlalchemy.ext.declarative import declarative_base

app = Flask(__name__)
Base = declarative_base()

class Account(Base):
    id = Column(Integer, primary_key=True)
    username = Column(String(50))

@app.route("/health", methods=["GET", "HEAD"])
def health():
    return {"status": "ok"}
"""
    req = PythonASTRequest(
        taskId="test-task-2",
        sessionId="test-session-2",
        files=[PythonFileItem(path="server.py", content=source)],
    )

    res = analyze_python_ast(req)
    assert res.status == "SUCCESS"

    # Route
    assert len(res.routes) == 2  # GET and HEAD
    assert res.routes[0].path == "/health"
    assert res.routes[0].framework == "flask"

    # Model
    assert len(res.models) == 1
    assert res.models[0].name == "Account"
    assert res.models[0].framework == "sqlalchemy"
    assert len(res.models[0].fields) >= 2


def test_security_invariant_never_executes_code() -> None:
    hostile_source = """
# This code is intentionally hostile and must NEVER be executed
raise RuntimeError("EXECUTION DETECTED - CODE WAS RUN RATHER THAN PARSED")

import subprocess
subprocess.run(["echo", "exploit"])
"""
    req = PythonASTRequest(
        taskId="test-task-sec",
        sessionId="test-session-sec",
        files=[PythonFileItem(path="malicious.py", content=hostile_source)],
    )

    # analyze_python_ast MUST parse without raising RuntimeError
    res = analyze_python_ast(req)
    assert res.status == "SUCCESS"
    assert res.taskId == "test-task-sec"


def test_syntax_error_resilience() -> None:
    bad_source = "def unclosed_func(x:"
    good_source = "def valid_func(): pass"

    req = PythonASTRequest(
        taskId="test-task-syn",
        sessionId="test-session-syn",
        files=[
            PythonFileItem(path="bad.py", content=bad_source),
            PythonFileItem(path="good.py", content=good_source),
        ],
    )

    res = analyze_python_ast(req)
    assert res.status == "SUCCESS"
    assert len(res.diagnostics) == 1
    assert res.diagnostics[0].code == "PARSE_SYNTAX_ERROR"
    assert res.diagnostics[0].sourceFile == "bad.py"


def test_payload_limits() -> None:
    # Too many files
    too_many = [
        PythonFileItem(path=f"file_{i}.py", content="x = 1") for i in range(25)
    ]
    req = PythonASTRequest(
        taskId="test-task-lim",
        sessionId="test-session-lim",
        files=too_many,
    )
    res = analyze_python_ast(req)
    assert res.status == "ERROR"
    assert res.error is not None
    assert res.error.code == "PAYLOAD_TOO_LARGE"
