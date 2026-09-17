# 从 GitHub 获取脚本，另行搬运数据库备份

适用于 Ubuntu 24.04 amd64。服务器需要能访问 GitHub 和 Ubuntu APT 软件源。
数据库 dump 由你另行复制到服务器，无需上传到 GitHub，也无需和脚本放在同一目录。

## 1. 获取仓库代码

第一次使用且没有 curl/CA 证书时，安装下载工具：

```bash
sudo apt-get update
sudo apt-get install -y curl ca-certificates
```

选择含 `deploy-db.sh` 的分支或已验证的 commit。当前部署代码位于
`codex/deploy-db` 分支；`main` 在合并前只有原安装脚本。下载命令：

```bash
DEPLOY_REF=codex/deploy-db
curl --fail --show-error --location --proto '=https' --proto-redir '=https' \
  "https://api.github.com/repos/jhchong0405/perodua-server-dependencies/tarball/${DEPLOY_REF}" \
  --output perodua-server-dependencies.tar.gz
mkdir perodua-server-dependencies
tar -xzf perodua-server-dependencies.tar.gz \
  -C perodua-server-dependencies --strip-components=1
cd perodua-server-dependencies
```

需要精确复现已验证版本时，将 `DEPLOY_REF` 改为验证报告中的完整 commit SHA。
这个仓库是公开的，下载脚本不需要 GitHub 密码。

## 2. 安装数据库依赖

```bash
sudo bash install-dependencies.sh --role db
```

## 3. 指向自己搬运的备份

假设你把 PostgreSQL `pg_dump -Fc` 文件放在 `/srv/imports/database.dump`：

```bash
cp deploy.conf.example deploy.conf
chmod 600 deploy.conf
nano deploy.conf
```

至少调整以下字段；尖括号内容须换成自己的实际值：

```ini
DB_NAME=perodua
DB_USER=odoo
BACKUP_FILE=/srv/imports/database.dump
BACKUP_URL=
BACKUP_SHA256=<从源端备份取得的64位SHA256>
DB_LC_COLLATE=<源数据库datcollate>
DB_LC_CTYPE=<源数据库datctype>
DB_LISTEN_IP=127.0.0.1
APP_CIDR=
```

`BACKUP_FILE` 可以是任意本地绝对路径。相对路径按 `deploy.conf` 所在目录解析。
脚本会验证文件存在、SHA-256 和备份格式；密码在运行时由终端输入。
已存在的同名数据库不会被覆盖。

源库的校验值和 locale 应随备份交付。搬运后可运行
`sha256sum /srv/imports/database.dump`，并与源端记录比较。
如果原 locale 是 `en_US.utf8`，先生成该 locale，再在配置中原样填写两项：

```bash
sudo localedef -i en_US -f UTF-8 en_US.UTF-8
```

## 4. 检查并恢复

```bash
bash deploy-db.sh --config deploy.conf --check-config
sudo bash deploy-db.sh --config deploy.conf
```

看到 `SUCCESS` 后，数据库已在本机恢复并验证。
App Server 就绪时，把 `DB_LISTEN_IP` 改为数据库服务器的本机内网 IPv4，
`APP_CIDR` 改为 App Server 的来源范围，重跑脚本，再配置相应防火墙规则。

Odoo 附件 filestore 另行放到 App Server；此流程只处理 PostgreSQL 数据库。
实际服务器的开机自启和应用网络连接需要在目标环境验收。

## 模拟验收的范围

模拟从原始 Ubuntu 24.04 开始，在容器内通过 GitHub 下载指定 commit 的仓库，
运行该版本的安装脚本。随后把真实 dump 单独复制到仓库之外的位置，通过绝对路径
配置完成恢复。测试不把本机脚本挂载进容器，也不使用预装 PostgreSQL 的测试镜像。
本机独立审计只用于恢复后的数据核对，不参与安装或部署。
