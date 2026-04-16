# netbox-meraki-sync

A NetBox 4.x plugin that synchronises Cisco Meraki network devices into NetBox using the official [Meraki Python SDK](https://github.com/meraki/dashboard-api-python). A read-only Meraki API key is all that is required.

Sites are linked to Meraki networks via two custom fields on `dcim.site` that are created automatically when the plugin loads:

| Custom field | Purpose |
|---|---|
| `meraki_network_id` | Meraki Dashboard network ID (e.g. `N_xxxxxxxxxxxx`). Populate this to enable sync. |
| `meraki_site_name` | Human-readable Meraki network name. Populated automatically; can also be set manually. |

---

## What gets synced

| NetBox object | Source |
|---|---|
| `dcim.Manufacturer` | "Cisco Meraki" — created once, reused |
| `dcim.DeviceType` | Meraki model string (e.g. `MS425-32`) |
| `dcim.DeviceRole` | Derived from model family: Switch / Firewall / Access Point / … |
| `dcim.Device` | One per Meraki serial number; matched by serial then by hostname |
| `dcim.Interface` | One per switch port / WAN port / radio; type derived from model family and link speed |
| `dcim.MACAddress` | One per client MAC seen on a switch port in the last 24 hours |
| `ipam.IPAddress` | Optional (requires `--sync-ips` flag); device LAN / WAN IPs |
| `extras.Tag` | `meraki` tag applied to every synced device |

### Custom fields added to `dcim.Device`

| Field | Description |
|---|---|
| `meraki_serial` | Meraki device serial number (cross-reference) |
| `meraki_firmware` | Installed firmware version |
| `meraki_tags` | Meraki Dashboard tags (comma-separated) |

### Supported Meraki product families

| Family | Devices | Interfaces |
|---|---|---|
| MS | Switches | One interface per switch port; speed-aware type mapping |
| MX | Security appliances / SD-WAN | WAN 1, WAN 2, LAN |
| MR | Wireless access points | Radio 0 (type: `ieee802.11ax`) |
| MG | Cellular gateways | Management |
| MV | Smart cameras | Management |
| MT | Environmental sensors | Management |

---

## Requirements

- NetBox 4.0 or later
- Python 3.10+
- `meraki` Python SDK ≥ 1.48.0 (installed automatically)
- A Cisco Meraki Dashboard API key with **read-only** access

---

## Installation

### 1. Install the package

Install into the same Python environment that runs NetBox (usually `/opt/netbox/venv`):

```bash
# From PyPI (once published)
source /opt/netbox/venv/bin/activate
pip install netbox-meraki-sync

# Or from a local clone
source /opt/netbox/venv/bin/activate
pip install /path/to/netbox-meraki-sync
```

### 2. Enable the plugin in NetBox

Edit NetBox's `configuration.py` (typically `/opt/netbox/netbox/netbox/configuration.py`):

```python
PLUGINS = [
    "netbox_meraki_sync",
]
```

### 3. Configure the plugin

Add a `PLUGINS_CONFIG` block to `configuration.py`. Only `meraki_api_key` is required:

```python
PLUGINS_CONFIG = {
    "netbox_meraki_sync": {
        # Meraki Dashboard API key — read-only scope is sufficient.
        # Can also be set via the MERAKI_DASHBOARD_API_KEY environment variable.
        "meraki_api_key": "your-api-key-here",

        # HTTP/HTTPS proxy for outbound Meraki API calls (optional).
        # "http_proxy": "http://proxy.example.com:3128",

        # Meraki API request timeout in seconds (default: 30).
        "request_timeout": 30,

        # When True, create IPAM IPAddress records for device LAN/WAN IPs.
        # Can also be enabled per-run with --sync-ips.
        "sync_ip_addresses": False,

        # Default NetBox device role slug for new devices.
        # Created automatically if it does not exist.
        "default_device_role": "network",
    }
}
```

### 4. Run database migrations

```bash
source /opt/netbox/venv/bin/activate
cd /opt/netbox/netbox
python manage.py migrate netbox_meraki_sync
```

### 5. Restart NetBox

```bash
sudo systemctl restart netbox netbox-rq
```

The plugin appears under **Plugins → Meraki Sync** in the NetBox navigation menu.  
The two custom fields (`meraki_network_id`, `meraki_site_name`) are created on `dcim.site` automatically at this point.

---

## Mapping sites to Meraki networks

Before running a sync you need to tell the plugin which NetBox site corresponds to which Meraki network.

### Step 1 — Find your Meraki network IDs

```bash
python manage.py sync_meraki --list-networks
```

Output example:

```
Organisation: Acme Corp  (id: 123456)
------------------------------------------------------------
  N_aabbccddeeff1122    London Office    [tags: office]
  N_112233445566aabb    Manchester DC    [tags: dc, prod]
```

### Step 2 — Set the custom field on the NetBox site

In the NetBox UI:
1. Navigate to **Organisation → Sites** and open the target site
2. Click **Edit**
3. Under **Custom Fields**, set **Meraki Network ID** to the value from step 1 (e.g. `N_aabbccddeeff1122`)
4. Optionally set **Meraki Site Name** to the human-readable network name
5. Save

Or via the NetBox API:

```bash
curl -X PATCH https://netbox.example.com/api/dcim/sites/42/ \
  -H "Authorization: Token YOUR_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"custom_fields": {"meraki_network_id": "N_aabbccddeeff1122"}}'
```

---

## Running a sync

```bash
# Sync all mapped sites
python manage.py sync_meraki

# Sync a single Meraki network (the corresponding site must have meraki_network_id set)
python manage.py sync_meraki --network N_aabbccddeeff1122

# Dry run — show what would change without writing anything
python manage.py sync_meraki --dry-run

# Also create IPAM IP address records for device LAN/WAN IPs
python manage.py sync_meraki --sync-ips

# Extend the client look-back window to 7 days for MAC tables
python manage.py sync_meraki --client-timespan 604800
```

### Scheduling

```
# /etc/cron.d/netbox-meraki-sync

# Sync every 4 hours
0 */4 * * * netbox /opt/netbox/venv/bin/python /opt/netbox/netbox/manage.py \
    sync_meraki >> /var/log/netbox/meraki_sync.log 2>&1
```

---

## Viewing sync results

Navigate to **Plugins → Meraki Sync → Sync Logs** in the NetBox UI to see the history of sync runs, including per-network counters for devices created/updated, interfaces, MACs, and IPs.

The REST API also exposes sync logs at:

```
GET /api/plugins/meraki/sync-logs/
GET /api/plugins/meraki/sync-logs/<id>/
```

---

## Meraki API key permissions

The plugin only reads data. The minimum required API key permissions are:

| Meraki permission | Required for |
|---|---|
| Organization > Networks — Read | `--list-networks` |
| Network > Devices — Read | All syncs |
| Network > Clients — Read | MAC address tables |
| Network > Topology — Read | Neighbour (CDP/LLDP) data |
| Device > Switch ports — Read | MS switch port details |

A **read-only** organisation-level API key covers all of the above.

---

## Project structure

```
netbox-meraki-sync/
├── netbox_meraki_sync/
│   ├── __init__.py             PluginConfig (base_url="meraki")
│   ├── signals.py              Auto-creates meraki_network_id / meraki_site_name custom fields
│   ├── choices.py              SyncStatusChoices, DeviceFamilyChoices
│   ├── collector.py            Meraki SDK wrapper — read-only, returns CollectedDevice objects
│   ├── syncer.py               Django ORM writer — creates/updates NetBox objects
│   ├── filtersets.py
│   ├── models/
│   │   └── sync_log.py         SyncLog (audit record per network per run)
│   ├── tables/
│   ├── forms/
│   ├── views/
│   ├── api/                    Read-only REST API for sync logs
│   ├── navigation.py
│   ├── urls.py
│   ├── migrations/
│   │   └── 0001_initial.py
│   └── templates/
│       └── netbox_meraki_sync/
│           ├── synclog_list.html
│           └── synclog.html
└── pyproject.toml
```

---

## Notes

- The plugin never deletes Device records from NetBox.
- Device matching priority: Meraki serial → device name within the same site.  If neither matches, a new device is created.
- MAC addresses from clients active in the last 24 hours are synced by default.  Use `--client-timespan` to change the look-back window.
- The `meraki` SDK retries automatically on HTTP 429 (rate limit exceeded) with exponential back-off.  No additional throttling is needed.
- Interface types are inferred from model family and link speed; they can be manually overridden in NetBox after creation.
