#!/usr/bin/env python3
"""usb-provision 本地编排器。

运行在 IPA ramdisk 内(由 usb-provision.service 拉起),驱动本机完成自动化装机:

1. 等待本机 IPA REST API (127.0.0.1:9999) 就绪
2. 挂载 U 盘 CONFIG-2 / IPA-IMAGES 分区,读取 deploy.yaml
3. 在 127.0.0.1:8484 起本地 HTTP 服务镜像目录(IPA 镜像下载仅支持 http/https)
4. 依次调用 IPA 命令:擦盘(可选) -> standby.prepare_image -> image.install_bootloader
5. 结果写回 CONFIG-2 分区,按配置关机

仅依赖 Python 标准库 + PyYAML(element 安装时装入)。
"""

import base64
import gzip
import http.server
import json
import logging
import os
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

import yaml

LOG = logging.getLogger('usb-provision')

# ---- 常量(PoC 约定,后续参数化) ----
# 注意:blkid 卷标匹配区分大小写,IPA 原生按小写 config-2 查找
CONFIG_LABEL = 'config-2'      # 配置分区卷标(IPA 原生 config-2 约定)
IMAGES_LABEL = 'ipa-images'    # 镜像分区卷标
DEPLOY_CONFIG = 'deploy.yaml'
IPA_API = 'http://127.0.0.1:9999'
IPA_API_TIMEOUT = 600          # 等 IPA API 就绪的最长秒数
HTTP_PORT = 8484               # 本地镜像 HTTP 服务端口
MOUNT_BASE = '/run/usb-provision'
# 新版 IPA 对命令端点强制 agent_token 鉴权(app.py require_agent_token_for_command),
# standalone 模式下没有 Ironic 下发 token,必须预共享:IPA 侧由 config-2 的
# etc/ironic-python-agent.d/agent.conf 提供同一个值,两边必须一致。
AGENT_TOKEN = 'usb-provision-poc-0123456789abcdef0123456789abcdef'

IMAGE_RE = re.compile(r'image \((?P<id>[^)]*)\) written to device (?P<device>\S+)'
                      r'(?: root_uuid=(?P<root_uuid>[0-9a-fA-F-]+))?',
                      re.IGNORECASE)
EFI_UUID_RE = re.compile(r'efi_system_partition_uuid=(?P<efi>[0-9a-fA-F-]+)', re.IGNORECASE)


def run(cmd, **kwargs):
    """执行 shell 命令并返回 stdout,失败抛异常。"""
    LOG.debug('$ %s', cmd)
    return subprocess.check_output(
        cmd, shell=isinstance(cmd, str), text=True,
        stderr=subprocess.STDOUT, **kwargs).strip()


def read_text(path):
    """读文本文件,容忍 Windows 产物:UTF-8 BOM / UTF-16(LE/BE) / UTF-8。

    U 盘里的文件(deploy.yaml、sha256)常在 Windows 下生成,
    PowerShell 5.1 的 `>` 重定向默认 UTF-16,Python 默认按 UTF-8 读会报
    "'utf-8' codec can't decode byte 0xff"。
    """
    with open(path, 'rb') as f:
        data = f.read()
    if data[:2] in (b'\xff\xfe', b'\xfe\xff'):
        return data.decode('utf-16')
    return data.decode('utf-8-sig', errors='replace')


# ---------------------------------------------------------------- 分区挂载

def find_partition(label):
    """按卷标查找分区设备路径,找不到返回 None。"""
    for cmd in ('blkid -L %s' % label, 'findfs LABEL=%s' % label):
        try:
            dev = run(cmd)
            if dev:
                return dev
        except subprocess.CalledProcessError:
            continue
    return None


def mount_partition(label, mountpoint, rw=False):
    dev = find_partition(label)
    if not dev:
        raise RuntimeError('找不到卷标为 %s 的分区' % label)
    os.makedirs(mountpoint, exist_ok=True)
    opts = 'rw' if rw else 'ro'
    run('mount -o %s %s %s' % (opts, dev, mountpoint))
    LOG.info('已挂载 %s -> %s (%s)', dev, mountpoint, label)
    return dev


# ---------------------------------------------------------------- 本地镜像 HTTP 服务

def start_image_http_server(images_dir):
    """在 127.0.0.1:HTTP_PORT 服务镜像目录,返回 (server, thread)。"""
    handler = http.server.SimpleHTTPRequestHandler
    # ThreadingHTTPServer 需要 bind 双参数目录(3.7+ 支持 directory kwarg)
    server = http.server.ThreadingHTTPServer(
        ('127.0.0.1', HTTP_PORT),
        lambda *args, **kw: handler(*args, directory=images_dir, **kw))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    LOG.info('镜像 HTTP 服务: http://127.0.0.1:%d/ <- %s', HTTP_PORT, images_dir)
    return server, thread


# ---------------------------------------------------------------- 目标盘选择

TARGET_HINT_FILE = '/run/usb-provision/target-device'


def list_disks():
    """列出磁盘,返回 [(name, size_bytes, removable)],name 形如 sda。"""
    out = run('lsblk -d -b -n -o NAME,SIZE,RM,TYPE -e 7,11')
    disks = []
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 4 and parts[3] == 'disk':
            disks.append((parts[0], int(parts[1]), parts[2] == '1'))
    return disks


def parent_disk(dev):
    """分区设备 -> 所在磁盘名(sdb3 -> sdb);磁盘名原样返回。"""
    try:
        out = run('lsblk -no PKNAME %s' % dev)
        if out:
            return out.splitlines()[0].strip()
    except subprocess.CalledProcessError:
        pass
    return os.path.basename(dev)


def resolve_target_device(cfg):
    """确定装机目标盘,返回设备名(如 sda)。

    standalone 的 IPA 选盘只取"最大的盘"(guess_root_disk),既不排除
    可移动盘也无法识别引导介质(root 是 ramdisk)—— U 盘会被当成装机盘。
    因此由编排器选盘:优先 deploy.yaml target.device 显式指定,
    否则排除 U 盘(config-2 / ipa-images 所在盘)后取最大非可移动盘,
    结果写入提示文件,IPA 侧(hardware.py 补丁)优先读取。
    """
    explicit = (cfg.get('target') or {}).get('device')
    if explicit:
        LOG.info('目标盘(deploy.yaml 指定): %s', explicit)
        return os.path.basename(explicit)

    excluded = set()
    for label in (CONFIG_LABEL, IMAGES_LABEL):
        dev = find_partition(label)
        if dev:
            excluded.add(parent_disk(dev))

    disks = [d for d in list_disks() if d[0] not in excluded]
    fixed = [d for d in disks if not d[2]]   # 非可移动盘优先
    pool = fixed or disks
    if not pool:
        raise RuntimeError('除 U 盘(%s)外找不到可用磁盘,拒绝自动选盘'
                           % ','.join(sorted(excluded)))
    pool.sort(key=lambda d: d[1])
    name = pool[-1][0]
    LOG.info('目标盘(自动选择,已排除 U 盘 %s): /dev/%s (%.1fG%s)',
             ','.join(sorted(excluded)), name, pool[-1][1] / 2**30,
             ', 可移动' if pool[-1][2] else '')
    return name


# ---------------------------------------------------------------- IPA REST 客户端

def api_post(command, params, wait=True):
    """POST /v1/commands 调用 IPA 命令,等待完成并返回结果 dict。"""
    url = IPA_API + '/v1/commands/?agent_token=' + AGENT_TOKEN
    if wait:
        url += '&wait=true'
    body = json.dumps({'name': command, 'params': params}).encode()
    req = urllib.request.Request(url, data=body,
                                 headers={'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=3600) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors='replace')
        raise RuntimeError('IPA 命令 %s 失败: HTTP %s: %s' % (command, e.code, detail))


def api_status():
    try:
        with urllib.request.urlopen(IPA_API + '/v1/', timeout=5) as resp:
            return resp.code == 200
    except Exception:
        return False


def wait_for_agent(timeout=IPA_API_TIMEOUT):
    LOG.info('等待 IPA API 就绪 (%s) ...', IPA_API)
    deadline = time.time() + timeout
    while time.time() < deadline:
        if api_status():
            LOG.info('IPA API 就绪')
            return
        time.sleep(3)
    raise RuntimeError('等待 IPA API 超时 (%ds)' % timeout)


# ---------------------------------------------------------------- 镜像与部署

HEX64_RE = re.compile(r'\b[0-9a-fA-F]{64}\b')


def read_sha256(path):
    """读取校验文件,返回 sha256 哈希。

    兼容两种来源:
    - sha256sum 格式(`哈希  文件名`)
    - PowerShell Get-FileHash 表格输出(Algorithm/Hash/Path 三列)
    编码经 read_text() 容错(Windows 常见 UTF-16 BOM / CRLF)。
    """
    m = HEX64_RE.search(read_text(path))
    if not m:
        raise RuntimeError('校验文件里找不到 sha256 哈希: %s' % path)
    return m.group(0).lower()


def build_image_info(cfg, images_dir):
    img = cfg['image']
    image_file = os.path.join(images_dir, img['file'])
    if not os.path.isfile(image_file):
        raise RuntimeError('镜像文件不存在: %s' % image_file)

    checksum_file = img.get('checksum_file') or img['file'] + '.sha256'
    checksum_path = os.path.join(images_dir, checksum_file)
    if not os.path.isfile(checksum_path):
        raise RuntimeError('校验文件不存在: %s' % checksum_path)

    info = {
        'id': img['file'],
        'urls': ['http://127.0.0.1:%d/%s' % (HTTP_PORT, img['file'])],
        'checksum': read_sha256(checksum_path),
        'disk_format': img.get('disk_format', 'qcow2'),
    }
    if img.get('stream_raw') and info['disk_format'] == 'raw':
        info['stream_raw_images'] = True
    if img.get('image_type'):
        info['image_type'] = img['image_type']
    LOG.info('image_info: %s', json.dumps(info, indent=2))
    return info


def resolve_configdrive(cfg, images_dir):
    """configdrive: 支持 IPA-IMAGES 里的 ISO 文件或现成的 gzip+base64 内容。

    IPA 侧(partition_utils.write_configdrive)对内容先做 base64 解码再
    gunzip —— URL 下载的裸 ISO 会因"解码成功但不是 gzip"而报
    "Not a gzipped file"。因此文件一律读入内存做 gzip+base64 后传递。
    """
    cd = cfg.get('configdrive') or {}
    val = cd.get('file') or cd.get('base64')
    if not val:
        return None
    if cd.get('base64'):
        # 原样传递 base64 内容字符串(须为 gzip+base64,IPA 侧自行解码)
        return val
    path = os.path.join(images_dir, val)
    if not os.path.isfile(path):
        raise RuntimeError('configdrive 文件不存在: %s' % path)
    with open(path, 'rb') as f:
        raw = f.read()
    encoded = base64.b64encode(gzip.compress(raw)).decode('ascii')
    LOG.info('configdrive 已编码: %s (%d B -> %d B base64)',
             val, len(raw), len(encoded))
    return encoded


def erase_devices():
    """通过 clean step 机制执行 erase_devices(ATA secure erase 优先)。"""
    step = {'step': 'erase_devices', 'priority': 10, 'interface': None,
            'reboot': False, 'abortable': True}
    LOG.info('执行擦盘 erase_devices (可能耗时较长)...')
    return api_post('clean.execute_clean_step',
                    {'step': step, 'node': {}, 'ports': []})


def deploy(cfg, images_dir):
    info = build_image_info(cfg, images_dir)
    configdrive = resolve_configdrive(cfg, images_dir)

    if cfg.get('erase', {}).get('enabled'):
        erase_devices()

    LOG.info('调用 standby.prepare_image 写盘 ...')
    res = api_post('standby.prepare_image',
                   {'image_info': info, 'configdrive': configdrive})
    LOG.info('prepare_image 完成: %s', res.get('command_result'))
    if res.get('command_status') != 'SUCCEEDED':
        raise RuntimeError('prepare_image 状态异常: %s: %s'
                           % (res.get('command_status'),
                              str(res.get('command_result') or '')[:800]))

    if info.get('image_type') == 'partition':
        # 分区镜像需要装 bootloader(chroot 目标镜像执行 grub-install)
        msg = str(res.get('command_result') or '')
        m = IMAGE_RE.search(msg)
        if not m:
            raise RuntimeError('无法从 prepare_image 结果解析 root_uuid: %s' % msg)
        params = {'root_uuid': m.group('root_uuid'),
                  'target_boot_mode': 'bios'}
        efi = EFI_UUID_RE.search(msg)
        if efi:
            params['efi_system_part_uuid'] = efi.group('efi')
            params['target_boot_mode'] = 'uefi'
        LOG.info('调用 image.install_bootloader: %s', params)
        res = api_post('image.install_bootloader', params)
        LOG.info('install_bootloader 完成: %s', res.get('command_result'))
        if res.get('command_status') != 'SUCCEEDED':
            raise RuntimeError('install_bootloader 状态异常: %s: %s'
                               % (res.get('command_status'),
                                  str(res.get('command_result') or '')[:800]))
    else:
        LOG.info('整盘镜像自带 bootloader,跳过 install_bootloader')

    return {'status': 'SUCCEEDED', 'image': info['id'],
            'root_uuid': m.group('root_uuid') if info.get('image_type') == 'partition' else None}


# ---------------------------------------------------------------- 结果写回

def write_result(config_dev, result):
    """把结果写回 CONFIG-2 分区(重新以 rw 挂载)。"""
    try:
        run('umount %s' % os.path.join(MOUNT_BASE, 'config'))
        mount_partition(CONFIG_LABEL, os.path.join(MOUNT_BASE, 'config'), rw=True)
        out = os.path.join(MOUNT_BASE, 'config', 'deploy-result.json')
        with open(out, 'w') as f:
            json.dump(result, f, indent=2)
        run('umount %s' % os.path.join(MOUNT_BASE, 'config'))
        run('sync')
        LOG.info('结果已写回 CONFIG-2: %s', out)
    except Exception as e:  # 写回失败不影响装机结果
        LOG.warning('结果写回 CONFIG-2 失败: %s', e)


# ---------------------------------------------------------------- main

def main():
    logging.basicConfig(
        level=logging.DEBUG,
        format='%(asctime)s %(levelname)s %(name)s: %(message)s')

    result = {'status': 'FAILED', 'error': None}
    server = None
    config_dev = None
    try:
        wait_for_agent()

        config_dev = mount_partition(CONFIG_LABEL, os.path.join(MOUNT_BASE, 'config'))
        mount_partition(IMAGES_LABEL, os.path.join(MOUNT_BASE, 'images'))

        config_path = os.path.join(MOUNT_BASE, 'config', DEPLOY_CONFIG)
        cfg = yaml.safe_load(read_text(config_path))
        LOG.info('配置加载完成: %s', json.dumps(cfg, ensure_ascii=False, indent=2))

        images_dir = os.path.join(MOUNT_BASE, 'images')
        server, _t = start_image_http_server(images_dir)

        target_dev = resolve_target_device(cfg)
        os.makedirs(MOUNT_BASE, exist_ok=True)
        with open(TARGET_HINT_FILE, 'w') as f:
            f.write(target_dev)
        LOG.info('目标盘提示已写入 %s: %s (IPA 侧优先生效)', TARGET_HINT_FILE,
                 target_dev)

        result = deploy(cfg, images_dir)
        result['status'] = 'SUCCEEDED'
    except Exception as e:
        LOG.exception('装机失败: %s', e)
        result = {'status': 'FAILED', 'error': str(e)}
    finally:
        if server:
            server.shutdown()
        if config_dev:
            write_result(config_dev, result)

    if result['status'] == 'SUCCEEDED':
        LOG.info('========== 装机成功,可拔出 U 盘 ==========')
        if cfg.get('post', {}).get('shutdown', True):
            run('poweroff || shutdown -h now')
    else:
        LOG.error('========== 装机失败,结果: %s ==========', result)
        sys.exit(1)


if __name__ == '__main__':
    main()
