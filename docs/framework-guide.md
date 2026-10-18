# 运行、标定与扩展

## 开发验证

环境：Linux、Python >= 3.11、Rust stable。Python 核心无第三方运行依赖。Rust 依赖锁定在 Cargo.lock；在仓库根目录运行 README 的构建和测试命令。

`python3 -m arma4os demo` 验证纯 Python 模拟闭环。构建 Rust 后传入 `--executor ./target/debug/arma-executor --executor-config configs/executor.simulated.json`，验证跨进程协议。每次指定的 `--output` 应为新的目录。模拟器不会修改真实内核调度器。

Agent run 目录包含 manifest.json、metrics.jsonl、decisions.jsonl、actions.jsonl、tools.jsonl、feedback.jsonl、summary.json、CLI 原始 stdout 与控制面错误 stderr。执行器诊断信息写父进程 stderr，可由部署者另行重定向保存。CLI 状态与审计来自相同 Decision / ActionResult，不生成演示成绩。实验 runner 则为每次实际 workload 保存原始 stdout/stderr。

## 应用指标与明确目标

将 `configs/objective.json` 复制到本地配置，设置应用主指标、单位、方向、约束、评价窗口和未来的 `expires_at_ms`。仓库示例有效期为 0，防止一份陈旧示例自动生效。`python3 -m arma4os capabilities` 输出机器 environment_id。

受信任的应用采集器以临时文件 + 原子替换的方式写入 KPI：

```json
{
  "scope": "global",
  "metrics": {
    "app.p99": {"value": 4.2, "unit": "ms", "source": "service-exporter", "timestamp_ms": 0, "quality": "valid"},
    "app.throughput": {"value": 1200, "unit": "ops/s", "source": "service-exporter", "timestamp_ms": 0, "quality": "valid"},
    "app.background_progress": {"value": 3, "unit": "items/s", "source": "batch-exporter", "timestamp_ms": 0, "quality": "valid"}
  }
}
```

以上数值只说明格式；timestamp_ms 要写测量源的实际 Unix 毫秒时间。文件中的旧时间不能因被重复读取而变新。只有 `app.` 前缀被接受，不允许应用覆盖系统 CPU 特征。scope 必须与目标一致。

```bash
python3 -m arma4os run --registry configs/registry.json \
  --objective /path/to/objective.json --application-metrics /path/to/kpi.json \
  --selector-config configs/selector.json --monitor-only --ticks 10
```

策略库为空、KPI 缺失、目标过期、未知负载、无可信收益和冷却均会解释保持。首个系统样本没有 CPU 差分，正常保持。CPU 压力是系统滚动特征，不是应用 KPI。

## 真实 scx 接入

1. 在目标内核核实 sched_ext、BTF、scx 版本与候选参数；记录 openEuler / WSL 内核、CPU 拓扑和资源分配。
2. 构建 `cargo build --workspace --release --locked`，将 real 配置复制到仅管理员可写的位置。逐个核实实际安装的二进制绝对路径、完整固定 argv 和该版本 sysfs root/ops 的名字。所有执行器共享同一规范 state_dir。
3. 先让受信任管理员在隔离实验机上启动 `arma-executor --config /path/to/real.json`，用协议 status / dry_run / apply / rollback 验证，观察真实 sysfs 和退出恢复。外部 scx 管理器应先按其官方流程退出。执行器不会替你调用 sudo，也不会接管外部进程。
4. 用真实执行器跑串行静态对比。默认配置仅含压缩烟雾测试；将已锁定的 schbench、混部命令与至少两个真实可加载专家写入新配置。固定参数、缓存规则与预热后，按轮次随机顺序做至少 5 次独立重复。
5. 保存所有失败、原始输出与调度器状态，复核标定后才在在线模式启用动作。不要拿模拟矩阵或训练集成绩当真实泛化收益。

```bash
python3 -m benchmarks run --config /path/to/static-matrix.json \
  --output results/static-001 --seed 42 --repeats 5 \
  --executor ./target/release/arma-executor --executor-config /path/to/real.json
```

在适当权限下运行上述命令。当前 stdio 子进程模型没有独立授权服务，尚不能声称 Python 和所有插件已经获得进程级特权隔离。

## 策略库草案

先按独立探索数据冻结上下文范围，在 `configs/calibration.example.json` 的本地副本中填写 manifest 的 environment_id、workload、真实单位映射与实测切换成本。示例中的成本数字是格式示范，不能当标定结果。

```bash
python3 -m arma4os.calibration results/static-001 \
  --context /path/to/calibration-context.json --output /path/to/registry-draft.json
```

导入拒绝失败组、不足 5 个独立重复、没有统计区间或单位不符的组。使用模拟执行器生成的草案仍为 synthetic，无法驱动真实执行器。草案需要人工复核：CPU contention 范围是否被实际覆盖、独立运行是否同一环境、目标 KPI 是否一致、版本是否锁定，以及收益是否超过噪声。注册表不会把在线窗口写成独立样本，也不会自动学习一次切换前后的因果收益。

Registry 中每条 evidence 包含 policy_id、environment_id、profile、samples、source、valid_until_ms、features 范围、metrics 的 mean / uncertainty / unit、switch_cost_s、synthetic。switch_cost_s 表示涉及该专家的完整转换耗时保守上界（包含旧策略卸载和新策略加载）；不是单独的加载耗时，也不与另一端重复相加。相同策略在同一上下文匹配多条记录会保持，需离线细分范围。任一合成条目会使整个 Registry 保留 synthetic 标记，核心 Agent 拒绝用它驱动真实后端。

## Tool、Skill 与低频分析

`python3 -m arma4os tools --schemas` 查看工具清单。每行请求形如：

通用请求与目标 schema 也在 `schemas/` 中，可供外部调用方生成校验器。

```json
{"schema_version":1,"request_id":"example-1","tool":"scheduler.status","scope":"global","timeout_ms":5000,"params":{}}
```

响应包含相同 request_id、ok、result 与结构化 error。调度与 hook 状态改变工具支持 dry_run；experiment.run 为固定实验任务，不提供虚构的 dry-run 成绩。apply 只接受本进程 scheduler.plan 返回的 Decision ID；计划短期有效并绑定当前策略。相同 request_id 与相同请求体重复返回已有结果；同 ID 不同内容拒绝。幂等缓存为进程内语义，跨进程重试应重新观测和规划。timeout_ms 传递给执行器和实验 runner；同步的轻量采集器由本地调用完成，插件应自行实施 I/O 超时。

新增 Tool：实现有明确 required / allowed 参数与 mutating 属性的 handler，注册到同一个 ToolRegistry。新增 Skill：声明所需工具与输入目标，编排预检查、计划、动作、核验及恢复，不把提示词视为可执行验证。现有 latency / batch / mixed 三类 Skill 共享同一底层路由规则。一次 Skill 只执行一个评估周期；连续稳定窗口和后续试用监测由同一 Agent 生命周期持续调用 tick 完成。

当前提供 stdio JSONL 接入；MCP 是可扩展的协议适配点，未声称此 CLI 已实现完整 MCP。只读请求使用有界缓存，不占用动作幂等配额；动作请求含失败结果保留去重语义，结果不确定时应重新观测并用新 ID 规划。相邻半秒内的 profile、snapshot、plan 共享同一次系统采样，防止反复读取 CPU 计数制造空窗口或重复投票。Advisor 接收可注入 provider 的结构化提议，验证策略 ID 和证据引用格式，提议进入离线实验，不自动部署。若 provider 接网络，必须自己配置可取消 I/O 和超时；在线循环无需运行 Advisor。

## 额外 hook

`demo.counter` 是明确标记的模拟生命周期示例。`CpuWeightHook` 是真实 cgroup v2 控制插件，授权目录在受信任配置中固定，修改后核验、退出恢复、遇外部修改拒绝覆盖。

`hooks/cgroup_counter.bpf.c` 是真实 eBPF 程序源码：对授权 cgroup 的 ingress 计数，始终放行。安装目标环境的 clang、libbpf 头文件、bpftool 后，在目标机编译：

```bash
mkdir -p build
clang -O2 -g -target bpf -I/usr/include/$(uname -m)-linux-gnu \
  -c hooks/cgroup_counter.bpf.c -o build/cgroup_counter.bpf.o
```

复制并调整 `configs/hooks.example.json`，只启用准备运行的插件。使用 `python3 -m arma4os tools --hooks-config /path/to/hooks.json` 后调用 hook.attach / hook.stats / hook.detach。hook_id 分别为 `cgroup.cpu_weight`、`ebpf.ingress_counter`，scope 为配置授权的 service。CPU weight 的 config 是 `{"weight":200}`，计数 hook 不接受动态参数。

eBPF 插件用 bpftool 实际加载与 attach，核验内核返回的 program id；detach 只针对自身 program，删除自身 pin。stats 返回原始 per-CPU map，使用者应按 CPU 聚合包数和字节。它不实现网络过滤，也不证明 security policy 已完成。本次开发没有真实 attach 验证；各内核/bpftool 版本须在目标机联调。网络 hook 运行和 CPU 主实验应分开，避免混入干扰。

## 后续实测验收

框架代码已为规则闭环、真实执行器和实验记录提供入口；仍需在目标机验证真实加载/退出/失败恢复、完成三场景静态矩阵、测实际切换开销、填写校准证据、执行动态与消融对照、跑长时间稳定性，并完成 openEuler 复现。当前没有已证明的收益、已训练的 XGBoost router、自生成调度器或隔离 VM 验证器。
