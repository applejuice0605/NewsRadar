"""Bounded primary-material discovery with allowlisted HTTPS and safe degradation."""
from __future__ import annotations
import hashlib
import http.client
import queue
import ssl
import threading
import ipaddress
import json
from html.parser import HTMLParser
from datetime import datetime, timezone
from pathlib import Path
import re
import socket
import time
import urllib.error
import urllib.request
import urllib.robotparser
from urllib.parse import urljoin, urlsplit

PRIMARY_HOSTS = {'openai.com','anthropic.com','deepseek.com','github.com','arxiv.org',
                 'huggingface.co','blog.google','ai.google.dev','developers.googleblog.com',
                 'developer.nvidia.com','docs.vllm.ai','vllm.ai','ai.meta.com','microsoft.com',
                 'learn.microsoft.com','developer.huawei.com','cloud.tencent.com','artificialanalysis.ai'}
PUBLISHER_HOSTS = {'ithome.com','www.ithome.com','juejin.cn','www.v2ex.com','v2ex.com'}
MAX_BYTES = 512_000
USER_AGENT = 'NewsRadarEvidence/1.0'


class TextLinks(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.skip = 0
        self.title = False
        self.title_parts, self.parts, self.links = [], [], []
        self.anchor = None
    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if tag in ('script','style','nav','footer','noscript'):
            self.skip += 1
        if tag == 'title': self.title = True
        if tag == 'a' and not self.skip: self.anchor = [values.get('href',''),[]]
        if tag in ('p','br','h1','h2','li'): self.parts.append('\n')
    def handle_endtag(self, tag):
        if tag == 'a' and self.anchor:
            self.links.append((self.anchor[0],''.join(self.anchor[1])))
            self.anchor = None
        if tag in ('script','style','nav','footer','noscript') and self.skip: self.skip -= 1
        if tag == 'title': self.title = False
    def handle_data(self, value):
        if self.title: self.title_parts.append(value)
        if not self.skip:
            self.parts.append(value)
            if self.anchor: self.anchor[1].append(value)
    def text(self):
        return '\n'.join(filter(None,(re.sub(r'\s+',' ',p).strip() for p in ''.join(self.parts).splitlines())))


def allowed_url(url, publisher=False):
    try:
        p = urlsplit(url)
        if p.scheme != 'https' or not p.hostname or p.username or p.password or p.port not in (None,443):
            return False
        host = p.hostname.lower()
        allowed = PRIMARY_HOSTS | (PUBLISHER_HOSTS if publisher else set())
        return host in allowed or any(host.endswith('.'+h) for h in PRIMARY_HOSTS if h not in {'github.com','arxiv.org','huggingface.co'})
    except (ValueError, TypeError):
        return False


def check_public_dns(host, timeout=4):
    # A daemon resolver cannot hold the publication process open after deadline.
    result = queue.Queue(maxsize=1)
    def resolve():
        try: result.put(socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM))
        except Exception as exc: result.put(exc)
    threading.Thread(target=resolve, daemon=True).start()
    try: addresses = result.get(timeout=max(.01, timeout))
    except queue.Empty: raise TimeoutError('dns_deadline') from None
    if isinstance(addresses, Exception): raise ValueError('dns_failed') from None
    ips = list(dict.fromkeys(a[4][0] for a in addresses))
    if not ips or any(not ipaddress.ip_address(ip).is_global for ip in ips):
        raise ValueError('non_public_address')
    return ips


class PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, host, ip, timeout):
        super().__init__(host, timeout=timeout, context=ssl.create_default_context())
        self.public_ip = ip
    def connect(self):
        # No second hostname lookup: TCP goes to the validated address while
        # SNI and certificate hostname verification still use the original host.
        sock = socket.create_connection((self.public_ip, 443), self.timeout)
        try: self.sock = self._context.wrap_socket(sock, server_hostname=self.host)
        except Exception:
            sock.close()
            raise


class PinnedResponse:
    def __init__(self, url, ip, timeout, deadline):
        p = urlsplit(url)
        self.connection = PinnedHTTPSConnection(p.hostname, ip, timeout)
        self.deadline = deadline
        def expire():
            sock = self.connection.sock
            if sock:
                try: sock.shutdown(socket.SHUT_RDWR)
                except OSError: pass
            self.connection.close()
        self.timer = threading.Timer(max(.01, deadline-time.monotonic()), expire)
        self.timer.daemon = True
        self.timer.start()
        try:
            path = p.path or '/'
            if p.query: path += '?' + p.query
            self.connection.request('GET', path, headers={'User-Agent': USER_AGENT, 'Accept': 'text/html,text/plain', 'Accept-Encoding': 'identity'})
            self.response = self.connection.getresponse()
            self.status, self.headers = self.response.status, self.response.headers
        except Exception:
            self.timer.cancel(); self.connection.close()
            raise
    def read(self, size):
        if time.monotonic() >= self.deadline: raise TimeoutError('read_deadline')
        return self.response.read1(size)
    def __enter__(self): return self
    def __exit__(self, *args): self.close()
    def close(self):
        self.timer.cancel(); self.response.close(); self.connection.close()


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class PrimaryEnricher:
    def __init__(self, cache_dir, *, max_articles=5, max_requests=12, budget_seconds=20, timeout=4, opener=None):
        self.cache_dir = Path(cache_dir)
        self.max_articles = min(max(0,int(max_articles)),5)
        self.max_requests = min(max(0,int(max_requests)),16)
        self.deadline = time.monotonic()+min(max(0,budget_seconds),30)
        self.timeout = min(max(0.1,timeout),5)
        self.requests = 0
        self.opener = opener
        self.robots = {}

    def _read(self,url,publisher=False):
        for _ in range(3):
            if not allowed_url(url,publisher) or self.requests >= self.max_requests or time.monotonic() >= self.deadline:
                raise ValueError('blocked_or_budget_exhausted')
            parsed=urlsplit(url)
            ips = check_public_dns(parsed.hostname, min(self.timeout, max(.01, self.deadline-time.monotonic())))
            if time.monotonic() >= self.deadline: raise TimeoutError('dns_exceeded_budget')
            self.requests += 1
            req=urllib.request.Request(url,headers={'User-Agent':USER_AGENT,'Accept':'text/html,text/plain'})
            try:
                timeout=min(self.timeout,max(.01,self.deadline-time.monotonic()))
                response=(self.opener.open(req,timeout=timeout) if self.opener else
                          PinnedResponse(url, ips[0], timeout, self.deadline))
                if response.status in (301,302,303,307,308):
                    url=urljoin(url,response.headers.get('Location',''))
                    response.close()
                    continue
            except urllib.error.HTTPError as error:
                if error.code in (301,302,303,307,308):
                    url=urljoin(url,error.headers.get('Location',''))
                    error.close()
                    continue
                raise
            with response:
                if response.status != 200:
                    code=response.status;headers=response.headers;response.close()
                    raise urllib.error.HTTPError(url,code,'http_status',headers,None)
                kind=response.headers.get('Content-Type','').lower()
                if not (kind.startswith('text/html') or kind.startswith('text/plain')): raise ValueError('unsupported_type')
                if response.headers.get('Content-Encoding','identity') not in ('','identity'): raise ValueError('unsupported_encoding')
                chunks=[];size=0
                while size<=MAX_BYTES:
                    if time.monotonic()>=self.deadline: raise ValueError('deadline_exceeded')
                    chunk=response.read(min(4096,MAX_BYTES+1-size))
                    if not chunk: break
                    chunks.append(chunk);size+=len(chunk)
                body=b''.join(chunks)
                if len(body)>MAX_BYTES: raise ValueError('response_too_large')
                return url, body.decode('utf-8',errors='replace')
        raise ValueError('redirect_limit')

    def _robot_allowed(self,url,publisher=False):
        p=urlsplit(url);origin='https://'+p.netloc
        if origin not in self.robots:
            try:
                _,text=self._read(origin+'/robots.txt',publisher)
                robot=urllib.robotparser.RobotFileParser();robot.parse(text.splitlines())
                self.robots[origin]=robot
            except urllib.error.HTTPError as error:
                if error.code in (404,410): self.robots[origin]=True
                else: self.robots[origin]=False
            except Exception:
                self.robots[origin]=False
        robot=self.robots[origin]
        return robot if isinstance(robot,bool) else robot.can_fetch(USER_AGENT,url)

    def _page(self,url,publisher=False):
        if not allowed_url(url,publisher) or not self._robot_allowed(url,publisher): raise ValueError('blocked_by_policy_or_robots')
        actual,text=self._read(url,publisher)
        parsed=TextLinks();parsed.feed(text)
        return actual,parsed

    def enrich(self,articles):
        output=[]
        attempted=0
        for original in articles:
            article=dict(original);article['primary_materials']=[]
            article['primary_status']='not_selected'
            if attempted>=self.max_articles:
                output.append(article);continue
            attempted+=1
            url=article.get('url','')
            candidates=[]
            try:
                key=hashlib.sha256(url.encode()).hexdigest()
                cache=self.cache_dir/(key+'.json')
                if cache.exists():
                    saved=json.loads(cache.read_text())
                    materials=saved.get('materials',[])
                    if time.time()-saved.get('saved_at',0)<21600 and materials and all(allowed_url(m.get('url','')) for m in materials):
                        article['primary_materials']=materials;article['primary_status']='cached';output.append(article);continue
                if allowed_url(url):
                    candidates=[url]
                elif allowed_url(url,True):
                    actual,page=self._page(url,True)
                    for link,label in page.links:
                        target=urljoin(actual,link)
                        path=urlsplit(target).path
                        if (allowed_url(target) and len(path.strip('/'))>3
                                and (re.search(r'官方|论文|项目|github|arxiv|报告|原文|source|release|research',label,re.I)
                                     or any(w in label.casefold() for w in re.findall(r'[a-zA-Z]{3,}',article['title'])))):
                            candidates.append(target)
                for target in dict.fromkeys(candidates):
                    try:
                        actual,page=self._page(target)
                        text=page.text()
                        if len(text)<100: continue
                        title=''.join(page.title_parts).strip()[:300]
                        article['primary_materials']=[{'url':actual,'title':title,'text':text[:5000],
                            'fetched_at':datetime.now(timezone.utc).isoformat(timespec='seconds'),
                            'kind':'project_primary' if urlsplit(actual).hostname in ('github.com','huggingface.co','arxiv.org') else 'publisher_primary',
                            'relation':'collected_link_candidate','verification':'原站材料候选，需核对与新闻的具体关联；不等于事实独立核验'}]
                        article['primary_status']='fetched'
                        self.cache_dir.mkdir(parents=True,exist_ok=True)
                        cache.write_text(json.dumps({'saved_at':time.time(),'materials':article['primary_materials']},ensure_ascii=False))
                        break
                    except Exception:
                        continue
                if not article['primary_materials']: article['primary_status']='unavailable'
            except Exception:
                article['primary_status']='unavailable'
            output.append(article)
        return output
