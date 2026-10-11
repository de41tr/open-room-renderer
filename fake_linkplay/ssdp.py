"""SSDP discovery responder.

The service listens for M-SEARCH requests and periodically advertises itself
with NOTIFY packets. Keep response fields conservative because discovery is
usually the first thing to regress.
"""

from __future__ import annotations

import asyncio
import logging
import socket
import struct
from dataclasses import dataclass
from email.utils import formatdate

from .config import Config

LOG = logging.getLogger(__name__)
SSDP_ADDR = "239.255.255.250"
SSDP_PORT = 1900
SERVER = "Linux/6.x UPnP/1.0 OpenRoomRenderer/0.1"
STS = (
    "ssdp:wiimudevice",
    "upnp:rootdevice",
    "urn:schemas-upnp-org:device:MediaRenderer:1",
    "urn:schemas-upnp-org:service:AVTransport:1",
    "urn:schemas-upnp-org:service:RenderingControl:1",
    "urn:schemas-upnp-org:service:ConnectionManager:1",
    "urn:schemas-wiimu-com:service:PlayQueue:1",
)


def usn(cfg: Config, st: str) -> str:
    """Return the stable USN value for a search/notify target."""

    base = f"uuid:{cfg.upnp_uuid}"
    return base if st.lower() == base.lower() else f"{base}::{st}"


def response_packet(cfg: Config, st: str) -> bytes:
    """Build an SSDP M-SEARCH response packet for ``st``."""

    lines = [
        "HTTP/1.1 200 OK",
        "CACHE-CONTROL: max-age=1800",
        f"DATE: {formatdate(usegmt=True)}",
        "EXT:",
        f"LOCATION: {cfg.description_url}",
        f"SERVER: {SERVER}",
        f"ST: {st}",
        f"USN: {usn(cfg, st)}",
        "DEVICE-SOURCE: SSDP",
        "",
        "",
    ]
    if cfg.wiim_home_profile and st == "urn:schemas-upnp-org:device:MediaRenderer:1":
        lines.insert(5, f"Bootid.upnp.org: {cfg.bootid}")
        lines.insert(6, f"ServiceName: ##hexStr##{cfg.linkplay_instance_name.encode('utf-8').hex().upper()}")
    return "\r\n".join(lines).encode()


def notify_packet(cfg: Config, nt: str, nts: str = "ssdp:alive") -> bytes:
    """Build an SSDP NOTIFY packet for an advertised target."""

    lines = [
        "NOTIFY * HTTP/1.1",
        f"HOST: {SSDP_ADDR}:{SSDP_PORT}",
        "CACHE-CONTROL: max-age=1800",
        f"LOCATION: {cfg.description_url}",
        f"NT: {nt}",
        f"NTS: {nts}",
        f"SERVER: {SERVER}",
        f"USN: {usn(cfg, nt)}",
        "DEVICE-SOURCE: SSDP",
        "",
        "",
    ]
    return "\r\n".join(lines).encode()


@dataclass
class SsdpServer:
    """UDP multicast SSDP listener and periodic advertiser."""

    cfg: Config
    sock: socket.socket | None = None
    tasks: list[asyncio.Task[None]] | None = None

    async def start(self) -> None:
        """Join the SSDP multicast group and start listener/notify tasks."""

        self.tasks = []
        try:
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
            self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
            except OSError:
                pass
            self.sock.bind(("", SSDP_PORT))
            mreq = struct.pack("4s4s", socket.inet_aton(SSDP_ADDR), socket.inet_aton("0.0.0.0"))
            self.sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
            self.sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
            self.sock.setblocking(False)
        except OSError as exc:
            LOG.error("SSDP multicast setup failed: %s", exc)
            return
        loop = asyncio.get_running_loop()
        self.tasks.append(loop.create_task(self._listen()))
        self.tasks.append(loop.create_task(self._notify_loop()))
        LOG.info("SSDP listening on udp://%s:%s advertising %s", SSDP_ADDR, SSDP_PORT, self.cfg.description_url)

    async def stop(self) -> None:
        """Cancel SSDP tasks and close the UDP socket."""

        for task in self.tasks or []:
            task.cancel()
        if self.sock:
            self.sock.close()

    async def _listen(self) -> None:
        """Handle incoming M-SEARCH datagrams and send matching responses."""

        assert self.sock is not None
        loop = asyncio.get_running_loop()
        while True:
            data, addr = await loop.sock_recvfrom(self.sock, 65535)
            text = data.decode(errors="ignore")
            if "M-SEARCH" not in text.upper():
                continue
            st = self._search_target(text)
            LOG.info("SSDP M-SEARCH from %s st=%s", addr, st)
            targets = self._targets_for_search(st)
            if not targets:
                LOG.info("SSDP ignoring unsupported search target st=%s", st)
                continue
            for target in targets:
                await loop.sock_sendto(self.sock, response_packet(self.cfg, target), addr)

    async def _notify_loop(self) -> None:
        """Broadcast periodic ssdp:alive NOTIFY packets."""

        assert self.sock is not None
        loop = asyncio.get_running_loop()
        while True:
            for target in self._advertised_targets():
                await loop.sock_sendto(self.sock, notify_packet(self.cfg, target), (SSDP_ADDR, SSDP_PORT))
            await asyncio.sleep(30)

    @staticmethod
    def _search_target(text: str) -> str:
        """Extract the ST header from an SSDP request."""

        for line in text.splitlines():
            if line.lower().startswith("st:"):
                return line.split(":", 1)[1].strip()
        return ""

    def _advertised_targets(self) -> tuple[str, ...]:
        """Include the UUID target required for each advertised UPnP device."""

        return (*STS, f"uuid:{self.cfg.upnp_uuid}")

    def _targets_for_search(self, st: str) -> tuple[str, ...]:
        """Map a requested search target to advertised targets to return."""

        st = st.strip()
        own_uuid = f"uuid:{self.cfg.upnp_uuid}"
        if st.lower() == own_uuid.lower():
            return (own_uuid,)
        if st == "ssdp:all":
            return self._advertised_targets()
        if st == "upnp:rootdevice":
            return ("upnp:rootdevice",)
        if st == "ssdp:wiimudevice":
            return (
                "ssdp:wiimudevice",
                "urn:schemas-upnp-org:device:MediaRenderer:1",
            )
        return tuple(x for x in STS if x == st)
