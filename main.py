
# main.py
# ESP32 MicroPython: Wi-Fi AP + DNS catch-all + HTTP server
# Serves index.html, /angle, /recalibrate, and OS connectivity checks.
# Requires reader.py (AngleTracker) and index.html in the filesystem.

import network
import uasyncio as asyncio
import socket
from machine import I2C, Pin
import utime

from reader import AngleTracker

# ---------- CONFIG ----------
SSID = "ESP32-Angle"
PASSWORD = "angle1234"

AP_IP = "192.168.4.1"
NETMASK = "255.255.255.0"
GATEWAY = "192.168.4.1"
DNS_IP = "8.8.8.8"

ANGLE_MODE = "PITCH"     # "PITCH" or "ROLL"
I2C_ID = 0
I2C_SCL_PIN = 22
I2C_SDA_PIN = 21
I2C_FREQ_HZ = 400_000
READ_PERIOD_MS = 100      # sensor refresh cadence for background task

# ---------- Angle tracker ----------
i2c = I2C(I2C_ID, scl=Pin(I2C_SCL_PIN), sda=Pin(I2C_SDA_PIN), freq=I2C_FREQ_HZ)
tracker = AngleTracker(i2c, angle_mode=ANGLE_MODE, calibration_delay_ms=1500)

print("Calibrating... keep sensor still.")
if tracker.recalibrate():
    print("Calibrated.")
else:
    print("Calibration failed (sensor read). Using default reference.")

# ---------- Wi-Fi AP ----------
ap = network.WLAN(network.AP_IF)
ap.active(True)
# WPA2 PSK
ap.config(essid=SSID, password=PASSWORD, authmode=network.AUTH_WPA_WPA2_PSK)
# Set static IP for the AP
ap.ifconfig((AP_IP, NETMASK, GATEWAY, DNS_IP))
print("AP started:", ap.ifconfig())

# ---------- HTML loader (index.html) ----------
_INDEX_HTML = None
def load_index_html():
    global _INDEX_HTML
    if _INDEX_HTML is None:
        try:
            with open("index.html", "r") as f:   # MicroPython: avoid encoding kwarg
                _INDEX_HTML = f.read()
        except Exception:
            _INDEX_HTML = "<h1>index.html missing</h1>"
    # Inject placeholders
    return (_INDEX_HTML
            .replace("{{MODE}}", ANGLE_MODE)
            .replace("{{SSID}}", SSID))

# ---------- Minimal DNS catch-all (answers every A query with AP_IP) ----------
async def dns_catch_all(ip=AP_IP):
    # UDP port 53
    addr = socket.getaddrinfo("0.0.0.0", 53)[0][-1]
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        try:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        except Exception:
            pass
        s.bind(addr)
        s.setblocking(False)
        ip_bytes = bytes(int(x) & 0xFF for x in ip.split("."))
        while True:
            try:
                data, cli = s.recvfrom(512)
                if not data or len(data) < 12:
                    await asyncio.sleep_ms(1)
                    continue
                # Transaction ID
                tid = data[0:2]
                # Flags: standard response, no error
                flags = b"\x81\x80"
                qdcount = data[4:6]     # echo original question count
                ancount = b"\x00\x01"   # 1 answer
                nscount = b"\x00\x00"
                arcount = b"\x00\x00"
                header = tid + flags + qdcount + ancount + nscount + arcount
                # Question section: copy as-is
                question = data[12:]
                # Answer: name pointer to offset 12 (0xC00C), type A, class IN, TTL 60, RDLEN 4, RDATA ip
                answer = b"\xC0\x0C" + b"\x00\x01" + b"\x00\x01" + b"\x00\x00\x00\x3C" + b"\x00\x04" + ip_bytes
                s.sendto(header + question + answer, cli)
            except OSError:
                await asyncio.sleep_ms(2)
    finally:
        try:
            s.close()
        except Exception:
            pass

# ---------- HTTP server ----------
async def send_response(writer, status, ctype, body):
    reason = {200:"OK", 204:"No Content", 404:"Not Found", 500:"Internal Server Error"}.get(status, "OK")
    # body must be str
    if not isinstance(body, str):
        body = str(body)
    hdr = (
        "HTTP/1.1 {} {}\r\n"
        "Content-Type: {}\r\n"
        "Content-Length: {}\r\n"
        "Cache-Control: no-store\r\n"
        "Connection: close\r\n\r\n"
    ).format(status, reason, ctype, len(body))
    await writer.awrite(hdr)
    if body and status != 204:
        await writer.awrite(body)

async def handle_client(reader, writer):
    try:
        req = await reader.readline()
        if not req:
            await writer.aclose(); return

        try:
            line = req.decode()
        except Exception:
            line = ""
        parts = line.split()
        method = parts[0] if len(parts) >= 1 else ""
        path = parts[1] if len(parts) >= 2 else "/"

        # Read headers (basic)
        content_length = 0
        host_hdr = ""
        while True:
            h = await reader.readline()
            if not h or h == b"\r\n":
                break
            hl = h.decode().strip()
            if hl.lower().startswith("content-length:"):
                try:
                    content_length = int(hl.split(":",1)[1].strip())
                except Exception:
                    content_length = 0
            if hl.lower().startswith("host:"):
                host_hdr = hl[5:].strip()

        if content_length:
            # consume body if present (we don't use it)
            try:
                await reader.readexactly(content_length)
            except Exception:
                pass

        # ---------- Connectivity check handlers (keep devices connected) ----------
        # Android
        if method == "GET" and path in ("/generate_204", "/gen_204"):
            await send_response(writer, 204, "text/plain; charset=utf-8", "")
        # iOS/macOS
        elif method == "GET" and path in ("/hotspot-detect.html", "/library/test/success.html"):
            await send_response(writer, 200, "text/html; charset=utf-8", "Success")
        # Windows NCSI
        elif method == "GET" and path in ("/connecttest.txt", "/ncsi.txt"):
            txt = "Microsoft Connect Test" if path.endswith("connecttest.txt") else "Microsoft NCSI"
            await send_response(writer, 200, "text/plain; charset=utf-8", txt)
        # Generic root page
        elif method == "GET" and (path == "/" or path.startswith("/index.html")):
            await send_response(writer, 200, "text/html; charset=utf-8", load_index_html())
        # Angle endpoint
        elif method == "GET" and path == "/angle":
            d = tracker.get_delta()
            body = "--.--" if d is None else f"{d:+.2f}"
            await send_response(writer, 200, "text/plain; charset=utf-8", body)
        # Recalibrate endpoint
        elif path == "/recalibrate" and method in ("POST", "GET"):
            ok = tracker.recalibrate()
            await send_response(writer, 200, "text/plain; charset=utf-8", "OK" if ok else "ERR")
        else:
            await send_response(writer, 404, "text/plain; charset=utf-8", "Not found")

    except Exception:
        try:
            await send_response(writer, 500, "text/plain; charset=utf-8", "Server error")
        except Exception:
            pass
    finally:
        try:
            await writer.aclose()
        except Exception:
            pass

async def periodic_read():
    # Keep last_delta fresh even without clients
    while True:
        tracker.get_delta()
        await asyncio.sleep_ms(READ_PERIOD_MS)

async def main():
    # Start background tasks
    asyncio.create_task(periodic_read())
    asyncio.create_task(dns_catch_all(AP_IP))
    # HTTP server
    srv = await asyncio.start_server(handle_client, "0.0.0.0", 80)
    print("HTTP server on http://{}/".format(AP_IP))
    # uasyncio doesn't expose serve_forever; idle-sleep forever
    while True:
        await asyncio.sleep(3600)

try:
    asyncio.run(main())
finally:
    # Clean loop for soft reboot friendliness
    asyncio.new_event_loop()
