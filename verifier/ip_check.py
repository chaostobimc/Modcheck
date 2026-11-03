"""
ip_check.py – IP-Informationen holen via proxycheck.io
"""

import os
import aiohttp
from dataclasses import dataclass

PROXYCHECK_API_KEY = os.getenv("PROXYCHECK_API_KEY", "")


@dataclass
class IPInfo:
    ip:           str
    country:      str = ""
    country_code: str = ""
    city:         str = ""
    isp:          str = ""
    is_vpn:       bool = False
    is_proxy:     bool = False
    risk:         int  = 0
    raw:          dict = None

    def __post_init__(self):
        if self.raw is None:
            self.raw = {}


async def check_ip(ip: str) -> IPInfo:
    """
    Fragt proxycheck.io nach Infos zur IP.
    Gibt immer ein IPInfo zurück, auch wenn die API nicht erreichbar ist.
    """
    # Lokale / Private IPs überspringen
    if _is_local_ip(ip):
        return IPInfo(ip=ip, country="Local", country_code="LO", isp="Local Network")

    if not PROXYCHECK_API_KEY:
        return IPInfo(ip=ip)

    try:
        url = (
            f"https://proxycheck.io/v2/{ip}"
            f"?key={PROXYCHECK_API_KEY}"
            f"&vpn=1&asn=1&risk=1&port=1&seen=1"
        )
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=8)) as resp:
                if resp.status != 200:
                    return IPInfo(ip=ip)

                data = await resp.json()

                if data.get("status") != "ok":
                    return IPInfo(ip=ip)

                ip_data = data.get(ip, {})

                proxy_val = ip_data.get("proxy", "no")
                vpn_val   = ip_data.get("type", "")

                is_proxy = proxy_val == "yes"
                is_vpn   = vpn_val.lower() in ("vpn", "tor", "dchost", "hosting")

                return IPInfo(
                    ip=ip,
                    country=ip_data.get("country", ""),
                    country_code=ip_data.get("isocode", ""),
                    city=ip_data.get("city", ""),
                    isp=ip_data.get("provider", ip_data.get("organisation", "")),
                    is_vpn=is_vpn,
                    is_proxy=is_proxy,
                    risk=int(ip_data.get("risk", 0)),
                    raw=ip_data,
                )

    except Exception as e:
        print(f"⚠️ [IPCheck] Fehler bei {ip}: {e}")
        return IPInfo(ip=ip)


def get_real_ip(request) -> str:
    """
    Extrahiert die echte Client-IP aus dem Request.
    Berücksichtigt ngrok, Cloudflare, nginx-Proxies.
    """
    from fastapi import Request

    # Reihenfolge: Cloudflare → ngrok/forwarded → direkt
    headers_to_check = [
        "cf-connecting-ip",       # Cloudflare – immer die echte IP
        "x-forwarded-for",        # Standard-Proxy (ngrok, nginx, etc.)
        "x-real-ip",              # nginx
        "forwarded",              # RFC 7239
    ]

    for header in headers_to_check:
        value = request.headers.get(header)
        if value:
            # x-forwarded-for kann mehrere IPs enthalten: "client, proxy1, proxy2"
            # Die erste ist die echte Client-IP
            ip = value.split(",")[0].strip()
            if ip and not _is_trusted_proxy(ip):
                return ip
            elif ip:
                # Wenn die erste IP ein vertrauenswürdiger Proxy ist,
                # nehmen wir die zweite (die echte)
                parts = value.split(",")
                if len(parts) > 1:
                    return parts[1].strip()

    # Fallback: direkte Verbindungs-IP
    if hasattr(request, "client") and request.client:
        return request.client.host

    return "unknown"


def _is_trusted_proxy(ip: str) -> bool:
    """Gibt True zurück wenn die IP ein bekannter Proxy/LB ist (kein echter Client)."""
    trusted_ranges = [
        "127.", "10.", "192.168.", "172.16.", "172.17.",
        "172.18.", "172.19.", "172.20.", "172.21.", "172.22.",
        "172.23.", "172.24.", "172.25.", "172.26.", "172.27.",
        "172.28.", "172.29.", "172.30.", "172.31.",
        # Cloudflare IPs (vereinfacht)
        "103.21.", "103.22.", "103.31.", "104.16.", "104.17.",
        "108.162.", "141.101.", "162.158.", "172.64.", "172.65.",
        "172.66.", "172.67.", "172.68.", "172.69.", "172.70.",
        "172.71.", "188.114.", "190.93.", "197.234.", "198.41.",
    ]
    return any(ip.startswith(r) for r in trusted_ranges)


def _is_local_ip(ip: str) -> bool:
    return (
        ip.startswith("127.")
        or ip.startswith("10.")
        or ip.startswith("192.168.")
        or ip == "::1"
        or ip == "localhost"
        or ip == "unknown"
    )
