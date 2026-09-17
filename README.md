# Server dependencies

Ubuntu 24.04 (amd64). Run the matching command on each server:

```sh
sudo bash install-dependencies.sh --role app
sudo bash install-dependencies.sh --role db
```

- `app`: Docker Engine, Compose, Buildx, curl and CA certificates.
- `db`: PostgreSQL 16.

Requires access to APT repositories. Uses configured package versions, adding [Docker's official repository](https://docs.docker.com/engine/install/ubuntu/) when needed. Installed target packages are not upgraded. Dependencies only; application deployment and database access configuration are separate.
