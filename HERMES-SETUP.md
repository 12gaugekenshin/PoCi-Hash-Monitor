# Connect PoCiSys Hash Monitor to Hermes

PoCiSys 1.5.0 exposes a small authenticated MCP endpoint for read-only mining
and Umbrel telemetry.

## 1. Generate a PoCiSys connection token

1. Open **PoCiSys Hash Monitor → Settings**.
2. Find **Hermes AI access**.
3. Select **Generate / rotate token**.
4. Copy the token immediately. PoCiSys stores only its SHA-256 digest and
   cannot reveal the reusable token again.
5. Turn on **Enable Hermes MCP** and save the settings.

Rotating or revoking the token immediately invalidates the old Hermes
connection. Revoking also disables the endpoint.

## 2. Add PoCiSys to Hermes

Open **Hermes → MCP** and add an HTTP/Streamable HTTP server:

- Name: `pocisys`
- URL: `http://<umbrel-ip>:8765/mcp`
- Header name: `Authorization`
- Header value: `Bearer <the-token-you-copied>`

For the server's tool filter, include only:

- `get_pocisys_overview`
- `list_pocisys_miners`
- `get_pocisys_miner`
- `get_pocisys_pools`
- `get_pocisys_block_odds`
- `get_pocisys_system_health`

Reload MCP or restart the Hermes gateway after saving.

## 3. Verify from Hermes or Discord

Try:

- `Give me a short overview of the Umbrel and mining fleet.`
- `What is Loki1's hashrate and temperature?`
- `How many Public Pool workers are online?`
- `Show CPU, RAM, disk usage, and uptime.`

## Security properties

- Every exposed MCP tool is read-only.
- The endpoint does not accept arbitrary URLs, file paths, shell commands, or
  miner IP addresses.
- Miner lookup is limited to devices already configured in PoCiSys.
- No Discord webhook, wallet secret, Bitcoin RPC credential, config mutation,
  miner control, or Umbrel administration method is exposed.
- Host metrics are explicitly labeled **container-visible host metrics**. They
  reflect the Linux resources visible to the PoCiSys Umbrel container.
