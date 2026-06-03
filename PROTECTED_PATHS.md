# 不可修改区

普通员工/Agent 禁止直接修改以下路径：

```text
/company_kernel/**
/bin/**
/company.sqlite
/config/policy.json
/config/hooks.json
/config/company_communications.json
/config/daemon.json
/state/**
/employees/*/profile.json
/employees/*/permissions.json
/employees/*/capabilities.json
/PROTECTED_PATHS.md
/config/protected_paths.json
```

如需修改内核、schema、审批、锁、状态或员工权限，必须生成 RFC：

```text
/rfcs/YYYYMMDD-change-xxx.md
```
