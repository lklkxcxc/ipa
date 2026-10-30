// make-configdrive —— NoCloud configdrive.iso 生成工具(Go 版,零运行时依赖)。
//
// 与 tools/make-configdrive.py 功能一致:生成 usb-provision 装机用的
// cloud-init NoCloud 配置盘(卷标 cidata,根目录 user-data / meta-data /
// network-config)。Go 版单文件、纯标准库(ISO9660+Rock Ridge 与 netplan
// YAML 均内置实现),`go build` 出静态二进制,目标环境无需安装任何包。
//
// 用法:
//	go build -o make-configdrive main.go
//	./make-configdrive --user enrigin --password enrigin --hostname enrigin-node
//
// 网络(可选,不传 = 第一块网卡 DHCP):
//	--ip 192.168.1.10/24 --gateway 192.168.1.1 --dns 8.8.8.8 --iface eth0
//	--bond bond0 --bond-slaves eth0,eth1 --bond-mode active-backup --ip ...
//	(任意复杂拓扑请用 Python 版的 --network-config 透传)
package main

import (
	"encoding/binary"
	"flag"
	"fmt"
	"os"
	"strings"
	"time"
)

const sectorSize = 2048

// ---------------------------------------------------------------- 参数

var (
	fUser      = flag.String("user", "", "创建的用户名(必填)")
	fPassword  = flag.String("password", "", "登录密码(必填)")
	fHostname  = flag.String("hostname", "ubuntu-server", "目标机主机名")
	fInstance  = flag.String("instance-id", "", "cloud-init instance-id(默认 usb-provision-<unix秒>)")
	fNoSSHpw   = flag.Bool("no-ssh-password", false, "关闭 SSH 密码登录")
	fOut       = flag.String("out", "configdrive.iso", "输出文件名")
	fIP        = flag.String("ip", "", "静态地址 CIDR,逗号分隔(如 10.0.0.5/24)")
	fGateway   = flag.String("gateway", "", "默认网关")
	fDNS       = flag.String("dns", "", "DNS 服务器,逗号分隔")
	fIface     = flag.String("iface", "", "静态 IP 所在网卡(默认 eth0,与 --bond 互斥)")
	fBond      = flag.String("bond", "", "bond 口名(如 bond0)")
	fBondSla   = flag.String("bond-slaves", "", "bond 成员网卡,逗号分隔(如 eth0,eth1)")
	fBondMode  = flag.String("bond-mode", "802.3ad", "bond 模式")
	fBondLacp  = flag.String("bond-lacp-rate", "", "LACP 频率 fast|slow(仅 802.3ad)")
	fMTU       = flag.Int("mtu", 0, "接口 MTU")
)

func die(format string, a ...interface{}) {
	fmt.Fprintf(os.Stderr, "错误: "+format+"\n", a...)
	os.Exit(1)
}

func splitList(s string) []string {
	var out []string
	for _, x := range strings.Split(s, ",") {
		if x = strings.TrimSpace(x); x != "" {
			out = append(out, x)
		}
	}
	return out
}

// ---------------------------------------------------------------- 配置内容

const userDataTmpl = `#cloud-config
hostname: %s
manage_etc_hosts: true
ssh_pwauth: %s
users:
  - default
  - name: %s
    sudo: ALL=(ALL) NOPASSWD:ALL
    groups: [sudo]
    shell: /bin/bash
    lock_passwd: false
    plain_text_passwd: %s
chpasswd:
  expire: false
`

const metaDataTmpl = "instance-id: %s\nlocal-hostname: %s\n"

// 网络配置(netplan v2)。iface 与 bond 二选一;空 = 不生成 network-config。
type netIface struct {
	name       string
	isBond     bool
	slaves     []string
	mode       string
	lacpRate   string
	addresses  []string
	gateway    string
	dns        []string
	mtu        int
}

func buildNetIface() *netIface {
	hasStatic := *fIP != "" || *fGateway != "" || *fDNS != ""
	if *fBond == "" && !hasStatic {
		return nil
	}
	if hasStatic && *fIP == "" {
		die("指定了 --gateway/--dns 但没有 --ip(静态配置需要地址,如 --ip 192.168.1.10/24)")
	}
	n := &netIface{
		addresses: splitList(*fIP),
		gateway:   *fGateway,
		dns:       splitList(*fDNS),
		mtu:       *fMTU,
	}
	for _, a := range n.addresses {
		if !strings.Contains(a, "/") {
			die("--ip 必须带前缀长度(CIDR),如 192.168.1.10/24,收到: %s", a)
		}
	}
	if *fBond != "" {
		if *fIface != "" {
			die("--bond 与 --iface 互斥")
		}
		if *fBondSla == "" {
			die("--bond 需要同时指定 --bond-slaves(如 eth0,eth1)")
		}
		n.name, n.isBond, n.mode, n.lacpRate = *fBond, true, *fBondMode, *fBondLacp
		n.slaves = splitList(*fBondSla)
		return n
	}
	n.name = *fIface
	if n.name == "" {
		n.name = "eth0"
	}
	return n
}

// emitYAML 输出 network-config v2(netplan 语法)。
func (n *netIface) emitYAML() string {
	var b strings.Builder
	b.WriteString("version: 2\n")
	kind := "ethernets"
	if n.isBond {
		kind = "bonds"
	}
	fmt.Fprintf(&b, "%s:\n  %s:\n", kind, n.name)
	if n.isBond {
		fmt.Fprintf(&b, "    interfaces:\n")
		for _, s := range n.slaves {
			fmt.Fprintf(&b, "      - %s\n", s)
		}
		b.WriteString("    parameters:\n")
		fmt.Fprintf(&b, "      mode: %s\n", n.mode)
		if n.lacpRate != "" && n.mode == "802.3ad" {
			fmt.Fprintf(&b, "      lacp-rate: %s\n", n.lacpRate)
		}
	}
	if len(n.addresses) > 0 {
		b.WriteString("    addresses:\n")
		for _, a := range n.addresses {
			fmt.Fprintf(&b, "      - %s\n", a)
		}
	}
	if n.gateway != "" {
		b.WriteString("    routes:\n      - to: default\n")
		fmt.Fprintf(&b, "        via: %s\n", n.gateway)
	}
	if len(n.dns) > 0 {
		b.WriteString("    nameservers:\n      addresses:\n")
		for _, d := range n.dns {
			fmt.Fprintf(&b, "        - %s\n", d)
		}
	}
	if n.mtu > 0 {
		fmt.Fprintf(&b, "    mtu: %d\n", n.mtu)
	}
	return b.String()
}

// ---------------------------------------------------------------- ISO9660 + Rock Ridge

type fileEntry struct {
	name string
	data []byte
}

func achars(s string, n int) []byte {
	b := []byte(s)
	if len(b) > n {
		b = b[:n]
	}
	for len(b) < n {
		b = append(b, ' ')
	}
	return b
}

func putBoth16(b []byte, v uint16) {
	binary.LittleEndian.PutUint16(b[:2], v)
	binary.BigEndian.PutUint16(b[2:4], v)
}

func putBoth32(b []byte, v uint32) {
	binary.LittleEndian.PutUint32(b[:4], v)
	binary.BigEndian.PutUint32(b[4:8], v)
}

func isoDate17(t time.Time) []byte {
	b := []byte(fmt.Sprintf("%04d%02d%02d%02d%02d%02d00", t.Year(), t.Month(),
		t.Day(), t.Hour(), t.Minute(), t.Second()))
	return append(b, 0) // 时区偏移 0 = UTC
}

// SUSP 记录:SP(SUSP 指示) / RR(可用记录集) / PX(POSIX 属性) / NM(长文件名)

func suspSP() []byte { return []byte{'S', 'P', 7, 1, 0xBE, 0xEF, 0} }

func suspRR(flags byte) []byte { return []byte{'R', 'R', 5, 1, flags} }

func suspPX(isDir bool) []byte {
	b := make([]byte, 44)
	b[0], b[1], b[2], b[3] = 'P', 'X', 44, 1
	var m uint32
	if isDir {
		m = 040755
	} else {
		m = 0100644
	}
	putBoth32(b[4:12], m)   // mode
	putBoth32(b[12:20], 1)  // nlink
	putBoth32(b[20:28], 0)  // uid
	putBoth32(b[28:36], 0)  // gid
	return b
}

func suspNM(name string) []byte {
	b := make([]byte, 5+len(name))
	b[0], b[1], b[2], b[3] = 'N', 'M', byte(len(b)), 1
	b[4] = 0 // flags: 完整名
	copy(b[5:], name)
	return b
}

// dirRecord 生成一条目录记录。ident 为 "."/".." 时按规范用 0x00/0x01。
func dirRecord(ident string, extent, size uint32, isDir bool, sysUse []byte, t time.Time) []byte {
	id := []byte(ident)
	if ident == "." {
		id = []byte{0}
	} else if ident == ".." {
		id = []byte{1}
	}
	recLen := 33 + len(id) + len(sysUse)
	if recLen%2 == 1 {
		recLen++
	}
	r := make([]byte, recLen)
	r[0] = byte(recLen)
	r[2] = 0
	putBoth32(r[2:10], extent)
	putBoth32(r[10:18], size)
	r[18] = byte(t.Year() - 1900)
	r[19] = byte(t.Month())
	r[20] = byte(t.Day())
	r[21] = byte(t.Hour())
	r[22] = byte(t.Minute())
	r[23] = byte(t.Second())
	r[24] = 0
	if isDir {
		r[25] = 2
	}
	putBoth16(r[28:32], 1)
	r[32] = byte(len(id))
	copy(r[33:], id)
	copy(r[33+len(id):], sysUse)
	return r
}

// padSector 补零到扇区边界。
func padSector(b []byte) []byte {
	if n := len(b) % sectorSize; n != 0 {
		b = append(b, make([]byte, sectorSize-n)...)
	}
	return b
}

func writeISO(path string, volID string, files []fileEntry, t time.Time) {
	const (
		secPVD       = 16
		secTerm      = 17
		secLPT       = 18
		secMPT       = 19
		secRootDir   = 20
		secFirstFile = 21
	)

	rootSysUse := append(append(suspSP(), suspRR(0x01)...), suspPX(true)...)
	fileSysUse := func(name string) []byte {
		return append(append(suspRR(0x09), suspPX(false)...), suspNM(name)...)
	}

	// 根目录内容:"." ".." + 各文件;记录 "."/".." 记录的偏移,拼完后回填目录大小
	var dotOff, dotdotOff int
	root := []byte{}
	dotOff = len(root)
	root = append(root, dirRecord(".", secRootDir, 0, true, rootSysUse, t)...)
	dotdotOff = len(root)
	root = append(root, dirRecord("..", secRootDir, 0, true, rootSysUse, t)...)
	extents := map[string]uint32{}
	off := uint32(secFirstFile)
	for _, f := range files {
		extents[f.name] = off
		off += uint32((len(f.data) + sectorSize - 1) / sectorSize)
	}
	for _, f := range files {
		root = append(root, dirRecord(f.name, extents[f.name],
			uint32(len(f.data)), false, fileSysUse(f.name), t)...)
	}
	root = padSector(root)
	rootSize := uint32(len(root))
	rootSectors := uint32((len(root) + sectorSize - 1) / sectorSize)
	putBoth32(root[dotOff+10:dotOff+18], rootSize)     // "." 的 size
	putBoth32(root[dotdotOff+10:dotdotOff+18], rootSize) // ".." 的 size

	// 路径表(仅根)
	lpt := make([]byte, 0, 16)
	mpt := make([]byte, 0, 16)
	{
		rec := []byte{0} // ext attr len
		e := make([]byte, 4)
		binary.LittleEndian.PutUint32(e, secRootDir)
		lpt = append(lpt, rec...)
		lpt = append(lpt, e...)
		p := make([]byte, 2)
		binary.LittleEndian.PutUint16(p, 1)
		lpt = append(lpt, p...)
		lpt = append(lpt, 1, 0) // 根名 \0
		if len(lpt)%2 == 1 {
			lpt = append(lpt, 0)
		}
		rec = []byte{0}
		e2 := make([]byte, 4)
		binary.BigEndian.PutUint32(e2, secRootDir)
		mpt = append(mpt, rec...)
		mpt = append(mpt, e2...)
		p2 := make([]byte, 2)
		binary.BigEndian.PutUint16(p2, 1)
		mpt = append(mpt, p2...)
		mpt = append(mpt, 1, 0)
		if len(mpt)%2 == 1 {
			mpt = append(mpt, 0)
		}
	}
	ptSize := uint32(len(lpt))

	// 总扇区 = 根目录之前(20) + 根目录 + 文件数据
	totalSectors := uint32(secRootDir) + rootSectors
	for _, f := range files {
		totalSectors += uint32((len(f.data) + sectorSize - 1) / sectorSize)
	}

	// PVD —— 偏移按 ECMA-119/内核 fs/isofs iso.h 结构体:
	// 8 系统ID(32) 40 卷ID(32) 72 unused(8) 80 卷空间(both32) 88 unused(32)
	// 120 卷集(both16) 124 序号(both16) 128 块大小(both16) 132 路径表大小(both32)
	// 140 L表(LE32) 144 optL(LE32) 148 M表(BE32) 152 optM(BE32) 156 根记录(34)
	// 190 卷集ID(128) 318 出版者(128) 446 制备者(128) 574 应用(128)
	// 702/739/776 版权/摘要/书目(37) 813/830/847/864 日期(17) 881 结构版本
	pvd := make([]byte, sectorSize)
	pvd[0] = 1
	copy(pvd[1:6], "CD001")
	pvd[6] = 1
	copy(pvd[8:40], achars("LINUX", 32))
	copy(pvd[40:72], achars(volID, 32))
	putBoth32(pvd[80:88], totalSectors)
	putBoth16(pvd[120:124], 1)  // volume set size
	putBoth16(pvd[124:128], 1)  // volume sequence
	putBoth16(pvd[128:132], sectorSize)
	putBoth32(pvd[132:140], ptSize)
	binary.LittleEndian.PutUint32(pvd[140:144], secLPT)
	binary.BigEndian.PutUint32(pvd[148:152], secMPT)
	// 根目录记录(PVD 内固定 34 字节,不含 SUSP)
	rootRec := dirRecord(".", secRootDir, rootSize, true, nil, t)
	copy(pvd[156:190], rootRec[:34])
	copy(pvd[190:318], achars("", 128)) // volume set id
	copy(pvd[318:446], achars("", 128)) // publisher
	copy(pvd[446:574], achars("make-configdrive", 128))
	copy(pvd[574:702], achars("make-configdrive", 128))
	copy(pvd[813:830], isoDate17(t))
	copy(pvd[830:847], isoDate17(t))
	copy(pvd[864:881], isoDate17(t))
	pvd[881] = 1 // file structure version

	term := make([]byte, sectorSize)
	term[0] = 255
	copy(term[1:6], "CD001")
	term[6] = 1

	// 组装
	out := make([]byte, 0, int(totalSectors)*sectorSize)
	out = append(out, make([]byte, secPVD*sectorSize)...)
	out = append(out, pvd...)
	out = append(out, term...)
	lpt = padSector(lpt)
	mpt = padSector(mpt)
	out = append(out, lpt...)
	out = append(out, mpt...)
	out = append(out, root...)
	for _, f := range files {
		d := padSector(append([]byte{}, f.data...))
		out = append(out, d...)
	}

	f, err := os.Create(path)
	if err != nil {
		die("写文件失败: %v", err)
	}
	if _, err := f.Write(out); err != nil {
		f.Close()
		die("写文件失败: %v", err)
	}
	if err := f.Close(); err != nil {
		die("写文件失败: %v", err)
	}
}

// ---------------------------------------------------------------- main

func main() {
	flag.Parse()
	if *fUser == "" || *fPassword == "" {
		die("--user 与 --password 必填")
	}
	if *fBond != "" && *fIface != "" {
		die("--bond 与 --iface 互斥")
	}

	instanceID := *fInstance
	if instanceID == "" {
		instanceID = fmt.Sprintf("usb-provision-%d", time.Now().Unix())
	}

	sshPwauth := "true"
	if *fNoSSHpw {
		sshPwauth = "false"
	}
	userData := fmt.Sprintf(userDataTmpl, *fHostname, sshPwauth, *fUser, *fPassword)
	metaData := fmt.Sprintf(metaDataTmpl, instanceID, *fHostname)

	files := []fileEntry{
		{"user-data", []byte(userData)},
		{"meta-data", []byte(metaData)},
	}

	n := buildNetIface()
	if n != nil {
		files = append(files, fileEntry{"network-config", []byte(n.emitYAML())})
	}

	if _, err := os.Stat(*fOut); err == nil {
		os.Remove(*fOut)
	}
	writeISO(*fOut, "cidata", files, time.Now().UTC())

	st, err := os.Stat(*fOut)
	if err != nil {
		die("%v", err)
	}
	fmt.Printf("已生成 %s (%d B)\n", *fOut, st.Size())
	fmt.Printf("  用户: %s / 密码: %s\n", *fUser, *fPassword)
	fmt.Printf("  主机名: %s  instance-id: %s\n", *fHostname, instanceID)
	fmt.Printf("  SSH 密码登录: %v\n", !*fNoSSHpw)
	switch {
	case n == nil:
		fmt.Println("  网络: DHCP(默认)")
	case n.isBond:
		fmt.Printf("  网络: bond %s (%s, 成员 %s)", n.name, n.mode, strings.Join(n.slaves, ","))
		if len(n.addresses) > 0 {
			fmt.Printf(", 静态 %s", strings.Join(n.addresses, ","))
		}
		fmt.Println()
	default:
		fmt.Printf("  网络: %s", n.name)
		if len(n.addresses) > 0 {
			fmt.Printf(", 静态 %s", strings.Join(n.addresses, ","))
		}
		fmt.Println()
	}
	fmt.Println("把它拷到 U 盘 IPA-IMAGES 分区根目录即可(deploy.yaml configdrive.file 指向文件名)")
}
