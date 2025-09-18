import asyncio
import re
from dataclasses import dataclass
from typing import Any

import httpx
from bolt11 import decode as decode_bolt11


@dataclass
class LightningProbeResult:
    invoice: str | None
    payee_pubkey: str | None
    node_name: str | None
    node_capacity_sats: int | None


async def fetch_invoice_request(
    mint_url: str, client: httpx.AsyncClient
) -> tuple[str | None, dict[str, Any] | None]:
    endpoint = f"{mint_url.rstrip('/')}/v1/mint/quote/bolt11"
    try:
        r = await client.post(endpoint, json={"unit": "sat", "amount": 100})
        if r.status_code != 200:
            return None, None
        data = r.json()
        invoice = data.get("request") or data.get("bolt11")
        return (invoice if isinstance(invoice, str) else None), data
    except Exception:
        return None, None


def decode_invoice_pubkey(invoice: str) -> str | None:
    try:
        parsed = decode_bolt11(invoice)
        payee = getattr(parsed, "payee", None)
        return payee if isinstance(payee, str) else None
    except Exception:
        return None


def _parse_capacity_sats_from_html(html: str) -> int | None:
    for m in re.finditer(r"([0-9][0-9,._ ]{0,20})\s*sats", html, flags=re.IGNORECASE):
        digits = re.sub(r"[^0-9]", "", m.group(1))
        if digits:
            value = int(digits)
            if value > 1000:
                return value
    m_btc = re.search(r"([0-9]+(?:\.[0-9]{1,8})?)\s*BTC", html, flags=re.IGNORECASE)
    if m_btc:
        return int(float(m_btc.group(1)) * 100_000_000)
    return None


def _parse_node_name_from_html(html: str) -> str | None:
    m = re.search(r"\"alias\"\s*:\s*\"([^\"]+)\"", html)
    if m:
        return m.group(1)
    m2 = re.search(
        r"<title>\s*(.*?)\s*[\-\u2013\u2014]\s*1ML", html, flags=re.IGNORECASE
    )
    if m2:
        return re.sub(r"\s+", " ", m2.group(1)).strip()
    m3 = re.search(
        r"Alias\s*</td>\s*<td[^>]*>\s*([^<]+)\s*</td>", html, flags=re.IGNORECASE
    )
    if m3:
        return m3.group(1).strip()
    return None


async def fetch_node_alias_and_capacity(
    pubkey: str, client: httpx.AsyncClient
) -> tuple[str | None, int | None]:
    try:
        r1 = await client.get(f"https://1ml.com/node/{pubkey}/channels?order=capacity")
        alias: str | None = None
        cap1: int | None = None
        if r1.status_code == 200:
            cap1 = _parse_capacity_sats_from_html(r1.text)
            alias = _parse_node_name_from_html(r1.text) or alias
        r2 = await client.get(f"https://1ml.com/node/{pubkey}")
        cap2: int | None = None
        if r2.status_code == 200:
            cap2 = _parse_capacity_sats_from_html(r2.text)
            alias = _parse_node_name_from_html(r2.text) or alias
        cap = cap1 if cap1 is not None else cap2
        return alias, cap
    except Exception:
        return None, None


async def probe_lightning(
    mint_url: str, client: httpx.AsyncClient
) -> LightningProbeResult:
    invoice, _ = await fetch_invoice_request(mint_url, client)
    pubkey = decode_invoice_pubkey(invoice) if invoice else None
    node_name, capacity = (
        await fetch_node_alias_and_capacity(pubkey, client) if pubkey else (None, None)
    )
    return LightningProbeResult(
        invoice=invoice,
        payee_pubkey=pubkey,
        node_name=node_name,
        node_capacity_sats=capacity,
    )


if __name__ == "__main__":
    import sys

    async def _main(urls: list[str]) -> None:
        async with httpx.AsyncClient(timeout=15.0) as client:
            for u in urls:
                res = await probe_lightning(u, client)
                print(u, res)

    argv = [a for a in sys.argv[1:] if a.strip()]
    if not argv:
        print("Usage: python lightning.py <mint_url> [<mint_url>...]")
    else:
        asyncio.run(_main(argv))
