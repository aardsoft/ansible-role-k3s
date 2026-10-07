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

import jinja2
from ansible.template import Templar
from ansible.utils.unsafe_proxy import wrap_var

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
            # Use Jinja2 directly with the role's templates directory in the
            # search path so {% include %} can resolve sibling templates
            # (e.g. init-scripts/*.sh.j2).
            template_dir = os.path.join(role_path, 'templates')
            env = jinja2.Environment(
                loader=jinja2.FileSystemLoader(template_dir),
                undefined=jinja2.StrictUndefined,
            )
            template = env.get_template('k3s-pod.yml.j2')
            rendered = template.render(render_vars)
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

    @staticmethod
    def _deep_merge_dicts(base, override):
        ''' Recursive dict merge: override wins per leaf field; nested dicts
        are merged recursively so e.g. persistentVolumeClaim fields merge.'''
        merged = dict(base)
        for key, value in (override or {}).items():
            if (isinstance(value, dict) and
                    isinstance(merged.get(key), dict)):
                merged[key] = InventoryExtension._deep_merge_dicts(
                    merged[key], value)
            else:
                merged[key] = value
        return merged

    def _merge_pod_sections(self, base, override):
        ''' Merge two pod section dicts.  override wins over base.

        Merge rules per key:
        - containers: deep merge per-container; override wins per field; dict
          sub-fields (env, resources, ...) are themselves merged so adding an
          env var does not wipe the others.
        - volumes, configmaps, secrets: list merge by name; a named override
          entry is DEEP-MERGED into the base entry of the same name (override
          wins per field; base entries keep their order).  A site entry can
          therefore tweak one field (e.g. persistentVolumeClaim.storageClass)
          and claimName/size from the snippet survive.  To REPLACE an entry
          wholesale, use a different name.  Unique base entries are kept
          first, then override-only entries are appended.
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
                index = {}
                merged = []
                for entry in base_list:
                    if isinstance(entry, dict) and 'name' in entry:
                        index[entry['name']] = len(merged)
                    merged.append(entry)
                tail = []
                for entry in list(value or []):
                    name = entry.get('name') if isinstance(entry, dict) else None
                    if name is not None and name in index:
                        pos = index[name]
                        merged[pos] = self._deep_merge_dicts(
                            merged[pos], entry)
                    else:
                        tail.append(entry)
                result[key] = merged + tail

            else:
                result[key] = value

        return result

    # ------------------------------------------------------------------
    # Cluster SSH resolution
    # ------------------------------------------------------------------

    def _resolve_cluster_ssh(self, plugin, hosts, pod_host):
        ''' Resolve the SSH address of the k3s cluster host for a k3s-pod.

        Checks k3s.cluster_host first (explicit SSH target host), then falls
        back to k3s.cluster for backwards compatibility with configurations
        where the cluster value was the first-server inventory hostname.

        Returns the SSH address string, or None if not determinable. '''

        k3s_cfg = pod_host.get('k3s')
        if not k3s_cfg or not isinstance(k3s_cfg, dict):
            return None

        # Prefer explicit cluster_host; fall back to cluster name as hostname
        ssh_host_name = k3s_cfg.get('cluster_host') or k3s_cfg.get('cluster')
        if not ssh_host_name:
            return None

        if ssh_host_name not in hosts:
            return None

        cluster_host = hosts[ssh_host_name]

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

        # Last resort: use the resolved host name itself (may be DNS-resolvable)
        plugin.display.warning(
            "k3s-pod '%s': cluster host '%s' has no ansible_host or network IP; "
            "falling back to inventory hostname as SSH address" % (
                pod_host.get('hostname', '?'), ssh_host_name))
        return ssh_host_name

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
        merged_k3s = {}
        snippet_role_paths = {}
        for role_name in snippets:
            role_path = plugin._find_role_path(role_name)
            snippet = self._load_pod_snippet(plugin, role_name, render_vars, parser)
            if snippet and 'pod' in snippet:
                merged_pod = self._merge_pod_sections(merged_pod, snippet['pod'])
            if snippet and 'k3s' in snippet and isinstance(snippet['k3s'], dict):
                merged_k3s.update(snippet['k3s'])
            if role_path:
                snippet_role_paths[role_name] = role_path

        # Apply snippet k3s keys into host k3s (host explicit values win)
        host_k3s = host_def.get('k3s') or {}
        for k3s_key, k3s_val in merged_k3s.items():
            if k3s_key not in host_k3s:
                data[valid_keys['hosts']][host].setdefault('k3s', {})[k3s_key] = k3s_val

        # Expose discovered snippet role paths so playbook tasks can locate
        # role-specific files (e.g. deploy_container.yml) without hardcoding
        # a specific roles directory layout.
        data[valid_keys['hosts']][host].setdefault('host_vars', {})['_snippet_role_paths'] = snippet_role_paths
        # Also expose at host top-level so it travels with network_pods when
        # the pod host is iterated on a cluster node (not in host scope there).
        data[valid_keys['hosts']][host]['_snippet_role_paths'] = snippet_role_paths

        # Apply the host's own pod section last so it always wins.
        host_pod = host_def.get('pod') or {}
        data[valid_keys['hosts']][host]['pod'] = self._merge_pod_sections(
            merged_pod, host_pod)

    def sanitise_host(self, plugin, host, host_def, data, k, parser):
        ''' Normalise k3s-pod network interfaces.

        1. Synthesizes an addresses dict from ipv4/ipv6 scalar keys on each
           interface in host_def.networks if addresses is not already explicitly set.
           This mirrors what site_yaml does for server networks keys, letting
           complex consumers (pod-service template, etc.) always work off
           addresses while simple roles continue using ipv4/ipv6 directly.

        2. Derives network policy zone labels from the vlan field of each
           interface and merges them into k3s.network_policy_labels.  Zone
           names come directly from the vlan string, matching the names used
           in network_policies.service_vlans and built-in zones (dmz, etc.).
           Explicitly configured network_policy_labels are preserved. '''

        network = host_def.get('networks')
        if not network or not isinstance(network, dict):
            return

        auto_vlans = set()
        for if_key, iface in network.items():
            if not isinstance(iface, dict):
                continue
            # Synthesize addresses dict
            if iface.get('addresses') is None:
                synthesized = {}
                if iface.get('ipv4') is not None:
                    synthesized[iface['ipv4']] = {}
                if iface.get('ipv6') is not None:
                    synthesized[iface['ipv6']] = {}
                if synthesized:
                    data[k['hosts']][host]['networks'][if_key]['addresses'] = synthesized
            # Collect vlan names for network policy label derivation
            vlan = iface.get('vlan')
            if vlan:
                auto_vlans.add(str(vlan))

        # Merge auto-derived vlan labels into k3s.network_policy_labels
        if auto_vlans:
            k3s_section = data[k['hosts']][host].get('k3s')
            if not isinstance(k3s_section, dict):
                data[k['hosts']][host]['k3s'] = {}
            existing = set(
                data[k['hosts']][host]['k3s'].get('network_policy_labels') or [])
            data[k['hosts']][host]['k3s']['network_policy_labels'] = sorted(
                existing | auto_vlans)

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

        cluster_host = k3s_cfg.get('cluster_host')
        if cluster_host and cluster_host not in hosts:
            parser['errors'].append(
                "%s: k3s.cluster_host '%s' not found in hosts" % (host, cluster_host))
        elif not cluster_host and k3s_cfg.get('cluster') and k3s_cfg['cluster'] not in hosts:
            # cluster used as SSH host (legacy); warn but don't error since it
            # may be a logical cluster name rather than an inventory hostname
            plugin.display.warning(
                "%s: k3s.cluster '%s' not found in hosts — if this is a logical "
                "cluster name, set k3s.cluster_host for SSH resolution" % (
                    host, k3s_cfg['cluster']))

    def setup_host(self, plugin, host, host_def, groups, data, valid_keys, parser):
        ''' Non-dryrun host setup: set connection vars and create per-container
        derived hosts.

        Returns {'network_pods': {host: wrap_var(host_def)}} so the plugin can
        set the network_pods global variable after processing all hosts.
        wrap_var marks all strings in host_def as AnsibleUnsafeText so that
        Ansible does not recursively evaluate embedded Jinja2 expressions
        (e.g. "{{ dhcpd_options.config_template }}") when building variable
        scope for when: conditions — which would otherwise cause silent task
        skips for all pods on the cluster host. '''

        hosts = data[valid_keys['hosts']]
        cluster_ssh = self._resolve_cluster_ssh(plugin, hosts, host_def)

        plugin.inventory.add_group('k3s_pods')
        plugin.inventory.add_child('k3s_pods', host)

        plugin.inventory.set_variable(host, 'ansible_connection', 'sshkubectl')
        plugin.inventory.set_variable(host, 'ansible_kubectl_pod', host)
        plugin.inventory.set_variable(host, 'ansible_kubectl_kubeconfig', '/etc/rancher/k3s/k3s.yaml')
        if cluster_ssh:
            plugin.inventory.set_variable(host, 'ansible_host', '%s@%s' % (host, cluster_ssh))

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

        return {'network_pods': {host: wrap_var(host_def)}}
