# Skill Package Standard

第一期只定义最小可运行标准：Skill 必须能被 Company Kernel 当作员工任务调用，并把最终产物登记为 Artifact，再 promoted to Evidence。

## Manifest

Skill package 使用 `skill.json`：

- `id/name/version`：技能身份。
- `input_schema/output_schema`：未来 marketplace 和任务表单使用。
- `runtime.type`：第一期只支持 `local-script`。
- `runtime.command`：在授权 task workspace 内执行。
- `permissions.workspace`：第一期固定为 `task`，不允许私扫全局目录。
- `pricing`：只记录报价单位，不参与第一期计费。
- `acceptance.final_artifact`：必须位于当前 task workspace 内，worker 会登记为 Artifact 并 promoted to Evidence。

## Run

```bash
bin/companyctl runtime register --runtime skill --command company-skill-package-worker --notes "Skill Package runtime"
bin/companyctl employee create --id ecommerce-copy-skill --name "Ecommerce Copy Skill" --role skill-worker --runtime skill --workspace "$PWD/employees/ecommerce-copy-skill"
bin/companyctl task submit --from main --to ecommerce-copy-skill --title "Run ecommerce copy demo" --description "Generate listing summary"
bin/company-skill-package-worker --agent ecommerce-copy-skill --package skill-packages/ecommerce-copy-demo/skill.json
```

成功标准：任务 `completed`，`artifacts` 有 final artifact，`evidence` 有 promoted final evidence，trace 可查。
