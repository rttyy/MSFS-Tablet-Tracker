"""
Shared outbound-HTTPS helper.

Certificate stores differ wildly across machines:
- some Windows systems have an OUTDATED store (missing recent Let's
  Encrypt roots → "certificate has expired" on the VATSIM feed);
- others run HTTPS-inspecting proxies or antivirus (corporate MITM,
  Kaspersky…) whose certificate exists ONLY in the system store.

So every request first tries the up-to-date certifi bundle, and on a
certificate-verification failure transparently retries with the system
store. Works inside the PyInstaller executable too.
"""

from __future__ import annotations

import ssl
import urllib.request

try:
    import certifi
    _CERTIFI_CTX = ssl.create_default_context(cafile=certifi.where())
except Exception:
    _CERTIFI_CTX = None
_SYSTEM_CTX = ssl.create_default_context()


def urlopen(req, timeout: float = 15):
    """Drop-in urllib.request.urlopen with resilient certificate handling."""
    if _CERTIFI_CTX is not None:
        try:
            return urllib.request.urlopen(req, timeout=timeout,
                                          context=_CERTIFI_CTX)
        except ssl.SSLCertVerificationError:
            pass  # cert only known to the system store → retry below
        except OSError as e:  # URLError wrapping an SSL verification error
            if "CERTIFICATE_VERIFY_FAILED" not in str(e):
                raise
    return urllib.request.urlopen(req, timeout=timeout, context=_SYSTEM_CTX)
