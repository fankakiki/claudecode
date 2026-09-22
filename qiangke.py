#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
抢课 · 回流票监控（macOS 单文件版 / 零依赖）
============================================

用法：
    python3 qiangke.py --probe     先探测，核对余量读数对不对
    python3 qiangke.py             立刻开跑，抢到为止
    python3 qiangke.py --at 12:30  到点再开抢
    python3 qiangke.py --watch     只看不抢，用来验证配置

只用 Python 标准库，Mac 自带的 python3 就能跑，不需要 pip install 任何东西。
抢到后会弹 Mac 系统通知；掉登录也会弹通知提醒你。

—— 使用前只需要填下面两段 cURL ——
"""

# ╔══════════════════════════════════════════════════════════════════════╗
# ║ ①  查询接口：选课页面点一次「查询」，在 Network 里找到返回课程 JSON  ║
# ║     的那条请求 → 右键 Copy → Copy as cURL → 粘贴到下面三引号中间     ║
# ╚══════════════════════════════════════════════════════════════════════╝
QUERY_CURL = r"""
在这里粘贴第①段 cURL
"""

# ╔══════════════════════════════════════════════════════════════════════╗
# ║ ②  提交接口：在目标课程（此刻满员）上点一次「选课」让它失败，       ║
# ║     把那条请求 Copy as cURL → 粘贴到下面三引号中间                   ║
# ║     必须是目标课程本身的提交请求，脚本原样重放它                     ║
# ╚══════════════════════════════════════════════════════════════════════╝
SUBMIT_CURL = r"""
在这里粘贴第②段 cURL
"""

# ────────────── 常用参数 ──────────────
KEYWORD = "MARX6007"      # 课程号
CLASS_KEYWORD = "卢湾1班"  # 教学班关键词。MARX6007 有 3 个班，必须指定到班；
                          # 留空("")则该课所有教学班都盯（但提交请求只有一个班的，会错炮）
INTERVAL = 1.0         # 轮询间隔（秒）。不要往下调，容易被风控，反而彻底抢不到
TIMEOUT = 15.0         # 读取响应的超时（秒）。网络差时给足耐心，别把慢当成失败
CONNECT_TIMEOUT = 8.0  # 建连+TLS握手的超时（秒）。握手卡住要快速失败重来
BURST = 3              # 发现余量后连续提交几次（失败立刻重试，不等下一轮）
BURST_GAP = 0.15       # 两次提交之间隔多久（秒）
MAX_FAILS = 20         # 连续多少轮提交失败就停机（防止无意义地反复砸接口）
NOTIFY = True          # 抢到 / 掉登录时弹 Mac 系统通知

# 字段名已按你的真实响应填好（KXRS=可选人数/容量，DQRS=当前人数/已选）
REMAINING_FIELD = None       # 该系统没有直接的"剩余"字段，留 None
CAPACITY_FIELD = "KXRS"
SELECTED_FIELD = "DQRS"
CONFLICT_FIELD = "IS_CONFLICT"  # 值为 1 表示与你已选课程时间冲突，抢了也是失败
SKIP_CONFLICT = True            # 冲突的班直接跳过，但会打日志，不静默
SUCCESS_PATTERN = None   # 例如 r"选课成功"
FAIL_PATTERN = None      # 例如 r"失败|已满|冲突"
# ──────────────────────────────────────

import argparse
import codecs
import gzip
import http.client
import json
import logging
import re
import shlex
import ssl
import subprocess
import sys
import time
import zlib
from datetime import datetime, timedelta
from urllib.parse import urlsplit

logger = logging.getLogger("qiangke")


def setup_logging(verbose):
    logger.setLevel(logging.DEBUG if verbose else logging.INFO)
    sh = logging.StreamHandler()
    sh.setFormatter(logging.Formatter("%(asctime)s.%(msecs)03d %(message)s", datefmt="%H:%M:%S"))
    logger.addHandler(sh)
    fh = logging.FileHandler("qiangke.log", encoding="utf-8")
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(fh)


# ============ Mac 通知 ============

def notify(title, message, sound="Glass"):
    """弹一个 Mac 系统通知。非 macOS 或 osascript 不可用时静默跳过。"""
    if not NOTIFY or sys.platform != "darwin":
        return

    def q(s):
        return '"' + str(s).replace("\\", "\\\\").replace('"', '\\"') + '"'

    script = "display notification %s with title %s" % (q(message[:200]), q(title))
    if sound:
        script += ' sound name "%s"' % sound
    try:
        subprocess.run(["osascript", "-e", script], capture_output=True, timeout=5)
    except Exception:
        pass


# ============ cURL 解析 ============

_ANSI_C = re.compile(r"\$'((?:\\.|[^'\\])*)'", re.S)
_DROP_HEADERS = {"content-length", "host", "connection", "accept-encoding"}
_PLACEHOLDER_HINT = "在这里粘贴"


class Req(object):
    def __init__(self, method, url, headers, body):
        self.method = method
        self.url = url
        self.headers = headers
        self.body = body
        p = urlsplit(url)
        self.scheme = p.scheme or "https"
        self.host = p.hostname
        self.port = p.port or (443 if self.scheme == "https" else 80)
        self.netloc = p.netloc
        self.path = p.path + (("?" + p.query) if p.query else "")


def parse_curl(text, label):
    if _PLACEHOLDER_HINT in text or not text.strip():
        raise SystemExit(
            "\n❌ 还没填【%s】的 cURL。\n"
            "   请打开 qiangke.py，把浏览器里 Copy as cURL 得到的内容\n"
            "   粘贴到文件顶部对应的三引号中间，再重新运行。\n" % label)

    stash = {}

    def _keep(m):
        key = "__ANSIC_%d__" % len(stash)
        stash[key] = codecs.decode(m.group(1), "unicode_escape")
        return "'%s'" % key

    text = _ANSI_C.sub(_keep, text)
    text = text.replace("\\\n", " ")

    try:
        tokens = shlex.split(text)
    except ValueError as e:
        raise SystemExit("❌ 【%s】的 cURL 解析失败：%s\n   请确认复制的是 Copy as cURL（不是 PowerShell 格式）" % (label, e))
    if not tokens or tokens[0] != "curl":
        raise SystemExit("❌ 【%s】不是以 curl 开头，请重新 Copy as cURL" % label)

    def rest(s):
        return stash.get(s, s)

    method = url = None
    headers = {}
    cookies = []
    bodies = []
    i = 1
    while i < len(tokens):
        t = tokens[i]
        if t in ("-X", "--request"):
            method = tokens[i + 1].upper(); i += 2
        elif t in ("-H", "--header"):
            raw = rest(tokens[i + 1])
            if ":" in raw and not raw.startswith(":"):
                k, v = raw.split(":", 1)
                k, v = k.strip(), v.strip()
                if k.lower() == "cookie":
                    cookies.append(v)
                elif k.lower() not in _DROP_HEADERS:
                    headers[k] = v
            i += 2
        elif t in ("-b", "--cookie"):
            cookies.append(rest(tokens[i + 1])); i += 2
        elif t in ("-d", "--data", "--data-raw", "--data-binary", "--data-ascii", "--data-urlencode"):
            bodies.append(rest(tokens[i + 1])); i += 2
        elif t in ("-A", "--user-agent"):
            headers["User-Agent"] = rest(tokens[i + 1]); i += 2
        elif t in ("-e", "--referer"):
            headers["Referer"] = rest(tokens[i + 1]); i += 2
        elif t == "--url":
            url = rest(tokens[i + 1]); i += 2
        elif t.startswith("-"):
            i += 1  # --compressed / -s / -k 之类的开关
        else:
            if url is None:
                url = rest(t)
            i += 1

    if not url:
        raise SystemExit("❌ 【%s】的 cURL 里没找到 URL" % label)
    if cookies:
        headers["Cookie"] = "; ".join(cookies)
    headers["Accept-Encoding"] = "gzip, deflate"  # br 需要第三方库，明确排除

    body = "&".join(bodies) if bodies else None
    if method is None:
        method = "POST" if body is not None else "GET"
    return Req(method, url, headers, body)


# ============ 带长连接的 HTTP ============

class Http(object):
    """复用同一条 TCP/TLS 连接，省掉每轮握手；断了自动重连一次。"""

    def __init__(self, req, timeout, connect_timeout=None):
        self.host, self.port, self.tls, self.timeout = req.host, req.port, req.scheme == "https", timeout
        self.connect_timeout = connect_timeout or timeout
        self.conn = None

    def _connect(self):
        # 握手和读取用两个超时：握手卡住要尽快放弃重来，读取则要给服务器慢慢答的余地。
        if self.tls:
            ctx = ssl.create_default_context()
            self.conn = http.client.HTTPSConnection(
                self.host, self.port, timeout=self.connect_timeout, context=ctx)
        else:
            self.conn = http.client.HTTPConnection(self.host, self.port, timeout=self.connect_timeout)
        self.conn.connect()
        if self.conn.sock is not None:
            self.conn.sock.settimeout(self.timeout)

    def close(self):
        try:
            if self.conn:
                self.conn.close()
        except Exception:
            pass
        self.conn = None

    def send(self, req, body=None, path=None):
        # URL 上的 _=<毫秒> 是浏览器加的防缓存参数。原样重放等于每次请求都是同一个
        # URL，中间任何一层缓存都可能回一份过期的课程列表 —— 那就永远看不到回流票。
        path = path or _bust_cache(req.path)
        payload = req.body if body is None else body
        data = payload.encode("utf-8") if isinstance(payload, str) else payload
        headers = dict(req.headers)
        if data is not None and "Content-Type" not in headers:
            headers["Content-Type"] = "application/x-www-form-urlencoded; charset=UTF-8"

        for attempt in (1, 2):
            try:
                if self.conn is None:
                    self._connect()
                self.conn.request(req.method, path or req.path, body=data, headers=headers)
                resp = self.conn.getresponse()
                raw = resp.read()  # 必须读完，否则连接没法复用
                if resp.getheader("Connection", "").lower() == "close":
                    self.close()
                return resp.status, resp.getheaders(), _decode(raw, resp.getheader("Content-Encoding"))
            except ssl.SSLCertVerificationError:
                raise SystemExit(
                    "❌ TLS 证书校验失败。如果你用的是 python.org 装的 Python，\n"
                    "   请到 /Applications/Python 3.x/ 里双击一次 'Install Certificates.command'。")
            except (http.client.HTTPException, OSError) as e:
                self.close()
                if attempt == 2:
                    raise
                logger.debug("连接中断(%s)，重连重试", e)
        raise RuntimeError("unreachable")


_TS_PARAM = re.compile(r"([?&]_=)\d+")


def _bust_cache(path):
    return _TS_PARAM.sub(lambda m: m.group(1) + str(int(time.time() * 1000)), path)


def _hdr(headers, name):
    """从 (k, v) 列表里取某个响应头。"""
    low = name.lower()
    for k, v in headers:
        if k.lower() == low:
            return v
    return ""


def _parse_cookie_header(value):
    out = {}
    for part in (value or "").split(";"):
        part = part.strip()
        if part and "=" in part:
            k, v = part.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def merge_set_cookie(headers, *reqs):
    """把服务端下发的 Set-Cookie 合并回请求头。

    会话跑几小时，服务端很可能中途轮换 JSESSIONID / route 之类的 cookie。
    一直发旧的那份，迟早被当成掉登录。
    """
    jar = _parse_cookie_header(reqs[0].headers.get("Cookie", ""))
    changed = False
    for k, v in headers:
        if k.lower() != "set-cookie":
            continue
        first = v.split(";", 1)[0].strip()
        if "=" not in first:
            continue
        name, val = first.split("=", 1)
        name, val = name.strip(), val.strip()
        if name and jar.get(name) != val:
            jar[name] = val
            changed = True
    if changed:
        cookie = "; ".join("%s=%s" % kv for kv in jar.items())
        for r in reqs:
            r.headers["Cookie"] = cookie
        logger.debug("Cookie 已更新（服务端轮换）")
    return changed


def _decode(raw, encoding):
    enc = (encoding or "").lower()
    try:
        if "gzip" in enc:
            raw = gzip.decompress(raw)
        elif "deflate" in enc:
            try:
                raw = zlib.decompress(raw)
            except zlib.error:
                raw = zlib.decompress(raw, -zlib.MAX_WBITS)
    except Exception:
        pass
    return raw.decode("utf-8", "replace")


_LOGIN_HINTS = re.compile(r"(未登录|登录超时|请重新登录|统一身份认证|jaccount|login\.html|会话.*(过期|失效))", re.I)


def logged_out(status, headers, text):
    if status in (301, 302, 303, 307, 401, 403):
        return True
    ctype = _hdr(headers, "Content-Type").lower()
    if "json" not in ctype and "<html" in text[:2000].lower():
        return True
    return bool(_LOGIN_HINTS.search(text[:2000]))


# ============ 在响应里定位课程 + 读余量 ============

def walk_dicts(obj, path=""):
    if isinstance(obj, dict):
        yield path, obj
        for k, v in obj.items():
            yield from walk_dicts(v, ("%s.%s" % (path, k)) if path else k)
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            yield from walk_dicts(v, "%s[%d]" % (path, i))


def _scalars(d):
    for v in d.values():
        if isinstance(v, (str, int, float)) and not isinstance(v, bool):
            yield str(v)


def _matches(rec, *keywords):
    vals = [v.lower() for v in _scalars(rec)]
    return all(any(kw.lower() in v for v in vals) for kw in keywords if kw)


def find_records(payload, keyword, class_keyword=""):
    """找出字面量字段里含关键词的最内层 dict，即课程/教学班记录。

    同一门课往往有多个教学班（MARX6007 就有 3 个），而提交请求只对应其中一个，
    所以必须能筛到班一级，否则会拿 A 班的余量去点 B 班的选课按钮。
    """
    hits = [(p, d) for p, d in walk_dicts(payload) if _matches(d, keyword, class_keyword)]
    out = []
    for p, d in hits:
        deeper = any(q != p and q.startswith(p) and q[len(p):len(p) + 1] in (".", "[") for q, _ in hits)
        if not deeper:
            out.append((p, d))
    return out


def is_conflict(rec):
    """该教学班是否与已选课程冲突（冲突的话抢到也是提交失败）。"""
    if not SKIP_CONFLICT or not CONFLICT_FIELD:
        return False
    return _as_int(rec.get(CONFLICT_FIELD)) == 1


# 注意：kxrs(可选人数) 是"容量"不是"剩余"，误判成剩余会导致满员时疯狂空炮
_REM_PAT = re.compile(r"(syrs|kyrs|sy_rs|剩余|余量|remain|surplus|available|free)", re.I)
_CAP_PAT = re.compile(r"(kxrs|kcrl|kkrl|jxbrl|容量|capacity|zrs|total|maxnum|max_num|upperlimit|limit)", re.I)
_SEL_PAT = re.compile(r"(dqrs|yxrs|yxzrs|xkrs|yx_rs|已选|selected|selectnum|enrolled)", re.I)
_COMBO_PAT = re.compile(r"^\s*(\d+)\s*/\s*(\d+)\s*$")


def _as_int(v):
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v
    if isinstance(v, float) and v.is_integer():
        return int(v)
    if isinstance(v, str) and re.fullmatch(r"\s*-?\d+\s*", v):
        return int(v.strip())
    return None


def read_quota(rec):
    """返回 (剩余名额, 推导说明)。推导不出来返回 (None, 原因)。"""
    if REMAINING_FIELD:
        n = _as_int(rec.get(REMAINING_FIELD))
        return (n, "%s=%s" % (REMAINING_FIELD, n)) if n is not None else (None, "字段 %s 不存在或不是数字" % REMAINING_FIELD)
    if CAPACITY_FIELD and SELECTED_FIELD:
        cap, sel = _as_int(rec.get(CAPACITY_FIELD)), _as_int(rec.get(SELECTED_FIELD))
        if cap is not None and sel is not None:
            return cap - sel, "%s=%d - %s=%d" % (CAPACITY_FIELD, cap, SELECTED_FIELD, sel)
        return None, "字段 %s / %s 不存在或不是数字" % (CAPACITY_FIELD, SELECTED_FIELD)

    for k, v in rec.items():
        n = _as_int(v)
        if n is not None and _REM_PAT.search(k):
            return n, "剩余字段 %s=%d" % (k, n)

    cap = sel = None
    cap_k = sel_k = ""
    for k, v in rec.items():
        n = _as_int(v)
        if n is None:
            continue
        if cap is None and _CAP_PAT.search(k):
            cap, cap_k = n, k
        elif sel is None and _SEL_PAT.search(k):
            sel, sel_k = n, k
    if cap is not None and sel is not None:
        return cap - sel, "%s=%d - %s=%d" % (cap_k, cap, sel_k, sel)

    for k, v in rec.items():
        if isinstance(v, str):
            m = _COMBO_PAT.match(v)
            if m:
                return int(m.group(2)) - int(m.group(1)), "%s='%s' 视为 已选/容量" % (k, v)

    return None, "识别不出余量字段，请在文件顶部填 CAPACITY_FIELD + SELECTED_FIELD"


def label_of(rec):
    picks = []
    for k, v in rec.items():
        if isinstance(v, str) and v.strip() and len(v) <= 40 and re.search(
                r"(bjmc|kcmc|课程名|name|jxbmc|教学班|teacher|jsxm|skjs|rkjs)", k, re.I):
            picks.append(v.strip())
    ident = " / ".join(dict.fromkeys(picks))[:60]
    return "%s%s" % (KEYWORD, " · " + ident if ident else "")


# ============ 提交与结果判定 ============

_CSRF_KEYNAME = re.compile(r"csrf", re.I)
_CSRF_IN_BODY = re.compile(r"(csrfToken=)[^&]*", re.I)
# 提交被 CSRF/令牌挡下来的典型措辞 —— 这种失败重试一万次也没用，必须立刻告诉用户
_TOKEN_ERR = re.compile(r"(csrf|token|令牌|非法请求|请求非法|重复提交|请刷新|重新登录)", re.I)
# 提交失败里属于"会话/令牌已死"的那一类：重试再多次也不可能成功，必须停机重抓，
# 否则脚本会以每秒一次的频率空砸提交接口，纯属自找风控。
_DEAD_SESSION = re.compile(r"(\[掉登录\]|csrf|token|令牌|非法请求|请求非法|重新登录|会话.*失效|未登录)", re.I)


def find_csrf(payload):
    """查询响应里如果带了新的 csrfToken 就捞出来，用于刷新提交请求体。"""
    for _, d in walk_dicts(payload):
        for k, v in d.items():
            if _CSRF_KEYNAME.search(k) and isinstance(v, str) and len(v) >= 16:
                return v
    return None


def refresh_csrf(submit, payload):
    """把提交请求体里的 csrfToken 换成最新的。查询响应里没有就原样不动。"""
    tok = find_csrf(payload)
    if not tok or not submit.body or "csrfToken=" not in submit.body.lower():
        return False
    new_body = _CSRF_IN_BODY.sub(lambda m: m.group(1) + tok, submit.body)
    if new_body != submit.body:
        submit.body = new_body
        logger.info("🔑 csrfToken 已刷新")
        return True
    return False


_FAIL_DEFAULT = re.compile(r"(失败|已满|满员|人数已|超过|不允许|不能|冲突|已选过|重复|无效|错误|error|false)", re.I)
_OK_DEFAULT = re.compile(r"(成功|success|\"code\"\s*:\s*\"?(1|0|200)\"?)", re.I)


def judge(text):
    """先判失败再判成功 —— 很多系统失败时也返回 code:1，只看 code 会把失败当成功。"""
    s = text.strip()[:400]
    fail = re.compile(FAIL_PATTERN, re.I) if FAIL_PATTERN else _FAIL_DEFAULT
    ok = re.compile(SUCCESS_PATTERN, re.I) if SUCCESS_PATTERN else _OK_DEFAULT
    if fail.search(s):
        return False, s
    if ok.search(s):
        return True, s
    return False, s


def burst_submit(http_sub, submit, query=None):
    """抢到余量的瞬间连打几次，失败立刻重试，不等下一个轮询周期。"""
    last = ""
    for i in range(BURST):
        try:
            status, headers, text = http_sub.send(submit)
            merge_set_cookie(headers, submit, *( [query] if query else [] ))
            if logged_out(status, headers, text):
                return False, "[掉登录] " + text[:150]
            ok, msg = judge(text)
            if not ok and _TOKEN_ERR.search(msg):
                logger.error("🔑 提交被令牌/会话挡下：%s", msg[:150])
                logger.error("   csrfToken 或 Cookie 已失效，重抓一次 SUBMIT_CURL 才能继续。")
                notify("抢课脚本：令牌失效", "csrfToken 过期，需要重新抓包")
                return False, msg
        except Exception as e:
            ok, msg = False, "提交异常: %s" % e
        logger.info("   提交 #%d → %s | %s", i + 1, "成功" if ok else "未成功", msg)
        if ok:
            return True, msg
        last = msg
        if i < BURST - 1:
            time.sleep(BURST_GAP)
    return False, last


# ============ 主流程 ============

def poll(http_q, query, *sync):
    status, headers, text = http_q.send(query)
    merge_set_cookie(headers, query, *sync)
    if logged_out(status, headers, text):
        raise PermissionError("登录态失效 (HTTP %s)" % status)
    if status >= 400:
        raise IOError("HTTP %s" % status)
    try:
        return json.loads(text)
    except ValueError:
        # 有些字段（如排课时间 PKSJDDMS）里带裸换行，严格 JSON 解析会失败。
        # 去掉换行对合法 JSON 无损（引号外的换行只是空白），对这种脏数据正好救回来。
        try:
            return json.loads(text.replace("\r", "").replace("\n", ""))
        except ValueError:
            raise ValueError("响应不是 JSON：%s" % text[:200])


def do_probe(query, submit):
    logger.info("查询接口: %s %s://%s%s", query.method, query.scheme, query.netloc, query.path)
    logger.info("请求头 %d 个 | Cookie %s | 请求体 %s",
                len(query.headers), "有" if "Cookie" in query.headers else "⚠️ 无",
                "%d 字节" % len(query.body) if query.body else "无")

    http_q = Http(query, TIMEOUT, CONNECT_TIMEOUT)
    t0 = time.time()
    payload = poll(http_q, query)
    logger.info("单轮耗时: %.0f ms", (time.time() - t0) * 1000)

    total = None
    for _, d in walk_dicts(payload):
        if "total" in d and _as_int(d.get("total")) is not None:
            total = _as_int(d["total"]); break
    psize = _as_int((re.search(r"pageSize=(\d+)", query.body or "") or [None, None])[1]) \
        if re.search(r"pageSize=(\d+)", query.body or "") else None
    if total is not None and psize is not None and total > psize:
        logger.warning("⚠️ 接口返回 total=%d 但 pageSize=%d —— 目标可能被分页挡在后面页！"
                       "把 QUERY_CURL 请求体里的 pageSize 调大。", total, psize)
    elif total is not None:
        logger.info("接口 total=%s，目标在第一页内", total)

    records = find_records(payload, KEYWORD, CLASS_KEYWORD)
    if not records:
        logger.error("❌ 响应里没有含 '%s' 的记录。可能是：", KEYWORD)
        logger.error("   · 抓的不是课程列表接口 —— 换一条 XHR 重抓")
        logger.error("   · 这个接口带了分页/筛选参数，目标课不在返回结果里")
        logger.error("   · 关键词写错了")
        logger.error("响应预览: %s", json.dumps(payload, ensure_ascii=False)[:1000])
        return 1

    logger.info("✅ 命中 %d 条记录：", len(records))
    if len(records) > 1:
        logger.warning("⚠️ 匹配到 %d 个教学班。提交请求只对应其中一个，"
                       "请用 CLASS_KEYWORD 精确到班，否则会拿这个班的余量去点另一个班。", len(records))
    for path, rec in records:
        rem, how = read_quota(rec)
        if is_conflict(rec):
            logger.warning(">>> ⚠️ 该班 IS_CONFLICT=1（与已选课程冲突），运行时会被跳过")
        logger.info("--- %s ---", path)
        logger.info("%s", json.dumps(rec, ensure_ascii=False, indent=2)[:1500])
        logger.info(">>> 余量推导: %s  →  剩余 %s", how, rem)
        if rem is None:
            logger.warning(">>> ⚠️ 照着上面的字段名，在 qiangke.py 顶部填 CAPACITY_FIELD / SELECTED_FIELD")
        else:
            logger.warning(">>> ⚠️ 请对照选课页面确认这个数字是对的，再开始抢")

    logger.info("提交接口: %s %s://%s%s", submit.method, submit.scheme, submit.netloc, submit.path)
    logger.info("提交请求体: %s", (submit.body or "")[:400])
    logger.info("（确认上面这段就是目标课程的提交请求，脚本会原样重放它）")
    return 0


def wait_until(ts):
    try:
        parts = [int(x) for x in ts.split(":")]
        h, m = parts[0], parts[1]
        s = parts[2] if len(parts) > 2 else 0
    except (ValueError, IndexError):
        raise SystemExit("--at 的格式应该是 HH:MM 或 HH:MM:SS，收到 %r" % ts)
    target = datetime.now().replace(hour=h, minute=m, second=s, microsecond=0)
    if target <= datetime.now():
        target += timedelta(days=1)
    logger.info("⏳ 等到 %s 开抢（还有 %.0f 秒）…", target.strftime("%m-%d %H:%M:%S"),
                (target - datetime.now()).total_seconds())
    while True:
        left = (target - datetime.now()).total_seconds()
        if left <= 0:
            break
        time.sleep(min(left, 0.5))
    logger.info("⏰ 到点，开抢！")


def run(query, submit, watch_only):
    http_q = Http(query, TIMEOUT, CONNECT_TIMEOUT)
    # 同域名就共用一条连接：它被轮询一直焐着，真要提交时省掉 TCP+TLS 握手，
    # 而那几百毫秒正好花在最关键的一刻上。
    if submit.netloc == query.netloc:
        http_s = http_q
        logger.debug("提交与查询同域，共用长连接")
    else:
        http_s = Http(submit, TIMEOUT, CONNECT_TIMEOUT)
    logger.info("🚀 开始监控 %s | 间隔 %.1fs | %s", KEYWORD, INTERVAL, "仅观察" if watch_only else "自动抢")

    cycle = miss = fails = 0
    backoff = 0.0
    last = {}
    skipped = set()
    t_start = time.time()

    while True:
        cycle += 1
        tick = time.time()
        try:
            payload = poll(http_q, query, submit)
            refresh_csrf(submit, payload)
            backoff = 0.0
        except PermissionError as e:
            logger.error("❌ %s —— Cookie 过期了，请重新 Copy as cURL 更新 QUERY_CURL", e)
            notify("抢课脚本已停止", "登录态失效，需要重新抓包")
            return 2
        except Exception as e:
            backoff = min(20.0, backoff * 2 + 1)
            logger.warning("查询失败: %s，%.1fs 后重试", e, backoff)
            http_q.close()
            time.sleep(backoff)
            continue

        records = find_records(payload, KEYWORD, CLASS_KEYWORD)
        if not records:
            miss += 1
            if miss in (3, 10) or miss % 50 == 0:
                logger.warning("⚠️ 连续 %d 轮没找到 %s —— 接口可能带了分页/筛选，"
                               "静默空转是抢不到课最常见的原因，请停下来复查！", miss, KEYWORD)
            time.sleep(max(0.0, INTERVAL - (time.time() - tick)))
            continue
        miss = 0

        for path, rec in records:
            if is_conflict(rec):
                if path not in skipped:
                    skipped.add(path)
                    logger.warning("⏭️ 跳过 %s：IS_CONFLICT=1，与你已选课程时间冲突，抢到也会被拒",
                                   label_of(rec))
                continue
            rem, how = read_quota(rec)
            if rem is None:
                if cycle == 1 or cycle % 120 == 0:
                    logger.warning("读不出余量（%s），先跑 --probe 配好字段", how)
                continue

            if last.get(path) != rem:
                logger.info("📊 %s 余量 %s → %d (%s)", label_of(rec), last.get(path, "?"), rem, how)
                last[path] = rem

            if rem > 0:
                if watch_only:
                    logger.info("🔥 出现回流票！余量 %d（仅观察模式，不提交）", rem)
                    continue
                logger.info("🔥 出现回流票！余量 %d，立即提交！", rem)
                ok, msg = burst_submit(http_s, submit, query)
                if not ok and _DEAD_SESSION.search(msg):
                    logger.error("❌ 提交被会话/令牌拒绝：%s", msg[:200])
                    logger.error("   继续重试只会空砸接口、招来风控，脚本停机。")
                    logger.error("   请重新抓一次两段 cURL（Cookie 和 csrfToken 都要新的）再启动。")
                    notify("抢课脚本已停止", "会话或 csrfToken 失效，需要重新抓包")
                    return 2
                if not ok:
                    fails += 1
                    if fails == 5:
                        logger.warning("⚠️ 已连续 %d 轮提交失败：%s", fails, msg[:120])
                        logger.warning("   如果每轮都是同样的失败原因，多半不是手慢，而是配置或权限问题。")
                        notify("抢课脚本", "连续 5 轮提交失败，去看一眼日志")
                    elif fails >= MAX_FAILS:
                        logger.error("❌ 连续 %d 轮提交失败，停机避免无意义地反复请求。", fails)
                        logger.error("   最后一次响应：%s", msg[:200])
                        notify("抢课脚本已停止", "连续提交失败过多")
                        return 3
                else:
                    fails = 0
                if ok:
                    logger.info("🎉🎉 选课成功：%s", label_of(rec))
                    logger.info("响应：%s", msg)
                    notify("🎉 抢到了！", "%s 选课成功，去系统里确认一下" % KEYWORD)
                    try:
                        time.sleep(0.6)
                        for _, r2 in find_records(poll(http_q, query, submit), KEYWORD, CLASS_KEYWORD):
                            logger.info("复查：%s", json.dumps(r2, ensure_ascii=False)[:300])
                    except Exception:
                        pass
                    logger.info("⚠️ 请立刻去选课系统刷新页面确认，不要只信这条日志。")
                    return 0
                logger.info("😢 这次没抢到（%s），继续盯。", msg[:100])

        if cycle % 60 == 0:
            mins = (time.time() - t_start) / 60
            per = (time.time() - t_start) / cycle
            logger.info("… 已盯 %d 轮 / %.1f 分钟 | 平均 %.2fs/轮（理想 %.2fs）| 当前余量 %s",
                        cycle, mins, per, INTERVAL, list(last.values()) or "未知")
            if per > INTERVAL * 1.5:
                logger.warning("⚠️ 实际节奏比设定慢了 %.0f%%，说明请求本身在拖时间，"
                               "不是脚本在等。先确认浏览器打开选课页快不快。", (per / INTERVAL - 1) * 100)

        elapsed = time.time() - tick
        logger.debug("第 %d 轮耗时 %.0f ms", cycle, elapsed * 1000)
        if elapsed > max(3.0, INTERVAL * 3) and cycle > 1:
            logger.warning("🐢 本轮耗时 %.1fs（正常应在 0.1s 内）—— 网络或服务器很慢，"
                           "回流票可能在这段空窗里被别人抢走。", elapsed)
        time.sleep(max(0.0, INTERVAL - elapsed))


def main():
    ap = argparse.ArgumentParser(description="抢课回流票监控（macOS 单文件版）")
    ap.add_argument("--probe", action="store_true", help="探测一次，核对余量读数")
    ap.add_argument("--watch", action="store_true", help="只监控不提交")
    ap.add_argument("--at", metavar="HH:MM", help="定时开抢，例如 --at 12:30")
    ap.add_argument("-v", "--verbose", action="store_true", help="显示每轮耗时")
    args = ap.parse_args()

    setup_logging(args.verbose)
    query = parse_curl(QUERY_CURL, "查询接口")
    submit = parse_curl(SUBMIT_CURL, "提交接口")

    try:
        if args.probe:
            return do_probe(query, submit)
        if args.at:
            wait_until(args.at)
        return run(query, submit, args.watch)
    except KeyboardInterrupt:
        logger.info("已手动停止。")
        return 130
    except PermissionError as e:
        # 最常见的一种失败：抓包放久了，Cookie 过期。不该甩 traceback 给用户。
        logger.error("")
        logger.error("❌ %s", e)
        logger.error("   Cookie 过期了 —— 这是最常见的情况，不是脚本坏了。")
        logger.error("")
        logger.error("   重新抓一次包就行：")
        logger.error("   1. 浏览器打开选课页面，确认还是登录状态（没登录就先登录）")
        logger.error("   2. ⌘⌥I 打开开发者工具 → Network → 勾 Fetch/XHR")
        logger.error("   3. 点一次页面上的「查询」→ 找到 loadJhnCourseInfo 那条")
        logger.error("      → 右键 Copy → Copy as cURL → 替换文件顶部的 QUERY_CURL")
        logger.error("   4. 在卢湾1班上点一次「选课」让它失败 → 找到 choiceCourse 那条")
        logger.error("      → 同样 Copy as cURL → 替换 SUBMIT_CURL")
        logger.error("")
        logger.error("   两段都要换：csrfToken 和 Cookie 是一起失效的。")
        return 2
    except (OSError, IOError) as e:
        logger.error("❌ 网络请求失败：%s", e)
        logger.error("   检查一下能不能正常打开 https://yjsxk.sjtu.edu.cn")
        return 4
    except ValueError as e:
        logger.error("❌ 响应解析失败：%s", e)
        logger.error("   多半是抓错了接口，或者返回了登录页而不是 JSON。")
        return 5


if __name__ == "__main__":
    sys.exit(main())
