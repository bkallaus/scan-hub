"""Find Epson network scanners on the local network.

Used when Epson's own discovery (`scanimage -L`) comes up empty. Two cheap
checks, both read-only:
  - ENPC: the UDP broadcast Epson's tools use to find devices (port 3289)
  - a sweep of this host's /24 subnets for Epson's network scan port (1865)
The results are only candidates; the caller confirms each one by opening it
with the SANE driver. Needs host networking: from a Docker bridge network
this only sees the container's own subnet.
"""
import concurrent.futures
import logging
import socket
import time

log = logging.getLogger("netfind")

SCAN_PORT = 1865          # ESC/I-2 network scanning
ENPC_PORT = 3289
ENPC_QUERY = b"EPSONQ\x03\x00\x00\x00\x00\x00\x00"


def local_prefixes():
    """First three octets of each private IPv4 address this host uses."""
    ips = set()
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))  # picks the default route; sends nothing
            ips.add(s.getsockname()[0])
    except OSError:
        pass
    try:
        ips.update(socket.gethostbyname_ex(socket.gethostname())[2])
    except OSError:
        pass
    out = []
    for ip in sorted(ips):
        a, b, *_ = (int(x) for x in ip.split("."))
        if a == 10 or (a == 172 and 16 <= b <= 31) or (a == 192 and b == 168):
            out.append(ip.rsplit(".", 1)[0])
    return sorted(set(out))


def is_enpc_reply(data):
    # Replies echo the query letter in lower case: "EPSONq...".
    return len(data) >= 6 and data.startswith(b"EPSON") and data[5:6].islower()


def enpc_replies(timeout=1.5):
    found = []
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            s.settimeout(0.3)
            s.bind(("", 0))
            s.sendto(ENPC_QUERY, ("255.255.255.255", ENPC_PORT))
            end = time.time() + timeout
            while time.time() < end:
                try:
                    data, (ip, _) = s.recvfrom(4096)
                except socket.timeout:
                    continue
                if is_enpc_reply(data) and ip not in found:
                    found.append(ip)
    except OSError as e:
        log.debug("ENPC broadcast failed: %s", e)
    return found


def _port_open(ip, connect_timeout):
    try:
        with socket.create_connection((ip, SCAN_PORT), timeout=connect_timeout):
            return True
    except OSError:
        return False


def find_scanner_ips(connect_timeout=0.5):
    """Candidate scanner IPs, most likely first."""
    found = enpc_replies()
    hosts = [f"{p}.{i}" for p in local_prefixes() for i in range(1, 255)]
    with concurrent.futures.ThreadPoolExecutor(64) as ex:
        for ip, ok in zip(hosts, ex.map(lambda h: _port_open(h, connect_timeout), hosts)):
            if ok and ip not in found:
                found.append(ip)
    log.info("Network search: %d candidate(s)%s", len(found), f" {found}" if found else "")
    return found
