# make-configdrive 使用说明(Python 版 / Go 版)

`tools/` 下有两个等价的 configdrive.iso 生成工具:

| 工具 | 依赖 | 适用场景 |
|---|---|---|
| `tools/make-configdrive.py` | python3 + genisoimage;**配网络时**需 PyYAML | 支持 `--network-config` 透传任意 netplan 拓扑(VLAN、多网卡等) |
| `tools/go/make-configdrive/`(Go) | **零依赖**(静态二进制) | 无 Python/ISO 工具环境;拷到哪都能跑 |

两个工具参数一致、输出等价,任选其一。工具用于生成 usb-provision 装机用的
NoCloud 格式 `configdrive.iso` —— 即装完系统后 cloud-init 读取的"初始化配置盘":
**账号密码、SSH 登录方式、主机名、网络(含 bond)都在这里配置**。

生成的 ISO 拷到 U 盘 `ipa-images` 分区根目录,`deploy.yaml` 里
`configdrive.file: configdrive.iso` 指向它即可。改任何配置只需重新生成 ISO
再拷入,**无需重做 U 盘、无需重装**。

## Go 版:构建与直接使用

```bash
# 已有静态二进制时直接用(lk 上已装到 /usr/local/bin/make-configdrive-go)
make-configdrive-go --user enrigin --password enrigin --hostname enrigin-node

# 或从源码构建(任意有 Go ≥1.13 的机器,产出静态二进制,零运行时依赖)
cd tools/go/make-configdrive
go build -o make-configdrive main.go
```

单文件 `main.go` 内置 ISO9660+Rock Ridge 写入器与 netplan YAML 序列化,
不调 genisoimage、不依赖 PyYAML。已验证:loop 挂载、xorriso 严格解析、
与 Python 版输出逐字节对比(内容一致)。

> Go 版不含 `--network-config` 文件透传;VLAN 等复杂拓扑请用 Python 版。

## 环境要求(Python 版)

- python3(标准库);**配置网络时**额外需要 PyYAML
  (`pip install pyyaml` 或 Ubuntu `apt install python3-yaml`)
- ISO 生成工具:genisoimage / mkisofs / xorriso 任一
  (Ubuntu: `apt install genisoimage`)

## 一、基础用法:账号密码

```bash
python3 tools/make-configdrive.py --user enrigin --password enrigin --hostname enrigin-node
# Go 版等价:
make-configdrive-go --user enrigin --password enrigin --hostname enrigin-node
```

- 创建用户 `enrigin`(免密 sudo),密码 `enrigin`
- 开启 SSH 密码登录
- 主机名 `enrigin-node`
- 输出 `configdrive.iso`(当前目录)

不想开 SSH 密码登录(仅密钥):加 `--no-ssh-password`。

## 二、网络配置

**不传任何网络参数 = cloud-init 默认 DHCP**(通常够用)。

### 静态 IP(单网卡)

```bash
python3 tools/make-configdrive.py --user enrigin --password enrigin \
    --ip 192.168.1.10/24 --gateway 192.168.1.1 \
    --dns 114.114.114.114,8.8.8.8 --iface eth0
```

- `--ip` 必须带前缀长度(CIDR);多地址逗号分隔,如 `--ip 10.0.0.5/24,10.0.0.6/24`
- `--iface` 缺省 `eth0`(目标机网卡名以实际为准,可用 `ip a` 在目标机确认)

### bond

```bash
# 802.3ad(LACP,需要交换机端配对应聚合口)
python3 tools/make-configdrive.py --user enrigin --password enrigin \
    --bond bond0 --bond-slaves eth0,eth1 --bond-mode 802.3ad --bond-lacp-rate fast \
    --ip 10.0.0.5/24 --gateway 10.0.0.1 --dns 8.8.8.8

# active-backup(主备,无需交换机配合,最常用)
python3 tools/make-configdrive.py --user enrigin --password enrigin \
    --bond bond0 --bond-slaves eth0,eth1 --bond-mode active-backup \
    --ip 10.0.0.5/24 --gateway 10.0.0.1
```

- 静态地址 / 网关 / DNS 配在 **bond 口**上(不要同时给 `--iface`,两者互斥)
- bond 模式速查:

| `--bond-mode` | 场景 |
|---|---|
| `active-backup` | 主备容错,交换机无需配置,**最通用** |
| `802.3ad` | LACP 动态聚合,需交换机配置,带宽叠加 |
| `balance-xor` | 静态聚合,需交换机配合 |
| `balance-rr` | 轮询,简单叠加带宽(交换机不聚合时可能乱序) |

### 任意复杂拓扑:netplan 文件透传

VLAN、多网卡多网段、策略路由等超出现有参数的,直接写 netplan(v2) YAML:

```yaml
# my-network.yaml 示例:VLAN + bond
version: 2
bonds:
  bond0:
    interfaces: [eth0, eth1]
    parameters: {mode: 802.3ad}
vlans:
  bond0.100:
    id: 100
    link: bond0
    addresses: [192.168.100.5/24]
    routes: [{to: default, via: 192.168.100.1}]
```

```bash
python3 tools/make-configdrive.py --user enrigin --password enrigin \
    --network-config my-network.yaml
```

`--network-config` 优先级最高,给了它就忽略 `--ip/--bond/...`。

## 三、全部参数

| 参数 | 默认 | 说明 |
|---|---|---|
| `--user` | 必填 | 创建的用户名(免密 sudo) |
| `--password` | 必填 | 登录密码(明文) |
| `--hostname` | `ubuntu-server` | 目标机主机名 |
| `--instance-id` | `usb-provision-<时间戳>` | cloud-init 实例 ID |
| `--no-ssh-password` | 关闭 | 加上后关闭 SSH 密码登录 |
| `--out` | `configdrive.iso` | 输出文件名 |
| `--ip` | 无 | 静态地址(CIDR),逗号分隔多个 |
| `--gateway` | 无 | 默认网关 |
| `--dns` | 无 | DNS,逗号分隔 |
| `--iface` | `eth0` | 静态 IP 所在网卡 |
| `--bond` | 无 | bond 口名(与 `--iface` 互斥) |
| `--bond-slaves` | 无 | bond 成员,逗号分隔 |
| `--bond-mode` | `802.3ad` | bond 模式 |
| `--bond-lacp-rate` | 无 | LACP 频率 fast/slow(仅 802.3ad) |
| `--mtu` | 无 | 接口 MTU |
| `--network-config` | 无 | netplan(v2) YAML 透传,优先级最高(仅 Python 版) |

## 四、部署到 U 盘

1. 生成:`python3 tools/make-configdrive.py ...` 或 `make-configdrive-go ... --out configdrive.iso`
2. 拷入 U 盘 `ipa-images` 分区根目录(Windows 下直接双击该分区拖文件即可)
3. `deploy.yaml` 确认 `configdrive.file: configdrive.iso`(文件名一致则不用改)
4. 插目标机装机,装完系统即带上述账号与网络配置

## 注意事项

- **instance-id 机制**:cloud-init 只在"新实例"上执行一次。本工具每次生成默认用
  新时间戳作 instance-id,所以重新生成 + 重装 = 配置一定生效。若手动固定
  `--instance-id`,重装后 cloud-init 会跳过已执行过的模块(改密码可能不生效)。
- **network-config 生效时机**:cloud-init 首启时应用并落到 netplan 配置;
  IP 变化需重新装机(或改 instance-id)。
- **bond 前置条件**:`802.3ad` 需要交换机侧配好聚合口,否则协商失败链路不通;
  拿不准就用 `active-backup`。
- ISO 卷标固定 `cidata`,文件在卷根:`user-data` / `meta-data` / `network-config`,
  这是 cloud-init NoCloud 的约定,不要改名。
- IPA 侧的 gzip+base64 编码由编排器自动完成,ISO 原样拷入即可。
