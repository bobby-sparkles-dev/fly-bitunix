"""Rate-limit handling is fail-fast and obeys venue instructions."""
from unittest.mock import patch
from urllib.error import HTTPError
import io
import pytest

from btc_edge.market import RateLimited, fetch_page


def test_429_honors_retry_after_without_sleeping():
    error=HTTPError("https://example.test",429,"Too Many Requests",
                    {"Retry-After":"120"},io.BytesIO(b"{}"))
    warnings=[]
    with patch("btc_edge.market.urllib.request.urlopen",side_effect=error) as urlopen:
        with patch("btc_edge.market.time.sleep",side_effect=AssertionError("unexpected sleep")):
            with pytest.raises(RateLimited) as caught:
                fetch_page("BTCUSDT",0,300000,on_retry=warnings.append)
    assert caught.value.retry_seconds==120
    assert urlopen.call_count==1
    assert warnings[0]["code"]==429


def test_418_has_long_cooldown():
    error=HTTPError("https://example.test",418,"Blocked",{},io.BytesIO(b"{}"))
    with patch("btc_edge.market.urllib.request.urlopen",side_effect=error):
        with pytest.raises(RateLimited) as caught:
            fetch_page("BTCUSDT",0,300000)
    assert caught.value.retry_seconds>=3600


def test_403_is_not_looped_or_concealed():
    error=HTTPError("https://example.test",403,"Forbidden",{},io.BytesIO(b"{}"))
    with patch("btc_edge.market.urllib.request.urlopen",side_effect=error) as api:
        with pytest.raises(HTTPError):
            fetch_page("BTCUSDT",0,300000)
    assert api.call_count==1
