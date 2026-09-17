FROM ubuntu:24.04

# This image is solely a disposable integration-test host, never the deployment target.
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        postgresql-16 postgresql-client-16 postgresql-contrib \
        python3 curl wget ca-certificates shellcheck \
    && rm -rf /var/lib/apt/lists/*

ENV DEPLOY_DB_TEST_ISOLATED=1
ENTRYPOINT ["bash", "/src/tests/run-deploy-db-integration.sh"]
