# Network Stats Dashboard and Node Exporter

## Summary

Add portable per-node network observability for training and rollout nodes,
plus NCCL-specific policy-transfer throughput. The design is Linux and
Kubernetes native: it does not depend on AWS, EKS, ENA, a particular CNI, or a
fixed network-interface name.

## Implementation

### NCCL transfer telemetry

- Add `grl.train.weight_sync.payload_bytes` as a histogram, emitted for every
  NCCL update after the existing `language_model_only` parameter filtering.
- Add `grl.train.weight_sync.transfer.duration`, measured only around vLLM's
  packed NCCL sender call.
- Label both metrics with `backend=nccl`; keep the existing end-to-end
  `grl.train.weight_sync.duration` metric labeled by the effective backend.
- Add `payload_bytes`, rollout-worker count, tensor-parallel size, and NCCL
  world size to the weight-sync trace.
- Do not add sync counters. A run uses one effective backend, so duration and
  derived throughput are the useful operational signals.

The dashboard derives NCCL payload throughput from
`payload_bytes / transfer.duration`, and effective update throughput from
`payload_bytes / weight_sync.duration`.

### Node Exporter

- Add a chart-managed Node Exporter DaemonSet to the resources chart, enabled
  by default and pinned to `quay.io/prometheus/node-exporter:v1.11.1`.
- Run one non-privileged pod on every schedulable Linux node. Mount host
  `/proc`, `/sys`, and `/` read-only; do not use host networking, a host port,
  process collectors, or cloud-specific collectors.
- Enable Node Exporter's `netdev`, `netstat`, `sockstat`, and `softnet`
  collectors.
- Add `nodeExporter.interfaceExcludeRegex` to chart values and pass it to the
  netdev collector. Default to excluding loopback and common virtual/CNI/
  overlay devices (`lo`, `veth`, Docker, Calico, Flannel, CNI, and tunnels).
  Operators may disable the exporter in restricted environments with
  `nodeExporter.enabled: false`.

### OpenTelemetry collection

- Add a `node-exporter` job to the existing in-cluster Collector's Prometheus
  receiver.
- Discover only the chart-managed Node Exporter pods, scrape port `9100`, and
  relabel the Kubernetes node name as `node`.
- Reuse the Collector service account's existing pod/node discovery rights; no
  cloud-provider API or additional RBAC is required.

### Grafana Network section

Extend the dashboard generator and regenerate every provisioned dashboard
artifact. Add panels for:

- Node transmit throughput from `node_network_transmit_bytes_total` rate.
- Node receive throughput from `node_network_receive_bytes_total` rate.
- TX/RX error and drop rates.
- TCP retransmission rate from `node_netstat_Tcp_RetransSegs`.
- Linux softnet drops and time-squeeze rates.
- NCCL payload throughput.
- NCCL sender-only transfer duration alongside end-to-end weight-sync duration.

Attribute samples to `training` and `rollouts` by reusing the existing
Ray-node-to-role join used by the DCGM panels. Display nodes without a matching
Ray role as `unassigned`.

Only add NIC utilization percentage when `node_network_speed_bytes` is present
and non-zero. Otherwise retain throughput panels without inventing a link
capacity.

## Validation

- Unit-test NCCL payload-byte accounting against the exact sent parameter
  iterator, including `language_model_only`.
- Unit-test sender-only and end-to-end metric scopes and labels.
- Render the resources chart to verify the Node Exporter DaemonSet and
  Collector scrape job are present when enabled and absent when disabled.
- Regenerate and JSON-validate the Grafana dashboard, including its version
  bump.
- In a cluster smoke test, verify one Node Exporter target per node, then run
  an NCCL update and confirm training-node TX, rollout-node RX, payload bytes,
  and sender duration correlate.
- Verify errors, drops, and retransmits remain near zero in a healthy run and
  are visible during an intentionally impaired-network test.

## Assumptions

- Per-node network attribution is sufficient. Kubelet/cAdvisor per-pod network
  metrics are intentionally deferred.
- Link-speed utilization is best-effort. Network rate, error, drop,
  retransmission, and NCCL transfer-throughput metrics remain available on any
  Linux Kubernetes cluster where Node Exporter can run.
