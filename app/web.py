"""FastAPI operations console.

Serves a single-page console that can
  * switch the authoritative endpoint's behaviour for the fixed domain
    (A record / NXDOMAIN / delayed A / timeout),
  * fire a real DNS query at the resolver and show the actual reply together
    with where it came from (upstream / cache-fresh / cache-stale / none),
  * show the resolver cache state (with live countdowns) and the query logs
    of both the resolver and the authoritative endpoint.

Configuration editing is field-level optimistic concurrency control:

  * the 2s auto-refresh shows the *effective* values but never overwrites an
    unsaved draft on the page;
  * "apply" sends only the fields this page actually changed, each with the
    value that was effective when editing of that field began (the baseline);
  * two pages editing different fields therefore both commit;
  * if a field's effective value now differs from both the baseline and this
    page's proposed value, the whole submission is rejected, the conflicting
    fields' effective values are shown, and every draft is kept for review.

The legacy POST /api/mode endpoint is kept unchanged for older clients:
missing ip/delay are filled from the values effective at submission time and
the full saved configuration is returned.
"""
from __future__ import annotations

import ipaddress
import time

import uvicorn
from dnslib import QTYPE, RCODE, DNSRecord
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field

from . import common

app = FastAPI(title="DNS duty console")

PAGE = """<!doctype html>
<html lang="zh">
<head>
<meta charset="utf-8">
<title>DNS 值班控制台</title>
<style>
  body { font-family: system-ui, sans-serif; margin: 2rem; background:#f6f7f9; color:#222; }
  h1 { font-size: 1.3rem; }
  section { background:#fff; border:1px solid #ddd; border-radius:8px;
            padding:1rem 1.2rem; margin-bottom:1rem; }
  h2 { font-size:1rem; margin-top:0; }
  label { margin-right:1rem; cursor:pointer; }
  input[type=text] { width:9rem; }
  input[type=number] { width:5rem; }
  button { padding:.35rem 1rem; cursor:pointer; }
  table { border-collapse:collapse; width:100%; font-size:.82rem; }
  th, td { border:1px solid #e2e2e2; padding:.25rem .5rem; text-align:left; }
  th { background:#f0f1f4; }
  pre { background:#0d1420; color:#cfe3ff; padding:.7rem; border-radius:6px;
        overflow:auto; font-size:.78rem; }
  .pill { display:inline-block; padding:.1rem .55rem; border-radius:1rem;
          background:#e8ecf3; font-size:.8rem; margin-right:.4rem; }
  .ok { color:#0a7a2f; } .bad { color:#b00020; } .muted { color:#777; }
  #qresult b { font-size:1.05rem; }
  .cfgrow { display:flex; align-items:center; gap:.6rem; margin:.45rem 0;
            flex-wrap:wrap; }
  .cfgrow .fname { width:7.5rem; color:#444; }
  .eff { font-weight:600; }
  .drafttag { display:none; font-size:.75rem; color:#8a5a00; background:#fff3d6;
              border:1px solid #e6c46a; border-radius:1rem; padding:.05rem .5rem; }
  .dirty .drafttag { display:inline-block; }
  .conflict input, .conflict #f-mode { outline:2px solid #b00020; }
  .conflicttag { display:none; font-size:.75rem; color:#fff; background:#b00020;
                 border-radius:1rem; padding:.05rem .5rem; }
  .conflict .conflicttag { display:inline-block; }
  #conflictbox { display:none; border:1px solid #b00020; background:#fdecec;
                 border-radius:6px; padding:.6rem .8rem; margin:.6rem 0; }
</style>
</head>
<body>
<h1>DNS 值班控制台 — <code id="dom">…</code></h1>
<p class="muted" id="params"></p>

<section>
  <h2>1. 权威端行为切换</h2>
  <p class="muted" style="margin-top:0">自动刷新只更新“生效值”，不会覆盖本页未保存的草稿；
    “应用”仅提交本页改动过的项。两页改不同项可分别生效；若某项被他人改过且与本页草稿不一致，整次拒绝并列出冲突项的生效值，草稿保留。</p>

  <div class="cfgrow" id="row-mode">
    <span class="fname">模式</span>
    <span id="f-mode">
      <label><input type="radio" name="mode" value="a"> A 记录</label>
      <label><input type="radio" name="mode" value="nxdomain"> NXDOMAIN</label>
      <label><input type="radio" name="mode" value="delay"> 延迟应答</label>
      <label><input type="radio" name="mode" value="timeout"> 超时（不应答）</label>
    </span>
    <span class="muted">生效值：<b class="eff" id="eff-mode">…</b></span>
    <span class="drafttag">草稿（未保存）</span>
    <span class="conflicttag">冲突</span>
  </div>

  <div class="cfgrow" id="row-ip">
    <span class="fname">A 记录 IP</span>
    <input type="text" id="f-ip" value="192.0.2.10">
    <span class="muted">生效值：<b class="eff" id="eff-ip">…</b></span>
    <span class="drafttag">草稿（未保存）</span>
    <span class="conflicttag">冲突</span>
  </div>

  <div class="cfgrow" id="row-delay_seconds">
    <span class="fname">延迟秒数</span>
    <input type="number" id="f-delay_seconds" value="3" min="0" max="60" step="0.5">
    <span class="muted">生效值：<b class="eff" id="eff-delay_seconds">…</b></span>
    <span class="drafttag">草稿（未保存）</span>
    <span class="conflicttag">冲突</span>
  </div>

  <p>
    <button onclick="applyControl()">应用</button>
    <button onclick="resetDraft()">重置草稿</button>
    <span id="modemsg"></span>
  </p>
  <div id="conflictbox"></div>
  <p class="muted">当前权威端配置每 2 秒自动刷新（更新于 <span id="updated_at">…</span>）</p>
</section>

<section>
  <h2>2. 发起查询（经解析器的真实 UDP 查询）</h2>
  <button onclick="doQuery()">查询 A 记录</button>
  <span id="qmsg"></span>
  <div id="qresult"></div>
  <pre id="qraw" style="display:none"></pre>
</section>

<section>
  <h2>3. 解析器缓存状态</h2>
  <div id="cache">…</div>
</section>

<section>
  <h2>4. 解析器查询日志（最近 15 条）</h2>
  <table id="rlog"><thead><tr>
    <th>时间</th><th>名称</th><th>类型</th><th>来源</th><th>RCODE</th><th>答复</th><th>说明</th>
  </tr></thead><tbody></tbody></table>
</section>

<section>
  <h2>5. 权威端接收日志（最近 15 条）</h2>
  <table id="alog"><thead><tr>
    <th>时间</th><th>名称</th><th>类型</th><th>模式</th><th>动作</th><th>说明</th>
  </tr></thead><tbody></tbody></table>
</section>

<script>
let serverOffset = 0;  // server_now - client_now
let control = null;    // latest effective control row from /api/state
const baseline = {};   // field -> effective value when editing of it began
const edited = new Set();  // fields this page has an unsaved draft on
const FIELDS = ['mode', 'ip', 'delay_seconds'];
const FIELD_LABEL = { mode: '模式', ip: 'A 记录 IP', delay_seconds: '延迟秒数' };

function fmtTs(ts) {
  return new Date((ts) * 1000).toLocaleTimeString();
}
function countdown(ts) {
  const left = ts - (Date.now() / 1000 + serverOffset);
  if (left <= 0) return '<span class="bad">已过期</span>';
  return '<span class="ok">剩 ' + left.toFixed(1) + 's</span>';
}

// -- form helpers -----------------------------------------------------------
function fieldVal(f) {
  if (f === 'mode') {
    const el = document.querySelector('input[name=mode]:checked');
    return el ? el.value : '';
  }
  return document.getElementById('f-' + f).value;
}
function setFieldVal(f, v) {
  if (f === 'mode') {
    const el = document.querySelector(`input[name=mode][value="${v}"]`);
    if (el) el.checked = true;
    return;
  }
  document.getElementById('f-' + f).value = v;
}
function sameVal(f, a, b) {
  if (a === null || a === undefined || b === null || b === undefined) return false;
  if (f === 'delay_seconds') return Number(a) === Number(b);
  return String(a) === String(b);
}
function isDirty(f) {
  // A draft is a field this page edited and has not saved/reset since.
  return edited.has(f);
}
function markRow(f, dirty, conflict) {
  const row = document.getElementById('row-' + f);
  row.classList.toggle('dirty', dirty);
  row.classList.toggle('conflict', !!conflict);
}

// Freeze the baseline the moment a field leaves the effective value; once it
// returns to the effective value the draft is dropped and it re-baselines.
function onEdit(f) {
  const nowDirty = !sameVal(f, fieldVal(f), control ? control[f] : fieldVal(f));
  if (nowDirty) {
    if (!edited.has(f)) baseline[f] = control[f];  // baseline once, at edit start
    edited.add(f);
  } else {
    edited.delete(f);
    delete baseline[f];
  }
  markRow(f, nowDirty, false);
}
for (const f of FIELDS) {
  if (f === 'mode') {
    document.querySelectorAll('input[name=mode]').forEach(el =>
      el.addEventListener('change', () => onEdit('mode')));
  } else {
    document.getElementById('f-' + f).addEventListener('input', () => onEdit(f));
  }
}

function showMsg(html) { document.getElementById('modemsg').innerHTML = html; }

async function refresh() {
  try {
    const s = await (await fetch('/api/state')).json();
    serverOffset = s.now - Date.now() / 1000;
    document.getElementById('dom').textContent = s.domain;
    document.getElementById('params').textContent =
      `成功答复 TTL=${s.params.fresh_ttl}s · 过期后旧 A 最多沿用 ${s.params.stale_window}s` +
      ` · 上游超时 ${s.params.upstream_timeout}s`;
    const c = s.control;
    control = c;
    if (c) {
      document.getElementById('updated_at').textContent = fmtTs(c.updated_at);
      for (const f of FIELDS) {
        document.getElementById('eff-' + f).textContent = c[f];
        const dirty = isDirty(f);
        if (!dirty) {
          setFieldVal(f, c[f]);           // keep drafts: only clean fields sync
          baseline[f] = c[f];             // clean fields track the baseline
        }
        markRow(f, dirty, false);
      }
    }
    const k = s.cache;
    document.getElementById('cache').innerHTML = k ?
      `<span class="pill">rcode=${k.rcode}</span>` +
      (k.a_record ? `<span class="pill">A=${k.a_record}</span>` : '<span class="pill">无 A 记录</span>') +
      `<span class="pill">fresh: ${countdown(k.fresh_until)}</span>` +
      (k.stale_until ? `<span class="pill">stale: ${countdown(k.stale_until)}</span>`
                     : '<span class="pill">无 stale 备用</span>') +
      (k.nx_confirmed_at ? `<span class="pill">NXDOMAIN 确认于 ${fmtTs(k.nx_confirmed_at)}</span>` : '')
      : '<span class="muted">（缓存为空）</span>';
    fillLog('rlog', s.resolver_log, r =>
      [fmtTs(r.ts), r.qname, r.qtype, r.source, r.rcode, r.answers, r.detail]);
    fillLog('alog', s.auth_log, r =>
      [fmtTs(r.ts), r.qname, r.qtype, r.mode, r.action, r.detail]);
  } catch (e) { console.log(e); }
}

function fillLog(id, rows, cols) {
  const tb = document.querySelector('#' + id + ' tbody');
  tb.innerHTML = rows.map(r =>
    '<tr>' + cols(r).map(x => `<td>${x ?? ''}</td>`).join('') + '</tr>').join('');
}

// -- apply only this page's changed fields, with per-field baselines --------
async function applyControl() {
  document.getElementById('conflictbox').style.display = 'none';
  if (!control) { showMsg('<span class="bad">状态尚未加载</span>'); return; }
  const changes = {}, expected = {};
  for (const f of FIELDS) {
    if (isDirty(f)) {
      changes[f] = f === 'delay_seconds' ? parseFloat(fieldVal(f)) : fieldVal(f);
      expected[f] = baseline[f] !== undefined ? baseline[f] : control[f];
    }
  }
  if (Object.keys(changes).length === 0) {
    showMsg('<span class="muted">本页没有改动，无需应用</span>');
    return;
  }
  let r, resp;
  try {
    resp = await fetch('/api/control/apply', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ changes, expected })
    });
    r = await resp.json();
  } catch (e) {
    showMsg('<span class="bad">请求失败：' + e + '</span>');
    return;
  }
  if (r.ok) {
    showMsg('<span class="ok">✔ 已应用本页 ' + Object.keys(changes).length +
            ' 项改动</span>');
    for (const f of Object.keys(changes)) {  // saved drafts are cleared
      edited.delete(f);
      delete baseline[f];
    }
    await refresh();   // saved fields show the new effective value; other drafts stay
    return;
  }
  if (r.error === 'conflict') {
    // Whole submission rejected: show the conflicting effective values and
    // keep every draft untouched for review and re-submission.
    control = r.control;
    for (const f of FIELDS) {
      document.getElementById('eff-' + f).textContent = control[f];
      markRow(f, isDirty(f), !!r.conflicts[f]);
    }
    const items = Object.entries(r.conflicts)
      .map(([f, v]) => `<li><b>${FIELD_LABEL[f]}</b>：生效值现为 <b>${v}</b>，
        本页草稿为 <b>${fieldVal(f)}</b>，开始编辑时为 <b>${expected[f]}</b></li>`)
      .join('');
    const box = document.getElementById('conflictbox');
    box.innerHTML = '<b class="bad">整次提交被拒绝（乐观锁冲突）</b>' +
      '<ul style="margin:.4rem 0">' + items + '</ul>' +
      '<span class="muted">草稿已保留，请核对生效值后修改并重新“应用”。</span>';
    box.style.display = 'block';
    showMsg('<span class="bad">✘ 存在冲突项，未写入任何改动</span>');
    return;
  }
  showMsg('<span class="bad">✘ ' + (r.error || '提交被拒绝') + '</span>');
}

function resetDraft() {
  if (!control) return;
  edited.clear();
  for (const f of FIELDS) {
    setFieldVal(f, control[f]);
    delete baseline[f];
    markRow(f, false, false);
  }
  document.getElementById('conflictbox').style.display = 'none';
  showMsg('<span class="muted">草稿已重置为当前生效值</span>');
}

async function doQuery() {
  document.getElementById('qmsg').textContent = ' 查询中…';
  const r = await (await fetch('/api/query', {method: 'POST'})).json();
  document.getElementById('qmsg').textContent = '';
  const el = document.getElementById('qresult');
  if (!r.ok) { el.innerHTML = '<b class="bad">查询失败：' + r.error + '</b>'; return; }
  const ans = r.answers.length
    ? r.answers.map(a => `${a.type} ${a.data} <span class="muted">TTL=${a.ttl}s</span>`).join('，')
    : '（无应答记录）';
  el.innerHTML = `<p><b>${r.rcode}</b> &nbsp; ${ans} &nbsp; ` +
    `<span class="pill">来源：${r.source}</span> ` +
    `<span class="muted">${r.elapsed_ms}ms · ${r.meta.join(' ')}</span></p>`;
  const raw = document.getElementById('qraw');
  raw.style.display = 'block';
  raw.textContent = r.raw;
  refresh();
}

setInterval(refresh, 2000);
refresh();
</script>
</body>
</html>
"""


class ModeIn(BaseModel):
    mode: str
    ip: str | None = None
    delay_seconds: float | None = None


class ControlApplyIn(BaseModel):
    changes: dict = Field(default_factory=dict)
    expected: dict = Field(default_factory=dict)


_ALLOWED_FIELDS = ("mode", "ip", "delay_seconds")
_VALID_MODES = ("a", "nxdomain", "delay", "timeout")


def _validate_field(field: str, value) -> tuple[bool, str]:
    if field not in _ALLOWED_FIELDS:
        return False, f"unknown field {field!r}"
    if field == "mode":
        if value not in _VALID_MODES:
            return False, f"unknown mode {value!r}"
    elif field == "ip":
        try:
            ipaddress.IPv4Address(str(value))
        except ValueError:
            return False, f"bad IPv4 address {value!r}"
    else:  # delay_seconds
        try:
            d = float(value)
        except (TypeError, ValueError):
            return False, f"bad delay value {value!r}"
        if not (0 <= d <= 60):
            return False, "delay must be 0..60s"
    return True, ""


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return PAGE


@app.get("/api/state")
def state() -> dict:
    return {
        "now": time.time(),
        "domain": common.FQDN,
        "params": {
            "fresh_ttl": common.FRESH_TTL,
            "stale_window": common.STALE_WINDOW,
            "upstream_timeout": common.UPSTREAM_TIMEOUT,
        },
        "control": common.get_control(),
        "cache": common.get_cache(),
        "resolver_log": common.recent("query_log"),
        "auth_log": common.recent("auth_log"),
    }


@app.post("/api/control/apply")
def apply_control(body: ControlApplyIn):
    """Merge one page's partial edit under field-level optimistic locking."""
    changes, expected = body.changes or {}, body.expected or {}
    if not isinstance(changes, dict) or not isinstance(expected, dict):
        return JSONResponse(
            {"ok": False, "error": "changes and expected must be objects"}, 400)
    if not changes:
        return JSONResponse({"ok": False, "error": "no changes submitted"}, 400)
    unknown = [f for f in (*changes, *expected) if f not in _ALLOWED_FIELDS]
    if unknown:
        return JSONResponse(
            {"ok": False, "error": f"unknown field(s): {', '.join(unknown)}"}, 400)
    missing_baseline = [f for f in changes if f not in expected]
    if missing_baseline:
        return JSONResponse(
            {"ok": False,
             "error": f"missing baseline for: {', '.join(missing_baseline)}"}, 400)
    for f, v in changes.items():
        ok, err = _validate_field(f, v)
        if not ok:
            return JSONResponse({"ok": False, "error": err}, 400)
    try:
        saved = common.apply_control_change(changes, expected)
    except common.ControlConflict as exc:
        # Nothing was written; return the conflicting effective values plus
        # the full current row so the page can keep its drafts and re-submit.
        return JSONResponse(
            {"ok": False, "error": "conflict",
             "conflicts": exc.conflicts, "control": common.get_control()},
            409,
        )
    return {"ok": True, "control": saved}


@app.post("/api/mode")
def set_mode(m: ModeIn):
    """Legacy whole-row mode switch.

    Missing ip/delay are filled from the values effective at submission time;
    a successful response returns the full configuration that was saved.
    """
    if m.mode not in _VALID_MODES:
        return JSONResponse({"ok": False, "error": f"unknown mode {m.mode!r}"}, 400)
    cur = common.get_control() or {"ip": common.DEFAULT_IP,
                                   "delay_seconds": common.DEFAULT_DELAY}
    ip = m.ip if m.ip is not None else cur["ip"]
    delay = m.delay_seconds if m.delay_seconds is not None else cur["delay_seconds"]
    ok, err = _validate_field("ip", ip)
    if not ok:
        return JSONResponse({"ok": False, "error": err}, 400)
    ok, err = _validate_field("delay_seconds", delay)
    if not ok:
        return JSONResponse({"ok": False, "error": err}, 400)
    common.set_control(m.mode, str(ip), float(delay))
    return {"ok": True, "control": common.get_control()}


@app.post("/api/query")
def do_query() -> dict:
    """Fire a real UDP A query at the resolver and report the actual reply."""
    query = DNSRecord.question(common.FQDN, "A")
    t0 = time.time()
    try:
        data = query.send(common.RESOLVER_HOST, common.RESOLVER_PORT,
                          timeout=common.UPSTREAM_TIMEOUT + 6)
        rep = DNSRecord.parse(data)
    except Exception as exc:  # noqa: BLE001 - report any transport failure
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    answers = [
        {"name": str(rr.rname), "type": QTYPE[rr.rtype], "data": str(rr.rdata),
         "ttl": rr.ttl}
        for rr in rep.rr
    ]
    txts = [str(rr.rdata).strip('"') for rr in rep.ar if rr.rtype == QTYPE.TXT]
    source = next(
        (t.split("=", 1)[1].split()[0] for t in txts if t.startswith("source=")),
        "unknown",
    )
    return {
        "ok": True,
        "rcode": RCODE[rep.header.rcode],
        "answers": answers,
        "source": source,
        "meta": txts,
        "elapsed_ms": int((time.time() - t0) * 1000),
        "raw": str(rep),
    }


def main() -> None:
    common.init_db()
    uvicorn.run(app, host="0.0.0.0", port=common.WEB_PORT, log_level="warning")


if __name__ == "__main__":
    main()
