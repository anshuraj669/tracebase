from pathlib import Path

from tracebase_cli import merge, reuse

OLD_USER = """<?php

namespace App\\Models;

use Illuminate\\Foundation\\Auth\\User as Authenticatable;
use Illuminate\\Database\\Eloquent\\Relations\\BelongsToMany;
use Tymon\\JWTAuth\\Contracts\\JWTSubject;

class User extends Authenticatable implements JWTSubject
{
    public function roles(): BelongsToMany
    {
        return $this->belongsToMany(Role::class);
    }

    /**
     * True if the user has any of the given roles.
     */
    public function hasRole(string ...$roles): bool
    {
        return $this->roles->pluck('name')->intersect($roles)->isNotEmpty();
    }

    public function getJWTIdentifier()
    {
        return $this->getKey();
    }
}
"""

NEW_USER = """<?php

namespace App\\Models;

use Illuminate\\Foundation\\Auth\\User as Authenticatable;
use Illuminate\\Notifications\\Notifiable;

class User extends Authenticatable
{
    use Notifiable;

    protected $casts = ['password' => 'hashed'];

    public function bookings()
    {
        return $this->hasMany(Booking::class);
    }
}
"""


def test_finds_php_methods_with_docblocks():
    fns = merge.functions(OLD_USER, "php")
    assert list(fns) == ["roles", "hasRole", "getJWTIdentifier"]
    s, e = fns["hasRole"]
    assert OLD_USER[s:e].lstrip().startswith("/**") and OLD_USER[s:e].rstrip().endswith("}")


def test_adds_only_needed_methods_and_their_imports():
    want = reuse.needed_methods(OLD_USER, "php", "if (!$user->hasRole(...$roles)) {}")
    assert want == ["hasRole", "roles"]                      # roles() because hasRole uses $this->roles
    out, added = merge.add_functions(NEW_USER, OLD_USER, "php", only=want)
    assert added == ["roles", "hasRole"]
    assert "use Notifiable;" in out and "protected $casts" in out and "function bookings()" in out
    assert "use Illuminate\\Database\\Eloquent\\Relations\\BelongsToMany;" in out
    assert "JWTSubject" not in out and "getJWTIdentifier" not in out   # unrelated old code stays behind
    assert out.rstrip().endswith("}") and out.count("class User") == 1
    assert merge.add_functions(out, OLD_USER, "php", only=want) == (out, [])   # idempotent


def test_typescript_and_python_functions():
    ts = "import x from 'y';\n\n/** doc */\nexport async function a(n: number) {\n  if (n) { return '}'; }\n}\n\nexport const b = (s: string) => {\n  return s;\n};\n"
    assert list(merge.functions(ts, "typescript")) == ["a", "b"]
    py = "import os\n\n\n@decorator\ndef a(x):\n    if x:\n        return 1\n\n    return 2\n\n\nclass B:\n    def m(self):\n        pass\n"
    fns = merge.functions(py, "python")
    assert py[fns["a"][0]:fns["a"][1]].startswith("@decorator") and "return 2" in py[fns["a"][0]:fns["a"][1]]
    assert "B" in fns and "m" in fns


def test_insert_wiring_lines():
    kernel = "    protected $aliases = [\n        'auth' => Auth::class,\n        'throttle' => T::class,\n    ];\n"
    sn = [{"line": "        'role' => Role::class,", "kind": "entry", "before": ["        'auth' => Auth::class,"]},
          {"line": "Route::get('x', [A::class, 'x']);", "kind": "nested", "before": [], "inside": "Route::group("}]
    out, added, manual = merge.insert_lines(kernel, sn)
    assert out.splitlines()[2].strip() == "'role' => Role::class," and len(manual) == 1
    routes = "<?php\n\nuse A\\B;\nuse Illuminate\\Support\\Facades\\Route;\n\nRoute::get('b', [B::class, 'i']);\n"
    out, added, _ = merge.insert_lines(routes, [
        {"line": "use App\\Http\\Controllers\\AuthController;", "kind": "import", "before": []},
        {"line": "Route::post('auth/login', [AuthController::class, 'login']);", "kind": "statement",
         "before": ["use Illuminate\\Support\\Facades\\Route;"]}])
    lines = out.splitlines()
    assert lines.index("use App\\Http\\Controllers\\AuthController;") < lines.index("Route::get('b', [B::class, 'i']);")
    assert lines[-1] == "Route::post('auth/login', [AuthController::class, 'login']);"
    assert merge.insert_lines(out, [{"line": lines[-1], "kind": "statement", "before": []}])[1] == []  # already there


def test_plan_and_apply_changes(tmp_path: Path):
    (tmp_path / "app/Models").mkdir(parents=True)
    (tmp_path / "app/Models/User.php").write_text(NEW_USER)
    bundle = {"repo": "old/app", "packages": [{"name": "tymon/jwt-auth", "version": "^2", "install": "composer require tymon/jwt-auth"}],
              "env": ["JWT_SECRET"], "files": [
        {"path": "app/Http/Middleware/Role.php", "target": "app/Http/Middleware/Role.php", "role": "picked",
         "reason": "picked", "content": "<?php\nclass Role { function handle($r) { return $r->user()->hasRole('a'); } }\n"},
        {"path": "app/Models/User.php", "target": "app/Models/User.php", "role": "dependency",
         "reason": "defines hasRole()", "content": OLD_USER},
    ]}
    changes = {c.target: c for c in reuse.plan_changes(tmp_path, bundle)}
    assert changes["app/Http/Middleware/Role.php"].kind == "create"
    assert changes["app/Models/User.php"].kind == "merge" and changes["app/Models/User.php"].added == ["roles", "hasRole"]
    backup = tmp_path / ".tracebase/backups/t"
    for c in changes.values():
        reuse.apply(tmp_path, c, backup)
    assert (backup / "app/Models/User.php").read_text() == NEW_USER
    assert "function hasRole" in (tmp_path / "app/Models/User.php").read_text()
    written = "\n".join(c.new_text for c in changes.values())
    assert reuse.packages_needed(bundle, written) == []        # the copied code doesn't use tymon
    assert reuse.packages_needed(bundle, written + "use Tymon\\JWTAuth\\JWT;") != []


GO_MAIN = """package main

import (
\t"net/http"

\t"github.com/gin-gonic/gin"
)

func main() {
\tr := gin.Default()
\tr.Run(":8080")
}
"""


def test_go_imports_go_into_the_import_block():
    out = merge._insert_imports(GO_MAIN, ['import "github.com/me/app/internal/auth"'], "go")
    assert '\t"github.com/gin-gonic/gin"\n\t"github.com/me/app/internal/auth"\n)' in out
    single = merge._insert_imports('package main\n\nimport "fmt"\n\nfunc main() {}\n', ['import "os"'], "go")
    assert 'import (\n\t"fmt"\n\t"os"\n)' in single
    assert merge._insert_imports(GO_MAIN, ['import "net/http"'], "go") == GO_MAIN  # already there


def test_go_functions_and_methods():
    src = "package store\n\nfunc Find(e string) *User {\n\treturn nil\n}\n\nfunc (u *User) Check(p string) bool {\n\treturn true\n}\n"
    assert set(merge.functions(src, "go")) == {"Find", "Check"}
    out, added = merge.add_functions("package store\n\nfunc Find(e string) *User {\n\treturn nil\n}\n", src, "go")
    assert added == ["Check"] and out.rstrip().endswith("}")


def test_java_methods_merge_into_the_class_with_imports():
    have = "package a;\n\nimport java.util.List;\n\npublic class Svc {\n    public int one() {\n        return 1;\n    }\n}\n"
    incoming = ("package a;\n\nimport java.util.Map;\n\npublic class Svc {\n    /** Two. */\n    public Map<String, Integer> two() {\n"
                "        if (true) { return Map.of(); }\n        return null;\n    }\n}\n")
    out, added = merge.add_functions(have, incoming, "java")
    assert added == ["two"]
    assert "import java.util.List;\nimport java.util.Map;" in out
    assert out.index("public Map<String, Integer> two()") < out.rindex("}") and out.count("class Svc") == 1


def test_ruby_methods_and_rails_macros():
    have = "class User < ApplicationRecord\n  has_many :bookings\nend\n"
    incoming = ("class User < ApplicationRecord\n  has_secure_password\n  has_many :roles\n\n"
                "  def has_role?(*names)\n    roles.where(name: names).exists?\n  end\nend\n")
    assert set(merge.functions(incoming, "ruby")) == {"has_role?"}
    out, added = merge.add_functions(have, incoming, "ruby")
    assert added == ["has_role?"] and out.endswith("  end\nend\n")
    out2, macros = merge.add_ruby_macros(out, incoming, "user&.authenticate(pw)\n" + out)
    assert macros == ["has_secure_password", "has_many :roles"]
    assert out2.startswith("class User < ApplicationRecord\n  has_secure_password\n  has_many :roles\n")


def test_nested_line_goes_after_the_nearest_neighbour_that_exists():
    sn = {"line": '\tadmin := r.Group("/admin")', "kind": "nested", "inside": "func main() {",
          "before": ['\tr.POST("/login", handlers.Login)', "\tr := gin.Default()"]}
    out, added, manual = merge.insert_lines(GO_MAIN, [sn])
    assert '\tr := gin.Default()\n\tadmin := r.Group("/admin")\n' in out and not manual
    out, added, manual = merge.insert_lines("package main\n", [sn])
    assert manual == [sn] and not added
