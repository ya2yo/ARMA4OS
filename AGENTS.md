# ARMA4OS Agent 工作约定

本文件适用于整个仓库，记录 ARMA4OS 的模块边界、行为约束与验证入口。
用户当前任务中的明确要求优先于本文件；进入子目录时同时检查更具体的 `AGENTS.md`。

## 1. 项目定位与实现边界

### 1.1 核心定位

**ARMA4OS** *(Adaptive Resource Management Agent for Operating Systems)* 是面向 openEuler / Linux 的**用户态自适应资源管控框架**。
核心目标是根据实际工作负载观测与明确的应用目标，通过 Linux `sched_ext` (scx) 调度策略和 cgroup 资源控制改善性能。模块设计见[架构](docs/architecture.md)，当前行为与实测结论通过代码及可复现验证确认，项目目标不能直接表述为已实现能力或已测收益。

### 1.2 运行与权限边界

- **在线控制无需 LLM**：调度热路径由上游 scx/BPF 实现；Python 在线循环使用确定性路由。Advisor 仅输出离线或低频结构化建议，经验证后才能进入策略证据库。
- **受限执行与协议校验**：Rust 执行器负责固定白名单、单例互斥、sysfs 核验和有界恢复。当前父子进程 stdio 通道不提供独立鉴权服务；部署者负责配置与进程权限，不能据此宣称 Python 与插件已获得特权隔离。
- **观测驱动决策**：画像与路由来自 procfs、cgroup 和应用 KPI；benchmark 阶段标签、未来时间表和事后最佳策略不能作为路由答案。

---

## 2. 模块分工

| 模块 / 路径 | 核心职责 | 实现语言与依赖 | 关键约定 |
| :--- | :--- | :--- | :--- |
| `arma4os/` | 控制面：采集、画像、目标、证据、路由、反馈、Tool / Skill | Python 3.11+，核心运行时仅依赖标准库 | 新增运行依赖须说明用途，并同步 `pyproject.toml` 与运行文档。 |
| `crates/arma-executor/` | 执行面：白名单、互斥、进程生命周期、sysfs 核验、期限与恢复 | Rust 2021，依赖由 `Cargo.lock` 锁定 | 真实模式使用绝对二进制路径，报告实际动作与恢复结果。 |
| `benchmarks/` | 工作负载、标定与实验结果重建 | Python / Shell | 调度动作复用 `arma4os.executor.RustExecutor` 及其清理机制。 |
| `arma4os/contracts.py`、`schemas/`、`crates/arma-executor/src/protocol.rs` | 控制面数据合同、外部 schema 与 Rust 协议 | Python / JSON Schema / Rust | 接口变更同步生产者、消费者、schema、合同测试与协议文档，说明版本兼容性。 |

新增 Tool / Skill / hook 时，复用现有注册与生命周期接口；hook 遵循 attach / stats / detach，变更动作声明参数和作用域。

---

## 3. 必须保持的行为

### 3.1 观测与证据可靠性

- **指标必须保真**：指标必须携带物理单位、数据来源、源时间戳与数据质量。缺失、过期或非有限值（NaN/Inf）必须显式处理，严禁静默填充为 0。
- **命名空间隔离**：应用 KPI 统一使用 `app.` 命名空间；重复读取不可刷新其原始测量时间戳。
- **证据严格隔离**：策略证据绑定特定机器、上下文范围、单位与置信区间；`synthetic` 标记贯穿实验与 Registry，真实系统严禁采用合成证据。
- **独立性与有效期**：标定的独立运行与在线采样窗口分别计数，重复窗口不能累积稳定票数；证据保留来源、独立重复与有效期。
- **实验事实分立陈述**：严格区分“代码编译/单元测试通过”、“模拟器闭环联调成功”、“真机内核加载运行”与“基准测试性能提升”。未在真机实测的内容绝不虚标为已测事实。

### 3.2 路由、防震荡与成本模型

- **保守效用与成本惩罚**：切换调度器存在客观耗时与缓存失效代价。`switch_cost_s` 表示卸载与加载完整转换的保守上界（涉及双专家时取两端最大值）。
- **效用单位**：相对收益乘评价窗口表示归一化效用积分；延迟目标不能据此宣称节省了墙钟时间。成本模型变更需同步单位、假设与验证方式，见[架构](docs/architecture.md)。
- **防震荡保护**：采用连续窗口和指数衰减投票机制，只有在预期相对收益积分跨越切换成本门槛且获得稳定支持后才触发切换；未满足条件时保守维持现状。

### 3.3 执行安全与防御性回滚

- **严格命令白名单**：执行器仅加载本地可信配置中的策略 ID 和固定绝对路径，拒绝任何动态拼接的 shell 脚本，不自动调用 sudo。
- **状态以 sysfs 为准**：全局 scx 调度器的加载成功必须以内核 `/sys/kernel/sched_ext/state` 状态及策略身份为准，不能仅凭启动子进程存活来判断。
- **全局单例与作用域**：全局 scx 策略使用 `global` 作用域；统一使用规范 `state_dir`；遇到非己拥有的外部活动调度器时报告冲突，拒绝暴力接管。
- **动作合同**：保留请求 ID、动作有效期、期望策略和幂等语义；同 ID 不同内容拒绝，失败动作也保留幂等结果。详细字段和预算见[执行协议](docs/protocol.md)。
- **有界恢复**：正常动作与紧急恢复分别有界。动作失败后尝试恢复已知目标，必要时退回默认策略；恢复成功也不能将原失败动作报告为成功。反馈回滚失败时保留重试状态，试用结束后继续健康与约束监控。
- **清理边界**：只管理自身拥有的进程、hook 和 pin；EOF、可处理信号及超时路径执行清理并如实报告结果。SIGKILL、掉电和内核崩溃的恢复能力单独验证，不能由用户态清理测试推断。

---

## 4. 环境、文档与验证

### 4.1 开发入口与测试目标

- 当前开发入口为 WSL，真实调度测试目标为 VMware 中的 openEuler 客机；执行前确认所在机器及当前内核与权限。连接参数以 `Makefile` 为准。
- 常规联调使用模拟执行器 `configs/executor.simulated.json`。真实 scx 加载、cgroup 写入和 eBPF attach 仅在任务包含这些系统变更且具备授权时执行。
- VMware 实验需考虑 vCPU 调度干扰与 PMU 可用性；指标缺失显式处理，保留虚拟化环境信息，结论限定到实际测试环境。

### 4.2 按任务查阅规范

| 任务涉及 | 查阅文档 |
| --- | --- |
| 模块边界、路由与成本模型 | [架构](docs/architecture.md) |
| 请求、期限、幂等与恢复生命周期 | [执行协议](docs/protocol.md) |
| 比赛范围、里程碑与实验设计 | [实施计划](docs/competition-plan.md) |

用户要求与相关项目规范定义应有行为；代码、配置和可复现验证确认当前行为。发现冲突时明确说明并同步修正，历史记录和计划中的预期不能直接当作当前事实。

### 4.3 验证要求与入口

所有命令均从对应机器的仓库根目录执行，入口以 `Makefile` 和 `.github/workflows/ci.yml` 为准。

| 变更范围 | 验证要求 |
| --- | --- |
| 仅文档 | 核对引用路径、命令与实现，执行 `git diff --check` |
| Python 行为 | 先运行相关 unittest；跨模块或控制循环变更运行完整 Python 测试 |
| Rust 行为 | 相关测试、`cargo test --workspace --locked`、rustfmt 与 Clippy |
| 跨语言协议或生命周期 | 完成两侧测试，构建 Rust 后运行 Python → Rust 模拟联调 |
| 真实 scx / cgroup / eBPF | 验证实际状态、异常恢复及退出清理；结论匹配实际证据 |

#### 常规本地验证（无需 root 或真实 scx）

```bash
# 完整测试集（Python 与 Rust 工作区测试）
make test

# 静态检查 (Clippy 零警告 + rustfmt 代码格式 + Python 语法编译)
make check

# 驱动 Rust 模拟执行器联调闭环
make build
python3 -m arma4os demo --executor ./target/debug/arma-executor \
  --executor-config configs/executor.simulated.json

# 格式与空白检查
git diff --check
```

#### 真实内核验证（涉及 openEuler 客机或 BPF 特权）

```bash
# 开发机执行：SSH 同步源码、客机构建与真实烟雾测试、取回结果
make test-vm

# openEuler 客机本地环境只读核验 (核验 sched_ext、sysfs、BTF 与调度器二进制状态)
make test-real-check

# openEuler 客机本地真实烟雾测试 (编译 release 执行器、运行 default + scx_simple 矩阵与回滚清理验证)
make test-real
```

`make test-vm` 和 `make test-real` 会加载内核调度器；`make test-real-check` 只读检查，不证明实际加载权限或 ABI 兼容性。

### 4.4 性能实验

按[实施计划](docs/competition-plan.md)设计和执行实验：同一环境串行切换策略，包含默认基线，预热与正式独立重复分开，按轮次随机化顺序。每次使用全新结果目录，保存环境、版本、参数、种子、实际顺序、退出状态和原始输出。

汇总由原始输出与执行记录重建；超时、非零退出、解析失败、元数据缺失和计划 trial 丢失均显式计入失败或无效，保留无收益与失败结果。生成数据和二进制放在忽略的 `results/` 或 `build/`，固定测试夹具放入测试目录。
