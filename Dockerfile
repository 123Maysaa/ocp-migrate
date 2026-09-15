FROM jenkins/jenkins:2.579-jdk21

USER root

COPY oc /usr/bin/oc
COPY kubectl /usr/bin/kubectl
COPY vault /usr/bin/vault
COPY yq /usr/bin/yq

RUN chmod +x /usr/bin/oc /usr/bin/kubectl /usr/bin/vault /usr/bin/yq

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        lsb-release ca-certificates curl jq python3 && \
    install -m 0755 -d /etc/apt/keyrings && \
    curl -fsSL https://download.docker.com/linux/debian/gpg \
        -o /etc/apt/keyrings/docker.asc && \
    chmod a+r /etc/apt/keyrings/docker.asc && \
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/debian $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
        > /etc/apt/sources.list.d/docker.list && \
    apt-get update && \
    apt-get install -y --no-install-recommends docker-ce-cli && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

USER jenkins

# Verify the migration helper's minimum Python version as the runtime user.
RUN python3 -c 'import sys; assert sys.version_info >= (3, 8), "Python 3.8+ required"; print(sys.version)'

# readYaml/readJSON/writeJSON used by pipeline.groovy.
RUN jenkins-plugin-cli --plugins "blueocean docker-workflow pipeline-utility-steps"
