"""
Meraki Dashboard API collector.

Wraps the official `meraki` Python SDK and returns structured dicts ready for
the syncer.  All API calls are read-only — no write operations are performed.

Supported device families
--------------------------
MS  — switches         (ports, CDP/LLDP neighbours, client MAC table)
MX  — security appliances / SD-WAN  (WAN + LAN ports)
MR  — wireless APs     (radio interfaces)
MG  — cellular gateways
MV  — smart cameras
MT  — sensors
Other Meraki models — basic device record only

Rate limiting
-------------
The Meraki SDK handles back-off automatically.  By default it retries on 429
responses with an exponential back-off.  No additional rate-limiting is needed
in this module.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

import meraki
import meraki.exceptions

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Structured data classes returned by the collector
# ---------------------------------------------------------------------------

@dataclass
class CollectedPort:
    port_id:     str
    name:        str
    description: str = ""
    enabled:     bool = True
    connected:   bool = False
    speed_mbps:  int  = 0          # 0 = unknown
    is_uplink:   bool = False


@dataclass
class CollectedMac:
    mac:     str
    port_id: str
    vlan:    int = 0


@dataclass
class CollectedNeighbour:
    local_port:  str
    remote_id:   str               # remote serial or hostname
    remote_port: str = ""
    protocol:    str = "lldp"      # "cdp" or "lldp"


@dataclass
class CollectedDevice:
    serial:      str
    name:        str
    model:       str
    firmware:    str   = ""
    lan_ip:      str   = ""
    wan1_ip:     str   = ""
    wan2_ip:     str   = ""
    tags:        list[str]              = field(default_factory=list)
    ports:       list[CollectedPort]    = field(default_factory=list)
    macs:        list[CollectedMac]     = field(default_factory=list)
    neighbours:  list[CollectedNeighbour] = field(default_factory=list)

    @property
    def family(self) -> str:
        """Two-letter Meraki model family (MS, MX, MR, …)."""
        return self.model[:2].upper() if self.model else "??"


# ---------------------------------------------------------------------------
# Collector
# ---------------------------------------------------------------------------

class MerakiCollector:
    """
    Thin wrapper around the Meraki Python SDK for device data collection.

    Usage::

        collector = MerakiCollector(api_key="…", timeout=30)
        devices = collector.collect_network("N_xxxxxxxxxxxx")
    """

    def __init__(
        self,
        api_key: str,
        *,
        timeout: int = 30,
        proxy: str = "",
    ) -> None:
        kwargs: dict = dict(
            api_key=api_key,
            output_log=False,
            print_console=False,
            suppress_logging=True,
            single_request_timeout=timeout,
            # Retry on 429 up to 10 times with back-off
            nginx_429_retry_wait_time=2,
            wait_on_rate_limit=True,
        )
        if proxy:
            kwargs["requests_proxy"] = proxy

        self.dashboard = meraki.DashboardAPI(**kwargs)

    # ------------------------------------------------------------------
    # High-level entry point
    # ------------------------------------------------------------------

    def collect_network(
        self,
        network_id: str,
        *,
        client_timespan: int = 86400,
    ) -> list[CollectedDevice]:
        """
        Collect all devices in a Meraki network.

        Parameters
        ----------
        network_id      : Meraki network ID (e.g. "N_xxxxxxxxxxxx")
        client_timespan : look-back window (seconds) for the client list used
                          to build MAC tables.  Default: 24 hours.
        """
        log.info("Meraki: collecting network %s", network_id)

        raw_devices = self._get_network_devices(network_id)
        if not raw_devices:
            log.warning("Meraki: no devices returned for network %s", network_id)
            return []

        # Clients keyed by (serial, portId) for MAC table building
        clients_by_port = self._index_clients(network_id, client_timespan)

        # Topology neighbours (supplement port-status CDP/LLDP)
        topology_nbrs = self._build_topology_index(network_id)

        results: list[CollectedDevice] = []
        for raw in raw_devices:
            try:
                dev = self._collect_device(
                    raw, clients_by_port, topology_nbrs
                )
                results.append(dev)
            except Exception as exc:
                serial = raw.get("serial", "?")
                log.warning("Meraki: failed to collect device %s: %s", serial, exc)

        log.info(
            "Meraki: collected %d device(s) from network %s",
            len(results), network_id,
        )
        return results

    # ------------------------------------------------------------------
    # Per-device collection
    # ------------------------------------------------------------------

    def _collect_device(
        self,
        raw: dict,
        clients_by_port: dict[tuple[str, str], list[dict]],
        topology_nbrs: dict[tuple[str, str], list[dict]],
    ) -> CollectedDevice:
        serial  = raw.get("serial", "")
        model   = raw.get("model", "")
        name    = raw.get("name") or raw.get("serial", "")
        lan_ip  = raw.get("lanIp") or ""
        wan1_ip = raw.get("wan1Ip") or ""
        wan2_ip = raw.get("wan2Ip") or ""
        fw      = raw.get("firmware") or ""
        tags    = raw.get("tags") or []

        dev = CollectedDevice(
            serial=serial,
            name=name,
            model=model,
            firmware=fw,
            lan_ip=lan_ip,
            wan1_ip=wan1_ip,
            wan2_ip=wan2_ip,
            tags=tags,
        )

        family = dev.family
        if family == "MS":
            self._collect_switch(dev, clients_by_port, topology_nbrs)
        elif family == "MX":
            self._collect_appliance(dev)
        elif family == "MR":
            self._collect_ap(dev, raw)
        else:
            # MG, MV, MT, or unknown — create a single management port
            dev.ports.append(CollectedPort(
                port_id="mgmt", name="Management", enabled=True
            ))

        return dev

    def _collect_switch(
        self,
        dev: CollectedDevice,
        clients_by_port: dict[tuple[str, str], list[dict]],
        topology_nbrs: dict[tuple[str, str], list[dict]],
    ) -> None:
        ports    = self._get_switch_ports(dev.serial)
        statuses = {
            str(s["portId"]): s
            for s in self._get_switch_port_statuses(dev.serial)
        }

        for raw_port in ports:
            port_id   = str(raw_port.get("portId", ""))
            status    = statuses.get(port_id, {})
            enabled   = raw_port.get("enabled", True)
            connected = status.get("status", "") == "Connected"

            cp = CollectedPort(
                port_id     = port_id,
                name        = port_id,
                description = raw_port.get("name") or "",
                enabled     = enabled,
                connected   = connected,
                speed_mbps  = _parse_speed_mbps(status.get("speed", "")),
            )
            dev.ports.append(cp)

            # CDP / LLDP neighbours from port status
            for proto, key in (("cdp", "cdpInfo"), ("lldp", "lldpInfo")):
                nbr_data = status.get(key)
                if nbr_data:
                    remote_id = (
                        nbr_data.get("systemName")
                        or nbr_data.get("sourcePort")
                        or ""
                    )
                    remote_port = (
                        nbr_data.get("portId")
                        or nbr_data.get("sourcePort")
                        or ""
                    )
                    if remote_id:
                        dev.neighbours.append(CollectedNeighbour(
                            local_port  = port_id,
                            remote_id   = remote_id,
                            remote_port = remote_port,
                            protocol    = proto,
                        ))

            # MAC table from connected clients
            for client in clients_by_port.get((dev.serial, port_id), []):
                mac = _normalise_mac(client.get("mac", ""))
                if mac:
                    dev.macs.append(CollectedMac(
                        mac     = mac,
                        port_id = port_id,
                        vlan    = client.get("vlan") or 0,
                    ))

        # Topology-based neighbours (fill gaps from port-status CDP/LLDP)
        existing_nbr_keys = {
            (n.local_port, n.remote_id) for n in dev.neighbours
        }
        for (serial, port_id), nbrs in topology_nbrs.items():
            if serial != dev.serial:
                continue
            for nbr in nbrs:
                key = (port_id, nbr["remote_id"])
                if key not in existing_nbr_keys:
                    dev.neighbours.append(CollectedNeighbour(
                        local_port  = port_id,
                        remote_id   = nbr["remote_id"],
                        remote_port = nbr["remote_port"],
                        protocol    = "lldp",
                    ))

    def _collect_appliance(self, dev: CollectedDevice) -> None:
        """MX appliance — WAN + LAN ports."""
        if dev.wan1_ip:
            dev.ports.append(CollectedPort(
                port_id="wan1", name="WAN 1",
                description="WAN 1 uplink",
                enabled=True, connected=True, is_uplink=True,
            ))
        if dev.wan2_ip:
            dev.ports.append(CollectedPort(
                port_id="wan2", name="WAN 2",
                description="WAN 2 uplink",
                enabled=True, connected=True, is_uplink=True,
            ))
        if dev.lan_ip:
            dev.ports.append(CollectedPort(
                port_id="lan", name="LAN",
                description="LAN interface",
                enabled=True, connected=True,
            ))
        if not dev.ports:
            dev.ports.append(CollectedPort(
                port_id="mgmt", name="Management", enabled=True,
            ))

    def _collect_ap(self, dev: CollectedDevice, raw: dict) -> None:
        """MR access point — one radio interface."""
        dev.ports.append(CollectedPort(
            port_id="radio0",
            name="Radio 0",
            description=raw.get("model", ""),
            enabled=True,
            connected=True,
        ))

    # ------------------------------------------------------------------
    # SDK wrappers
    # ------------------------------------------------------------------

    def _get_network_devices(self, network_id: str) -> list[dict]:
        try:
            return self.dashboard.networks.getNetworkDevices(networkId=network_id)
        except meraki.exceptions.APIError as exc:
            log.error("Meraki: getNetworkDevices failed for %s: %s", network_id, exc)
            return []

    def _get_switch_ports(self, serial: str) -> list[dict]:
        try:
            return self.dashboard.devices.getDeviceSwitchPorts(serial=serial)
        except meraki.exceptions.APIError:
            return []

    def _get_switch_port_statuses(self, serial: str) -> list[dict]:
        try:
            return self.dashboard.devices.getDeviceSwitchPortsStatuses(serial=serial)
        except meraki.exceptions.APIError:
            return []

    def _get_network_clients(
        self, network_id: str, timespan: int
    ) -> list[dict]:
        try:
            return self.dashboard.networks.getNetworkClients(
                networkId=network_id,
                timespan=timespan,
                total_pages="all",
            )
        except meraki.exceptions.APIError as exc:
            log.warning("Meraki: getNetworkClients failed for %s: %s", network_id, exc)
            return []

    def _get_network_topology(self, network_id: str) -> dict:
        try:
            return self.dashboard.networks.getNetworkTopologyLinkLayer(
                networkId=network_id
            )
        except meraki.exceptions.APIError:
            return {}

    # ------------------------------------------------------------------
    # Index builders
    # ------------------------------------------------------------------

    def _index_clients(
        self, network_id: str, timespan: int
    ) -> dict[tuple[str, str], list[dict]]:
        """Return clients keyed by (device_serial, switchport)."""
        index: dict[tuple[str, str], list[dict]] = {}
        for client in self._get_network_clients(network_id, timespan):
            serial    = client.get("recentDeviceSerial") or ""
            switchport = str(client.get("switchport") or "")
            if serial and switchport:
                index.setdefault((serial, switchport), []).append(client)
        return index

    def _build_topology_index(
        self, network_id: str
    ) -> dict[tuple[str, str], list[dict]]:
        """
        Parse the topology/linkLayer response into:
        (local_serial, local_port) → [{remote_id, remote_port}, …]
        """
        topology = self._get_network_topology(network_id)
        index: dict[tuple[str, str], list[dict]] = {}

        for link in (topology.get("links") or []):
            ends = link.get("ends") or []
            if len(ends) != 2:
                continue
            a, b = ends
            for local, remote in ((a, b), (b, a)):
                serial = (local.get("device") or {}).get("serial", "")
                port   = str(
                    (local.get("discovered") or {}).get("portId")
                    or (local.get("connected") or {}).get("portId")
                    or ""
                )
                r_serial = (remote.get("device") or {}).get("serial", "")
                r_port   = str(
                    (remote.get("discovered") or {}).get("portId")
                    or (remote.get("connected") or {}).get("portId")
                    or ""
                )
                if serial and port and r_serial:
                    index.setdefault((serial, port), []).append({
                        "remote_id":   r_serial,
                        "remote_port": r_port,
                    })

        return index

    # ------------------------------------------------------------------
    # Organisation-level helpers (used by management command)
    # ------------------------------------------------------------------

    def get_organizations(self) -> list[dict]:
        """Return all organisations accessible to the API key."""
        try:
            return self.dashboard.organizations.getOrganizations()
        except meraki.exceptions.APIError as exc:
            log.error("Meraki: getOrganizations failed: %s", exc)
            return []

    def get_organization_networks(self, org_id: str) -> list[dict]:
        """Return all networks in an organisation."""
        try:
            return self.dashboard.organizations.getOrganizationNetworks(
                organizationId=org_id,
                total_pages="all",
            )
        except meraki.exceptions.APIError as exc:
            log.error("Meraki: getOrganizationNetworks(%s) failed: %s", org_id, exc)
            return []


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_speed_mbps(speed_str: str) -> int:
    """Convert a Meraki speed string ('1 Gbps', '100 Mbps', …) to Mbps."""
    if not speed_str:
        return 0
    s = speed_str.strip().lower()
    try:
        if "gbps" in s:
            return int(float(s.replace("gbps", "").strip()) * 1000)
        if "mbps" in s:
            return int(float(s.replace("mbps", "").strip()))
    except ValueError:
        pass
    return 0


def _normalise_mac(mac: str) -> str:
    """Return a colon-separated lowercase MAC or '' if invalid."""
    digits = mac.lower().replace(":", "").replace("-", "").replace(".", "")
    if len(digits) != 12:
        return ""
    return ":".join(digits[i: i + 2] for i in range(0, 12, 2))
