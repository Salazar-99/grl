# GRL Traces Panel

Custom Grafana panel (`grl-traces-panel`) for inspecting GRL rollout trajectories.

It renders the ClickHouse `grl_trajectories` table and opens a chat-style modal when you click a row's **Body** field. The modal parses Qwen-style XML traces (`system` / `user` / `<think>` / `<tool_call>` / `<tool_response>`) into:

- full system prompt (top)
- initial user task
- a back-and-forth transcript where agent thinking + tool calls sit on the left and tool responses sit on the right

## Local development (Docker)

The scaffolded Docker workflow already mounts the built plugin and allows unsigned loading:

```bash
cd infra/observability/grafana/traces-panel
npm install
npm run dev          # watch build into dist/
npm run server       # docker compose up --build → http://localhost:3000
```

Open the provisioned **Provisioned traces dashboard** to exercise the table + modal against TestData fixtures.

## Install into an existing Grafana

### 1. Build the plugin

```bash
cd infra/observability/grafana/traces-panel
npm install
npm run build
```

This writes the loadable plugin to `dist/`.

### 2. Place it on the Grafana plugin path

Copy or mount `dist/` so Grafana sees it as:

```text
<grafana-plugins-dir>/grl-traces-panel/
  plugin.json
  module.js
  ...
```

Examples:

```bash
# filesystem install
sudo mkdir -p /var/lib/grafana/plugins
sudo cp -R dist /var/lib/grafana/plugins/grl-traces-panel
sudo chown -R grafana:grafana /var/lib/grafana/plugins/grl-traces-panel
```

Or mount the same path into a Grafana container / Kubernetes volume.

The Grafana process must be able to read the directory.

### 3. Allow unsigned plugins (development / private installs)

Until the plugin is signed and published, Grafana will refuse to load it unless you explicitly allow the plugin ID `grl-traces-panel`.

**Option A — `grafana.ini`:**

```ini
[plugins]
allow_loading_unsigned_plugins = grl-traces-panel
```

**Option B — environment variable:**

```bash
GF_PLUGINS_ALLOW_LOADING_UNSIGNED_PLUGINS=grl-traces-panel
```

If you already allow other unsigned plugins, append with a comma:

```bash
GF_PLUGINS_ALLOW_LOADING_UNSIGNED_PLUGINS=other-plugin,grl-traces-panel
```

### 4. Restart Grafana and verify

Restart Grafana after installing or changing plugin config. Confirm registration via:

- Administration → Plugins → search for **Traces** / `grl-traces-panel`
- Grafana logs for `Plugin registered ... pluginID=grl-traces-panel`

Any change to `plugin.json` also requires a Grafana restart.

### 5. Wire the GRL dashboard + ClickHouse datasource

1. Ensure a ClickHouse datasource exists with UID `clickhouse` (matches [`grl-dashboard.json`](../grl-dashboard.json)).
2. Import or provision [`../grl-dashboard.json`](../grl-dashboard.json).
3. Open the **Trajectories** row — panel type is `grl-traces-panel` and uses:

```sql
SELECT TimeUnix AS time, TaskId, GroupId, RolloutIndex, Reward, NumTurns,
       DoneReason, PromptTokens, ResponseTokens, PolicyVersionStart,
       PolicyVersionCurrent, Body
FROM default.grl_trajectories
WHERE RunId = '${run_id}'
ORDER BY TimeUnix DESC
LIMIT 500
```

4. Click a Body cell to open the trace modal.

## Production signing

For shared/production Grafana, prefer signing the plugin instead of unsigned loading:

```bash
npm run sign
```

See Grafana's docs on [signing a plugin](https://grafana.com/developers/plugin-tools/publish-a-plugin/sign-a-plugin). The first part of the plugin ID (`grl`) must match your Grafana Cloud org slug when using Grafana's signing flow.

## Useful scripts

| Script | Purpose |
| --- | --- |
| `npm run dev` | Watch build |
| `npm run build` | Production build |
| `npm run server` | Local Grafana via Docker Compose |
| `npm run test:ci` | Unit tests |
| `npm run e2e` | Playwright e2e against the Docker Grafana |
| `npm run lint` / `npm run typecheck` | Static checks |

## Notes

- Do not edit files under `.config/`; they are managed by `@grafana/create-plugin`.
- The parser currently specializes on Qwen XML tool-call format (`<function=...>` / `<parameter=...>`), with best-effort JSON `<tool_call>` fallback.
- Rebuild (`npm run build` or `npm run dev`) after frontend changes; restart Grafana after plugin install/config/`plugin.json` changes.
