import asyncio
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
    node_channel_count: int | None


async def fetch_invoice_request(
    mint_url: str, client: httpx.AsyncClient
) -> tuple[str | None, dict[str, Any] | None]:
    u = mint_url.rstrip("/")
    if u.startswith("https://"):
        u = u[8:]
    elif u.startswith("http://"):
        u = u[7:]

    targets = [f"https://{u}", f"http://{u}"]

    for base in targets:
        endpoint = f"{base}/v1/mint/quote/bolt11"
        try:
            r = await client.post(endpoint, json={"unit": "sat", "amount": 100})
            if r.status_code == 200:
                data = r.json()
                invoice = data.get("request") or data.get("bolt11")
                return (invoice if isinstance(invoice, str) else None), data
        except Exception:
            pass

    return None, None


def decode_invoice_pubkey(invoice: str) -> str | None:
    try:
        parsed = decode_bolt11(invoice)
        payee = getattr(parsed, "payee", None)
        return payee if isinstance(payee, str) else None
    except Exception:
        return None


async def fetch_node_metadata(
    pubkey: str, client: httpx.AsyncClient
) -> tuple[str | None, int | None, int | None]:
    try:
        r = await client.get(f"https://1ml.com/node/{pubkey}/json")
        if r.status_code != 200:
            return None, None, None
        data = r.json()
        alias_val = data.get("alias")
        alias: str | None = alias_val if isinstance(alias_val, str) else None
        cap_val = data.get("capacity")
        capacity: int | None = (
            int(cap_val)
            if isinstance(cap_val, (int, str)) and str(cap_val).isdigit()
            else None
        )
        ch_val = data.get("channelcount")
        channels: int | None = (
            int(ch_val)
            if isinstance(ch_val, (int, str)) and str(ch_val).isdigit()
            else None
        )
        return alias, capacity, channels
    except Exception:
        return None, None, None


async def probe_lightning(
    mint_url: str, client: httpx.AsyncClient
) -> LightningProbeResult:
    invoice, _ = await fetch_invoice_request(mint_url, client)
    pubkey = decode_invoice_pubkey(invoice) if invoice else None
    name, capacity, channels = (
        await fetch_node_metadata(pubkey, client) if pubkey else (None, None, None)
    )
    return LightningProbeResult(
        invoice=invoice,
        payee_pubkey=pubkey,
        node_name=name,
        node_capacity_sats=capacity,
        node_channel_count=channels,
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
