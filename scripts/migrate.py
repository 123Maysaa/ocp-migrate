#!/usr/bin/env python3
"""Offline manifest transformation and Vault CLI provisioning (Python stdlib only).

Cluster operations deliberately stay in the Jenkins OpenShift Client Plugin context.
Never log values, CLI stdout/stderr, or complete source/generated manifests.
"""
import base64
import copy
import json
import os
from pathlib import Path
import re
import subprocess
import sys

WORK = Path('.migration-work')
API = 'secrets.hashicorp.com/v1beta1'


class MigrationError(Exception):
    pass


def require(ok, message):
    if not ok:
        raise MigrationError(message)


def read(path):
    with open(path, encoding='utf-8') as stream:
        return json.load(stream)


def write(path, value):
    with open(path, 'w', encoding='utf-8') as stream:
        json.dump(value, stream, ensure_ascii=False)
    os.chmod(path, 0o600)


def name(value, limit=253):
    require(isinstance(value, str) and len(value) <= limit and
            re.fullmatch(r'[a-z0-9](?:[-a-z0-9.]*[a-z0-9])?', value),
            'Nama resource tidak valid atau terlalu panjang')
    return value


def entries(document):
    require(isinstance(document, dict) and isinstance(document.get('data'), list)
            and document['data'], 'migrate.yaml: data harus array tidak kosong')
    result, seen = [], set()
    for group in document['data']:
        ns = name(group['namespace'], 63)
        require('.' not in ns, 'Namespace tidak boleh mengandung titik')
        for field, kind in [('deploymentconfigs', 'deploymentconfig'), ('deployments', 'deployment')]:
            for item in group.get(field, []):
                service = name(item['name'])
                require((ns, service) not in seen, 'Nama workload duplikat dalam namespace')
                seen.add((ns, service))
                require(item.get('containers'), 'Daftar containers wajib diisi')
                containers = set()
                for container in item['containers']:
                    cname = name(container['name'], 63)
                    require(cname not in containers, 'Container duplikat')
                    containers.add(cname)
                    for key in ('secrets', 'configmap', 'env'):
                        require(isinstance(container.get(key, []), list), key + ' harus array')
                    for key in ('secrets', 'configmap'):
                        for resource in container.get(key, []):
                            name(resource)
                    require(all(isinstance(v, str) and v for v in container.get('env', [])),
                            'Nama env harus string tidak kosong')
                result.append(dict(namespace=ns, name=service, kind=kind, containers=item['containers']))
    require(result, 'Tidak ada workload untuk dimigrasikan')
    return result


def make_requests():
    config = read(WORK / 'config.json')
    for key in ('ocp', 'vaultaddr', 'vaultcred'):
        require(isinstance(config.get(key), str) and config[key] and
                not re.search(r'\s', config[key]), 'env.yaml: ' + key + ' wajib string tanpa whitespace')
    require(re.fullmatch(r'https?://[^\s]+', config['vaultaddr']), 'Alamat Vault harus HTTP/HTTPS')
    requests, seen = [], set()
    for item in entries(read(WORK / 'input.json')):
        targets = [(item['kind'], item['name'])]
        for c in item['containers']:
            targets += [('secret', n) for n in c.get('secrets', [])]
            targets += [('configmap', n) for n in c.get('configmap', [])]
        for kind, resource in targets:
            identity = (item['namespace'], kind, resource)
            if identity not in seen:
                seen.add(identity)
                requests.append(dict(namespace=identity[0], kind=kind, name=resource))
    write(WORK / 'requests.json', requests)
    print('Konfigurasi valid; {} resource sumber akan dibaca'.format(len(requests)))


def resource_data(resource):
    """Canonical bytes for exact comparison, including ConfigMap binaryData."""
    if resource['kind'] == 'Secret':
        return {k: base64.b64decode(v, validate=True) for k, v in resource.get('data', {}).items()}
    data = {k: v.encode('utf-8') for k, v in resource.get('data', {}).items()}
    data.update({k: base64.b64decode(v, validate=True) for k, v in resource.get('binaryData', {}).items()})
    return data


def prefixed_labels(labels):
    result = {}
    for key, value in labels.items():
        new_value = 'new-' + value
        require(len(new_value) <= 63, 'Label terlalu panjang setelah prefix: ' + key)
        result[key] = new_value
    return result


def selected_sources(config):
    return ({('secret', n) for n in config.get('secrets', [])} |
            {('configmap', n) for n in config.get('configmap', [])})


def ref_source(ref):
    if 'secretKeyRef' in ref:
        return 'secret', ref['secretKeyRef']
    if 'configMapKeyRef' in ref:
        return 'configmap', ref['configMapKeyRef']
    return None, None


def secret_env(var, key, destination, optional=False):
    ref = dict(name=destination, key=key)
    if optional:
        ref['optional'] = True
    return dict(name=var, valueFrom=dict(secretKeyRef=ref))


def transform_container(container, config, sources, destination, merged):
    selected = selected_sources(config)
    chosen_env = set(config.get('env', []))
    original_env = container.get('env', [])
    require(chosen_env <= {v['name'] for v in original_env},
            'Env pilihan tidak ditemukan pada container ' + container['name'])
    for entry in original_env:
        if entry['name'] in chosen_env:
            # Dynamic fieldRef/resourceFieldRef cannot be frozen into a shared static value.
            require('value' in entry or 'valueFrom' not in entry,
                    'Env pilihan harus literal; valueFrom dipindah melalui daftar Secret/ConfigMap: ' + entry['name'])
            value = entry.get('value', '')
            require('$(' not in value, 'Env dengan ekspansi runtime belum didukung: ' + entry['name'])
            merged[entry['name']] = value.encode('utf-8')

    # Preserve envFrom usage and its prefix. All migrated references share one Secret.
    # Consequently envFrom exposes the merged key set, as documented in README.
    all_from = container.get('envFrom', [])
    rewritten_from = []
    for entry in all_from:
        kind = 'secret' if 'secretRef' in entry else 'configmap'
        ref = entry.get('secretRef', entry.get('configMapRef', {}))
        identity = (kind, ref.get('name'))
        if identity not in selected:
            rewritten_from.append(entry)
            continue
        converted = copy.deepcopy(entry)
        converted.pop('configMapRef', None)
        converted['secretRef'] = dict(ref, name=destination)
        rewritten_from.append(converted)
    rewritten = []
    for entry in original_env:
        if entry['name'] in chosen_env:
            rewritten.append(secret_env(entry['name'], entry['name'], destination))
        else:
            kind, ref = ref_source(entry.get('valueFrom', {}))
            if ref and (kind, ref['name']) in selected:
                rewritten.append(secret_env(entry['name'], ref['key'], destination, ref.get('optional', False)))
            else:
                rewritten.append(entry)
    if original_env:
        container['env'] = rewritten
    if all_from:
        container['envFrom'] = rewritten_from


def rewrite_volume_source(source, kind, sources, selected, destination, projected=False):
    name_key = 'name' if kind == 'configmap' or projected else 'secretName'
    identity = (kind, source[name_key])
    if identity not in selected:
        return None
    result = copy.deepcopy(source)
    result.pop(name_key)
    result['name' if projected else 'secretName'] = destination
    if 'items' not in result:
        keys = sorted(sources[identity])
        require(keys, 'Volume sumber kosong tidak dapat dipetakan ke Secret gabungan')
        result['items'] = [dict(key=k, path=k) for k in keys]
    return result


def deployment(source, item, sources, destination, merged):
    template = copy.deepcopy(source['spec']['template'])
    meta = template.setdefault('metadata', {})
    meta.pop('creationTimestamp', None)
    meta['labels'] = prefixed_labels(meta.get('labels', {}))
    require(meta['labels'], 'Pod sumber harus mempunyai label untuk selector Deployment baru')
    # Remove controller-managed metadata rather than propagating source rollout state.
    for key in ('name', 'namespace', 'uid', 'resourceVersion', 'ownerReferences', 'managedFields', 'generateName'):
        meta.pop(key, None)
    pod = template['spec']
    configs = {c['name']: c for c in item['containers']}
    all_containers = pod.get('containers', []) + pod.get('initContainers', [])
    require(set(configs) <= {c['name'] for c in all_containers}, 'Container pilihan tidak ditemukan')
    selected = set()
    for container in all_containers:
        if container['name'] in configs:
            conf = configs[container['name']]
            selected |= selected_sources(conf)
            transform_container(container, conf, sources, destination, merged)
    # Volumes are pod-scoped. Only rewrite if every mounting container selected the source.
    for volume in pod.get('volumes', []):
        identities = []
        for field, kind in [('secret', 'secret'), ('configMap', 'configmap')]:
            if field in volume:
                ref = volume[field]
                identities.append((kind, ref.get('secretName', ref.get('name'))))
        for projection in volume.get('projected', {}).get('sources', []):
            for field, kind in [('secret', 'secret'), ('configMap', 'configmap')]:
                if field in projection:
                    identities.append((kind, projection[field]['name']))
        for identity in set(identities) & selected:
            consumers = [c for c in all_containers if any(m['name'] == volume['name'] for m in c.get('volumeMounts', []))]
            require(all(identity in selected_sources(configs.get(c['name'], {})) for c in consumers),
                    'Volume bersama harus dipilih pada seluruh container pemakai: ' + volume['name'])
        if 'configMap' in volume:
            changed = rewrite_volume_source(volume['configMap'], 'configmap', sources, selected, destination)
            if changed is not None:
                del volume['configMap']
                volume['secret'] = changed
        elif 'secret' in volume:
            changed = rewrite_volume_source(volume['secret'], 'secret', sources, selected, destination)
            if changed is not None:
                volume['secret'] = changed
        for projection in volume.get('projected', {}).get('sources', []):
            for field, kind in [('configMap', 'configmap'), ('secret', 'secret')]:
                if field in projection:
                    changed = rewrite_volume_source(projection[field], kind, sources, selected, destination, True)
                    if changed is not None:
                        del projection[field]
                        projection['secret'] = changed
                    break
    spec = dict(replicas=1, selector=dict(matchLabels=copy.deepcopy(meta['labels'])), template=template)
    for field in ('minReadySeconds', 'revisionHistoryLimit', 'progressDeadlineSeconds'):
        if field in source['spec']:
            spec[field] = source['spec'][field]
    strategy = source['spec'].get('strategy', {})
    if item['kind'] == 'deployment':
        if strategy:
            spec['strategy'] = copy.deepcopy(strategy)
    else:
        strategy_type = strategy.get('type', 'Rolling')
        require(strategy_type in ('Rolling', 'Recreate'), 'Strategi DC Custom belum didukung')
        for param in ('rollingParams', 'recreateParams'):
            require(not any(strategy.get(param, {}).get(h) for h in ('pre', 'mid', 'post')),
                    'DC lifecycle hook perlu konversi manual')
        spec['strategy'] = dict(type='Recreate' if strategy_type == 'Recreate' else 'RollingUpdate')
        if strategy_type == 'Rolling':
            rolling = {k: v for k, v in strategy.get('rollingParams', {}).items()
                       if k in ('maxSurge', 'maxUnavailable')}
            if rolling:
                spec['strategy']['rollingUpdate'] = rolling
        # Freeze currently resolved template images; DC ImageChange triggers are not copied.
    require(all(c.get('image') for c in all_containers), 'Image container sumber belum terisi')
    annotations = copy.deepcopy(source['metadata'].get('annotations', {}))
    for key in list(annotations):
        if key in ('kubectl.kubernetes.io/last-applied-configuration', 'image.openshift.io/triggers') or key.startswith(('deployment.kubernetes.io/', 'openshift.io/deployment')):
            del annotations[key]
    annotations['migration.local/source'] = item['kind'] + '/' + item['name']
    return dict(apiVersion='apps/v1', kind='Deployment', metadata=dict(
        name=name('new-' + item['name']), namespace=item['namespace'],
        labels=prefixed_labels(source['metadata'].get('labels', {})), annotations=annotations), spec=spec)


def manifest(kind, ns, resource_name, spec):
    return dict(apiVersion=API, kind=kind, metadata=dict(name=name(resource_name), namespace=ns), spec=spec)


def prepare():
    config = read(WORK / 'config.json')
    snapshots = {}
    for index, request in enumerate(read(WORK / 'requests.json')):
        snapshots[(request['namespace'], request['kind'], request['name'])] = read(WORK / ('source-{}.json'.format(index)))
    plan = []
    for index, item in enumerate(entries(read(WORK / 'input.json'))):
        ns, service = item['namespace'], item['name']
        destination = name('vaultsecret-' + ns + '-' + service)
        for generated_secret in (destination, 'holder-secret-' + service):
            require((ns, 'secret', generated_secret) not in snapshots,
                    'Nama Secret tujuan bertabrakan dengan Secret sumber: ' + generated_secret)
        sources, merged = {}, {}
        for c in item['containers']:
            for kind, resource in sorted(selected_sources(c)):
                sources[(kind, resource)] = resource_data(snapshots[(ns, kind, resource)])
                merged.update(sources[(kind, resource)])  # Initial scope assumes no conflicting keys.
        source = snapshots[(ns, item['kind'], service)]
        clone = deployment(source, item, sources, destination, merged)
        require(merged, 'Tidak ada data untuk workload ' + service)
        # Static Vault strings preserve all UTF-8 bytes (including trailing newlines).
        # Non-UTF8 binary data needs an explicit VSO transformation, not silent corruption.
        try:
            payload = {k: v.decode('utf-8') for k, v in merged.items()}
        except UnicodeDecodeError:
            raise MigrationError('Data biner non-UTF8 belum didukung: ' + service)
        prefix = str(WORK / str(index))
        record = dict(index=index, namespace=ns, source=service, target=clone['metadata']['name'],
                      destination=destination, mount=ns + '-kv', authMount=ns + '-approle',
                      policy=ns + '-' + service + '-access')
        for field, suffix in [('deploymentFile', 'deployment'), ('connectionFile', 'connection'),
                              ('staticFile', 'static'), ('holderFile', 'holder'), ('authFile', 'auth'),
                              ('payloadFile', 'payload'), ('expectedFile', 'expected'), ('actualFile', 'actual')]:
            record[field] = prefix + '-' + suffix + '.json'
        write(record['deploymentFile'], clone)
        write(record['payloadFile'], payload)
        write(record['expectedFile'], {k: base64.b64encode(v).decode('ascii') for k, v in merged.items()})
        write(record['connectionFile'], manifest('VaultConnection', ns, 'vault-connection-' + ns,
              dict(address=config['vaultaddr'], skipTLSVerify=True)))
        write(record['staticFile'], manifest('VaultStaticSecret', ns, 'vaultstaticsecret-' + ns + '-' + service,
              dict(vaultAuthRef='vaultauth-' + service, mount=record['mount'], type='kv-v2', path=service,
                   refreshAfter='5s', destination=dict(create=True, name=destination,
                       transformation=dict(excludeRaw=True)))))
        plan.append(record)
        print('Siap: {}/{} -> {}; {} key'.format(ns, service, record['target'], len(merged)))
    write(WORK / 'plan.json', plan)


def vault(*args, json_output=False):
    result = subprocess.run(['vault', *args], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    require(result.returncode == 0, 'Vault CLI gagal pada operasi ' + args[0] + '; periksa koneksi dan izin credential')
    return json.loads(result.stdout) if json_output else result.stdout.decode().strip()


def provision():
    plan = read(WORK / 'plan.json')
    auth = vault('auth', 'list', '-format=json', json_output=True)
    mounts = vault('secrets', 'list', '-format=json', json_output=True)
    # Validate all existing mount types before the first mutation.
    for item in plan:
        a, m = auth.get(item['authMount'] + '/'), mounts.get(item['mount'] + '/')
        require(not a or a['type'] == 'approle', 'Auth mount existing bukan AppRole')
        require(not m or (m['type'] == 'kv' and str(m.get('options', {}).get('version')) == '2'),
                'KV mount existing bukan KV v2')
    enabled = set()
    for item in plan:
        ns, service = item['namespace'], item['source']
        if ns not in enabled:
            if item['authMount'] + '/' not in auth:
                vault('auth', 'enable', '-path=' + item['authMount'], 'approle')
            if item['mount'] + '/' not in mounts:
                vault('secrets', 'enable', '-path=' + item['mount'], 'kv-v2')
            enabled.add(ns)
        vault('kv', 'put', '-mount=' + item['mount'], service, '@' + item['payloadFile'])
        policy_file = WORK / ('{}-policy.hcl'.format(item['index']))
        policy_file.write_text('path "' + item['mount'] + '/data/' + service +
                               '" { capabilities = ["read"] }\n', encoding='utf-8')
        vault('policy', 'write', item['policy'], str(policy_file))
        role_path = 'auth/' + item['authMount'] + '/role/' + service
        vault('write', role_path, 'token_policies=' + item['policy'])
        role_id = vault('read', '-field=role_id', role_path + '/role-id')
        secret_id = vault('write', '-field=secret_id', '-f', role_path + '/secret-id')
        require(role_id and secret_id, 'Vault tidak mengembalikan RoleID/SecretID')
        write(item['holderFile'], dict(apiVersion='v1', kind='Secret',
              metadata=dict(name=name('holder-secret-' + service), namespace=ns),
              type='Opaque', stringData=dict(id=secret_id)))
        write(item['authFile'], manifest('VaultAuth', ns, 'vaultauth-' + service,
              dict(vaultConnectionRef='vault-connection-' + ns, method='appRole', mount=item['authMount'],
                   appRole=dict(roleId=role_id, secretRef='holder-secret-' + service))))
        print('Vault siap: ' + item['mount'] + '/' + service)


def verify(index):
    item = read(WORK / 'plan.json')[index]
    expected = read(item['expectedFile'])
    actual = resource_data(dict(read(item['actualFile']), kind='Secret'))
    expected = {k: base64.b64decode(v) for k, v in expected.items()}
    missing = sorted(expected.keys() - actual.keys())
    extra = sorted(actual.keys() - expected.keys())
    different = sorted(k for k in expected.keys() & actual.keys() if expected[k] != actual[k])
    if missing or extra or different:
        print('Menunggu {}: missing={}, extra={}, different={}'.format(item['destination'], missing, extra, different))
        return 1
    print('VERIFIED {}/{}: seluruh {} key/value cocok'.format(item['namespace'], item['destination'], len(expected)))
    return 0


def main():
    os.umask(0o077)
    command = sys.argv[1]
    if command == 'verify':
        return verify(int(sys.argv[2]))
    {'requests': make_requests, 'prepare': prepare, 'provision': provision}[command]()
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except MigrationError as exc:
        print('ERROR: ' + str(exc), file=sys.stderr)
        sys.exit(1)
    except Exception as exc:
        # Parse/subprocess exceptions can include sensitive input. Do not print the payload.
        print('ERROR: {} saat memproses migrasi; periksa struktur input/resource'.format(type(exc).__name__), file=sys.stderr)
        sys.exit(1)
