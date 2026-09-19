#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
SJTU 研究生选课 · 接口版回流票监控脚本
=====================================

与 Selenium 版的区别：不开浏览器、不渲染页面，直接重放你抓到的后端接口，
单轮耗时从 2~5 秒降到几十毫秒，这是抢回流票唯一有意义的优化方向。

工作方式
--------
脚本不"猜"学校的接口长什么样，而是直接吃你从浏览器里复制出来的请求：

  1. 打开选课页面，F12 → Network，正常点一次「查询」，
     在请求列表里找到返回课程 JSON 的那条 → 右键 → Copy → Copy as cURL (bash)
     → 保存为 query.curl

  2. 在目标课程（满员状态）上点一次「选课」，让它失败，
     把这条提交请求同样 Copy as cURL (bash) → 保存为 submit.curl
     （关键：抓的就是目标课程本身的提交请求，所以请求体里的课程号/教学班号
       已经是对的，脚本只要原样重放即可，不需要理解任何字段。）

  3. 先探测，确认能解析出余量：
        python sjtu_grab.py probe --keyword MARX6007
  4. 确认无误后开跑：
        python sjtu_grab.py run --keyword MARX6007

注意
----
- Cookie 会过期。脚本会检测到"掉登录"并立即告警退出，重新抓一次 query.curl 即可。
- 默认轮询间隔 1 秒。不要把它调到几十毫秒去打学校的服务器：既容易被限流/风控
  （反而彻底抢不到），也是给别人添麻烦。遇到 429/5xx 脚本会自动退避。
- 脚本只会重放你提供的 submit 请求，永远不会调用退选接口。
"""

from __future__ import annotations

import argparse
import codecs
import json
import logging
import os
import random
import re
import shlex
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Iterator
from urllib.parse import urlsplit

try:
    import requests
    from requests.adapters import HTTPAdapter
except ImportError:  # pragma: no cover
    sys.exit("缺少依赖，请先执行: pip install requests")


# ================= 日志 =================

logger = logging.getLogger("grab")


def setup_logging(verbose: bool, logfile: str = "sjtu_grab.log") -> None:
    logger.setLevel(logging.DEBUG if verbose else logging.INFO)
    fmt = logging.Formatter("%(asctime)s.%(msecs)03d %(levelname)s %(message)s", datefmt="%H:%M:%S")
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    logger.addHandler(sh)
    fh = logging.FileHandler(logfile, encoding="utf-8")
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(fh)


# ================= cURL 解析 =================

@dataclass
class CapturedRequest:
    """一条从浏览器复制出来的请求。"""
    method: str
    url: str
    headers: dict[str, str] = field(default_factory=dict)
    cookies: dict[str, str] = field(default_factory=dict)
    body: str | None = None

    @property
    def origin(self) -> str:
        p = urlsplit(self.url)
        return f"{p.scheme}://{p.netloc}"


# Chrome 在请求体含特殊字符时会输出 $'...' 形式的 ANSI-C 引用，shlex 处理不了，
# 先抠出来单独解码再塞回去。
_ANSI_C = re.compile(r"\$'((?:\\.|[^'\\])*)'", re.S)
# requests 会自行计算/处理的头，保留反而出错
_DROP_HEADERS = {"content-length", "host", "connection"}


def parse_curl(text: str) -> CapturedRequest:
    placeholders: dict[str, str] = {}

    def _stash(m: re.Match) -> str:
        key = f"__ANSIC_{len(placeholders)}__"
        placeholders[key] = codecs.decode(m.group(1), "unicode_escape")
        return f"'{key}'"

    text = _ANSI_C.sub(_stash, text)
    text = text.replace("\\\n", " ").replace("^\n", " ")

    try:
        tokens = shlex.split(text)
    except ValueError as e:
        raise ValueError(f"cURL 文本无法解析（请确认复制的是 bash 格式）: {e}") from e

    def restore(s: str) -> str:
        return placeholders.get(s, s)

    if not tokens or tokens[0] != "curl":
        raise ValueError("文件不是以 'curl' 开头，请使用 Copy as cURL (bash)")

    method: str | None = None
    url: str | None = None
    headers: dict[str, str] = {}
    cookies: dict[str, str] = {}
    bodies: list[str] = []

    i = 1
    while i < len(tokens):
        tok = tokens[i]
        if tok in ("-X", "--request"):
            method = tokens[i + 1].upper()
            i += 2
        elif tok in ("-H", "--header"):
            raw = restore(tokens[i + 1])
            if ":" in raw:
                k, v = raw.split(":", 1)
                k, v = k.strip(), v.strip()
                if k.lower() == "cookie":
                    cookies.update(_parse_cookie_header(v))
                elif k.lower() not in _DROP_HEADERS and not k.startswith(":"):
                    headers[k] = v
            i += 2
        elif tok in ("-b", "--cookie"):
            cookies.update(_parse_cookie_header(restore(tokens[i + 1])))
            i += 2
        elif tok in ("-d", "--data", "--data-raw", "--data-binary", "--data-ascii", "--data-urlencode"):
            bodies.append(restore(tokens[i + 1]))
            i += 2
        elif tok in ("-A", "--user-agent"):
            headers["User-Agent"] = restore(tokens[i + 1])
            i += 2
        elif tok in ("-e", "--referer"):
            headers["Referer"] = restore(tokens[i + 1])
            i += 2
        elif tok == "--url":
            url = restore(tokens[i + 1])
            i += 2
        elif tok.startswith("-"):
            # --compressed / -s / -k / --insecure 之类的开关，忽略
            i += 1
        else:
            if url is None:
                url = restore(tok)
            i += 1

    if not url:
        raise ValueError("cURL 里没找到 URL")

    body = "&".join(bodies) if bodies else None
    if method is None:
        method = "POST" if body is not None else "GET"
    return CapturedRequest(method=method, url=url, headers=headers, cookies=cookies, body=body)


def _parse_cookie_header(value: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for part in value.split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        k, v = part.split("=", 1)
        out[k.strip()] = v.strip()
    return out


def load_curl_file(path: str) -> CapturedRequest:
    with open(path, encoding="utf-8") as f:
        return parse_curl(f.read())


# ================= 响应解析：找到目标课程那条记录 =================

def walk_dicts(obj: Any, path: str = "") -> Iterator[tuple[str, dict]]:
    if isinstance(obj, dict):
        yield path, obj
        for k, v in obj.items():
            yield from walk_dicts(v, f"{path}.{k}" if path else k)
    elif isinstance(obj, list):
        for idx, v in enumerate(obj):
            yield from walk_dicts(v, f"{path}[{idx}]")


def _scalars(d: dict) -> Iterator[str]:
    for v in d.values():
        if isinstance(v, (str, int, float)) and not isinstance(v, bool):
            yield str(v)


def find_records(payload: Any, keyword: str) -> list[tuple[str, dict]]:
    """找出所有"字面量字段里含关键词"的最内层 dict，即课程/教学班记录。"""
    kw = keyword.lower()
    hits = [(p, d) for p, d in walk_dicts(payload) if any(kw in s.lower() for s in _scalars(d))]
    # 父节点也会命中，只保留最深的那层
    leaves = []
    for p, d in hits:
        if not any(q != p and q.startswith(p) and q[len(p):len(p) + 1] in (".", "[") for q, _ in hits):
            leaves.append((p, d))
    return leaves


# 字段名启发式（顺序有意义：先剩余，再容量/已选）
_REM_PAT = re.compile(r"(syrs|kyrs|sy_rs|kxrs|剩余|余量|remain|surplus|available|free)", re.I)
_CAP_PAT = re.compile(r"(kcrl|kkrl|jxbrl|容量|capacity|zrs|total|maxnum|max_num|upperlimit|limit)", re.I)
_SEL_PAT = re.compile(r"(yxrs|yxzrs|xkrs|yx_rs|已选|selected|selectnum|enrolled)", re.I)
_COMBO_PAT = re.compile(r"^\s*(\d+)\s*/\s*(\d+)\s*$")


def _as_int(v: Any) -> int | None:
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v
    if isinstance(v, float) and v.is_integer():
        return int(v)
    if isinstance(v, str) and re.fullmatch(r"\s*-?\d+\s*", v):
        return int(v.strip())
    return None


@dataclass
class Quota:
    remaining: int | None
    how: str  # 人类可读的推导说明，probe 模式下用来核对


def read_quota(record: dict, cfg: dict) -> Quota:
    """从一条记录里推导剩余名额。配置可显式指定字段名，否则用启发式。"""
    f_rem = cfg.get("remaining_field")
    f_cap = cfg.get("capacity_field")
    f_sel = cfg.get("selected_field")

    if f_rem and f_rem in record:
        v = _as_int(record[f_rem])
        if v is not None:
            return Quota(v, f"{f_rem}={v}")
    if f_cap and f_sel and f_cap in record and f_sel in record:
        cap, sel = _as_int(record[f_cap]), _as_int(record[f_sel])
        if cap is not None and sel is not None:
            return Quota(cap - sel, f"{f_cap}={cap} - {f_sel}={sel}")
    if f_rem or (f_cap and f_sel):
        return Quota(None, "配置指定的字段在记录里不存在或不是数字")

    for k, v in record.items():
        if _REM_PAT.search(k) and (n := _as_int(v)) is not None:
            return Quota(n, f"启发式: 剩余字段 {k}={n}")

    cap = sel = None
    cap_k = sel_k = ""
    for k, v in record.items():
        n = _as_int(v)
        if n is None:
            continue
        if cap is None and _CAP_PAT.search(k):
            cap, cap_k = n, k
        elif sel is None and _SEL_PAT.search(k):
            sel, sel_k = n, k
    if cap is not None and sel is not None:
        return Quota(cap - sel, f"启发式: {cap_k}={cap} - {sel_k}={sel}")

    for k, v in record.items():
        if isinstance(v, str) and (m := _COMBO_PAT.match(v)):
            a, b = int(m.group(1)), int(m.group(2))
            if cfg.get("combo_order", "selected/capacity") == "capacity/selected":
                return Quota(a - b, f"启发式: {k}='{v}' 视为 容量/已选")
            return Quota(b - a, f"启发式: {k}='{v}' 视为 已选/容量")

    return Quota(None, "无法识别余量字段，请在配置里显式指定 remaining_field 或 capacity_field+selected_field")


def label_of(record: dict, keyword: str) -> str:
    """给日志用的简短标识：课程名 + 教学班/教师之类。"""
    picks = []
    for k, v in record.items():
        if not isinstance(v, str) or not v.strip() or len(v) > 40:
            continue
        if re.search(r"(kcmc|课程名|name|jxbmc|教学班|teacher|jsxm|skjs)", k, re.I):
            picks.append(v.strip())
        if len(picks) >= 3:
            break
    ident = " / ".join(dict.fromkeys(picks))
    return f"{keyword}{' · ' + ident if ident else ''}"


# ================= 会话 =================

def build_session(cap: CapturedRequest) -> requests.Session:
    s = requests.Session()
    s.headers.update(cap.headers)
    for k, v in cap.cookies.items():
        s.cookies.set(k, v)
    adapter = HTTPAdapter(pool_connections=8, pool_maxsize=16, max_retries=0)
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    return s


def clone_session(src: requests.Session) -> requests.Session:
    s = requests.Session()
    s.headers.update(src.headers)
    s.cookies.update(src.cookies)
    adapter = HTTPAdapter(pool_connections=4, pool_maxsize=8, max_retries=0)
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    return s


def send(sess: requests.Session, cap: CapturedRequest, timeout: float,
         url: str | None = None, body: str | None = None) -> requests.Response:
    return sess.request(
        cap.method,
        url if url is not None else cap.url,
        data=(body if body is not None else cap.body),
        timeout=timeout,
        allow_redirects=False,  # 掉登录时会 302 到认证页，不跟随才好识别
    )


_LOGIN_HINTS = re.compile(r"(未登录|登录超时|请重新登录|统一身份认证|jaccount|login\.html|session.*(过期|失效))", re.I)


def looks_logged_out(resp: requests.Response) -> bool:
    if resp.status_code in (301, 302, 303, 307, 401, 403):
        return True
    ctype = resp.headers.get("Content-Type", "")
    text = resp.text[:2000]
    if "json" not in ctype.lower() and "<html" in text.lower():
        return True
    return bool(_LOGIN_HINTS.search(text))


# ================= 提交 =================

_PLACEHOLDER = re.compile(r"\{\{(\w+)\}\}")


def render(template: str | None, record: dict) -> str | None:
    """把 submit.curl 里手写的 {{字段名}} 用记录里的值替换掉。没有占位符就原样返回。"""
    if template is None:
        return None

    def sub(m: re.Match) -> str:
        key = m.group(1)
        if key not in record:
            raise KeyError(f"submit 模板里的 {{{{{key}}}}} 在课程记录中不存在")
        return str(record[key])

    return _PLACEHOLDER.sub(sub, template)


_FAIL_PAT = re.compile(r"(失败|已满|满员|人数已|超过|不允许|不能|冲突|已选过|重复|无效|错误|error|false)", re.I)
_OK_PAT = re.compile(r"(成功|success|\"code\"\s*:\s*\"?(1|0|200)\"?)", re.I)


def judge(text: str, cfg: dict) -> tuple[bool, str]:
    """判定提交结果。先判失败再判成功——很多系统失败响应里也带 code:1。"""
    snippet = text.strip()[:400]
    fail_pat = re.compile(cfg["fail_pattern"], re.I) if cfg.get("fail_pattern") else _FAIL_PAT
    ok_pat = re.compile(cfg["success_pattern"], re.I) if cfg.get("success_pattern") else _OK_PAT
    if fail_pat.search(snippet):
        return False, snippet
    if ok_pat.search(snippet):
        return True, snippet
    return False, snippet


def attempt_submit(sess: requests.Session, cap: CapturedRequest, record: dict,
                   cfg: dict, timeout: float) -> tuple[bool, str]:
    url = render(cap.url, record) or cap.url
    body = render(cap.body, record)
    resp = send(sess, cap, timeout, url=url, body=body)
    if looks_logged_out(resp):
        return False, "[掉登录] " + resp.text[:200]
    return judge(resp.text, cfg)


def burst_submit(main: requests.Session, cap: CapturedRequest, record: dict,
                 cfg: dict, timeout: float) -> tuple[bool, str]:
    """抢到余量的瞬间连打几次。失败不等下一轮——这一秒就是全部窗口期。"""
    attempts = int(cfg.get("burst_attempts", 3))
    workers = max(1, int(cfg.get("burst_workers", 1)))
    gap = float(cfg.get("burst_gap", 0.15))

    if workers == 1:
        last = ""
        for i in range(attempts):
            try:
                ok, msg = attempt_submit(main, cap, record, cfg, timeout)
            except Exception as e:  # noqa: BLE001
                ok, msg = False, f"提交异常: {e}"
            logger.info("   提交 #%d → %s | %s", i + 1, "成功" if ok else "未成功", msg)
            if ok:
                return True, msg
            last = msg
            if i < attempts - 1:
                time.sleep(gap)
        return False, last

    sessions = [main] + [clone_session(main) for _ in range(workers - 1)]
    results: list[tuple[bool, str]] = []
    lock = threading.Lock()

    def worker(idx: int) -> None:
        try:
            ok, msg = attempt_submit(sessions[idx % workers], cap, record, cfg, timeout)
        except Exception as e:  # noqa: BLE001
            ok, msg = False, f"提交异常: {e}"
        with lock:
            results.append((ok, msg))
        logger.info("   提交 #%d → %s | %s", idx + 1, "成功" if ok else "未成功", msg)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(worker, range(attempts)))
    for ok, msg in results:
        if ok:
            return True, msg
    return False, results[-1][1] if results else "无响应"


# ================= 主流程 =================

DEFAULT_CFG: dict[str, Any] = {
    "keyword": "",
    "query_curl": "query.curl",
    "submit_curl": "submit.curl",
    "interval": 1.0,
    "jitter": 0.2,
    "timeout": 6.0,
    "burst_attempts": 3,
    "burst_workers": 1,
    "burst_gap": 0.15,
    "stop_on_success": True,
    "watch_only": False,
    # 下面这些留空就走启发式，probe 结果不对时再填
    "remaining_field": None,
    "capacity_field": None,
    "selected_field": None,
    "combo_order": "selected/capacity",
    "success_pattern": None,
    "fail_pattern": None,
}


def load_config(path: str | None) -> dict:
    cfg = dict(DEFAULT_CFG)
    if path and os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            cfg.update(json.load(f))
        logger.info("已加载配置: %s", path)
    return cfg


def poll_once(sess: requests.Session, cap: CapturedRequest, cfg: dict) -> Any:
    resp = send(sess, cap, float(cfg["timeout"]))
    if looks_logged_out(resp):
        raise PermissionError(f"登录态失效 (HTTP {resp.status_code})，请重新抓取 query.curl")
    resp.raise_for_status()
    try:
        return resp.json()
    except ValueError:
        try:
            return json.loads(resp.text)
        except ValueError as e:
            raise ValueError(f"响应不是 JSON: {resp.text[:200]}") from e


def cmd_probe(cfg: dict) -> int:
    query = load_curl_file(cfg["query_curl"])
    logger.info("查询接口: %s %s", query.method, query.url)
    logger.info("请求头 %d 个，Cookie %d 个，请求体 %s",
                len(query.headers), len(query.cookies),
                f"{len(query.body)} 字节" if query.body else "无")

    sess = build_session(query)
    t0 = time.perf_counter()
    payload = poll_once(sess, query, cfg)
    logger.info("单轮耗时: %.0f ms", (time.perf_counter() - t0) * 1000)

    records = find_records(payload, cfg["keyword"])
    if not records:
        logger.error("在响应里没找到含 '%s' 的记录。可能原因：", cfg["keyword"])
        logger.error("  · 抓的不是课程列表接口（换一条 XHR 再试）")
        logger.error("  · 关键词写错，或该接口返回的是分页数据、目标课程不在本页")
        preview = json.dumps(payload, ensure_ascii=False)[:1200]
        logger.error("响应预览: %s", preview)
        return 1

    logger.info("命中 %d 条记录：", len(records))
    for path, rec in records:
        q = read_quota(rec, cfg)
        logger.info("--- %s ---", path)
        logger.info("%s", json.dumps(rec, ensure_ascii=False, indent=2)[:2000])
        logger.info(">>> 余量推导: %s  →  剩余 %s", q.how, q.remaining)
        if q.remaining is None:
            logger.warning(">>> 请对照上面的字段，在配置里显式设置 remaining_field 或 capacity_field + selected_field")

    if os.path.exists(cfg["submit_curl"]):
        sub = load_curl_file(cfg["submit_curl"])
        logger.info("提交接口: %s %s", sub.method, sub.url)
        logger.info("提交请求体: %s", (sub.body or "")[:500])
        holes = set(_PLACEHOLDER.findall((sub.url or "") + (sub.body or "")))
        if holes:
            missing = [h for h in holes if h not in records[0][1]]
            logger.info("模板占位符: %s%s", ", ".join(sorted(holes)),
                        f"  ⚠ 记录中缺失: {missing}" if missing else "  ✓ 均可填充")
        else:
            logger.info("提交请求体无占位符，将原样重放（确认它抓的就是目标课程）")
    else:
        logger.warning("未找到 %s，run 模式需要它", cfg["submit_curl"])
    return 0


def wait_until(ts: str) -> None:
    now = datetime.now()
    try:
        h, m, s = (list(map(int, ts.split(":"))) + [0])[:3]
    except ValueError:
        raise SystemExit(f"--start-at 格式应为 HH:MM 或 HH:MM:SS，收到 {ts!r}")
    target = now.replace(hour=h, minute=m, second=s, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    delta = (target - now).total_seconds()
    logger.info("⏳ 等待至 %s 开抢（还有 %.0f 秒）...", target.strftime("%Y-%m-%d %H:%M:%S"), delta)
    while (delta := (target - datetime.now()).total_seconds()) > 0:
        time.sleep(min(delta, 1.0))


def cmd_run(cfg: dict) -> int:
    query = load_curl_file(cfg["query_curl"])
    submit = None if cfg["watch_only"] else load_curl_file(cfg["submit_curl"])
    sess = build_session(query)

    keyword = cfg["keyword"]
    interval = float(cfg["interval"])
    logger.info("🚀 开始监控 '%s' | 间隔 %.2fs | 模式 %s",
                keyword, interval, "仅观察" if cfg["watch_only"] else "自动抢")

    cycle = 0
    miss_streak = 0
    backoff = 0.0
    last_state: dict[str, int | None] = {}
    t_start = time.time()

    while True:
        cycle += 1
        tick = time.perf_counter()
        try:
            payload = poll_once(sess, query, cfg)
            backoff = 0.0
        except PermissionError as e:
            logger.error("❌ %s", e)
            return 2
        except requests.HTTPError as e:
            code = e.response.status_code if e.response is not None else 0
            backoff = min(30.0, backoff * 2 + 2) if code in (429, 500, 502, 503, 504) else 2.0
            logger.warning("HTTP %s，退避 %.1fs（频率过高会被风控，别再调低间隔）", code, backoff)
            time.sleep(backoff)
            continue
        except (requests.RequestException, ValueError) as e:
            backoff = min(15.0, backoff * 2 + 1)
            logger.warning("查询失败: %s，退避 %.1fs", e, backoff)
            time.sleep(backoff)
            continue

        records = find_records(payload, keyword)
        if not records:
            miss_streak += 1
            if miss_streak in (3, 10) or miss_streak % 50 == 0:
                logger.warning("⚠️ 连续 %d 轮没找到 '%s'。接口可能带了分页/筛选参数，"
                               "或课程已下架 —— 静默空转是抢不到课最常见的原因，请复查。", miss_streak, keyword)
            time.sleep(max(0.0, interval - (time.perf_counter() - tick)))
            continue
        miss_streak = 0

        for path, rec in records:
            q = read_quota(rec, cfg)
            name = label_of(rec, keyword)
            if q.remaining is None:
                if cycle == 1 or cycle % 100 == 0:
                    logger.warning("无法读出余量（%s）。请跑 probe 并在配置里指定字段。", q.how)
                continue

            if last_state.get(path) != q.remaining:
                logger.info("📊 %s 余量 %s → %s (%s)", name, last_state.get(path, "?"), q.remaining, q.how)
                last_state[path] = q.remaining

            if q.remaining > 0:
                logger.info("🔥 [%s] 出现回流票，余量 %d，立即提交！", name, q.remaining)
                if cfg["watch_only"]:
                    logger.info("   （仅观察模式，不提交）")
                    continue
                ok, msg = burst_submit(sess, submit, rec, cfg, float(cfg["timeout"]))
                if ok:
                    logger.info("🎉🎉 选课成功: %s | %s", name, msg)
                    logger.info("请立刻去系统里刷新页面确认，不要只信这一条日志。")
                    if cfg["stop_on_success"]:
                        return 0
                else:
                    logger.info("😢 本次未抢到（%s），继续盯。", msg[:120])

        if cycle % 60 == 0:
            logger.info("… 已监控 %d 轮 / %.1f 分钟，当前余量 %s",
                        cycle, (time.time() - t_start) / 60,
                        {p.split('.')[-1]: v for p, v in last_state.items()} or "未知")

        elapsed = time.perf_counter() - tick
        logger.debug("轮次 %d 耗时 %.0f ms", cycle, elapsed * 1000)
        time.sleep(max(0.0, interval - elapsed) + random.uniform(0, float(cfg["jitter"])))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="SJTU 选课回流票监控（接口版）")
    ap.add_argument("mode", choices=["probe", "run"], help="probe=探测并核对字段；run=开始监控")
    ap.add_argument("--keyword", help="目标课程关键词，如 MARX6007")
    ap.add_argument("--config", default="config.json", help="配置文件路径（可选）")
    ap.add_argument("--query-curl", help="查询接口的 cURL 文件（默认 query.curl）")
    ap.add_argument("--submit-curl", help="提交接口的 cURL 文件（默认 submit.curl）")
    ap.add_argument("--interval", type=float, help="轮询间隔秒，默认 1.0")
    ap.add_argument("--burst", type=int, help="命中后连续提交次数，默认 3")
    ap.add_argument("--workers", type=int, help="并发提交线程数，默认 1")
    ap.add_argument("--watch-only", action="store_true", help="只监控不提交，用来验证")
    ap.add_argument("--keep-going", action="store_true", help="抢到后不退出，继续监控")
    ap.add_argument("--start-at", help="定时开抢，格式 HH:MM[:SS]")
    ap.add_argument("-v", "--verbose", action="store_true", help="输出每轮耗时等调试信息")
    args = ap.parse_args(argv)

    setup_logging(args.verbose)
    cfg = load_config(args.config)
    for key, val in (
        ("keyword", args.keyword), ("query_curl", args.query_curl), ("submit_curl", args.submit_curl),
        ("interval", args.interval), ("burst_attempts", args.burst), ("burst_workers", args.workers),
    ):
        if val is not None:
            cfg[key] = val
    if args.watch_only:
        cfg["watch_only"] = True
    if args.keep_going:
        cfg["stop_on_success"] = False

    if not cfg["keyword"]:
        logger.error("必须指定目标课程关键词：--keyword MARX6007")
        return 1
    if not os.path.exists(cfg["query_curl"]):
        logger.error("找不到 %s —— 先按 README 抓取查询接口的 cURL。", cfg["query_curl"])
        return 1

    try:
        if args.mode == "probe":
            return cmd_probe(cfg)
        if args.start_at:
            wait_until(args.start_at)
        return cmd_run(cfg)
    except KeyboardInterrupt:
        logger.info("已手动停止。")
        return 130
    except FileNotFoundError as e:
        logger.error("%s", e)
        return 1
    except ValueError as e:
        logger.error("%s", e)
        return 1


if __name__ == "__main__":
    sys.exit(main())
