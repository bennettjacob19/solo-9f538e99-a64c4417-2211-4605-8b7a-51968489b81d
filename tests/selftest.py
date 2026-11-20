#!/usr/bin/env python3
"""End-to-end self-test for the serve-stale DNS duty system.

Spawns auth + resolver + web on loopback with the production timing
parameters (fresh TTL 5s, stale window 20s, upstream timeout 2s) and drives
the key interleavings:

  A. basic fetch / fresh-cache hit / TTL countdown / refresh after expiry
  B. upstream timeout after expiry -> stale A (<=20s) -> SERVFAIL beyond
  C. confirmed NXDOMAIN purges the old A; NXDOMAIN is never served stale
  D. a late (delayed) old answer cannot resurrect anything
  E. resolver restart does not extend the old A's validity
  F. web console smoke test (page, mode switch, query, state)
  G. delay mode snapshots address/delay at receive time; edits while a
     request is waiting never change the in-flight request, which answers
     exactly once, measured from its own receive moment
  H. field-level optimistic concurrency on the console (partial applies,
     two pages on different fields, whole-tx rejection on conflict) and the
     legacy /api/mode compatibility contract

Exits non-zero if any check fails.  Runtime: ~95s.
"""
from __future__ import annotations

import json
import os
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from dnslib import QTYPE, DNSRecord  # noqa: E402

DOMAIN = "service.example"
AUTH_PORT, RES_PORT, WEB_PORT = 15354, 15353, 18000
TMP = tempfile.mkdtemp(prefix="dns-selftest-")
DB = os.path.join(TMP, "app.db")

env = dict(os.environ)
env.update(
    DB_PATH=DB,
    FIXED_DOMAIN=DOMAIN,
    AUTH_HOST="127.0.0.1",
    AUTH_PORT=str(AUTH_PORT),
    RESOLVER_HOST="127.0.0.1",
    RESOLVER_PORT=str(RES_PORT),
    WEB_PORT=str(WEB_PORT),
    FRESH_TTL="5",
    STALE_WINDOW="20",
    UPSTREAM_TIMEOUT="2",
    DEFAULT_IP="192.0.2.10",
    DEFAULT_DELAY="3",
    PYTHONPATH=ROOT,
    PYTHONUNBUFFERED="1",
)

procs: dict[str, subprocess.Popen] = {}
FAILS: list[str] = []


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def start(name: str, module: str) -> None:
    log = open(os.path.join(TMP, f"{name}.log"), "ab")
    procs[name] = subprocess.Popen(
        [sys.executable, "-m", module],
        cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT,
    )


def stop(name: str) -> None:
    p = procs.get(name)
    if p and p.poll() is None:
        p.terminate()
        try:
            p.wait(5)
        except subprocess.TimeoutExpired:
            p.kill()


def set_mode(mode: str, ip: str = "192.0.2.10", delay: float = 3.0) -> None:
    conn = sqlite3.connect(DB, timeout=15)
    conn.execute("PRAGMA busy_timeout=15000")
    conn.execute(
        "INSERT INTO control(id, mode, ip, delay_seconds, updated_at)"
        " VALUES (1, ?, ?, ?, ?)"
        " ON CONFLICT(id) DO UPDATE SET mode=excluded.mode, ip=excluded.ip,"
        " delay_seconds=excluded.delay_seconds, updated_at=excluded.updated_at",
        (mode, ip, delay, time.time()),
    )
    conn.commit()
    conn.close()


def get_cache() -> dict | None:
    conn = sqlite3.connect(DB, timeout=15)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM cache WHERE id = 1").fetchone()
    conn.close()
    return dict(row) if row else None


def auth_actions() -> list[tuple]:
    conn = sqlite3.connect(DB, timeout=15)
    rows = conn.execute(
        "SELECT mode, action, detail FROM auth_log ORDER BY id"
    ).fetchall()
    conn.close()
    return rows


def clear_cache() -> None:
    conn = sqlite3.connect(DB, timeout=15)
    conn.execute("PRAGMA busy_timeout=15000")
    conn.execute("DELETE FROM cache WHERE id = 1")
    conn.commit()
    conn.close()


def dns_query() -> tuple[int, list[tuple], str | None]:
    """Returns (rcode, [(type, data, ttl)], source)."""
    q = DNSRecord.question(DOMAIN, "A")
    data = q.send("127.0.0.1", RES_PORT, timeout=8)
    rep = DNSRecord.parse(data)
    answers = [(QTYPE[rr.rtype], str(rr.rdata), rr.ttl) for rr in rep.rr]
    src = None
    for rr in rep.ar:
        if rr.rtype == QTYPE.TXT:
            txt = str(rr.rdata).strip('"')
            if txt.startswith("source="):
                src = txt.split("=", 1)[1].split()[0]
    return rep.header.rcode, answers, src


def auth_query(timeout: float = 6.0) -> tuple[float, str | None]:
    """Query the authoritative endpoint directly.

    Returns (elapsed_seconds, answered_ip).  A dedicated socket means a late
    datagram cannot be mistaken for this request's answer.
    """
    q = DNSRecord.question(DOMAIN, "A")
    t0 = time.time()
    data = q.send("127.0.0.1", AUTH_PORT, timeout=timeout)
    rep = DNSRecord.parse(data)
    ip = str(rep.rr[0].rdata) if rep.rr else None
    return time.time() - t0, ip


def count_auth_replies(wait: float) -> int:
    """Send one auth query and count every datagram received within wait."""
    q = DNSRecord.question(DOMAIN, "A")
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(0.2)
    sock.sendto(q.pack(), ("127.0.0.1", AUTH_PORT))
    n = 0
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        try:
            sock.recvfrom(4096)
            n += 1
        except socket.timeout:
            pass
    sock.close()
    return n


def http_post(path: str, body: dict) -> tuple[int, dict]:
    req = urllib.request.Request(
        f"http://127.0.0.1:{WEB_PORT}{path}",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode())


def check(name: str, cond: bool, info: str = "") -> None:
    print(f"  [{'PASS' if cond else 'FAIL'}] {name} {info}", flush=True)
    if not cond:
        FAILS.append(name)


def sleep_until(ts: float) -> None:
    time.sleep(max(0.0, ts - time.time()))


def wait_resolver_ready(timeout: float = 25) -> bool:
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            dns_query()
            return True
        except Exception:
            time.sleep(0.3)
    return False


def wait_web_ready(timeout: float = 25) -> bool:
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{WEB_PORT}/api/state", timeout=2
            ) as r:
                if r.status == 200:
                    return True
        except Exception:
            time.sleep(0.3)
    return False


# --------------------------------------------------------------------------
# scenario
# --------------------------------------------------------------------------
def main() -> int:
    print(f"[selftest] workdir={TMP}", flush=True)
    start("auth", "app.auth_server")
    start("resolver", "app.resolver")
    start("web", "app.web")
    try:
        if not (wait_resolver_ready() and wait_web_ready()):
            print("  [FAIL] services did not come up")
            return 1

        # -- A. basic fetch / fresh cache / refresh --------------------------
        print("A. 基本应答与新鲜缓存", flush=True)
        clear_cache()  # the readiness probe already populated the cache
        set_mode("a", "192.0.2.10")
        rc, ans, src = dns_query()
        check("A1 上游取回 A", rc == 0 and ans == [("A", "192.0.2.10", 5)]
              and src == "upstream", f"rcode={rc} ans={ans} src={src}")
        rc, ans, src = dns_query()
        check("A2 命中新鲜缓存", rc == 0 and src == "cache-fresh"
              and ans and ans[0][1] == "192.0.2.10" and 1 <= ans[0][2] <= 5,
              f"ans={ans} src={src}")
        set_mode("a", "192.0.2.20")  # change the A record on the auth side
        rc, ans, src = dns_query()
        check("A3 缓存期内仍给旧 IP", rc == 0 and src == "cache-fresh"
              and ans and ans[0][1] == "192.0.2.10", f"ans={ans} src={src}")
        time.sleep(5.5)
        rc, ans, src = dns_query()
        check("A4 过期后重新取回新 IP", rc == 0 and src == "upstream"
              and ans == [("A", "192.0.2.20", 5)], f"ans={ans} src={src}")
        cache = get_cache()
        fresh_until, stale_until = cache["fresh_until"], cache["stale_until"]

        # -- B. stale on timeout, SERVFAIL beyond the window -----------------
        print("B. 过期+上游超时 -> 旧 A 沿用 <=20s -> SERVFAIL", flush=True)
        set_mode("timeout")
        sleep_until(fresh_until + 0.5)
        t0 = time.time()
        rc, ans, src = dns_query()
        check("B1 上游超时后给旧 A(stale)",
              rc == 0 and src == "cache-stale" and ans
              and ans[0][1] == "192.0.2.20"
              and stale_until - time.time() - 1 <= ans[0][2] <= 20,
              f"ans={ans} src={src} 耗时={time.time()-t0:.1f}s")
        sleep_until(stale_until - 4)
        rc, ans, src = dns_query()
        check("B2 窗口末尾仍是 stale", rc == 0 and src == "cache-stale",
              f"ans={ans} src={src}")
        sleep_until(stale_until + 1)
        rc, ans, src = dns_query()
        check("B3 超过 20s 返回 SERVFAIL", rc == 2 and not ans and src == "none",
              f"rcode={rc} ans={ans} src={src}")

        # -- C. NXDOMAIN purges old A; NXDOMAIN never served stale -----------
        print("C. 确认 NXDOMAIN 清除旧 A；NXDOMAIN 不作过期备用", flush=True)
        set_mode("a", "192.0.2.30")
        rc, ans, src = dns_query()
        assert rc == 0 and src == "upstream", f"setup failed: {rc} {src}"
        set_mode("nxdomain")
        time.sleep(5.5)  # let the fresh A expire
        rc, ans, src = dns_query()
        check("C1 上游确认 NXDOMAIN", rc == 3 and not ans and src == "upstream",
              f"rcode={rc} ans={ans} src={src}")
        cache = get_cache()
        check("C2 旧 A 已被持久清除",
              cache["rcode"] == "NXDOMAIN" and cache["a_record"] is None
              and cache["stale_until"] is None,
              f"cache={cache}")
        set_mode("timeout")
        time.sleep(5.5)  # let the fresh NXDOMAIN expire
        rc, ans, src = dns_query()
        check("C3 NXDOMAIN 不作 stale 备用 -> SERVFAIL",
              rc == 2 and not ans and src == "none",
              f"rcode={rc} ans={ans} src={src}")

        # -- D. late old answer cannot resurrect -----------------------------
        print("D. 超时后迟到的旧答复不能复活记录", flush=True)
        set_mode("a", "192.0.2.40")
        rc, ans, src = dns_query()
        assert rc == 0 and src == "upstream", f"setup failed: {rc} {src}"
        set_mode("delay", "192.0.2.40", 3.0)  # 3s delay > 2s upstream timeout
        time.sleep(5.5)  # fresh A expires
        t0 = time.time()
        rc, ans, src = dns_query()  # upstream times out at 2s; delayed A lands ~1s later
        check("D1 延迟模式下先按超时处理 -> stale",
              rc == 0 and src == "cache-stale" and 1.8 <= time.time() - t0 <= 4,
              f"ans={ans} src={src} 耗时={time.time()-t0:.1f}s")
        time.sleep(1.5)  # the delayed A has now arrived at a closed socket
        set_mode("nxdomain")
        rc, ans, src = dns_query()
        check("D2 随后确认 NXDOMAIN", rc == 3 and src == "upstream",
              f"rcode={rc} src={src}")
        set_mode("timeout")
        time.sleep(5.5)  # past NXDOMAIN freshness; delayed A long arrived
        rc, ans, src = dns_query()
        check("D3 迟到旧 A 未复活任何记录 -> SERVFAIL",
              rc == 2 and not ans, f"rcode={rc} ans={ans} src={src}")
        cache = get_cache()
        check("D4 缓存中仍无 A 记录", cache["a_record"] is None,
              f"cache={cache}")
        delayed = [r for r in auth_actions() if r[0] == "delay" and r[1] == "answered"]
        check("D5 权威端确实发出过延迟应答（被解析器丢弃）", len(delayed) >= 1,
              f"delay 应答次数={len(delayed)}")

        # -- E. restart does not extend validity -----------------------------
        print("E. 重启不延长旧 A 有效期", flush=True)
        set_mode("a", "192.0.2.50")
        rc, ans, src = dns_query()
        assert rc == 0 and src == "upstream", f"setup failed: {rc} {src}"
        stale_until = get_cache()["stale_until"]
        time.sleep(4)
        stop("resolver")
        time.sleep(1)
        start("resolver", "app.resolver")  # restart with the same DB
        set_mode("timeout")
        assert wait_resolver_ready(), "resolver did not come back"
        sleep_until(stale_until - 4)
        rc, ans, src = dns_query()
        check("E1 重启后窗口内仍可用旧 A(绝对期限未变)",
              rc == 0 and src == "cache-stale" and ans
              and ans[0][1] == "192.0.2.50", f"ans={ans} src={src}")
        sleep_until(stale_until + 2)
        rc, ans, src = dns_query()
        check("E2 到达原绝对期限即 SERVFAIL（未被重启延长）",
              rc == 2 and not ans, f"rcode={rc} ans={ans} src={src}")

        # -- F. web console smoke test ---------------------------------------
        print("F. 网页控制台冒烟", flush=True)
        with urllib.request.urlopen(f"http://127.0.0.1:{WEB_PORT}/", timeout=5) as r:
            body = r.read().decode()
        check("F1 主页可访问", r.status == 200 and "DNS" in body)
        req = urllib.request.Request(
            f"http://127.0.0.1:{WEB_PORT}/api/mode",
            data=json.dumps({"mode": "a", "ip": "192.0.2.60"}).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=5) as r:
            ok = json.load(r)["ok"]
        check("F2 网页切换模式生效", ok and get_control_mode() == "a")
        req = urllib.request.Request(f"http://127.0.0.1:{WEB_PORT}/api/query",
                                     method="POST")
        with urllib.request.urlopen(req, timeout=15) as r:
            res = json.load(r)
        check("F3 网页发起查询并显示来源",
              res["ok"] and res["rcode"] == "NOERROR"
              and res["source"] in ("upstream", "cache-fresh")
              and res["answers"] and res["answers"][0]["data"] == "192.0.2.60",
              f"res={res.get('rcode')} src={res.get('source')}")
        with urllib.request.urlopen(
                f"http://127.0.0.1:{WEB_PORT}/api/state", timeout=5) as r:
            st = json.load(r)
        check("F4 状态接口含控制/缓存/日志",
              st["control"]["mode"] == "a" and st["cache"]
              and len(st["resolver_log"]) > 0 and len(st["auth_log"]) > 0)
        req = urllib.request.Request(
            f"http://127.0.0.1:{WEB_PORT}/api/mode",
            data=json.dumps({"mode": "bogus"}).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        try:
            urllib.request.urlopen(req, timeout=5)
            check("F5 非法模式被拒绝", False)
        except urllib.error.HTTPError as e:
            check("F5 非法模式被拒绝", e.code == 400)

        # -- G. delay snapshot at receive; in-flight edits have no effect ----
        print("G. 延迟应答在收包时快照地址与延迟；等待中改配置不影响在途请求",
              flush=True)
        clear_cache()
        set_mode("delay", "192.0.2.70", 3.0)
        result = {}

        def g_worker():
            result["elapsed"], result["ip"] = auth_query(timeout=8)

        th = threading.Thread(target=g_worker)
        th.start()
        time.sleep(0.4)  # the auth side has received the packet and is waiting
        # while the request is still in flight: shorten the delay and change
        # the address.  The committed request must ignore both edits.
        set_mode("delay", "192.0.2.71", 0.5)
        th.join()
        check("G1 在途请求沿用提交时的 3s 延迟",
              2.8 <= result["elapsed"] <= 3.4, f"耗时={result['elapsed']:.2f}s")
        check("G2 在途请求仍答提交时的旧地址", result["ip"] == "192.0.2.70",
              f"ip={result['ip']}")
        # a subsequently received packet uses the newly committed values,
        # with the 0.5s measured from its own receive time
        el, ip = auth_query(timeout=5)
        check("G3 新请求按新延迟 0.5s（从收包时刻起算）", 0.4 <= el <= 1.0,
              f"耗时={el:.2f}s")
        check("G4 新请求返回新地址", ip == "192.0.2.71", f"ip={ip}")
        n = count_auth_replies(wait=4.0)
        check("G5 到点只答一次", n == 1, f"收到应答数={n}")
        # the resolver's cache/timeout rules are untouched: a 3s delayed
        # upstream answer still exceeds the 2s upstream timeout
        set_mode("a", "192.0.2.72")
        rc, ans, src = dns_query()
        assert rc == 0 and src == "upstream", f"setup failed: {rc} {src}"
        set_mode("delay", "192.0.2.73", 3.0)
        time.sleep(5.5)  # fresh A expires
        t0 = time.time()
        rc, ans, src = dns_query()  # upstream timeout 2s; delayed answer lands late
        check("G6 解析器仍按原回源超时规则给 stale",
              rc == 0 and src == "cache-stale"
              and 1.8 <= time.time() - t0 <= 4, f"src={src} 耗时={time.time()-t0:.1f}s")

        # -- H. field-level optimistic concurrency + legacy compatibility ----
        print("H. 字段级乐观锁：局部应用/不同页分别生效/冲突整次拒绝", flush=True)
        set_mode("a", "192.0.2.80", 3.0)
        # two pages editing different fields both commit
        code, r = http_post("/api/control/apply", {
            "changes": {"delay_seconds": 0.5},
            "expected": {"delay_seconds": 3.0}})
        check("H1 只提交本页改动（延迟），其余项保留",
              code == 200 and r["ok"] and r["control"]["delay_seconds"] == 0.5
              and r["control"]["mode"] == "a"
              and r["control"]["ip"] == "192.0.2.80", str(r))
        code, r = http_post("/api/control/apply", {
            "changes": {"mode": "delay"}, "expected": {"mode": "a"}})
        check("H2 另一页改模式同样生效", code == 200 and r["ok"]
              and r["control"]["mode"] == "delay"
              and r["control"]["delay_seconds"] == 0.5, str(r))
        # conflict: effective 0.5 differs from both stale baseline 3.0 and
        # this page's proposal 9.0 -> whole transaction rejected, nothing written
        code, r = http_post("/api/control/apply", {
            "changes": {"delay_seconds": 9.0},
            "expected": {"delay_seconds": 3.0}})
        check("H3 冲突整次拒绝并回显冲突项生效值",
              code == 409 and not r["ok"] and r["error"] == "conflict"
              and r["conflicts"] == {"delay_seconds": 0.5}
              and r["control"]["delay_seconds"] == 0.5, str(r))
        # one conflicting field rejects the whole multi-field submission
        before = common_get_control()
        code, r = http_post("/api/control/apply", {
            "changes": {"mode": "timeout", "ip": "10.9.9.9"},
            "expected": {"mode": "nxdomain", "ip": "192.0.2.80"}})
        check("H4 多字段中一项冲突则全部不写入",
              code == 409 and list(r["conflicts"]) == ["mode"]
              and common_get_control() == before, str(r))
        # convergence: proposal equal to the new effective value is accepted
        code, r = http_post("/api/control/apply", {
            "changes": {"delay_seconds": 0.5},
            "expected": {"delay_seconds": 3.0}})
        check("H5 拟存值与生效值一致时不算冲突", code == 200 and r["ok"], str(r))
        # legacy endpoint: missing fields filled at submit time; full config back
        code, r = http_post("/api/mode", {"mode": "a"})
        check("H6 旧 /api/mode 缺省项按生效值补齐并返回完整配置",
              code == 200 and r["ok"] and r["control"]["mode"] == "a"
              and r["control"]["ip"] == "192.0.2.80"
              and r["control"]["delay_seconds"] == 0.5
              and {"id", "mode", "ip", "delay_seconds", "updated_at"}
              <= set(r["control"]), str(r))
        code, r = http_post("/api/control/apply", {
            "changes": {"ip": "not-an-ip"},
            "expected": {"ip": "192.0.2.80"}})
        check("H7 新接口非法值被拒绝", code == 400 and not r["ok"], str(r))
    finally:
        for name in list(procs):
            stop(name)
        print(f"[selftest] logs in {TMP}", flush=True)

    print(f"\n{'=' * 60}\n结果: {len(FAILS)} 项失败" if FAILS
          else f"\n{'=' * 60}\n结果: 全部通过", flush=True)
    return 1 if FAILS else 0


def get_control_mode() -> str:
    conn = sqlite3.connect(DB, timeout=15)
    mode = conn.execute("SELECT mode FROM control WHERE id = 1").fetchone()[0]
    conn.close()
    return mode


def common_get_control() -> dict:
    conn = sqlite3.connect(DB, timeout=15)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM control WHERE id = 1").fetchone()
    conn.close()
    return dict(row)


if __name__ == "__main__":
    sys.exit(main())
