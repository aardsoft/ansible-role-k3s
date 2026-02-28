''' Inventory extension for the k3s Ansible role.

Registered by the site_yaml inventory plugin when it discovers this file at
roles/k3s/plugins/inventory_extension.py.  Handles hosts of type 'k3s-pod'.

Class attributes read by the plugin:
  HANDLES_TYPES  -- list of host type strings this extension manages
  ENFORCED       -- if True, this type is added to enforced_types in
                    _sanitise_hosts_data and receives the standard physical
                    network port/vlan/bridge validation pass.  Defaults to
                    False when absent.  k3s-pod hosts have no physical ports
                    so this is False.

Lifecycle hooks called by the plugin:
  sanitise_host    -- during _sanitise_hosts_data: normalise or validate raw
                      host data; called for both enforced and non-enforced
                      extension types
  preprocess_host  -- after sanitisation, before _parse_hosts; merges pod
                      snippets from k3s.snippets into the host definition
  validate_host    -- dryrun: type-specific validation
  setup_host       -- non-dryrun: set inventory vars and create derived hosts;
                      returns global variable contributions
'''

import os
import re

from ansible.template import Templar

HANDLES_TYPES = ['k3s-pod']


class InventoryExtension:

    # k3s-pod hosts have no physical switch ports, so the standard
    # port/vlan/bridge validation pass is not applicable.
    ENFORCED = False

    # ------------------------------------------------------------------
    # Pod snippet helpers
    # ------------------------------------------------------------------

    def _load_pod_snippet(self, plugin, role_name, render_vars, parser):
        ''' Load and Jinja2-render templates/k3s-pod.yml.j2 from a role.

        Only variables available at inventory time (from the host's site.yaml
        entry) are in scope for template rendering.  Variables from group_vars/
        host_vars directories are loaded by Ansible after inventory construction
        and are therefore not available here; supply them via host_vars: in
        the site.yaml entry if the snippet template needs them.

        Returns the parsed dict on success, or None on any error (errors are
        appended to parser). '''

        role_path = plugin._find_role_path(role_name)
        if not role_path:
            parser['errors'].append(
                "Pod snippet: role '%s' not found in roles path" % role_name)
            return None

        snippet_path = os.path.join(role_path, 'templates', 'k3s-pod.yml.j2')
        if not os.path.isfile(snippet_path):
            parser['errors'].append(
                "Pod snippet: '%s' has no templates/k3s-pod.yml.j2" % role_name)
            return None

        try:
            with open(snippet_path, 'r') as f:
                raw = f.read()
        except Exception as e:
            parser['errors'].append(
                "Pod snippet: failed to read '%s': %s" % (snippet_path, e))
            return None

        try:
            templar = Templar(loader=plugin.loader, variables=render_vars)
            rendered = templar.template(raw, fail_on_undefined=True)
        except Exception as e:
            parser['errors'].append(
                "Pod snippet: failed to render '%s/templates/k3s-pod.yml.j2': %s"
                % (role_name, e))
            return None

        try:
            parsed = plugin.loader.load(rendered)
        except Exception as e:
            parser['errors'].append(
                "Pod snippet: failed to parse rendered YAML from '%s': %s"
                % (role_name, e))
            return None

        if not isinstance(parsed, dict):
            parser['errors'].append(
                "Pod snippet: '%s' rendered YAML is not a dict" % role_name)
            return None

        return parsed

    def _merge_pod_sections(self, base, override):
        ''' Merge two pod section dicts.  override wins over base.

        Merge rules per key:
        - containers: deep merge per-container; override wins per field; dict
          sub-fields (env, resources, ...) are themselves merged so adding an
          env var does not wipe the others.
        - volumes, configmaps, secrets: list merge by name; override entry
          replaces base entry with the same name; unique entries from both
          sides are kept (base entries first, then override entries).
        - tolerations: concatenate base then override (no dedup).
        - all other keys: override wins outright. '''

        if not base:
            return dict(override) if override else {}
        if not override:
            return dict(base)

        result = dict(base)

        for key, value in override.items():
            if key == 'containers':
                merged = dict(result.get('containers') or {})
                for cnt_name, cnt_def in (value or {}).items():
                    if cnt_name in merged and isinstance(cnt_def, dict):
                        merged_cnt = dict(merged[cnt_name])
                        for field, field_val in cnt_def.items():
                            if (isinstance(field_val, dict) and
                                    isinstance(merged_cnt.get(field), dict)):
                                sub = dict(merged_cnt[field])
                                sub.update(field_val)
                                merged_cnt[field] = sub
                            else:
                                merged_cnt[field] = field_val
                        merged[cnt_name] = merged_cnt
                    else:
                        merged[cnt_name] = cnt_def
                result['containers'] = merged

            elif key == 'tolerations':
                result['tolerations'] = (
                    list(result.get('tolerations') or []) + list(value or []))

            elif key in ('volumes', 'configmaps', 'secrets'):
                base_list = list(result.get(key) or [])
                override_list = list(value or [])
                override_names = {
                    e['name'] for e in override_list
                    if isinstance(e, dict) and 'name' in e}
                kept = [e for e in base_list
                        if not (isinstance(e, dict) and
                                e.get('name') in override_names)]
                result[key] = kept + override_list

            else:
                result[key] = value

        return result

    # ------------------------------------------------------------------
    # Cluster SSH resolution
    # ------------------------------------------------------------------

    def _resolve_cluster_ssh(self, plugin, hosts, pod_host):
        ''' Resolve the SSH address of the k3s cluster host for a k3s-pod.
        Returns the SSH address string, or None if not determinable. '''

        k3s_cfg = pod_host.get('k3s')
        if not k3s_cfg or not isinstance(k3s_cfg, dict):
            return None

        cluster_name = k3s_cfg.get('cluster')
        if not cluster_name:
            return None

        if cluster_name not in hosts:
            return None

        cluster_host = hosts[cluster_name]

        # Try host_vars.ansible_host first - most explicit setting
        host_vars = cluster_host.get('host_vars')
        if host_vars and isinstance(host_vars, dict):
            ansible_host = host_vars.get('ansible_host')
            if ansible_host:
                return ansible_host

        # Fall back to finding any IP in the cluster host's networks
        networks = cluster_host.get('networks')
        if networks and isinstance(networks, dict):
            for if_name, iface in networks.items():
                if not isinstance(iface, dict):
                    continue
                ipv4 = iface.get('ipv4')
                if ipv4:
                    return re.sub(r'/.*$', '', ipv4)

        # Last resort: use the cluster hostname itself (may be DNS-resolvable)
        plugin.display.warning(
            "k3s-pod '%s': cluster host '%s' has no ansible_host or network IP; "
            "falling back to inventory hostname as SSH address" % (
                pod_host.get('hostname', '?'), cluster_name))
        return cluster_name

    # ------------------------------------------------------------------
    # Lifecycle hooks
    # ------------------------------------------------------------------

    def preprocess_host(self, plugin, host, host_def, data, valid_keys, parser):
        ''' Load and merge pod snippets declared in k3s.snippets.  Modifies
        data[valid_keys['hosts']][host] in place so the merged pod section is
        visible everywhere network_nodes is used. '''

        k3s_cfg = host_def.get('k3s')
        if not isinstance(k3s_cfg, dict):
            return
        snippets = k3s_cfg.get('snippets')
        if not snippets:
            return

        # Variables available for Jinja2 rendering in snippet templates.
        # Only site.yaml-level vars are in scope at inventory time; use
        # host_vars: in the site.yaml entry for values the snippet needs.
        render_vars = dict(host_def.get('host_vars') or {})
        render_vars['inventory_hostname'] = host

        # Merge snippets in order: each successive snippet overrides earlier ones.
        merged_pod = {}
        for role_name in snippets:
            snippet = self._load_pod_snippet(plugin, role_name, render_vars, parser)
            if snippet and 'pod' in snippet:
                merged_pod = self._merge_pod_sections(merged_pod, snippet['pod'])

        # Apply the host's own pod section last so it always wins.
        host_pod = host_def.get('pod') or {}
        data[valid_keys['hosts']][host]['pod'] = self._merge_pod_sections(
            merged_pod, host_pod)

    def validate_host(self, plugin, host, host_def, hosts, parser):
        ''' Dryrun validation for k3s-pod hosts. '''

        k3s_cfg = host_def.get('k3s', {}) or {}

        # Skip container check when snippets are declared: any snippet loading
        # errors are already reported by preprocess_host.
        if not k3s_cfg.get('snippets'):
            pod_cfg = host_def.get('pod', {})
            if not pod_cfg or not pod_cfg.get('containers'):
                parser['errors'].append(
                    "%s: k3s-pod type requires pod.containers to be defined" % host)

        if k3s_cfg.get('cluster') and k3s_cfg['cluster'] not in hosts:
            parser['errors'].append(
                "%s: k3s.cluster '%s' not found in hosts" % (host, k3s_cfg['cluster']))

    def setup_host(self, plugin, host, host_def, groups, data, valid_keys, parser):
        ''' Non-dryrun host setup: set connection vars and create per-container
        derived hosts.

        Returns {'network_pods': {host: host_def}} so the plugin can set the
        network_pods global variable after processing all hosts. '''

        hosts = data[valid_keys['hosts']]
        cluster_ssh = self._resolve_cluster_ssh(plugin, hosts, host_def)
        namespace = host_def.get('k3s', {}).get('namespace')

        plugin.inventory.add_group('k3s_pods')
        plugin.inventory.add_child('k3s_pods', host)

        plugin.inventory.set_variable(host, 'ansible_connection', 'sshkubectl')
        plugin.inventory.set_variable(host, 'ansible_kubectl_pod', host)
        plugin.inventory.set_variable(host, 'ansible_kubectl_kubeconfig', '/etc/rancher/k3s/k3s.yaml')
        if cluster_ssh:
            plugin.inventory.set_variable(host, 'ansible_host', '%s@%s' % (host, cluster_ssh))
        if namespace:
            plugin.inventory.set_variable(host, 'ansible_kubectl_namespace', namespace)

        # determine default container (single container or explicitly marked)
        containers = host_def.get('pod', {}).get('containers', {})
        default_container = host_def.get('k3s', {}).get('default_container')
        if not default_container and len(containers) == 1:
            default_container = list(containers.keys())[0]

        if default_container and default_container in containers:
            plugin.inventory.set_variable(host, 'ansible_kubectl_container', default_container)

        # create derived hosts for each container
        plugin.inventory.add_group('k3s_pod_containers')
        for container_name in containers:
            cnt_host = '%s-cnt-%s' % (host, container_name)

            if cnt_host in plugin.inventory.groups:
                parser['errors'].append(
                    "%s exists as host and group name, rename one" % cnt_host)
                continue

            plugin.inventory.add_host(host=cnt_host)
            plugin.inventory.add_child('k3s_pod_containers', cnt_host)

            for group in groups:
                plugin.inventory.add_child(group.replace("-", "_"), cnt_host)

            plugin.inventory.set_variable(cnt_host, 'network_nodes', data[valid_keys['hosts']])
            plugin.inventory.set_variable(cnt_host, 'ansible_connection', 'sshkubectl')
            plugin.inventory.set_variable(cnt_host, 'ansible_kubectl_pod', host)
            plugin.inventory.set_variable(cnt_host, 'ansible_kubectl_container', container_name)
            plugin.inventory.set_variable(cnt_host, 'ansible_kubectl_kubeconfig', '/etc/rancher/k3s/k3s.yaml')
            if cluster_ssh:
                plugin.inventory.set_variable(cnt_host, 'ansible_host', '%s@%s' % (host, cluster_ssh))
            if namespace:
                plugin.inventory.set_variable(cnt_host, 'ansible_kubectl_namespace', namespace)

        return {'network_pods': {host: host_def}}
