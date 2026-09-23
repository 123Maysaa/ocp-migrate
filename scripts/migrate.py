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
            'Nama resource tidak valid atau terlalu panjang: ' + str(value))
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
            require('value' in entry or 'valueFrom' not in entry,
                    'Env pilihan harus literal; valueFrom dipindah melalui daftar Secret/ConfigMap: ' + entry['name'])
            value = entry.get('value', '')
            require('$(' not in value, 'Env dengan ekspansi runtime belum didukung: ' + entry['name'])
            merged[entry['name']] = value.encode('utf-8')

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
    cluster_name = config.get('cluster_name', 'ocp-dgt-jkt')
    
    snapshots = {}
    for index, request in enumerate(read(WORK / 'requests.json')):
        snapshots[(request['namespace'], request['kind'], request['name'])] = read(WORK / ('source-{}.json'.format(index)))
    
    plan = []
    for index, item in enumerate(entries(read(WORK / 'input.json'))):
        ns, service = item['namespace'], item['name']
        
        # --- ATURAN PENAMAAN BARU ---
        mount_path = ns + '-kv'                   # 1. Mount secret pada vault: [namespace]-kv
        secret_path = service                     # 2. Path secret: [nama secret existing]
        destination = name(service + '-vault')     # 3. Destination secret: [nama secret existing]-vault
        
        approle_name = 'approle-' + cluster_name  # 4. AppRole naming: approle-[cluster_name]
        policy_name = 'policy-' + cluster_name    # 5. Policy naming: policy-[cluster_name]
        
        holder_secret_name = name('holder-secret-' + ns)           # 6. Holder secret: holder-secret-[namespace]
        connection_name = name('vault-connection-' + ns)          # 7. Vault Connection: vault-connection-[namespace]
        auth_name = name('vault-auth-' + ns)                      # 8. Vault Auth: vault-auth-[namespace]
        static_secret_name = name('vault-static-secret-' + service)# 9. Vault Static Secret: vault-static-secret-[nama secret existing]
        
        for generated_secret in (destination, holder_secret_name):
            require((ns, 'secret', generated_secret) not in snapshots,
                    'Nama Secret tujuan bertabrakan dengan Secret sumber: ' + generated_secret)
        
        sources, merged = {}, {}
        for c in item['containers']:
            for kind, resource in sorted(selected_sources(c)):
                sources[(kind, resource)] = resource_data(snapshots[(ns, kind, resource)])
                merged.update(sources[(kind, resource)])
        
        source = snapshots[(ns, item['kind'], service)]
        clone = deployment(source, item, sources, destination, merged)
        require(merged, 'Tidak ada data untuk workload ' + service)
        
        try:
            payload = {k: v.decode('utf-8') for k, v in merged.items()}
        except UnicodeDecodeError:
            raise MigrationError('Data biner non-UTF8 belum didukung: ' + service)
        
        prefix = str(WORK / str(index))
        record = dict(
            index=index,
            namespace=ns,
            source=service,
            target=clone['metadata']['name'],
            destination=destination,
            mount=mount_path,
            vaultPath=secret_path,
            approle=approle_name,
            policy=policy_name,
            holderSecretName=holder_secret_name,
            connectionName=connection_name,
            authName=auth_name,
            staticSecretName=static_secret_name
        )
        
        for field, suffix in [('deploymentFile', 'deployment'), ('connectionFile', 'connection'),
                              ('staticFile', 'static'), ('holderFile', 'holder'), ('authFile', 'auth'),
                              ('payloadFile', 'payload'), ('expectedFile', 'expected'), ('actualFile', 'actual')]:
            record[field] = prefix + '-' + suffix + '.json'
        
        write(record['deploymentFile'], clone)
        write(record['payloadFile'], payload)
        write(record['expectedFile'], {k: base64.b64encode(v).decode('ascii') for k, v in merged.items()})
        
        # Manifest 1: VaultConnection
        write(record['connectionFile'], manifest('VaultConnection', ns, connection_name,
              dict(address=config['vaultaddr'], skipTLSVerify=True)))
        
        # Manifest 2: VaultStaticSecret
        write(record['staticFile'], manifest('VaultStaticSecret', ns, static_secret_name,
              dict(vaultAuthRef=auth_name, mount=mount_path, type='kv-v2', path=secret_path,
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
    auth_list = vault('auth', 'list', '-format=json', json_output=True)
    mounts_list = vault('secrets', 'list', '-format=json', json_output=True)
    
    # 1. Enable AppRole Auth Engine & KV v2 Secret Engine
    if 'approle/' not in auth_list:
        vault('auth', 'enable', 'approle')
        
    enabled_mounts = set()
    for item in plan:
        mount_path = item['mount']
        if mount_path not in enabled_mounts:
            if (mount_path + '/') not in mounts_list:
                vault('secrets', 'enable', '-path=' + mount_path, 'kv-v2')
            enabled_mounts.add(mount_path)
            
        # 2. Put Payload Secret ke Vault Path: [nama secret existing]
        vault('kv', 'put', '-mount=' + mount_path, item['vaultPath'], '@' + item['payloadFile'])
        
        # 3. Write Policy: policy-[cluster_name]
        policy_file = WORK / ('{}-policy.hcl'.format(item['index']))
        policy_file.write_text('path "' + mount_path + '/data/*" { capabilities = ["read"] }\n', encoding='utf-8')
        vault('policy', 'write', item['policy'], str(policy_file))
        
        # 4. Write AppRole: approle-[cluster_name]
        role_path = 'auth/approle/role/' + item['approle']
        vault('write', role_path, 'token_policies=' + item['policy'])
        
        role_id = vault('read', '-field=role_id', role_path + '/role-id')
        secret_id = vault('write', '-field=secret_id', '-f', role_path + '/secret-id')
        require(role_id and secret_id, 'Vault tidak mengembalikan RoleID/SecretID')
        
        # 5. Manifest Holder Secret (Simpan SecretID)
        write(item['holderFile'], dict(
            apiVersion='v1', kind='Secret',
            metadata=dict(name=item['holderSecretName'], namespace=item['namespace']),
            type='Opaque', stringData=dict(id=secret_id)
        ))
        
        # 6. Manifest VaultAuth
        write(item['authFile'], manifest('VaultAuth', item['namespace'], item['authName'],
              dict(vaultConnectionRef=item['connectionName'], method='appRole', mount='approle',
                   appRole=dict(roleId=role_id, secretRef=item['holderSecretName']))))
        
        print('Vault siap: ' + mount_path + '/' + item['vaultPath'])


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
        print('ERROR: {} saat memproses migrasi; periksa struktur input/resource'.format(type(exc).__name__), file=sys.stderr)
        sys.exit(1)
