# usb-provision — 基于 U 盘的本地自动化装机系统 (PoC)

基于 OpenStack ironic-python-agent (IPA) 的本地装机方案:**不依赖 Ironic API**,全部装机逻辑由
U 盘上的配置文件 + ramdisk 内的本地编排器驱动,复用 IPA 成熟的写盘/bootloader/configdrive/RAID/擦盘能力。

## 架构

```
U 盘 (4 分区)
├── p1 bios-grub    无文件系统             grub BIOS Boot Partition (core.img)
├── p2 ESP          FAT32, 卷标 ESP       grub + kernel + initramfs (2GiB, BIOS/UEFI 双启)
├── p3 CONFIG-2     FAT32, 卷标 config-2  deploy.yaml + etc/ironic-python-agent.d (256MiB)
└── p4 IPA-IMAGES   FAT32/ext4             系统镜像 + .sha256 + (可选) configdrive.iso
```

启动流程:

1. BIOS/UEFI → grub → kernel + IPA initramfs(不加 `ipa-api-url`,IPA 跳过 lookup/heartbeat,
   但自身 REST API 照常监听 `127.0.0.1:9999`)
2. `usb-provision-config.service`(Before=ironic-python-agent.service)把 config-2 分区的
   `etc/ironic-python-agent.d/` 拷入 ramdisk,作为 IPA 的 `--config-dir`(可下发 debug、
   listen_port 等 agent 配置;注:IPA 原生 vmedia 拷贝机制排除分区设备,故不用
   `boot_method=vmedia`)
3. systemd 拉起 `usb-provision.service` → `orchestrator.py`
4. 编排器挂载 `config-2` 分区读取 `deploy.yaml`,挂载 IPA-IMAGES 分区并在 `127.0.0.1:8484` 起本地
   HTTP 服务(IPA 镜像下载只支持 http/https,不支持 file://)
5. 按序调用 IPA REST 命令完成装机:
   - (可选) `clean.execute_clean_step` — 擦盘
   - 编排器选定目标盘(`target.device` 显式指定,否则排除 U 盘后取最大非可移动盘,
     写入 `/run/usb-provision/target-device`;IPA 侧 `hardware.py` 构建期已打补丁优先读取,
     避免 standalone 模式把 U 盘当装机盘)
   - `standby.prepare_image` — 下载镜像、校验 sha256、写盘、写 configdrive
   - (分区镜像) `image.install_bootloader` — chroot 目标镜像装 grub(BIOS/UEFI)
6. 结果写回 `config-2` 分区 `deploy-result.json`,按配置关机

> 命令端点鉴权:新版 IPA 强制校验 `agent_token`,采用两侧预共享 —— config-2 的
> `etc/ironic-python-agent.d/agent.conf`(`make-usb.sh` 烧录时写入,同一值亦烧进
> initramfs 兜底)与编排器 `AGENT_TOKEN` 常量必须一致。

## 目录

```
deploy.yaml                                装机配置示例(会被复制到 U 盘 `config-2` 分区)
build/
  Dockerfile                               构建环境(Ubuntu + diskimage-builder)
  build-ramdisk.sh                         构建 IPA initramfs + kernel
  element/usb-provision/orchestrator.py    本地编排器(打包进 ramdisk)
  element/usb-provision/                   自定义 DIB element(extra-data/install/systemd)
  grub.cfg                                 U 盘 grub 配置
  make-usb.sh                              组装 usb-image.raw 四分区磁盘镜像(含 agent.conf 下发)
tools/
  make-configdrive.py                      生成 NoCloud configdrive.iso(用户/密码/SSH/网络,bond)
  go/make-configdrive/main.go              同功能的 Go 版:单文件零依赖,go build 出静态二进制
```

## 构建

```bash
# 1. 构建环境镜像(上下文 = usb-provision 项目根)
docker build -f build/Dockerfile -t usb-provision-build .

# 2. 构建 ramdisk:产出 dist/ipa-usb.{kernel,initramfs}
docker run --rm -v "$(pwd)/dist:/out" usb-provision-build

# 3. 组装 U 盘镜像:dist/usb-image.raw(需要 loop 设备权限,在宿主机原生运行)
OUT=$(pwd)/dist IPA_DIR=$(pwd) bash build/make-usb.sh
```

## 烧录与验证

```bash
# 写入 U 盘(慎用,确认设备名)
dd if=usb-image.raw of=/dev/sdX bs=4M conv=fsync status=progress

# 或先用 QEMU 验证(BIOS 模式 / UEFI 模式)
qemu-system-x86_64 -m 4096 -hda usb-image.raw -nographic
qemu-system-x86_64 -m 4096 -hda usb-image.raw -bios /usr/share/ovmf/OVMF.fd -nographic
```

目标机要求:内存 ≥ 3 GB(initramfs 解包后约 1.4 GB),≥ 1 块非可移动磁盘。

## 已知限制 (PoC)

- `deploy.yaml` 中 U 盘卷标/分区名按 PoC 约定,后续参数化
- 目标盘选择:排除 U 盘后取最大非可移动盘;精确指定用 `deploy.yaml` 的 `target.device`
- FAT32 单文件上限 4GiB;超过请把 IPA-IMAGES 分区改为 ext4
- UEFI Secure Boot 未验证,目标机请先关闭
- 分区镜像 (partition image) 要求镜像内含 grub2(`install_bootloader` 在目标镜像内 chroot 执行)
- 整盘镜像自带 bootloader,自动跳过 install_bootloader
- 云镜像用 **generic** 变体(如 `jammy-server-cloudimg-amd64.img`);`-kvm` 变体无
  LSI SCSI 等驱动,VMware 上装完系统启动黑屏假死(详见手册 §7)
