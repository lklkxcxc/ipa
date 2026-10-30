# usb-provision ramdisk 构建环境
# 基于 Ubuntu 22.04 + diskimage-builder + ironic-python-agent-builder
# 构建(docker 上下文 = usb-provision 项目根目录):
#   cd <usb-provision> && docker build -f build/Dockerfile -t usb-provision-build .
FROM ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
        python3 python3-pip python3-dev git curl wget xz-utils cpio bzip2 \
        build-essential libssl-dev libffi-dev \
        qemu-utils parted dosfstools e2fsprogs sudo squashfs-tools \
        grub-pc-bin grub-efi-amd64-bin grub2-common grub-common mtools \
        kpartx mdadm gdisk \
        qemu-system-x86 ovmf \
    && rm -rf /var/lib/apt/lists/*

# diskimage-builder(DIB)负责组装 ramdisk
RUN pip3 install 'diskimage-builder>=3.30.0' PyYAML

# ironic-python-agent-builder:官方 DIB elements
RUN git clone --depth 1 https://github.com/openstack/ironic-python-agent-builder.git /opt/ironic-python-agent-builder

# usb-provision 自定义 element 与脚本(上下文 = 项目根目录)
COPY build/element /opt/usb-provision/element
COPY build/build-ramdisk.sh build/make-usb.sh build/grub.cfg /opt/usb-provision/
COPY deploy.yaml /opt/usb-provision/deploy.yaml

RUN chmod +x /opt/usb-provision/*.sh /opt/usb-provision/element/usb-provision/extra-data.d/* \
                                 /opt/usb-provision/element/usb-provision/install.d/*

ENV ELEMENTS_PATH=/opt/ironic-python-agent-builder/dib:/opt/usb-provision/element

CMD ["/opt/usb-provision/build-ramdisk.sh"]
