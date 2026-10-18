# ARMA4OS
面向openEuler的自适应资源管控Agent

## 核心目标
设计并实现一个运行在用户态的自适应资源管控 Agent 框架。Agent 能够感知系统中的 workload，并根据负载特征动态选择或生成资源管理策略，重点方向是借助 Linux sched_ext / scx 调整 CPU 调度策略，从而改善目标 workload 的性能。

扩展方向：除 CPU 调度外，可进一步把 eBPF 作为系统侧 hook，由 Agent 负责策略决策，扩展到网络策略、安全策略、cgroup 资源控制等操作系统资源管理场景。

**赛题要求：**
- 构建可扩展的用户态资源管控 Agent 框架，并设计标准化的 Tool / Skills 能力接口，便于快速新增 Agent 能力。
- 能够感知 workload 的运行状态和资源需求，并据此调整 CPU 调度策略。
- 集成 sched_ext / scx 调度器，形成可实际运行的调度闭环，并用实验验证对 workload 的性能优化效果。
- 支持以 eBPF 作为 hook 扩展到 network policy、security policy、resource control（如 cgroup）等方向。
- 提供完整的性能对比数据，以及可复现的实验环境、配置和测试方法。


**参考资料：**
https://mp.weixin.qq.com/s/ewC8gTMRvm2NO6ywoproAA
Towards Agentic OS: An LLM Agent Framework for Linux Schedulers
Mixture-of-Schedulers: An Adaptive Scheduling Agent as a Learned Router for Expert Policies
https://github.com/sched-ext/scx

评分细则
评分项	分值	重点
创新性	30	Agent 架构设计与调度策略创新
功能完整性	25	workload 感知、策略调整准确性与灵活性
性能提升	25	相对默认调度器的实际性能改善
代码质量	10	代码结构、注释、可维护性
演示效果	10	现场演示的完整性、流畅度与可验证性

## 框架代码

当前版本提供 **Python 在线控制面 + Rust 受限执行器**。Python 使用标准库，Rust 执行器使用 serde / serde_json / libc。先运行无需 root 的模拟闭环，再在目标内核上配置真实 scx 专家与标定数据。

```bash
# Python 3.11+；从仓库根目录执行，无需 pip 安装
python3 -m arma4os capabilities
python3 -m arma4os demo

# 使用同一 Python 控制面驱动 Rust 模拟执行器
cargo build --workspace --locked
python3 -m arma4os demo --executor ./target/debug/arma-executor \
  --executor-config configs/executor.simulated.json

# 实际运行固定输入压缩任务，记录默认调度器上的独立重复
python3 -m benchmarks run --config configs/benchmarks.json \
  --output results/baseline-001 --repeats 5 --seed 42
python3 -m benchmarks summarize results/baseline-001

# 回归验证
python3 -m unittest discover -s tests -v
cargo test --workspace --locked
```

`demo` 的观测、专家成绩和性能退化均为**合成数据**，只验证协议、决策与恢复流程，不能作为性能改善证据。压缩基准会真正运行任务；使用模拟执行器的多策略矩阵也不能用于比较真实调度策略。

在线模式：

```bash
# 无特权监控；初始策略库为空、示例目标已过期，会解释保持原因
python3 -m arma4os run --ticks 5 --monitor-only

# 输出 Tool schema，或从 stdin 接收 JSONL 请求
python3 -m arma4os tools --schemas
python3 -m arma4os tools

# 执行有前置条件与恢复步骤的 Skill
python3 -m arma4os skill mixed_workload_tuning --dry-run
```

真实执行需显式传入 `--executor` 与 `--executor-config configs/executor.real.json`。执行器只接受配置中的策略 ID 和固定 argv，核对 sysfs 的实际状态，管理自己启动的进程。真实运行前需按目标机器核实 scx 二进制路径、`kernel_ops`、权限、目标有效期和标定证据。真实执行模式和 eBPF 示例未由上述模拟测试证明可在目标 openEuler 内核加载；本仓库尚无性能收益结论。

主要目录：

| 路径 | 职责 |
| --- | --- |
| `arma4os/` | 版本化合同、采集、画像、策略库、路由、反馈、Tool / Skill、审计与 CLI |
| `crates/arma-executor/` | Rust JSONL 执行器、策略白名单、互斥、sysfs 核验、超时与恢复 |
| `benchmarks/` | 固定工作负载、解析器、串行随机矩阵、原始数据与汇总 |
| `configs/` | 执行器、目标、路由器、实验与 hook 的示例配置 |
| `hooks/` | 额外 cgroup_skb eBPF 观测 hook 源码 |
| `tests/` | Python 决策、恢复、权限范围和实验回归测试 |

详细设计见 [架构与论文方法映射](docs/architecture.md)、[运行与扩展指南](docs/framework-guide.md)、[Rust/Python 协议](docs/protocol.md)。后续实测与比赛里程碑仍按 [实施计划](docs/competition-plan.md) 推进。
