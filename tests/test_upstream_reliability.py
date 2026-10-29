"""Offline regressions for the bounded v6.10 -> v6.11 reliability backports."""

from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

import requests
from botocore.exceptions import ClientError, EndpointConnectionError

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "TrendRadar"))

# Importing trendradar also initializes LiteLLM; force its bundled price map.
with patch.dict(os.environ, {"LITELLM_LOCAL_MODEL_COST_MAP": "True"}), patch(
    "socket.socket.connect", side_effect=AssertionError("Network forbidden during imports")
), patch("socket.getaddrinfo", side_effect=AssertionError("DNS forbidden during imports")):
    from trendradar.crawler.rss.fetcher import RSSFeedConfig, RSSFetcher
    from trendradar.storage.remote import RemoteStorageBackend
    from trendradar.utils import time as time_utils


def setUpModule():
    # A missed mock must fail locally, never contact an RSS source or AWS.
    for target in ("socket.socket.connect", "socket.create_connection", "socket.getaddrinfo"):
        guard = patch(target, side_effect=AssertionError("Network forbidden in offline tests"))
        guard.start()
        unittest.addModuleCleanup(guard.stop)


def response(status=200, headers=None, text="<rss version='2.0'><channel/></rss>"):
    result = requests.Response()
    result.status_code = status
    result.headers.update(headers or {})
    result.url = "https://feeds.invalid/rss"
    result._content = text.encode()
    result.encoding = "utf-8"
    return result


def s3_error(code, status=None):
    payload = {"Error": {"Code": code, "Message": "offline fixture"}}
    if status is not None:
        payload["ResponseMetadata"] = {"HTTPStatusCode": status}
    return ClientError(payload, "HeadObject")


class RSSRetryTests(unittest.TestCase):
    def setUp(self):
        self.feed = RSSFeedConfig("offline", "Offline", "https://feeds.invalid/rss")
        self.fetcher = RSSFetcher([self.feed], timeout=7)
        self.addCleanup(self.fetcher.session.close)

    def fetch_with(self, outcomes):
        with patch.object(self.fetcher.session, "get", side_effect=outcomes) as get, patch(
            "trendradar.crawler.rss.fetcher.time.sleep"
        ) as sleep:
            result = self.fetcher.fetch_feed(self.feed)
        sleep.assert_not_called()
        for call in get.call_args_list:
            self.assertEqual(call.args, (self.feed.url,))
            self.assertEqual(call.kwargs, {"timeout": 7})
        return result, get.call_count

    def test_success_does_not_retry(self):
        self.assertEqual(self.fetch_with([response()]), (([], None), 1))

    def test_transient_transport_errors_retry_once_and_recover(self):
        for error_type in (requests.Timeout, requests.ConnectTimeout, requests.ReadTimeout,
                           requests.ConnectionError, requests.exceptions.ProxyError):
            with self.subTest(error=error_type.__name__):
                self.assertEqual(self.fetch_with([error_type("transient"), response()]),
                                 (([], None), 2))

    def test_only_selected_http_statuses_retry(self):
        for status in (408, 500, 502, 503, 504):
            with self.subTest(status=status):
                self.assertEqual(self.fetch_with([response(status), response()]),
                                 (([], None), 2))

    def test_permanent_and_unselected_http_errors_do_not_retry(self):
        for status in (400, 401, 403, 404, 410, 422, 429, 501, 505):
            with self.subTest(status=status):
                (items, error), calls = self.fetch_with([response(status), response()])
                self.assertEqual(calls, 1)
                self.assertEqual(items, [])
                self.assertIn(str(status), error)

    def test_ssl_and_other_request_errors_do_not_retry(self):
        for error in (requests.exceptions.SSLError("certificate"),
                      requests.exceptions.InvalidURL("bad URL"),
                      requests.exceptions.TooManyRedirects("redirect loop"),
                      requests.RequestException("unspecified"),
                      requests.HTTPError("missing response")):
            with self.subTest(error=type(error).__name__):
                (items, message), calls = self.fetch_with([error, response()])
                self.assertEqual((items, calls), ([], 1))
                self.assertIn(str(error), message)

    def test_retry_after_presence_blocks_immediate_retry(self):
        for status in (408, 429, 500, 502, 503, 504):
            for value in ("0", "120", "Wed, 21 Oct 2026 07:28:00 GMT", ""):
                with self.subTest(status=status, retry_after=value):
                    (_, error), calls = self.fetch_with([
                        response(status, {"retry-after": value}), response()
                    ])
                    self.assertEqual(calls, 1)
                    self.assertIn(str(status), error)
        for kind in (requests.Timeout, requests.ConnectionError):
            error = kind("server wait", response=response(503, {"Retry-After": "60"}))
            self.assertEqual(self.fetch_with([error, response()])[1], 1)

    def test_retry_budget_is_two_attempts_and_reports_final_error(self):
        for final in (requests.ReadTimeout("second timeout"),
                      requests.ConnectionError("second connection failure"), response(503),
                      response(403), requests.exceptions.SSLError("second certificate")):
            with self.subTest(final=str(final)):
                (items, error), calls = self.fetch_with([
                    requests.ConnectionError("first failure"), final, response()
                ])
                self.assertEqual((items, calls), ([], 2))
                self.assertTrue(error)
                self.assertNotIn("first failure", error)

    def test_parse_errors_never_retry_even_after_transport_recovery(self):
        for initial in ([], [requests.Timeout("transient")]):
            with self.subTest(recovered=bool(initial)):
                (items, error), calls = self.fetch_with(initial + [response(text="<broken")])
                self.assertEqual(items, [])
                self.assertIn("解析失败", error)
                self.assertEqual(calls, len(initial) + 1)
        # Even a request-shaped exception raised by parsing is outside the retry loop.
        with patch.object(self.fetcher.parser, "parse", side_effect=requests.Timeout("parser")):
            self.assertEqual(self.fetch_with([response(), response()])[1], 1)

    def test_custom_json_feed_acceptance_and_item_mapping_survive_retry(self):
        self.assertIn("application/feed+json", self.fetcher.session.headers["Accept"])
        self.feed.max_items = 1
        body = '''{"version":"https://jsonfeed.org/version/1.1","items":[
            {"id":"first","url":"https://news.invalid/1","title":"Old article",
             "date_published":"2000-01-01T00:00:00Z","summary":"Summary",
             "authors":[{"name":"Author"}]},
            {"id":"second","url":"https://news.invalid/2","title":"Second"}]}'''
        (items, error), calls = self.fetch_with([requests.Timeout(), response(text=body)])
        self.assertIsNone(error)
        self.assertEqual(calls, 2)
        self.assertEqual(len(items), 1)
        self.assertEqual((items[0].feed_id, items[0].guid, items[0].summary, items[0].author),
                         ("offline", "first", "Summary", "Author"))
        # Freshness filtering remains at delivery time; old articles are still stored.
        self.assertTrue(items[0].published_at.startswith("2000-01-01"))


class ISOTimeTests(unittest.TestCase):
    def test_equivalent_iso_representations_preserve_instant_and_fraction(self):
        now = datetime(2026, 10, 29, 12, 0, 0, 750000, tzinfo=timezone.utc)
        variants = ("2026-10-28T12:00:00.750000Z", "2026-10-28T12:00:00.750000+00:00",
                    "2026-10-28T07:00:00.750000-05:00", "2026-10-28T06:30:00.750000-05:30",
                    "2026-10-28T20:00:00.750000+08:00", "2026-10-28T12:00:00.750000",
                    "2026-10-28 12:00:00.750000")
        for value in variants:
            for zone in ("UTC", "Asia/Shanghai", "America/New_York"):
                with self.subTest(value=value, zone=zone), patch.object(
                    time_utils, "get_configured_time", return_value=now
                ) as clock:
                    parsed = time_utils._parse_iso_time(value)
                    self.assertEqual(parsed.microsecond, 750000)
                    self.assertEqual(parsed, now - timedelta(days=1))
                    self.assertEqual(time_utils.calculate_days_old(value, zone), 1.0)
                    self.assertTrue(time_utils.is_within_days(value, 1, zone))
                    clock.assert_called_with(zone)
                    self.assertEqual(time_utils.format_iso_time_friendly(value, "Asia/Shanghai"),
                                     "10-28 20:00")

    def test_subsecond_freshness_boundary(self):
        now = datetime(2026, 10, 29, 12, 0, 0, 500000, tzinfo=timezone.utc)
        with patch.object(time_utils, "get_configured_time", return_value=now):
            for fraction, expected in (("499999", False), ("500000", True), ("500001", True)):
                for prefix, suffix in (("12:00:00", ""), ("07:00:00", "-05:00"),
                                       ("12:00:00", "Z"), ("20:00:00", "+08:00")):
                    value = f"2026-10-28T{prefix}.{fraction}{suffix}"
                    with self.subTest(value=value):
                        self.assertEqual(time_utils.is_within_days(value, 1), expected)
                        expected_age = 1 + (500000 - int(fraction)) / 1_000_000 / 86400
                        self.assertAlmostEqual(time_utils.calculate_days_old(value), expected_age,
                                               places=14)

    def test_negative_offset_without_fraction_filters_old_articles(self):
        with patch.object(time_utils, "get_configured_time", return_value=datetime(
            2026, 10, 29, 12, tzinfo=timezone.utc
        )):
            value = "2026-10-25T07:00:00-05:00"
            self.assertEqual(time_utils.calculate_days_old(value), 4.0)
            self.assertFalse(time_utils.is_within_days(value, 3))

    def test_display_rollover_dst_and_unknown_timezone_fallback(self):
        cases = (("2026-10-28T23:30:00.5-05:00", "Asia/Shanghai", "10-29 12:30"),
                 ("2026-10-28T23:30:00", "UTC", "10-28 23:30"),
                 ("2026-10-28T23:30:00Z", "Unknown/Zone", "10-29 07:30"),
                 ("2026-11-01T01:30:00-04:00", "UTC", "11-01 05:30"),
                 ("2026-11-01T01:30:00-05:00", "UTC", "11-01 06:30"))
        for value, zone, expected in cases:
            with self.subTest(value=value, zone=zone):
                self.assertEqual(time_utils.format_iso_time_friendly(value, zone), expected)
                self.assertEqual(time_utils.format_iso_time_friendly(value, zone, False), expected[-5:])

    def test_missing_invalid_disabled_and_future_policies_remain(self):
        for value in ("", "invalid", "2026-10-28Tbad"):
            with self.subTest(value=value):
                self.assertIsNone(time_utils.calculate_days_old(value))
                self.assertTrue(time_utils.is_within_days(value, 1))
        self.assertEqual(time_utils.format_iso_time_friendly("invalid"), "invalid")
        self.assertEqual(time_utils.format_iso_time_friendly("2026-10-28Tbad"), "10-28 bad")
        self.assertEqual(time_utils.format_iso_time_friendly(""), "")
        for limit in (0, -1):
            self.assertTrue(time_utils.is_within_days("2000-01-01T00:00:00Z", limit))
        with patch.object(time_utils, "get_configured_time", return_value=datetime(
            2026, 10, 29, tzinfo=timezone.utc
        )):
            self.assertTrue(time_utils.is_within_days("2026-10-30T00:00:00Z", 1))
            self.assertEqual(time_utils.calculate_days_old("2026-10-30T00:00:00Z"), -1.0)


class RemoteExistenceTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.client = Mock()
        with patch("trendradar.storage.remote.boto3.client", return_value=self.client):
            self.backend = RemoteStorageBackend("offline-bucket", "dummy", "dummy",
                                                "https://storage.invalid", temp_dir=self.directory.name)
        self.addCleanup(self.backend.cleanup)

    def test_existing_object(self):
        self.assertTrue(self.backend._check_object_exists("news/2026-10-29.db"))
        self.client.head_object.assert_called_once_with(
            Bucket="offline-bucket", Key="news/2026-10-29.db"
        )

    def test_explicit_not_found_variants(self):
        for code, status in (("404", None), (404, None), ("NoSuchKey", None),
                             ("Not Found", None), ("NotFound", None), ("", 404)):
            with self.subTest(code=code, status=status):
                self.client.head_object.side_effect = s3_error(code, status)
                self.assertFalse(self.backend._check_object_exists("missing.db"))

    def test_other_errors_propagate_and_never_create_or_upload_empty_database(self):
        errors = [s3_error("AccessDenied", 403), s3_error("InvalidAccessKeyId", 403),
                  s3_error("SignatureDoesNotMatch", 403), s3_error("SlowDown", 503),
                  s3_error("TooManyRequests", 429), s3_error("InternalError", 500),
                  s3_error("", None), EndpointConnectionError(endpoint_url="https://storage.invalid"),
                  TimeoutError("offline timeout"), RuntimeError("unexpected failure")]
        for db_type in ("news", "rss"):
            for error in errors:
                with self.subTest(db_type=db_type, error=str(error)):
                    self.client.head_object.side_effect = error
                    with self.assertRaises(type(error)) as raised:
                        self.backend._get_connection("2026-10-29", db_type)
                    self.assertIs(raised.exception, error)
                    self.assertEqual(self.backend._db_connections, {})
                    self.assertEqual(list(Path(self.directory.name).rglob("*.db")), [])
                    self.client.get_object.assert_not_called()
                    self.client.put_object.assert_not_called()

    def test_explicit_missing_object_allows_new_database(self):
        self.client.head_object.side_effect = s3_error("NotFound", 404)
        for db_type, table in (("news", "news_items"), ("rss", "rss_items")):
            with self.subTest(db_type=db_type):
                conn = self.backend._get_connection("2026-10-29", db_type)
                self.assertEqual(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], 0)
                self.assertTrue(self.backend._get_local_db_path("2026-10-29", db_type).is_file())
        self.client.get_object.assert_not_called()
        self.client.put_object.assert_not_called()

    def test_existing_database_download_preserves_history(self):
        conn = sqlite3.connect(":memory:")
        self.addCleanup(conn.close)
        conn.execute("CREATE TABLE history (title TEXT)")
        conn.execute("INSERT INTO history VALUES ('existing article')")
        conn.commit()
        body = Mock()
        body.iter_chunks.return_value = [conn.serialize()]
        self.client.get_object.return_value = {"Body": body}
        downloaded = self.backend._get_connection("2026-10-29")
        self.assertEqual(downloaded.execute("SELECT title FROM history").fetchone()[0],
                         "existing article")
        self.client.put_object.assert_not_called()

    def test_get_object_failure_after_successful_head_also_blocks_creation(self):
        for error in (s3_error("AccessDenied", 403), s3_error("SlowDown", 503),
                      TimeoutError("download timeout")):
            with self.subTest(error=str(error)):
                self.client.get_object.side_effect = error
                with self.assertRaises(type(error)) as raised:
                    self.backend._get_connection("2026-10-29")
                self.assertIs(raised.exception, error)
                self.assertEqual(list(Path(self.directory.name).rglob("*.db")), [])
                self.assertEqual(self.backend._db_connections, {})
                self.client.put_object.assert_not_called()


if __name__ == "__main__":
    unittest.main()
