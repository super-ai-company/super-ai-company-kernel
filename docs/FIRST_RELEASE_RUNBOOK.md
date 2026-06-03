# First Release Runbook

第一版只看这几个动作。

## 1. 安装本机自启动服务

这会安装 daemon、API Gateway `8765`、静态 dashboard `8780` 三个 launchd 服务。

```bash
cd /Users/owner/openclaw/company-kernel
bash bin/company-services-install-launchd
curl http://127.0.0.1:8765/v1/health
curl http://127.0.0.1:8780/dashboard.html
```

卸载：

```bash
bash bin/company-services-uninstall-launchd
```

## 2. 重新生成并打开操作台

```bash
bin/company-dashboard --variant advanced
open http://127.0.0.1:8780/dashboard.html
```

## 3. 直接叫员工说话

```bash
bin/companyctl message direct --from main --to nestcar --body "只回复：NESTCAR_OK"
bin/companyctl message direct --from main --to codex --body "只回复：CODEX_OK"
```

如果员工已经配置默认用户回复桥，不需要每次手工带 `--deliver --reply-channel telegram --reply-account default --reply-to current`。

## 4. 缺参数时走 followup

```bash
bin/companyctl followup request --from nestcar --to main --question "请补充本次还车里程"
bin/companyctl followup reply --followup-id <followup-id> --by main --answer "本次还车里程是 10234 km"
```

`reply` 会把答案继续 direct 回原员工。

## 5. 做一键验收

```bash
bin/company-local-smoke --json-only
```

看结果文件：

```bash
open /Users/owner/openclaw/company-kernel/state/local-smoke/latest.json
```

通过标准：

- `ok=true`
- `attendance_counts.online=3`
- `session_missing=0`
- `worker_stalled=0`
- `direct_matrix` 里 `nestcar/chindahotpot/codex` 都是 `direct_status=ok`
