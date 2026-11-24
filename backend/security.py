"""Security helpers: rate limiting, browser security headers, origin check, password rules.

The rate limiter keeps its counters in memory. That is correct for this project's deployment
(one gunicorn worker, see render.yaml). If you ever run several workers or servers, move the
counters to a shared store such as Redis, otherwise each worker counts separately.
"""
import threading
import time
from collections import defaultdict, deque

from flask import request

# Passwords that are far too common to allow, whatever their length.
COMMON_PASSWORDS = {
    "password", "password1", "password123", "12345678", "123456789", "1234567890", "qwertyuiop",
    "11111111", "00000000", "iloveyou", "abcd1234", "admin123", "letmein123", "welcome123",
}


def password_problem(password, email=""):
    """Return a message if the password is too weak, otherwise None."""
    if not isinstance(password, str) or len(password) < 8:
        return "Password must be at least 8 characters."
    if len(password) > 200:
        return "Password is too long."
    lowered = password.lower()
    if lowered in COMMON_PASSWORDS or len(set(lowered)) < 4:
        return "That password is too easy to guess. Choose something less common."
    if email and lowered == email.lower():
        return "Your password cannot be your email address."
    return None


class RateLimiter:
    """Sliding-window counter. `hit` returns 0 if allowed, otherwise the seconds to wait."""

    def __init__(self):
        self._events = defaultdict(deque)
        self._lock = threading.Lock()

    def hit(self, key, limit, window_seconds):
        now = time.monotonic()
        with self._lock:
            events = self._events[key]
            while events and now - events[0] > window_seconds:
                events.popleft()
            if len(events) >= limit:
                return max(1, int(window_seconds - (now - events[0])) + 1)
            events.append(now)
            if len(self._events) > 20000:  # keep memory bounded under a flood of distinct keys
                for stale in [k for k, v in self._events.items() if not v or now - v[-1] > window_seconds][:5000]:
                    self._events.pop(stale, None)
            return 0

    def peek(self, key, limit, window_seconds):
        """Like `hit` but does not count: returns the seconds to wait if the key is already blocked, else 0."""
        now = time.monotonic()
        with self._lock:
            events = self._events.get(key)
            if not events:
                return 0
            while events and now - events[0] > window_seconds:
                events.popleft()
            if len(events) >= limit:
                return max(1, int(window_seconds - (now - events[0])) + 1)
            return 0

    def reset(self):
        with self._lock:
            self._events.clear()


CSP = "; ".join([
    "default-src 'self'",
    "script-src 'self'",
    "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com",
    "font-src 'self' https://fonts.gstatic.com",
    "img-src 'self' data: https:",
    "connect-src 'self'",
    "form-action 'self'",
    "frame-ancestors 'none'",
    "base-uri 'self'",
    "object-src 'none'",
])


def apply_security_headers(response, secure):
    response.headers.setdefault("Content-Security-Policy", CSP)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=(), payment=()")
    if secure:
        response.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
    if request.path.startswith("/api/") or request.path.startswith("/admin"):
        # Never let a shared computer or a proxy keep order data or admin pages.
        response.headers["Cache-Control"] = "no-store"
    return response


def origin_is_same_site():
    """Defence in depth against cross-site form posts: if the browser says where a state-changing
    request came from, it must be this site. Requests with no Origin (curl, Paystack's webhook) pass."""
    origin = request.headers.get("Origin")
    if not origin:
        return True
    return origin.split("://", 1)[-1].rstrip("/") == request.host
