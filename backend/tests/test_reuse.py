"""Reuse: find the developer's past implementation and bundle everything it needs. No LLM involved."""
from pathlib import Path

from tracebase.config import settings
from tracebase.reuse.bundle import bundle, snippets
from tracebase.reuse.retrieve import clean_task, find_examples
from tracebase.reuse.stack import detect, repo_stack

LARAVEL = {"languages": ["php"], "primary": "laravel"}


def _workspace_ctx() -> dict:
    root = settings.repos_dir.parent / "workspace" / "booking-api"
    tree = sorted(p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file() and ".git" not in p.parts)
    return {"name": "booking-api", "tree": tree, "files": {p: (root / p).read_text() for p in tree}}


def test_clean_task_keeps_only_what_to_search_for():
    assert clean_task("search my projects and copy the login logic and write it here") == "login"
    assert clean_task("search my project and implement the role based access control in the project") == \
        "role based access control"
    assert clean_task("copy") == "copy"   # never empty


def test_detects_stacks(indexed):
    ctx = _workspace_ctx()
    assert detect(ctx["tree"], lambda p: ctx["files"].get(p))["primary"] == "laravel"
    assert repo_stack("freelance/clinic-app")["primary"] == "laravel"
    assert repo_stack("side/expense-tracker")["primary"] == "express"


def test_rbac_prefers_same_framework(indexed):
    laravel = find_examples("add role based access control", LARAVEL)["examples"]
    assert laravel[0]["repo"] == "freelance/clinic-app"
    assert "EnsureUserHasRole" in [e["symbol"] for e in laravel[:3]]
    express = find_examples("add role based access control", {"languages": ["typescript"], "primary": "express"})["examples"]
    assert express[0]["repo"] == "side/expense-tracker"


def test_login_finds_the_login_function(indexed):
    ex = find_examples("search my projects and copy the login logic and write it here", LARAVEL)["examples"]
    assert ex[0]["symbol"] == "AuthController::login"


def test_exclude_current_project(indexed):
    ex = find_examples("role middleware", {}, exclude_repo="clinic-app")["examples"]
    assert all(e["repo"] != "freelance/clinic-app" for e in ex)


def test_rbac_bundle_follows_dependencies_and_wiring(indexed):
    ctx = _workspace_ctx()
    b = bundle("freelance/clinic-app", ["app/Http/Middleware/EnsureUserHasRole.php"], ctx)
    files = {f["path"]: f for f in b["files"]}
    assert files["app/Http/Middleware/EnsureUserHasRole.php"]["role"] == "picked"
    assert "hasRole()" in files["app/Models/User.php"]["reason"]           # method it calls
    assert files["app/Models/User.php"]["exists_in_project"]
    assert "app/Models/Role.php" in files and "app/Models/Permission.php" in files   # classes, transitively
    assert any("migrations" in p and "roles" in p for p in files)           # table for the new model
    kernel = files["app/Http/Kernel.php"]
    assert kernel["role"] == "wiring" and kernel["snippets"][0]["kind"] == "entry"
    assert "'role'" in kernel["snippets"][0]["line"]
    routes = files["routes/api.php"]                                         # uses the 'role' alias
    assert routes["role"] == "wiring" and all(s["kind"] == "nested" for s in routes["snippets"])


def test_login_bundle(indexed):
    b = bundle("freelance/clinic-app", ["app/Http/Controllers/AuthController.php"], _workspace_ctx())
    routes = next(f for f in b["files"] if f["path"] == "routes/api.php")
    kinds = {s["line"].strip(): s["kind"] for s in routes["snippets"]}
    assert kinds["Route::post('auth/login', [AuthController::class, 'login']);"] == "statement"
    assert kinds["Route::get('auth/me', [AuthController::class, 'me']);"] == "nested"   # was inside auth:api group
    assert kinds["use App\\Http\\Controllers\\AuthController;"] == "import"
    assert [p["name"] for p in b["packages"]] == ["tymon/jwt-auth"]


def test_express_bundle_imports_packages_and_env(indexed):
    b = bundle("side/expense-tracker", ["src/auth/tokens.ts"], {"tree": [], "files": {"package.json": '{"dependencies": {}}'}})
    assert {f["path"] for f in b["files"]} == {"src/auth/tokens.ts", "src/lib/redis.ts"}
    assert {p["name"] for p in b["packages"]} == {"ioredis", "jsonwebtoken"}
    assert b["env"] == ["JWT_SECRET", "REDIS_URL"]


def test_unknown_repo_or_file(indexed):
    assert "error" in bundle("nope", ["x.php"])
    assert "error" in bundle("freelance/clinic-app", ["missing.php"])


def test_snippet_kinds():
    text = "<?php\nuse A\\B;\n\nRoute::get('a', [B::class, 'x']);\nRoute::group(function () {\n    Route::get('b', [B::class, 'y']);\n});\n$x = [\n    'k' => B::class,\n];\n"
    kinds = [s["kind"] for s in snippets(text, ["B"])]
    assert kinds == ["import", "statement", "nested", "entry"]


def test_symlinked_repo_is_readable_but_traversal_is_not(tmp_path, monkeypatch):
    from tracebase.code.sources import get_source
    real = tmp_path / "elsewhere" / "proj"
    real.mkdir(parents=True)
    (real / "a.py").write_text("x = 1\n")
    repos = tmp_path / "repos"
    repos.mkdir()
    (repos / "proj").symlink_to(real)
    monkeypatch.setattr(settings, "repos_dir", repos)
    assert get_source("proj").read("a.py") == "x = 1\n"
    assert get_source("../elsewhere/proj") is None
    assert get_source("proj").read("../../elsewhere/proj/a.py") is None


# ---- found while testing on real projects ------------------------------------------------------------
from tracebase.reuse.blocks import keep_only  # noqa: E402
from tracebase.reuse.bundle import _names, keep_only_patterns  # noqa: E402

ROUTER = '''import httpx
from fastapi import APIRouter, Depends
from fastapi.responses import RedirectResponse
from models.schemas import (
    LoginRequest, RegisterRequest, AuthResponse
)
from services.auth_service import AuthService, get_current_user

router = APIRouter(prefix="/auth")


@router.post("/register")
async def register(r: RegisterRequest):
    return AuthService.register(r)


@router.post("/login", response_model=AuthResponse)
async def login(r: LoginRequest, db=Depends(lambda: None)):
    return AuthService.login(db, r.email, r.password)


@router.get("/google")
async def google():
    async with httpx.AsyncClient() as c:
        return RedirectResponse("x")
'''


def test_consumer_keeps_only_the_endpoint_that_calls_the_symbol():
    out = keep_only_patterns(ROUTER, "python", [r"\bAuthService(\.|::|->)login\b"])
    assert "async def login" in out and "def register" not in out and "def google" not in out
    assert "router = APIRouter" in out                                   # used by the decorator
    assert "import httpx" not in out and "RedirectResponse" not in out   # unused imports pruned
    assert "from models.schemas import LoginRequest, AuthResponse" in out
    assert "from services.auth_service import AuthService\n" in out      # get_current_user dropped


def test_partial_copy_keeps_what_the_kept_names_use():
    src = "import os\n\nBASE = 2\n\n\ndef used():\n    return BASE\n\n\ndef helper_a():\n    return used()\n\n\ndef unrelated():\n    return os.getcwd()\n"
    out = keep_only(src, "python", {"helper_a"})
    assert "def helper_a" in out and "def used" in out and "BASE = 2" in out
    assert "def unrelated" not in out and "import os" not in out


def test_typescript_partial_copy():
    src = "import { a, b } from './x';\n\nexport function keep() {\n  return a();\n}\n\nexport const drop = () => {\n  return b();\n};\n"
    out = keep_only(src, "typescript", {"keep"})
    assert "function keep" in out and "drop" not in out and "import { a } from './x';" in out


def test_import_name_parsing():
    assert _names("a, b as c") == {"a", "b"}
    assert _names("(\n  X,\n  Y\n)") == {"X", "Y"}
    assert _names("*") is None


def test_renamer_keeps_spelling_style_and_word_boundaries():
    from tracebase.reuse.adapt import Renamer
    r = Renamer([("clinic-app", "booking-api"), ("Doctor", "Staff")])
    out = r("namespace ClinicApp; 'clinic-app' CLINIC_APP_KEY clinic_app.db DoctorController isDoctor "
            "$this->doctors() DOCTOR_ROLE Doctorate doctorly")
    assert out == ("namespace BookingApi; 'booking-api' BOOKING_API_KEY booking_api.db StaffController isStaff "
                   "$this->staffs() STAFF_ROLE Doctorate doctorly")
    assert r.counts == {"clinic-app → booking-api": 4, "Doctor → Staff": 4}


def test_adapt_go_module_java_package_and_file_names():
    from tracebase.reuse.adapt import adapt
    files = [
        {"path": "internal/auth/jwt.go", "target": "internal/auth/jwt.go",
         "content": 'package auth\nimport "github.com/me/clinic/internal/store"\n'},
        {"path": "src/main/java/com/acme/clinic/auth/DoctorGuard.java",
         "target": "src/main/java/com/acme/clinic/auth/DoctorGuard.java",
         "content": "package com.acme.clinic.auth;\nimport com.acme.clinic.model.Role;\nclass DoctorGuard {}\n"},
    ]
    old = {"name": "clinic", "tree": ["src/main/java/com/acme/clinic/App.java", "src/main/java/com/acme/clinic/auth/X.java"],
           "files": {"go.mod": "module github.com/me/clinic\n"}}
    new = {"name": "booking", "tree": ["src/main/java/io/shop/booking/App.java", "src/main/java/io/shop/booking/web/Y.java"],
           "files": {"go.mod": "module github.com/me/booking\n"}}
    summary = adapt(files, old, new, [("Doctor", "Staff")])
    assert '"github.com/me/booking/internal/store"' in files[0]["content"]
    java = files[1]
    assert "package io.shop.booking.auth;" in java["content"] and "import io.shop.booking.model.Role;" in java["content"]
    assert "class StaffGuard" in java["content"]
    assert java["target"] == "src/main/java/io/shop/booking/auth/StaffGuard.java"
    assert any(s.startswith("Go module") for s in summary) and any(s.startswith("Java package") for s in summary)


def test_bundle_reports_adaptation(indexed):
    res = bundle("freelance/clinic-app", ["app/Http/Middleware/EnsureUserHasRole.php"], _workspace_ctx(),
                      renames=[("doctor", "staff")])
    assert any(a.startswith("doctor → staff") for a in res["adapted"])
    assert "role:staff" in "\n".join(f["content"] for f in res["files"])


def _ctx(name: str) -> dict:
    """Context of a demo workspace project, as the CLI sends it."""
    root = settings.repos_dir.parent / "workspace" / name
    tree = sorted(str(p.relative_to(root)) for p in root.rglob("*") if p.is_file() and ".git/" not in str(p.relative_to(root)) + "/")
    tree = [t for t in tree if not t.startswith(".git/")]
    files = {t: (root / t).read_text() for t in tree if "/" not in t}
    return {"name": name, "tree": tree, "files": files}


def test_go_bundle_follows_packages_siblings_wiring_and_module(indexed):
    res = bundle("side/go-auth-service", ["internal/middleware/rbac.go"], _ctx("booking-go"))
    paths = [f["path"] for f in res["files"]]
    assert "internal/auth/jwt.go" in paths          # imported package
    assert "internal/middleware/roles.go" in paths  # same package helper hasAny
    main = next(f for f in res["files"] if f["path"] == "main.go")
    assert main["role"] == "wiring"
    lines = [s["line"].strip() for s in main["snippets"]]
    assert 'import "github.com/acme/booking-go/internal/middleware"' in lines
    assert any("middleware.RequireRole" in l for l in lines)
    jwt = next(f for f in res["files"] if f["path"] == "internal/auth/jwt.go")
    assert "github.com/ankit" not in jwt["content"]
    assert [p["name"] for p in res["packages"]] == ["github.com/golang-jwt/jwt/v5"]
    assert res["env"] == ["JWT_SECRET"]
    assert any(a.startswith("Go module") for a in res["adapted"])


def test_java_bundle_follows_imports_and_moves_package(indexed):
    res = bundle("freelance/spring-clinic", ["src/main/java/com/acme/clinic/web/AuthController.java"],
                 _ctx("booking-java"), renames=[("Doctor", "Staff")])
    targets = {f["path"]: f["target"] for f in res["files"]}
    base = "src/main/java/io/acme/booking/"
    assert targets["src/main/java/com/acme/clinic/security/JwtService.java"] == base + "security/JwtService.java"
    assert targets["src/main/java/com/acme/clinic/model/Doctor.java"] == base + "model/Staff.java"
    ctrl = next(f for f in res["files"] if f["path"].endswith("AuthController.java"))
    assert "package io.acme.booking.web;" in ctrl["content"] and "import io.acme.booking.model.Staff;" in ctrl["content"]
    assert "StaffRepository staffs" in ctrl["content"]
    assert [p["name"] for p in res["packages"]] == ["io.jsonwebtoken:jjwt-api"]
    assert res["env"] == ["JWT_SECRET"]


def test_ruby_bundle_follows_constants_methods_migrations_and_routes(indexed):
    res = bundle("side/rails-shop", ["app/controllers/sessions_controller.rb"], _ctx("booking-rails"))
    paths = [f["path"] for f in res["files"]]
    assert {"lib/json_web_token.rb", "app/models/user.rb"} <= set(paths)
    routes = next(f for f in res["files"] if f["path"] == "config/routes.rb")
    assert routes["role"] == "wiring" and any("sessions#create" in s["line"] for s in routes["snippets"])
    assert {p["name"] for p in res["packages"]} == {"jwt", "bcrypt"}
    assert res["env"] == ["JWT_SECRET"]
    rbac = bundle("side/rails-shop", ["app/controllers/concerns/authorizable.rb"], _ctx("booking-rails"))
    rpaths = [f["path"] for f in rbac["files"]]
    assert "app/models/user.rb" in rpaths and "db/migrate/20240105000000_create_roles.rb" in rpaths
