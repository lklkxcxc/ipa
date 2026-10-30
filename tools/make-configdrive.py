#!/usr/bin/env python3
"""make-configdrive.py —— 生成 NoCloud 格式的 configdrive.iso。

给 usb-provision U 盘装机用:生成 cloud-init NoCloud 配置盘(IPA-IMAGES 分区里的
configdrive.iso,deploy.yaml configdrive.file 指向它)。

用法示例(创建用户 enrigin / 密码 enrigin,开 SSH 密码登录):

    python3 make-configdrive.py --user enrigin --password enrigin \
        --hostname enrigin-node --out configdrive.iso

网络配置(可选):
    # 默认:不传网络参数 = cloud-init 对第一块网卡 DHCP
    # 静态 IP:
    python3 make-configdrive.py ... --ip 192.168.1.10/24 --gateway 192.168.1.1 \
        --dns 114.114.114.114,8.8.8.8 --iface eth0
    # bond(802.3ad):
    python3 make-configdrive.py ... --bond bond0 --bond-slaves eth0,eth1 \
        --bond-mode 802.3ad --ip 10.0.0.5/24 --gateway 10.0.0.1
    # 任意复杂拓扑:直接给 netplan(v2)文件
    python3 make-configdrive.py ... --network-config my-netplan.yaml

依赖:python3 标准库 + 系统里任意一个 ISO 生成工具
      (genisoimage / mkisofs / xorriso,Ubuntu 上 apt install genisoimage)。

仅本脚本即可完成,不需要其它文件;生成的 ISO 卷标为 cidata(NoCloud 约定),
根目录含 user-data + meta-data(+ network-config,若配置了网络)。
"""

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
import time

# NoCloud 数据源约定:卷标必须是 cidata,文件在卷根目录
VOLID = 'cidata'

USER_DATA_TEMPLATE = """#cloud-config
hostname: {hostname}
manage_etc_hosts: true
ssh_pwauth: {ssh_pwauth}
users:
  - default
  - name: {user}
    sudo: ALL=(ALL) NOPASSWD:ALL
    groups: [sudo]
    shell: /bin/bash
    lock_passwd: false
    plain_text_passwd: {password}
chpasswd:
  expire: false
"""

META_DATA_TEMPLATE = """instance-id: {instance_id}
local-hostname: {hostname}
"""

# 常见 ISO 生成工具;xorriso 走 -as mkisofs 兼容模式
ISO_TOOLS = [
    ['genisoimage'],
    ['mkisofs'],
    ['xorriso', '-as', 'mkisofs'],
]


def find_iso_tool():
    """找到系统里可用的 ISO 生成工具,返回命令前缀列表,找不到返回 None。"""
    for cmd in ISO_TOOLS:
        if shutil.which(cmd[0]):
            return cmd
    return None


# ---------------------------------------------------------------- 网络配置

def _split_list(s):
    """'a,b,c' -> ['a', 'b', 'c']。"""
    return [x.strip() for x in s.split(',') if x.strip()]


def _routes(gateway):
    if not gateway:
        return []
    return [{'to': 'default', 'via': gateway}]


def _nameservers(dns):
    dns = _split_list(dns or '')
    if not dns:
        return {}
    # netplan v2 语义:nameservers 是嵌套结构,不能与 addresses 平级
    return {'nameservers': {'addresses': dns}}


def build_network_config(args):
    """按命令行参数组装 cloud-init network-config v2(netplan 语法) dict。

    优先级:--network-config 文件透传 > bond > 单网卡。
    无任何网络参数时返回 None(= cloud-init 默认 DHCP 第一块网卡)。
    """
    if args.network_config:
        with open(args.network_config, encoding='utf-8') as f:
            text = f.read()
        # 容忍 Windows 产物
        if text.startswith('﻿'):
            text = text.lstrip('﻿')
        return {'_raw': text}   # 原样嵌入,不做 yaml 解析

    has_static = args.ip or args.gateway or args.dns
    if not args.bond and not has_static:
        return None
    if has_static and not args.ip:
        sys.exit('错误: 指定了 --gateway/--dns 但没有 --ip(静态配置需要地址,'
                 '如 --ip 192.168.1.10/24)')

    addresses = _split_list(args.ip or '')
    for a in addresses:
        if '/' not in a:
            sys.exit('错误: --ip 必须带前缀长度(CIDR),如 192.168.1.10/24,收到: %s' % a)

    if args.bond:
        if not args.bond_slaves:
            sys.exit('错误: --bond 需要同时指定 --bond-slaves(如 eth0,eth1)')
        iface_cfg = {
            'interfaces': _split_list(args.bond_slaves),
            'parameters': {'mode': args.bond_mode},
        }
        if args.bond_lacp_rate and args.bond_mode == '802.3ad':
            iface_cfg['parameters']['lacp-rate'] = args.bond_lacp_rate
        if args.mtu:
            iface_cfg['mtu'] = args.mtu
        iface_cfg.update({
            'addresses': addresses,
            'routes': _routes(args.gateway),
        })
        iface_cfg.update(_nameservers(args.dns))
        return {'version': 2, 'bonds': {args.bond: iface_cfg}}

    iface = args.iface or 'eth0'
    iface_cfg = {'addresses': addresses, 'routes': _routes(args.gateway)}
    iface_cfg.update(_nameservers(args.dns))
    if args.mtu:
        iface_cfg['mtu'] = args.mtu
    return {'version': 2, 'ethernets': {iface: iface_cfg}}


# ---------------------------------------------------------------- ISO 生成

def build_iso(out_path, files):
    """把 {文件名: 内容} 做成 NoCloud ISO。"""
    tool = find_iso_tool()
    if not tool:
        sys.exit('错误: 系统里没有 genisoimage / mkisofs / xorriso,'
                 '请先安装(Ubuntu: apt install genisoimage)')

    with tempfile.TemporaryDirectory() as tmp:
        grafts = []
        for name, content in files.items():
            path = os.path.join(tmp, name)
            with open(path, 'w', encoding='utf-8') as f:
                f.write(content)
            grafts.append('%s=%s' % (name, path))

        cmd = tool + [
            '-output', out_path,
            '-volid', VOLID,
            '-joliet', '-rock',
            '-graft-points',
        ] + grafts
        subprocess.run(cmd, check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)


def main():
    ap = argparse.ArgumentParser(
        description='生成 usb-provision 用的 NoCloud configdrive.iso',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog='网络配置示例:\n'
               '  静态IP: --ip 192.168.1.10/24 --gateway 192.168.1.1 --dns 8.8.8.8 --iface eth0\n'
               '  bond:   --bond bond0 --bond-slaves eth0,eth1 --bond-mode 802.3ad '
               '--ip 10.0.0.5/24 --gateway 10.0.0.1\n'
               '  透传:   --network-config netplan.yaml(v2/netplan 语法,支持 VLAN/路由等任意拓扑)')
    ap.add_argument('--user', required=True, help='创建的用户名')
    ap.add_argument('--password', required=True, help='该用户的登录密码(明文)')
    ap.add_argument('--hostname', default='ubuntu-server',
                    help='目标机主机名(默认 ubuntu-server)')
    ap.add_argument('--instance-id', default=None,
                    help='cloud-init instance-id(默认 usb-provision-<时间戳>;'
                         '重装时换一个可强制 cloud-init 重新执行)')
    ap.add_argument('--no-ssh-password', action='store_true',
                    help='关闭 SSH 密码登录(默认开启)')
    ap.add_argument('--out', default='configdrive.iso',
                    help='输出文件名(默认 configdrive.iso)')

    net = ap.add_argument_group('网络配置(不传 = 第一块网卡 DHCP)')
    net.add_argument('--ip', default=None,
                     help='静态地址,CIDR 格式,多地址逗号分隔(如 192.168.1.10/24,10.0.0.5/24)')
    net.add_argument('--gateway', default=None, help='默认网关')
    net.add_argument('--dns', default=None, help='DNS 服务器,逗号分隔')
    net.add_argument('--iface', default=None,
                     help='静态 IP 所在网卡(默认 eth0;与 --bond 互斥)')
    net.add_argument('--bond', default=None,
                     help='bond 接口名(如 bond0),与 --iface 互斥')
    net.add_argument('--bond-slaves', default=None,
                     help='bond 成员网卡,逗号分隔(如 eth0,eth1)')
    net.add_argument('--bond-mode', default='802.3ad',
                     help='bond 模式(默认 802.3ad;active-backup/802.3ad/balance-xor ...)')
    net.add_argument('--bond-lacp-rate', default=None,
                     help='LACP 协商频率,仅 802.3ad:fast|slow')
    net.add_argument('--mtu', type=int, default=None, help='接口 MTU')
    net.add_argument('--network-config', default=None,
                     help='直接使用 netplan(v2) YAML 文件,优先级最高')

    args = ap.parse_args()

    if args.bond and args.iface:
        sys.exit('错误: --bond 与 --iface 互斥(bond 场景静态地址配在 bond 口上)')

    instance_id = args.instance_id or 'usb-provision-%d' % time.time()

    user_data = USER_DATA_TEMPLATE.format(
        hostname=args.hostname,
        ssh_pwauth='false' if args.no_ssh_password else 'true',
        user=args.user,
        password=args.password)
    meta_data = META_DATA_TEMPLATE.format(
        instance_id=instance_id, hostname=args.hostname)

    net_cfg = build_network_config(args)

    files = {'user-data': user_data, 'meta-data': meta_data}
    if net_cfg:
        if '_raw' in net_cfg:
            files['network-config'] = net_cfg['_raw']
        else:
            # network-config v2 用标准 YAML 输出(依赖 PyYAML;无网络配置时零依赖)
            try:
                import yaml
            except ImportError:
                sys.exit('错误: 配置网络需要 PyYAML(pip install pyyaml 或 '
                         'apt install python3-yaml);不带网络参数则无需此依赖')
            files['network-config'] = yaml.safe_dump(
                net_cfg, default_flow_style=False, sort_keys=False)

    if os.path.exists(args.out):
        os.remove(args.out)
    build_iso(args.out, files)
    print('已生成 %s (%d B)' % (args.out, os.path.getsize(args.out)))
    print('  用户: %s / 密码: %s' % (args.user, args.password))
    print('  主机名: %s  instance-id: %s' % (args.hostname, instance_id))
    print('  SSH 密码登录: %s' % ('关闭' if args.no_ssh_password else '开启'))
    if net_cfg:
        if '_raw' in net_cfg:
            print('  网络: 使用自定义 network-config 文件')
        elif 'bonds' in net_cfg:
            b = list(net_cfg['bonds'].values())[0]
            print('  网络: bond %s (%s, 成员 %s)%s'
                  % (list(net_cfg['bonds'])[0], b['parameters']['mode'],
                     ','.join(b['interfaces']),
                     ', 静态 %s' % ','.join(b['addresses']) if b['addresses'] else ''))
        else:
            e = list(net_cfg['ethernets'].values())[0]
            print('  网络: %s%s' % (list(net_cfg['ethernets'])[0],
                  ', 静态 %s' % ','.join(e['addresses']) if e['addresses'] else ''))
    else:
        print('  网络: DHCP(默认)')
    print('把它拷到 U 盘 IPA-IMAGES 分区根目录即可(deploy.yaml configdrive.file 指向文件名)')


if __name__ == '__main__':
    main()
