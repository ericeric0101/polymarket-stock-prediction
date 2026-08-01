"""Localhost-only web dashboard for isolated cross-market research."""

from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
from typing import Callable, Mapping

from .cross_market import research_dashboard_state


HTML = r'''<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Polymarket Stock Research</title>
<style>
:root{color-scheme:dark;--bg:#101214;--surface:#171a1d;--line:#30353a;--muted:#969da5;--text:#edf0f2;--cyan:#39c5d8;--green:#42c878;--red:#ef5b5b;--yellow:#d7c64a;--blue:#4d91ff}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:14px ui-monospace,SFMono-Regular,Menlo,monospace;letter-spacing:0}header{height:72px;border-bottom:1px solid var(--line);display:flex;align-items:center;justify-content:space-between;padding:0 24px;background:#131619}h1{font-size:20px;margin:0;letter-spacing:0}h2{font-size:15px;margin:0 0 14px}.sub,.muted{color:var(--muted)}.status{display:flex;gap:18px;align-items:center}.dot{width:8px;height:8px;border-radius:50%;display:inline-block;background:var(--green);margin-right:7px}nav{display:flex;gap:2px;padding:12px 24px 0;border-bottom:1px solid var(--line);background:#131619}button.tab{border:0;border-bottom:2px solid transparent;background:transparent;color:var(--muted);padding:10px 14px;cursor:pointer;font:inherit}button.tab.active{color:var(--text);border-color:var(--cyan)}main{padding:20px 24px 36px}.view{display:none}.view.active{display:block}.band{border:1px solid var(--line);background:var(--surface);padding:16px;margin-bottom:16px;border-radius:4px}.toolbar{display:flex;gap:18px;justify-content:space-between;align-items:center;margin-bottom:12px}.legend{display:flex;gap:16px;flex-wrap:wrap}.swatch{width:12px;height:3px;display:inline-block;margin-right:6px;vertical-align:middle}.scroll{overflow:auto}table{width:100%;border-collapse:collapse;table-layout:fixed}th{text-align:left;color:var(--muted);font-weight:600;border-bottom:1px solid var(--line);padding:10px 9px}td{border-bottom:1px solid #272c30;padding:11px 9px;vertical-align:top;overflow-wrap:anywhere}.symbol{font-weight:700;color:#fff}.checkpoint{white-space:nowrap}.positive,.confirm{color:var(--green)}.negative,.disagree{color:var(--red)}.mixed{color:var(--yellow)}.unreliable{color:var(--muted)}.pill{display:inline-block;border:1px solid currentColor;padding:2px 6px;border-radius:3px;font-size:12px}canvas{display:block;width:100%;height:420px;background:#121518;border:1px solid var(--line)}.chart-empty{height:420px;display:grid;place-items:center;color:var(--muted);border:1px solid var(--line);background:#121518}.split{display:grid;grid-template-columns:minmax(0,2fr) minmax(260px,1fr);gap:16px}.metric{padding:12px 0;border-bottom:1px solid var(--line);display:flex;justify-content:space-between;gap:12px}.metric:last-child{border:0}.warning{color:var(--yellow)}@media(max-width:900px){header{height:auto;min-height:72px;align-items:flex-start;gap:12px;flex-direction:column;padding:16px}.status{flex-wrap:wrap}nav,main{padding-left:12px;padding-right:12px}.split{grid-template-columns:1fr}table{min-width:920px}canvas,.chart-empty{height:320px}}
</style>
</head>
<body>
<header><div><h1>Polymarket Stock Research</h1><div class="sub">Core signals and isolated price-ladder diagnostics</div></div><div class="status"><span><i class="dot"></i>Research only</span><span id="date">-</span><span id="updated">-</span></div></header>
<nav><button class="tab active" data-view="core">Core Up/Down</button><button class="tab" data-view="distribution">Price Distribution</button><button class="tab" data-view="cross">Cross-Market</button></nav>
<main>
<section class="view active" id="core"><div class="band"><div class="toolbar"><h2>Immutable checkpoint decisions</h2><span class="muted">Side · selected fair · recorded ask · fee/buffer edge</span></div><div class="scroll"><table><thead><tr><th style="width:9%">Symbol</th><th>12:00 EDT</th><th>14:00 EDT</th><th>15:30 EDT</th><th style="width:26%">Latest core status</th></tr></thead><tbody id="core-body"></tbody></table></div></div></section>
<section class="view" id="distribution"><div class="split"><div class="band"><div class="toolbar"><h2>Implied close distribution</h2><select id="symbol-select" aria-label="Symbol"></select></div><div class="legend"><span><i class="swatch" style="background:var(--cyan)"></i>Monotonic midpoint</span><span><i class="swatch" style="background:var(--yellow)"></i>Executable uncertainty</span><span><i class="swatch" style="background:var(--red)"></i>Price to beat</span></div><canvas id="curve" width="1200" height="420"></canvas><div id="curve-empty" class="chart-empty" hidden>No ladder snapshots for this New York date.</div></div><aside class="band"><h2>Curve quality</h2><div id="curve-metrics"></div></aside></div></section>
<section class="view" id="cross"><div class="band"><div class="toolbar"><h2>Three-way comparison</h2><span class="warning">Never changes entries or sizing</span></div><div class="scroll"><table><thead><tr><th>Symbol</th><th>Checkpoint</th><th>Price to beat</th><th>Model Up</th><th>Up/Down market</th><th>Ladder implied</th><th>Executable range</th><th>Status</th><th>Reasons</th></tr></thead><tbody id="cross-body"></tbody></table></div></div></section>
</main>
<script>
let state={};const names=['1200_EDT','1400_EDT','1530_EDT'];
const pct=v=>v==null?'-':(v*100).toFixed(1)+'%';const price=v=>v==null?'-':Number(v).toFixed(v<.01?3:2);
function checkpointCell(p){if(!p)return '<span class="muted">PENDING</span>';let side=p.model_outcome;if(side!=='UP'&&side!=='DOWN'){const ue=Number(p.up_edge),de=Number(p.down_edge);side=Math.max(ue,de)>0?(ue>=de?'UP':'DOWN'):(Number(p.fair_up_probability)>=.5?'UP':'DOWN')}const fair=side==='UP'?Number(p.fair_up_probability):1-Number(p.fair_up_probability);const ask=side==='UP'?p.up_ask:p.down_ask;const edge=side==='UP'?p.up_edge:p.down_edge;const cls=p.paper_outcome===side?'positive':Number(edge)>0?'mixed':'muted';return `<span class="checkpoint ${cls}">${side} ${pct(fair)} · a${price(ask)} · e${edge==null?'-':(edge>=0?'+':'')+pct(edge)}</span>`}
function renderCore(){const body=document.querySelector('#core-body');const rows=state.core_rows||[];body.innerHTML=rows.length?rows.map(r=>{const cp=r.checkpoints||{};const latest=names.slice().reverse().find(n=>cp[n]);const p=latest?cp[latest]:null;let status='WAIT';if(p?.paper_outcome)status=`ENTRY-ELIGIBLE ${p.paper_outcome}`;else if(p?.paper_entry_block_reasons?.length)status=`BLOCKED ${p.paper_entry_block_reasons[0]}`;else if(p)status='NO ENTRY AT CHECKPOINT';return `<tr><td class="symbol">${r.symbol}</td>${names.map(n=>`<td>${checkpointCell(cp[n])}</td>`).join('')}<td>${status}</td></tr>`}).join(''):'<tr><td colspan="5" class="muted">Waiting for regular-session evaluations.</td></tr>'}
function renderCross(){const body=document.querySelector('#cross-body');const rows=state.cross_market||[];body.innerHTML=rows.length?rows.map(r=>`<tr><td class="symbol">${r.symbol}</td><td>${r.checkpoint_name.replace('_EDT','')}</td><td>${Number(r.price_to_beat).toFixed(2)}</td><td>${pct(r.model_up_probability)}</td><td>${pct(r.up_down_market_probability)}</td><td>${pct(r.ladder_up_probability)}</td><td>${pct(r.ladder_lower_bound)} – ${pct(r.ladder_upper_bound)}</td><td><span class="pill ${r.status.toLowerCase()}">${r.status}</span></td><td class="muted">${(r.reasons||[]).join(', ')||'-'}</td></tr>`).join(''):'<tr><td colspan="9" class="muted">No matching core checkpoint and ladder snapshot yet.</td></tr>'}
function renderDistribution(){const curves=state.ladder_curves||[];const select=document.querySelector('#symbol-select');const previous=select.value;select.innerHTML=curves.map(c=>`<option>${c.symbol}</option>`).join('');if(curves.some(c=>c.symbol===previous))select.value=previous;const curve=curves.find(c=>c.symbol===select.value)||curves[0];const canvas=document.querySelector('#curve'),empty=document.querySelector('#curve-empty');if(!curve||!curve.points.length){canvas.hidden=true;empty.hidden=false;document.querySelector('#curve-metrics').innerHTML='<div class="muted">No data</div>';return}canvas.hidden=false;empty.hidden=true;drawCurve(canvas,curve);const spreads=curve.points.map(p=>p.spread);document.querySelector('#curve-metrics').innerHTML=`<div class="metric"><span>Symbol</span><b>${curve.symbol}</b></div><div class="metric"><span>Strikes</span><b>${curve.points.length}</b></div><div class="metric"><span>Raw monotonic violations</span><b class="${curve.violations?'warning':'confirm'}">${curve.violations}</b></div><div class="metric"><span>Mean executable width</span><b>${pct(spreads.reduce((a,b)=>a+b,0)/spreads.length)}</b></div><div class="metric"><span>Last snapshot</span><b>${new Date(curve.observed_at).toLocaleTimeString()}</b></div>`}
function drawCurve(canvas,curve){const dpr=window.devicePixelRatio||1;const rect=canvas.getBoundingClientRect();canvas.width=Math.max(600,rect.width*dpr);canvas.height=Math.max(300,rect.height*dpr);const c=canvas.getContext('2d');c.scale(dpr,dpr);const w=canvas.width/dpr,h=canvas.height/dpr,p={l:58,r:24,t:24,b:42};c.clearRect(0,0,w,h);const pts=curve.points,xmin=pts[0].strike,xmax=pts[pts.length-1].strike;const x=v=>p.l+(v-xmin)/Math.max(xmax-xmin,1)*(w-p.l-p.r),y=v=>p.t+(1-v)*(h-p.t-p.b);c.strokeStyle='#30353a';c.fillStyle='#969da5';c.font='12px ui-monospace';for(let q=0;q<=1.001;q+=.25){c.beginPath();c.moveTo(p.l,y(q));c.lineTo(w-p.r,y(q));c.stroke();c.fillText((q*100).toFixed(0)+'%',8,y(q)+4)}pts.forEach(pt=>c.fillText('$'+pt.strike,x(pt.strike)-18,h-15));c.beginPath();pts.forEach((pt,i)=>{const xx=x(pt.strike),yy=y(pt.upper_bound);i?c.lineTo(xx,yy):c.moveTo(xx,yy)});[...pts].reverse().forEach(pt=>c.lineTo(x(pt.strike),y(pt.lower_bound)));c.closePath();c.fillStyle='rgba(215,198,74,.16)';c.fill();c.strokeStyle='#39c5d8';c.lineWidth=2;c.beginPath();pts.forEach((pt,i)=>{const xx=x(pt.strike),yy=y(pt.adjusted_probability);i?c.lineTo(xx,yy):c.moveTo(xx,yy)});c.stroke();pts.forEach(pt=>{c.fillStyle='#39c5d8';c.beginPath();c.arc(x(pt.strike),y(pt.adjusted_probability),3,0,Math.PI*2);c.fill()})}
async function refresh(){try{const r=await fetch('/api/state',{cache:'no-store'});state=await r.json();document.querySelector('#date').textContent=`NY ${state.market_date}`;document.querySelector('#updated').textContent=new Date(state.generated_at).toLocaleTimeString();renderCore();renderCross();renderDistribution()}catch(e){document.querySelector('#updated').textContent='API unavailable'}}
document.querySelectorAll('.tab').forEach(b=>b.onclick=()=>{document.querySelectorAll('.tab,.view').forEach(x=>x.classList.remove('active'));b.classList.add('active');document.querySelector('#'+b.dataset.view).classList.add('active');if(b.dataset.view==='distribution')renderDistribution()});document.querySelector('#symbol-select').onchange=renderDistribution;window.onresize=()=>{if(document.querySelector('#distribution').classList.contains('active'))renderDistribution()};refresh();setInterval(refresh,3000);
</script></body></html>'''


class ResearchDashboardServer:
    def __init__(
        self, journal_path: Path, *, host: str = "127.0.0.1", port: int = 8765,
        state_fn: Callable[..., Mapping[str, object]] = research_dashboard_state,
    ) -> None:
        if host not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError("research dashboard is localhost-only")
        if not 1 <= port <= 65535:
            raise ValueError("invalid research dashboard port")
        self.journal_path = journal_path
        self.host = host
        self.port = port
        self.state_fn = state_fn

    def serve_forever(self) -> None:
        journal_path = self.journal_path
        state_fn = self.state_fn

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                if self.path == "/" or self.path.startswith("/?"):
                    self._send(200, "text/html; charset=utf-8", HTML.encode("utf-8"))
                    return
                if self.path == "/api/state":
                    payload = json.dumps(state_fn(journal_path), sort_keys=True, default=str).encode("utf-8")
                    self._send(200, "application/json", payload)
                    return
                self._send(404, "application/json", b'{"error":"not found"}')

            def _send(self, status: int, content_type: str, body: bytes) -> None:
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'")
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, format: str, *args: object) -> None:
                return

        server = ThreadingHTTPServer((self.host, self.port), Handler)
        print(f"Research dashboard: http://{self.host}:{self.port}", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            server.server_close()
