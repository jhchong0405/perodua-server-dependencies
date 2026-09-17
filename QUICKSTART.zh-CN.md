# 数据库端部署：快速开始

适用环境：Ubuntu 24.04，已经安装 PostgreSQL 16，默认集群 `main`、端口 `5432`。
此脚本启动和配置 PostgreSQL，不启动 Odoo，不处理 filestore 附件。

## 1. 准备工具和备份

如果数据库软件尚未安装：

```bash
sudo bash install-dependencies.sh --role db
```

DB 安装器已经包含客户端、Python 3 标准库、curl、CA 证书和 locales，
不需要 Python 虚拟环境或 pip 依赖。

如果脚本只能从 GitHub 获取、备份由你另外搬运，请按 `GITHUB-DEPLOY.zh-CN.md`
的步骤操作；备份可以位于仓库目录之外。

备份必须是 PostgreSQL `pg_dump -Fc` 生成的完整 `.dump` 文件。
本版不接受 Odoo ZIP、普通 SQL、Docker 数据卷或加密压缩包。
从备份制作方取得 SHA-256 校验值、原库的 `datcollate` / `datctype` 信息。
附件和对应版本的应用源码另外交给 App Server。

## 2. 填写配置

```bash
cp deploy.conf.example deploy.conf
chmod 600 deploy.conf
nano deploy.conf
```

至少检查：

- `DB_NAME`：准备新建的数据库名。
- `DB_USER`：应用连接数据库的账号。
- `BACKUP_FILE` 或 `BACKUP_URL`：二选一，另一个留空。
- `BACKUP_SHA256`：备份文件的 64 位 SHA-256。
- `DOWNLOAD_USER`：云端下载账号，没有认证则留空。下载 URL 必须是 HTTPS 直链。
- `DB_LC_COLLATE` / `DB_LC_CTYPE`：与原库一致；示例默认 `C` / `C.UTF-8`。
- `DB_LISTEN_IP`：数据库服务器本机的内网 IPv4。
- `APP_CIDR`：允许连接的 App Server 地址，例如 `10.0.0.10/32`。
- `MIN_FREE_MB`：按未压缩数据库、索引、日志和备份所需空间设置。

如果 App Server 还没准备好，保留 `DB_LISTEN_IP=127.0.0.1` 和空的
`APP_CIDR`，可以先恢复数据库，之后修改这两个配置并重新运行。
配置里不要写密码，也不要写 shell 命令。

## 3. 检查并执行

```bash
bash deploy-db.sh --config deploy.conf --check-config
sudo bash deploy-db.sh --config deploy.conf
```

按提示输入应用数据库密码并确认；使用云端认证下载时，再输入云端密码。
应用密码支持 12–1024 个可打印 ASCII 字符，包括空格和符号。
已有的数据库账号不会被重设密码，需要输入它当前的密码。

脚本会启动服务、校验备份、恢复到临时数据库、验证账号和表，最后才改成目标库名。
同一份备份已经成功部署时，重跑只验证并更新连接配置，保留之后新增的业务数据。
存在其他同名数据库时会停止，不覆盖。失败的临时库保留供检查，不自动删除。

## 4. 确认结果

看到 `SUCCESS` 后，记录脚本输出的数据库地址、端口、数据库名和用户名。
日志和不含密码的连接信息位于：

```text
/var/lib/perodua-db-deploy/16-main/数据库名/
```

脚本配置 PostgreSQL 的访问规则，但不修改主机防火墙或云安全组。
通过原有防火墙管理方式，允许 App Server 访问数据库端口，再从 App Server
测试连接。`SUCCESS` 表示数据库本机检查通过，不代表外部网络或 Odoo 已验收。

如果失败，先查看对应日志再重跑。不要用删除现有数据库的方式强行继续。
完整限制、备份制作方法和故障恢复说明见 `README.md`。
