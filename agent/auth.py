"""
API authentication middleware — JWT + API Key support (P1-B 修复版).

变更点：
- query 参数解析改用 urllib.parse.parse_qs（旧版手写 split 遇到含 '=' 的值直接崩）
- 新增 AuthMiddleware.create_access_token(subject, tenant_id)（app_fastapi.py 的
  /api/auth/token 依赖它；PyJWT 延迟导入，未安装/未配置 JWT_SECRET 时抛 ValueError）
- check_api_key 成功后在请求对象上写入 auth_subject / auth_tenant_id / auth_scheme
  三个属性（app_fastapi.py 的 authenticate_request 中读取，契约见该文件 150-160 行）

Usage:
    from agent.auth import AuthMiddleware, require_auth
    if not AuthMiddleware.check_api_key(self):
        self.send_error(401)
        return
"""

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
import urllib.parse
from typing import Optional


JWT_SECRET_MIN_BYTES = 32  # 256 bits — OWASP minimum for HMAC-SHA256


def _jwt_secret() -> str:
    secret = os.getenv("JWT_SECRET", "").strip()
    if secret:
        _reject_production_placeholder(secret)
        environment = os.getenv("APP_ENV", "").strip().lower()
        if environment in {"prod", "production"} and len(secret.encode("utf-8")) < JWT_SECRET_MIN_BYTES:
            raise ValueError(
                f"JWT_SECRET must be at least {JWT_SECRET_MIN_BYTES} bytes in production "
                f"(got {len(secret.encode('utf-8'))} bytes). Use: python -c 'import secrets; print(secrets.token_urlsafe(48))'"
            )
    return secret


def _jwt_ttl() -> int:
    try:
        # JWT_ACCESS_TTL_SECONDS is the public access-token setting; keep
        # JWT_TTL_SECONDS as a backwards-compatible fallback.
        raw = os.getenv("JWT_ACCESS_TTL_SECONDS",
                        os.getenv("JWT_TTL_SECONDS", "3600"))
        return int(raw)
    except ValueError:
        return 3600




def _refresh_token_ttl() -> int:
    """Return the browser refresh-session lifetime in seconds."""
    try:
        return max(60, int(os.getenv("JWT_REFRESH_TTL_SECONDS", str(60 * 60 * 24 * 14))))
    except (TypeError, ValueError):
        return 60 * 60 * 24 * 14


def _refresh_token_pepper() -> str:
    """Use a server-side pepper so a DB dump cannot be replayed as tokens."""
    pepper = os.getenv("REFRESH_TOKEN_PEPPER", "").strip() or _jwt_secret()
    if not pepper:
        raise ValueError("JWT_SECRET or REFRESH_TOKEN_PEPPER is required for refresh tokens")
    _reject_production_placeholder(pepper)
    return pepper


def refresh_token_ttl_seconds() -> int:
    """Public refresh-token TTL helper used by the HTTP and persistence layers."""
    return _refresh_token_ttl()


def create_refresh_token() -> str:
    """Create a high-entropy opaque refresh token; it is never a JWT."""
    _refresh_token_pepper()  # fail closed when server-side protection is absent
    return secrets.token_urlsafe(48)


def hash_refresh_token(token: str) -> str:
    """Return a keyed one-way hash suitable for PostgreSQL storage."""
    if not token:
        raise ValueError("refresh token must not be empty")
    return hmac.new(
        _refresh_token_pepper().encode("utf-8"),
        token.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()

def _reject_production_placeholder(secret: str) -> None:
    """Reject example secrets when the application is running in production."""
    environment = os.getenv("APP_ENV", "").strip().lower()
    if environment not in {"prod", "production"}:
        return
    normalized = secret.strip().lower()
    placeholders = {
        "replace-with-a-long-random-secret",
        "change-me",
        "your-secret-key",
        "your-jwt-secret",
    }
    if normalized in placeholders or normalized.startswith((
        "replace-with-", "change-me-", "your-secret",
    )):
        raise ValueError("JWT_SECRET is a placeholder and cannot be used in production")


# ════════════════════════════════════════════════════════════════════
# JWT 编解码：PyJWT 优先（守卫导入），缺席时降级纯 stdlib HS256 实现。
# 两条路径均产出标准 HS256 JWT，互相可解码 —— 保证：
#   * 生产装了 PyJWT 时行为不变；
#   * 纯 stdlib 单测/无第三方依赖环境仍可签发+校验（幂等键、往返测试）。
# ════════════════════════════════════════════════════════════════════

_JWT_ALG = "HS256"


def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64url_decode(seg: str) -> bytes:
    pad = "=" * (-len(seg) % 4)
    return base64.urlsafe_b64decode(seg + pad)


def _stdlib_jwt_encode(payload: dict, secret: str) -> str:
    """纯 stdlib HS256 签发（PyJWT 缺席时的降级路径）。"""
    header = {"alg": _JWT_ALG, "typ": "JWT"}
    seg_h = _b64url_encode(json.dumps(header, separators=(",", ":"),
                                      sort_keys=True).encode("utf-8"))
    seg_p = _b64url_encode(json.dumps(payload, separators=(",", ":"),
                                      sort_keys=True).encode("utf-8"))
    signing_input = f"{seg_h}.{seg_p}".encode("ascii")
    sig = hmac.new(secret.encode("utf-8"), signing_input,
                   hashlib.sha256).digest()
    return f"{seg_h}.{seg_p}.{_b64url_encode(sig)}"


def _stdlib_jwt_decode(token: str, secret: str) -> dict:
    """纯 stdlib HS256 校验；签名/结构/过期任一不符抛 ValueError。"""
    parts = token.split(".")
    if len(parts) != 3:
        raise ValueError("malformed token")
    seg_h, seg_p, seg_s = parts
    signing_input = f"{seg_h}.{seg_p}".encode("ascii")
    expected = hmac.new(secret.encode("utf-8"), signing_input,
                        hashlib.sha256).digest()
    try:
        got = _b64url_decode(seg_s)
    except Exception as e:  # noqa: BLE001
        raise ValueError("bad signature encoding") from e
    if not hmac.compare_digest(expected, got):
        raise ValueError("signature verification failed")
    try:
        claims = json.loads(_b64url_decode(seg_p).decode("utf-8"))
    except Exception as e:  # noqa: BLE001
        raise ValueError("bad payload") from e
    exp = claims.get("exp")
    if exp is not None and int(time.time()) >= int(exp):
        raise ValueError("token expired")
    return claims


def _jwt_encode(payload: dict, secret: str) -> str:
    """PyJWT 优先，缺席降级 stdlib。"""
    try:
        import jwt  # PyJWT，延迟导入（守卫）
    except ImportError:
        return _stdlib_jwt_encode(payload, secret)
    token = jwt.encode(payload, secret, algorithm=_JWT_ALG)
    return token.decode("utf-8") if isinstance(token, bytes) else token


def _jwt_decode(token: str, secret: str) -> dict:
    """PyJWT 优先，缺席降级 stdlib。无效时抛异常（由调用方决定吞/抛）。"""
    try:
        import jwt  # PyJWT，延迟导入（守卫）
    except ImportError:
        return _stdlib_jwt_decode(token, secret)
    return jwt.decode(token, secret, algorithms=[_JWT_ALG])


# ── 模块级公开 API（app_fastapi /api/auth/* 与 user_memory 复用）────

def create_access_token(subject: str, tenant: str = "default",
                        ttl: Optional[int] = None, scope: str = "") -> str:
    """签发 HS256 access token。

    Args:
        subject: user_id（记忆主键）；不可为空。
        tenant:  租户隔离标识（默认 "default"）。
        ttl:     有效期秒数；None 时读 JWT_TTL_SECONDS（默认 3600）。
        scope:   权限范围（如 "admin"），空表示普通用户。

    Raises:
        ValueError: JWT_SECRET 未配置 或 subject 为空。
    """
    secret = _jwt_secret()
    if not secret:
        raise ValueError("JWT_SECRET is not configured; token issuance disabled")
    _reject_production_placeholder(secret)
    if not subject:
        raise ValueError("subject must not be empty")
    now = int(time.time())
    payload = {
        "sub": subject,
        "tenant_id": tenant or "default",
        "iat": now,
        "exp": now + int(ttl if ttl is not None else _jwt_ttl()),
    }
    if scope:
        payload["scope"] = scope
    return _jwt_encode(payload, secret)


def verify_token(token: str) -> dict:
    """校验并解出 claims。

    Raises:
        ValueError: JWT_SECRET 未配置 / 签名无效 / 过期 / 结构损坏。
    """
    secret = _jwt_secret()
    if not secret:
        raise ValueError("JWT_SECRET is not configured; verification disabled")
    if not token or token.count(".") != 2:
        raise ValueError("malformed token")
    try:
        return _jwt_decode(token, secret)
    except ValueError:
        raise
    except Exception as e:  # PyJWT 的 ExpiredSignature / InvalidToken 等
        raise ValueError(str(e) or "invalid token") from e


# ── 密码哈希（pbkdf2-hmac-sha256；纯 stdlib）─────────────────────────

_PBKDF2_ROUNDS = 120_000


def hash_password(password: str, *, salt: Optional[bytes] = None) -> str:
    """pbkdf2_hmac(sha256) 密码哈希，格式 'pbkdf2_sha256$rounds$salt_hex$hash_hex'。"""
    if salt is None:
        salt = os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt,
                             _PBKDF2_ROUNDS)
    return f"pbkdf2_sha256${_PBKDF2_ROUNDS}${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    """恒定时间校验密码是否匹配存储的哈希。格式非法时返回 False。"""
    try:
        algo, rounds_s, salt_hex, hash_hex = stored.split("$")
        if algo != "pbkdf2_sha256":
            return False
        rounds = int(rounds_s)
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(hash_hex)
    except (ValueError, AttributeError):
        return False
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, rounds)
    return hmac.compare_digest(dk, expected)


class AuthMiddleware:
    """API key + JWT authentication.

    - Bearer <jwt>            → JWT 校验（HS256，需配置 JWT_SECRET 且安装 PyJWT）
    - Bearer <api_key>        → API key 校验（JWT 不匹配时回退）
    - X-API-Key: <api_key>    → API key 校验
    - ?api_key=<api_key>      → 兼容旧客户端（urllib.parse.parse_qs 解析）
    """

    JWT_ALGORITHM = "HS256"

    def __init__(self, api_key: Optional[str] = None):
        self.api_key = api_key or os.getenv("API_KEY", "")

    # ── 主校验入口 ────────────────────────────────────

    @classmethod
    def check_api_key(cls, request_handler) -> bool:
        """Check API key / JWT from header or query parameter.

        成功时在 request_handler 上设置：
            auth_scheme    "jwt" | "api_key"
            auth_subject   JWT sub or a one-way API-key fingerprint
            auth_tenant_id JWT tenant_id or "default"

        Args:
            request_handler: 需有 .headers (mapping) 与 .path (str，可为完整 URL)

        Returns:
            True if authenticated, False otherwise
        """
        cls._set_auth_context(request_handler, "", "", "")

        # 1) Authorization: Bearer <token>
        auth_header = request_handler.headers.get("Authorization", "") or ""
        if auth_header.startswith("Bearer "):
            token = auth_header[7:].strip()
            claims = cls._decode_jwt(token)
            if claims is not None:
                cls._set_auth_context(
                    request_handler, "jwt",
                    str(claims.get("sub", "")),
                    str(claims.get("tenant_id", "default")) or "default")
                return True
            if cls._validate_key(token):
                cls._set_auth_context(request_handler, "api_key", cls._api_key_subject(token), "default")
                return True

        # 2) X-API-Key header
        api_key_header = request_handler.headers.get("X-API-Key", "") or ""
        if api_key_header and cls._validate_key(api_key_header):
            cls._set_auth_context(request_handler, "api_key", cls._api_key_subject(api_key_header), "default")
            return True

        # 3) Query parameter（向后兼容）。parse_qs 正确处理 URL 编码、
        #    重复参数与值内含 '=' 的情况（旧版手写 split("=") 会 ValueError）。
        path = getattr(request_handler, "path", "") or ""
        query = urllib.parse.urlparse(path).query
        if query:
            params = urllib.parse.parse_qs(query, keep_blank_values=False)
            for key in params.get("api_key", []):
                if key and cls._validate_key(key):
                    cls._set_auth_context(request_handler, "api_key", cls._api_key_subject(key), "default")
                    return True

        return False

    @staticmethod
    def _api_key_subject(api_key: str) -> str:
        """Return an opaque stable subject for a validated API key.

        The raw key is never placed in request state, logs, session ownership,
        or database records.  A per-key subject prevents all valid API keys
        from collapsing into the old shared ``api-key`` identity.
        """
        digest = hashlib.sha256(api_key.encode("utf-8")).hexdigest()[:24]
        return f"api-{digest}"

    @staticmethod
    def _set_auth_context(request_handler, scheme: str, subject: str, tenant_id: str) -> None:
        try:
            request_handler.auth_scheme = scheme
            request_handler.auth_subject = subject
            request_handler.auth_tenant_id = tenant_id
        except AttributeError:
            # 只读对象（如某些 handler）：跳过属性写入，不影响布尔返回值
            pass

    # ── JWT ──────────────────────────────────────────

    @classmethod
    def create_access_token(cls, subject: str, tenant_id: str = "default",
                            ttl_seconds: Optional[int] = None) -> str:
        """签发短时效 JWT access token（app_fastapi.py /api/auth/token 调用）。

        Raises:
            ValueError: JWT_SECRET 未配置或 PyJWT 未安装（上层转 503）。
        """
        # 统一走模块级 create_access_token（PyJWT→stdlib 降级已内建）。
        return create_access_token(
            subject, tenant=tenant_id or "default", ttl=ttl_seconds)

    @classmethod
    def _decode_jwt(cls, token: str) -> Optional[dict]:
        """解码并校验 JWT；无效/未配置时返回 None（回退 API key 校验）。"""
        try:
            return verify_token(token)
        except Exception:
            return None

    # ── API key ──────────────────────────────────────

    @staticmethod
    def _validate_key(api_key: str) -> bool:
        """Validate API key against stored keys (comma-separated in API_KEYS env).

        Uses constant-time comparison (hmac.compare_digest) to prevent timing
        attacks that could leak key material through response-time variations.
        """
        valid_keys = [k.strip() for k in os.getenv("API_KEYS", "").split(",") if k.strip()]
        if not api_key or not valid_keys:
            return False
        # Constant-time comparison: iterate all keys to avoid early-return timing leak
        for valid_key in valid_keys:
            if hmac.compare_digest(api_key, valid_key):
                return True
        return False

    @staticmethod
    def is_public_endpoint(path: str) -> bool:
        """Check if endpoint should be publicly accessible."""
        # 本地开发模式：API_KEYS 为空时所有端点公开
        api_keys = os.getenv("API_KEYS", "").strip()
        if not api_keys:
            return True
        # path 可能是完整 URL（app_fastapi 传 str(request.url)），统一取 path 部分
        pure_path = urllib.parse.urlparse(path).path or path
        public_paths = ["/", "/index.html", "/health", "/api/health", "/api/ready", "/healthz", "/api/metrics",
                        "/api/auth/login", "/api/auth/register", "/api/auth/token",
                        "/api/auth/refresh", "/api/auth/logout", "/api/auth/me"]
        return pure_path in public_paths or pure_path.startswith("/static/")


def require_auth(handler_method):
    """Decorator to require API key authentication.

    Usage:
        @require_auth
        def do_POST(self):
            ...
    """
    def wrapper(self, *args, **kwargs):
        if not AuthMiddleware.is_public_endpoint(self.path):
            if not AuthMiddleware.check_api_key(self):
                self.send_response(401)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"error": "Unauthorized: Invalid or missing API key"}')
                return
        return handler_method(self, *args, **kwargs)
    return wrapper
