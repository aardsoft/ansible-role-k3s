class FilterModule(object):
    def filters(self):
        return {
            'k3s_np_expected_names':        self.k3s_np_expected_names,
            'k3s_np_expected_names_system': self.k3s_np_expected_names_system,
            'k3s_np_labels':                self.k3s_np_labels,
            'k3s_np_effective_namespaces':  self.k3s_np_effective_namespaces,
            'k3s_np_name_collisions':       self.k3s_np_name_collisions,
            'k3s_np_unmatched_areas':       self.k3s_np_unmatched_areas,
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

        Each entry in the list becomes a 'network/<name>: true' label on the
        pod, matching the names defined in network_policies.service_vlans,
        network_policies.access_areas, and the built-in 'dmz' and
        'cluster-service' zones.  Reusing a name across the label list and
        a config section is legitimate (the label opts the pod into the
        section's policies); what must NOT collide is the section-key space
        itself — see k3s_np_name_collisions.

        Reserved labels with dedicated policies: 'api-server' opts the pod
        into Kubernetes API egress (allow-api-server-egress).  A config
        section reusing the name does NOT widen the opted pods' egress (an
        'api-server' section generates allow-api-server-ingress and matches
        the different network/api-server-access label on its egress side) —
        but the SAME label matching two unrelated policies with opposite
        postures (opt-in API egress vs. an ingress area the pod has nothing
        to receive from) is exactly the confusion the name reservation
        prevents, so k3s_np_name_collisions fails the run for it (guarded
        on k3s_cidrs, like 'dmz' is on dmz_cidrs).
        """
        labels = {}
        for zone in (k3s_cfg or {}).get('network_policy_labels', []):
            labels['network/' + zone] = 'true'
        return labels

    def k3s_np_name_collisions(self, np_cfg):
        """Return NetworkPolicy base names that would be generated twice.

        The sections dmz (dmz_cidrs set), access_areas and service_vlans
        all generate 'allow-<name>-ingress' and — except for dmz egress
        naming — 'allow-<name>-access-egress' policies.  Two sections
        using the same name (e.g. an access_areas entry and a
        service_vlans entry both named 'mgmt') render two policies with
        the same (namespace, name): sequential apply means last-wins and
        the loser re-applies every run (permanent churn).  Also returns
        collisions with the built-in names ('cluster-services', and 'dmz'
        when dmz_cidrs is set).

        Returns the list of conflicting names; an empty list means the
        config is collision-free.

        Deliberately over-strict: section keys are counted even when their
        section would render nothing (an empty access_areas entry, a
        service_vlans entry with neither `namespace` nor `cidrs`), so a
        colliding-but-empty name still fails the run.  A config like that
        is a latent break-in-waiting the moment someone fills the entry
        in — failing now is the safe direction, and keeping the check
        name-based keeps it render-independent.

        Also reserved: 'api-server' when k3s_cidrs is configured (the
        label-opt-in Kubernetes API egress policy).  An access-area or zone
        reusing the name does not collide at the OBJECT level (different
        generated policy names), but its 'network/api-server' label would
        be the same label that opts pods into API egress — two unrelated
        policies with opposite postures matching one label.  Fail loudly
        instead; rename the section.
        """
        cfg = np_cfg or {}
        reserved = ['cluster-services']
        if cfg.get('dmz_cidrs'):
            reserved.append('dmz')
        if cfg.get('k3s_cidrs'):
            reserved.append('api-server')
        seen = {}
        for name in reserved + sorted(cfg.get('access_areas') or {},
                                      key=str) + sorted(cfg.get('service_vlans') or {},
                                                        key=str):
            seen.setdefault(name, 0)
            seen[name] += 1
        return [name for name, count in seen.items() if count > 1]

    def k3s_np_unmatched_areas(self, k3s_cfg):
        """Return MetalLB pool access_area values that name no policy section.

        traefik-ansible-label.j2 derives 'network/<area>' pod labels from
        metallb.pools.<pool>.access_area.  If <area> is not resolvable to
        an allow-<area>-ingress policy in the system namespaces, the label
        renders but is inert and external LB traffic to the pool stays
        blackholed with NO failing task anywhere — the same silent-hazard
        class the name-collision assert guards against.

        Resolvable means: an access_areas entry with non-empty cidrs, a
        cidr/vlan-defined service_vlans zone, or 'dmz' when dmz_cidrs is
        set.  A namespace-defined service_vlans zone is NOT resolvable
        regardless of its cidrs — its ingress policy renders only inside
        the zone namespace, and the traefik LB pods live in kube-system, so
        the label never matches anything there.

        Returns the sorted list of unknown area values; empty means every
        access_area resolves to a policy section.
        """
        cfg = k3s_cfg or {}
        np_cfg = cfg.get('network_policies') or {}

        # Only CIDR-defined areas count as resolvable for the LB pods, which
        # live in kube-system: an access_areas entry renders its ingress
        # policy there only when it has non-empty cidrs, and a
        # namespace-defined service_vlans zone renders allow-<zone>-ingress
        # ONLY inside the zone namespace (`_namespace == _vlan_ns` guard in
        # both templates) — never where traefik runs.  Without this
        # discriminator the filter false-passes those pools and they stay
        # blackholed with no failing task.
        known = set()
        for name, area in (np_cfg.get('access_areas') or {}).items():
            if (area or {}).get('cidrs'):
                known.add(name)
        for name, zone in (np_cfg.get('service_vlans') or {}).items():
            zone = zone or {}
            if zone.get('namespace'):
                continue
            if zone.get('vlan') is not None:
                # CIDRs come from dhcp_networks[vlan].subnets at render time;
                # a bogus vlan ref fails the template render loudly instead
                known.add(name)
            elif zone.get('cidrs') or zone.get('cidr'):
                known.add(name)
        if np_cfg.get('dmz_cidrs'):
            known.add('dmz')
        pools = (cfg.get('metallb') or {}).get('pools') or {}
        used = {p['access_area'] for p in pools.values()
                if isinstance(p, dict) and p.get('access_area')}
        return sorted(used - known)

    def k3s_np_expected_names(self, np_cfg):
        """Return the list of NetworkPolicy names that network-policies.yaml.j2
        would generate for the given network_policies config block.

        Mirrors the template logic so the stale-cleanup task can diff against
        what is currently deployed without re-parsing the rendered YAML.

        For namespace-defined zones (service_vlans entry with a `namespace` key)
        the zone-namespace policies (allow-<zone>-ingress, allow-<zone>-intra)
        are listed unconditionally even when the zone namespace is currently
        excluded from management: stale cleanup only ever DELETES policies
        whose names are not in this list, so erring on the superset side is
        the safe direction.
        """
        names = [
            'baseline',
            'allow-cluster-services-ingress',
            # rendered only when k3s_cidrs is configured, unconditional here
            # per the superset convention (stale cleanup must still match it)
            'allow-api-server-egress',
        ]

        if np_cfg.get('dmz_cidrs'):
            names.extend(['allow-dmz-ingress', 'allow-dmz-egress'])

        for area_name in np_cfg.get('access_areas') or {}:
            names.append('allow-{}-access-egress'.format(area_name))
            names.append('allow-{}-ingress'.format(area_name))

        for zone_name, zone in np_cfg.get('service_vlans', {}).items():
            # both zone flavors create the access-egress policy everywhere
            names.append('allow-{}-access-egress'.format(zone_name))
            names.append('allow-{}-ingress'.format(zone_name))
            if zone.get('namespace'):
                # namespace-defined zone: the intra policy additionally
                # renders inside the zone namespace once it is managed
                names.append('allow-{}-intra'.format(zone_name))

        return names

    def k3s_np_expected_names_system(self, np_cfg):
        """Return the list of NetworkPolicy names that network-policies-system.yaml.j2
        would generate for system namespaces.

        Mirrors the template logic for system namespace policies.  See
        k3s_np_expected_names for the superset convention on namespace zones.
        """
        names = [
            'baseline',
            'allow-coredns-upstream-egress',
            'allow-cert-manager-https-egress',
            'allow-cluster-services-ingress',
        ]

        if np_cfg.get('dmz_cidrs'):
            names.extend(['allow-dmz-ingress', 'allow-dmz-egress'])

        for area_name in np_cfg.get('access_areas') or {}:
            names.append('allow-{}-access-egress'.format(area_name))
            names.append('allow-{}-ingress'.format(area_name))

        for zone_name, zone in np_cfg.get('service_vlans', {}).items():
            names.append('allow-{}-access-egress'.format(zone_name))
            names.append('allow-{}-ingress'.format(zone_name))
            if zone.get('namespace'):
                names.append('allow-{}-intra'.format(zone_name))

        return names
