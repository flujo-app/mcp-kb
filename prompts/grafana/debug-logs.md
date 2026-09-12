---
description: Debug a workload's recent logs in Loki with the Grafana MCP server.
arguments:
- name: app
  description: The workload to investigate, as its Loki labels name it — a deployment, container or service.
  required: true
- name: namespace
  description: The Kubernetes namespace, if the app name is not unique on its own. Defaults to any.
  default: any
- name: since
  description: How far back to look, as a LogQL range such as 30m or 6h. Defaults to 1h.
  default: 1h
- name: symptom
  description: What is going wrong, in a few words. Leave empty for a general health check.
  default: none reported, so look for errors and anything unusual
---
Investigate the logs of **{{ app }}** with the Grafana MCP server.

- Namespace: {{ namespace }}
- Time range: the last {{ since }}
- Symptom: {{ symptom }}

1. Call `list_datasources` and pick the Loki datasource.
2. Find the labels that identify the app: `list_loki_label_names`, then `list_loki_label_values` for a likely label such as `app`, `container` or `service_name`. Do not guess a selector.
3. Check volume with `query_loki_stats`, then read recent lines with `query_loki_logs` over the time range. Start broad, then narrow with line filters.
4. Group repeated errors with `query_loki_patterns` or `find_error_pattern_logs`.
5. Report the selector you used, what you found with a few example lines, the likely cause, and what to check next. Say plainly if the logs show nothing wrong.

For LogQL syntax, read the resource `skill://grafana/loki`.
