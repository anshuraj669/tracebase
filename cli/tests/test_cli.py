from pathlib import Path

import pytest

from tracebase_cli import main, project, reuse


def _laravel(tmp: Path) -> Path:
    files = {
        "composer.json": '{"require": {"laravel/framework": "^10.0"}}',
        "app/Models/User.php": "<?php class User {}",
        "app/Models/Booking.php": "<?php class Booking {}",
        "app/Http/Kernel.php": "<?php class Kernel {}",
        "routes/api.php": "<?php Route::get('b', [BookingController::class, 'index']);",
        "app/Http/Controllers/BookingController.php": "<?php class BookingController { function index() {} }",
        "resources/css/app.css": "body{}",
        "vendor/laravel/framework/x.php": "<?php",
        "node_modules/a/index.js": "x",
        "storage/logs/laravel.log": "log",
        ".env": "APP_KEY=secret",
        "public/logo.png": "png",
        "ignored/cache.php": "<?php",
        ".gitignore": "/ignored\n",
    }
    for rel, text in files.items():
        p = tmp / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    return tmp


def test_tree_skips_vendor_env_binaries_and_gitignored(tmp_path):
    tree = project.scan_tree(_laravel(tmp_path))
    assert "composer.json" in tree and "routes/api.php" in tree
    assert not any(p.startswith(("vendor/", "node_modules/", "storage/", "ignored/")) for p in tree)
    assert ".env" not in tree and "public/logo.png" not in tree


def test_context_picks_relevant_files(tmp_path):
    ctx = project.context(_laravel(tmp_path), "add role based access control")
    assert {"composer.json", "routes/api.php", "app/Http/Kernel.php", "app/Models/User.php"} <= set(ctx["files"])
    assert "resources/css/app.css" not in ctx["files"]
    assert "app/Http/Controllers/BookingController.php" in ctx["files"]   # referenced by routes/api.php
    assert ctx["name"] == tmp_path.name


def test_safe_target_refuses_outside(tmp_path):
    assert reuse.safe_target(tmp_path, "app/x.php") == (tmp_path / "app/x.php").resolve()
    with pytest.raises(reuse.CopyError):
        reuse.safe_target(tmp_path, "../x.php")


def test_candidates_one_per_file():
    ex = [{"repo": "a", "path": "x.php"}, {"repo": "a", "path": "x.php"}, {"repo": "b/c", "path": "y.php"}]
    assert [e["path"] for e in main.candidates(ex)] == ["x.php", "y.php"]
    assert [e["repo"] for e in main.candidates(ex, repo="c")] == ["b/c"]
