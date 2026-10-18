# 框架交付与验证记录

本轮从仅有 README / 实施计划的仓库补齐 Python + Rust 框架。验证的是可运行的工程闭环与证据链，不代表已完成比赛的性能与部署验收。

## 最终验证

| 命令 / 工作流 | 结果 | 证明范围 |
| --- | --- | --- |
| `python3 -W error::ResourceWarning -m unittest discover -s tests -v` | 94 passed | 目标 / 成本路由、采集、KPI、反馈与重试、Tool / Skill、hook 生命周期、CLI 信号、实验解析和数据完整性 |
| `cargo test --workspace --locked` | 34 passed | 配置 / 协议、互斥、模拟启停、假 sysfs、动作期限、失败恢复、管道中断、缓存与自有进程清理 |
| `cargo clippy --workspace --all-targets --locked -- -D warnings` | 通过 | Rust 静态检查零警告 |
| `cargo fmt --all -- --check` | 通过 | Rust 格式 |
| `cargo build --workspace --release --locked` | 通过 | release 执行器可构建 |
| `python3 -m compileall -q arma4os benchmarks tests` | 通过 | Python 模块语法与导入可编译 |
| `clang -O2 -g -target bpf -I/usr/include/x86_64-linux-gnu -c hooks/cgroup_counter.bpf.c -o /tmp/arma-cgroup-counter.bpf.o` | 通过 | eBPF 示例源码可编译；不证明内核可加载 |
| Python Agent + release Rust simulated executor | 通过 | 连续观测、选择 bpfland、状态核验、SLO 退化恢复 default、清理退出 |
| 默认调度器压缩烟雾实验 | 5 次正式运行 + 1 次预热，0 失败 | 实际工作负载、原始输出、独立重复和重建路径 |
| 模拟三策略矩阵 + 策略库导入 | 15 次正式运行 + 3 次预热，0 失败 | 串行随机矩阵、受管理生命周期、完整记录和合成标记传播；禁止作为调度器性能比较 |

开发产物保存在被 git 忽略的 `results/validation/`：

- `default-smoke/`：实际默认策略烟雾运行与原始输出。
- `rust-demo/`：明确标记的合成闭环审计。
- `simulated-matrix/`：实际压缩任务 + 模拟策略标签，manifest 和 summary 均标记不可用于真实策略比较。
- `synthetic-registry.json`：导入草案，Registry 与每条 Evidence 均保留 synthetic，真实后端不能使用。

## 独立审查与修正

并行使用 pi 默认 agent / 默认 `antigravity/gemini-3.8-flash-high` 完成 Rust、实验运行器与独立审查，主进程复核并执行最终检查。未回退 DeepSeek 或 Codex。首次 pi 枚举模型因沙箱内配置锁目录只读失败，授权后恢复；实际委派保留默认扩展和 MCP 配置。用户另行要求删除的 gemini-4-argon 仅从活动模型配置移除，默认模型未变。

审查结果按当前代码重新核对，采用并验证的主要修正：

- 保留回滚失败的试用状态，允许下一轮恢复；试用结束后继续监测约束。
- 显式携带恢复目标，重复回滚不重新激活故障策略，避免 Skill 对执行器已恢复状态二次回滚。
- 计划有效期在 Python 与 Rust 双侧核验；真实动作要求 expected_policy 与 expires_at_unix_ms。
- 只读缓存与动作去重分开，10,010 次 Rust 状态查询不耗尽动作配额；相同 ID 不同请求拒绝。
- 相邻 profile / plan 共用实际采样窗口；重复观测不能凑够稳定票数。
- 实际 sysfs 身份核验，不把子进程存活当成功；自有 loader 被内核卸载后可确认并清理回默认，外部活动策略仍拒绝接管。
- 请求总预算与独立紧急恢复预算分开；stdout 管道中断、EOF 与可处理信号均有清理路径。
- 原始输出与 record.json 重建指标，不信任缓存分数；缺少元数据或丢失已计划 trial 计失败，不静默丢弃。
- 合成标记下沉到 Evidence，核心 Agent 再次拦截真实执行；导出的汇总单独保留模拟标记。
- 归一化目标效用积分与完整切换耗时上界比较，避免将延迟收益代理误写成节省的墙钟秒，也避免对完整转换重复计费。

未直接采纳的审查建议：失败动作的幂等结果不能随意丢弃，否则结果不确定的动作可能重放；未来时间戳继续保守拒绝，修正的是测试时钟顺序而非放宽新鲜度；模板目标过期和空策略库是明确的初始配置状态，启用前由用户配置目标与标定证据。

开发中出现过一次 Python 测试时钟失败（56 项中 1 项失败），修正后最终 94 项通过。Rust 加强必填前置字段时暴露旧测试缺字段，并出现过格式与 Clippy 布尔表达式检查失败，已更新测试合同与格式并经最终全量检查通过。没有通过删掉失败用例或弱化状态核验消除失败。

## 尚未验证的目标机路径

只读检查确认当前内核 `6.18.33.2-microsoft-standard-WSL2+` 具备 sched_ext sysfs、BTF 和 cgroup v2，检查结束时真实 sched_ext 状态为 disabled。本次未真实加载 scx 或 attach eBPF。

尚需按实施计划完成：真实专家与目标内核 ABI / 权限联调、实际加载与退出恢复、三场景 45 次正式静态矩阵、实测上下文与转换成本、动态与消融实验、长时间稳定性、openEuler 部署和交叉复现。

当前 root 权限边界由可信本地部署者负责，stdio 子进程不是带鉴权的特权守护服务；MCP 和模型训练保留为扩展方向。Advisor provider 需自行支持可取消 I/O 超时。上述边界已在运行指南说明，不把编译、模拟或单元测试等同于真实内核加载成功或性能收益。
