#!/usr/bin/env python3
"""Create a demo NAS layout under demo/volume1 reproducing the Tracebase scenarios: Laravel/Node/Python/Go/Java/
Rails repos, a git repo with history, an old .zip archive, a few (fake) leaked secrets for the secrets scan,
target projects to copy code into, and production logs with two incidents for Log Detective.

    python demo/seed.py            # creates demo/volume1/repos, demo/volume1/workspace, demo/volume1/logs
    python demo/seed.py --reset    # wipe and recreate
"""
from __future__ import annotations

import os
import random
import shutil
import subprocess
import sys
import textwrap
import zipfile
from datetime import datetime, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
V = HERE / "volume1"
REPOS = V / "repos"
LOGS = V / "logs"
random.seed(42)


def w(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(content).lstrip("\n"))


def git(cwd: Path, *args: str, date: str | None = None) -> None:
    env = {**os.environ, "GIT_AUTHOR_NAME": "Ankit", "GIT_AUTHOR_EMAIL": "dev@example.com",
           "GIT_COMMITTER_NAME": "Ankit", "GIT_COMMITTER_EMAIL": "dev@example.com"}
    if date:
        env["GIT_AUTHOR_DATE"] = env["GIT_COMMITTER_DATE"] = date
    subprocess.run(["git", *args], cwd=cwd, env=env, check=True, capture_output=True)


# ---------------------------------------------------------------- shop-api (Laravel, git history)
def shop_api() -> None:
    r = REPOS / "shop-api"
    w(r / "composer.json", '{"name": "acme/shop-api", "require": {"php": "^8.2", "laravel/framework": "^11.0", "stripe/stripe-php": "^13.0"}}\n')
    w(r / ".gitignore", "/vendor\n/node_modules\n.env\nstorage/*.key\n")
    w(r / "artisan", "#!/usr/bin/env php\n<?php\n// Laravel artisan\n")
    w(r / "app/Services/CartService.php", """
        <?php

        namespace App\\Services;

        use App\\Models\\Cart;
        use App\\Models\\Coupon;
        use Illuminate\\Support\\Facades\\Log;

        /**
         * Cart totals, discounts and tax.
         */
        class CartService
        {
            public function __construct(private TaxCalculator $tax)
            {
            }

            /**
             * Sum line items (price x quantity).
             */
            public function subtotal(array $cart): float
            {
                $sum = 0.0;
                foreach ($cart['items'] as $item) {
                    $sum += $item['price'] * $item['qty'];
                }
                return round($sum, 2);
            }

            public function addItem(Cart $cart, int $productId, int $qty = 1): Cart
            {
                $line = $cart->items()->firstOrNew(['product_id' => $productId]);
                $line->qty = ($line->qty ?? 0) + $qty;
                $line->save();
                return $cart->refresh();
            }

            public function removeItem(Cart $cart, int $productId): Cart
            {
                $cart->items()->where('product_id', $productId)->delete();
                return $cart->refresh();
            }

            public function clear(Cart $cart): Cart
            {
                $cart->items()->delete();
                $cart->save();
                return $cart->refresh();
            }

            /**
             * Apply a coupon / promo code and return the discounted total.
             */
            public function applyDiscount(array $cart)
            {
                $total = $cart['total'];
                $code  = $cart['coupon'];
                if (!$code) {
                    return $total;
                }
                $coupon = Coupon::where('code', $code)->where('active', true)->first();
                if (!$coupon) {
                    Log::warning("Unknown coupon {$code}");
                    return $total;
                }
                $discount = $coupon->percent ? $total * $coupon->percent / 100 : $coupon->amount;
                return max(0, round($total - $discount, 2));
            }

            public function total(array $cart): float
            {
                $cart['total'] = $this->subtotal($cart);
                $afterDiscount = $this->applyDiscount($cart);
                return $afterDiscount + $this->tax->forAmount($afterDiscount, $cart['country'] ?? 'IN');
            }
        }
    """)
    w(r / "app/Http/Controllers/CheckoutController.php", """
        <?php

        namespace App\\Http\\Controllers;

        use App\\Services\\CartService;
        use App\\Services\\OrderService;
        use Illuminate\\Http\\Request;

        class CheckoutController extends Controller
        {
            public function __construct(private CartService $carts, private OrderService $orders)
            {
            }

            /**
             * POST /api/checkout
             *
             * Validates the cart, computes the total (discounts + tax)
             * and places the order. Returns 201 with the order id.
             * Validation errors return 422.
             */
            public function store(Request $request): \\Illuminate\\Http\\JsonResponse
            {
                $cart = $request->validate([
                    'items' => 'required|array',
                    'coupon' => 'nullable|string',
                    'country' => 'nullable|string|size:2',
                ]);
                $cart['items'] = $request->input('items');

                $total = $this->carts->total($cart);
                $order = $this->orders->place($request->user(), $cart, $total);

                return response()->json(['order_id' => $order->id, 'total' => $total], 201);
            }
        }
    """)
    # OrderService with line 142 inside place()
    body = ["<?php", "", "namespace App\\Services;", "", "use App\\Models\\Order;", "use App\\Models\\User;",
            "use Illuminate\\Support\\Facades\\DB;", "use Illuminate\\Support\\Facades\\Log;", "", "class OrderService", "{"]
    for i in range(1, 12):
        body += [f"    /** Helper {i}: formatting for order export column {i}. */",
                 f"    protected function exportColumn{i}(Order $order): string",
                 "    {", f"        return (string) ($order->meta['col{i}'] ?? '');", "    }", ""]
    while len(body) < 133:
        body.append("")
    body[132:] = [
        "    /**",
        "     * Persist an order. Called by CheckoutController::store.",
        "     */",
        "    public function place(User $user, array $cart, float $total): Order",
        "    {",
        "        Log::info('placing order', ['user' => $user->id, 'total' => $total]);",
        "        // NOTE: no retry here -- a DB blip fails the whole checkout",
        "        return DB::transaction(function () use ($user, $cart, $total) {",
        "            $order = new Order(['user_id' => $user->id, 'total' => $total, 'status' => 'pending']);",
        "            $order->save();",  # line 142
        "            foreach ($cart['items'] as $item) {",
        "                $order->items()->create($item);",
        "            }",
        "            return $order;",
        "        });",
        "    }",
        "}",
    ]
    (r / "app/Services/OrderService.php").write_text("\n".join(body) + "\n")
    w(r / "app/Http/Controllers/StripeWebhookController.php", """
        <?php

        namespace App\\Http\\Controllers;

        use Illuminate\\Http\\Request;
        use Stripe\\Webhook;
        use Stripe\\Exception\\SignatureVerificationException;

        class StripeWebhookController extends Controller
        {
            /**
             * Handle Stripe webhooks (payment_intent.succeeded, charge.refunded).
             */
            public function handle(Request $request)
            {
                $payload = $request->getContent();
                $sig = $request->header('Stripe-Signature');
                try {
                    $event = Webhook::constructEvent($payload, $sig, config('services.stripe.webhook_secret'));
                } catch (SignatureVerificationException $e) {
                    logger()->error('Stripe webhook signature mismatch', ['evt' => $request->input('id')]);
                    return response('invalid signature', 400);
                }
                match ($event->type) {
                    'payment_intent.succeeded' => $this->markPaid($event->data->object),
                    'charge.refunded' => $this->markRefunded($event->data->object),
                    default => null,
                };
                return response('ok');
            }

            private function markPaid($intent): void
            {
                \\App\\Models\\Order::where('payment_intent', $intent->id)->update(['status' => 'paid']);
            }

            private function markRefunded($charge): void
            {
                \\App\\Models\\Order::where('payment_intent', $charge->payment_intent)->update(['status' => 'refunded']);
            }
        }
    """)
    w(r / "config/services.php", """
        <?php

        return [
            'stripe' => [
                'key' => env('STRIPE_KEY'),
                'secret' => env('STRIPE_SECRET'),
                'webhook_secret' => env('STRIPE_WEBHOOK_SECRET'),
            ],
            'mail' => ['from' => env('MAIL_FROM_ADDRESS', 'shop@example.com')],
        ];
    """)
    w(r / "routes/api.php", """
        <?php

        use App\\Http\\Controllers\\CheckoutController;
        use App\\Http\\Controllers\\StripeWebhookController;
        use Illuminate\\Support\\Facades\\Route;

        Route::middleware('auth:sanctum')->post('/checkout', [CheckoutController::class, 'store']);
        Route::post('/stripe/webhook', [StripeWebhookController::class, 'handle']);
    """)
    w(r / "docker-compose.yml", """
        services:
          app:
            build: .
            environment:
              DB_HOST: db
              DB_DATABASE: shop
          db:
            image: mysql:8.4
            environment:
              MYSQL_DATABASE: shop
              MYSQL_ROOT_PASSWORD: ${DB_ROOT_PASSWORD}
    """)
    git(r, "init", "-q", "-b", "main")
    git(r, "add", "-A")
    git(r, "commit", "-q", "-m", "Initial shop API: cart, checkout, orders, Stripe webhooks", date="2026-10-20T10:00:00")
    # coupon support introduced later, with the bug
    cs = (r / "app/Services/CartService.php").read_text()
    (r / "app/Services/CartService.php").write_text(cs.replace("$code  = $cart['coupon'];", "$code  = $cart['coupon'] ?? null;"))
    git(r, "commit", "-q", "-am", "Cart: null-safe coupon lookup", date="2026-10-28T16:20:00")
    (r / "app/Services/CartService.php").write_text(cs)
    git(r, "commit", "-q", "-am", "Refactor applyDiscount for promo campaign (BLACKFRIDAY)", date="2026-11-08T13:42:00")
    # the new checkout shipped "dark" behind a Flagsmith flag: the code is weeks old, turning the flag on activates it
    w(r / "app/Services/CheckoutV2.php", """
        <?php

        namespace App\\Services;

        use App\\Models\\User;
        use Flagsmith\\Flagsmith;

        /**
         * New checkout: prices shown and charged in the shopper's currency.
         * Rolled out with the `new_checkout` feature flag.
         */
        class CheckoutV2
        {
            public function __construct(private Flagsmith $flags, private CartService $carts, private FxRates $fx)
            {
            }

            public function total(array $cart, User $user): float
            {
                $total = $this->carts->total($cart);
                $flags = $this->flags->getIdentityFlags($user->email);
                if ($flags->isFeatureEnabled('new_checkout')) {
                    $rates = $this->fx->latest();
                    $rate  = $rates[$cart['currency']];
                    return round($total * $rate, 2);
                }
                return $total;
            }
        }
    """)
    w(r / "app/Services/FxRates.php", """
        <?php

        namespace App\\Services;

        use Illuminate\\Support\\Facades\\Cache;
        use Illuminate\\Support\\Facades\\Http;

        class FxRates
        {
            public function latest(): array
            {
                return Cache::remember('fx:latest', 3600, fn () => Http::get(config('services.fx.url'))->json('rates'));
            }
        }
    """)
    git(r, "add", "-A")
    git(r, "commit", "-q", "-m", "Checkout v2: charge in the shopper's currency (behind new_checkout flag)", date="2026-11-02T11:05:00")


def clinic_app() -> None:
    r = REPOS / "freelance/clinic-app"
    w(r / "composer.json", '{"name": "freelance/clinic-app", "require": {"laravel/framework": "^10.0", "tymon/jwt-auth": "^2.0"}}\n')
    lines = ["<?php", "", "namespace App\\Http\\Controllers;", "", "use Illuminate\\Http\\Request;",
             "use Tymon\\JWTAuth\\Facades\\JWTAuth;", "use Tymon\\JWTAuth\\Exceptions\\TokenBlacklistedException;", "",
             "class AuthController extends Controller", "{"]
    lines += ["    public function login(Request $request)", "    {",
              "        $credentials = $request->only('email', 'password');",
              "        if (!$token = auth('api')->attempt($credentials)) {",
              "            return response()->json(['error' => 'Unauthorized'], 401);", "        }",
              "        return $this->respondWithToken($token);", "    }", ""]
    while len(lines) < 87:
        lines.append("    // " + random.choice(["audit trail hook", "TODO: rate limit", "patient portal auth"]))
    lines += [
        "    /**",
        "     * Refresh a JWT. Uses tymon/jwt-auth with a 14 day refresh window",
        "     * (JWT_REFRESH_TTL=20160) and blacklists the old token.",
        "     */",
        "    public function refresh()",
        "    {",
        "        try {",
        "            $newToken = JWTAuth::parseToken()->refresh(true, true); // forceForever=false, resetClaims",
        "        } catch (TokenBlacklistedException $e) {",
        "            return response()->json(['error' => 'token_blacklisted'], 401);",
        "        }",
        "        return $this->respondWithToken($newToken);",
        "    }",
        "",
        "    public function logout()",
        "    {",
        "        auth('api')->logout(); // invalidates + blacklists the token",
        "        return response()->json(['message' => 'Logged out']);",
        "    }",
        "",
        "    protected function respondWithToken(string $token)",
        "    {",
        "        return response()->json([",
        "            'access_token' => $token,",
        "            'token_type' => 'bearer',",
        "            'expires_in' => auth('api')->factory()->getTTL() * 60,",
        "            'refresh_ttl' => config('jwt.refresh_ttl'),",
        "        ]);",
        "    }",
        "",
        "    public function me()",
        "    {",
        "        return response()->json(auth('api')->user());",
        "    }",
        "}",
    ]
    (r / "app/Http/Controllers/AuthController.php").parent.mkdir(parents=True, exist_ok=True)
    (r / "app/Http/Controllers/AuthController.php").write_text("\n".join(lines) + "\n")
    w(r / "app/Notifications/ResetPassword.php", """
        <?php

        namespace App\\Notifications;

        use Illuminate\\Bus\\Queueable;
        use Illuminate\\Notifications\\Messages\\MailMessage;
        use Illuminate\\Notifications\\Notification;

        class ResetPassword extends Notification
        {
            use Queueable;

            public function __construct(public string $token)
            {
            }

            public function via($notifiable): array
            {
                return ['mail'];
            }

            /**
             * Build the password-reset e-mail with a signed link valid for 60 minutes.
             */
            public function toMail($notifiable): MailMessage
            {
                $url = url(route('password.reset', [
                    'token' => $this->token,
                    'email' => $notifiable->getEmailForPasswordReset(),
                ], false));

                return (new MailMessage)
                    ->subject('Reset your clinic portal password')
                    ->line('You are receiving this email because we received a password reset request.')
                    ->action('Reset Password', $url)
                    ->line('This link expires in 60 minutes.');
            }
        }
    """)
    w(r / "docker-compose.yml", """
        services:
          web:
            build: .
            ports: ["8080:80"]
          mysql:
            image: mysql:8.0
            environment:
              MYSQL_DATABASE: clinic
              MYSQL_ROOT_PASSWORD: Clinic2024!root
    """)
    w(r / ".gitignore", "/vendor\n.env\n")

    # --- role based access control (admin / doctor / receptionist)
    w(r / "app/Models/User.php", """
        <?php

        namespace App\\Models;

        use Illuminate\\Foundation\\Auth\\User as Authenticatable;
        use Illuminate\\Database\\Eloquent\\Relations\\BelongsToMany;
        use Tymon\\JWTAuth\\Contracts\\JWTSubject;

        class User extends Authenticatable implements JWTSubject
        {
            protected $fillable = ['name', 'email', 'password'];
            protected $hidden = ['password', 'remember_token'];

            public function roles(): BelongsToMany
            {
                return $this->belongsToMany(Role::class);
            }

            /**
             * True if the user has any of the given roles, e.g. hasRole('doctor', 'admin').
             */
            public function hasRole(string ...$roles): bool
            {
                return $this->roles->pluck('name')->intersect($roles)->isNotEmpty();
            }

            /**
             * Permissions come from the user's roles (role -> permissions pivot).
             */
            public function hasPermission(string $permission): bool
            {
                return $this->roles()
                    ->whereHas('permissions', fn ($q) => $q->where('name', $permission))
                    ->exists();
            }

            public function getJWTIdentifier()
            {
                return $this->getKey();
            }

            public function getJWTCustomClaims(): array
            {
                return ['roles' => $this->roles->pluck('name')];
            }
        }
    """)
    w(r / "app/Models/Role.php", """
        <?php

        namespace App\\Models;

        use Illuminate\\Database\\Eloquent\\Model;
        use Illuminate\\Database\\Eloquent\\Relations\\BelongsToMany;

        class Role extends Model
        {
            protected $fillable = ['name', 'label'];

            public function permissions(): BelongsToMany
            {
                return $this->belongsToMany(Permission::class);
            }

            public function users(): BelongsToMany
            {
                return $this->belongsToMany(User::class);
            }
        }
    """)
    w(r / "app/Models/Permission.php", """
        <?php

        namespace App\\Models;

        use Illuminate\\Database\\Eloquent\\Model;

        class Permission extends Model
        {
            protected $fillable = ['name', 'label'];
        }
    """)
    w(r / "app/Http/Middleware/EnsureUserHasRole.php", """
        <?php

        namespace App\\Http\\Middleware;

        use Closure;
        use Illuminate\\Http\\Request;
        use Symfony\\Component\\HttpFoundation\\Response;

        /**
         * Route middleware: ->middleware('role:doctor,admin') lets only those roles through.
         */
        class EnsureUserHasRole
        {
            public function handle(Request $request, Closure $next, string ...$roles): Response
            {
                $user = $request->user();
                if (!$user || !$user->hasRole(...$roles)) {
                    return response()->json(['error' => 'forbidden', 'required_roles' => $roles], 403);
                }
                return $next($request);
            }
        }
    """)
    w(r / "app/Providers/AuthServiceProvider.php", """
        <?php

        namespace App\\Providers;

        use App\\Models\\Permission;
        use Illuminate\\Foundation\\Support\\Providers\\AuthServiceProvider as ServiceProvider;
        use Illuminate\\Support\\Facades\\Gate;
        use Illuminate\\Support\\Facades\\Schema;

        class AuthServiceProvider extends ServiceProvider
        {
            public function boot(): void
            {
                // admins can do everything
                Gate::before(fn ($user) => $user->hasRole('admin') ? true : null);

                // one gate per permission row: Gate::allows('view-medical-records')
                if (Schema::hasTable('permissions')) {
                    foreach (Permission::all() as $permission) {
                        Gate::define($permission->name, fn ($user) => $user->hasPermission($permission->name));
                    }
                }
            }
        }
    """)
    w(r / "database/migrations/2024_02_01_000000_create_roles_and_permissions_tables.php", """
        <?php

        use Illuminate\\Database\\Migrations\\Migration;
        use Illuminate\\Database\\Schema\\Blueprint;
        use Illuminate\\Support\\Facades\\Schema;

        return new class extends Migration
        {
            public function up(): void
            {
                Schema::create('roles', function (Blueprint $table) {
                    $table->id();
                    $table->string('name')->unique();   // admin, doctor, receptionist
                    $table->string('label')->nullable();
                    $table->timestamps();
                });
                Schema::create('permissions', function (Blueprint $table) {
                    $table->id();
                    $table->string('name')->unique();   // view-medical-records, manage-appointments
                    $table->string('label')->nullable();
                    $table->timestamps();
                });
                Schema::create('role_user', function (Blueprint $table) {
                    $table->foreignId('role_id')->constrained()->cascadeOnDelete();
                    $table->foreignId('user_id')->constrained()->cascadeOnDelete();
                    $table->primary(['role_id', 'user_id']);
                });
                Schema::create('permission_role', function (Blueprint $table) {
                    $table->foreignId('permission_id')->constrained()->cascadeOnDelete();
                    $table->foreignId('role_id')->constrained()->cascadeOnDelete();
                    $table->primary(['permission_id', 'role_id']);
                });
            }

            public function down(): void
            {
                Schema::dropIfExists('permission_role');
                Schema::dropIfExists('role_user');
                Schema::dropIfExists('permissions');
                Schema::dropIfExists('roles');
            }
        };
    """)
    w(r / "app/Http/Kernel.php", """
        <?php

        namespace App\\Http;

        use Illuminate\\Foundation\\Http\\Kernel as HttpKernel;

        class Kernel extends HttpKernel
        {
            protected $middlewareGroups = [
                'api' => [
                    \\Illuminate\\Routing\\Middleware\\ThrottleRequests::class.':api',
                    \\Illuminate\\Routing\\Middleware\\SubstituteBindings::class,
                ],
            ];

            protected $middlewareAliases = [
                'auth' => \\App\\Http\\Middleware\\Authenticate::class,
                'role' => \\App\\Http\\Middleware\\EnsureUserHasRole::class,
                'throttle' => \\Illuminate\\Routing\\Middleware\\ThrottleRequests::class,
            ];
        }
    """)
    w(r / "routes/api.php", """
        <?php

        use App\\Http\\Controllers\\AppointmentController;
        use App\\Http\\Controllers\\AuthController;
        use App\\Http\\Controllers\\MedicalRecordController;
        use Illuminate\\Support\\Facades\\Route;

        Route::post('auth/login', [AuthController::class, 'login']);

        Route::middleware('auth:api')->group(function () {
            Route::post('auth/refresh', [AuthController::class, 'refresh']);
            Route::post('auth/logout', [AuthController::class, 'logout']);
            Route::get('auth/me', [AuthController::class, 'me']);

            Route::middleware('role:doctor,admin')->group(function () {
                Route::get('records/{patient}', [MedicalRecordController::class, 'show']);
            });
            Route::middleware('role:receptionist,admin')->apiResource('appointments', AppointmentController::class);
        });
    """)


def expense_tracker() -> None:
    r = REPOS / "side/expense-tracker"
    w(r / "package.json", '{"name": "expense-tracker", "version": "1.4.0", "dependencies": {"express": "^4.19.0", "ioredis": "^5.4.0", "sharp": "^0.33.0", "nodemailer": "^6.9.0", "jsonwebtoken": "^9.0.0"}}\n')
    w(r / ".gitignore", "node_modules\n.env\ndist\n")
    w(r / "src/auth/tokens.ts", """
        import crypto from 'node:crypto';
        import jwt from 'jsonwebtoken';
        import { redis } from '../lib/redis';

        const ACCESS_TTL = '15m';
        const REFRESH_TTL_SECONDS = 60 * 60 * 24 * 30;

        export function signAccessToken(userId: string): string {
          return jwt.sign({ sub: userId }, process.env.JWT_SECRET!, { expiresIn: ACCESS_TTL });
        }

        function hashToken(token: string): string {
          return crypto.createHash('sha256').update(token).digest('hex');
        }

        export async function issueRefreshToken(userId: string): Promise<string> {
          const token = crypto.randomBytes(48).toString('base64url');
          await redis.set(`rt:${hashToken(token)}`, userId, 'EX', REFRESH_TTL_SECONDS);
          return token;
        }

        /**
         * Rotate a refresh token: the presented token is single-use. It is looked up by its
         * SHA-256 hash in Redis, deleted, and a brand-new token is issued. Re-use of an old
         * token (theft detection) revokes every session of that user.
         */
        export async function rotateRefreshToken(presented: string) {
          const key = `rt:${hashToken(presented)}`;
          const userId = await redis.getdel(key);
          if (!userId) {
            const stolenFrom = await redis.get(`rt-used:${hashToken(presented)}`);
            if (stolenFrom) {
              await revokeAllSessions(stolenFrom);
            }
            throw new Error('invalid_refresh_token');
          }
          await redis.set(`rt-used:${hashToken(presented)}`, userId, 'EX', REFRESH_TTL_SECONDS);
          const refreshToken = await issueRefreshToken(userId);
          return { accessToken: signAccessToken(userId), refreshToken };
        }

        export async function revokeAllSessions(userId: string): Promise<void> {
          const keys = await redis.keys('rt:*');
          for (const k of keys) {
            if ((await redis.get(k)) === userId) await redis.del(k);
          }
        }
    """)
    w(r / "src/mail/reset.ts", """
        import nodemailer from 'nodemailer';

        const transport = nodemailer.createTransport({ host: process.env.SMTP_HOST, port: 587 });

        export async function sendResetEmail(to: string, token: string) {
          const link = `${process.env.APP_URL}/reset?token=${encodeURIComponent(token)}`;
          await transport.sendMail({
            from: 'Expense Tracker <no-reply@example.com>',
            to,
            subject: 'Password reset',
            text: `Click to choose a new password: ${link} (valid 1 hour)`,
          });
        }
    """)
    w(r / "src/upload/images.ts", """
        import sharp from 'sharp';
        import path from 'node:path';

        /**
         * Make a 320px-wide thumbnail and an 1280px web version of an uploaded receipt photo.
         */
        export async function makeThumbnails(filePath: string, outDir: string) {
          const base = path.parse(filePath).name;
          const thumb = path.join(outDir, `${base}_320.webp`);
          const web = path.join(outDir, `${base}_1280.webp`);
          await sharp(filePath).rotate().resize({ width: 320 }).webp({ quality: 70 }).toFile(thumb);
          await sharp(filePath).rotate().resize({ width: 1280, withoutEnlargement: true }).webp({ quality: 82 }).toFile(web);
          return { thumb, web };
        }
    """)
    w(r / "src/payments/stripe.ts", """
        import Stripe from 'stripe';
        import type { Request, Response } from 'express';

        const stripe = new Stripe(process.env.STRIPE_SECRET_KEY!);

        export async function stripeWebhook(req: Request, res: Response) {
          const sig = req.headers['stripe-signature'] as string;
          let event: Stripe.Event;
          try {
            event = stripe.webhooks.constructEvent(req.body, sig, process.env.STRIPE_WEBHOOK_SECRET!);
          } catch (err) {
            return res.status(400).send(`Webhook Error: ${(err as Error).message}`);
          }
          if (event.type === 'invoice.paid') {
            // mark subscription active
          }
          res.json({ received: true });
        }
    """)
    w(r / "src/auth/rbac.ts", """
        import type { NextFunction, Request, Response } from 'express';

        export type Role = 'owner' | 'accountant' | 'viewer';

        /** What each role may do. Checked by requirePermission(). */
        const PERMISSIONS: Record<Role, string[]> = {
          owner: ['expenses:read', 'expenses:write', 'reports:export', 'members:manage'],
          accountant: ['expenses:read', 'expenses:write', 'reports:export'],
          viewer: ['expenses:read'],
        };

        /** Express middleware: only the listed roles may continue (req.user is set by requireAuth). */
        export function requireRole(...roles: Role[]) {
          return (req: Request, res: Response, next: NextFunction) => {
            const role = (req as any).user?.role as Role | undefined;
            if (!role || !roles.includes(role)) {
              return res.status(403).json({ error: 'forbidden', required: roles });
            }
            next();
          };
        }

        export function can(role: Role | undefined, permission: string): boolean {
          return !!role && PERMISSIONS[role]?.includes(permission);
        }

        export function requirePermission(permission: string) {
          return (req: Request, res: Response, next: NextFunction) => {
            if (!can((req as any).user?.role, permission)) {
              return res.status(403).json({ error: 'forbidden', permission });
            }
            next();
          };
        }
    """)
    w(r / "src/lib/redis.ts", "import Redis from 'ioredis';\nexport const redis = new Redis(process.env.REDIS_URL ?? 'redis://localhost:6379');\n")
    fake_key = "sk_live_" + "51NxQe" + "R4nd0mD3m0K3yN0tR3al00xyz"
    w(r / ".env.backup", f"""
        APP_URL=https://expenses.example.com
        JWT_SECRET=please-change
        STRIPE_SECRET_KEY={fake_key}
        REDIS_URL=redis://localhost:6379
    """)


def invoice_bot() -> None:
    r = REPOS / "invoice-bot"
    w(r / "requirements.txt", "httpx\njinja2\nweasyprint\n")
    w(r / "invoice_bot/render.py", '''
        """Render monthly invoices to PDF and e-mail them to clients."""
        import smtplib
        from email.message import EmailMessage

        from jinja2 import Template
        from weasyprint import HTML


        def render_invoice(client: dict, items: list[dict]) -> bytes:
            total = sum(i["hours"] * i["rate"] for i in items)
            html = Template(open("templates/invoice.html").read()).render(client=client, items=items, total=total)
            return HTML(string=html).write_pdf()


        def email_invoice(to: str, pdf: bytes, month: str) -> None:
            msg = EmailMessage()
            msg["Subject"] = f"Invoice {month}"
            msg["To"] = to
            msg.set_content("Please find this month's invoice attached.")
            msg.add_attachment(pdf, maintype="application", subtype="pdf", filename=f"invoice-{month}.pdf")
            with smtplib.SMTP("localhost") as s:
                s.send_message(msg)
    ''')
    git(r, "init", "-q", "-b", "main")
    git(r, "add", "-A")
    git(r, "commit", "-q", "-m", "invoice-bot: render + email invoices", date="2026-11-05T09:00:00")


def old_blog_zip() -> None:
    tmp = HERE / "_old-blog"
    w(tmp / "old-blog/lib/session.php", """
        <?php
        // Very old session helper (2022). Long-lived "remember me" cookie, no rotation.
        function remember_login($userId) {
            $token = md5($userId . time());
            setcookie('remember', $token, time() + 60 * 60 * 24 * 365, '/');
            db()->query("UPDATE users SET remember_token = '$token' WHERE id = $userId");
        }

        function refresh_session() {
            // refresh token == same cookie forever; not recommended
            if (!empty($_COOKIE['remember'])) {
                $_SESSION['uid'] = user_by_token($_COOKIE['remember']);
            }
        }
    """)
    w(tmp / "old-blog/config/db.php", """
        <?php
        $db_host = 'localhost';
        $db_user = 'blog';
        $db_password = 'blogpass2022';
        $db_name = 'blog';
    """)
    w(tmp / "old-blog/index.php", "<?php require 'lib/session.php'; require 'config/db.php'; echo 'Hello blog';\n")
    out = REPOS / "archive/old-blog.zip"
    out.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(out, "w") as z:
        for p in (tmp).rglob("*"):
            if p.is_file():
                z.write(p, p.relative_to(tmp).as_posix())
    shutil.rmtree(tmp)


# ---------------------------------------------------------------- a NEW project to build into (not indexed)
def new_project() -> None:
    """A fresh Laravel API with no auth yet: the target for `tracebase copy` demos."""
    r = V / "workspace/booking-api"
    w(r / "composer.json", '{"name": "acme/booking-api", "require": {"php": "^8.1", "laravel/framework": "^10.0"}}\n')
    w(r / ".gitignore", "/vendor\n.env\n")
    w(r / "app/Models/User.php", """
        <?php

        namespace App\\Models;

        use Illuminate\\Foundation\\Auth\\User as Authenticatable;
        use Illuminate\\Notifications\\Notifiable;

        class User extends Authenticatable
        {
            use Notifiable;

            protected $fillable = ['name', 'email', 'password'];
            protected $hidden = ['password', 'remember_token'];
            protected $casts = ['password' => 'hashed'];

            public function bookings()
            {
                return $this->hasMany(Booking::class);
            }
        }
    """)
    w(r / "app/Models/Booking.php", """
        <?php

        namespace App\\Models;

        use Illuminate\\Database\\Eloquent\\Model;

        class Booking extends Model
        {
            protected $fillable = ['user_id', 'room_id', 'starts_at', 'ends_at', 'status'];
            protected $casts = ['starts_at' => 'datetime', 'ends_at' => 'datetime'];

            public function user()
            {
                return $this->belongsTo(User::class);
            }
        }
    """)
    w(r / "app/Http/Controllers/Controller.php", """
        <?php

        namespace App\\Http\\Controllers;

        use Illuminate\\Foundation\\Auth\\Access\\AuthorizesRequests;
        use Illuminate\\Foundation\\Validation\\ValidatesRequests;
        use Illuminate\\Routing\\Controller as BaseController;

        class Controller extends BaseController
        {
            use AuthorizesRequests, ValidatesRequests;
        }
    """)
    w(r / "app/Http/Controllers/BookingController.php", """
        <?php

        namespace App\\Http\\Controllers;

        use App\\Models\\Booking;
        use Illuminate\\Http\\Request;

        class BookingController extends Controller
        {
            public function index()
            {
                return Booking::latest()->paginate(20);
            }

            public function store(Request $request)
            {
                $data = $request->validate([
                    'room_id' => 'required|integer',
                    'starts_at' => 'required|date',
                    'ends_at' => 'required|date|after:starts_at',
                ]);
                return Booking::create($data + ['user_id' => $request->user()?->id, 'status' => 'pending']);
            }

            public function destroy(Booking $booking)
            {
                $booking->delete();
                return response()->noContent();
            }
        }
    """)
    w(r / "app/Http/Kernel.php", """
        <?php

        namespace App\\Http;

        use Illuminate\\Foundation\\Http\\Kernel as HttpKernel;

        class Kernel extends HttpKernel
        {
            protected $middlewareGroups = [
                'api' => [
                    \\Illuminate\\Routing\\Middleware\\ThrottleRequests::class.':api',
                    \\Illuminate\\Routing\\Middleware\\SubstituteBindings::class,
                ],
            ];

            protected $middlewareAliases = [
                'auth' => \\App\\Http\\Middleware\\Authenticate::class,
                'throttle' => \\Illuminate\\Routing\\Middleware\\ThrottleRequests::class,
            ];
        }
    """)
    w(r / "routes/api.php", """
        <?php

        use App\\Http\\Controllers\\BookingController;
        use Illuminate\\Support\\Facades\\Route;

        Route::get('bookings', [BookingController::class, 'index']);
        Route::post('bookings', [BookingController::class, 'store']);
        Route::delete('bookings/{booking}', [BookingController::class, 'destroy']);
    """)
    w(r / "database/migrations/2014_10_12_000000_create_users_table.php", """
        <?php

        use Illuminate\\Database\\Migrations\\Migration;
        use Illuminate\\Database\\Schema\\Blueprint;
        use Illuminate\\Support\\Facades\\Schema;

        return new class extends Migration
        {
            public function up(): void
            {
                Schema::create('users', function (Blueprint $table) {
                    $table->id();
                    $table->string('name');
                    $table->string('email')->unique();
                    $table->string('password');
                    $table->rememberToken();
                    $table->timestamps();
                });
            }

            public function down(): void
            {
                Schema::dropIfExists('users');
            }
        };
    """)
    git(r, "init", "-q", "-b", "main")
    git(r, "add", "-A")
    git(r, "commit", "-q", "-m", "Booking API skeleton", date="2026-11-01T10:00:00")


# ---------------------------------------------------------------- go-auth-service (Go, Gin)
def go_auth() -> None:
    r = REPOS / "side/go-auth-service"
    w(r / "go.mod", """
        module github.com/ankit/go-auth-service

        go 1.22

        require (
            github.com/gin-gonic/gin v1.10.0
            github.com/golang-jwt/jwt/v5 v5.2.1
            golang.org/x/crypto v0.24.0
        )
    """)
    w(r / "main.go", """
        package main

        import (
            "github.com/ankit/go-auth-service/internal/handlers"
            "github.com/ankit/go-auth-service/internal/middleware"
            "github.com/gin-gonic/gin"
        )

        func main() {
            r := gin.Default()
            r.POST("/login", handlers.Login)
            admin := r.Group("/admin", middleware.RequireRole("admin"))
            admin.GET("/users", handlers.ListUsers)
            r.Run(":8080")
        }
    """)
    w(r / "internal/auth/jwt.go", """
        package auth

        import (
            "os"
            "time"

            "github.com/golang-jwt/jwt/v5"
        )

        // Claims carried in our access tokens.
        type Claims struct {
            UserID int64    `json:"uid"`
            Roles  []string `json:"roles"`
            jwt.RegisteredClaims
        }

        // IssueToken signs a 15-minute access token for the user.
        func IssueToken(userID int64, roles []string) (string, error) {
            claims := Claims{UserID: userID, Roles: roles, RegisteredClaims: jwt.RegisteredClaims{
                ExpiresAt: jwt.NewNumericDate(time.Now().Add(15 * time.Minute)),
            }}
            return jwt.NewWithClaims(jwt.SigningMethodHS256, claims).SignedString([]byte(os.Getenv("JWT_SECRET")))
        }

        // ParseToken verifies a token and returns its claims.
        func ParseToken(raw string) (*Claims, error) {
            c := &Claims{}
            _, err := jwt.ParseWithClaims(raw, c, func(t *jwt.Token) (any, error) {
                return []byte(os.Getenv("JWT_SECRET")), nil
            })
            return c, err
        }
    """)
    w(r / "internal/middleware/rbac.go", """
        package middleware

        import (
            "net/http"
            "strings"

            "github.com/ankit/go-auth-service/internal/auth"
            "github.com/gin-gonic/gin"
        )

        // RequireRole lets the request through only if the bearer token has one of the roles.
        func RequireRole(roles ...string) gin.HandlerFunc {
            return func(c *gin.Context) {
                claims, err := auth.ParseToken(strings.TrimPrefix(c.GetHeader("Authorization"), "Bearer "))
                if err != nil || !hasAny(claims.Roles, roles) {
                    c.AbortWithStatusJSON(http.StatusForbidden, gin.H{"error": "forbidden"})
                    return
                }
                c.Set("uid", claims.UserID)
                c.Next()
            }
        }
    """)
    w(r / "internal/middleware/roles.go", """
        package middleware

        func hasAny(have, want []string) bool {
            for _, h := range have {
                for _, w := range want {
                    if h == w {
                        return true
                    }
                }
            }
            return false
        }
    """)
    w(r / "internal/store/users.go", """
        package store

        import "golang.org/x/crypto/bcrypt"

        type User struct {
            ID           int64
            Email        string
            PasswordHash string
            Roles        []string
        }

        var users = map[string]*User{}

        func FindUserByEmail(email string) (*User, bool) {
            u, ok := users[email]
            return u, ok
        }

        func (u *User) CheckPassword(pw string) bool {
            return bcrypt.CompareHashAndPassword([]byte(u.PasswordHash), []byte(pw)) == nil
        }
    """)
    w(r / "internal/handlers/auth.go", """
        package handlers

        import (
            "net/http"

            "github.com/ankit/go-auth-service/internal/auth"
            "github.com/ankit/go-auth-service/internal/store"
            "github.com/gin-gonic/gin"
        )

        type loginRequest struct {
            Email    string `json:"email" binding:"required"`
            Password string `json:"password" binding:"required"`
        }

        // Login checks the credentials and returns an access token.
        func Login(c *gin.Context) {
            var req loginRequest
            if err := c.ShouldBindJSON(&req); err != nil {
                c.JSON(http.StatusBadRequest, gin.H{"error": err.Error()})
                return
            }
            u, ok := store.FindUserByEmail(req.Email)
            if !ok || !u.CheckPassword(req.Password) {
                c.JSON(http.StatusUnauthorized, gin.H{"error": "invalid email or password"})
                return
            }
            token, err := auth.IssueToken(u.ID, u.Roles)
            if err != nil {
                c.JSON(http.StatusInternalServerError, gin.H{"error": "could not sign token"})
                return
            }
            c.JSON(http.StatusOK, gin.H{"access_token": token})
        }

        func ListUsers(c *gin.Context) {
            c.JSON(http.StatusOK, gin.H{"users": []string{}})
        }
    """)


# ---------------------------------------------------------------- spring-clinic (Java, Spring Boot)
def spring_clinic() -> None:
    r = REPOS / "freelance/spring-clinic"
    w(r / "pom.xml", """
        <project>
          <modelVersion>4.0.0</modelVersion>
          <groupId>com.acme</groupId>
          <artifactId>spring-clinic</artifactId>
          <version>1.0.0</version>
          <parent>
            <groupId>org.springframework.boot</groupId>
            <artifactId>spring-boot-starter-parent</artifactId>
            <version>3.3.0</version>
          </parent>
          <dependencies>
            <dependency>
              <groupId>org.springframework.boot</groupId>
              <artifactId>spring-boot-starter-web</artifactId>
            </dependency>
            <dependency>
              <groupId>io.jsonwebtoken</groupId>
              <artifactId>jjwt-api</artifactId>
              <version>0.12.5</version>
            </dependency>
          </dependencies>
        </project>
    """)
    base = r / "src/main/java/com/acme/clinic"
    w(base / "ClinicApplication.java", """
        package com.acme.clinic;

        import org.springframework.boot.SpringApplication;
        import org.springframework.boot.autoconfigure.SpringBootApplication;

        @SpringBootApplication
        public class ClinicApplication {
            public static void main(String[] args) {
                SpringApplication.run(ClinicApplication.class, args);
            }
        }
    """)
    w(base / "security/JwtService.java", """
        package com.acme.clinic.security;

        import io.jsonwebtoken.Jwts;
        import io.jsonwebtoken.security.Keys;
        import java.util.Date;
        import javax.crypto.SecretKey;
        import org.springframework.stereotype.Service;

        @Service
        public class JwtService {
            private final SecretKey key = Keys.hmacShaKeyFor(System.getenv("JWT_SECRET").getBytes());

            /** Signs a one-hour token for the doctor or patient. */
            public String issue(String email, String role) {
                return Jwts.builder().subject(email).claim("role", role)
                    .expiration(new Date(System.currentTimeMillis() + 3_600_000)).signWith(key).compact();
            }
        }
    """)
    w(base / "model/Doctor.java", """
        package com.acme.clinic.model;

        public class Doctor {
            private Long id;
            private String email;
            private String passwordHash;
            private String role = "doctor";

            public String getEmail() { return email; }
            public String getRole() { return role; }
            public boolean checkPassword(String raw) { return raw != null && raw.hashCode() == passwordHash.hashCode(); }
        }
    """)
    w(base / "repository/DoctorRepository.java", """
        package com.acme.clinic.repository;

        import com.acme.clinic.model.Doctor;
        import java.util.Optional;

        public interface DoctorRepository {
            Optional<Doctor> findByEmail(String email);
        }
    """)
    w(base / "web/AuthController.java", """
        package com.acme.clinic.web;

        import com.acme.clinic.model.Doctor;
        import com.acme.clinic.repository.DoctorRepository;
        import com.acme.clinic.security.JwtService;
        import java.util.Map;
        import org.springframework.http.ResponseEntity;
        import org.springframework.web.bind.annotation.*;

        @RestController
        @RequestMapping("/auth")
        public class AuthController {
            private final DoctorRepository doctors;
            private final JwtService jwt;

            public AuthController(DoctorRepository doctors, JwtService jwt) {
                this.doctors = doctors;
                this.jwt = jwt;
            }

            /** Doctor login: returns a bearer token. */
            @PostMapping("/login")
            public ResponseEntity<?> login(@RequestBody Map<String, String> body) {
                Doctor d = doctors.findByEmail(body.get("email")).orElse(null);
                if (d == null || !d.checkPassword(body.get("password"))) {
                    return ResponseEntity.status(401).body(Map.of("error", "invalid credentials"));
                }
                return ResponseEntity.ok(Map.of("token", jwt.issue(d.getEmail(), d.getRole())));
            }
        }
    """)


# ---------------------------------------------------------------- rails-shop (Ruby on Rails)
def rails_shop() -> None:
    r = REPOS / "side/rails-shop"
    w(r / "Gemfile", """
        source 'https://rubygems.org'

        gem 'rails', '~> 7.1'
        gem 'pg', '~> 1.5'
        gem 'bcrypt', '~> 3.1'
        gem 'jwt', '~> 2.8'
    """)
    w(r / "config/routes.rb", """
        Rails.application.routes.draw do
          post '/login', to: 'sessions#create'
          resources :products
        end
    """)
    w(r / "app/controllers/application_controller.rb", """
        class ApplicationController < ActionController::API
          include Authorizable
        end
    """)
    w(r / "app/controllers/concerns/authorizable.rb", """
        module Authorizable
          extend ActiveSupport::Concern

          # before_action :require_role!, only: [...]  -> 403 unless the user has the role
          def require_role!(*roles)
            head :forbidden unless current_user&.has_role?(*roles)
          end

          def current_user
            @current_user ||= User.find_by(id: JsonWebToken.decode(request.headers['Authorization'].to_s.split.last)&.dig('uid'))
          end
        end
    """)
    w(r / "app/controllers/sessions_controller.rb", """
        class SessionsController < ApplicationController
          # POST /login
          def create
            user = User.find_by(email: params[:email])
            if user&.authenticate(params[:password])
              render json: { token: JsonWebToken.encode(uid: user.id) }
            else
              render json: { error: 'invalid email or password' }, status: :unauthorized
            end
          end
        end
    """)
    w(r / "app/models/user.rb", """
        class User < ApplicationRecord
          has_secure_password
          has_and_belongs_to_many :roles

          def has_role?(*names)
            roles.where(name: names.map(&:to_s)).exists?
          end
        end
    """)
    w(r / "app/models/role.rb", """
        class Role < ApplicationRecord
          has_and_belongs_to_many :users
        end
    """)
    w(r / "lib/json_web_token.rb", """
        require 'jwt'

        class JsonWebToken
          SECRET = ENV.fetch('JWT_SECRET')

          def self.encode(payload, exp = 24.hours.from_now)
            JWT.encode(payload.merge(exp: exp.to_i), SECRET)
          end

          def self.decode(token)
            JWT.decode(token, SECRET).first
          rescue JWT::DecodeError
            nil
          end
        end
    """)
    w(r / "db/migrate/20240105000000_create_roles.rb", """
        class CreateRoles < ActiveRecord::Migration[7.1]
          def change
            create_table :roles do |t|
              t.string :name, null: false, index: { unique: true }
              t.timestamps
            end
            create_join_table :roles, :users
          end
        end
    """)


def new_polyglot_projects() -> None:
    """Fresh Go / Java / Rails projects to copy into (not indexed)."""
    g = V / "workspace/booking-go"
    w(g / "go.mod", """
        module github.com/acme/booking-go

        go 1.22

        require github.com/gin-gonic/gin v1.10.0
    """)
    w(g / "main.go", """
        package main

        import (
            "net/http"

            "github.com/gin-gonic/gin"
        )

        func main() {
            r := gin.Default()
            r.GET("/health", func(c *gin.Context) { c.JSON(http.StatusOK, gin.H{"ok": true}) })
            r.Run(":8080")
        }
    """)
    j = V / "workspace/booking-java"
    w(j / "pom.xml", """
        <project>
          <modelVersion>4.0.0</modelVersion>
          <groupId>io.acme</groupId>
          <artifactId>booking-java</artifactId>
          <version>0.1.0</version>
          <dependencies>
            <dependency>
              <groupId>org.springframework.boot</groupId>
              <artifactId>spring-boot-starter-web</artifactId>
            </dependency>
          </dependencies>
        </project>
    """)
    w(j / "src/main/java/io/acme/booking/BookingApplication.java", """
        package io.acme.booking;

        import org.springframework.boot.SpringApplication;
        import org.springframework.boot.autoconfigure.SpringBootApplication;

        @SpringBootApplication
        public class BookingApplication {
            public static void main(String[] args) {
                SpringApplication.run(BookingApplication.class, args);
            }
        }
    """)
    w(j / "src/main/java/io/acme/booking/web/HealthController.java", """
        package io.acme.booking.web;

        import org.springframework.web.bind.annotation.GetMapping;
        import org.springframework.web.bind.annotation.RestController;

        @RestController
        public class HealthController {
            @GetMapping("/health")
            public String health() { return "ok"; }
        }
    """)
    rb = V / "workspace/booking-rails"
    w(rb / "Gemfile", """
        source 'https://rubygems.org'

        gem 'rails', '~> 7.1'
        gem 'pg', '~> 1.5'
    """)
    w(rb / "config/routes.rb", """
        Rails.application.routes.draw do
          resources :bookings
        end
    """)
    w(rb / "app/controllers/application_controller.rb", """
        class ApplicationController < ActionController::API
        end
    """)
    w(rb / "app/models/user.rb", """
        class User < ApplicationRecord
          has_many :bookings
        end
    """)
    for d in (g, j, rb):
        git(d, "init", "-q", "-b", "main")
        git(d, "add", "-A")
        git(d, "commit", "-q", "-m", "skeleton", date="2026-11-01T10:00:00")


# ---------------------------------------------------------------- logs (Phase 3: Log Detective)
def _line_of(path: Path, needle: str) -> int:
    return next(i for i, l in enumerate(path.read_text().splitlines(), 1) if needle in l)


def logs() -> None:
    """Two nights of production logs. Stack-trace line numbers come from the seeded repos, so they link to real code."""
    LOGS.mkdir(parents=True, exist_ok=True)
    cart = REPOS / "shop-api/app/Services/CartService.php"
    l_coupon = _line_of(cart, "$code  = $cart['coupon'];")
    l_total = _line_of(cart, "$afterDiscount = $this->applyDiscount($cart);")
    l_ctrl = _line_of(REPOS / "shop-api/app/Http/Controllers/CheckoutController.php", "$total = $this->carts->total($cart);")
    l_smtp = _line_of(REPOS / "invoice-bot/invoice_bot/render.py", 'with smtplib.SMTP("localhost")')

    # checkout: ~2 errors/hour baseline, then 3am: "connection refused" x thousands while the DB is down
    start, incident, incident_end = datetime(2026, 11, 7, 3, 0), datetime(2026, 11, 8, 3, 0, 1), datetime(2026, 11, 8, 3, 41)
    lines: list[tuple[datetime, str]] = []
    order, t = 88000, start
    while t < datetime(2026, 11, 8, 15, 0):
        t += timedelta(seconds=random.randint(20, 90))
        order += 1
        lines.append((t, f"{t:%Y-%m-%d %H:%M:%S} [INFO] checkout  Order {order} created"))
        if random.random() < 0.012:
            lines.append((t, f"{t:%Y-%m-%d %H:%M:%S} [ERROR] checkout  Payment gateway timeout after 30s (order {order})"))
    t, n = incident, 0
    while t < incident_end and n < 3940:
        lines.append((t, f"{t:%Y-%m-%d %H:%M:%S} [ERROR] checkout  SQLSTATE[HY000] [2002] Connection refused (host=db:3306)"))
        n += 1
        t += timedelta(milliseconds=random.randint(200, 1050))
    lines.sort(key=lambda x: x[0])
    (LOGS / "checkout.log").write_text("\n".join(l for _, l in lines) + "\n")

    # the real root cause, in two other services: the nightly backup stops the MySQL container
    (LOGS / "db.log").write_text(textwrap.dedent("""
        2026-11-07T21:14:02.118 0 [System] [MY-010931] [Server] /usr/sbin/mysqld: ready for connections. Version: '8.4.2'  port: 3306
        2026-11-08T03:00:00.402 0 [System] [MY-013172] [Server] Received SHUTDOWN from user root. Shutting down mysqld (Version: 8.4.2).
        2026-11-08T03:00:01.977 0 [System] [MY-010910] [Server] /usr/sbin/mysqld: Shutdown complete (mysqld 8.4.2)
        2026-11-08T03:40:57.310 0 [System] [MY-010116] [Server] /usr/sbin/mysqld (mysqld 8.4.2) starting as process 1
        2026-11-08T03:40:58.884 0 [System] [MY-010931] [Server] /usr/sbin/mysqld: ready for connections. Version: '8.4.2'  port: 3306
    """).lstrip())
    (LOGS / "nightly-backup.log").write_text(textwrap.dedent("""
        2026-11-08 03:00:00 [INFO] nightly-backup  stopping container db for cold dump of /var/lib/mysql
        2026-11-08 03:40:51 [INFO] nightly-backup  dump finished (412 MB), starting container db
    """).lstrip())

    # payments: a known, noisy Stripe webhook problem (JSON logs)
    import json
    pl, t = [], datetime(2026, 11, 7, 6, 0)
    for i in range(212):
        t += timedelta(minutes=random.randint(1, 9))
        pl.append(json.dumps({"time": f"{t:%Y-%m-%dT%H:%M:%S}", "level": "error", "service": "payments",
                              "msg": f"Stripe webhook signature mismatch (evt_1Q9x{random.randint(10000, 99999)}...)"}))
        if i % 3 == 0:
            pl.append(json.dumps({"time": f"{t:%Y-%m-%dT%H:%M:%S}", "level": "info", "service": "payments",
                                  "msg": "payment_intent.succeeded processed"}))
    (LOGS / "payments.jsonl").write_text("\n".join(pl) + "\n")

    # shop-api (Laravel): Undefined array key "coupon" right after the 13:42 deploy
    ll, t = [], datetime(2026, 11, 8, 14, 3, 12)
    for i in range(64):
        t += timedelta(seconds=random.randint(20, 75))
        ll.append(f"[{t:%Y-%m-%d %H:%M:%S}] production.ERROR: Undefined array key \"coupon\" {{\"userId\":{random.randint(100, 999)},"
                  f"\"exception\":\"[object] (ErrorException(code: 0): Undefined array key \\\"coupon\\\" at "
                  f"/var/www/html/app/Services/CartService.php:{l_coupon})")
        ll += ["[stacktrace]",
               "#0 /var/www/html/vendor/laravel/framework/src/Illuminate/Foundation/Bootstrap/HandleExceptions.php(255): "
               "Illuminate\\Foundation\\Bootstrap\\HandleExceptions->handleError()",
               f"#1 /var/www/html/app/Services/CartService.php({l_coupon}): Illuminate\\Foundation\\Bootstrap\\HandleExceptions->"
               "Illuminate\\Foundation\\Bootstrap\\{closure}()",
               f"#2 /var/www/html/app/Services/CartService.php({l_total}): App\\Services\\CartService->applyDiscount()",
               f"#3 /var/www/html/app/Http/Controllers/CheckoutController.php({l_ctrl}): App\\Services\\CartService->total()",
               "#4 {main}", "\"}"]
    (LOGS / "shop-api").mkdir(exist_ok=True)
    (LOGS / "shop-api" / "laravel.log").write_text("\n".join(ll) + "\n")

    # invoice-bot: a Python traceback
    (LOGS / "invoice-bot.log").write_text(textwrap.dedent(f"""
        2026-11-08 06:00:00,101 - invoice_bot - INFO - rendering 12 invoices
        2026-11-08 06:00:03,552 - invoice_bot - ERROR - failed to email invoice
        Traceback (most recent call last):
          File "/app/invoice_bot/render.py", line {l_smtp}, in email_invoice
            with smtplib.SMTP("localhost") as s:
          File "/usr/lib/python3.12/smtplib.py", line 255, in __init__
            (code, msg) = self.connect(host, port)
        ConnectionRefusedError: [Errno 111] Connection refused
        2026-11-08 06:00:04,010 - invoice_bot - INFO - done
    """).lstrip())


def main() -> None:
    if "--reset" in sys.argv and V.exists():
        shutil.rmtree(V)
    if "--logs" in sys.argv:  # add / refresh only the demo logs
        logs()
        print(f"demo logs written to {LOGS}")
        return
    if (REPOS / "shop-api").exists():
        print(f"{V} already exists (use --reset to recreate, or --logs to add the demo logs)")
        return
    for d in (REPOS,):
        d.mkdir(parents=True, exist_ok=True)
    shop_api()
    clinic_app()
    expense_tracker()
    invoice_bot()
    old_blog_zip()
    go_auth()
    spring_clinic()
    rails_shop()
    new_project()
    new_polyglot_projects()
    logs()
    print(f"demo data created in {V}")
    print(f"try:  cd {V / 'workspace/booking-api'} && tracebase copy \"role based access control\"")


if __name__ == "__main__":
    main()
