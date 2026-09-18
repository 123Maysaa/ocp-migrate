#!/usr/bin/env python3
import sys
import subprocess

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
            secrets_raw = run_oc(['get', 'dc', name, '-o', f"jsonpath={{.spec.template.spec.containers[?(@.name=='{c}')].env[*].valueFrom.secretKeyRef.name}}"])
            secrets = sorted(list(set(secrets_raw.split())))
            
            cms_raw = run_oc(['get', 'dc', name, '-o', f"jsonpath={{.spec.template.spec.containers[?(@.name=='{c}')].envFrom[*].configMapRef.name}}"])
            cms = sorted(list(set(cms_raw.split())))
            
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

# Menulis berkas migrate.yaml secara langsung tanpa modul PyYAML
with open('migrate.yaml', 'w') as f:
    f.write("data:\n")
    f.write(f'  - namespace: "{result_data["namespace"]}"\n')
    
    if result_data["deploymentconfigs"]:
        f.write("    deploymentconfigs:\n")
        for dc in result_data["deploymentconfigs"]:
            f.write(f'      - name: "{dc["name"]}"\n')
            f.write("        containers:\n")
            for c in dc["containers"]:
                f.write(f'          - name: "{c["name"]}"\n')
                if c["secrets"]:
                    f.write("            secrets:\n")
                    for s in c["secrets"]:
                        f.write(f'              - "{s}"\n')
                if c["configmap"]:
                    f.write("            configmap:\n")
                    for cm in c["configmap"]:
                        f.write(f'              - "{cm}"\n')
                if c["env"]:
                    f.write("            env:\n")
                    for e in c["env"]:
                        f.write(f'              - "{e}"\n')

    if result_data["deployments"]:
        f.write("    deployments:\n")
        for d in result_data["deployments"]:
            f.write(f'      - name: "{d["name"]}"\n')
            f.write("        containers:\n")
            for c in d["containers"]:
                f.write(f'          - name: "{c["name"]}"\n')
                if c["secrets"]:
                    f.write("            secrets:\n")
                    for s in c["secrets"]:
                        f.write(f'              - "{s}"\n')
                if c["configmap"]:
                    f.write("            configmap:\n")
                    for cm in c["configmap"]:
                        f.write(f'              - "{cm}"\n')
                if c["env"]:
                    f.write("            env:\n")
                    for e in c["env"]:
                        f.write(f'              - "{e}"\n')

print("migrate.yaml berhasil dibuat secara presisi.")
