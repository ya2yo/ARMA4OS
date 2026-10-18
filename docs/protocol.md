# Rust / Python 执行协议 v1

Python 控制面启动一个长期运行的 `arma-executor --config configs/executor.json` 子进程。
stdin / stdout 为每行一个 JSON；stdout 只写响应，日志写 stderr。进程关闭 stdin 时清理自己拥有的调度器。
真实模式持有配置 state_dir 下的进程锁，拒绝第二个执行器；不允许同时运行其他 scx 管理器。

请求：`{"schema_version":1,"request_id":"uuid","operation":"status","timeout_ms":5000,"params":{}}`。
响应：`{"schema_version":1,"request_id":"uuid","ok":true,"result":{...},"error":null}`；失败时 result 为 null，error 为 `{"code":"...","message":"..."}`。
支持 `capabilities`, `list`, `status`, `apply`, `rollback`, `shutdown`。

`status` result：`{"policy_id":"default","kernel_state":"disabled","kernel_ops":null,"managed_pid":null,"healthy":true,"backend":"simulated"}`。
真实状态无法映射或外部占用时 policy_id 为 `external`，healthy=false。disabled 时默认策略。

`apply` params：`{"policy_id":"scx_bpfland","expected_policy":"default","expires_at_unix_ms":123456789,"dry_run":false}`。
必须检查有效期、期望状态、配置白名单，不能执行调用者传入的命令或参数。命令行参数只能由受信任的本地配置固定。
`rollback` params 采用相同期望状态和有效期字段；恢复最近一次成功切换前的策略。
可显式传 `policy_id` 恢复试用前的已知策略，供反馈模块避免依赖失效的内部记忆；重复回滚不得重新激活被回滚策略。
动作 result：`{"planned_policy":"...","previous_policy":"...","actual_policy":"...","status":"applied|held|dry_run|rolled_back|failed","verified":true,"started_at_unix_ms":0,"finished_at_unix_ms":0,"managed_pid":null,"error":null,"recovery":null,"backend":"simulated"}`。
恢复字段记录真实恢复结果；新策略加载失败即使恢复成功也不能返回 applied。

`timeout_ms`（1..120000）是普通动作的总预算。加载失败后的紧急恢复有独立的有界预算（stop_timeout_ms + start_timeout_ms）；客户端会为恢复保留这段响应宽限，不能在普通预算结束后立刻杀死尚在恢复的执行器。有效期约束动作开始，恢复不能因为原计划到期而被跳过。shutdown 结果包含 shutdown 标志；即使清理失败也应退出循环并报告真实清理错误，不能只打印成功。

执行器配置：

```json
{"schema_version":1,"backend":"simulated","state_dir":"results/executor-state","sysfs_root":"/sys/kernel/sched_ext","policies":[{"id":"default","command":[],"kernel_ops":null},{"id":"scx_bpfland","command":["/usr/bin/scx_bpfland"],"kernel_ops":"bpfland"},{"id":"scx_rusty","command":["/usr/bin/scx_rusty"],"kernel_ops":"rusty"}]}
```

真实执行器读取 sysfs state 和 root/ops（兼容可用的 root/ops 路径），不通过进程是否存在推断加载成功。程序使用直接 argv 执行，不经 shell、不调用 sudo。权限由部署者授予执行器。
