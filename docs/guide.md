# 环境配置指南

本项目运行于 **openEuler 24.03 LTS**。因 openEuler 官方发布的内核默认未开启 Linux `sched_ext` (scx) 特性，需要重新编译内核并开启对应配置，随后安装项目所需的构建与运行环境。

---

## 1. 基础依赖与工具链安装

在 openEuler 系统中执行以下命令，安装内核编译、eBPF 开发、Python 与 Rust 工具链：

```bash
# 基础构建与内核编译工具
sudo dnf install -y \
  gcc gcc-c++ make flex bison bc elfutils-libelf-devel openssl-devel \
  dwarves clang llvm libbpf-devel tar xz zstd diffutils grubby dracut

# Python 3.11+
sudo dnf install -y python3 python3-pip

# Rust 2021 工具链 (若未安装)
curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y
source "$HOME/.cargo/env"
```

> **注意**：BTF 生成依赖 `dwarves`（含 `pahole`），要求版本 `>= 1.24`（openEuler 24.03 官方源提供 1.28，已满足要求）。

---

## 2. openEuler 内核重编译（启用 sched_ext）

### 2.1 为什么必须重编译内核

- `CONFIG_SCHED_CLASS_EXT` 是内核核心调度类的 `bool` 配置项，**无法**作为外部内核模块（`.ko`）动态加载。
- openEuler 24.03 LTS SP3 源码（OLK-6.6）中已合入 `sched_ext` 相关实现，但官方预编译二进制内核默认未启用（`# CONFIG_SCHED_CLASS_EXT is not set`），因此必须通过源码重新编译。
- 必须基于当前系统的 `/boot/config-$(uname -r)` 底包进行修改，以完整保留硬件及虚拟机驱动。

### 2.2 获取官方内核源码

下载与当前系统版本一致的官方源码包（以 24.03 SP3 为例）：

```bash
# 查看当前运行内核版本
uname -r

# 下载官方 SRPM 源码包
wget https://repo.openeuler.org/openEuler-24.03-LTS-SP3/update/source/Packages/kernel-6.6.0-145.3.35.166.oe2403sp3.src.rpm

# 解包并应用所有官方补丁
rpm -ivh kernel-6.6.0-145.3.35.166.oe2403sp3.src.rpm
cd ~/rpmbuild/SPECS
rpmbuild -bp kernel.spec

# 进入展开后的源码目录
cd ~/rpmbuild/BUILD/kernel-6.6.0/linux-6.6.0-*
```

### 2.3 修改内核配置

```bash
# 1. 复制当前正在运行的内核配置作为底包
cp /boot/config-$(uname -r) .config

# 2. 启用 sched_ext 调度类
scripts/config --enable CONFIG_SCHED_CLASS_EXT

# 3. 确保 BPF 与 BTF 支持已开启
scripts/config --enable CONFIG_BPF_SYSCALL
scripts/config --enable CONFIG_BPF_JIT
scripts/config --enable CONFIG_DEBUG_INFO_BTF
scripts/config --enable CONFIG_DEBUG_INFO_BTF_MODULES

# 4. 清空系统信任密钥路径（避免本地无官方私钥导致编译中断报错）
scripts/config --set-str CONFIG_SYSTEM_TRUSTED_KEYS ""
scripts/config --set-str CONFIG_SYSTEM_REVOCATION_KEYS ""

# 5. 设置版本后缀，避免覆盖原官方内核模块
scripts/config --set-str CONFIG_LOCALVERSION "-arma-scx"
scripts/config --disable CONFIG_LOCALVERSION_AUTO

# 6. 补全依赖并更新配置
make olddefconfig

# 7. 检查关键项是否配置成功
grep -E "CONFIG_SCHED_CLASS_EXT|CONFIG_DEBUG_INFO_BTF|CONFIG_SYSTEM_TRUSTED_KEYS" .config
```

### 2.4 编译与安装新内核

```bash
# 1. 编译内核与模块
make -j$(nproc) bzImage modules

# 2. 安装模块到 /lib/modules/
sudo make modules_install

# 3. 安装内核二进制与 System.map 到 /boot
KERNEL_RELEASE=$(make -s kernelrelease)
sudo install -m 0644 arch/x86/boot/bzImage /boot/vmlinuz-${KERNEL_RELEASE}
sudo install -m 0644 System.map /boot/System.map-${KERNEL_RELEASE}
sudo install -m 0644 .config /boot/config-${KERNEL_RELEASE}

# 4. 生成 initramfs
sudo dracut --kver ${KERNEL_RELEASE} --force \
  --add-drivers "mptspi mptscsih mptbase e1000 vmxnet3 ext4 dm_mod" \
  /boot/initramfs-${KERNEL_RELEASE}.img

# 5. 添加启动项
sudo grubby --add-kernel=/boot/vmlinuz-${KERNEL_RELEASE} \
  --initrd=/boot/initramfs-${KERNEL_RELEASE}.img \
  --title="openEuler (${KERNEL_RELEASE}) 24.03" \
  --copy-default --args="panic=15"

# 6. 设置为默认启动内核并重启系统
sudo grubby --set-default=/boot/vmlinuz-${KERNEL_RELEASE}
sudo reboot
```

### 2.5 启动后环境核验

重启登录系统后，核验 `sched_ext` 是否正常启用：

```bash
# 1. 检查当前内核版本（应包含 -arma-scx 后缀）
uname -r

# 2. 检查 sched_ext 控制接口（初始应输出 disabled）
cat /sys/kernel/sched_ext/state

# 3. 检查 BTF 文件是否存在
ls -lh /sys/kernel/btf/vmlinux
```

---

## 3. 编译调度器与项目执行器

### 3.1 编译 scx 调度器二进制

内核源码的 `tools/sched_ext` 目录下自带测试调度器，可直接编译：

```bash
cd ~/rpmbuild/BUILD/kernel-6.6.0/linux-6.6.0-*/tools/sched_ext
make -j$(nproc) scx_simple

# 安装到指定目录供执行器调用
sudo mkdir -p /root/sched-ext-dev/tools/sched_ext/build/bin
sudo cp build/bin/scx_simple /root/sched-ext-dev/tools/sched_ext/build/bin/
sudo chmod +x /root/sched-ext-dev/tools/sched_ext/build/bin/scx_simple
```

### 3.2 编译 ARMA4OS 执行器与环境检查

进入 ARMA4OS 项目目录进行构建与环境核验：

```bash
cd /path/to/ARMA4OS

# 1. 编译 Rust 执行器
make build
# 或发布版本
make build-release

# 2. 执行环境只读核验（检查 sysfs、BTF 和调度器路径）
make test-real-check SCX_SIMPLE=/root/sched-ext-dev/tools/sched_ext/build/bin/scx_simple

# 3. 执行单机测试
make test
```
