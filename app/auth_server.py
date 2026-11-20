"""Local authoritative endpoint for the fixed domain.

Its behaviour is switched at runtime from the web console via the shared
control table.  The control row is read exactly once per received packet and
the whole snapshot (mode / ip / delay) is committed to that request:

  a        -- answer the snapshot's A record (TTL = FRESH_TTL)
  nxdomain -- answer NXDOMAIN
  delay    -- answer the snapshot's A record exactly once, after
              ``delay_seconds`` measured from the moment this packet arrived
  timeout  -- never answer (simulates the authoritative endpoint being lost)

Because the snapshot is taken at receive time, editing the control row while
a delayed request is still waiting has no effect on the in-flight request:
it keeps the address and the delay that were committed when the packet was
received, and the new values only apply to subsequently received packets.

Every received query is logged to auth_log so the console can show what the
upstream actually saw and did.
"""
from __future__ import annotations

import socket
import threading
import time
import traceback

from dnslib import A, QTYPE, RCODE, RR, DNSRecord

from . import common


def handle(data: bytes, addr, sock: socket.socket) -> None:
    qname = qtype = "?"
    mode = "?"
    try:
        recv_mono = time.monotonic()
        query = DNSRecord.parse(data)
        qname = str(query.q.qname)
        qtype = QTYPE[query.q.qtype]

        # Snapshot the committed control row once, at receive time.  The rest
        # of this request never re-reads the table, so concurrent edits cannot
        # change the address/delay this request will use.
        ctl = common.get_control() or {
            "mode": "a", "ip": common.DEFAULT_IP, "delay_seconds": common.DEFAULT_DELAY
        }
        mode = ctl["mode"]
        snap_mode = ctl["mode"]
        snap_ip = ctl["ip"]
        snap_delay = max(0.0, float(ctl["delay_seconds"]))

        in_scope = (
            str(query.q.qname).rstrip(".").lower() == common.FIXED_DOMAIN.lower()
            and query.q.qtype in (QTYPE.A, QTYPE.ANY)
        )
        if not in_scope:
            rep = query.reply()
            rep.header.rcode = RCODE.NXDOMAIN
            sock.sendto(rep.pack(), addr)
            common.log_auth(qname, qtype, snap_mode, "nxdomain",
                            "out-of-scope name/qtype")
            return

        if snap_mode == "timeout":
            common.log_auth(qname, qtype, snap_mode, "dropped",
                            "simulating lost authoritative endpoint")
            return  # no answer at all -> the resolver's upstream times out

        if snap_mode == "delay":
            common.log_auth(
                qname, qtype, snap_mode, "delayed",
                f"snapshot at receive: A={snap_ip} delay={snap_delay}s"
                f" (counting from receive time)",
            )
            # Measured from this packet's receive moment; config edits during
            # the wait do not touch snap_delay, so the answer fires once here.
            time.sleep(max(0.0, snap_delay - (time.monotonic() - recv_mono)))

        if snap_mode == "nxdomain":
            rep = query.reply()
            rep.header.rcode = RCODE.NXDOMAIN
            sock.sendto(rep.pack(), addr)
            common.log_auth(qname, qtype, snap_mode, "nxdomain",
                            "confirmed negative")
            return

        # modes "a" and "delay": answer exactly once with the snapshot address
        rep = query.reply()
        rep.add_answer(
            RR(common.FQDN, QTYPE.A, rdata=A(snap_ip), ttl=int(common.FRESH_TTL))
        )
        sock.sendto(rep.pack(), addr)
        waited = time.monotonic() - recv_mono
        if snap_mode == "delay":
            common.log_auth(
                qname, qtype, snap_mode, "answered",
                f"A={snap_ip} answered once after {waited:.2f}s"
                f" (snapshot delay {snap_delay}s, from receive time)",
            )
        else:
            common.log_auth(qname, qtype, snap_mode, "answered", f"A={snap_ip}")
    except Exception:
        common.log_auth(qname, qtype, mode, "error", traceback.format_exc(limit=3))


def main() -> None:
    common.init_db()
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("0.0.0.0", common.AUTH_PORT))
    print(f"[auth] authoritative for {common.FQDN} on udp/{common.AUTH_PORT}",
          flush=True)
    while True:
        data, addr = sock.recvfrom(4096)
        threading.Thread(target=handle, args=(data, addr, sock), daemon=True).start()


if __name__ == "__main__":
    main()
