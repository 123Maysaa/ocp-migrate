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

                    withCredentials([string(credentialsId: config.ocpcred, variable: 'OCP_TOKEN')]) {
                        // Menggunakan string tunggal (') untuk menghindari ekspansi Groovy yang memicu error EOF
                        sh 'oc login ' + config.ocp + ' --token="$OCP_TOKEN" --insecure-skip-tls-verify=true >/dev/null'
                        sh 'python3 scripts/generate_input.py "' + params.TARGET_NAMESPACE + '" "' + params.SELECTED_WORKLOADS + '"'
                    }

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
                                    if (existing) { 
                                        echo "Deployment tujuan ${item.namespace}/${item.target} sudah ada, melakukan uji dry-run update..." 
                                    }
                                    openshift.raw('apply', '--dry-run=server', '-f', item.deploymentFile, '-o=name', '--certificate-authority=""', '--insecure-skip-tls-verify=true')
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
                                    openshift.raw('apply', '-f', item.deploymentFile, '-o=name', '--certificate-authority=""', '--insecure-skip-tls-verify=true')
                                    echo "Di-apply/update ${item.namespace}/${item.target}, replica=1; pemeriksaan aplikasi manual."
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
