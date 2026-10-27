"""
upnp_ports.py - automatyczne otwieranie portow UDP audio WebRTC na routerze
przez UPnP IGD (np. Funbox Orange nie pozwala recznie przekierowac zakresu
portow, a UPnP obsluguje).

Odnajduje router w sieci lokalnej (SSDP), w jego opisie urzadzenia szuka
uslugi WANIPConnection/WANPPPConnection i prosi (SOAP AddPortMapping) o
przekierowanie kazdego portu z zakresu WEBRTC_UDP_PORTS na ten komputer.
Przekierowania maja czas waznosci i sa odnawiane (refresh_loop) - po
restarcie routera wracaja same. Czysty Python (socket + urllib), wolany w
osobnym watku (asyncio.to_thread): nic nie blokuje petli i nie przechodzi
przez podmieniony create_datagram_endpoint z webrtc_audio.
"""
import asyncio
import socket
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

_SSDP_ADDR = ("239.255.255.250", 1900)
_SEARCH_TARGETS = (
    "urn:schemas-upnp-org:device:InternetGatewayDevice:1",
    "urn:schemas-upnp-org:device:InternetGatewayDevice:2",
    "urn:schemas-upnp-org:service:WANIPConnection:1",
    "urn:schemas-upnp-org:service:WANPPPConnection:1",
)
_SERVICE_TYPES = (
    "urn:schemas-upnp-org:service:WANIPConnection:2",
    "urn:schemas-upnp-org:service:WANIPConnection:1",
    "urn:schemas-upnp-org:service:WANPPPConnection:1",
)
LEASE_S = 3600          # czas waznosci przekierowania
REFRESH_S = 20 * 60     # odnawianie co 20 min (przed wygasnieciem)
_DESC = "HAMCTRL audio"


def _discover(timeout: float = 3.0) -> list[str]:
    """Adresy LOCATION (opis urzadzenia) routerow odpowiadajacych na SSDP."""
    found: list[str] = []
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    try:
        s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
        s.settimeout(timeout)
        for st in _SEARCH_TARGETS:
            msg = ("M-SEARCH * HTTP/1.1\r\n"
                   f"HOST: {_SSDP_ADDR[0]}:{_SSDP_ADDR[1]}\r\n"
                   'MAN: "ssdp:discover"\r\nMX: 2\r\n'
                   f"ST: {st}\r\n\r\n")
            s.sendto(msg.encode(), _SSDP_ADDR)
        while True:
            try:
                data, _ = s.recvfrom(4096)
            except socket.timeout:
                break
            for line in data.decode(errors="ignore").split("\r\n"):
                if line.lower().startswith("location:"):
                    loc = line.split(":", 1)[1].strip()
                    if loc not in found:
                        found.append(loc)
    finally:
        s.close()
    return found


def _find_service(location: str) -> tuple[str, str] | None:
    """(controlURL, serviceType) uslugi przekierowan z opisu urzadzenia."""
    with urllib.request.urlopen(location, timeout=5) as r:
        root = ET.fromstring(r.read())
    ns_strip = lambda t: t.split("}", 1)[-1]
    base = location
    for el in root.iter():
        if ns_strip(el.tag) == "URLBase" and (el.text or "").strip():
            base = el.text.strip()
    services = {}
    for svc in root.iter():
        if ns_strip(svc.tag) != "service":
            continue
        fields = {ns_strip(c.tag): (c.text or "").strip() for c in svc}
        if fields.get("serviceType") in _SERVICE_TYPES and fields.get("controlURL"):
            services[fields["serviceType"]] = urllib.parse.urljoin(base, fields["controlURL"])
    for st in _SERVICE_TYPES:
        if st in services:
            return services[st], st
    return None


def _soap(control_url: str, service: str, action: str, args: dict) -> str:
    body = "".join(f"<{k}>{v}</{k}>" for k, v in args.items())
    envelope = ('<?xml version="1.0"?>'
                '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
                's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/"><s:Body>'
                f'<u:{action} xmlns:u="{service}">{body}</u:{action}>'
                '</s:Body></s:Envelope>')
    req = urllib.request.Request(control_url, data=envelope.encode(), method="POST", headers={
        "Content-Type": 'text/xml; charset="utf-8"',
        "SOAPAction": f'"{service}#{action}"',
    })
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.read().decode(errors="ignore")
    except urllib.error.HTTPError as e:
        # Bledy UPnP przychodza jako HTTP 500 z kodem w tresci (np. 718, 725).
        raise RuntimeError(e.read().decode(errors="ignore")) from None


def _local_ip_for(url: str) -> str:
    host = urllib.parse.urlparse(url).hostname
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((host, 1900))
        return s.getsockname()[0]
    finally:
        s.close()


class UpnpPortMapper:
    def __init__(self, lo: int, hi: int):
        self.lo, self.hi = lo, hi
        self.control_url: str | None = None
        self.service: str | None = None
        self.local_ip: str | None = None
        self.lease = LEASE_S

    def _setup(self) -> bool:
        for loc in _discover():
            try:
                svc = _find_service(loc)
            except Exception:
                continue
            if svc:
                self.control_url, self.service = svc
                self.local_ip = _local_ip_for(self.control_url)
                return True
        return False

    def _map_all(self) -> tuple[int, list[str]]:
        ok, errors = 0, []
        for port in range(self.lo, self.hi + 1):
            args = {
                "NewRemoteHost": "", "NewExternalPort": port, "NewProtocol": "UDP",
                "NewInternalPort": port, "NewInternalClient": self.local_ip,
                "NewEnabled": 1, "NewPortMappingDescription": _DESC,
                "NewLeaseDuration": self.lease,
            }
            try:
                _soap(self.control_url, self.service, "AddPortMapping", args)
                ok += 1
            except Exception as e:
                msg = str(e)
                if "725" in msg and self.lease:
                    # OnlyPermanentLeasesSupported - router przyjmuje tylko stale
                    self.lease = 0
                    return self._map_all()
                errors.append(f"{port}: {msg[:120]}")
        return ok, errors

    def _external_ip(self) -> str:
        try:
            xml = _soap(self.control_url, self.service, "GetExternalIPAddress", {})
            root = ET.fromstring(xml)
            for el in root.iter():
                if el.tag.endswith("NewExternalIPAddress"):
                    return (el.text or "").strip()
        except Exception:
            pass
        return "?"

    async def refresh_loop(self):
        """Otwiera porty przy starcie i odnawia je co REFRESH_S. Nigdy nie
        rzuca - brak routera z UPnP to tylko komunikat w konsoli."""
        announced = False
        while True:
            try:
                if not self.control_url:
                    if not await asyncio.to_thread(self._setup):
                        if not announced:
                            print("[upnp] nie znaleziono routera z UPnP (wlacz UPnP w routerze "
                                  "albo przekieruj porty recznie) - audio dziala jak dotad", flush=True)
                            announced = True
                        await asyncio.sleep(REFRESH_S)
                        continue
                ok, errors = await asyncio.to_thread(self._map_all)
                if not announced or errors:
                    ext = await asyncio.to_thread(self._external_ip)
                    print(f"[upnp] router {urllib.parse.urlparse(self.control_url).hostname}, "
                          f"IP publiczne {ext}: otwarte UDP {ok}/{self.hi - self.lo + 1} portow "
                          f"{self.lo}-{self.hi} -> {self.local_ip}"
                          + (f" (stale przekierowania)" if self.lease == 0 else ""), flush=True)
                    for e in errors[:3]:
                        print(f"[upnp]   blad: {e}", flush=True)
                    announced = True
                if ok == 0:
                    # Router mogl sie zmienic (restart, inny adres) - szukamy od nowa.
                    self.control_url = None
            except asyncio.CancelledError:
                raise
            except Exception as e:
                print(f"[upnp] blad: {e}", flush=True)
                self.control_url = None
            await asyncio.sleep(REFRESH_S if self.lease else 6 * REFRESH_S)
