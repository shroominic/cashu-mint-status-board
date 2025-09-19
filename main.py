import asyncio
import time
import contextlib
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, cast
from decimal import Decimal, ROUND_HALF_UP

import httpx
from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from sqlmodel import SQLModel, Field, Session, create_engine, select
from contextlib import asynccontextmanager

from nostr import stream_nostr
from lightning import probe_lightning


DB_URL = "sqlite:///./mint_status.db"
engine = create_engine(DB_URL, connect_args={"check_same_thread": False})


class Mint(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    url: str = Field(index=True)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class HealthCheck(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    mint_id: int = Field(foreign_key="mint.id", index=True)
    status: bool
    response_ms: int | None = None
    checked_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class LightningSnapshot(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    mint_id: int = Field(foreign_key="mint.id", index=True)
    invoice: str | None = None
    payee_pubkey: str | None = Field(default=None, index=True)
    node_name: str | None = None
    node_capacity_sats: int | None = None
    checked_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class MintListing(BaseModel):
    id: str
    pubkey: str
    created_at: int
    kind: int
    tags: list[list[str]]
    content: str
    sig: str
    relay: str


def create_db() -> None:
    SQLModel.metadata.create_all(engine)


async def fetch_nostr_mints() -> list[MintListing]:
    def _parse(event: dict[str, Any]) -> MintListing | None:
        try:
            return MintListing.model_validate(event)
        except Exception:
            return None

    return [
        mint
        async for event in stream_nostr(
            [
                "wss://relay.damus.io",
                "wss://relay.snort.social",
                "wss://relay.nostr.band",
                "wss://relay.primal.net",
                "wss://nos.lol",
            ],
            [{"kinds": [38172]}],
            stop_after_idle=1,
        )
        if (mint := _parse(event)) is not None
    ]


def extract_url(mint: MintListing) -> str | None:
    return next((tag[1] for tag in mint.tags if len(tag) >= 2 and tag[0] == "u"), None)


async def discover_mint_urls() -> list[str]:
    mints = await fetch_nostr_mints()
    urls = [u for m in mints if (u := extract_url(m))]
    return sorted(set(urls))


async def http_health(
    url: str, client: httpx.AsyncClient
) -> tuple[str, bool, int | None]:
    start = time.perf_counter()
    try:
        r = await client.get(f"{url.rstrip('/')}/v1/info")
        elapsed_ms = int((time.perf_counter() - start) * 1000)
        return url, r.status_code == 200, elapsed_ms
    except Exception:
        return url, False, None


def ensure_mints(session: Session, urls: list[str]) -> dict[str, int]:
    existing = session.exec(select(Mint).where(cast(Any, Mint.url).in_(urls))).all()
    known = {m.url: m.id for m in existing if m.id is not None}
    for url in urls:
        if url not in known:
            m = Mint(url=url)
            session.add(m)
            session.commit()
            session.refresh(m)
            known[url] = int(m.id)  # type: ignore[arg-type]
    return known


async def monitor_loop(stop: asyncio.Event) -> None:
    while not stop.is_set():
        urls = await discover_mint_urls()
        async with httpx.AsyncClient(timeout=5.0) as client:
            results = await asyncio.gather(*[http_health(u, client) for u in urls])
        with Session(engine) as s:
            url_to_id = ensure_mints(s, [u for u, _, _ in results])
            for url, ok, ms in results:
                s.add(HealthCheck(mint_id=url_to_id[url], status=ok, response_ms=ms))
            s.commit()
        try:
            await asyncio.wait_for(stop.wait(), timeout=60.0)
        except asyncio.TimeoutError:
            pass


async def lightning_loop(stop: asyncio.Event) -> None:
    while not stop.is_set():
        urls = await discover_mint_urls()
        active_urls: list[str] = []
        with Session(engine) as s:
            url_to_id = ensure_mints(s, urls)
            for url, mint_id in url_to_id.items():
                last = s.exec(
                    select(HealthCheck)
                    .where(HealthCheck.mint_id == mint_id)
                    .order_by(cast(Any, HealthCheck.checked_at).desc())
                    .limit(1)
                ).first()
                if last and last.status:
                    active_urls.append(url)
        if not active_urls:
            try:
                await asyncio.wait_for(stop.wait(), timeout=15 * 60.0)
            except asyncio.TimeoutError:
                pass
            continue
        async with httpx.AsyncClient(timeout=15.0) as client:
            probes = await asyncio.gather(
                *[probe_lightning(u, client) for u in active_urls]
            )
        with Session(engine) as s:
            url_to_id = ensure_mints(s, active_urls)
            for url, res in zip(active_urls, probes):
                s.add(
                    LightningSnapshot(
                        mint_id=url_to_id[url],
                        invoice=res.invoice,
                        payee_pubkey=res.payee_pubkey,
                        node_name=res.node_name,
                        node_capacity_sats=res.node_capacity_sats,
                    )
                )
            s.commit()
        try:
            await asyncio.wait_for(stop.wait(), timeout=15 * 60.0)
        except asyncio.TimeoutError:
            pass


def render_sparkline(statuses: list[bool], total: int = 50) -> str:
    cells = statuses[:total]
    cells = cells + [False] * (total - len(cells))
    colors = ["#16a34a" if s else "#ef4444" for s in cells]
    return "".join(f'<span class="cell" style="background:{c}"></span>' for c in colors)


@dataclass
class MintRow:
    url: str
    uptime_24h: str
    last_seen: str
    checks_html: str
    ln_pubkey: str | None
    ln_name: str | None
    ln_capacity: str | None
    avg_latency_ms: int | None
    is_up: bool
    uptime_class: str
    latency_class: str


def compute_rows() -> list[MintRow]:
    now = datetime.now(timezone.utc)
    since_24h = now - timedelta(hours=24)
    rows_with_metrics: list[tuple[int, float, int, MintRow]] = []
    with Session(engine) as s:
        mints = s.exec(select(Mint).order_by(Mint.url)).all()
        for m in mints:
            recent = s.exec(
                select(HealthCheck)
                .where(HealthCheck.mint_id == m.id)
                .order_by(cast(Any, HealthCheck.checked_at).desc())
                .limit(500)
            ).all()
            last_time = recent[0].checked_at if recent else None
            if last_time and last_time.tzinfo is None:
                last_time = last_time.replace(tzinfo=timezone.utc)
            last_seen = (
                f"{max(0, int((now - last_time).total_seconds()))}s"
                if last_time
                else "-"
            )
            last50 = [hc.status for hc in recent]
            is_up = bool(recent and recent[0].status)
            lns = s.exec(
                select(LightningSnapshot)
                .where(LightningSnapshot.mint_id == m.id)
                .order_by(cast(Any, LightningSnapshot.checked_at).desc())
                .limit(1)
            ).first()
            pubkey = lns.payee_pubkey if lns else None
            node_name = lns.node_name if lns else None
            cap = lns.node_capacity_sats if lns else None
            cap_str = None
            if cap is not None:
                btc = Decimal(cap) / Decimal(100_000_000)
                if btc >= Decimal(10):
                    decimals, q = 1, Decimal("0.1")
                elif btc >= Decimal(1):
                    decimals, q = 2, Decimal("0.01")
                else:
                    decimals, q = 4, Decimal("0.0001")
                value = btc.quantize(q, rounding=ROUND_HALF_UP)
                cap_str = f"{value:,.{decimals}f} BTC"
            since = s.exec(
                select(HealthCheck).where(
                    HealthCheck.mint_id == m.id,
                    HealthCheck.checked_at >= since_24h,
                )
            ).all()
            up = sum(1 for hc in since if hc.status)
            total = len(since)
            avg_samples = [
                hc.response_ms
                for hc in since
                if hc.status and hc.response_ms is not None
            ]
            avg_latency = (
                int(sum(avg_samples) / len(avg_samples)) if avg_samples else None
            )
            uptime_ratio = (up / total) if total else -1.0
            uptime_class = (
                "none"
                if total == 0
                else (
                    "good"
                    if uptime_ratio >= 0.995
                    else ("warn" if uptime_ratio >= 0.97 else "bad")
                )
            )
            latency_class = (
                "none"
                if avg_latency is None
                else (
                    "fast"
                    if avg_latency <= 300
                    else ("ok" if avg_latency <= 1000 else "slow")
                )
            )
            row = MintRow(
                url=m.url,
                uptime_24h=(f"{(uptime_ratio*100):.0f}%" if total else "-"),
                last_seen=last_seen,
                checks_html=render_sparkline(last50),
                ln_pubkey=pubkey,
                ln_name=node_name,
                ln_capacity=cap_str,
                avg_latency_ms=avg_latency,
                is_up=is_up,
                uptime_class=uptime_class,
                latency_class=latency_class,
            )
            cap_num = cap if cap is not None else -1
            lat_sort = avg_latency if avg_latency is not None else 1_000_000_000
            rows_with_metrics.append((cap_num, uptime_ratio, lat_sort, row))
    rows_with_metrics.sort(key=lambda t: (-t[0], -t[1], t[2]))
    return [r for _, _, _, r in rows_with_metrics]


def render_tbody() -> str:
    rows = compute_rows()
    with_cap = [r for r in rows if r.ln_capacity is not None]
    no_cap = [r for r in rows if r.ln_capacity is None]

    def row_html(r: MintRow, extra_class: str = "") -> str:
        classes = (
            f"{'up' if r.is_up else 'down'}{(' ' + extra_class) if extra_class else ''}"
        )
        return (
            f"<tr class={classes}>"
            f"<td class=mint><span class=\"status-dot {'up' if r.is_up else 'down'}\" title=\"{'Up' if r.is_up else 'Down'}\"></span><a class=link href=\"{r.url}\" target=\"_blank\" rel=\"noopener\">{r.url.replace('https://','').replace('http://','')}</a></td>"
            f"<td><span class=\"badge uptime {r.uptime_class}\" title=\"Uptime last 24h\">{r.uptime_24h}</span></td>"
            f"<td><div class=spark>{r.checks_html}</div></td>"
            f"<td class=mono>{(f'<a class=\'link\' href=\'https://1ml.com/node/{r.ln_pubkey}\' target=\'_blank\' rel=\'noopener\'>{r.ln_name or r.ln_pubkey}</a>') if r.ln_pubkey else '-'}</td>"
            f"<td>{(f'<span class=\'badge cap\'>{r.ln_capacity}</span>') if r.ln_capacity else '-'}</td>"
            f"<td>{(f'<span class=\'badge latency {r.latency_class}\'>{r.avg_latency_ms} ms</span>') if r.avg_latency_ms is not None else '-'}</td>"
            f"</tr>"
        )

    body_main = "".join(row_html(r) for r in with_cap)
    body_no_cap = "".join(row_html(r, "no-cap") for r in no_cap)
    divider = (
        '<tr class="section-divider"><td colspan="6"><span>Node not found</span></td></tr>'
        if body_no_cap
        else ""
    )
    return f"<tbody id=dashboard>{body_main}{divider}{body_no_cap}</tbody>"


def render_table() -> str:
    tbody = render_tbody()
    return (
        "<table class=card>"
        "<thead><tr><th>Mint</th><th>Uptime (24h)</th><th>Last hour</th><th>LN Node</th><th>LN Capacity (BTC)</th><th>Latency</th></tr></thead>"
        f"{tbody}"
        "</table>"
    )


def render_index() -> str:
    styles = """
    :root{color-scheme:dark;--bg:#0a0b0f;--surface:#101216;--surface-2:#0f1114;--border:#1f2937;--text:#e5e7eb;--muted:#9ca3af;--accent:#60a5fa;--green:#16a34a;--red:#ef4444;--amber:#f59e0b}
    *{box-sizing:border-box}
    body{margin:0;font-family:ui-sans-serif,system-ui,-apple-system,Segoe UI,Roboto,Ubuntu,Cantarell,Noto Sans,sans-serif;line-height:1.5;background:var(--bg);color:var(--text)}
    header{padding:24px 16px 12px;max-width:1100px;margin:0 auto}
    h1{margin:0;font-size:24px;font-weight:700;letter-spacing:.2px}
    .sub{margin-top:6px;color:var(--muted);font-size:13px}
    main{padding:8px 16px 44px;max-width:1100px;margin:0 auto}
    .card{width:100%;border-collapse:separate;border-spacing:0;background:linear-gradient(180deg,var(--surface),var(--surface-2));border:1px solid var(--border);border-radius:14px;overflow:hidden;box-shadow:0 10px 30px #00000055}
    th,td{padding:13px 14px;border-bottom:1px solid var(--border);text-align:left;font-size:14px}
    thead th{position:sticky;top:0;background:#0c0f13;z-index:1;font-weight:600;color:#cbd5e1}
    tbody tr{transition:background .15s ease}
    tbody tr:hover{background:#0f1217}
    tbody tr:last-child td{border-bottom:none}
    tbody tr.up td.mint .status-dot{background:linear-gradient(180deg,#10b981,#16a34a);box-shadow:0 0 0 2px #0b0f14,0 0 12px #10b98155}
    tbody tr.down td.mint .status-dot{background:linear-gradient(180deg,#ef4444,#dc2626);box-shadow:0 0 0 2px #0b0f14,0 0 10px #ef444455}
    tbody tr.no-cap{opacity:.55}
    tr.section-divider td{background:#0c0f13;border-top:2px solid #0b0e12;border-bottom:2px solid #0b0e12;color:#94a3b8;font-weight:600}
    tr.section-divider td span{display:inline-block;padding:8px 6px}
    .spark{display:grid;grid-template-columns:repeat(50,minmax(2px,1fr));gap:2px}
    .cell{display:block;aspect-ratio:1/1;border-radius:2px;opacity:.95}
    .mono{font-family:ui-monospace,SFMono-Regular,Menlo,Monaco,Consolas,monospace;font-size:12px}
    .muted{color:var(--muted)}
    .mint{white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:0;min-width:280px}
    .mint .status-dot{display:inline-block;width:10px;height:10px;border-radius:999px;margin-right:10px;vertical-align:middle}
    .badge{display:inline-block;padding:4px 8px;border-radius:999px;font-size:12px;font-weight:600;letter-spacing:.2px}
    .badge.uptime.none{background:#0f1318;color:#64748b}
    .badge.uptime.good{background:#052e1a;color:#22c55e}
    .badge.uptime.warn{background:#2a2105;color:#eab308}
    .badge.uptime.bad{background:#2d0c0c;color:#f87171}
    .badge.latency.none{background:#0f1318;color:#64748b}
    .badge.latency.fast{background:#062b1a;color:#16a34a}
    .badge.latency.ok{background:#2a2105;color:#f59e0b}
    .badge.latency.slow{background:#2d0c0c;color:#f87171}
    .badge.cap{background:#0f1318;color:#93c5fd}
    a{color:var(--accent);text-decoration:none}
    a:hover{text-decoration:underline}
    footer{max-width:1100px;margin:12px auto 0;padding:0 16px;color:var(--muted);font-size:12px}
    #meta{display:flex;gap:14px;align-items:center;margin-top:8px;color:var(--muted);font-size:12px}
    #last-updated{font-variant-numeric:tabular-nums}
    """.strip()
    return (
        "<!doctype html><html><head><meta charset=utf-8>"
        '<meta name=viewport content="width=device-width, initial-scale=1">'
        '<meta name="theme-color" content="#0a0b0f">'
        "<title>Cashu Mint Status</title>"
        f"<style>{styles}</style>"
        '<script src="https://unpkg.com/htmx.org@2.0.2" integrity="sha384-7Y/OLJm7GG4l7uYf4x2nY2hVqXzjP4uYbUhg0oMiJ2z2hQ0zDgANbHgxqCwR8K8y" crossorigin="anonymous"></script>'
        "</head><body>"
        "<header><h1>Cashu Mint Status Board</h1><div id=meta><span class=muted>Auto-refresh every 10s</span><span class=muted>•</span><span class=muted>Last updated: <strong id=last-updated>-</strong></span></div></header>"
        "<main>"
        '<div hx-get="/dashboard" hx-trigger="load, every 10s" hx-target="#dashboard" hx-swap="outerHTML">'
        f"{render_table()}"
        "</div>"
        "</main>"
        "<script>document.addEventListener('htmx:afterSwap',function(e){if(e.detail && e.detail.target && e.detail.target.id==='dashboard'){var el=document.getElementById('last-updated');if(el){var d=new Date();el.textContent=d.toLocaleTimeString();}}});</script>"
        "</body></html>"
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    create_db()
    stop_event: asyncio.Event = asyncio.Event()
    monitor_task: asyncio.Task[Any] = asyncio.create_task(monitor_loop(stop_event))
    lnd_task: asyncio.Task[Any] = asyncio.create_task(lightning_loop(stop_event))
    app.state.stop_event = stop_event
    app.state.monitor_task = monitor_task
    app.state.lnd_task = lnd_task
    try:
        yield
    finally:
        stop_event.set()
        monitor_task.cancel()
        lnd_task.cancel()
        with contextlib.suppress(Exception):
            await monitor_task
        with contextlib.suppress(Exception):
            await lnd_task


app = FastAPI(lifespan=lifespan)


@app.get("/", response_class=HTMLResponse)
async def index() -> str:
    return render_index()


@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard() -> str:
    return render_tbody()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=False)
