# GRL Traces Panel

Custom Grafana panel (`grl-traces-panel`) for inspecting GRL rollout trajectories from ClickHouse.

Click a row's **Body** field to open a chat-style modal with:

- full system prompt
- initial user task
- agent thinking / tool calls on the left and tool responses on the right

Parses Qwen XML tool-call traces (`<think>`, `<tool_call>`, `<function=...>`, `<parameter=...>`, `<tool_response>`).

## Requirements

- Grafana `>=12.3.0`
- ClickHouse datasource with UID `clickhouse` (for the GRL training dashboard)
- For unsigned private installs: allow plugin ID `grl-traces-panel`

## Install

1. Build:

```bash
npm install
npm run build
```

2. Copy `dist/` to Grafana's plugin directory as `grl-traces-panel/`.

3. Allow unsigned loading while developing / privately distributing:

```ini
[plugins]
allow_loading_unsigned_plugins = grl-traces-panel
```

or:

```bash
GF_PLUGINS_ALLOW_LOADING_UNSIGNED_PLUGINS=grl-traces-panel
```

4. Restart Grafana and confirm **Traces** appears under Administration → Plugins.

5. Import `grl-dashboard.json` (or select visualization `Traces` on a panel querying `default.grl_trajectories`).

Prefer `npm run sign` for production instead of unsigned loading.

## Local development

```bash
npm install
npm run dev
npm run server
```

Open http://localhost:3000 and use the provisioned traces dashboard.
