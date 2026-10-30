#!/bin/bash
# 组装 usb-image.raw:四分区 U 盘镜像
#   p1 bios-grub   (无文件系统)            BIOS Boot Partition,grub core.img
#   p2 ESP         FAT32 卷标 ESP         grub(BIOS+UEFI)+ kernel + initramfs
#   p3 config-2    FAT32 卷标 config-2    deploy.yaml + etc/ironic-python-agent.d
#   p4 ipa-images  FAT32 卷标 ipa-images  镜像文件(生产可换 ext4)
# 前置条件:build-ramdisk.sh 已产出 /out/ipa-usb.{kernel,initramfs}
# 需要在具备 loop 设备权限的 Linux 容器/主机中运行
# 兼容 Docker Desktop/WSL2:其内核 loop 模块 max_part=0,不支持分区扫描
# (无 /dev/loopXpN),因此按 GPT 偏移量为每个分区单独绑定 loop 设备
set -euo pipefail

OUT=${OUT:-/out}
IMG="$OUT/usb-image.raw"
IPA_DIR=${IPA_DIR:-/opt/usb-provision}
# Docker 镜像里 grub.cfg/deploy.yaml 拍平在 IPA_DIR;源码树里 grub.cfg 在 build/ 子目录
GRUB_CFG=$IPA_DIR/grub.cfg
[[ -f $GRUB_CFG ]] || GRUB_CFG=$IPA_DIR/build/grub.cfg
DEPLOY_YAML=$IPA_DIR/deploy.yaml
[[ -f $DEPLOY_YAML ]] || DEPLOY_YAML=$IPA_DIR/build/../deploy.yaml
SIZE=${USB_SIZE:-8G}

KERNEL="$OUT/ipa-usb.kernel"
INITRAMFS="$OUT/ipa-usb.initramfs"
[[ -f $KERNEL && -f $INITRAMFS ]] || { echo "缺少 ramdisk 产物,请先运行 build-ramdisk.sh"; exit 1; }

rm -f "$IMG"
truncate -s "$SIZE" "$IMG"

# ---- 分区表 ----
# p1 bios_grub: GPT 下 BIOS 启动必须有 BIOS Boot Partition(grub 嵌入 core.img)
parted -s "$IMG" mklabel gpt \
    mkpart bios-grub 34s 2047s \
    set 1 bios_grub on \
    mkpart ESP fat32 1MiB 2049MiB \
    set 2 esp on \
    mkpart config fat32 2049MiB 2305MiB \
    mkpart images fat32 2305MiB 100%

# ---- loop 绑定 ----
LOOP=$(losetup -f --show "$IMG")     # 整盘(BIOS grub 嵌入用)
DEVS=("$LOOP")
MNT=$(mktemp -d)

cleanup() {
    umount -R "$MNT" 2>/dev/null || true
    for d in "${DEVS[@]}"; do losetup -d "$d" 2>/dev/null || true; done
}
trap cleanup EXIT

# bind_part <partnum>:按 GPT 偏移量把分区绑定为独立 loop 设备并回显路径
bind_part() {
    local part=$1 start end
    read -r start end < <(parted -s -m "$IMG" unit s print | awk -F: -v p="$part" \
        '$1==p{gsub(/s/,"",$2); gsub(/s/,"",$3); print $2, $3}')
    losetup -f --show -o $((start * 512)) --sizelimit $(((end - start + 1) * 512)) "$IMG"
}

ESP=$(bind_part 2); DEVS+=("$ESP")
CFG=$(bind_part 3); DEVS+=("$CFG")
IMGS=$(bind_part 4); DEVS+=("$IMGS")

mkfs.vfat -n ESP        "$ESP"
mkfs.vfat -n config-2   "$CFG"
mkfs.vfat -n ipa-images "$IMGS"

# ---- p2: ESP + grub ----
mount "$ESP" "$MNT"

mkdir -p "$MNT/boot/grub"
cp "$KERNEL"     "$MNT/boot/ipa-usb.kernel"
cp "$INITRAMFS"  "$MNT/boot/ipa-usb.initramfs"
cp "$GRUB_CFG" "$MNT/boot/grub/grub.cfg"

# ---- BIOS 引导(手工组装:偏移绑定的 loop 无分区设备节点,
#      grub-install/grub-probe 无法解析拓扑,故显式指定 prefix)----
# (hd0,gpt2):BIOS 从 U 盘启动时 U 盘即第一块 BIOS 硬盘
GRUB_PC=/usr/lib/grub/i386-pc
grub-mkimage -O i386-pc -o /tmp/grub-core.img \
    -p '(hd0,gpt2)/boot/grub' \
    biosdisk part_gpt fat normal linux echo ls sleep search search_fs_file
mkdir -p /tmp/grub-bios-setup && cd /tmp/grub-bios-setup
cp $GRUB_PC/boot.img .
cp /tmp/grub-core.img .
# grub-bios-setup 自行解析 GPT:core.img 嵌入 p1 bios-grub 分区,
# 并把 MBR boot.img 的 blocklist 补丁指向它(无分区设备节点也可用)
# 注意 -b/-c 相对 -d 目录解析,只能传文件名
$GRUB_PC/grub-bios-setup -d . -b boot.img -c grub-core.img -f "$LOOP"
mkdir -p "$MNT/boot/grub/i386-pc"
cp $GRUB_PC/*.mod "$MNT/boot/grub/i386-pc/"
# UEFI(grub-install --removable 只写文件,不探测设备)
grub-install --target=x86_64-efi --efi-directory="$MNT" \
    --boot-directory="$MNT/boot" --removable --no-nvram
echo "grub 安装完成 (BIOS 手工组装 + UEFI)"

# ---- p3: config-2 ----
mkdir -p "$MNT/config2"
mount "$CFG" "$MNT/config2"
cp "$DEPLOY_YAML" "$MNT/config2/"
# agent 配置钩子:usb-provision-config.service 会在 IPA 启动前
# 把此目录拷入 ramdisk 的 /etc/ironic-python-agent.d/(IPA --config-dir)
mkdir -p "$MNT/config2/etc/ironic-python-agent.d"
# 新版 IPA 命令端点强制 agent_token 鉴权,standalone 模式无 Ironic 下发,
# 两侧预共享(与 element/usb-provision/orchestrator.py 的 AGENT_TOKEN 一致)
cat > "$MNT/config2/etc/ironic-python-agent.d/agent.conf" <<'EOF'
[DEFAULT]
agent_token = usb-provision-poc-0123456789abcdef0123456789abcdef
EOF
umount "$MNT/config2"

# ---- p4: ipa-images ----
mkdir -p "$MNT/images"
mount "$IMGS" "$MNT/images"
cat > "$MNT/images/README.txt" <<'EOF'
把装机镜像放这里:
  rootfs.qcow2 / rootfs.raw        (deploy.yaml image.file 对应)
  rootfs.qcow2.sha256              (sha256sum 格式)
  configdrive.iso                  (可选,deploy.yaml configdrive.file)
注意: FAT32 单文件上限 4GiB,超出请改用 ext4 分区。
EOF
umount "$MNT/images"

umount -R "$MNT"
echo "== USB 镜像组装完成: $IMG =="
ls -lh "$IMG"
