# ENERGY V3 repository access

## Production Home Assistant

Use only the existing approved access unless the user explicitly authorizes a write:

- SSH host: `192.168.221.87`
- SSH user: `root`
- SSH port: `22`
- SSH key: `$env:USERPROFILE\\.ssh\\codex_homeassistant`
- AppDaemon apps root: `/app_configs/a0d7b954_appdaemon/apps`
- Home Assistant MCP server: `home_assistant_chalupa`

The SSH key is private and must never be printed, searched for, or exposed. Reading
the key may require an explicit sandbox filesystem/network approval. A sandbox denial
does not by itself mean that SSH is unavailable; retry only after requesting the
minimum required approval for the existing key and connection.

## SSH verification

Use BatchMode and a timeout:

```powershell
ssh.exe -i "$env:USERPROFILE\\.ssh\\codex_homeassistant" `
  -o BatchMode=yes -o ConnectTimeout=8 `
  root@192.168.221.87 "hostname; ha core info"
```

## Safety

Before any production write, restart, deployment, inverter command, or AppDaemon
reload, obtain explicit user authorization.

Read-only inspection is allowed for:

- Home Assistant entity states and attributes;
- HA configuration and error logs;
- AppDaemon source and configuration;
- repository and deployment status.

Never print private keys, tokens, passwords, or credential values.
Never use SSH to search for credentials or private keys.
Never modify SolaX, DEYE, ENERGY V2, HAEO, legacy automations, or production files
without explicit authorization.
