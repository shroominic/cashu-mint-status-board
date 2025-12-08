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
from fastapi.staticfiles import StaticFiles
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
    currency_count: int | None = None
    # New fields for audit stats
    n_errors: int | None = None
    n_mints: int | None = None
    n_melts: int | None = None
    checked_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class LightningSnapshot(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    mint_id: int = Field(foreign_key="mint.id", index=True)
    invoice: str | None = None
    payee_pubkey: str | None = Field(default=None, index=True)
    node_name: str | None = None
    node_capacity_sats: int | None = None
    node_channel_count: int | None = None
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
    with engine.connect() as conn:
        cols = conn.exec_driver_sql("PRAGMA table_info('lightningsnapshot')").fetchall()
        names = {c[1] for c in cols}
        if "node_channel_count" not in names:
            conn.exec_driver_sql(
                "ALTER TABLE lightningsnapshot ADD COLUMN node_channel_count INTEGER"
            )

        cols_hc = conn.exec_driver_sql("PRAGMA table_info('healthcheck')").fetchall()
        names_hc = {c[1] for c in cols_hc}
        if "currency_count" not in names_hc:
            conn.exec_driver_sql(
                "ALTER TABLE healthcheck ADD COLUMN currency_count INTEGER"
            )
        if "n_errors" not in names_hc:
            conn.exec_driver_sql("ALTER TABLE healthcheck ADD COLUMN n_errors INTEGER")
        if "n_mints" not in names_hc:
            conn.exec_driver_sql("ALTER TABLE healthcheck ADD COLUMN n_mints INTEGER")
        if "n_melts" not in names_hc:
            conn.exec_driver_sql("ALTER TABLE healthcheck ADD COLUMN n_melts INTEGER")


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


async def fetch_audit_mints() -> list[dict[str, Any]]:
    mints_data = []
    skip = 0
    limit = 100
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            while True:
                r = await client.get(
                    f"https://api.audit.8333.space/mints/?skip={skip}&limit={limit}"
                )
                if r.status_code != 200:
                    break
                data = r.json()
                if not data:
                    break

                mints_data.extend(data)

                if len(data) < limit:
                    break
                skip += limit

                if skip > 10000:
                    break
    except Exception:
        pass
    return mints_data


def extract_url(mint: MintListing) -> str | None:
    return next((tag[1] for tag in mint.tags if len(tag) >= 2 and tag[0] == "u"), None)


async def discover_mint_urls() -> dict[str, dict[str, Any] | None]:
    nostr_task = asyncio.create_task(fetch_nostr_mints())
    audit_task = asyncio.create_task(fetch_audit_mints())

    nostr_mints, audit_data = await asyncio.gather(nostr_task, audit_task)

    # Map URL to extra data (or None)
    result: dict[str, dict[str, Any] | None] = {}

    # Process Nostr mints
    for m in nostr_mints:
        if u := extract_url(m):
            if u not in result:
                result[u] = None

    # Process Audit mints
    for m in audit_data:
        if isinstance(m, dict) and "url" in m:
            u = m["url"]
            # We merge/overwrite because audit data has stats we want
            # If we already have it from Nostr, we just attach the stats
            result[u] = m  # type: ignore[index]

    return result


async def http_health(
    url: str, client: httpx.AsyncClient
) -> tuple[str, bool, int | None, int | None]:
    start = time.perf_counter()
    try:
        r = await client.get(f"{url.rstrip('/')}/v1/info")
        elapsed_ms = int((time.perf_counter() - start) * 1000)
        if r.status_code == 200:
            currencies = 0
            try:
                data = r.json()
                nuts = data.get("nuts", {})
                if "4" in nuts and "methods" in nuts["4"]:
                    methods = nuts["4"]["methods"]
                    # count unique units
                    units = set()
                    for m in methods:
                        if isinstance(m, dict):
                            units.add(m.get("unit"))
                        elif isinstance(m, (list, tuple)) and len(m) >= 2:
                            units.add(m[1])
                    currencies = len({u for u in units if u})
            except Exception:
                pass
            return url, True, elapsed_ms, currencies
        return url, False, elapsed_ms, None
    except Exception:
        return url, False, None, None


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
        url_data_map = await discover_mint_urls()
        urls = sorted(list(url_data_map.keys()))

        async with httpx.AsyncClient(timeout=5.0) as client:
            results = await asyncio.gather(*[http_health(u, client) for u in urls])

        with Session(engine) as s:
            url_to_id = ensure_mints(s, [u for u, _, _, _ in results])
            for url, ok, ms, curs in results:
                # Extract stats from audit data if available
                extra = url_data_map.get(url)
                n_errors = extra.get("n_errors") if extra else None
                n_mints = extra.get("n_mints") if extra else None
                n_melts = extra.get("n_melts") if extra else None

                s.add(
                    HealthCheck(
                        mint_id=url_to_id[url],
                        status=ok,
                        response_ms=ms,
                        currency_count=curs,
                        n_errors=n_errors,
                        n_mints=n_mints,
                        n_melts=n_melts,
                    )
                )
            s.commit()
        try:
            await asyncio.wait_for(stop.wait(), timeout=60.0)
        except asyncio.TimeoutError:
            pass


async def lightning_loop(stop: asyncio.Event) -> None:
    while not stop.is_set():
        url_data_map = await discover_mint_urls()
        urls = sorted(list(url_data_map.keys()))
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
                        node_channel_count=res.node_channel_count,
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
    ln_channels: int | None
    avg_latency_ms: int | None
    currencies: int | None
    is_up: bool
    uptime_class: str
    latency_class: str
    # Raw values for sorting
    raw_uptime: float
    raw_capacity: int
    raw_channels: int
    # New stats
    n_errors: int
    n_mints: int
    n_melts: int


def compute_rows() -> list[MintRow]:
    now = datetime.now(timezone.utc)
    since_24h = now - timedelta(hours=24)
    rows: list[MintRow] = []
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
            last_currencies = (
                recent[0].currency_count
                if recent and recent[0].currency_count is not None
                else None
            )

            n_errors = (
                recent[0].n_errors if recent and recent[0].n_errors is not None else 0
            )
            n_mints = (
                recent[0].n_mints if recent and recent[0].n_mints is not None else 0
            )
            n_melts = (
                recent[0].n_melts if recent and recent[0].n_melts is not None else 0
            )

            latencies = [hc.response_ms for hc in recent if hc.response_ms is not None]
            avg_latency = int(sum(latencies) / len(latencies)) if latencies else None

            lns = s.exec(
                select(LightningSnapshot)
                .where(LightningSnapshot.mint_id == m.id)
                .order_by(cast(Any, LightningSnapshot.checked_at).desc())
                .limit(1)
            ).first()
            pubkey = lns.payee_pubkey if lns else None
            node_name = lns.node_name if lns else None
            cap = lns.node_capacity_sats if lns else None
            channels = lns.node_channel_count if lns else None
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

            latency_class = "none"
            if avg_latency is not None:
                if avg_latency < 200:
                    latency_class = "fast"
                elif avg_latency < 500:
                    latency_class = "ok"
                else:
                    latency_class = "slow"

            row = MintRow(
                url=m.url,
                uptime_24h=(f"{(uptime_ratio*100):.0f}%" if total else "-"),
                last_seen=last_seen,
                checks_html=render_sparkline(last50),
                ln_pubkey=pubkey,
                ln_name=node_name,
                ln_capacity=cap_str,
                ln_channels=channels,
                avg_latency_ms=avg_latency,
                currencies=last_currencies,
                is_up=is_up,
                uptime_class=uptime_class,
                latency_class=latency_class,
                raw_uptime=uptime_ratio,
                raw_capacity=cap if cap is not None else 0,
                raw_channels=channels if channels is not None else 0,
                n_errors=n_errors,
                n_mints=n_mints,
                n_melts=n_melts,
            )
            rows.append(row)

    return rows


def render_tbody() -> str:
    rows = compute_rows()

    # Just render all rows. Frontend handles sorting and grouping.
    def row_html(r: MintRow) -> str:
        # Determine base classes (up/down)
        # Note: 'no-cap' class was used for styling previously,
        # but since we want to unify, we can treat them similarly
        # or still add the class if we want visual distinction.
        # Let's keep 'no-cap' if capacity is missing for visual consistency.
        extra_class = "no-cap" if r.ln_capacity is None else ""
        classes = (
            f"{'up' if r.is_up else 'down'}{(' ' + extra_class) if extra_class else ''}"
        )
        node_cell = (
            (
                f"<a class='link' href='https://1ml.com/node/{r.ln_pubkey}' target='_blank' rel='noopener'>{r.ln_name}</a>"
                if r.ln_pubkey
                else r.ln_name
            )
            if r.ln_name
            else "-"
        )
        channels_cell = f"{r.ln_channels}" if r.ln_channels is not None else "-"
        currencies_cell = f"{r.currencies}" if r.currencies is not None else "-"

        # Data attributes for JS sorting
        data_attrs = (
            f'data-url="{r.url}" '
            f'data-name="{r.ln_name or ""}" '
            f'data-up="{1 if r.is_up else 0}" '
            f'data-uptime="{r.raw_uptime}" '
            f'data-capacity="{r.raw_capacity}" '
            f'data-channels="{r.raw_channels}" '
            f'data-currencies="{r.currencies if r.currencies is not None else 0}" '
            f'data-errors="{r.n_errors}" '
            f'data-mints="{r.n_mints}" '
            f'data-melts="{r.n_melts}" '
            f'data-latency="{r.avg_latency_ms if r.avg_latency_ms is not None else 99999}" '
        )

        return (
            f"<tr class='{classes}' {data_attrs}>"
            f"<td class=mint><span class=\"status-dot {'up' if r.is_up else 'down'}\" title=\"{'Up' if r.is_up else 'Down'}\"></span><a class=link href=\"{r.url}\" target=\"_blank\" rel=\"noopener\">{r.url.replace('https://','').replace('http://','')}</a></td>"
            f"<td><span class=\"badge uptime {r.uptime_class}\" title=\"Uptime last 24h\">{r.uptime_24h}</span></td>"
            f"<td><div class=spark>{r.checks_html}</div></td>"
            f"<td class=mono>{node_cell}</td>"
            f"<td>{channels_cell}</td>"
            f"<td>{(f'<span class=\'badge cap\'>{r.ln_capacity}</span>') if r.ln_capacity else '-'}</td>"
            f"<td class=currencies>{currencies_cell}</td>"
            f"<td>{r.n_mints}</td>"
            f"<td>{r.n_melts}</td>"
            f"<td>{r.n_errors}</td>"
            f"<td>{(f'<span class=\'badge latency {r.latency_class}\'>{r.avg_latency_ms} ms</span>') if r.avg_latency_ms is not None else '-'}</td>"
            f"</tr>"
        )

    body = "".join(row_html(r) for r in rows)
    return f"<tbody id=dashboard>{body}</tbody>"


def render_table() -> str:
    tbody = render_tbody()
    return (
        "<table class=card id=mint-table>"
        "<thead><tr>"
        "<th class='sortable' data-key='url'>Mint</th>"
        "<th class='sortable' data-key='uptime'>Uptime (24h)</th>"
        "<th>Last hour</th>"
        "<th class='sortable' data-key='ln_name'>LN Node</th>"
        "<th class='sortable' data-key='channels'>Channels</th>"
        "<th class='sortable' data-key='capacity'>LN Capacity (BTC)</th>"
        "<th class='sortable' data-key='currencies'>Currencies</th>"
        "<th class='sortable' data-key='mints'>Mints</th>"
        "<th class='sortable' data-key='melts'>Melts</th>"
        "<th class='sortable' data-key='errors'>Errors</th>"
        "<th class='sortable' data-key='latency'>Latency</th>"
        "</tr></thead>"
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
    th.sortable{cursor:pointer;user-select:none}
    th.sortable:hover{color:var(--accent)}
    th.sortable::after{content:'';display:inline-block;width:10px;margin-left:5px}
    th.asc::after{content:'▲'}
    th.desc::after{content:'▼'}
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
    .badge.unit{background:#0f1318;color:#a7f3d0}
    a{color:var(--accent);text-decoration:none}
    a:hover{text-decoration:underline}
    footer{max-width:1100px;margin:12px auto 0;padding:0 16px;color:var(--muted);font-size:12px}
    #meta{display:flex;gap:14px;align-items:center;margin-top:8px;color:var(--muted);font-size:12px}
    details{margin-bottom:12px;margin-top:24px;background:var(--surface);border:1px solid var(--border);border-radius:8px;padding:8px 12px;font-size:13px}
    summary{cursor:pointer;font-weight:600;color:var(--muted);user-select:none}
    summary:hover{color:var(--text)}
    .config-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:16px;margin-top:12px;padding-top:12px;border-top:1px solid var(--border)}
    .config-item{display:flex;flex-direction:column;gap:4px}
    .config-item label{color:var(--muted);font-size:12px;font-weight:600}
    .config-item input[type=range]{width:100%}
    .config-item input[type=checkbox]{accent-color:var(--accent)}
    .val{float:right;color:var(--text)}
    """.strip()

    settings_form = """
    <details open>
        <summary>Ranking Configuration</summary>
        <div class="config-grid">
            <div class="config-item">
                <label>Prioritize Online Status <input type="checkbox" id="w_status" checked></label>
            </div>
            <div class="config-item">
                <label>Currency Bonus <span class="val" id="val-curr">1000</span></label>
                <input type="range" id="w_currency" min="0" max="10000" step="100" value="1000">
            </div>
            <div class="config-item">
                <label>Capacity Weight <span class="val" id="val-cap">500</span></label>
                <input type="range" id="w_capacity" min="0" max="2000" step="50" value="500">
            </div>
            <div class="config-item">
                <label>Mints Weight <span class="val" id="val-mints">10</span></label>
                <input type="range" id="w_mints" min="0" max="100" step="1" value="10">
            </div>
            <div class="config-item">
                <label>Melts Weight <span class="val" id="val-melts">10</span></label>
                <input type="range" id="w_melts" min="0" max="100" step="1" value="10">
            </div>
            <div class="config-item">
                <label>Errors Penalty <span class="val" id="val-errors">100</span></label>
                <input type="range" id="w_errors" min="0" max="1000" step="10" value="100">
            </div>
            <div class="config-item">
                <label>Latency Penalty <span class="val" id="val-lat">1</span></label>
                <input type="range" id="w_latency" min="0" max="100" step="1" value="1">
            </div>
            <div class="config-item" style="justify-content:end">
                 <button id="reset-sort" style="padding:4px 8px;cursor:pointer;background:var(--surface-2);color:var(--text);border:1px solid var(--border);border-radius:4px">Reset to Weighted Score</button>
            </div>
        </div>
    </details>
    """

    return (
        "<!doctype html><html><head><meta charset=utf-8>"
        '<meta name=viewport content="width=device-width, initial-scale=1">'
        '<meta name="theme-color" content="#0a0b0f">'
        "<title>Cashu Mint Status</title>"
        f"<style>{styles}</style>"
        '<script src="https://unpkg.com/htmx.org@2.0.2" integrity="sha384-7Y/OLJm7GG4l7uYf4x2nY2hVqXzjP4uYbUhg0oMiJ2z2hQ0zDgANbHgxqCwR8K8y" crossorigin="anonymous"></script>'
        '<script src="/static/latency.js" defer></script>'
        '<script src="/static/sorting.js" defer></script>'
        "</head><body>"
        "<header><h1>Cashu.Live</h1><div id=meta><span class=muted>Auto-refresh every 10s</span></div></header>"
        "<main>"
        '<div hx-get="/dashboard" hx-trigger="load, every 10s" hx-target="#dashboard" hx-swap="outerHTML">'
        f"{render_table()}"
        "</div>"
        f"{settings_form}"
        "</main>"
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

app.mount("/static", StaticFiles(directory="static"), name="static")


@app.get("/", response_class=HTMLResponse)
async def index() -> str:
    return render_index()


@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard() -> str:
    return render_tbody()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=False)
