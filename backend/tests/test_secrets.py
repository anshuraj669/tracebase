from tracebase.code.secrets import scan_text


def test_detects_stripe_key_and_masks_it():
    key = "sk_live_" + "A" * 24
    f = scan_text(".env.backup", f"STRIPE_SECRET_KEY={key}\n")
    assert f[0]["severity"] == "HIGH" and "Stripe" in f[0]["rule"]
    assert key not in f[0]["preview"] and "****" in f[0]["preview"]


def test_hardcoded_db_password():
    f = scan_text("config/db.php", "$db_password = 'hunter2hunter';\n")
    assert f and f[0]["rule"] == "Hard-coded DB password"


def test_ignores_env_references_and_placeholders():
    text = "DB_PASSWORD=${DB_PASSWORD}\nAPI_KEY=changeme\nSECRET=your_secret_here\n"
    assert scan_text("docker-compose.yml", text) == []


def test_compose_password_is_medium():
    f = scan_text("docker-compose.yml", "      MYSQL_ROOT_PASSWORD: S3cretRoot!\n")
    assert f and f[0]["severity"] == "MEDIUM"
