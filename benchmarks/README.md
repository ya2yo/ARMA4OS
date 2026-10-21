# benchmarks 模块结构

实现按五个模块组织。命令入口 `cli.py` 只解析参数、分发命令和展示结果；实验生命周期由编排模块负责。

| 模块 | 职责 | 主要入口 |
| --- | --- | --- |
| `orchestration/` | 配置、数据合同、随机实验矩阵、预热与独立重复、完整实验生命周期 | `engine.run_benchmark`、`config.load_config` |
| `execution/` | 单次试验、进程组与超时清理、调度策略核验、RustExecutor 适配及具体负载 | `trial.run_trial`、`executor.ExecutorClient` |
| `evidence/` | 原始记录持久化、有界日志读取、环境清单、输入来源与通用输出解析 | `store.RunStore`、`parsers.dispatch.parse_trial_evidence` |
| `reporting/` | 从原始证据重建结果、统计聚合、置信区间和报告展示 | `reconstruction.reconstruct_summary` |
| `deployment/` | SSH / PowerShell 传输、源码快照、客机部署和结果回收、环境及恢复验收 | `transport.Connection`、`vm.main`、`checks.main` |

```text
benchmarks/
  cli.py
  orchestration/
    models.py                 # 仅标准库的数据合同与校验
    config.py
    matrix.py
    engine.py
    formal/
      config.py               # 场景配置、输入和环境冻结
      scenario.py             # kernel / schbench / mixed 子任务协调
      parsing.py              # formal 专属原始证据解释
      commands.py
      __main__.py
  execution/
    trial.py
    processes.py
    policy.py
    executor.py
    workloads/
      compression/workload.py
      ffmpeg/transcode.py
      kernel_build/build.py
  evidence/
    store.py
    files.py
    manifest.py
    provenance.py
    parsers/
      base.py
      dispatch.py
      elapsed.py
      schbench.py
      ffmpeg.py
  reporting/
    reconstruction.py
    statistics.py
    formatting.py
  deployment/
    transport.py
    snapshot.py
    vm.py
    checks.py
    _support.py
```

## 调用关系

`orchestration.models` 是共享合同，只依赖标准库。其他模块可以引用合同，但不能因为需要一个类型而导入实验引擎。

```text
CLI / deployment
       │
       ▼
orchestration.engine ──► execution.trial ──► 负载子进程
       │                      │
       ├──────────────────────┴──► evidence
       └─────────────────────────► reporting ──► evidence
```

- `execution` 负责执行与调度状态，`evidence` 负责保留和解释事实。正式调度动作通过 `ExecutorClient` 复用 `arma4os.executor.RustExecutor`，不维护第二套协议传输。
- `reporting` 从原始目录重建结果，不导入负载执行、进程管理或实验引擎。`metrics.jsonl` 是派生缓存，不能替代缺失的 `record.json`，也不能覆盖原始失败状态。
- 在线试验和离线重建统一调用 `parse_trial_evidence`。通用解析器位于 `evidence/parsers/`；formal 的专属解析由分发入口延迟加载 `orchestration.formal.parsing`，不会加载 formal 执行协调或实验引擎。
- formal 套件的实现集中在 `orchestration/formal/`。其执行协调复用 `execution/workloads/kernel_build/`；其解析只依赖证据工具和解析合同，不导入负载实现。
- 准备脚本直接使用 `deployment.transport`，无需导入 smoke 验收流程。
- 子包初始化保持轻量，导入解析器不会连带初始化执行和部署模块。类型和函数直接从所属模块导入，根包不再提供历史符号转发。

## 命令入口与数据格式

代码直接使用上述五个模块中的路径。原有根目录转发文件和旧 `parsers/`、`workloads/` 目录已删除。负载配置中的模块名、部署命令和测试也使用新路径。

命令入口：

```bash
python3 -m benchmarks run --config configs/benchmarks.json --output results/new-run
python3 -m benchmarks formal --config configs/benchmarks.formal.local.json --output results/new-formal
python3 -m benchmarks summarize results/new-run
python3 -m benchmarks.execution.workloads.compression --size-kb 64 --iterations 1
python3 -m benchmarks.deployment.checks --help
python3 -m benchmarks.deployment.vm --help
```

FFmpeg 负载入口为 `python3 -m benchmarks.execution.workloads.ffmpeg`；formal 场景子进程使用 `python3 -m benchmarks.orchestration.formal`。仓库外的旧配置如果包含 `benchmarks.workloads.*`，需要同步修改模块名。示例配置统一放在 `configs/`，包括 `benchmarks.schbench.example.json`。

配置和实验结果继续使用 `schema_version=1`。结果目录仍包含 `manifest.json`、`actions.jsonl`、`metrics.jsonl`、`summary.json` 和 `trials/<trial_id>/`。原始 `record.json` 保存执行状态；派生指标保存于 `metrics.jsonl`，汇总始终可以从原始输出重建。负载退出码与策略核验结果分开保留：即使负载退出码为 0，策略核验失败的试验仍计为失败。

若执行器在环境清单生成前启动失败，编排模块会执行清理，并为计划试验保存取消记录；此时尚无完整的 `manifest.json` 和初始 `summary.json`，不能据此还原完整实验环境。

## 本地验证

从仓库根目录执行 `make test`、`make check`，以及 `make build` 后的 Rust 模拟执行器联调。模块迁移和本地测试不证明真实内核调度器可加载，也不构成性能提升证据。
