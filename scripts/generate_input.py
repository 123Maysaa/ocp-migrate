#!/usr/bin/env python3
import json
import os
import sys
import subprocess
import yaml

target_ns = sys.argv[1]
raw_workloads = sys.argv[2].replace(',', ' ').split()

def run_oc(args):
    cmd = ['oc'] + args + ['-n', target_ns, '--insecure-skip-tls-verify=true']
    res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    return res.stdout.strip() if res.returncode == 0 else ""

result_data = {"namespace": target_ns, "deploymentconfigs": [], "deployments": []}

for name in raw_workloads:
    if not name:
        continue
    
    # 1. Cek DeploymentConfig
    if run_oc(['get', 'dc', name, '-o', 'name']):
        containers_str = run_oc(['get', 'dc', name, '-o', "jsonpath={.spec.template.spec.containers[*].name}"])
        containers_list = []
        for c in containers_str.split():
            # Get secrets
            secrets_raw = run_oc(['get', 'dc', name, '-o', f"jsonpath={{.spec.template.spec.containers[?(@.name=='{c}')].env[*].valueFrom.secretKeyRef.name}}"])
            secrets = sorted(list(set(secrets_raw.split())))
            
            # Get configmaps
            cms_raw = run_oc(['get', 'dc', name, '-o', f"jsonpath={{.spec.template.spec.containers[?(@.name=='{c}')].envFrom[*].configMapRef.name}}"])
            cms = sorted(list(set(cms_raw.split())))
            
            # Get literal envs
            envs_raw = run_oc(['get', 'dc', name, '-o', f"jsonpath={{.spec.template.spec.containers[?(@.name=='{c}')].env[?(@.value)].name}}"])
            envs = sorted(list(set(envs_raw.split())))
            
            containers_list.append({"name": c, "secrets": secrets, "configmap": cms, "env": envs})
        
        result_data["deploymentconfigs"].append({"name": name, "containers": containers_list})

    # 2. Cek Deployment
    if run_oc(['get', 'deploy', name, '-o', 'name']):
        containers_str = run_oc(['get', 'deploy', name, '-o', "jsonpath={.spec.template.spec.containers[*].name}"])
        containers_list = []
        for c in containers_str.split():
            secrets_raw = run_oc(['get', 'deploy', name, '-o', f"jsonpath={{.spec.template.spec.containers[?(@.name=='{c}')].env[*].valueFrom.secretKeyRef.name}}"])
            secrets = sorted(list(set(secrets_raw.split())))
            
            cms_raw = run_oc(['get', 'deploy', name, '-o', f"jsonpath={{.spec.template.spec.containers[?(@.name=='{c}')].envFrom[*].configMapRef.name}}"])
            cms = sorted(list(set(cms_raw.split())))
            
            envs_raw = run_oc(['get', 'deploy', name, '-o', f"jsonpath={{.spec.template.spec.containers[?(@.name=='{c}')].env[?(@.value)].name}}"])
            envs = sorted(list(set(envs_raw.split())))
            
            containers_list.append({"name": c, "secrets": secrets, "configmap": cms, "env": envs})
            
        result_data["deployments"].append({"name": name, "containers": containers_list})

# Cleanup empty lists
if not result_data["deploymentconfigs"]:
    del result_data["deploymentconfigs"]
if not result_data["deployments"]:
    del result_data["deployments"]

output = {"data": [result_data]}
with open('migrate.yaml', 'w') as f:
    yaml.dump(output, f, default_flow_style=False)

print("migrate.yaml berhasil dibuat secara presisi.")
