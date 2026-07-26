"""
API authentication middleware — JWT + API Key support.

Usage:
    from agent.auth import AuthMiddleware, require_auth
    # In your HTTP handler:
    if not AuthMiddleware.check_api_key(self):
        self.send_error(401)
        return
    # Or use decorator for specific methods
"""

import os
import time
from typing import Optional


class AuthMiddleware:
    """Simple API key authentication.
    
    Supports both header and query parameter auth.
    API keys are stored in environment variable or .env file.
    """
    
    def __init__(self, api_key: Optional[str] = None):
        self.api_key = api_key or os.getenv("API_KEY", "")
        
    @classmethod
    def check_api_key(cls, request_handler) -> bool:
        """Check API key from header or query parameter.
        
        Args:
            request_handler: HTTP request handler instance
            
        Returns:
            True if authenticated, False otherwise
        """
        # Check Authorization header (Bearer token)
        auth_header = request_handler.headers.get("Authorization", "")
        if auth_header.startswith("Bearer "):
            token = auth_header[7:]  # Remove 'Bearer ' prefix
            if cls._validate_key(token):
                return True
        
        # Check X-API-Key header
        api_key_header = request_handler.headers.get("X-API-Key", "")
        if api_key_header and cls._validate_key(api_key_header):
            return True
        
        # Check query parameter (for backward compatibility)
        if "?" in request_handler.path:
            query_params = dict(param.split("=") for param in request_handler.path.split("?")[1].split("&"))
            key = query_params.get("api_key", "")
            if key and cls._validate_key(key):
                return True
        
        return False
    
    @staticmethod
    def _validate_key(api_key: str) -> bool:
        """Validate API key against stored keys."""
        # Support multiple API keys (comma-separated in env var)
        valid_keys = [k.strip() for k in os.getenv("API_KEYS", "").split(",") if k.strip()]
        return api_key in valid_keys
    
    @staticmethod
    def is_public_endpoint(path: str) -> bool:
        """Check if endpoint should be publicly accessible."""
        # 本地开发模式：API_KEYS 为空时所有端点公开
        api_keys = os.getenv("API_KEYS", "").strip()
        if not api_keys:
            return True

        public_paths = ["/health", "/api/health", "/api/metrics"]
        return path in public_paths or path.startswith("/static/")


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
