#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
discover.py —— 精简版节点注册/用户同步/流量上报脚本（仿项目里的 cron.py）

把本机（GitHub Actions runner）上运行的 xray VLESS-WS + cloudflared 隧道节点接入网关：
  - 注册节点（生成 qrcode / clash 上报，自动取 IP/国旗）
  - 同步启用用户到 xray（用 HandlerService 的 adu/rmu 动态增删，**免重启**）
  - 上报每用户流量增量（StatsService statsquery，读即清零）

网关只用一个基址变量 GATEWAY_URL（含你自己的路径前缀），代码按通用后缀拼出不同功能：
  - {GATEWAY_URL}/discover  注册
  - {GATEWAY_URL}/users     拉启用用户
  - {GATEWAY_URL}/traffic   上报流量

环境变量：
    GATEWAY_URL      必填  网关基址（含私有路径前缀，整段放 GitHub Secrets，避免公开仓库泄露）
    CF_HOSTNAME      注册需要  Cloudflare 隧道公网域名（= 客户端 server / SNI / Host 头）
    CF_TUNNEL_TOKEN  必填  cloudflared 隧道 token，作为派生 VLESS uuid 的唯一种子
    PROXY_NAME       可选  节点名称（国旗自动前缀），默认 "Github Actions"
    ORDER            可选  排序权重，默认 200
    AI               可选  true/false，默认按归属地自动判断（CN/HK/MO=false，其他=true）
    NODE_ID          可选  节点 id（注册 uuid / 流量 nodeId），默认用派生出的 VLESS uuid
    XRAY_BIN         可选  xray 可执行路径，默认 ./xray
    XRAY_PORT        可选  xray 本地监听端口，默认 8080

用户【不】落盘：xray.json 只含节点自身 uuid，用户由每小时任务经 xray API(adu/rmu) 动态增删，
只活在运行中的 xray 内存里，绝不写进磁盘配置或日志（避免真实 uuid 泄露到公开 Actions 日志）。

用法：
    python3 discover.py                          注册 + 用户同步(免重启) + 流量上报（每小时跑）
    python3 discover.py --print-uuid             仅打印派生出的 VLESS uuid
    python3 discover.py --write-xray-config PATH 生成基础 xray.json（仅节点 uuid + stats/api，无用户）
    python3 discover.py --report-traffic         仅上报一次流量（接力交接前 flush 用）
"""

import os
import sys
import json
import uuid as uuidlib
import base64
import tempfile
import subprocess
from collections import OrderedDict
from urllib.request import Request, urlopen

__version__ = 'gha-relay-1.0.0'

# WebSocket 路径（固定）。必须与 xray.json 里 wsSettings.path 保持一致。
WS_PATH = '/api/v3/runner/heartbeat'
DEFAULT_NAME = 'Github Actions'
DEFAULT_ORDER = 200
DEFAULT_XRAY_PORT = 8080
XRAY_VLESS_TAG = 'vless-in'
XRAY_API_ADDR = '127.0.0.1:10085'   # dokodemo-door api inbound 监听地址
# 网关通用功能后缀（私有路径前缀由 GATEWAY_URL 提供，不写死在代码里）
DISCOVER_PATH = '/discover'
USERS_PATH = '/users'
TRAFFIC_PATH = '/traffic'
# 派生 uuid 用的固定命名空间（项目私有，随意但固定即可）
UUID_NAMESPACE = uuidlib.UUID('9f3a7c1e-5b2d-4e88-a1c6-3d0f2b6e7a90')


# ---------------------------------------------------------------- 基础工具

def gateway_base():
    return os.environ.get('GATEWAY_URL', '').strip().rstrip('/')


def discover_url():
    return gateway_base() + DISCOVER_PATH


def users_url():
    return gateway_base() + USERS_PATH


def traffic_url():
    return gateway_base() + TRAFFIC_PATH


def xray_bin():
    return os.environ.get('XRAY_BIN', './xray')


def xray_port():
    try:
        return int(os.environ.get('XRAY_PORT', str(DEFAULT_XRAY_PORT)))
    except ValueError:
        return DEFAULT_XRAY_PORT


def run_command(cmd):
    """执行 shell 命令，返回 (rc, stdout_str, stderr_str)"""
    try:
        p = subprocess.Popen(cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        out, err = p.communicate()
        return p.returncode, _to_str(out), _to_str(err)
    except Exception as e:
        return -1, '', str(e)


def _to_str(b):
    return b.decode('utf-8', 'replace') if isinstance(b, bytes) else (b or '')


def resolve_uuid():
    """由 CF_TUNNEL_TOKEN 经 uuid5 确定性派生 VLESS uuid（唯一来源）。
    同一 token 永远派生同一 uuid，使 xray.json 与注册/同步取值一致。返回 (uuid, error)。
    """
    token = os.environ.get('CF_TUNNEL_TOKEN', '').strip()
    if not token:
        return None, 'CF_TUNNEL_TOKEN 未设置，无法派生 uuid'
    return str(uuidlib.uuid5(UUID_NAMESPACE, token)), None


def _mask_uuid(u):
    """日志脱敏：只露前 8 位，其余隐去，避免真实 uuid 进公开 Actions 日志"""
    return (u[:8] + '…') if u and len(u) > 8 else '***'


def country_code_to_flag(code):
    try:
        return u''.join(chr(0x1F1E6 + ord(c) - ord('A')) for c in code.upper())
    except Exception:
        return u''


def url_encode(s):
    """UTF-8 percent-encoding（与 cron.py 一致，保留 -_.~）"""
    out = []
    for ch in s:
        if (ch.isalnum() and ord(ch) < 128) or ch in '-_.~':
            out.append(ch)
        else:
            for b in ch.encode('utf-8'):
                out.append('%%%02X' % b)
    return ''.join(out)


def b64url(data):
    if isinstance(data, str):
        data = data.encode('utf-8')
    return base64.b64encode(data).decode('ascii').replace('+', '-').replace('/', '_').rstrip('=')


def get_ip_info():
    """ip-api.com 查询 runner 公网 IP + 地理信息（失败抛异常）"""
    url = 'http://ip-api.com/json/?fields=query,countryCode,regionName,city'
    req = Request(url, headers={'User-Agent': 'curl/7.64.1'})
    obj = json.loads(urlopen(req, timeout=10).read().decode('utf-8'))
    return {
        'ip':          obj.get('query', ''),
        'countryCode': obj.get('countryCode', ''),
        'city':        obj.get('city', ''),
        'regionName':  obj.get('regionName', ''),
    }


# ---------------------------------------------------------------- 用户 / 配置

def fetch_users(url):
    """拉启用用户 [{userId,uuid}]（仿 cron.py.fetch_users）。失败返回 None（绝不据此清空）。"""
    try:
        req = Request(url, headers={'User-Agent': 'proxy-relay-discover/%s' % __version__})
        obj = json.loads(urlopen(req, timeout=15).read().decode('utf-8'))
    except Exception as e:
        print('WARN: 拉取用户失败: %s' % e, file=sys.stderr)
        return None
    result, seen = [], set()
    for u in (obj.get('users') or []):
        uid, uu = u.get('userId'), u.get('uuid')
        if uid and uu and u.get('enabled') and uu not in seen:
            seen.add(uu)
            result.append({'userId': uid, 'uuid': uu})
    return result


def _vless_inbound(uuid, port, clients=None):
    """vless-ws inbound（含 stats/adu 所需的 tag）。

    clients 默认只放节点自身那一个派生 uuid——用户【不】写进落盘配置，
    避免真实 uuid 出现在公开的 Actions 日志里；用户运行时经 xray API 动态添加。
    """
    if clients is None:
        clients = [OrderedDict([('id', uuid)])]   # 仅节点自身（无 email，不计流量）
    return OrderedDict([
        ('listen', '127.0.0.1'),
        ('port', port),
        ('protocol', 'vless'),
        ('tag', XRAY_VLESS_TAG),
        ('settings', OrderedDict([('clients', clients), ('decryption', 'none')])),
        ('streamSettings', OrderedDict([
            ('network', 'ws'),
            ('wsSettings', OrderedDict([('path', WS_PATH)])),
        ])),
    ])


def build_xray_config(uuid, port):
    """落盘 xray 配置：vless-ws（仅节点自身 uuid）+ StatsService/HandlerService。

    用户【不】落盘——运行中的 xray 从一开始就支持 statsquery(读流量) 与 adu/rmu，
    用户由每小时任务经 `xray api adu` 动态加入（只在 xray 内存里，不落盘、不进日志）。
    """
    api_host, _, api_port = XRAY_API_ADDR.partition(':')
    return OrderedDict([
        ('log', OrderedDict([('loglevel', 'warning')])),
        ('stats', OrderedDict()),
        ('policy', OrderedDict([('levels', OrderedDict([
            ('0', OrderedDict([('statsUserUplink', True), ('statsUserDownlink', True)])),
        ]))])),
        ('api', OrderedDict([('tag', 'api'), ('services', ['StatsService', 'HandlerService'])])),
        ('inbounds', [
            _vless_inbound(uuid, port),
            OrderedDict([
                ('tag', 'api'),
                ('listen', api_host),
                ('port', int(api_port)),
                ('protocol', 'dokodemo-door'),
                ('settings', OrderedDict([('address', api_host)])),
            ]),
        ]),
        ('routing', OrderedDict([('rules', [
            OrderedDict([('type', 'field'), ('inboundTag', ['api']), ('outboundTag', 'api')]),
        ])])),
        ('outbounds', [OrderedDict([('protocol', 'freedom')])]),
    ])


def write_xray_config(path, uuid):
    with open(path, 'w') as f:
        json.dump(build_xray_config(uuid, xray_port()), f, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------- xray API（免重启同步 / 流量）

def xray_inbound_user_emails(tag):
    """`xray api inbounduser -tag` 取运行中 inbound 的用户 email 集合；失败返回 None。"""
    rc, out, err = run_command('%s api inbounduser --server=%s -tag=%s' % (xray_bin(), XRAY_API_ADDR, tag))
    if rc != 0:
        print('WARN: xray inbounduser 失败 (tag=%s rc=%s): %s' % (tag, rc, err.strip()), file=sys.stderr)
        return None
    try:
        d = json.loads(out) if out.strip() else {}
    except Exception:
        return None
    return set(u.get('email') for u in (d.get('users') or []) if u.get('email'))


def sync_xray_users_via_api(uuid, users):
    """以运行实例 inbounduser 为真值与启用用户 diff，adu/rmu 动态增删（免重启）。

    用户只存在于运行中的 xray 内存里——【不】回写落盘配置，避免真实 uuid 进公开日志。
    代价：xray 若 crash 重启，用户要等下一轮同步补回（最多 1h），节点 6h 退出，可接受。
    """
    tag = XRAY_VLESS_TAG
    live = xray_inbound_user_emails(tag)
    if live is None:
        print('WARN: 取 live 用户失败，跳过本轮用户同步', file=sys.stderr)
        return
    live_u = set(e for e in live if e and e.startswith('u_'))
    desired = OrderedDict((u['userId'], u['uuid']) for u in users)
    to_add = [uid for uid in desired if uid not in live_u]
    to_remove = [e for e in live_u if e not in desired]

    if to_remove:
        rc, _, err = run_command('%s api rmu --server=%s -tag=%s %s' % (
            xray_bin(), XRAY_API_ADDR, tag, ' '.join(to_remove)))
        if rc != 0:
            print('WARN: xray rmu 失败 (rc=%s): %s' % (rc, err.strip()), file=sys.stderr)

    if to_add:
        add_clients = [OrderedDict([('id', desired[uid]), ('email', uid)]) for uid in to_add]
        # adu 会 build inbound 提取用户，故须可 build：用节点 inbound + 仅待加用户作 clients
        pi = _vless_inbound(uuid, xray_port(), clients=add_clients)
        payload = {'inbounds': [pi]}
        fd, tmp = tempfile.mkstemp(prefix='.xray-adu-', suffix='.json')
        try:
            with os.fdopen(fd, 'w') as f:
                json.dump(payload, f)
            rc, out, err = run_command('%s api adu --server=%s %s' % (xray_bin(), XRAY_API_ADDR, tmp))
            if rc != 0 or ('Added %d user' % len(add_clients)) not in (out or ''):
                print('WARN: xray adu 可能失败 (rc=%s): %s' % (rc, (out + err).strip()), file=sys.stderr)
        finally:
            try:
                os.remove(tmp)
            except OSError:
                pass

    # 不回写落盘配置：用户只活在运行中的 xray 里，避免真实 uuid 进公开日志
    print('用户同步: live=%d desired=%d +%d -%d' % (len(live_u), len(desired), len(to_add), len(to_remove)))


def read_xray_user_traffic():
    """`xray api statsquery -reset 'user>>>'`（读即增量）→ {userId:{up,down}}"""
    rc, out, err = run_command("%s api statsquery --server=%s -reset 'user>>>'" % (xray_bin(), XRAY_API_ADDR))
    if rc != 0:
        print('WARN: xray statsquery 失败 (rc=%s): %s' % (rc, err.strip()), file=sys.stderr)
        return {}
    try:
        obj = json.loads(out) if out.strip() else {}
    except Exception as e:
        print('WARN: 解析 stats 失败: %s' % e, file=sys.stderr)
        return {}
    result = {}
    for stat in (obj.get('stat') or []):
        name = stat.get('name', '')
        try:
            value = int(stat.get('value', 0) or 0)
        except (TypeError, ValueError):
            continue
        parts = name.split('>>>')   # user>>>{email}>>>traffic>>>uplink|downlink
        if len(parts) == 4 and parts[0] == 'user' and parts[2] == 'traffic':
            entry = result.setdefault(parts[1], {'up': 0, 'down': 0})
            if parts[3] == 'uplink':
                entry['up'] += value
            elif parts[3] == 'downlink':
                entry['down'] += value
    return result


def post_traffic(node_id, ip, traffic_map):
    """上报每用户流量增量到网关 {GATEWAY_URL}/traffic"""
    deltas = [{'userId': uid, 'up': t.get('up', 0), 'down': t.get('down', 0)}
              for uid, t in traffic_map.items() if t.get('up', 0) or t.get('down', 0)]
    if not deltas:
        print('流量上报: 本轮无增量')
        return True
    body = {'nodeId': node_id, 'ip': ip, 'version': __version__, 'deltas': json.dumps(deltas)}
    from urllib.parse import urlencode
    try:
        req = Request(traffic_url(), data=urlencode(body).encode('utf-8'))
        req.add_header('User-Agent', 'proxy-relay-discover/%s' % __version__)
        urlopen(req, timeout=15).read()
        print('流量上报: %d 个用户增量' % len(deltas))
        return True
    except Exception as e:
        print('WARN: 流量上报失败: %s' % e, file=sys.stderr)
        return False


# ---------------------------------------------------------------- 注册

def build_qrcode(uuid, host, path, name):
    """Shadowrocket VLESS-WS 链接（格式同 cron.py）"""
    obfs_param = json.dumps({'Host': host}, separators=(',', ':'), ensure_ascii=False)
    params = [
        'path=' + url_encode(path),
        'remarks=' + url_encode(name),
        'obfsParam=' + url_encode(obfs_param),
        'obfs=websocket', 'tls=1',
        'peer=' + url_encode(host), 'tfo=1',
    ]
    return 'vless://%s?%s' % (b64url(':%s@%s:%d' % (uuid, host, 443)), '&'.join(params))


def build_clash(uuid, host, path, name):
    """Clash/Mihomo VLESS-WS 配置（格式同 cron.py）"""
    return OrderedDict([
        ('name', name), ('type', 'vless'), ('server', host), ('port', 443),
        ('uuid', uuid), ('udp', True), ('tls', True), ('network', 'ws'),
        ('client-fingerprint', 'random'), ('servername', host), ('skip-cert-verify', False),
        ('ws-opts', OrderedDict([('path', path), ('headers', OrderedDict([('Host', host)]))])),
    ])


def post_discover(url, proxy_name, qrcode, clash, node_uuid, ip, ip_info):
    """POST 到网关注册接口，code 为 0 或 88 视为成功"""
    from urllib.parse import urlencode
    body = {
        'uuid': node_uuid, 'proxyName': proxy_name,
        'clash': b64url(json.dumps(clash, ensure_ascii=False)),
        'qrcode': qrcode or '', 'ip': ip, 'version': __version__,
        'countryCode': ip_info.get('countryCode', ''),
        'city': ip_info.get('city', ''), 'regionName': ip_info.get('regionName', ''),
    }
    req = Request(url, data=urlencode(body).encode('utf-8'))
    req.add_header('Content-Type', 'application/x-www-form-urlencoded; charset=UTF-8')
    resp = urlopen(req, timeout=30).read().decode('utf-8')
    return json.loads(resp).get('code') in (0, 88), resp


def register_node(uuid, node_id, host, ip_info):
    """生成 qrcode/clash 并上报注册（名称 国旗+PROXY_NAME）"""
    label = os.environ.get('PROXY_NAME', DEFAULT_NAME).strip() or DEFAULT_NAME
    try:
        order = int(os.environ.get('ORDER', str(DEFAULT_ORDER)))
    except ValueError:
        order = DEFAULT_ORDER
    ai_env = os.environ.get('AI', '').strip().lower()
    cc = ip_info.get('countryCode', '')
    name_flagged = (u'%s %s' % (country_code_to_flag(cc), label)).strip()
    ai = (ai_env == 'true') if ai_env in ('true', 'false') else (cc not in ('CN', 'HK', 'MO'))
    url = '%s?order=%d&ai=%s' % (discover_url(), order, 'true' if ai else 'false')
    qrcode = build_qrcode(uuid, host, WS_PATH, label)            # remarks 用裸标签（同 cron.py）
    clash = build_clash(uuid, host, WS_PATH, name_flagged)
    print(u'注册节点: %s (ip=%s host=%s order=%d ai=%s)' % (name_flagged, ip_info.get('ip', ''), host, order, ai))
    try:
        ok, resp = post_discover(url, name_flagged, qrcode, clash, node_id, ip_info.get('ip', ''), ip_info)
        print('网关响应: %s' % resp)
        return ok
    except Exception as e:
        print('WARN: 注册失败: %s' % e, file=sys.stderr)
        return False


# ---------------------------------------------------------------- main

def main():
    argv = sys.argv[1:]

    if '--print-uuid' in argv:
        uuid, err = resolve_uuid()
        if err:
            print('ERROR: %s' % err, file=sys.stderr)
            return 1
        print(uuid)
        return 0

    if '--write-xray-config' in argv:
        try:
            out_path = argv[argv.index('--write-xray-config') + 1]
        except IndexError:
            print('ERROR: --write-xray-config 需要输出路径参数', file=sys.stderr)
            return 1
        uuid, err = resolve_uuid()
        if err:
            print('ERROR: %s' % err, file=sys.stderr)
            return 1
        # 只写仅含节点自身 uuid 的基础配置；用户运行时经 xray API 动态加入，不落盘、不进日志
        write_xray_config(out_path, uuid)
        print('VLESS_UUID=%s (node-self; 用户运行时经 xray API 动态添加，不落盘)' % _mask_uuid(uuid))
        return 0

    uuid, err = resolve_uuid()
    if err:
        print('ERROR: %s' % err, file=sys.stderr)
        return 1
    node_id = os.environ.get('NODE_ID', '').strip() or uuid

    if not gateway_base():
        print('ERROR: GATEWAY_URL 必填（网关基址，建议放 GitHub Secrets）', file=sys.stderr)
        return 1

    try:
        ip_info = get_ip_info()
    except Exception as e:
        print('WARN: 获取 IP 信息失败: %s' % e, file=sys.stderr)
        ip_info = {'ip': '', 'countryCode': '', 'city': '', 'regionName': ''}
    ip = ip_info.get('ip', '')

    # --report-traffic：仅 flush 上报一次流量（接力交接前用）
    if '--report-traffic' in argv:
        post_traffic(node_id, ip, read_xray_user_traffic())
        return 0

    # 默认：注册 + 用户同步(免重启) + 流量上报
    host = os.environ.get('CF_HOSTNAME', '').strip()
    if host:
        register_node(uuid, node_id, host, ip_info)
    else:
        print('WARN: CF_HOSTNAME 未设置，跳过注册', file=sys.stderr)

    users = fetch_users(users_url())
    if users is not None:
        sync_xray_users_via_api(uuid, users)

    post_traffic(node_id, ip, read_xray_user_traffic())
    return 0


if __name__ == '__main__':
    sys.exit(main())
