import base64
import copy
import importlib.util
import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('migrate', Path(__file__).parents[1] / 'scripts/migrate.py')
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


def fixture(kind='deploymentconfig'):
    item = dict(namespace='task-api-a', name='pikachu-dc', kind=kind, containers=[
        dict(name='pikachu', secrets=['credentials'], configmap=['settings'], env=['APP_MODE'])])
    source = dict(apiVersion='apps.openshift.io/v1', kind='DeploymentConfig',
                  metadata=dict(name='pikachu-dc', uid='source-uid', labels={'app': 'pikachu'},
                                annotations={'image.openshift.io/triggers': 'old-trigger'}),
                  spec=dict(replicas=3, selector={'app': 'pikachu'}, strategy=dict(type='Rolling'),
                            template=dict(metadata=dict(labels={'app': 'pikachu'}), spec=dict(containers=[
                                dict(name='pikachu', image='example/app@sha256:abc',
                                     env=[dict(name='APP_MODE', value='testing'),
                                          dict(name='PASSWORD', valueFrom=dict(secretKeyRef=dict(name='credentials', key='PASSWORD'))),
                                          dict(name='LOG', valueFrom=dict(configMapKeyRef=dict(name='settings', key='LOG'))),
                                          dict(name='KEEP', value='unchanged')],
                                     envFrom=[dict(secretRef=dict(name='credentials')),
                                              dict(prefix='CFG_', configMapRef=dict(name='settings'))],
                                     volumeMounts=[dict(name='settings-volume', mountPath='/config')])],
                                volumes=[dict(name='settings-volume', configMap=dict(name='settings', defaultMode=288))]))))
    sources = {('secret', 'credentials'): {'PASSWORD': b'p\n', 'UNUSED': b'keep'},
               ('configmap', 'settings'): {'LOG': b'info', 'app.conf': b'line1\nline2\n'}}
    return item, source, sources


class TransformationTests(unittest.TestCase):
    def test_dc_clone_keeps_source_and_rewrites_config(self):
        item, source, sources = fixture()
        original = copy.deepcopy(source)
        merged = {}
        result = m.deployment(source, item, sources, 'merged-secret', merged)
        self.assertEqual(source, original)
        self.assertEqual(result['kind'], 'Deployment')
        self.assertEqual(result['apiVersion'], 'apps/v1')
        self.assertEqual(result['metadata']['name'], 'new-pikachu-dc')
        self.assertNotIn('uid', result['metadata'])
        self.assertNotIn('image.openshift.io/triggers', result['metadata']['annotations'])
        self.assertEqual(result['spec']['replicas'], 1)
        self.assertEqual(result['spec']['selector']['matchLabels'], {'app': 'new-pikachu'})
        self.assertEqual(result['spec']['template']['metadata']['labels'], {'app': 'new-pikachu'})
        container = result['spec']['template']['spec']['containers'][0]
        for env in container['env'][:3]:
            self.assertEqual(env['valueFrom']['secretKeyRef']['name'], 'merged-secret')
        self.assertEqual(container['env'][3]['value'], 'unchanged')
        self.assertEqual(container['envFrom'][1], dict(prefix='CFG_', secretRef=dict(name='merged-secret')))
        volume = result['spec']['template']['spec']['volumes'][0]['secret']
        self.assertEqual(volume['secretName'], 'merged-secret')
        self.assertEqual(volume['defaultMode'], 288)
        self.assertEqual({x['key'] for x in volume['items']}, {'LOG', 'app.conf'})
        self.assertEqual(merged['APP_MODE'], b'testing')

    def test_deployment_retains_strategy_and_pod_configuration(self):
        item, source, sources = fixture('deployment')
        source['spec']['strategy'] = dict(type='RollingUpdate', rollingUpdate=dict(maxSurge=1, maxUnavailable=0))
        source['spec']['template']['spec']['serviceAccountName'] = 'existing-account'
        source['spec']['template']['spec']['containers'][0]['readinessProbe'] = dict(tcpSocket=dict(port=8080))
        result = m.deployment(source, item, sources, 'merged', {})
        self.assertEqual(result['spec']['strategy'], source['spec']['strategy'])
        self.assertEqual(result['spec']['template']['spec']['serviceAccountName'], 'existing-account')
        self.assertIn('readinessProbe', result['spec']['template']['spec']['containers'][0])

    def test_projected_volume_retains_paths_modes_and_other_sources(self):
        item, source, sources = fixture()
        pod = source['spec']['template']['spec']
        pod['volumes'] = [dict(name='settings-volume', projected=dict(defaultMode=256, sources=[
            dict(configMap=dict(name='settings', items=[dict(key='app.conf', path='nested/app.conf', mode=288)])),
            dict(secret=dict(name='credentials')),
            dict(downwardAPI=dict(items=[dict(path='name', fieldRef=dict(fieldPath='metadata.name'))]))]))]
        result = m.deployment(source, item, sources, 'merged', {})
        projected = result['spec']['template']['spec']['volumes'][0]['projected']
        self.assertEqual(projected['defaultMode'], 256)
        self.assertEqual(projected['sources'][0]['secret']['items'][0]['path'], 'nested/app.conf')
        self.assertEqual(projected['sources'][0]['secret']['items'][0]['mode'], 288)
        self.assertEqual(projected['sources'][1]['secret']['name'], 'merged')
        self.assertIn('downwardAPI', projected['sources'][2])

    def test_shared_volume_requires_all_consumers_selected(self):
        item, source, sources = fixture()
        source['spec']['template']['spec']['containers'].append(dict(name='sidecar', image='sidecar:1',
            volumeMounts=[dict(name='settings-volume', mountPath='/etc/settings')]))
        with self.assertRaisesRegex(m.MigrationError, 'Volume bersama'):
            m.deployment(source, item, sources, 'merged', {})

    def test_multiple_containers_share_destination(self):
        item, source, sources = fixture()
        second = copy.deepcopy(source['spec']['template']['spec']['containers'][0])
        second['name'] = 'worker'
        source['spec']['template']['spec']['containers'].append(second)
        item['containers'].append(dict(item['containers'][0], name='worker'))
        clone = m.deployment(source, item, sources, 'merged', {})
        for container in clone['spec']['template']['spec']['containers']:
            self.assertEqual(container['envFrom'][0]['secretRef']['name'], 'merged')

    def test_dc_hooks_and_dynamic_env_rejected(self):
        item, source, sources = fixture()
        source['spec']['strategy']['rollingParams'] = dict(pre=dict(execNewPod=dict(command=['run'])))
        with self.assertRaisesRegex(m.MigrationError, 'lifecycle hook'):
            m.deployment(source, item, sources, 'merged', {})
        source['spec']['strategy'] = dict(type='Rolling')
        source['spec']['template']['spec']['containers'][0]['env'][0] = dict(
            name='APP_MODE', valueFrom=dict(fieldRef=dict(fieldPath='metadata.name')))
        with self.assertRaisesRegex(m.MigrationError, 'literal'):
            m.deployment(source, item, sources, 'merged', {})

    def test_binary_and_newlines_decoded_exactly(self):
        self.assertEqual(m.resource_data(dict(kind='Secret', data={'X': 'AP8K'})), {'X': b'\x00\xff\n'})
        self.assertEqual(m.resource_data(dict(kind='ConfigMap', data={'X': 'a\n'}, binaryData={'Y': 'Ygo='})),
                         {'X': b'a\n', 'Y': b'b\n'})


class OfflineFlowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.patch = patch.object(m, 'WORK', Path(self.temp.name))
        self.patch.start()
        item, source, sources = fixture()
        m.write(m.WORK / 'config.json', dict(ocp='crc-sip', vaultaddr='http://vault:8200', vaultcred='sip-vault'))
        m.write(m.WORK / 'input.json', dict(data=[dict(namespace=item['namespace'], deploymentconfigs=[
            dict(name=item['name'], containers=item['containers'])])]))
        with redirect_stdout(io.StringIO()):
            m.make_requests()
        for i, request in enumerate(m.read(m.WORK / 'requests.json')):
            if request['kind'] == 'deploymentconfig':
                data = source
            else:
                values = sources[(request['kind'], request['name'])]
                data = dict(kind='Secret' if request['kind'] == 'secret' else 'ConfigMap', data={
                    k: base64.b64encode(v).decode() if request['kind'] == 'secret' else v.decode()
                    for k, v in values.items()})
            m.write(m.WORK / ('source-{}.json'.format(i)), data)

    def tearDown(self):
        self.patch.stop()
        self.temp.cleanup()

    def test_prepare_and_verify_including_unused_key_and_exact_newline(self):
        with redirect_stdout(io.StringIO()):
            m.prepare()
        plan = m.read(m.WORK / 'plan.json')
        self.assertEqual(len(plan), 1)
        item = plan[0]
        self.assertEqual(item['mount'], 'task-api-a-kv')
        self.assertEqual(item['source'], 'pikachu-dc')
        payload = m.read(item['payloadFile'])
        self.assertEqual(payload['UNUSED'], 'keep')
        self.assertEqual(payload['PASSWORD'], 'p\n')
        self.assertEqual(payload['APP_MODE'], 'testing')
        data = m.read(item['expectedFile'])
        m.write(item['actualFile'], dict(data=data))
        with redirect_stdout(io.StringIO()):
            self.assertEqual(m.verify(0), 0)
        data['PASSWORD'] = base64.b64encode(b'p').decode()
        m.write(item['actualFile'], dict(data=data))
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(m.verify(0), 1)
        self.assertIn('PASSWORD', output.getvalue())
        self.assertNotIn('testing', output.getvalue())

    def test_verify_rejects_missing_and_extra_keys(self):
        with redirect_stdout(io.StringIO()):
            m.prepare()
            item = m.read(m.WORK / 'plan.json')[0]
            m.write(item['actualFile'], {})
            self.assertEqual(m.verify(0), 1)
            data = m.read(item['expectedFile'])
            data['EXTRA'] = 'eA=='
            m.write(item['actualFile'], dict(data=data))
            self.assertEqual(m.verify(0), 1)

    def test_provision_scopes_role_policy_and_keeps_values_out_of_arguments(self):
        with redirect_stdout(io.StringIO()):
            m.prepare()
        calls = []

        def fake_vault(*args, **kwargs):
            calls.append(args)
            if args[:2] in [('auth', 'list'), ('secrets', 'list')]:
                return {}
            if '-field=role_id' in args:
                return 'fake-role-id'
            if '-field=secret_id' in args:
                return 'fake-secret-id'
            return ''

        with patch.object(m, 'vault', side_effect=fake_vault), redirect_stdout(io.StringIO()):
            m.provision()
        self.assertIn(('write', 'auth/task-api-a-approle/role/pikachu-dc',
                       'token_policies=task-api-a-pikachu-dc-access'), calls)
        self.assertFalse(any('testing' in arg for call in calls for arg in call))
        item = m.read(m.WORK / 'plan.json')[0]
        self.assertEqual(m.read(item['holderFile'])['stringData']['id'], 'fake-secret-id')
        self.assertEqual(m.read(item['authFile'])['spec']['appRole']['roleId'], 'fake-role-id')

    def test_wrong_mount_type_prevents_mutations(self):
        with redirect_stdout(io.StringIO()):
            m.prepare()
        with patch.object(m, 'vault', side_effect=[{'task-api-a-approle/': {'type': 'userpass'}}, {}]) as cli:
            with self.assertRaises(m.MigrationError):
                m.provision()
        self.assertEqual(cli.call_count, 2)

    def test_generated_secret_must_not_overwrite_any_selected_source(self):
        document = m.read(m.WORK / 'input.json')
        document['data'][0]['deploymentconfigs'][0]['containers'][0]['secrets'].append(
            'vaultsecret-task-api-a-pikachu-dc')
        m.write(m.WORK / 'input.json', document)
        requests = m.read(m.WORK / 'requests.json')
        index = len(requests)
        requests.append(dict(namespace='task-api-a', kind='secret', name='vaultsecret-task-api-a-pikachu-dc'))
        m.write(m.WORK / 'requests.json', requests)
        m.write(m.WORK / ('source-{}.json'.format(index)), dict(kind='Secret', data={'KEY': 'eA=='}))
        with self.assertRaisesRegex(m.MigrationError, 'bertabrakan'):
            m.prepare()


if __name__ == '__main__':
    unittest.main()
