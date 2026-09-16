// Script Path Jenkins: pipeline.groovy. YAML/JSON steps: Pipeline Utility Steps.
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
        string(name: 'MIGRATE_FILE', defaultValue: 'migrate.yaml', description: 'Daftar sumber migrasi')
        string(name: 'SYNC_TIMEOUT_SECONDS', defaultValue: '180', description: 'Batas tunggu sinkronisasi VSO')
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
                    writeJSON(file: '.migration-work/config.json', json: config)
                    writeJSON(file: '.migration-work/input.json', json: readYaml(file: params.MIGRATE_FILE))
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
                    openshift.withCluster(config.ocp, config.ocpcred) {
                        openshift.verbose(false)
                        requests.eachWithIndex { item, index ->
                            openshift.withProject(item.namespace) {
                                // Never echo the result: source manifests can contain secrets.
                                def result = openshift.raw('get', item.kind, item.name, '-o=json')
                                writeFile(file: ".migration-work/source-${index}.json", text: result.out)
                                result = null
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
                    openshift.withCluster(config.ocp, config.ocpcred) {
                        plan.each { item ->
                            openshift.withProject(item.namespace) {
                                // Create-only clones: an existing name must never be overwritten.
                                def existing = openshift.raw('get', 'deployment', item.target,
                                    '--ignore-not-found', '-o=name').out.trim()
                                if (existing) { error("Deployment tujuan sudah ada: ${item.namespace}/${item.target}") }
                                openshift.raw('create', '--dry-run=server', '-f', item.deploymentFile, '-o=name')
                                openshift.raw('apply', '--dry-run=server', '-f', item.connectionFile, '-o=name')
                                openshift.raw('apply', '--dry-run=server', '-f', item.staticFile, '-o=name')
                                openshift.raw('get', 'vaultauths.secrets.hashicorp.com', '-o=name')
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
                    openshift.withCluster(config.ocp, config.ocpcred) {
                        plan.each { item ->
                            openshift.withProject(item.namespace) {
                                // File arguments avoid embedding SecretID values in Pipeline step arguments.
                                [item.connectionFile, item.holderFile, item.authFile, item.staticFile].each { path ->
                                    openshift.raw('apply', '-f', path, '-o=name')
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
                    openshift.withCluster(config.ocp, config.ocpcred) {
                        plan.each { item ->
                            openshift.withProject(item.namespace) {
                                timeout(time: params.SYNC_TIMEOUT_SECONDS.toInteger(), unit: 'SECONDS') {
                                    waitUntil(initialRecurrencePeriod: 5000, quiet: true) {
                                        def result = openshift.raw('get', 'secret', item.destination,
                                            '--ignore-not-found', '-o=json')
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
        stage('Create New Deployments') {
            steps {
                script {
                    openshift.withCluster(config.ocp, config.ocpcred) {
                        plan.each { item ->
                            openshift.withProject(item.namespace) {
                                openshift.raw('create', '-f', item.deploymentFile, '-o=name')
                                echo "Dibuat ${item.namespace}/${item.target}, replica=1; pemeriksaan aplikasi manual."
                            }
                        }
                    }
                }
            }
        }
    }
    post {
        always {
            // No stash/archive of source values, payloads, or generated credentials.
            dir('.migration-work') { deleteDir() }
        }
    }
}
