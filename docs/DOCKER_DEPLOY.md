# Company Kernel — Docker 私有部署

一键起内核 + 网关 + 控制台 + daemon。核心仅依赖 Python 标准库(SQLite/http.server),镜像精简、可离线运行。

## 快速开始

```bash
docker compose up -d --build
# 控制台 / API:
open http://localhost:8765/
```

数据持久化在命名卷 `company-kernel-data`(容器内 `/data/company.sqlite`)。

## 运行模式(entrypoint 参数)

- `all`(默认):daemon 后台 + 网关前台
- `gateway`:只起 API 网关 + 控制台
- `daemon`:只起常驻 daemon

```bash
docker run --rm -p 8765:8765 -v ck-data:/data company-kernel:latest gateway
```

## 鉴权(对外暴露时强烈建议)

设 `COMPANY_KERNEL_API_TOKEN`,所有 `/v1/*` 接口要求 `Authorization: Bearer <token>`;控制台首页 `/` 不需要 token。

## 私有部署 License(付费 SKU)

自托管/开发默认**不校验**(default-allow)。付费私有部署镜像打开强制校验:

```bash
COMPANY_KERNEL_LICENSE_ENFORCE=1
COMPANY_KERNEL_LICENSE_KEY=CK1.<payload>.<sig>
COMPANY_KERNEL_LICENSE_SECRET=<厂商签发密钥>
```

License 离线校验(HMAC-SHA256,不回连),payload 至少含 `org` 与可选 `exp`(过期日 `YYYY-MM-DD`)。厂商签发:

```python
from company_kernel import license
print(license.issue_license({"org": "Acme", "exp": "2027-12-31"}, "<厂商签发密钥>"))
```

校验失败时容器以退出码 78 拒绝启动,日志打印失败原因(密钥缺失/签名不符/已过期)。

## 升级与备份

- 升级:`docker compose pull && docker compose up -d`(数据卷保留)。
- 备份:容器内 `python3 -m company_kernel.companyctl`,或 `bin/company-backup`;数据卷 `/data` 可直接快照。
