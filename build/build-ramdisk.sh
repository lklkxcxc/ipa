#!/bin/bash
# 构建 usb-provision ramdisk:ipa-usb.kernel + ipa-usb.initramfs
# 在构建容器内执行;产物输出到 /out
set -euo pipefail

OUT=${OUT:-/out}
mkdir -p "$OUT"

# 国内网络:基础镜像与 chroot apt 换国内源(均可用环境变量覆盖回官方源)
export DIB_RELEASE=${DIB_RELEASE:-noble}
export DIB_CLOUD_IMAGES=${DIB_CLOUD_IMAGES:-https://mirrors.ustc.edu.cn/ubuntu-cloud-images/$DIB_RELEASE/current}
export DIB_DISTRIBUTION_MIRROR=${DIB_DISTRIBUTION_MIRROR:-http://mirrors.aliyun.com/ubuntu}

cd /opt/usb-provision

# ubuntu(基础发行版) + ironic-python-agent-ramdisk(IPA+工具链) + usb-provision(编排器)
# ironic-python-agent-ramdisk 直接产出 .initramfs / .kernel 文件
disk-image-create -a amd64 -o ipa-usb \
    ubuntu \
    ironic-python-agent-ramdisk \
    usb-provision

cp -v ipa-usb.kernel ipa-usb.initramfs "$OUT/"/

echo "== ramdisk 构建完成 =="
ls -lh "$OUT"
