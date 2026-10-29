import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import sys
import urllib.error
sys.path.insert(0,str(Path(__file__).parents[1]/'TrendRadar'))
from trendradar.ai.primary_materials import PrimaryEnricher,allowed_url,MAX_BYTES


class Response(io.BytesIO):
    def __init__(self,body,kind='text/html'):
        super().__init__(body.encode());self.status=200;self.headers={'Content-Type':kind}


class PrimaryTests(unittest.TestCase):
    def test_only_allowed_https_hosts_and_no_credentials(self):
        for url in ('http://openai.com/a','https://openai.com.evil.test/a','https://127.0.0.1/a','https://user:pass@openai.com/a','https://openai.com:8443/a','https://[::1]/a','https://unknown.test/a'):
            self.assertFalse(allowed_url(url))
        self.assertTrue(allowed_url('https://openai.com/index/research'))
        self.assertTrue(allowed_url('https://docs.vllm.ai/en/latest/'))
    def test_discovery_fetch_and_cache_bounded(self):
        class Opener:
            calls=[]
            def open(self,req,timeout):
                self.calls.append(req.full_url)
                if req.full_url.endswith('robots.txt'): return Response('User-agent: *\nAllow: /','text/plain')
                if 'ithome' in req.full_url: return Response('<p><a href="https://docs.vllm.ai/en/latest/serving/">vLLM 官方文档</a></p>')
                return Response('<title>vLLM serving</title><script>evil</script><p>'+('Version deployment performance. '*10)+'</p>')
        with tempfile.TemporaryDirectory() as directory,patch('trendradar.ai.primary_materials.check_public_dns'):
            opener=Opener();enricher=PrimaryEnricher(directory,opener=opener)
            row={'title':'vLLM部署新版本','url':'https://www.ithome.com/1/001/123.htm'}
            result=enricher.enrich([row])[0]
            self.assertEqual(result['primary_status'],'fetched')
            self.assertNotIn('evil',result['primary_materials'][0]['text'])
            calls=len(opener.calls)
            self.assertEqual(enricher.enrich([row])[0]['primary_status'],'cached')
            self.assertEqual(len(opener.calls),calls)
            self.assertLessEqual(calls,4)
    def test_robots_budget_network_failure_do_not_block_articles(self):
        class Fail:
            def open(self,*args,**kwargs): raise TimeoutError('private URL omitted')
        with tempfile.TemporaryDirectory() as directory,patch('trendradar.ai.primary_materials.check_public_dns'):
            rows=[{'title':'vLLM版本','url':'https://docs.vllm.ai/en/latest/'}]*8
            enricher=PrimaryEnricher(directory,max_articles=3,max_requests=2,opener=Fail())
            result=enricher.enrich(rows)
            self.assertEqual(len(result),8)
            self.assertTrue(all(not row['primary_materials'] for row in result))
            self.assertEqual([r['primary_status'] for r in result[3:]],['not_selected']*5)
            self.assertLessEqual(enricher.requests,2)
    def test_redirect_to_private_host_not_followed_and_oversize_refused(self):
        class Redirect:
            def open(self,req,timeout):
                if req.full_url.endswith('robots.txt'): return Response('User-agent: *\nAllow: /','text/plain')
                raise urllib.error.HTTPError(req.full_url,302,'redirect',{'Location':'https://127.0.0.1/private'},None)
        class Huge:
            def open(self,req,timeout):
                if req.full_url.endswith('robots.txt'): return Response('User-agent: *\nAllow: /','text/plain')
                return Response('x'*(MAX_BYTES+1))
        with tempfile.TemporaryDirectory() as directory,patch('trendradar.ai.primary_materials.check_public_dns'):
            for opener in (Redirect(),Huge()):
                result=PrimaryEnricher(directory,opener=opener).enrich([{'title':'AI研究','url':'https://openai.com/index/research'}])
                self.assertEqual(result[0]['primary_status'],'unavailable')
    def test_robots_disallow_does_not_read_page(self):
        class Block:
            calls=[]
            def open(self,req,timeout):
                self.calls.append(req.full_url)
                return Response('User-agent: *\nDisallow: /','text/plain')
        with tempfile.TemporaryDirectory() as directory,patch('trendradar.ai.primary_materials.check_public_dns'):
            opener=Block()
            result=PrimaryEnricher(directory,opener=opener).enrich([{'title':'AI研究','url':'https://openai.com/index/research'}])
            self.assertEqual(result[0]['primary_status'],'unavailable')
            self.assertEqual(len(opener.calls),1)

    def test_pinned_tcp_and_hostname_validation_use_different_intended_values(self):
        from trendradar.ai.primary_materials import PinnedHTTPSConnection
        from unittest.mock import Mock
        connection=PinnedHTTPSConnection('docs.vllm.ai','8.8.8.8',2)
        sock=Mock();wrapped=Mock()
        with patch('trendradar.ai.primary_materials.socket.create_connection',return_value=sock) as create, patch.object(connection._context,'wrap_socket',return_value=wrapped) as tls:
            connection.connect()
            self.assertEqual(create.call_args.args[0],('8.8.8.8',443))
            self.assertEqual(tls.call_args.kwargs['server_hostname'],'docs.vllm.ai')
            self.assertTrue(connection._context.check_hostname)
        connection.close()

    def test_dns_private_address_and_deadline_are_rejected(self):
        from trendradar.ai.primary_materials import check_public_dns
        import socket,time
        with patch('trendradar.ai.primary_materials.socket.getaddrinfo',return_value=[(socket.AF_INET,socket.SOCK_STREAM,6,'',('127.0.0.1',443))]):
            with self.assertRaises(ValueError): check_public_dns('openai.com',.1)
        with patch('trendradar.ai.primary_materials.socket.getaddrinfo',side_effect=lambda *a,**k:time.sleep(.05)):
            start=time.monotonic()
            with self.assertRaises(TimeoutError): check_public_dns('openai.com',.01)
            self.assertLess(time.monotonic()-start,.04)
