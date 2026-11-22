from tracebase.code.chunker import chunk_file, detect_language

PHP = """<?php
class CartService
{
    public function subtotal(array $cart): float
    {
        return 1.0;
    }

    /**
     * Apply a coupon.
     */
    public function applyDiscount(array $cart)
    {
        $code = $cart['coupon'];
        return $code;
    }
}
"""


def test_detect_language():
    assert detect_language("app/Services/CartService.php") == "php"
    assert detect_language("src/a.ts") == "typescript"
    assert detect_language("Dockerfile") == "dockerfile"
    assert detect_language(".env.backup") == "dotenv"
    assert detect_language("image.png") is None


def test_php_small_class_is_one_chunk():
    chunks = chunk_file(PHP, "php")
    cls = [c for c in chunks if c.kind == "class"]
    assert len(cls) == 1 and cls[0].symbol == "CartService"
    assert cls[0].start_line == 2 and cls[0].end_line == 17


def test_large_class_is_split_into_methods():
    big = PHP.replace("return 1.0;", "\n".join(["        $x = 1;"] * 80) + "\n        return 1.0;")
    chunks = chunk_file(big, "php")
    names = {c.symbol for c in chunks}
    assert "CartService::subtotal" in names
    assert "CartService::applyDiscount" in names
    m = next(c for c in chunks if c.symbol == "CartService::applyDiscount")
    assert "Apply a coupon" in m.content  # docblock attached to the method
    assert not any(s.endswith("::class") for s in names)  # keyword tokens are not symbols


def test_python_functions():
    src = "import os\n\n\ndef a():\n    return 1\n\n\nclass B:\n    def c(self):\n        return 2\n"
    names = {c.symbol for c in chunk_file(src, "python")}
    assert {"a", "B"} <= names
    assert "module" not in names


def test_fallback_for_unknown_language():
    chunks = chunk_file("line\n" * 300, None)
    assert len(chunks) >= 3 and all(c.end_line - c.start_line < 120 for c in chunks)
