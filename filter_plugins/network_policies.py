class FilterModule(object):
    def filters(self):
        return {
            'k3s_np_expected_names':        self.k3s_np_expected_names,
            'k3s_np_expected_names_system': self.k3s_np_expected_names_system,
            'k3s_np_labels':                self.k3s_np_labels,
            'k3s_np_effective_namespaces':  self.k3s_np_effective_namespaces,
        }

    def k3s_np_effective_namespaces(self, np_cfg, all_namespaces=None):
        """Return the list of namespaces that should have policies applied.

        In whitelist mode (default): returns np_cfg.namespaces.
        In blacklist mode: returns all_namespaces minus system_namespaces and
        excluded_namespaces.  all_namespaces should be the list of namespace
        names currently present in the cluster.
        """
        cfg = np_cfg or {}
        mode = cfg.get('mode', 'whitelist')
        system_ns = set(cfg.get('system_namespaces', []))

        if mode == 'blacklist':
            excluded = system_ns | set(cfg.get('excluded_namespaces', []))
            return [ns for ns in (all_namespaces or []) if ns not in excluded]

        return list(cfg.get('namespaces', []))

    def k3s_np_labels(self, k3s_cfg):
        """Convert k3s.network_policy_labels to a pod label dict.

        Each entry in the list becomes a 'network/<zone>: true' label on the
        pod, matching the zone names defined in network_policies.service_vlans
        and the built-in 'dmz' and 'cluster-service' zones.  The zone names
        are arbitrary strings defined entirely by the operator in the cluster
        config — this filter applies no special meaning to any name.

        Returns an empty dict when the key is absent so callers can always
        use | combine(...) without a conditional.
        """
        labels = {}
        for zone in (k3s_cfg or {}).get('network_policy_labels', []):
            labels['network/' + zone] = 'true'
        return labels

    def k3s_np_expected_names(self, np_cfg):
        """Return the list of NetworkPolicy names that network-policies.yaml.j2
        would generate for the given network_policies config block.

        Mirrors the template logic so the stale-cleanup task can diff against
        what is currently deployed without re-parsing the rendered YAML.
        """
        names = [
            'default-deny-all',
            'allow-dns-egress',
            'allow-cluster-services-egress',
            'allow-cluster-services-ingress',
        ]

        for ns in np_cfg.get('system_namespaces', []):
            names.append('allow-to-{}'.format(ns))
            names.append('allow-from-{}'.format(ns))

        if np_cfg.get('dmz_cidrs'):
            names.extend(['allow-dmz-ingress', 'allow-dmz-egress'])

        for vlan_name in np_cfg.get('service_vlans', {}):
            names.append('allow-{}-ingress'.format(vlan_name))
            names.append('allow-{}-access-egress'.format(vlan_name))

        return names

    def k3s_np_expected_names_system(self, np_cfg):
        """Return the list of NetworkPolicy names that network-policies-system.yaml.j2
        would generate for system namespaces.

        Mirrors the template logic for system namespace policies.
        """
        names = [
            'default-deny-all',
            'allow-dns-egress',
            'allow-node-network-ingress',
            'allow-to-node-network',
            'allow-intra-namespace',
            'allow-coredns-upstream-egress',
            'allow-cert-manager-https-egress',
            'allow-cluster-services-egress',
            'allow-cluster-services-ingress',
        ]

        for ns in np_cfg.get('system_namespaces', []):
            names.append('allow-to-{}'.format(ns))
            names.append('allow-from-{}'.format(ns))

        if np_cfg.get('dmz_cidrs'):
            names.extend(['allow-dmz-ingress', 'allow-dmz-egress'])

        for vlan_name in np_cfg.get('service_vlans', {}):
            names.append('allow-{}-ingress'.format(vlan_name))
            names.append('allow-{}-access-egress'.format(vlan_name))

        return names
