import copy
import json

# Annotations that change on every Ansible run (run-id, user path, machine
# version).  Stripped before comparing desired vs current so routine runs do
# not trigger spurious 'changed' reports or unnecessary resource writes.
_VOLATILE_ANNOTATIONS = frozenset({
    'ansible.run-id',
    'ansible.config_file',
    'ansible.version',
    # Set by kubectl client-side apply; contains the full previous desired spec
    # (including volatile annotations from the last run) so it always differs.
    'kubectl.kubernetes.io/last-applied-configuration',
})

# Kubernetes-managed metadata fields that are not part of the desired spec.
_META_RUNTIME_KEYS = frozenset({
    'resourceVersion', 'uid', 'creationTimestamp', 'generation',
    'managedFields', 'selfLink', 'ownerReferences',
})

# Kind-specific fields managed by the cluster that should be ignored during
# content comparison (e.g. Kubernetes injects .secrets into ServiceAccounts).
_KIND_RUNTIME_KEYS = {
    'ServiceAccount': frozenset({'secrets', 'imagePullSecrets'}),
}


def _strip_volatile_annots(node, key):
    """Strip volatile annotations from node[key] in-place, ignoring missing paths."""
    try:
        node[key] = {k: v for k, v in node[key].items() if k not in _VOLATILE_ANNOTATIONS}
    except (KeyError, TypeError):
        pass


def _strip_volatile(obj):
    """Return a deep copy of obj with volatile annotations removed.

    Strips from both metadata.annotations (resource-level) and
    spec.template.metadata.annotations (pod template in Deployments etc.)
    so that annotation value changes in either location do not trigger
    spurious applies or pod rollouts.
    """
    if not isinstance(obj, dict):
        return obj
    r = copy.deepcopy(obj)
    _strip_volatile_annots(r.get('metadata') or {}, 'annotations')
    _strip_volatile_annots(
        ((r.get('spec') or {}).get('template') or {}).get('metadata') or {},
        'annotations'
    )
    return r


class FilterModule(object):
    def filters(self):
        return {
            'k3s_content_changed':   self.k3s_content_changed,
            'k3s_resource_index':    self.k3s_resource_index,
            'k3s_ns_resource_index': self.k3s_ns_resource_index,
        }

    def k3s_content_changed(self, desired, current):
        """Return True if resource content (excluding volatile annotations) differs.

        Strips ansible.run-id, ansible.config_file, and ansible.version before
        comparing so that changes to those fields alone do not cause spurious
        'changed' reports or unnecessary Kubernetes writes.  Also ignores
        Kubernetes runtime-only metadata (resourceVersion, uid, etc.).

        Uses kubectl.kubernetes.io/last-applied-configuration as the comparison
        baseline when present.  This annotation (set by client-side apply) stores
        exactly what was applied last run — none of the defaults or extra fields
        the API server adds — so comparing desired vs last-applied correctly
        detects real changes while ignoring API server normalisation (e.g. CRD
        spec defaults, kubernetes.io/metadata.name on Namespaces, clusterIP on
        Services, container defaults on Deployments, etc.).

        Falls back to comparing against the live resource when the annotation is
        absent (resource was never client-side applied or was created externally).

        Returns True when current is falsy (resource does not yet exist).

        Usage in Jinja2:  desired_dict | k3s_content_changed(current_or_none)
        """
        if not current:
            return True

        # Policy check: if the desired resource carries a volatile annotation
        # key that is absent from the live resource, the annotation must be
        # written (policy requires it to be present).  This is checked
        # separately from the content comparison below, which strips all
        # volatile annotations from both sides so that value-only changes
        # (e.g. a new run-id when nothing else changed) do not trigger a
        # spurious apply.
        _policy_keys = _VOLATILE_ANNOTATIONS - {
            'kubectl.kubernetes.io/last-applied-configuration'
        }
        d_annots = (desired.get('metadata') or {}).get('annotations') or {}
        c_annots = (current.get('metadata') or {}).get('annotations') or {}
        if any(k in d_annots and k not in c_annots for k in _policy_keys):
            return True

        d = _strip_volatile(desired)

        # Prefer last-applied-configuration over live resource state as baseline
        last_applied_str = None
        try:
            annots = (current.get('metadata') or {}).get('annotations') or {}
            last_applied_str = annots.get(
                'kubectl.kubernetes.io/last-applied-configuration')
        except (AttributeError, TypeError):
            pass

        baseline = None
        if last_applied_str:
            try:
                baseline = _strip_volatile(json.loads(last_applied_str))
            except (ValueError, TypeError):
                pass
        if baseline is None:
            baseline = _strip_volatile(current)

        # Compare metadata minus runtime-only keys
        b_meta = {k: v for k, v in baseline.get('metadata', {}).items()
                  if k not in _META_RUNTIME_KEYS}
        d_meta = {k: v for k, v in d.get('metadata', {}).items()
                  if k not in _META_RUNTIME_KEYS}

        # Normalise missing vs empty dicts so they compare equal
        for key in ('annotations', 'labels'):
            if b_meta.get(key) == {} and key not in d_meta:
                b_meta.pop(key)
            if d_meta.get(key) == {} and key not in b_meta:
                d_meta.pop(key)

        if b_meta != d_meta:
            return True

        # Compare all content fields (skip metadata, status, and kind-specific
        # runtime fields such as ServiceAccount .secrets)
        kind = desired.get('kind') or baseline.get('kind')
        kind_specific_skip = _KIND_RUNTIME_KEYS.get(kind, frozenset())
        skip = {'metadata', 'status', 'apiVersion', 'kind'} | kind_specific_skip
        for key in (set(baseline) | set(d)) - skip:
            if baseline.get(key) != d.get(key):
                return True

        # Out-of-band scale check: spec.replicas in the desired manifest must
        # match the LIVE object, not just the last-applied baseline.  kubectl
        # scale (or an HPA) patches only spec.replicas without updating the
        # annotation, so the baseline comparison above misses a manual
        # scale-down forever and the replica count is never reconciled
        if desired.get('kind') in ('Deployment', 'StatefulSet', 'ReplicaSet'):
            _d_replicas = (d.get('spec') or {}).get('replicas')
            _c_replicas = (current.get('spec') or {}).get('replicas')
            if _d_replicas is not None and _c_replicas is not None \
                    and _d_replicas != _c_replicas:
                return True

        return False

    def k3s_resource_index(self, resources):
        """Build a {kind/name: resource} dict from a list of Kubernetes resources.

        Suitable as a pre-fetch lookup table so multi-document template loops
        can look up the current live resource by kind and name without key
        collisions (e.g. a pool and an advertisement both named 'main').

        Usage:  k8s_info_result.resources | k3s_resource_index
        """
        index = {}
        for r in (resources or []):
            try:
                key = '{}/{}'.format(r['kind'], r['metadata']['name'])
                index[key] = r
            except (KeyError, TypeError):
                pass
        return index

    def k3s_ns_resource_index(self, resources):
        """Build a {namespace/name: resource} dict from a list of Kubernetes resources.

        Like k3s_resource_index but keyed by namespace/name instead of kind/name.
        Suitable for namespace-scoped resources of a single kind (e.g. NetworkPolicy)
        where the same name may appear in multiple namespaces.

        Usage:  k8s_info_result.resources | k3s_ns_resource_index
        """
        index = {}
        for r in (resources or []):
            try:
                key = '{}/{}'.format(r['metadata']['namespace'], r['metadata']['name'])
                index[key] = r
            except (KeyError, TypeError):
                pass
        return index
