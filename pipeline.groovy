// Script Path Jenkins: pipeline.groovy.
def config
def requests
def plan

pipeline {
    agent any
    options {
        disableConcurrentBuilds()
        timeout(time: 30, unit: 'MINUTES')
    }
    parameters {
        string(name: 'ENV_FILE', defaultValue: 'env.yaml', description: 'Konfigurasi Jenkins/OCP/Vault')
        string(name: 'SYNC_TIMEOUT_SECONDS', defaultValue: '180', description: 'Batas tunggu sinkronisasi VSO')
        
        // Parameter Choice Standard (Langsung muncul di Dropdown GUI)
        choice(
            name: 'TARGET_NAMESPACE', 
            choices: ['bebas-openshift-vault', 'task-api-a'], 
            description: 'Pilih Project OpenShift yang akan dimigrasikan'
        )
        
        choice(
            name: 'SELECTED_WORKLOADS', 
            choices: ['pikachu-dc gengar-api', 'pikachu-dc', 'gengar-api'], 
            description: 'Pilih Workload yang akan diintegrasikan (Pisahkan dengan spasi jika lebih dari satu)'
        )
    }
    stages {
        stage('Validate Configuration') {
            steps {
                script {
                    sh '''#!/bin/sh
                        set -eu
                        set +x
                        command -v python3 >/dev/null
                        command -v oc >/dev/null
                        command -v vault >/dev/null
                        mkdir -p .migration-work
                        chmod 700 .migration-work
                    '''

                    config = readYaml(file: params.ENV_FILE)

                    // Generate migrate.yaml secara presisi tanpa block kosong
                    sh """#!/bin/bash
                        set -eu
                        
                        RAW_WORKLOADS="${params.SELECTED_WORKLOADS}"
                        CLEAN_WORKLOADS=\$(echo "\$RAW_WORKLOADS" | tr ',' ' ')

                        # Detect ketersediaan DC & Deploy
                        HAS_DC=""
                        HAS_DEPLOY=""
                        for NAME in \$CLEAN_WORKLOADS; do
                            if [ -n "\$NAME" ]; then
                                if oc get dc "\$NAME" -n ${params.TARGET_NAMESPACE} -o name --insecure-skip-tls-verify=true 2>/dev/null | grep -q dc; then
                                    HAS_DC="true"
                                fi
                                if oc get deploy "\$NAME" -n ${params.TARGET_NAMESPACE} -o name --insecure-skip-tls-verify=true 2>/dev/null | grep -q deployment; then
                                    HAS_DEPLOY="true"
                                fi
                            fi
                        done

                        cat <<EOF > migrate.yaml
data:
  - namespace: "${params.TARGET_NAMESPACE}"
EOF

                        # Write DeploymentConfigs jika ada
                        if [ -n "\$HAS_DC" ]; then
                            echo "    deploymentconfigs:" >> migrate.yaml
                            for NAME in \$CLEAN_WORKLOADS; do
                                if oc get dc "\$NAME" -n ${params.TARGET_NAMESPACE} -o name --insecure-skip-tls-verify=true 2>/dev/null | grep -q dc; then
                                    echo "      - name: \"\$NAME\"" >> migrate.yaml
                                    echo "        containers:" >> migrate.yaml
                                    CONTAINERS=\$(oc get dc "\$NAME" -n ${params.TARGET_NAMESPACE} -o jsonpath='{.spec.template.spec.containers[*].name}' --insecure-skip-tls-verify=true)
                                    for c in \$CONTAINERS; do
                                        echo "          - name: \"\$c\"" >> migrate.yaml
                                        echo "            env: [\"APP_MODE\"]" >> migrate.yaml
                                    done
                                fi
                            done
                        fi

                        # Write Deployments jika ada
                        if [ -n "\$HAS_DEPLOY" ]; then
                            echo "    deployments:" >> migrate.yaml
                            for NAME in \$CLEAN_WORKLOADS; do
                                if oc get deploy "\$NAME" -n ${params.TARGET_NAMESPACE} -o name --insecure-skip-tls-verify=true 2>/dev/null | grep -q deployment; then
                                    echo "      - name: \"\$NAME\"" >> migrate.yaml
                                    echo "        containers:" >> migrate.yaml
                                    CONTAINERS=\$(oc get deploy "\$NAME" -n ${params.TARGET_NAMESPACE} -o jsonpath='{.spec.template.spec.containers[*].name}' --insecure-skip-tls-verify=true)
                                    for c in \$CONTAINERS; do
                                        echo "          - name: \"\$c\"" >> migrate.yaml
                                        echo "            env: [\"APP_MODE\"]" >> migrate.yaml
                                    done
                                fi
                            done
                        fi
                    """

                    writeJSON(file: '.migration-work/config.json', json: config)
                    writeJSON(file: '.migration-work/input.json', json: readYaml(file: 'migrate.yaml'))
                    sh 'python3 scripts/migrate.py requests'
                    requests = readJSON(file: '.migration-work/requests.json', returnPojo: true)
                    
                    if (!(params.SYNC_TIMEOUT_SECONDS ==~ /[1-9][0-9]{0,3}/)) {
                        error('SYNC_TIMEOUT_SECONDS harus integer 1 sampai 9999')
                    }
                    env.VAULT_ADDR = config.vaultaddr
                }
            }
        }
        stage('Read Source Resources') {
            steps {
                script {
                    withCredentials([string(credentialsId: config.ocpcred, variable: 'OCP_TOKEN')]) {
                        openshift.withCluster(config.ocp, OCP_TOKEN) {
                            openshift.verbose(false)
                            requests.eachWithIndex { item, index ->
                                openshift.withProject(item.namespace) {
                                    def result = openshift.raw('get', item.kind, item.name, '-o=json', '--certificate-authority=""', '--insecure-skip-tls-verify=true')
                                    writeFile(file: ".migration-work/source-${index}.json", text: result.out)
                                    result = null
                                }
                            }
                        }
                    }
                    sh 'python3 scripts/migrate.py prepare'
                    plan = readJSON(file: '.migration-work/plan.json', returnPojo: true)
                }
            }
        }
        stage('Preflight New Resources') {
            steps {
                script {
                    withCredentials([string(credentialsId: config.ocpcred, variable: 'OCP_TOKEN')]) {
                        openshift.withCluster(config.ocp, OCP_TOKEN) {
                            plan.each { item ->
                                openshift.withProject(item.namespace) {
                                    def existing = openshift.raw('get', 'deployment', item.target,
                                        '--ignore-not-found', '-o=name', '--certificate-authority=""', '--insecure-skip-tls-verify=true').out.trim()
                                    if (existing) { error("Deployment tujuan sudah ada: ${item.namespace}/${item.target}") }
                                    openshift.raw('create', '--dry-run=server', '-f', item.deploymentFile, '-o=name', '--certificate-authority=""', '--insecure-skip-tls-verify=true')
                                    openshift.raw('apply', '--dry-run=server', '-f', item.connectionFile, '-o=name', '--certificate-authority=""', '--insecure-skip-tls-verify=true')
                                    openshift.raw('apply', '--dry-run=server', '-f', item.staticFile, '-o=name', '--certificate-authority=""', '--insecure-skip-tls-verify=true')
                                    openshift.raw('get', 'vaultauths.secrets.hashicorp.com', '-o=name', '--certificate-authority=""', '--insecure-skip-tls-verify=true')
                                }
                            }
                        }
                    }
                }
            }
        }
        stage('Provision Vault') {
            steps {
                script {
                    withCredentials([[$class: 'VaultTokenCredentialBinding',
                        credentialsId: config.vaultcred, vaultAddr: config.vaultaddr]]) {
                        sh '''#!/bin/sh
                            set -eu
                            set +x
                            python3 scripts/migrate.py provision
                        '''
                    }
                }
            }
        }
        stage('Apply Vault Resources') {
            steps {
                script {
                    withCredentials([string(credentialsId: config.ocpcred, variable: 'OCP_TOKEN')]) {
                        openshift.withCluster(config.ocp, OCP_TOKEN) {
                            plan.each { item ->
                                openshift.withProject(item.namespace) {
                                    [item.connectionFile, item.holderFile, item.authFile, item.staticFile].each { path ->
                                        openshift.raw('apply', '-f', path, '-o=name', '--certificate-authority=""', '--insecure-skip-tls-verify=true')
                                    }
                                }
                            }
                        }
                    }
                }
            }
        }
        stage('Verify Synchronized Data') {
            steps {
                script {
                    withCredentials([string(credentialsId: config.ocpcred, variable: 'OCP_TOKEN')]) {
                        openshift.withCluster(config.ocp, OCP_TOKEN) {
                            plan.each { item ->
                                openshift.withProject(item.namespace) {
                                    timeout(time: params.SYNC_TIMEOUT_SECONDS.toInteger(), unit: 'SECONDS') {
                                        waitUntil(initialRecurrencePeriod: 5000, quiet: true) {
                                            def result = openshift.raw('get', 'secret', item.destination,
                                                '--ignore-not-found', '-o=json', '--certificate-authority=""', '--insecure-skip-tls-verify=true')
                                            writeFile(file: item.actualFile, text: result.out.trim() ?: '{}')
                                            result = null
                                            return sh(script: "python3 scripts/migrate.py verify ${item.index}",
                                                returnStatus: true) == 0
                                        }
                                    }
                                }
                            }
                        }
                    }
                }
            }
        }
        stage('Create New Deployments') {
            steps {
                script {
                    withCredentials([string(credentialsId: config.ocpcred, variable: 'OCP_TOKEN')]) {
                        openshift.withCluster(config.ocp, OCP_TOKEN) {
                            plan.each { item ->
                                openshift.withProject(item.namespace) {
                                    openshift.raw('create', '-f', item.deploymentFile, '-o=name', '--certificate-authority=""', '--insecure-skip-tls-verify=true')
                                    echo "Dibuat ${item.namespace}/${item.target}, replica=1; pemeriksaan aplikasi manual."
                                }
                            }
                        }
                    }
                }
            }
        }
    }
    post {
        always {
            dir('.migration-work') { deleteDir() }
        }
    }
}
