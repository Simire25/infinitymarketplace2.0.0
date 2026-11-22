"""Paystack integration (standard library only, no extra packages).

Flow: the server creates a transaction (amount in kobo) and sends the customer
to Paystack's checkout page. After payment, Paystack calls our callback URL and
our webhook. Both paths re-verify the transaction with Paystack's API before an
order is marked as paid. The browser is never trusted to report success.
"""
import hashlib
import hmac
import json
import urllib.error
import urllib.parse
import urllib.request

PAYSTACK_BASE_URL = "https://api.paystack.co"


class PaystackError(Exception):
    """Raised when Paystack is unavailable, misconfigured or rejects a request."""


class Paystack:
    def __init__(self, secret_key, timeout=15):
        self.secret_key = secret_key or ""
        self.timeout = timeout

    @property
    def configured(self):
        return bool(self.secret_key)

    def _request(self, method, path, payload=None):
        if not self.configured:
            raise PaystackError("Online payment is not configured yet.")
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        req = urllib.request.Request(
            PAYSTACK_BASE_URL + path,
            data=data,
            method=method,
            headers={
                "Authorization": f"Bearer {self.secret_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                body = json.loads(resp.read().decode("utf-8") or "{}")
        except urllib.error.HTTPError as exc:
            try:
                body = json.loads(exc.read().decode("utf-8") or "{}")
            except ValueError:
                body = {}
            raise PaystackError(body.get("message") or f"Paystack returned HTTP {exc.code}.")
        except (urllib.error.URLError, TimeoutError, ValueError):
            raise PaystackError("Could not reach Paystack. Please try again.")

        if not body.get("status"):
            raise PaystackError(body.get("message") or "Paystack request failed.")
        return body

    def initialize(self, email, amount_naira, reference, callback_url, metadata=None):
        """Create a transaction. Returns Paystack's data (includes authorization_url)."""
        body = self._request("POST", "/transaction/initialize", {
            "email": email,
            "amount": int(amount_naira) * 100,  # Paystack expects kobo
            "currency": "NGN",
            "reference": reference,
            "callback_url": callback_url,
            "metadata": metadata or {},
        })
        return body["data"]

    def verify(self, reference):
        """Ask Paystack for the true status of a transaction."""
        body = self._request("GET", "/transaction/verify/" + urllib.parse.quote(reference, safe=""))
        return body["data"]

    @staticmethod
    def signature_is_valid(secret_key, raw_body, signature):
        """Webhook check: HMAC-SHA512 of the raw body must equal x-paystack-signature."""
        if not secret_key or not signature:
            return False
        expected = hmac.new(secret_key.encode("utf-8"), raw_body, hashlib.sha512).hexdigest()
        return hmac.compare_digest(expected, signature)
