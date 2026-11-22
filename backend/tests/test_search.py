from tracebase.code import search
from tracebase.code.sources import discover
from tracebase.config import settings


def test_discovers_folders_git_and_zip(indexed):
    repos = set(discover(settings.repos_dir))
    assert {"shop-api", "freelance/clinic-app", "side/expense-tracker", "invoice-bot", "archive/old-blog.zip"} <= repos


def _top(q, n=5):
    return [(r["repo"], r["symbol"]) for r in search.search(q, limit=n)["results"]]


def test_semantic_jwt_refresh(indexed):
    top = _top("Where did I implement JWT refresh tokens before?")
    repos = {r for r, _ in top}
    assert {"freelance/clinic-app", "side/expense-tracker", "archive/old-blog.zip"} <= repos


def test_natural_language_without_shared_words(indexed):
    # The code never says "resize uploaded images" in one place; synonyms + identifiers bridge the gap.
    assert _top("function that resizes uploaded images", 1)[0] == ("side/expense-tracker", "makeThumbnails")


def test_password_reset_email(indexed):
    top = _top("send password reset email", 2)
    assert ("side/expense-tracker", "sendResetEmail") in top
    assert ("freelance/clinic-app", "ResetPassword") in top


def test_repo_filter(indexed):
    res = search.search("stripe webhook", repo="shop-api")["results"]
    assert res and all(r["repo"] == "shop-api" for r in res)


def test_exact_search_line_accurate(indexed):
    hits = search.exact_search("sendMail(")["results"]
    assert hits[0]["path"] == "src/mail/reset.ts" and hits[0]["line"] == 7


def test_read_code_inside_zip(indexed):
    res = search.read_code("archive/old-blog.zip", "lib/session.php", 9, 14)
    assert any("refresh_session" in l["text"] for l in res["lines"])


def test_secrets_found_during_indexing(indexed):
    from tracebase.db import rows

    found = {(r["repo"], r["path"]) for r in rows("SELECT repo, path FROM secrets")}
    assert ("side/expense-tracker", ".env.backup") in found
    assert ("archive/old-blog.zip", "config/db.php") in found
    assert ("freelance/clinic-app", "docker-compose.yml") in found


def test_incremental_reindex_skips_unchanged(indexed):
    from tracebase.code.indexer import index_all

    again = index_all()
    assert all(not s.get("indexed") for s in again["repos"].values())


def test_git_blame(indexed):
    from tracebase.code.git import history

    h = history("shop-api", path="app/Services/CartService.php", line=57)
    assert "BLACKFRIDAY" in h["commits"][0]["message"]
