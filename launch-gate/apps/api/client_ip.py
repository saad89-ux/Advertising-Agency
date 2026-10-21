"""Uploader IP identity, trusting forwarded chains only from configured proxy networks."""
from __future__ import annotations
import ipaddress
import os
from typing import Any


def _address(value):
    address = ipaddress.ip_address(value)
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
        address = address.ipv4_mapped
    return address


def get_client_ip(request: Any) -> str:
    peer = request.client.host if request.client else "127.0.0.1"
    if peer == "localhost":
        peer = "127.0.0.1"
    try:
        address = _address(peer)
    except ValueError:
        return peer
    networks = []
    for value in os.getenv("TRUSTED_PROXIES", "").split(","):
        try:
            networks.append(ipaddress.ip_network(value.strip(), strict=False))
        except ValueError:
            continue
    def trusted(item):
        return any(item in network for network in networks)
    if trusted(address):
        forwarded = request.headers.get("x-forwarded-for") or request.headers.get("x-real-ip", "")
        for value in reversed(forwarded.split(",")):
            if not trusted(address):
                break
            try:
                address = _address(value.strip())
            except ValueError:
                return str(_address(peer))
    return str(address)
