<p align="center">
  <img src="https://sce.scut.edu.cn/_upload/site/01/46/326/logo.jpg" alt="Header Logo" width="100%" />
</p>

<div align="center">

# ARMA4OS

### 面向 openEuler 的自适应资源管控 Agent
*(Adaptive Resource Management Agent for Operating Systems)*

[![Python](https://img.shields.io/badge/Python-3.11+-3776AB?style=flat-square&logo=python&logoColor=white)](https://www.python.org/)
[![Rust](https://img.shields.io/badge/Rust-2021-dea584?style=flat-square&logo=rust&logoColor=white)](https://www.rust-lang.org/)
[![sched_ext](https://img.shields.io/badge/sched__ext-Linux%20scx-E95420?style=flat-square&logo=linux&logoColor=white)](https://github.com/sched-ext/scx)
[![eBPF](https://img.shields.io/badge/eBPF-Tracing%20%26%20Hooks-FFDD00?style=flat-square&logo=ebpf&logoColor=black)](https://ebpf.io/)
[![openEuler](https://img.shields.io/badge/openEuler-24.03%20LTS-002FA7?style=flat-square&logo=openeuler&logoColor=white)](https://www.openeuler.org/)

<p align="center">
  <a href="#-核心目标">核心目标</a> •
  <a href="#-赛题要求与评分细则">赛题要求</a> •
  <a href="#-框架架构与快速开始">快速开始</a> •
  <a href="#-主要目录职责">项目结构</a> •
  <a href="#-相关文档">技术文档</a>
</p>

</div>

---

## 🎯 核心目标

设计并实现一个运行在用户态的自适应资源管控 Agent 框架。Agent 能够实时感知系统中的 workload，并根据负载特征动态选择或生成资源管理策略。重点方向是借助 Linux `sched_ext` / `scx` 调整 CPU 调度策略，从而显著改善目标 workload 的性能表现。

- **核心场景**：基于 `sched_ext` (scx) 的用户态 CPU 调度自适应切换与优化闭环。
- **扩展方向**：以 eBPF 作为系统侧 hook，由 Agent 负责统一策略决策，延伸覆盖网络策略 (Network Policy)、安全策略 (Security Policy) 及 cgroup 细粒度资源控制等操作系统资源管理场景。

---

## 📋 赛题要求与评分细则

### 赛题要求

- **架构可扩展**：构建可扩展的用户态资源管控 Agent 框架，设计标准化的 Tool / Skills 能力接口，便于快速新增 Agent 管控能力。
- **负载感知**：能够实时感知 workload 的运行状态和资源需求，并据此精准调整 CPU 调度策略。
- **闭环运行**：集成 `sched_ext` / `scx` 调度器，形成可实际运行的调度闭环，并用实验验证对 workload 的性能优化效果。
- **Hook 扩展**：支持以 eBPF 作为 hook，扩展到 network policy、security policy、resource control（如 cgroup）等方向。
- **可复现验证**：提供完整的性能对比数据，以及可复现的实验环境、配置清单和基准测试方法。

### 评分细则

| 评分项 | 分值 | 评价重点 |
| :--- | :---: | :--- |
| **创新性** | **30** | Agent 架构设计与调度策略创新 |
| **功能完整性** | **25** | Workload 感知、策略调整准确性与灵活性 |
| **性能提升** | **25** | 相对默认调度器的实际性能改善 |
| **代码质量** | **10** | 代码结构、注释规范、工程质量与可维护性 |
| **演示效果** | **10** | 现场演示的完整性、流畅度与可验证性 |

### 参考资料

- 智能调度前沿：[Towards Agentic OS: An LLM Agent Framework for Linux Schedulers](https://mp.weixin.qq.com/s/ewC8gTMRvm2NO6ywoproAA)
- 专家路由模型：[Mixture-of-Schedulers: An Adaptive Scheduling Agent as a Learned Router for Expert Policies](https://github.com/sched-ext/scx)
- 上游调度仓库：[sched-ext/scx (Linux sched_ext BPF Schedulers)](https://github.com/sched-ext/scx)

---

## 🚀 框架架构与快速开始

当前系统采用 **Python 在线控制面 + Rust 受限执行器** 的双层解耦架构：
- **Python 控制面**：基于 Python 3.11+ 标准库构建，负责指标采集、负载画像、目标管理、证据库、路由推断与 Tool / Skill 注册。
- **Rust 执行器**：负责执行面白名单过滤、单例互斥排他、sysfs 状态真实核验、生命周期管理、超时管控与故障回滚。

开发时建议先运行无需 root 特权的**模拟闭环**，再在具备支持的目标内核上接入真实 scx 专家与标定数据。

### 1. 模拟闭环与基准测试

```bash
# 1. 检查控制面能力（Python 3.11+，仅依赖标准库，无需外部依赖安装）
python3 -m arma4os capabilities

# 2. 运行纯 Python 模拟闭环演示
python3 -m arma4os demo

# 3. 编译并使用 Python 控制面驱动 Rust 模拟执行器联调
cargo build --workspace --locked
python3 -m arma4os demo --executor ./target/debug/arma-executor \
  --executor-config configs/executor.simulated.json

# 4. 运行固定输入压缩任务，采集默认调度器上的基准数据
python3 -m benchmarks run --config configs/benchmarks.json \
  --output results/baseline-001 --repeats 5 --seed 42
python3 -m benchmarks summarize results/baseline-001

# 5. 回归与单元测试
python3 -m unittest discover -s tests -v
cargo test --workspace --locked
```

> **注意**：`demo` 输出的观测数据、专家评分与性能退化均为**合成数据 (Synthetic Data)**，用于验证协议格式、决策路由与异常回滚流程，不代表真实内核性能优化收益。

### 2. 在线模式与 Tool / Skill 交互

```bash
# 无特权只读监控（初始策略库为空且目标过期时，会详细解释保持默认的原因）
python3 -m arma4os run --ticks 5 --monitor-only

# 导出标准化 Tool schemas，或从标准输入接收 JSONL 请求进行交互
python3 -m arma4os tools --schemas
python3 -m arma4os tools

# 试运行（Dry-run）具备前置检查与恢复步骤的复合 Skill
python3 -m arma4os skill mixed_workload_tuning --dry-run
```

### 3. 真实内核调度执行

从当前开发机一条命令连接已启动的 VMware openEuler、同步源码、运行真实测试并取回结果：

```bash
make test-vm
# 地址变化或使用原生 Linux SSH 时可覆盖连接参数
make test-vm VM_HOST=root@192.168.66.128 VM_TRANSPORT=native
```

默认通过免密 SSH 连接 `root@192.168.66.128`；WSL 下自动使用 Windows PowerShell 的 SSH 与已配置的密钥。每次同步当前工作区源码（包含未提交修改）到新的客机目录，在客机执行 `make test-real`，再校验并取回结果到本地 `results/vmware/vm-*`。需要虚拟机已开机、网络和 SSH 认证可用；不会启动 VMware 或修改 SSH 配置。可配置变量见 [Makefile](Makefile)。

在已准备好 sched_ext 内核和 scx_simple 的 openEuler 客机中，可通过 Makefile 重跑真实烟雾测试：

```bash
# 在客机仓库根目录运行；此步骤只读，不加载调度器
make test-real-check

# 在具备 BPF 权限的管理员终端运行：构建、真实对比、回滚/EOF/SIGTERM 清理验证
make test-real

# 可覆盖调度器绝对路径、重复次数和全新结果目录
make test-real SCX_SIMPLE=/absolute/path/to/scx_simple \
  REAL_REPEATS=5 REAL_SEED=42 REAL_OUTPUT=results/real-001
```

默认调度器路径为 `/root/sched-ext-dev/tools/sched_ext/build/bin/scx_simple`，与当前 openEuler 实验机一致。默认每策略 1 次预热、5 次正式运行，结果自动保存到新的 `results/real-smoke-*` 目录；失败返回非零并保留日志。命令在当前主机执行，不自动 SSH 或 sudo。

在真实环境中执行时，需显式传入执行器二进制路径与实际配置文件：

```bash
python3 -m arma4os run --executor ./target/release/arma-executor \
  --executor-config configs/executor.real.json
```

> **内核安全与权限说明**：
> - 真实执行器严格限制仅接受配置文件预置的策略 ID 与固定 argv，不接受动态 shell 拼接。
> - 每次动作均实时比对 `/sys/kernel/sched_ext` 的状态与 `kernel_ops`，只管理框架自身拥有的调度器进程。
> - 在目标 openEuler 机器上线前，需预先确认内核版本（推荐 6.12+ 或包含 backport 支持）、BTF 具备情况、scx 二进制可用性及运行权限。

---

## 📁 主要目录职责

| 目录路径 | 核心职责说明 |
| :--- | :--- |
| `arma4os/` | Python 控制面：版本化合同、指标采集、画像分析、策略库、决策路由、反馈评价、Tool / Skill、审计与 CLI 入口 |
| `crates/arma-executor/` | Rust 执行面：JSONL 协议处理、策略白名单机制、单例互斥锁、sysfs 核验、超时管控与自动恢复 |
| `benchmarks/` | 固定基准负载、指标解析器、串行随机执行矩阵、原始指标记录与汇总分析器 |
| `configs/` | 执行器环境定义、优化目标模板、策略路由器、基准测试矩阵与 hook 的标准配置 |
| `hooks/` | 扩展 eBPF 观测与管控探针（如 `cgroup_skb` hook）源码 |
| `tests/` | Python 决策逻辑、恢复机制、权限范围及基准实验回归测试用例 |

---

## 📚 相关文档

详细设计、协议规范与实验方法请参阅各专题文档：

- 🏛️ **[架构设计与论文方法映射](docs/architecture.md)**：系统模块职责、反馈循环模型、路由设计与成本模型。
- 🔌 **[Rust/Python 跨语言协议](docs/protocol.md)**：JSONL 交互协议、动作生命周期、期限超时与故障恢复合同。
- 🗓️ **[比赛里程碑与实施计划](docs/competition-plan.md)**：阶段任务划分、交付件要求与实验矩阵设计。
